"""NO LIVE MONEY: Linux Demo GET-only service and offline state operations."""
import argparse
from contextlib import ExitStack, closing
from datetime import datetime
from decimal import Decimal
import json
import os
from pathlib import Path
import signal
import shutil
import sqlite3
from time import time

from factorlab.errors import DataQualityError
from .demo_cli import load_bound_credential, open_session
from .demo_credentials import DemoCredentialStore, DemoCredentialStoreError
from .demo_market_feed import DemoMarketFeed, FeedPaused
from .demo_runtime import DemoRuntime, SMOKE_CAMPAIGN, operator_lock, state_root
from .demo_strategy_supervisor import MarketClient
from .demo_supervisor import DemoSupervisor
from .observability import RequestCounts, worker_metrics
from .protocol import ExchangeError, SafetyBlocked


CONFIG_ERROR = 78
MIN_FREE_BYTES = 256 * 1024 * 1024


def readonly(path):
    return sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)


def durable_state(root, runtime):
    authorization = runtime.continuous_authorization()
    if authorization is not None and authorization['state'] in {'STOPPED', 'HALTED'}:
        return authorization['state']
    if runtime._get('stop_epoch') and not runtime._get('active') and not runtime._get('continuous_window'):
        return 'STOPPED'
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
    return 'PAUSED' if paused else 'READ_ONLY'


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
        self.market_refusals = dict(STALE_POLL=0, R13_QUOTE_GAP=0)
        self.supervisor = None

    def watermarks(self):
        return self.feed.watermarks() if self.feed is not None else (None, None)

    def pause(self, reason):
        if reason in self.market_refusals:
            self.market_refusals[reason] += 1
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
        if self.session is None:
            # Identity/configuration refusals are fatal; transient network errors still
            # reach DemoSupervisor's existing recovery backoff.
            try:
                self.session, _ = self.stack.enter_context(open_session(
                    self.root, self.store, self.runtime, self.requests))
            except SafetyBlocked:
                raise ValueError('BOUND_ACCOUNT_CONFIGURATION_REFUSED') from None
        try:
            # TODO: reuse incremental GET recovery once late-fill/commission coverage
            # is proven; the existing full journal recovery must not be shortened here.
            account = self.session.recover()
            self.status = 'RECOVERING'
            if not account['reconciled']:
                self.last_exception = 'RECONCILIATION_FAILED'
                raise FeedPaused('RECONCILIATION_FAILED')
            if account['unknown']:
                self.last_exception = 'ORDER_UNKNOWN'
                raise FeedPaused('ORDER_UNKNOWN')
            if self.feed is None:
                self.feed = self.stack.enter_context(closing(DemoMarketFeed(
                    MarketClient(self.requests), SilentConsumer(), state_path=self.root / 'cloud-feed.sqlite3',
                    consumer_id='cloud-get-only-v1', interval=self.interval, catch_up=True)))
            result = self.feed.poll()
            self.status, self.reason = ('PAUSED', 'PAUSED') if desired == 'PAUSED' else ('READ_ONLY', None)
            return result
        except ExchangeError:
            self.last_exception = 'NETWORK_EXCHANGE_ERROR'
            raise
        except SafetyBlocked:
            self.last_exception = 'RECONCILIATION_FAILED'
            raise FeedPaused('RECONCILIATION_FAILED') from None
        except OSError:
            self.last_exception = 'NETWORK_INTERRUPTED'
            raise
        except DataQualityError:
            self.last_exception = 'MARKET_STALE_OR_INVALID'
            raise

    def snapshot(self):
        from .demo_cloud_storage import storage_metrics
        facts = self.session.adapter.facts if self.session is not None else None
        metrics = (worker_metrics(self.session, self.feed, self.runtime, self.requests)
                   if self.session is not None and self.feed is not None else {})
        quote = self.watermarks()[1]
        api = metrics.get('api') or self.requests.snapshot()
        endpoints = api['endpoints']
        count = sum(row['count'] for row in endpoints)
        failures = sum(sum(row['errors'].values()) for row in endpoints)
        return dict(version=1, pid=os.getpid(), heartbeat_at=time(), status=self.status, reason=self.reason,
            mode='OFFLINE' if self.offline else 'GET_ONLY', desired=durable_state(self.root, self.runtime),
            last_valid_market_at=metrics.get('last_valid_market_at'),
            last_reconciled_at=self.session.last_reconciled_at if self.session else None,
            market_batch_age_seconds=None if quote is None else max(0, time() - quote / 1000),
            requests=dict(self.requests), api=api,
            last_recovery_seconds=metrics.get('last_recovery_seconds'),
            safety_events=dict(self.market_refusals,
                CLOCK_SKEW=sum(row['errors'].get('CLOCK_SKEW', 0) for row in endpoints)),
            request_failure_rate=failures / count if count else None,
            unknown_count=metrics.get('unknown_count'),
            positions=[row for row in facts.position_details() if row['quantity'] != 0] if facts else None,
            equity=facts.usdt() if facts else None,
            last_exception=self.last_exception, journals=metrics.get('journals'),
            **budget_metrics(self.runtime, metrics.get('campaign')),
            **storage_metrics(self.root))


def run(root, *, offline=False, seconds=None, poll_seconds=10, min_free_bytes=MIN_FREE_BYTES):
    from .demo_cloud_storage import atomic_json, validate_state
    with ExitStack() as stack:
        stack.enter_context(operator_lock(root, allow_quarantined=True))
        validate_state(root, min_free_bytes=min_free_bytes, recover=True)
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
        starts = json.loads(previous.read_text())['starts'] + 1 if previous.exists() else 1

        def publish(_=None):
            payload = feed.snapshot()
            payload.update(started_at=started_at, starts=starts, restart_count=starts - 1)
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


def health(root, *, max_age=90):
    payload = json.loads((root / 'cloud-health.json').read_text())
    elapsed = time() - payload['heartbeat_at']
    fresh = 0 <= elapsed <= max_age
    alive = fresh and payload['status'] not in {'EXITED', 'STOPPED', 'HALTED'}
    ready = alive and (payload['mode'] == 'OFFLINE' or (
        payload['status'] == 'READ_ONLY' and payload['market_batch_age_seconds'] is not None
        and payload['market_batch_age_seconds'] + elapsed <= max_age
        and payload['last_reconciled_at'] is not None
        and 0 <= time() - datetime.fromisoformat(payload['last_reconciled_at']).timestamp() <= max_age))
    print(json.dumps(dict(payload, alive=alive, ready=ready,
        offline_heartbeat_healthy=ready and payload['mode'] == 'OFFLINE',
        get_only_healthy=ready and payload['mode'] == 'GET_ONLY',
        cloud_get_only_accepted=False, demo_execution_ready=False)))
    return 0 if ready else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('run', 'health', 'backup', 'restore'))
    parser.add_argument('--state-root', type=Path)
    parser.add_argument('--offline', action='store_true')
    parser.add_argument('--seconds', type=float)
    parser.add_argument('--poll-seconds', type=float, default=10)
    parser.add_argument('--min-free-bytes', type=int, default=MIN_FREE_BYTES)
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
            return health(root)
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
