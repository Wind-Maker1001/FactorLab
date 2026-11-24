"""Closed-bar Binance Demo Grid worker with isolated GET-only previews."""
import argparse
from contextlib import ExitStack, closing, nullcontext
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D, ROUND_CEILING, ROUND_FLOOR
import json
import os
from pathlib import Path
import shutil
import signal
import sqlite3
import tempfile
from time import time

from factorlab.errors import DataQualityError
from factorlab.strategies.grid import GridConfig
from factorlab.strategies.grid_execution import GridExecution, GridRules
from factorlab.trading import Market
from factorlab.trading.realtime import PersistenceConflict
from factorlab.trading.storage import PaperStore, decode
from .demo_cloud_runtime import (
    CONFIG_ERROR, MIN_FREE_BYTES, budget_metrics, detect_systemd_unit, diagnostic_info,
    next_start_count, recent_order_summaries,
)
from .demo_credentials import DemoCredentialStore, DemoCredentialStoreError
from .demo_grid_bridge import minimum_quantity
from .demo_market_feed import DemoMarketFeed, FeedPaused, INTERVALS, SOURCE
from .demo_runtime import (
    DemoRuntime, MAX_NOTIONAL, MAX_QUANTITY, durable_control_state, journal_id,
    open_grid_session, operator_lock, private_binding, restore_grid, saved_grid_deployment,
    state_root,
)
from .demo_session import DEMO
from .demo_preflight import _book
from .demo_strategy_supervisor import MarketClient, StrategyControl
from .demo_supervisor import DemoSupervisor
from .protocol import ExchangeError, SafetyBlocked, credential_account_id
from .observability import RequestCounts, worker_metrics


def grid_factory(settings, *, new_deployment=False):
    """Bind the initial range once; subsequent starts restore the saved deployment."""
    def create(session, deployment_id):
        saved = None if new_deployment else saved_grid_deployment(session.controller.store)
        if saved is not None:
            deployment = decode(json.loads(saved))['deployment']
            if deployment['deployment_id'] == deployment_id:
                grid = restore_grid(deployment)
                if settings is not None and grid.config != GridConfig(**settings):
                    raise SafetyBlocked('persisted Grid configuration differs')
                return grid
        status = session.status()
        if not status['reconciled'] or status['positions'] or status['open_orders'] or status['unknown']:
            raise SafetyBlocked('initial Grid deployment requires a reconciled flat dedicated account')
        session.refresh_rules()
        book = session._fresh_book()
        filters = session.gateway.rules.filters
        lot, tick = filters['LOT_SIZE'], D(filters['PRICE_FILTER']['tickSize'])
        price = book['askPrice']
        lower = (price * D('.99') / tick).to_integral_value(rounding=ROUND_FLOOR) * tick
        upper = (price * D('1.01') / tick).to_integral_value(rounding=ROUND_CEILING) * tick
        config = GridConfig(**settings) if settings is not None else GridConfig(
            lower=float(lower), upper=float(upper), levels=1, initial_price=float(price),
            initial_inventory=0, quantity=1, max_inventory=1, max_cash_committed=7, breakout='pause')
        rules = GridRules(MAX_QUANTITY, D(lot['stepSize']), tick, D(lot['minQty']),
                          D(filters['MIN_NOTIONAL']['notional']))
        if (max(minimum_quantity(rules, D(str(config.lower))),
                minimum_quantity(rules, D(str(config.upper)))) > MAX_QUANTITY
                or MAX_QUANTITY * max(D(str(config.upper)), price) > MAX_NOTIONAL):
            raise SafetyBlocked('minimum legal Grid pair exceeds Demo budgets')
        return GridExecution(config, rules, deployment_id=deployment_id,
            account_id=session.adapter.account_id, market=Market.BINANCE_USDM,
            instrument_id='NVDAUSDT', initial_equity=status['balances']['walletBalance'],
            continuous_capacity=True)
    return create


class GridConsumer:
    """Bars advance Grid; GET reports alone advance economic state."""
    def __init__(self, bridge, control, *, authority=lambda: True,
                 clock=lambda: datetime.now(timezone.utc), proposal_observer=None, ensure_campaign=None,
                 request_counts=None, rotate=None):
        self.request_counts = request_counts
        self.ensure_campaign = ensure_campaign
        self.proposal_observer = proposal_observer
        self.rotate = rotate
        self.activated_at = getattr(bridge, 'activated_at', None)
        self.bridge, self.control, self.authority, self.clock = bridge, control, authority, clock
        self.supervisor = None
        self.eligible_after = clock()
        self.live_bar = False
        self.initialized = False
        self.running = False
        self.reason = 'STARTING'
        self.last_bar = None
        self.latest_book = None
        self._exit_dispatch_active = False

    def gate(self):
        if self.supervisor is not None and self.supervisor.collection is not None:
            self.supervisor.collection.check()
        if self.control.desired() != 'RUNNING':
            raise SafetyBlocked('Grid control prohibits submission')
        if not self.authority():
            raise SafetyBlocked('AUTHORIZATION_REQUIRED')
        if not self.running:
            raise SafetyBlocked('FRESH_CLOSED_BAR_REQUIRED')
        if self._exit_dispatch_active:
            if not self.bridge.grid.closed_by_breakout:
                raise SafetyBlocked('FRESH_CLOSED_BAR_REQUIRED')
            return
        if not self.live_bar:
            raise SafetyBlocked('FRESH_CLOSED_BAR_REQUIRED')

    def recovered(self):
        """Complete consumer recovery before withdrawing any unsubmitted proposals."""

    def _refresh_reason(self, desired, allowed):
        if not allowed:
            self.reason = 'PAUSED' if desired == 'PAUSED' else 'AUTHORIZATION_REQUIRED'
            return
        exit_status = self.bridge.breakout_exit_status(desired=desired, authorized=allowed)
        if exit_status is not None and exit_status['phase'] != 'FLAT_CONFIRMED':
            self.reason = exit_status['reason'] or exit_status['phase']
        else:
            self.reason = None if self.live_bar else 'FRESH_CLOSED_BAR_REQUIRED'

    def before_poll(self):
        desired = self.control.desired()
        if desired == 'STOPPED':
            self.reason = 'STOPPED'
            self.supervisor.stop()
            return
        try:
            with self.request_counts.measure('grid_recovery') if self.request_counts is not None else nullcontext():
                collection = None
                if self.supervisor is not None and self.supervisor.collection is not None:
                    collection = self.supervisor.feed.collection
                if collection is None:
                    self.bridge.recover()
                else:
                    self.bridge.recover(collection=collection)
            self.recovered()
            if desired == 'RUNNING' and self.ensure_campaign is not None:
                self.ensure_campaign()
            if desired == 'RUNNING' and self.rotate is not None:
                replacement = self.rotate(self.bridge, running=self._rotation_allowed)
                if replacement is not self.bridge:
                    self.bridge = replacement
                    self.activated_at = replacement.activated_at
                    self.running = self.live_bar = False
                    self.last_bar = None
                    self.latest_book = None
        except SafetyBlocked as error:
            raise FeedPaused(str(error)) from error
        desired = self.control.desired()
        allowed = desired == 'RUNNING' and self.authority()
        if not self.initialized or not allowed or not self.running:
            self.bridge.withdraw_unsubmitted()
            self.live_bar = False
            self.eligible_after = self.clock()
        self.initialized = True
        self.running = allowed
        self._refresh_reason(desired, allowed)

    def _rotation_allowed(self):
        return (self.control.desired() == 'RUNNING'
                and (self.supervisor is None or not self.supervisor.stopped.is_set()))

    def require_fresh_bar(self):
        """A long owner interruption cannot promote its accumulated bars."""
        self.bridge.withdraw_unsubmitted()
        self.live_bar = False
        self.eligible_after = self.clock()
        self.reason = 'FRESH_CLOSED_BAR_REQUIRED'

    def disconnect(self):
        self.bridge.ready = False
        self.running = self.live_bar = False
        self.latest_book = None
        self.eligible_after = self.clock()
        self.reason = 'RECONCILE_REQUIRED'

    def historical_bar(self, bar):
        self.live_bar = False
        self.reason = 'FRESH_CLOSED_BAR_REQUIRED'
        return self._accept_bar(bar, historical=True)

    def closed_bar(self, bar):
        return self._accept_bar(bar, historical=False)

    def _accept_bar(self, bar, *, historical):
        if (not bar.complete or bar.source != SOURCE or bar.symbol != 'NVDAUSDT'
                or bar.closed_at > self.clock()):
            raise FeedPaused('INVALID_CLOSED_BAR')
        if self.control.desired() == 'STOPPED':
            raise FeedPaused('STOPPED')
        if self.activated_at is not None and bar.closed_at <= self.activated_at:
            return False
        identity = f'{bar.source}:{bar.interval_seconds}:{int(bar.closed_at.timestamp())}'
        # Eligibility is not part of source identity: replay after a crash retains
        # the original decision rather than promoting a historical bar to live.
        store = self.bridge.controller.store
        source_key = self.bridge.prefix + 'source:' + identity
        saved = store.event_payload(source_key)
        live = (not historical and self.running and self.control.desired() == 'RUNNING'
                and bar.closed_at > self.eligible_after
                and self.clock() - bar.closed_at < timedelta(seconds=bar.interval_seconds))
        event = dict(id='bar-' + str(int(bar.closed_at.timestamp())),
            time=bar.closed_at.replace(tzinfo=None).isoformat(), price=str(bar.close), volume=0,
            completed_bar=True, high=str(bar.high), low=str(bar.low), close=str(bar.close),
            source_bar=dict(opened_at=bar.opened_at.isoformat(), open=str(bar.open), volume=str(bar.volume)))
        if saved is not None:
            original = decode(json.loads(saved))
            expected = dict(original)
            expected.pop('reseed_withdrawn', None)
            if expected != event:
                raise ValueError('Grid market event identity conflict')
            event = original
            live = False
        elif live:
            event['reseed_withdrawn'] = True
        if self.proposal_observer is not None:
            self.proposal_observer.begin(identity, bar, live, self.clock())
        self.bridge.observe(event, dispatch=False, source_id=identity)
        if self.proposal_observer is not None:
            self.proposal_observer.capture(identity, self.bridge.grid, self.clock(),
                                          eligible=event.get('reseed_withdrawn', False))
        if not live:
            self.bridge.withdraw_unsubmitted()
        else:
            self.live_bar = True
            self._refresh_reason(self.control.desired(), self.running)
        self.last_bar = bar.closed_at
        return saved is None

    def set_latest_book(self, raw):
        # The feed already checked freshness against its exchange-time range.
        # Parse here; execution checks freshness against its own clock at use.
        self.latest_book = _book(dict(raw))
        self.bridge.last_book = self.latest_book

    def _step_breakout_exit(self):
        if not self.running or self.control.desired() != 'RUNNING' or self.bridge.preview:
            return None
        if not self.bridge.grid.closed_by_breakout:
            return None
        self._exit_dispatch_active = True
        try:
            self.gate()
            result = self.bridge.step_breakout_exit(book=self.latest_book)
        except SafetyBlocked as error:
            self.reason = str(error)
            raise FeedPaused(self.reason) from error
        finally:
            self._exit_dispatch_active = False
        self._refresh_reason(self.control.desired(), self.running)
        return result

    def maintain(self, raw):
        self.set_latest_book(raw)
        return self._step_breakout_exit()

    def quote(self, quote):
        previous = self.bridge.gateway.state.quotes.get(quote.instrument_id)
        if previous is None or quote.observed_at >= previous.observed_at:
            self.bridge.controller.quote(quote)
        if not self.running or self.control.desired() != 'RUNNING':
            return None
        if self.bridge.preview:
            return None
        if self.bridge.grid.closed_by_breakout:
            return self._step_breakout_exit()
        if not self.live_bar:
            return None
        try:
            self.gate()
            return self.bridge.dispatch()
        except SafetyBlocked as error:
            self.reason = str(error)
            raise FeedPaused(self.reason) from error

    def status(self):
        known = self.bridge.gateway.state.orders
        account = self.bridge.session.status()
        effective = ('STOPPED' if self.control.desired() == 'STOPPED' else
                     'PAUSED' if not self.running or self.reason else self.bridge.grid.state)
        breakout_exit = self.bridge.breakout_exit_status(
            desired=self.control.desired(), authorized=self.running or self.authority())
        return dict(reason=self.reason, effective=effective, deployment=self.bridge.grid.deployment(),
            grid_state=self.bridge.grid.state, grid_reason=self.bridge.grid.reason,
            breakout_exit=breakout_exit,
            last_bar=self.last_bar, inventory=(sum(
                (position['quantity'] for position in account['positions']), D(0))
                if account['positions'] is not None else None),
            reservations=[asdict(order.intent) for order in self.bridge.grid.active
                          if order.intent.client_order_id not in known],
            account=account)


def add_arguments(parser):
    parser.add_argument('action', choices=('start', 'pause', 'resume', 'stop', 'status'))
    parser.add_argument('--execute', action='store_true', help='use existing persistent Demo authorization')
    parser.add_argument('--new-deployment', action='store_true',
                        help='initialize a new campaign Grid on a reconciled flat account; start --execute only')
    parser.add_argument('--interval', choices=INTERVALS, default='1m')
    parser.add_argument('--poll-seconds', type=float, default=2)
    parser.add_argument('--max-backfill', type=int, default=120)
    parser.add_argument('--seconds', type=float)
    parser.add_argument('--config', type=Path, help='GridConfig JSON; immutable after first deployment')
    parser.add_argument('--min-free-bytes', type=int, default=MIN_FREE_BYTES)


def _check_storage_and_integrity(directory, *, min_free_bytes, strict_permissions):
    from .demo_cloud_storage import _DB_SUFFIXES, _SIDECARS, _integrity, validate_state
    if min_free_bytes < 0:
        raise ValueError('min-free-bytes cannot be negative')
    if strict_permissions:
        validate_state(directory, min_free_bytes=min_free_bytes, recover=True)
        return
    if min_free_bytes and shutil.disk_usage(directory).free < min_free_bytes:
        raise SafetyBlocked('insufficient free space for Demo storage')
    for root_dir, _, names in os.walk(directory, followlinks=False):
        for name in sorted(names):
            if name == 'session.lock' or name.endswith(_SIDECARS):
                continue
            path = Path(root_dir) / name
            with path.open('rb') as stream:
                header = stream.read(16)
            if path.suffix in _DB_SUFFIXES or header == b'SQLite format 3\x00':
                _integrity(path, recover=True)


def _safe_storage_metrics(directory, *, strict_permissions):
    from .demo_cloud_storage import _DB_SUFFIXES, _SIDECARS, storage_metrics
    if strict_permissions:
        return storage_metrics(directory)
    databases, sizes = [], {}
    for root_dir, _, names in os.walk(directory, followlinks=False):
        for name in sorted(names):
            path = Path(root_dir) / name
            try:
                sizes[path] = path.stat().st_size
            except FileNotFoundError:
                if name.endswith(_SIDECARS):
                    continue
                raise
            if name == 'session.lock' or name.endswith(_SIDECARS):
                continue
            with path.open('rb') as stream:
                header = stream.read(16)
            if path.suffix in _DB_SUFFIXES or header == b'SQLite format 3\x00':
                databases.append(path)
    records = {}
    for path in sorted(databases):
        record = {'database_bytes': sizes[path]}
        for suffix in _SIDECARS:
            record[suffix[1:] + '_bytes'] = sizes.get(Path(str(path) + suffix), 0)
        record['total_bytes'] = sum(record.values())
        records[path.relative_to(directory).as_posix()] = record
    usage = shutil.disk_usage(directory)
    return {
        'databases': records,
        'database_bytes': sum(r['database_bytes'] for r in records.values()),
        'sidecar_bytes': sum(r['total_bytes'] - r['database_bytes'] for r in records.values()),
        'state_bytes': sum(sizes.values()),
        'disk_free_bytes': usage.free,
        'disk_bytes': usage.total,
    }


def _write_health_json(path, payload, *, strict_permissions):
    from .demo_cloud_storage import atomic_json
    if strict_permissions:
        atomic_json(path, payload)
        return
    encoded = (json.dumps(payload, allow_nan=False, sort_keys=True, separators=(',', ':'), default=str) + '\n').encode('utf-8')
    descriptor, temporary = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
    temporary = Path(temporary)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _is_auth_exchange_error(error):
    return isinstance(error, ExchangeError) and (
        error.category in {'AUTH', 'SIGNATURE'} or error.status in {401, 403}
    )


def _is_recoverable_startup_safety_error(error):
    text = str(error)
    return text.startswith('STALE_QUOTE') or text in {'RECONCILE_REQUIRED', 'STALE_ACCOUNT'}


def _preflight_local_state(directory, runtime, store, settings, *, execute, new_deployment,
                           require_saved_grid=False):
    if settings is not None:
        GridConfig(**settings)
    binding = private_binding(store)
    credential = store.read()
    if credential is None:
        raise SafetyBlocked('saved Demo credential missing; run setup')
    if credential.environment is not DEMO:
        raise SafetyBlocked('Demo credentials required')
    if credential_account_id(DEMO, credential.key) != binding['account_id']:
        raise SafetyBlocked('persisted Demo account binding mismatch')
    continuous_auth = runtime.continuous_authorization() if execute else None
    campaign = dict(id='grid-preview', prefix='preview') if not execute else runtime.active_campaign()
    automatic = execute and continuous_auth is not None
    if execute:
        if automatic:
            runtime._validate_continuous(binding, continuous_auth)
            if new_deployment:
                raise SafetyBlocked('continuous Grid deployments rotate automatically after a reconciled breakout')
        else:
            if campaign is None or campaign['smoke']:
                raise SafetyBlocked('no persistent Demo authorization; run start-continuous')
            runtime.authority(binding, campaign)
    journal = directory / journal_id(binding['account_id'])
    controller_path = journal / 'controller.sqlite3'
    if controller_path.exists():
        with closing(PaperStore(controller_path, continuous_capacity=True)) as controller_store:
            saved = saved_grid_deployment(controller_store)
            if saved is not None:
                deployment = decode(json.loads(saved))
                if new_deployment:
                    if campaign is not None and deployment['campaign'] == campaign['id']:
                        raise SafetyBlocked('current campaign already has a Grid deployment; reuse it')
                else:
                    if not runtime.grid_deployment_matches(controller_store, deployment, campaign, continuous_auth):
                        raise SafetyBlocked('persisted Grid deployment or Demo campaign differs')
                    grid = restore_grid(deployment['deployment'])
                    if settings is not None and grid.config != GridConfig(**settings):
                        raise SafetyBlocked('persisted Grid configuration differs')
                return binding
    if require_saved_grid:
        raise SafetyBlocked('the bound Grid deployment is missing from transferred state')
    return binding


class GridSupervisorRuntime:
    """Lazy, recoverable Grid execution feed sharing DemoSupervisor's existing backoff loop."""

    def __init__(self, *, root, directory, path, runtime, control, store, requests, stack,
                 args, mode, settings, started_at, starts, sqlite_integrity_checked_at,
                 strict_permissions, min_free_bytes):
        self.root = root
        self.directory = directory
        self.path = path
        self.runtime = runtime
        self.control = control
        self.store = store
        self.requests = requests
        self.stack = stack
        self.args = args
        self.mode = mode
        self.execute = bool(args.execute)
        self.settings = settings
        self.interval = args.interval
        self.seconds = INTERVALS[args.interval]
        self.started_at = started_at
        self.starts = starts
        self.sqlite_integrity_checked_at = sqlite_integrity_checked_at
        self.strict_permissions = strict_permissions
        self.min_free_bytes = min_free_bytes
        self.systemd_unit = detect_systemd_unit()
        self.consumer = None
        self.feed = None
        self._collection = None
        self.initial_collection = None
        self.collection_requests = None
        self.supervisor = None
        self.status = 'STARTING'
        self.reason = 'STARTING'
        self.account_recovered = False
        self.last_exception = None
        self.refusals = dict(STALE_ACCOUNT=0, STALE_POLL=0, R13_QUOTE_GAP=0)
        self.recovery_attempts = 0
        self.consecutive_failures = 0
        self.last_failure_at = None
        self.last_recovered_at = None
        self.startup_recovered = False
        self._pending_startup_error = None

    @property
    def bridge(self):
        return self.consumer.bridge if self.consumer is not None else None

    @property
    def collection(self):
        return self._collection

    @collection.setter
    def collection(self, value):
        self._collection = value
        if self.feed is not None:
            self.feed.collection = value

    def watermarks(self):
        return self.feed.watermarks() if self.feed is not None else (None, None)

    def _effective_desired(self):
        if self.execute:
            runtime_stop = durable_control_state(self.runtime)
            if runtime_stop is not None:
                return runtime_stop
        return self.control.desired()

    def _has_saved_deployment(self):
        binding = private_binding(self.store)
        controller_path = self.directory / journal_id(binding['account_id']) / 'controller.sqlite3'
        if not controller_path.exists():
            return False
        with closing(PaperStore(controller_path, continuous_capacity=True)) as controller_store:
            return saved_grid_deployment(controller_store) is not None

    def _open(self):
        if self.bridge is not None:
            return
        if self.execute and self.control.desired() == 'PAUSED' and not self._has_saved_deployment():
            raise FeedPaused('PAUSED')
        attempt = ExitStack()
        consumer = None

        def gate():
            consumer.gate()

        try:
            bridge = attempt.enter_context(open_grid_session(
                self.root, self.store,
                grid_factory(self.settings, new_deployment=self.args.new_deployment),
                new_deployment=self.args.new_deployment, preview=not self.execute,
                continuous=True, request_counts=self.requests, dispatch_gate=gate,
                runtime=self.runtime, ensure_on_open=False))

            def authority():
                if not self.execute:
                    return True
                current = consumer.bridge
                campaign = self.runtime.active_campaign()
                if (campaign is None or campaign['smoke'] or self._effective_desired() != 'RUNNING'
                        or self.runtime.post_count(campaign['id']) >= campaign['orders']
                        or current.session.receipt_error or current.session.write_uncertain):
                    return False
                store = current.controller.store
                deployment = decode(json.loads(saved_grid_deployment(store)))
                return (current.grid.deployment_id == deployment['deployment']['deployment_id']
                        and self.runtime.grid_deployment_matches(
                            store, deployment, campaign, self.runtime.continuous_authorization()))

            consumer = GridConsumer(bridge, self.control, authority=authority,
                clock=bridge.session._now, ensure_campaign=bridge.ensure_campaign,
                request_counts=self.requests,
                rotate=bridge.rotate_if_terminal if self.execute and self.settings is None else None)
            if self.execute:
                from .demo_strategy_release import ReleaseConsumer
                consumer = ReleaseConsumer(consumer, self.directory, attempt)
            client = MarketClient(self.requests)
            initial_bars = max(1, bridge.grid.config.atr_window if bridge.grid.config.mode == 'atr' else 1)
            feed = attempt.enter_context(closing(DemoMarketFeed(
                client, consumer, state_path=self.path,
                consumer_id='grid-' + self.mode + ':' + bridge.feed_deployment_id,
                interval=self.args.interval, max_backfill=self.args.max_backfill,
                initial_bars=initial_bars, catch_up=True,
                exchange_time=bridge.session.client.demo_time,
                receipt_clock=bridge.session._now)))
            from .demo_market_collection import DemoBookCollection, collection_client
            reader, collection_requests = collection_client(self.requests, MarketClient)
            reader.share_demo_clock(bridge.session.client)
            collection = DemoBookCollection(reader, poll_seconds=self.args.poll_seconds,
                clock=reader.monotonic, on_acquired=collection_requests.acquired,
                exchange_time=reader.demo_time)
            if self.supervisor is not None:
                feed.collection = collection
                collection.start()
        except Exception as error:
            attempt.close()
            if _is_auth_exchange_error(error):
                self.last_exception = error.category
                raise SafetyBlocked(f'Demo API authentication rejected ({error.category})') from None
            if isinstance(error, SafetyBlocked):
                if _is_recoverable_startup_safety_error(error):
                    raise FeedPaused(str(error)) from error
                raise
            raise
        self.stack.enter_context(attempt.pop_all())
        self.consumer = consumer
        self.feed = feed
        self.collection_requests = collection_requests
        self.initial_collection = collection
        if self.supervisor is not None:
            self.collection = collection
            self.consumer.supervisor = self.supervisor
            self.supervisor.collection = collection
            self.supervisor.collection_requests = collection_requests

    def open_or_defer(self):
        """Attempt one initial open; hand transient network/market errors to DemoSupervisor."""
        try:
            self._open()
        except (SafetyBlocked, DemoCredentialStoreError, PersistenceConflict, sqlite3.DatabaseError):
            raise
        except (DataQualityError, FeedPaused, OSError) as error:
            self._pending_startup_error = error
            self.status = 'RECOVERING'
            self.reason = str(error) if isinstance(error, FeedPaused) else getattr(error, 'category', type(error).__name__)
            self.last_exception = self.reason

    def _stop_if_requested(self):
        desired = self._effective_desired()
        if desired in {'STOPPED', 'HALTED'}:
            self.status, self.reason = desired, desired
            self.supervisor.stop()
            return True
        return False

    def before_poll(self):
        if self._stop_if_requested():
            return
        self.account_recovered = False
        if self.execute and self._effective_desired() == 'RUNNING':
            from .demo_portfolio_consumer import DemoPortfolioConsumer
            DemoPortfolioConsumer(self.bridge.session, self.runtime, self.directory).poll()
        try:
            self.consumer.before_poll()
        except ExchangeError as error:
            if _is_auth_exchange_error(error):
                self.last_exception = error.category
                raise SafetyBlocked(f'Demo API authentication rejected ({error.category})') from None
            raise
        except FeedPaused as error:
            if self._stop_if_requested():
                self.last_exception = str(error)
                return
            raise
        account = self.bridge.session.status()
        self.account_recovered = bool(account['reconciled'] and not account['unknown'])

    def poll(self, *, before_delivery=None):
        if self._stop_if_requested():
            return None
        if self.min_free_bytes and shutil.disk_usage(self.directory).free < self.min_free_bytes:
            self.last_exception = 'DISK_SPACE_LOW'
            raise ValueError('DISK_SPACE_LOW')
        if self._pending_startup_error is not None:
            error, self._pending_startup_error = self._pending_startup_error, None
            raise error
        self._open()
        result = self.feed.poll(before_delivery=before_delivery)
        if result is None:
            return None
        if self.consecutive_failures or not self.startup_recovered:
            self.last_recovered_at = datetime.now(timezone.utc).isoformat()
            self.startup_recovered = True
        self.consecutive_failures = 0
        desired = self._effective_desired()
        if desired in {'STOPPED', 'HALTED'}:
            self.status, self.reason = desired, desired
        elif desired == 'PAUSED' or not self.consumer.running:
            self.status = 'PAUSED'
            self.reason = self.consumer.reason or 'PAUSED'
        else:
            self.status = 'RUNNING'
            self.reason = self.consumer.reason
        return result

    def pause(self, reason):
        if reason in self.refusals:
            self.refusals[reason] += 1
        if reason != 'STOPPED':
            self.last_exception = reason
            self.recovery_attempts += 1
            self.consecutive_failures += 1
            self.last_failure_at = datetime.now(timezone.utc).isoformat()
            self.account_recovered = False
            self.status = 'RECOVERING' if self.bridge is None else 'PAUSED'
            self.reason = reason
        else:
            self.status, self.reason = 'PAUSED', 'STOPPED'
        if self.feed is not None:
            self.feed.pause(reason)

    def on_pause(self):
        self.account_recovered = False
        if self.consumer is not None:
            self.consumer.disconnect()

    def on_recovery_delay(self):
        if self.consumer is not None:
            self.consumer.require_fresh_bar()

    def control_payload(self, base=None):
        payload = dict(base or {})
        payload.update(mode=self.mode, requests=self.requests)
        if self.consumer is not None and self.bridge is not None and self.feed is not None:
            payload.update(self.consumer.status(),
                metrics=worker_metrics(self.bridge.session, self.feed, self.runtime, self.requests))
        else:
            payload.update(
                reason=self.reason,
                effective=self.status,
                desired=self._effective_desired(),
                last_exception=self.last_exception,
                recovery=self._recovery_snapshot(),
            )
        return payload

    def _recovery_snapshot(self):
        state = ('RECOVERING' if self.consecutive_failures
            else 'RECOVERED' if self.last_recovered_at is not None else 'IDLE')
        return dict(
            state=state,
            consecutive_failures=self.consecutive_failures,
            recovery_attempts=self.recovery_attempts,
            last_failure_at=self.last_failure_at,
            last_failure_reason=self.last_exception,
            last_recovered_at=self.last_recovered_at,
            startup_recovered=self.startup_recovered,
        )

    def health_snapshot(self):
        mode_label = 'EXECUTE' if self.execute else 'PREVIEW'
        desired = self._effective_desired()
        session = self.bridge.session if self.bridge is not None else None
        consumer_status = self.consumer.status() if self.consumer is not None else None
        account = consumer_status['account'] if consumer_status is not None else (
            session.status() if session is not None else None)
        metrics = (worker_metrics(session, self.feed, self.runtime, self.requests)
                   if session is not None and self.feed is not None else {})
        quote = self.watermarks()[1]
        api = metrics.get('api') or self.requests.snapshot()
        endpoints = api['endpoints']
        count = sum(row['count'] for row in endpoints)
        failures = sum(sum(row['errors'].values()) for row in endpoints)
        account_ok = bool(self.account_recovered and account is not None
                          and account['reconciled'] and not account['unknown'])
        facts = session.adapter.facts if (account_ok and session is not None) else None
        equity = facts.usdt() if facts is not None else None
        nav_usdt = None
        if equity and equity['walletBalance'] is not None and equity['unrealizedProfit'] is not None:
            nav_usdt = equity['walletBalance'] + equity['unrealizedProfit']
        grid_info = None
        if consumer_status is not None:
            grid_info = dict(
                deployment_id=consumer_status['deployment']['deployment_id'],
                state=consumer_status['grid_state'],
                grid_reason=consumer_status['grid_reason'],
                breakout_exit=consumer_status.get('breakout_exit'),
                effective=consumer_status['effective'],
                reason=consumer_status['reason'],
                running=self.consumer.running,
                live_bar=self.consumer.live_bar,
                last_bar=consumer_status['last_bar'],
                inventory=consumer_status['inventory'],
                reservations_count=len(consumer_status['reservations']),
            )
        unknown_orders = account['unknown'] if account is not None else None
        unknown_count = len(unknown_orders) if unknown_orders is not None else metrics.get('unknown_count')
        last_reconciled = session.last_reconciled_at if session is not None else None
        if isinstance(last_reconciled, datetime):
            last_reconciled = last_reconciled.isoformat()
        exchange_now = session._now() if session is not None else None
        account_age = None if facts is None else max(
            (exchange_now - facts.observed_at).total_seconds(),
            session.client.monotonic() - facts.read_started_monotonic,
        )
        return dict(
            version=1,
            pid=os.getpid(),
            heartbeat_at=time(),
            started_at=self.started_at,
            starts=self.starts,
            restart_count=self.starts - 1,
            sqlite_integrity_checked_at=self.sqlite_integrity_checked_at,
            systemd_unit=self.systemd_unit,
            status=self.status,
            reason=self.reason,
            mode=mode_label,
            desired=desired,
            last_valid_market_at=metrics.get('last_valid_market_at'),
            last_reconciled_at=last_reconciled,
            account_recovered=account_ok,
            market_batch_age_seconds=None if quote is None else exchange_now.timestamp() - quote / 1000,
            account_age_seconds=account_age,
            requests=dict(self.requests),
            api=api,
            last_recovery_seconds=session.last_recovery_seconds if session is not None else None,
            safety_events=dict(self.refusals,
                CLOCK_SKEW=sum(row['errors'].get('CLOCK_SKEW', 0) for row in endpoints)),
            request_failure_rate=failures / count if count else None,
            unknown_count=unknown_count,
            unknown_orders=unknown_orders,
            recent_orders=recent_order_summaries(account['orders']) if account is not None else None,
            open_orders=account['open_orders'] if account_ok else None,
            positions=[row for row in facts.position_details() if row['quantity'] != 0] if facts is not None else None,
            equity=equity,
            nav_usdt=nav_usdt,
            grid=grid_info,
            strategy_release=consumer_status.get('strategy_release') if consumer_status else None,
            diagnostic=diagnostic_info(self.runtime),
            recovery=self._recovery_snapshot(),
            last_exception=self.last_exception,
            journals=metrics.get('journals'),
            **budget_metrics(self.runtime, metrics.get('campaign')),
            **_safe_storage_metrics(self.directory, strict_permissions=self.strict_permissions),
        )


def _stopped_health_snapshot(directory, runtime, requests, *, mode_label, desired,
                             started_at, starts, sqlite_integrity_checked_at, strict_permissions):
    api = requests.snapshot()
    return dict(
        version=1,
        pid=os.getpid(),
        heartbeat_at=time(),
        started_at=started_at,
        starts=starts,
        restart_count=starts - 1,
        sqlite_integrity_checked_at=sqlite_integrity_checked_at,
        systemd_unit=detect_systemd_unit(),
        status=desired,
        reason=desired,
        mode=mode_label,
        desired=desired,
        last_valid_market_at=None,
        last_reconciled_at=None,
        account_recovered=False,
        market_batch_age_seconds=None,
        requests=dict(requests),
        api=api,
        last_recovery_seconds=None,
        safety_events=dict(STALE_ACCOUNT=0, STALE_POLL=0, R13_QUOTE_GAP=0, CLOCK_SKEW=0),
        request_failure_rate=None,
        unknown_count=None,
        unknown_orders=None,
        recent_orders=None,
        open_orders=None,
        positions=None,
        equity=None,
        nav_usdt=None,
        grid=None,
        diagnostic=diagnostic_info(runtime),
        recovery=dict(state='IDLE', consecutive_failures=0, recovery_attempts=0,
            last_failure_at=None, last_failure_reason=None, last_recovered_at=None,
            startup_recovered=False),
        last_exception=None,
        journals=None,
        **budget_metrics(runtime),
        **_safe_storage_metrics(directory, strict_permissions=strict_permissions),
    )


def run(args, *, root=None, credentials=None):
    if args.new_deployment and (args.action != 'start' or not args.execute):
        raise SafetyBlocked('--new-deployment requires start --execute')
    strict_permissions = root is None
    root = state_root() if root is None else Path(root)
    mode = 'execute' if args.execute else 'dry-run'
    mode_label = 'EXECUTE' if args.execute else 'PREVIEW'
    directory = root if args.execute else root / 'grid-preview'
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / 'grid-supervisor.sqlite3'
    if args.action != 'start':
        if not path.exists():
            raise SafetyBlocked('Grid supervisor has not been started')
        with closing(StrategyControl(path)) as control:
            if args.action != 'status':
                desired = dict(pause='PAUSED', resume='RUNNING', stop='STOPPED')[args.action]
                if args.execute:
                    from .demo_cloud_storage import assert_source_controls_available

                    # Share the write order with Grid promotion and order reservation.
                    with closing(DemoRuntime(directory)) as runtime, runtime.db:
                        runtime.db.execute('BEGIN IMMEDIATE')
                        assert_source_controls_available(directory)
                        control.request(desired)
                else:
                    control.request(desired)
            print(json.dumps(control.snapshot(), default=str), flush=True)
        return 0
    min_free_bytes = getattr(args, 'min_free_bytes', MIN_FREE_BYTES)
    settings = json.loads(args.config.read_text(encoding='utf-8')) if args.config is not None else None
    configuration = dict(mode=mode, interval=args.interval, source=SOURCE, max_backfill=args.max_backfill,
                         settings=settings)
    requests = RequestCounts()
    with ExitStack() as stack:
        stack.enter_context(operator_lock(directory))
        _check_storage_and_integrity(directory, min_free_bytes=min_free_bytes,
            strict_permissions=strict_permissions)
        sqlite_integrity_checked_at = time()
        runtime = stack.enter_context(closing(DemoRuntime(directory)))
        health_path = directory / 'cloud-health.json'
        started_at = time()
        starts = next_start_count(health_path)
        # Durable account-wide STOP/HALTED must be honored before control init, credentials or network.
        runtime_stop = durable_control_state(runtime) if args.execute else None
        if runtime_stop in {'STOPPED', 'HALTED'}:
            if path.exists():
                with closing(StrategyControl(path)) as existing_control:
                    try:
                        existing_control.desired()
                    except SafetyBlocked:
                        pass
                    else:
                        existing_control.report(dict(mode=mode, status=runtime_stop, desired=runtime_stop,
                            reason=runtime_stop, requests=dict(requests)))
            health_payload = _stopped_health_snapshot(directory, runtime, requests,
                mode_label=mode_label, desired=runtime_stop, started_at=started_at, starts=starts,
                sqlite_integrity_checked_at=sqlite_integrity_checked_at,
                strict_permissions=strict_permissions)
            _write_health_json(health_path, health_payload, strict_permissions=strict_permissions)
            print(json.dumps(dict(mode=mode, status=runtime_stop, desired=runtime_stop,
                reason=runtime_stop, requests=dict(requests)), default=str), flush=True)
            return 0
        control = stack.enter_context(closing(StrategyControl(path, configuration)))
        if control.desired() == 'STOPPED':
            payload = dict(mode=mode, status='STOPPED', desired='STOPPED',
                reason='STOPPED', requests=dict(requests))
            control.report(payload)
            health_payload = _stopped_health_snapshot(directory, runtime, requests,
                mode_label=mode_label, desired='STOPPED', started_at=started_at, starts=starts,
                sqlite_integrity_checked_at=sqlite_integrity_checked_at,
                strict_permissions=strict_permissions)
            _write_health_json(health_path, health_payload, strict_permissions=strict_permissions)
            print(json.dumps(payload, default=str), flush=True)
            return 0
        store = credentials or DemoCredentialStore()
        _preflight_local_state(directory, runtime, store, settings,
            execute=args.execute, new_deployment=args.new_deployment)
        worker = GridSupervisorRuntime(
            root=root, directory=directory, path=path, runtime=runtime, control=control,
            store=store, requests=requests, stack=stack, args=args, mode=mode, settings=settings,
            started_at=started_at, starts=starts,
            sqlite_integrity_checked_at=sqlite_integrity_checked_at,
            strict_permissions=strict_permissions, min_free_bytes=min_free_bytes,
        )
        worker.open_or_defer()

        def publish_health():
            _write_health_json(health_path, worker.health_snapshot(), strict_permissions=strict_permissions)

        def report(raw):
            base = json.loads(raw)
            base['feed_reason'] = base['reason']
            payload = worker.control_payload(base)
            control.report(payload)
            publish_health()
            print(json.dumps(payload, default=str), flush=True)

        supervisor = DemoSupervisor(worker, poll_seconds=args.poll_seconds,
            max_backoff=max(60, args.poll_seconds), report=report,
            before_poll=worker.before_poll, on_pause=worker.on_pause, request_counts=requests,
            collection=worker.initial_collection, collection_requests=worker.collection_requests,
            on_recovery_delay=worker.on_recovery_delay, recover_after_bars=True)
        worker.supervisor = supervisor
        if worker.consumer is not None:
            worker.consumer.supervisor = supervisor
        original = {sig: signal.signal(sig, lambda *_: supervisor.stop())
                    for sig in (signal.SIGINT, signal.SIGTERM)}
        try:
            publish_health()
            supervisor.run(seconds=args.seconds)
        finally:
            if worker.consumer is not None:
                worker.consumer.running = False
            final_desired = worker._effective_desired()
            final_status = final_desired if final_desired in {'STOPPED', 'HALTED'} else 'EXITED'
            worker.status = final_status
            if final_status in {'STOPPED', 'HALTED'}:
                worker.reason = final_status
            payload = worker.control_payload(dict(
                status=final_status,
                desired=final_desired,
                watermarks=worker.watermarks(),
            ))
            control.report(payload)
            publish_health()
            print(json.dumps(payload, default=str), flush=True)
            for sig, handler in original.items():
                signal.signal(sig, handler)
    return 0


def _refusal_reason(error):
    if isinstance(error, DemoCredentialStoreError):
        return 'CREDENTIAL_ERROR'
    if isinstance(error, sqlite3.DatabaseError):
        return 'SQLITE_CORRUPT_OR_UNAVAILABLE'
    if isinstance(error, PersistenceConflict):
        return 'CONFIGURATION_CONFLICT'
    if isinstance(error, SafetyBlocked):
        text = str(error)
        if 'another Demo session is active' in text:
            return 'SESSION_LOCK_CONTENDED'
        if 'quarantined' in text or 'GET-only recovery' in text:
            return 'RESTORE_QUARANTINE'
        if 'SQLite integrity' in text:
            return 'SQLITE_INTEGRITY_FAILED'
        if 'authentication rejected' in text:
            return 'EXCHANGE_AUTH_REJECTED'
        if 'binding' in text or 'credential' in text or 'configuration mismatch' in text:
            return 'IDENTITY_OR_CREDENTIAL_ERROR'
        if 'insufficient free space' in text:
            return 'DISK_SPACE_LOW'
        return 'SAFETY_BLOCKED'
    if isinstance(error, ValueError) and str(error) == 'DISK_SPACE_LOW':
        return 'DISK_SPACE_LOW'
    return 'STATE_OR_CONFIGURATION_ERROR'


def run_cli(args, *, root=None, credentials=None):
    try:
        return run(args, root=root, credentials=credentials)
    except (SafetyBlocked, DemoCredentialStoreError, PersistenceConflict,
            ValueError, OSError, sqlite3.DatabaseError) as error:
        print(json.dumps(dict(event='grid_start_refused', reason=_refusal_reason(error))), flush=True)
        return CONFIG_ERROR


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    add_arguments(parser)
    return run_cli(parser.parse_args(argv))


if __name__ == '__main__':
    raise SystemExit(main())
