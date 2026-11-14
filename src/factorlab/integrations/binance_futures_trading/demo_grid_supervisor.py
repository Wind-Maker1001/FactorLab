"""Closed-bar Binance Demo Grid worker with isolated GET-only previews."""
import argparse
from contextlib import closing
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D, ROUND_CEILING, ROUND_FLOOR
import json
from pathlib import Path
import signal

from factorlab.strategies.grid import GridConfig
from factorlab.strategies.grid_execution import GridExecution, GridRules
from factorlab.trading import Market
from factorlab.trading.storage import decode
from .demo_credentials import DemoCredentialStore
from .demo_grid_bridge import minimum_quantity
from .demo_market_feed import DemoMarketFeed, FeedPaused, INTERVALS, SOURCE
from .demo_runtime import (MAX_NOTIONAL, MAX_QUANTITY, open_grid_session, restore_grid,
                           saved_grid_deployment, state_root)
from .demo_strategy_supervisor import MarketClient, StrategyControl
from .demo_supervisor import DemoSupervisor
from .protocol import SafetyBlocked


def grid_factory(settings):
    """Bind the initial range once; subsequent starts restore the saved deployment."""
    def create(session, deployment_id):
        saved = saved_grid_deployment(session.controller.store)
        if saved is not None:
            deployment = decode(json.loads(saved))['deployment']
            if deployment['deployment_id'] != deployment_id:
                raise SafetyBlocked('Grid deployment belongs to another campaign')
            grid = restore_grid(deployment)
            if settings is not None and grid.config != GridConfig(**settings):
                raise SafetyBlocked('persisted Grid configuration differs')
            return grid
        session.recover()
        status = session.status()
        if not status['reconciled'] or status['positions'] or status['open_orders'] or status['unknown']:
            raise SafetyBlocked('initial Grid deployment requires a reconciled flat dedicated account')
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
                 clock=lambda: datetime.now(timezone.utc)):
        self.bridge, self.control, self.authority, self.clock = bridge, control, authority, clock
        self.supervisor = None
        self.eligible_after = clock()
        self.live_bar = False
        self.initialized = False
        self.running = False
        self.reason = 'STARTING'
        self.last_bar = None

    def gate(self):
        if self.control.desired() != 'RUNNING':
            raise SafetyBlocked('Grid control prohibits submission')
        if not self.authority():
            raise SafetyBlocked('AUTHORIZATION_REQUIRED')
        if not self.running or not self.live_bar:
            raise SafetyBlocked('FRESH_CLOSED_BAR_REQUIRED')

    def before_poll(self):
        desired = self.control.desired()
        if desired == 'STOPPED':
            self.reason = 'STOPPED'
            self.supervisor.stop()
            return
        try:
            self.bridge.recover()
        except SafetyBlocked as error:
            raise FeedPaused(str(error)) from error
        allowed = desired == 'RUNNING' and self.authority()
        if not self.initialized or not allowed or not self.running:
            self.bridge.withdraw_unsubmitted()
            self.live_bar = False
            self.eligible_after = self.clock()
        self.initialized = True
        self.running = allowed
        self.reason = (None if self.live_bar else 'FRESH_CLOSED_BAR_REQUIRED') if allowed else (
            'PAUSED' if desired == 'PAUSED' else 'AUTHORIZATION_REQUIRED')

    def disconnect(self):
        self.bridge.ready = False
        self.running = self.live_bar = False
        self.eligible_after = self.clock()
        self.reason = 'RECONCILE_REQUIRED'

    def closed_bar(self, bar):
        if (not bar.complete or bar.source != SOURCE or bar.symbol != 'NVDAUSDT'
                or bar.closed_at > self.clock()):
            raise FeedPaused('INVALID_CLOSED_BAR')
        if self.control.desired() == 'STOPPED':
            raise FeedPaused('STOPPED')
        identity = f'{bar.source}:{bar.interval_seconds}:{int(bar.closed_at.timestamp())}'
        # Eligibility is not part of source identity: replay after a crash retains
        # the original decision rather than promoting a historical bar to live.
        store = self.bridge.controller.store
        source_key = self.bridge.prefix + 'source:' + identity
        saved = store.event_payload(source_key)
        live = (self.running and self.control.desired() == 'RUNNING'
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
        self.bridge.observe(event, dispatch=False, source_id=identity)
        if not live:
            self.bridge.withdraw_unsubmitted()
        else:
            self.live_bar = True
            self.reason = None
        self.last_bar = bar.closed_at
        return saved is None

    def quote(self, quote):
        self.bridge.controller.quote(quote)
        if not self.running or self.control.desired() != 'RUNNING':
            return None
        if self.bridge.preview or not self.live_bar:
            return None
        try:
            self.gate()
            return self.bridge.dispatch()
        except SafetyBlocked as error:
            self.reason = str(error)
            raise FeedPaused(self.reason) from error

    def status(self):
        known = self.bridge.gateway.state.orders
        effective = ('STOPPED' if self.control.desired() == 'STOPPED' else
                     'PAUSED' if not self.running or self.reason else self.bridge.grid.state)
        return dict(reason=self.reason, effective=effective, deployment=self.bridge.grid.deployment(),
            grid_state=self.bridge.grid.state, grid_reason=self.bridge.grid.reason,
            last_bar=self.last_bar, inventory=sum(
                (position.quantity for position in self.bridge.controller.account().positions), D(0)),
            reservations=[asdict(order.intent) for order in self.bridge.grid.active
                          if order.intent.client_order_id not in known],
            account=self.bridge.session.status())


def add_arguments(parser):
    parser.add_argument('action', choices=('start', 'pause', 'resume', 'stop', 'status'))
    parser.add_argument('--execute', action='store_true', help='use existing persistent Demo authorization')
    parser.add_argument('--interval', choices=INTERVALS, default='1m')
    parser.add_argument('--poll-seconds', type=float, default=2)
    parser.add_argument('--max-backfill', type=int, default=120)
    parser.add_argument('--seconds', type=float)
    parser.add_argument('--config', type=Path, help='GridConfig JSON; immutable after first deployment')


def run(args, *, root=None, credentials=None):
    root = state_root() if root is None else root
    mode = 'execute' if args.execute else 'dry-run'
    directory = root if args.execute else root / 'grid-preview'
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / 'grid-supervisor.sqlite3'
    if args.action != 'start':
        if not path.exists():
            raise SafetyBlocked('Grid supervisor has not been started')
        with closing(StrategyControl(path)) as control:
            if args.action != 'status':
                control.request(dict(pause='PAUSED', resume='RUNNING', stop='STOPPED')[args.action])
            print(json.dumps(control.snapshot(), default=str), flush=True)
        return 0
    settings = json.loads(args.config.read_text(encoding='utf-8')) if args.config is not None else None
    configuration = dict(mode=mode, interval=args.interval, source=SOURCE, max_backfill=args.max_backfill,
                         settings=settings)
    requests = dict(GET=0, POST=0, DELETE=0)
    with closing(StrategyControl(path, configuration)) as control:
        control.request('RUNNING')
        consumer = None
        def gate():
            consumer.gate()
        with open_grid_session(root, credentials or DemoCredentialStore(), grid_factory(settings),
                preview=not args.execute, continuous=True, request_counts=requests,
                dispatch_gate=gate) as bridge:
            def authority():
                if not args.execute:
                    return True
                campaign = bridge.runtime.active_campaign()
                return (campaign is not None and not campaign['smoke']
                    and bridge.grid.deployment_id == campaign['prefix'] + '-g'
                    and bridge.runtime.post_count(campaign['id']) < campaign['orders']
                    and not bridge.session.receipt_error and not bridge.session.write_uncertain)
            consumer = GridConsumer(bridge, control, authority=authority)
            client = MarketClient(requests)
            initial_bars = max(1, bridge.grid.config.atr_window if bridge.grid.config.mode == 'atr' else 1)
            with closing(DemoMarketFeed(client, consumer, state_path=path, consumer_id='grid-' + mode,
                    interval=args.interval, max_backfill=args.max_backfill, initial_bars=initial_bars)) as feed:
                def report(raw):
                    payload = json.loads(raw)
                    payload['feed_reason'] = payload['reason']
                    payload.update(consumer.status(), mode=mode, requests=requests)
                    control.report(payload)
                    print(json.dumps(payload, default=str), flush=True)
                supervisor = DemoSupervisor(feed, poll_seconds=args.poll_seconds, report=report,
                    before_poll=consumer.before_poll, on_pause=consumer.disconnect)
                consumer.supervisor = supervisor
                signal.signal(signal.SIGINT, lambda *_: supervisor.stop())
                signal.signal(signal.SIGTERM, lambda *_: supervisor.stop())
                try:
                    supervisor.run(seconds=args.seconds)
                finally:
                    consumer.running = False
                    payload = dict(mode=mode, status='EXITED', desired=control.desired(),
                                   watermarks=feed.watermarks(), requests=requests, **consumer.status())
                    control.report(payload)
                    print(json.dumps(payload, default=str), flush=True)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    add_arguments(parser)
    return run(parser.parse_args(argv))


if __name__ == '__main__':
    raise SystemExit(main())
