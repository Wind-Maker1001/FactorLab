"""NO LIVE MONEY: Linux Demo GET-only service and offline state operations."""
import argparse
from contextlib import ExitStack, closing
from datetime import datetime, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
import signal
import shutil
import sqlite3
import subprocess
from time import time

from .demo_cli import load_bound_credential, open_session
from .demo_credentials import DemoCredentialStore, DemoCredentialStoreError
from .adapter import ACCOUNT_READ_SECONDS
from .demo_market_feed import DemoMarketFeed, FeedPaused
from .demo_runtime import (DemoRuntime, SMOKE_CAMPAIGN, durable_control_state,
                           operator_lock, state_root)
from .demo_strategy_supervisor import MarketClient
from .demo_supervisor import DemoSupervisor
from .observability import RequestCounts, worker_metrics
from .protocol import SafetyBlocked


CONFIG_ERROR = 78
MIN_FREE_BYTES = 256 * 1024 * 1024


def readonly(path):
    return sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)


def durable_state(root, runtime, *, default='READ_ONLY'):
    runtime_state = durable_control_state(runtime)
    if runtime_state is not None:
        return runtime_state
    paused = False
    for path in (root / 'grid-supervisor.sqlite3', root / 'strategy-execute.sqlite3'):
        if path.exists():
            with closing(readonly(path)) as db:
                row = db.execute("SELECT name FROM sqlite_master WHERE name='strategy_control'").fetchone()
                if row:
                    desired = db.execute('SELECT desired FROM strategy_control WHERE id=1').fetchone()
                    if desired:
                        if desired[0] == 'STOPPED':
                            return 'STOPPED'
                        paused = paused or desired[0] == 'PAUSED'
    return 'PAUSED' if paused else default


def detect_systemd_unit():
    if os.name == 'nt':
        return None
    configured = os.environ.get('FACTORLAB_DEMO_SYSTEMD_UNIT')
    if configured:
        return configured
    if not os.environ.get('INVOCATION_ID'):
        return None
    try:
        for line in Path('/proc/self/cgroup').read_text(encoding='utf-8').splitlines():
            segment = line.rsplit('/', 1)[-1].strip()
            if segment.endswith('.service'):
                return segment
    except OSError:
        return None
    return None


def systemd_unit_state(unit):
    if not unit or os.name == 'nt':
        return None
    try:
        result = subprocess.run(
            ['systemctl', 'show', unit,
             '--property=Id,LoadState,ActiveState,SubState,MainPID,NRestarts,Result,ExecMainStatus,ExecMainStartTimestamp'],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return dict(unit=unit, available=False)
    if result.returncode != 0 or not result.stdout.strip():
        return dict(unit=unit, available=False)
    properties = {}
    for line in result.stdout.splitlines():
        if '=' in line:
            key, value = line.split('=', 1)
            properties[key] = value
    def parse_int(name):
        value = properties.get(name, '')
        return int(value) if value.isdigit() else None
    return dict(
        unit=properties.get('Id') or unit,
        available=True,
        load_state=properties.get('LoadState') or None,
        active_state=properties.get('ActiveState') or None,
        sub_state=properties.get('SubState') or None,
        main_pid=parse_int('MainPID'),
        n_restarts=parse_int('NRestarts'),
        result=properties.get('Result') or None,
        exec_main_status=parse_int('ExecMainStatus'),
        exec_main_start_timestamp=properties.get('ExecMainStartTimestamp') or None,
    )


def recent_order_summaries(orders, *, limit=10):
    if not isinstance(orders, dict):
        return None
    items = list(orders.items())[-limit:]
    return [dict(
        client_order_id=client_id,
        order_id=order.get('order_id'),
        side=order.get('side'),
        status=order.get('status'),
        quantity=order.get('quantity'),
        limit_price=order.get('limit_price'),
        filled_quantity=order.get('filled_quantity'),
        reduce_only=order.get('reduce_only'),
        reconcile_required=order.get('reconcile_required'),
        exchange_updated_at=order.get('exchange_updated_at'),
    ) for client_id, order in items]


def next_start_count(path):
    if path.exists():
        try:
            prior = json.loads(path.read_text(encoding='utf-8'))
            if isinstance(prior, dict) and type(prior.get('starts')) is int and prior['starts'] >= 0:
                return prior['starts'] + 1
        except (OSError, ValueError):
            pass
    return 1


def diagnostic_info(runtime):
    raw = runtime._get('continuous_diagnostic')
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return dict(reason=str(raw))


def budget_metrics(runtime, report=None):
    report = runtime.report(SMOKE_CAMPAIGN) if report is None else report
    authorization = report.get('USER_DEMO_AUTHORIZATION')
    campaign = {key: report[key] for key in (
        'enabled', 'expires_at', 'post_budget', 'posts_consumed', 'limits')}
    campaign['remaining_posts'] = (max(0, report['post_budget'] - report['posts_consumed'])
                                   if report['post_budget'] is not None else None)
    global_budget = None
    if authorization is not None:
        global_budget = {key: authorization[key] for key in (
            'state', 'max_loss', 'opening_equity', 'total_orders', 'total_notional', 'consumed')}
        global_budget['remaining_posts'] = max(0, authorization['total_orders'] - authorization['consumed']['posts'])
        global_budget['remaining_notional'] = str(max(Decimal(0), Decimal(authorization['total_notional'])
            - Decimal(authorization['consumed']['submitted_notional'])))
    return dict(campaign=campaign, global_budget=global_budget)


class SilentConsumer:
    def closed_bar(self, bar):
        pass

    def quote(self, quote):
        pass

    def historical_bar(self, bar):
        pass


class CloudFeed:
    """Lazy session creation lets existing backoff recover an unavailable network."""
    interval = '1m'

    def __init__(self, root, runtime, store, requests, stack, *, offline=False, min_free_bytes=MIN_FREE_BYTES):
        self.root, self.runtime, self.store = root, runtime, store
        self.requests, self.stack, self.offline = requests, stack, offline
        self.min_free_bytes = min_free_bytes
        self.session = self.feed = None
        self.status, self.reason = 'STARTING', None
        self.last_exception = None
        self.account_recovered = False
        self.refusals = dict(STALE_ACCOUNT=0, STALE_POLL=0, R13_QUOTE_GAP=0)
        self.supervisor = None
        self.systemd_unit = detect_systemd_unit()
        self.recovery_attempts = 0
        self.consecutive_failures = 0
        self.last_failure_at = None
        self.last_recovered_at = None
        self.startup_recovered = False

    def watermarks(self):
        return self.feed.watermarks() if self.feed is not None else (None, None)

    def pause(self, reason):
        if reason in self.refusals:
            self.refusals[reason] += 1
        if reason != 'STOPPED':
            self.last_exception = reason
            self.recovery_attempts += 1
            self.consecutive_failures += 1
            self.last_failure_at = datetime.now(timezone.utc).isoformat()
        self.status, self.reason = 'PAUSED', reason
        if self.feed is not None:
            self.feed.pause(reason)

    def poll(self):
        if shutil.disk_usage(self.root).free < self.min_free_bytes:
            self.last_exception = 'DISK_SPACE_LOW'
            raise ValueError('DISK_SPACE_LOW')
        desired = durable_state(self.root, self.runtime)
        if desired in {'STOPPED', 'HALTED'}:
            self.status, self.reason = desired, desired
            self.supervisor.stop()
            return dict(bars=0, quotes=0)
        if self.offline:
            self.status, self.reason = 'OFFLINE', None
            return dict(bars=0, quotes=0)
        self.account_recovered = False
        try:
            if self.session is None:
                # Identity/configuration refusals are fatal; transient errors reach backoff.
                try:
                    self.session, _ = self.stack.enter_context(open_session(
                        self.root, self.store, self.runtime, self.requests))
                except SafetyBlocked:
                    self.last_exception = 'BOUND_ACCOUNT_CONFIGURATION_REFUSED'
                    raise ValueError(self.last_exception) from None
            # TODO: reuse incremental GET recovery once late-fill/commission coverage
            # is proven; the existing full journal recovery must not be shortened here.
            account = self.session.recover()
            self.status = 'RECOVERING'
            if not account['reconciled']:
                raise FeedPaused('RECONCILIATION_FAILED')
            if account['unknown']:
                raise FeedPaused('ORDER_UNKNOWN')
            self.account_recovered = True
            if self.feed is None:
                self.feed = self.stack.enter_context(closing(DemoMarketFeed(
                    MarketClient(self.requests), SilentConsumer(), state_path=self.root / 'cloud-feed.sqlite3',
                    consumer_id='cloud-get-only-v1', interval=self.interval, catch_up=True)))
            result = self.feed.poll()
            if self.consecutive_failures or not self.startup_recovered:
                self.last_recovered_at = datetime.now(timezone.utc).isoformat()
                self.startup_recovered = True
            self.consecutive_failures = 0
            self.status, self.reason = ('PAUSED', 'PAUSED') if desired == 'PAUSED' else ('READ_ONLY', None)
            return result
        except SafetyBlocked:
            raise FeedPaused('RECONCILIATION_FAILED') from None

    def snapshot(self):
        from .demo_cloud_storage import storage_metrics
        facts = self.session.adapter.facts if self.account_recovered else None
        session_status = self.session.status() if self.session is not None else None
        metrics = (worker_metrics(self.session, self.feed, self.runtime, self.requests)
                   if self.session is not None and self.feed is not None else {})
        quote = self.watermarks()[1]
        api = metrics.get('api') or self.requests.snapshot()
        endpoints = api['endpoints']
        count = sum(row['count'] for row in endpoints)
        failures = sum(sum(row['errors'].values()) for row in endpoints)
        equity = facts.usdt() if facts else None
        nav_usdt = None
        if equity and equity['walletBalance'] is not None and equity['unrealizedProfit'] is not None:
            nav_usdt = equity['walletBalance'] + equity['unrealizedProfit']
        recovery_state = ('RECOVERING' if self.consecutive_failures
            else 'RECOVERED' if self.last_recovered_at is not None else 'IDLE')
        return dict(version=1, pid=os.getpid(), heartbeat_at=time(), status=self.status, reason=self.reason,
            systemd_unit=self.systemd_unit,
            mode='OFFLINE' if self.offline else 'GET_ONLY', desired=durable_state(self.root, self.runtime),
            last_valid_market_at=metrics.get('last_valid_market_at'),
            last_reconciled_at=self.session.last_reconciled_at if self.session else None,
            account_recovered=self.account_recovered,
            market_batch_age_seconds=None if quote is None else max(0, time() - quote / 1000),
            requests=dict(self.requests), api=api,
            last_recovery_seconds=self.session.last_recovery_seconds if self.session else None,
            safety_events=dict(self.refusals,
                CLOCK_SKEW=sum(row['errors'].get('CLOCK_SKEW', 0) for row in endpoints)),
            request_failure_rate=failures / count if count else None,
            unknown_count=metrics.get('unknown_count'),
            unknown_orders=session_status['unknown'] if session_status else None,
            recent_orders=recent_order_summaries(session_status['orders']) if session_status else None,
            open_orders=session_status['open_orders'] if session_status and self.account_recovered else None,
            positions=[row for row in facts.position_details() if row['quantity'] != 0] if facts else None,
            equity=equity, nav_usdt=nav_usdt,
            grid=None,
            diagnostic=diagnostic_info(self.runtime),
            recovery=dict(state=recovery_state, consecutive_failures=self.consecutive_failures,
                recovery_attempts=self.recovery_attempts, last_failure_at=self.last_failure_at,
                last_failure_reason=self.last_exception, last_recovered_at=self.last_recovered_at,
                startup_recovered=self.startup_recovered),
            last_exception=self.last_exception, journals=metrics.get('journals'),
            **budget_metrics(self.runtime, metrics.get('campaign')),
            **storage_metrics(self.root))


def run(root, *, offline=False, seconds=None, poll_seconds=10, min_free_bytes=MIN_FREE_BYTES):
    from .demo_cloud_storage import atomic_json, validate_state
    with ExitStack() as stack:
        stack.enter_context(operator_lock(root, allow_quarantined=True))
        validate_state(root, min_free_bytes=min_free_bytes, recover=True)
        sqlite_integrity_checked_at = time()
        runtime = stack.enter_context(closing(DemoRuntime(root)))
        # STOP must be observable without opening credentials or touching the network.
        desired = durable_state(root, runtime)
        store = None
        if not offline and desired not in {'STOPPED', 'HALTED'}:
            store = DemoCredentialStore()
            load_bound_credential(store)
        requests = RequestCounts()
        feed = CloudFeed(root, runtime, store, requests, stack, offline=offline, min_free_bytes=min_free_bytes)
        started_at = time()
        previous = root / 'cloud-health.json'
        starts = next_start_count(previous)

        def publish(_=None):
            payload = feed.snapshot()
            payload.update(started_at=started_at, starts=starts, restart_count=starts - 1,
                sqlite_integrity_checked_at=sqlite_integrity_checked_at)
            atomic_json(previous, payload)
            # The durable file has metrics; logs only carry bounded state classifications.
            print(json.dumps(dict(event='cloud_health', pid=payload['pid'], starts=starts, status=payload['status'],
                reason=payload['reason'], last_exception=payload['last_exception'])), flush=True)

        supervisor = DemoSupervisor(feed, poll_seconds=poll_seconds,
            max_backoff=max(60, poll_seconds), report=publish, request_counts=requests)
        feed.supervisor = supervisor
        original = {sig: signal.signal(sig, lambda *_: supervisor.stop())
                    for sig in (signal.SIGINT, signal.SIGTERM)}
        try:
            publish()
            supervisor.run(seconds=seconds)
        finally:
            feed.status = durable_state(root, runtime)
            if feed.status not in {'STOPPED', 'HALTED'}:
                feed.status = 'EXITED'
            publish()
            for sig, handler in original.items():
                signal.signal(sig, handler)
    return 0


def health(root, *, max_age=90, unit=None):
    payload = json.loads((root / 'cloud-health.json').read_text(encoding='utf-8'))
    now = time()
    elapsed = now - payload['heartbeat_at']
    fresh = 0 <= elapsed <= max_age
    target_unit = unit or payload.get('systemd_unit')
    systemd = systemd_unit_state(target_unit) if target_unit else None
    systemd_matches = True
    if systemd is not None and systemd.get('available'):
        systemd_matches = (
            systemd.get('active_state') == 'active'
            and systemd.get('sub_state') == 'running'
            and systemd.get('main_pid') == payload.get('pid')
        )
    desired = payload.get('desired')
    stop_or_halted = (
        payload['status'] in {'STOPPED', 'HALTED'}
        or desired in {'STOPPED', 'HALTED'}
    )
    alive = fresh and payload['status'] not in {'EXITED', 'STOPPED', 'HALTED'} and systemd_matches
    market_age = payload.get('market_batch_age_seconds')
    if market_age is not None:
        market_age += elapsed
    evidence_age_limit = max_age
    if payload['mode'] == 'EXECUTE':
        evidence_age_limit = min(max_age, ACCOUNT_READ_SECONDS)
        account_age = payload.get('account_age_seconds')
        if account_age is not None:
            account_age += elapsed
    else:
        reconciled_at = payload.get('last_reconciled_at')
        account_age = None if reconciled_at is None else now - datetime.fromisoformat(reconciled_at).timestamp()
    market_fresh = alive and market_age is not None and 0 <= market_age <= evidence_age_limit
    account_recovered = (alive and bool(payload.get('account_recovered')) and account_age is not None
        and 0 <= account_age <= evidence_age_limit)
    offline_healthy = alive and payload['mode'] == 'OFFLINE'
    get_only_healthy = (market_fresh and account_recovered
        and payload['mode'] == 'GET_ONLY' and payload['status'] == 'READ_ONLY')
    grid = payload.get('grid')
    campaign = payload.get('campaign') or {}
    unknown_count = payload.get('unknown_count')
    execute_healthy = (
        alive
        and payload['mode'] == 'EXECUTE'
        and not stop_or_halted
        and payload['status'] == 'RUNNING'
        and desired == 'RUNNING'
        and account_recovered
        and market_fresh
        and unknown_count == 0
        and bool(campaign.get('enabled'))
        and isinstance(grid, dict)
        and grid.get('state') == 'RUNNING'
        and bool(grid.get('running'))
    )
    dispatch_eligible = execute_healthy and bool(grid.get('live_bar'))
    demo_execution_ready = execute_healthy
    ready = offline_healthy or get_only_healthy or execute_healthy
    print(json.dumps(dict(payload,
        systemd=systemd,
        alive=alive, ready=ready,
        uptime_seconds=now - payload['started_at'] if alive else None,
        market_fresh=market_fresh, market_batch_age_seconds=market_age,
        account_recovered=account_recovered,
        positions=payload.get('positions') if account_recovered else None,
        equity=payload.get('equity') if account_recovered else None,
        nav_usdt=payload.get('nav_usdt') if account_recovered else None,
        open_orders=payload.get('open_orders') if account_recovered else None,
        offline_heartbeat_healthy=offline_healthy, get_only_healthy=get_only_healthy,
        execute_healthy=execute_healthy, dispatch_eligible=dispatch_eligible,
        cloud_get_only_accepted=False, demo_execution_ready=demo_execution_ready)))
    return 0 if ready else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('run', 'health', 'backup', 'restore'))
    parser.add_argument('--state-root', type=Path)
    parser.add_argument('--offline', action='store_true')
    parser.add_argument('--seconds', type=float)
    parser.add_argument('--poll-seconds', type=float, default=10)
    parser.add_argument('--min-free-bytes', type=int, default=MIN_FREE_BYTES)
    parser.add_argument('--unit')
    parser.add_argument('--destination', type=Path)
    parser.add_argument('--backup', type=Path)
    args = parser.parse_args(argv)
    if args.min_free_bytes < 0:
        parser.error('min-free-bytes cannot be negative')
    if args.state_root is not None:
        os.environ['FACTORLAB_DEMO_STATE_ROOT'] = str(args.state_root)
    if os.name != 'nt':
        os.umask(0o077)
    try:
        from .demo_cloud_storage import backup_state, restore_state
        if args.action == 'restore':
            if args.backup is None or args.destination is None:
                parser.error('restore requires --backup and --destination')
            restore_state(args.backup, args.destination)
            return 0
        if args.action == 'health':
            configured = os.environ.get('FACTORLAB_DEMO_STATE_ROOT')
            if configured:
                root = Path(configured)
                if not root.is_absolute():
                    raise SafetyBlocked('absolute state root required')
            elif os.name == 'nt':
                root = Path(os.environ['LOCALAPPDATA']) / 'FactorLab' / 'BinanceDemo'
            else:
                raise SafetyBlocked('explicit state root required')
            return health(root, unit=args.unit)
        root = state_root()
        if args.action == 'run':
            return run(root, offline=args.offline, seconds=args.seconds,
                       poll_seconds=args.poll_seconds, min_free_bytes=args.min_free_bytes)
        if args.destination is None:
            parser.error('backup requires --destination')
        backup_state(root, args.destination)
        return 0
    except (SafetyBlocked, DemoCredentialStoreError, ValueError, OSError, sqlite3.DatabaseError):
        print(json.dumps(dict(event='cloud_start_refused', reason='STATE_OR_CONFIGURATION_ERROR')), flush=True)
        return CONFIG_ERROR


if __name__ == '__main__':
    raise SystemExit(main())
