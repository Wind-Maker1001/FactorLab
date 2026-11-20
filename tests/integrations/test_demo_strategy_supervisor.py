"""Persistent strategy control composed with real Demo runtime and offline REST."""
from contextlib import closing, contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
import json
from urllib.parse import parse_qs
from types import SimpleNamespace
from pathlib import Path
import subprocess
import sys

import pytest

from test_binance_demo_strategy_session import deployment, wired
from test_binance_demo_network import NetworkFixture
from factorlab.integrations.binance.client import Response
from factorlab.integrations.binance_futures_trading.demo_cli import open_session
from factorlab.integrations.binance_futures_trading.demo_market_feed import DemoMarketFeed, FeedPaused, SOURCE
from factorlab.integrations.binance_futures_trading.fake import NOW
from factorlab.integrations.binance_futures_trading.demo_runtime import DemoRuntime
from factorlab.integrations.binance_futures_trading.demo_strategy_supervisor import StrategyConsumer, StrategyControl
from factorlab.integrations.binance_futures_trading.protocol import Environment, ExchangeError, FuturesClient, OfflineTransport
from factorlab.integrations.binance_futures_trading.strategy_runtime import DemoStrategyRuntime
from factorlab.trading.paper import Quote
from factorlab.trading.realtime import PersistenceConflict
from factorlab.trading.strategy import ClosedBar, DualMAStrategy


class Market(OfflineTransport):
    def __init__(self):
        self.at = NOW + timedelta(milliseconds=100)
        self.boundary = int(NOW.timestamp()) // 3600 * 3600000
        self.start = self.boundary - 21 * 3600000

    def clock(self):
        return self.at.timestamp() + .005

    def client(self):
        return FuturesClient(environment=Environment.DEMO, transport=self,
            clock=self.clock, monotonic=self.clock)

    def __call__(self, request, timeout):
        assert request.method == 'GET'
        assert request.environment is Environment.DEMO
        assert request.headers == {}
        milliseconds = round(self.at.timestamp() * 1000)
        if request.path.endswith('/time'):
            payload = dict(serverTime=milliseconds + 5)
        elif request.path.endswith('/klines'):
            start = int(parse_qs(request.query)['startTime'][0])
            boundary = milliseconds // 3600000 * 3600000
            payload = []
            for opened in range(start, boundary, 3600000):
                price = '100' if opened < self.start + 20 * 3600000 else '110'
                payload.append([opened, price, price, price, price, '100', opened + 3599999])
        else:
            assert request.path.endswith('/bookTicker')
            payload = dict(symbol='NVDAUSDT', bidPrice='100', askPrice='100', time=milliseconds)
        return Response(200, {}, json.dumps(payload).encode())


def make_runner(session, expect, campaign, market, monkeypatch, *, preview=True):
    monkeypatch.setattr(session, '_now', lambda: market.at)
    monkeypatch.setattr(session.gateway, 'clock', lambda: market.at)
    monkeypatch.setattr(session.controller, 'clock', lambda: market.at)
    original = NetworkFixture.__call__
    def current_book(self, request, timeout):
        if request.path.endswith('/bookTicker'):
            payload = dict(symbol='NVDAUSDT', bidPrice='100', askPrice='100',
                time=int(market.at.timestamp() * 1000))
            return Response(200, {}, json.dumps(payload).encode())
        return original(self, request, timeout)
    monkeypatch.setattr(NetworkFixture, '__call__', current_book)
    return DemoStrategyRuntime(session, DualMAStrategy(max_weight=D('.004'), source=SOURCE),
        client_id_prefix=campaign['prefix'] + '-', expect=expect,
        max_notional=D(7), max_quantity=D('.04'), preview=preview)


def make_feed(root, market, consumer):
    return DemoMarketFeed(market.client(), consumer, state_path=root / 'strategy-feed.sqlite',
        consumer_id='strategy-preview', interval='1h', initial_bars=21,
        clock=lambda: market.at)


@pytest.mark.parametrize('response', ['accepted', 'rejected', 'unknown'])
def test_execution_feed_signal_has_one_persistent_submission(deployment, monkeypatch, response):
    root, store, campaign, fake = deployment
    market = Market()
    counts = dict(GET=0, POST=0, DELETE=0)
    if response == 'rejected':
        fake.response_next = Response(400, {}, b'{"code":-2019}')
    elif response == 'unknown':
        fake.response_next = Response(503, {}, b'{"code":-1000}')
    for restart in range(2):
        with closing(DemoRuntime(root)) as runtime:
            with open_session(root, store, runtime, counts, campaign=campaign) as (session, expect):
                runner = make_runner(session, expect, campaign, market, monkeypatch, preview=False)
                with closing(StrategyControl(root / 'control.sqlite', configuration=dict(mode='execute'))) as control:
                    consumer = StrategyConsumer(runner, control)
                    if restart and response == 'unknown':
                        with pytest.raises(ExchangeError, match='ORDER_NOT_FOUND'):
                            consumer.before_poll()
                        assert counts['POST'] == runtime.post_count(campaign['id']) == 1
                        assert not session.gateway.state.reconciled
                        continue
                    consumer.before_poll()
                    with closing(make_feed(root, market, consumer)) as feed:
                        feed.poll()
                        assert session.gateway.state.runtime['last_order']['intent']['order_type'].value == 'LIMIT'
                        assert counts['POST'] == 1
                        assert runtime.post_count(campaign['id']) == 1
                        feed.poll()
                        assert counts['POST'] == 1
                        if response != 'unknown':
                            consumer.before_poll()
                        if response == 'accepted' and not restart:
                            order = session.gateway.state.runtime['last_order']
                            key = order['client_order_id']
                            quantity = order['intent']['quantity']
                            fake.trade_update(key, '.01')
                            consumer.before_poll()
                            assert session.controller.account().positions[0].quantity == D('.01')
                            fake.trade_update(key, str(quantity - D('.01')))
                            consumer.before_poll()
                            assert session.controller.account().positions[0].quantity == quantity
                        assert counts['POST'] == 1
                    if response == 'accepted' and not restart:
                        market.at += timedelta(hours=1)
                        consumer.before_poll()
                        with closing(make_feed(root, market, consumer)) as resumed:
                            assert resumed.poll()['bars'] == 1
                        assert counts['POST'] == 1


@pytest.fixture
def worker_composition(deployment, monkeypatch):
    from factorlab.integrations.binance_futures_trading import demo_strategy_supervisor as module
    from factorlab.integrations.binance_futures_trading.demo_session import DemoSession
    root, store, campaign, fake = deployment
    market = Market()
    monkeypatch.setattr(DemoSession, '_now', lambda session: market.at)
    original = NetworkFixture.__call__

    def current_book(self, request, timeout):
        if request.path.endswith('/bookTicker'):
            return market(request, timeout)
        return original(self, request, timeout)

    monkeypatch.setattr(NetworkFixture, '__call__', current_book)
    monkeypatch.setattr(module, 'MarketClient', lambda requests: market.client())
    monkeypatch.setattr(module, 'DemoMarketFeed', lambda *args, **kwargs: DemoMarketFeed(
        *args, clock=lambda: market.at, **kwargs))
    return module, root, store, campaign, fake, market


@pytest.mark.parametrize('execute', [False, True])
def test_worker_composition_mode_and_persistent_status(worker_composition, monkeypatch, execute):
    module, root, store, campaign, fake, market = worker_composition
    reports, supervisors = [], []
    supervisor_type = module.DemoSupervisor

    class OnePoll(supervisor_type):
        def run(self, *, seconds=None):
            supervisors.append(self)
            report = self.report

            def completed(raw):
                reports.append(json.loads(raw))
                report(raw)
                self.stop()

            self.report = completed
            super().run(seconds=seconds)

    monkeypatch.setattr(module, 'DemoSupervisor', OnePoll)
    counts = dict(GET=0, POST=0, DELETE=0)
    args = SimpleNamespace(execute=execute, interval='1h', max_backfill=120,
        poll_seconds=.001, seconds=60)
    with closing(DemoRuntime(root)) as runtime:
        module.run_worker(args, root, store, runtime, counts)
        assert counts['POST'] == runtime.post_count(campaign['id']) == int(execute)
        assert counts['DELETE'] == 0
    posts = [params for method, path, params in fake.calls if method == 'POST']
    assert len(posts) == int(execute)
    if execute:
        post, = posts
        assert post['type'] == ['LIMIT'] and post['side'] == ['BUY']
        assert list(map(D, post['quantity'])) == [D('.04')]
        assert list(map(D, post['price'])) == [D('100')]
    report, = reports
    assert report['bars'] == 21
    assert report['quotes'] == 1
    assert report['reason'] is None
    supervisor, = supervisors
    import sqlite3
    with pytest.raises(sqlite3.ProgrammingError, match='closed'):
        supervisor.feed.db.execute('SELECT 1')
    mode = 'execute' if execute else 'dry-run'
    with closing(StrategyControl(root / ('strategy-' + mode + '.sqlite3'))) as control:
        snapshot = control.snapshot()
        assert snapshot['observed']['status'] == 'EXITED'
        assert snapshot['observed']['strategy']['last_signal']['direction'] == 'BUY'
        assert snapshot['observed']['watermarks'][0] == market.boundary
        assert snapshot['observed']['account']['reconciled']
        strategy = snapshot['observed']['strategy']
        assert len(strategy['bars']) == 21
        intent = strategy['last_order']['intent']
        assert intent['order_type'] == 'LIMIT' and intent['side'] == 'BUY'
        assert D(intent['quantity']) == D('.04')
        assert D(intent['limit_price']) == D('100')
        assert not intent['reduce_only']


@pytest.mark.parametrize('execute', [False, True])
def test_recovery_past_deadline_exits_before_market_poll(worker_composition, monkeypatch, execute):
    from factorlab.integrations.binance_futures_trading import demo_supervisor
    from factorlab.integrations.binance_futures_trading.demo_runtime import operator_lock
    import sqlite3
    module, root, store, campaign, fake, market = worker_composition
    elapsed, recovered, feeds = [0.], [], []
    monkeypatch.setattr(demo_supervisor, 'monotonic', lambda: elapsed[0])
    before_poll = StrategyConsumer.before_poll
    feed_type = module.DemoMarketFeed

    def recovery(consumer):
        before_poll(consumer)
        recovered.append(consumer.runner.session.status()['reconciled'])
        elapsed[0] = .02

    def feed(*args, **kwargs):
        opened = feed_type(*args, **kwargs)
        feeds.append(opened)
        monkeypatch.setattr(opened, 'poll', lambda: pytest.fail('poll started after deadline'))
        return opened

    monkeypatch.setattr(StrategyConsumer, 'before_poll', recovery)
    monkeypatch.setattr(module, 'DemoMarketFeed', feed)
    counts = dict(GET=0, POST=0, DELETE=0)
    args = SimpleNamespace(execute=execute, interval='1h', max_backfill=120,
        poll_seconds=.001, seconds=.01)
    with operator_lock(root), closing(DemoRuntime(root)) as runtime:
        module.run_worker(args, root, store, runtime, counts)
        assert runtime.post_count(campaign['id']) == 0
    assert recovered == [True]
    assert counts['POST'] == counts['DELETE'] == 0
    mode = 'execute' if execute else 'dry-run'
    with closing(StrategyControl(root / ('strategy-' + mode + '.sqlite3'))) as control:
        observed = control.snapshot()['observed']
        assert observed['status'] == 'EXITED'
        assert 'last_signal' not in observed['strategy']
        assert observed['strategy']['bars'] == []
        assert observed['watermarks'] == [None, None]
    opened, = feeds
    with pytest.raises(sqlite3.ProgrammingError, match='closed'):
        opened.db.execute('SELECT 1')
    with operator_lock(root):
        pass
    with operator_lock(root if execute else root / 'strategy-preview'):
        pass


def test_abrupt_process_exit_before_feed_ack_and_reopen(tmp_path):
    program = '''
import os, sys
from pathlib import Path
from contextlib import closing
from datetime import timedelta
import pytest
sys.path.insert(0, sys.argv[3])
from test_demo_strategy_supervisor import *
from test_binance_demo_strategy_session import deployment, wired
patch = pytest.MonkeyPatch()
root = Path(sys.argv[1])
restarting = sys.argv[2] == 'restart'
setup = root / 'fixture' if restarting else root
setup.mkdir(exist_ok=True)
_, store, _, fake = deployment.__wrapped__(setup, patch, wired.__wrapped__(patch))
market = Market()
counts = dict(GET=0, POST=0, DELETE=0)
with closing(DemoRuntime(root)) as runtime:
    campaign = runtime.active_campaign()
    with open_session(root, store, runtime, counts, campaign=campaign) as (session, expect):
        runner = make_runner(session, expect, campaign, market, patch)
        with closing(StrategyControl(root / 'control.sqlite', dict(mode='preview'))) as control:
            consumer = StrategyConsumer(runner, control)
            consumer.before_poll()
            with closing(make_feed(root, market, consumer)) as feed:
                if not restarting:
                    ack = feed._ack
                    def crash(column, value):
                        if column == 'bar' and value == market.boundary:
                            os._exit(29)
                        ack(column, value)
                    feed._ack = crash
                else:
                    assert len(session.gateway.state.runtime['bars']) == 21
                    assert feed.watermarks()[0] == market.boundary - 3600000
                feed.poll()
                assert feed.watermarks()[0] == market.boundary
                assert counts['POST'] == counts['DELETE'] == 0
                print('RESTART_RECONCILED')
'''
    root = tmp_path / 'process-state'
    root.mkdir()
    tests = str(Path(__file__).parent)
    crash = subprocess.run([sys.executable, '-c', program, str(root), 'crash', tests],
        capture_output=True, text=True, timeout=30)
    assert crash.returncode == 29, crash.stderr
    restart = subprocess.run([sys.executable, '-c', program, str(root), 'restart', tests],
        capture_output=True, text=True, timeout=30)
    assert restart.returncode == 0, restart.stderr
    assert 'RESTART_RECONCILED' in restart.stdout


@pytest.mark.parametrize('book_age', [0, 6])
def test_slow_account_refresh_uses_fresh_book_not_aged_trigger(deployment, monkeypatch, book_age):
    root, store, campaign, fake = deployment
    market = Market()
    counts = dict(GET=0, POST=0, DELETE=0)
    with closing(DemoRuntime(root)) as runtime:
        with open_session(root, store, runtime, counts, campaign=campaign) as (session, expect):
            runner = make_runner(session, expect, campaign, market, monkeypatch)
            runner.recover()
            with closing(StrategyControl(root / 'control.sqlite', dict(mode='preview'))) as control:
                consumer = StrategyConsumer(runner, control)
                prepare = session.prepare_limit_book
                def delayed_book():
                    book = prepare()
                    market.at += timedelta(seconds=10)
                    return dict(book, time_utc=market.at - timedelta(seconds=book_age))
                monkeypatch.setattr(session, 'prepare_limit_book', delayed_book)
                with closing(make_feed(root, market, consumer)) as feed:
                    feed.poll()
                state = session.gateway.state.runtime
                if book_age:
                    assert state['last_rejection'] == 'STALE_QUOTE'
                    assert state['pending'] is not None
                else:
                    assert state['last_order']['submission'] == 'PREVIEW'
                assert counts['POST'] == counts['DELETE'] == 0


def test_stop_requested_during_session_startup_is_not_overwritten(deployment, monkeypatch):
    from factorlab.integrations.binance_futures_trading import demo_strategy_supervisor as module
    root, store, campaign, fake = deployment
    original = module.open_session
    @contextmanager
    def stopping_session(*args, **kwargs):
        with original(*args, **kwargs) as opened:
            with closing(StrategyControl(root / 'strategy-execute.sqlite3')) as control:
                control.request('STOPPED')
            yield opened
    monkeypatch.setattr(module, 'open_session', stopping_session)
    counts = dict(GET=0, POST=0, DELETE=0)
    args = SimpleNamespace(execute=True, interval='1m', max_backfill=120,
        poll_seconds=.001, seconds=.01)
    with closing(DemoRuntime(root)) as runtime:
        module.run_worker(args, root, store, runtime, counts)
        assert runtime.post_count(campaign['id']) == 0
    with closing(StrategyControl(root / 'strategy-execute.sqlite3')) as control:
        assert control.desired() == 'STOPPED'
        assert control.snapshot()['observed']['strategy']['status'] == 'STOPPED'
    assert counts['POST'] == counts['DELETE'] == 0


def test_control_persists_configuration_desired_and_report(tmp_path):
    path = tmp_path / 'control.sqlite'
    configuration = dict(symbol='NVDAUSDT', interval='1h', preview=True)
    with closing(StrategyControl(path, configuration=configuration)) as control:
        assert control.desired() == 'RUNNING'
        control.request('PAUSED')
        control.report(dict(status='PAUSED', reason='operator'))
        snapshot = control.snapshot()
    with closing(StrategyControl(path)) as control:
        assert control.desired() == 'PAUSED'
        assert control.snapshot() == snapshot
        control.request('STOPPED')
    with closing(StrategyControl(path, configuration=configuration)) as control:
        assert control.desired() == 'STOPPED'
    with pytest.raises(PersistenceConflict):
        StrategyControl(path, configuration=dict(configuration, interval='3m'))


def test_preview_feed_dualma_duplicate_and_restart_watermarks(deployment, monkeypatch):
    root, store, campaign, fake = deployment
    market = Market()
    counts = dict(GET=0, POST=0, DELETE=0)
    path = root / 'strategy-control.sqlite'
    checkpoint = watermarks = None
    for restart in range(2):
        with closing(DemoRuntime(root)) as runtime:
            with open_session(root, store, runtime, counts, campaign=campaign) as (session, expect):
                runner = make_runner(session, expect, campaign, market, monkeypatch)
                with closing(StrategyControl(path, configuration=dict(mode='preview', interval='1h'))) as control:
                    consumer = StrategyConsumer(runner, control)
                    before = counts['GET']
                    consumer.before_poll()
                    assert counts['GET'] > before
                    with closing(make_feed(root, market, consumer)) as feed:
                        if restart:
                            assert feed.watermarks() == watermarks
                            assert feed.poll() == dict(bars=0, quotes=0)
                            assert session.gateway.state.runtime == checkpoint
                        else:
                            assert feed.poll() == dict(bars=21, quotes=1)
                            state = session.gateway.state.runtime
                            assert len(state['bars']) == 21
                            assert state['last_signal']['direction'] == 'BUY'
                            assert state['last_signal']['status'] == 'SIGNAL'
                            assert state['pending'] is None
                            checkpoint = deepcopy(state)
                            watermarks = feed.watermarks()
                            assert feed.poll() == dict(bars=0, quotes=0)
                            assert session.gateway.state.runtime == checkpoint
                        assert counts['POST'] == runtime.post_count(campaign['id']) == 0
                        assert not any(method == 'POST' for method, _, _ in fake.calls)


@pytest.mark.parametrize('barrier', ['PAUSED', 'STOPPED', 'authority'])
def test_quote_rechecks_control_and_authority_after_poll_recovery(deployment, monkeypatch, barrier):
    root, store, campaign, fake = deployment
    market = Market()
    counts = dict(GET=0, POST=0, DELETE=0)
    with closing(DemoRuntime(root)) as runtime:
        with open_session(root, store, runtime, counts, campaign=campaign) as (session, expect):
            runner = make_runner(session, expect, campaign, market, monkeypatch)
            with closing(StrategyControl(root / 'control.sqlite', configuration=dict(mode='preview'))) as control:
                authority = [True]
                consumer = StrategyConsumer(runner, control, authority=lambda: authority[0])
                consumer.before_poll()
                positions = deepcopy(session.controller.account().positions)
                if barrier == 'authority':
                    authority[0] = False
                else:
                    control.request(barrier)
                def forbidden(_):
                    pytest.fail('a quote crossed the lifecycle barrier')
                monkeypatch.setattr(runner, 'quote', forbidden)
                consumer.quote(Quote('NVDAUSDT', D(100), market.at))
                assert session.controller.account().positions == positions
                assert counts['POST'] == counts['DELETE'] == 0
                if barrier != 'authority':
                    consumer.before_poll()
                    assert session.gateway.state.runtime['status'] == barrier


def test_consumer_sequence_gap_is_not_acknowledged(deployment, monkeypatch):
    root, store, campaign, fake = deployment
    market = Market()
    counts = dict(GET=0, POST=0, DELETE=0)
    with closing(DemoRuntime(root)) as runtime:
        with open_session(root, store, runtime, counts, campaign=campaign) as (session, expect):
            runner = make_runner(session, expect, campaign, market, monkeypatch)
            with closing(StrategyControl(root / 'control.sqlite', configuration=dict(mode='preview'))) as control:
                consumer = StrategyConsumer(runner, control)
                consumer.before_poll()
                first = datetime.fromtimestamp(market.start / 1000, timezone.utc)
                runner.closed_bar(ClosedBar('NVDAUSDT', first - timedelta(hours=2),
                    first - timedelta(hours=1), D(100), D(100), D(100), D(100),
                    D(100), 3600, SOURCE))
                with closing(make_feed(root, market, consumer)) as feed:
                    with pytest.raises(FeedPaused, match='GAP'):
                        feed.poll()
                    assert feed.watermarks() == (None, None)
                    assert len(session.gateway.state.runtime['bars']) == 1
                    assert counts['POST'] == counts['DELETE'] == 0


def test_pause_resume_and_disconnect_recover_without_writes(deployment, monkeypatch):
    root, store, campaign, fake = deployment
    market = Market()
    counts = dict(GET=0, POST=0, DELETE=0)
    with closing(DemoRuntime(root)) as runtime:
        with open_session(root, store, runtime, counts, campaign=campaign) as (session, expect):
            runner = make_runner(session, expect, campaign, market, monkeypatch)
            with closing(StrategyControl(root / 'control.sqlite', configuration=dict(mode='preview'))) as control:
                consumer = StrategyConsumer(runner, control)
                consumer.before_poll()
                control.request('PAUSED')
                consumer.before_poll()
                assert session.gateway.state.runtime['status'] == 'PAUSED'
                with closing(make_feed(root, market, consumer)) as feed:
                    assert feed.poll() == dict(bars=21, quotes=1)
                    assert session.gateway.state.runtime['pending']['direction'] == 'BUY'
                    control.request('RUNNING')
                    consumer.before_poll()
                    assert session.gateway.state.runtime['status'] == 'RUNNING'
                    assert session.gateway.state.runtime['pending'] is None
                    consumer.disconnect()
                    assert not session.gateway.state.reconciled
                    before = counts['GET']
                    consumer.before_poll()
                    assert counts['GET'] > before
                    assert session.gateway.state.reconciled
                    assert session.gateway.state.runtime['status'] == 'RUNNING'
                    assert feed.poll() == dict(bars=0, quotes=0)
                assert counts['POST'] == counts['DELETE'] == 0
