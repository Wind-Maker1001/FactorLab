"""Persistent closed-bar Demo strategy worker; defaults to GET-only dry-run."""
import argparse
from contextlib import closing
import json
import signal
import sqlite3

from factorlab.trading.realtime import PersistenceConflict
from factorlab.trading.strategy import DualMAStrategy
from .demo_cli import open_session
from .demo_credentials import DemoCredentialStore
from .demo_market_feed import DemoMarketFeed, FeedPaused, INTERVALS, SOURCE
from .demo_runtime import DemoRuntime, MAX_NOTIONAL, MAX_QUANTITY, operator_lock, state_root
from .demo_supervisor import DemoSupervisor
from .protocol import Environment, FuturesClient, ReadOnlyNetworkTransport, SafetyBlocked
from .strategy_runtime import DemoStrategyRuntime


class MarketClient(FuturesClient):
    def __init__(self, requests):
        super().__init__(environment=Environment.DEMO,
            transport=ReadOnlyNetworkTransport(permit_account_read=True, environment=Environment.DEMO))
        self.requests = requests

    def request(self, method, path, params=None, *, signed=True):
        self.requests[method] += 1
        return super().request(method, path, params, signed=signed)


class StrategyControl:
    """Small control record alongside feed cursors, not a trading journal."""
    def __init__(self, path, configuration=None):
        self.db = sqlite3.connect(path)
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('CREATE TABLE IF NOT EXISTS strategy_control (id INTEGER PRIMARY KEY, configuration TEXT, desired TEXT, report TEXT)')
        if configuration is not None:
            encoded = json.dumps(configuration, sort_keys=True)
            with self.db:
                self.db.execute("INSERT OR IGNORE INTO strategy_control VALUES (1, ?, 'RUNNING', '{}')", (encoded,))
            if self.db.execute('SELECT configuration FROM strategy_control WHERE id=1').fetchone()[0] != encoded:
                raise PersistenceConflict('strategy worker configuration changed')

    def desired(self):
        row = self.db.execute('SELECT desired FROM strategy_control WHERE id=1').fetchone()
        if row is None:
            raise SafetyBlocked('strategy has not been started')
        return row[0]

    def request(self, state):
        if state not in {'RUNNING', 'PAUSED', 'STOPPED'}:
            raise ValueError('invalid strategy control state')
        self.desired()
        with self.db:
            self.db.execute('UPDATE strategy_control SET desired=? WHERE id=1', (state,))

    def report(self, payload):
        with self.db:
            self.db.execute('UPDATE strategy_control SET report=? WHERE id=1', (json.dumps(payload, default=str),))

    def snapshot(self):
        self.desired()
        configuration, desired, report = self.db.execute('SELECT configuration, desired, report FROM strategy_control WHERE id=1').fetchone()
        return dict(configuration=json.loads(configuration), desired=desired, observed=json.loads(report))

    def close(self):
        self.db.close()


class StrategyConsumer:
    def __init__(self, runner, control, authority=lambda: True):
        self.runner, self.control, self.authority = runner, control, authority
        self.supervisor = None
        self.reason = None
        self.was_running = False

    def _control(self):
        desired = self.control.desired()
        if desired == 'STOPPED':
            self.runner.stop()
            self.reason = 'STOPPED'
            if self.supervisor is not None:
                self.supervisor.stop()
            return False
        allowed = desired == 'RUNNING' and self.authority()
        if not allowed:
            self.runner.pause()
            self.reason = 'PAUSED' if desired == 'PAUSED' else 'AUTHORIZATION_REQUIRED'
            self.was_running = False
        return allowed

    def before_poll(self):
        allowed = self._control()
        if self.control.desired() == 'STOPPED':
            return
        try:
            result = self.runner.recover()
        except SafetyBlocked as error:
            raise FeedPaused(str(error)) from error
        if not result.reconciled:
            self.reason = 'RECONCILE_REQUIRED'
            raise FeedPaused(self.reason)
        if allowed and not self.was_running:
            # Signals accumulated while paused cannot be executed on resume.
            self.runner.controller._atomic(lambda: self.runner.gateway.state.runtime.update(pending=None))
            self.runner.resume()
            self.was_running = True
        self.reason = None if allowed else self.reason

    def disconnect(self):
        self.runner.disconnect()
        self.was_running = False
        self.reason = 'RECONCILE_REQUIRED'

    def closed_bar(self, bar):
        if self.control.desired() == 'STOPPED':
            self._control()
            raise FeedPaused('STOPPED')
        accepted = self.runner.closed_bar(bar)
        runtime = self.runner.gateway.state.runtime
        if not accepted and runtime.get('reason') == 'BAR_SEQUENCE_GAP':
            raise FeedPaused('BAR_SEQUENCE_GAP')
        return accepted

    def quote(self, quote):
        if not self._control():
            return None
        try:
            return self.runner.quote(quote)
        except SafetyBlocked as error:
            raise FeedPaused(str(error)) from error


def run_worker(args, root, store, runtime, requests):
    mode = 'execute' if args.execute else 'dry-run'
    configuration = dict(mode=mode, interval=args.interval, source=SOURCE, max_backfill=args.max_backfill)
    state = root / ('strategy-' + mode + '.sqlite3')
    with closing(StrategyControl(state, configuration)) as control:
        control.request('RUNNING')
        campaign = runtime.active_campaign() if args.execute else None
        if args.execute and (campaign is None or campaign['smoke']):
            raise SafetyBlocked('current persistent Demo authorization required')
        journal_root = root if args.execute else root / 'strategy-preview'
        journal_root.mkdir(exist_ok=True)
        with open_session(journal_root, store, runtime, requests, campaign=campaign) as (session, expect):
            strategy = DualMAStrategy(source=SOURCE, interval_seconds=INTERVALS[args.interval])
            checkpoint = session.gateway.state.runtime
            prefix = campaign['prefix'] + '-' if campaign is not None else 'preview-'
            old_prefix = checkpoint.get('configuration', {}).get('client_id_prefix', prefix)
            saved = checkpoint.get('configuration')
            settings = dict(max_notional=MAX_NOTIONAL, max_quantity=MAX_QUANTITY, preview=not args.execute)
            previous_settings = settings if saved is None else {key: saved.get(key, False if key == 'preview' else None) for key in settings}
            def expect_order(client_id, side, quantity):
                if control.desired() != 'RUNNING' or not authority():
                    raise SafetyBlocked('strategy paused or authorization lost before submission')
                expect(client_id, side, quantity)

            runner = DemoStrategyRuntime(session, strategy, client_id_prefix=old_prefix, expect=expect_order,
                **previous_settings)
            if checkpoint.get('status') == 'STOPPED' or old_prefix != prefix or previous_settings != settings:
                runner.stop()
                runner.activate(client_id_prefix=prefix, **settings)

            def authority():
                active = runtime.active_campaign()
                return (not args.execute or active is not None and active['id'] == campaign['id']
                    and runtime.post_count(active['id']) < active['orders']
                    and not session.write_uncertain and not session.receipt_error)

            consumer = StrategyConsumer(runner, control, authority)
            client = MarketClient(requests)
            with closing(DemoMarketFeed(client, consumer, state_path=state,
                    consumer_id='dual-ma-' + mode, interval=args.interval,
                    max_backfill=args.max_backfill, initial_bars=strategy.history_limit)) as feed:
                def report(raw):
                    payload = json.loads(raw)
                    checkpoint = runner.gateway.state.runtime
                    payload.update(mode=mode, reason=consumer.reason or payload['reason'],
                        strategy={key: value for key, value in checkpoint.items() if key != 'bars'},
                        retained_bars=len(checkpoint['bars']), account=session.status(), requests=requests)
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
                    runner.pause()
                    control.report(dict(mode=mode, status='EXITED', desired=control.desired(),
                        watermarks=feed.watermarks(), strategy=runner.gateway.state.runtime,
                        account=session.status(), requests=requests))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('start', 'pause', 'resume', 'stop', 'status'))
    parser.add_argument('--execute', action='store_true', help='explicitly enable the existing persistent Demo authorization')
    parser.add_argument('--interval', choices=INTERVALS, default='1m')
    parser.add_argument('--poll-seconds', type=float, default=2)
    parser.add_argument('--max-backfill', type=int, default=120)
    parser.add_argument('--seconds', type=float)
    args = parser.parse_args(argv)
    root = state_root()
    mode = 'execute' if args.execute else 'dry-run'
    if args.command != 'start':
        path = root / ('strategy-' + mode + '.sqlite3')
        if not path.exists():
            raise SafetyBlocked('strategy has not been started')
        with closing(StrategyControl(path)) as control:
            if args.command != 'status':
                control.request(dict(pause='PAUSED', resume='RUNNING', stop='STOPPED')[args.command])
            print(json.dumps(control.snapshot(), default=str))
        return 0
    requests = dict(GET=0, POST=0, DELETE=0)
    with operator_lock(root), closing(DemoRuntime(root)) as runtime:
        run_worker(args, root, DemoCredentialStore(), runtime, requests)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
