"""Version-local history recovery through the production REST feed."""
from contextlib import closing
from decimal import Decimal as D
import json
from urllib.parse import parse_qs
from types import SimpleNamespace

import pytest

from factorlab.integrations.binance.client import Response
from factorlab.errors import DataQualityError
from factorlab.integrations.binance_futures_trading.demo_market_feed import DemoMarketFeed, FeedPaused
from factorlab.integrations.binance_futures_trading.protocol import Environment, FuturesClient, OfflineTransport
from factorlab.strategies.loader import StrategyWorkerError
from factorlab.strategies.protocol import Decision
from test_unified_cloud_strategy_s04 import (
    harness, build_decision_package, EXAMPLES, promote_candidate, make_bar,
)
from test_binance_demo_grid_network import network_grid, writes
from test_binance_demo_network import wired


ORDERS = '''from decimal import Decimal as D
from factorlab.strategies.protocol import StrategySpec, Decision
from factorlab.trading.contracts import OrderIntent, OrderType, Side
class WindowOrders:
    def describe(self):
        return StrategySpec('window_orders', '1', 'orders', ['NVDAUSDT'], [60],
            ['closed_bar', 'quote', 'order_event'], history_limit=3)
    def decide(self, event, state):
        if event['kind'] == 'closed_bar':
            state = dict(state, live_bars=state.get('live_bars', 0) + 1)
            if len(event['bars']) >= 3 and not state.get('entry_issued'):
                intent = OrderIntent(event['account']['market'], 'NVDAUSDT', Side.BUY,
                    OrderType.LIMIT, D('.04'), D('148'),
                    event['deployment_id'] + '-entry', position_side='BOTH')
                return Decision(orders=(intent,), state=dict(state, entry_issued=True))
        return Decision(state=state)
'''


def package(tmp_path, harness, mode, name='a'):
    if mode == 'target':
        args = dict(source_dir=EXAMPLES / 'close_momentum_v1', strategy_id='close_momentum',
            strategy_version='1.0.0', entrypoint='__init__:CloseMomentum',
            files=['__init__.py', 'decision.py'], package=True,
            configuration=dict(symbol='NVDAUSDT', interval_seconds=60, weight='.01'))
    else:
        source = tmp_path / ('source-' + name)
        source.mkdir()
        (source / 'window.py').write_text(ORDERS, encoding='utf-8')
        args = dict(source_dir=source, strategy_id='window_orders', strategy_version='1',
            entrypoint='window:WindowOrders', files=['window.py'], configuration={})
    archive, manifest = build_decision_package(tmp_path, name, execution_mode=mode, **args)
    harness.install(archive)
    return manifest


class Market(OfflineTransport):
    def __init__(self, harness):
        self.harness, self.requests, self.bad = harness, [], None
        self.prices = {}

    def __call__(self, request, timeout):
        assert request.method == 'GET'
        self.requests.append(request)
        at = int(self.harness.now().timestamp() * 1000)
        if request.path.endswith('/time'):
            payload = dict(serverTime=at)
        elif request.path.endswith('/klines'):
            p = parse_qs(request.query)
            start, end = int(p['startTime'][0]), int(p['endTime'][0])
            payload = [[t, '150', '151', '149', self.prices.get(t, '150'), '10', t + 59999]
                       for t in range(start, end + 1, 60000)]
            if self.bad == 'missing':
                payload = payload[1:]
            elif self.bad == 'duplicate':
                payload = payload[:1] * 2
            elif self.bad == 'reversed':
                payload.reverse()
            elif self.bad == 'failure':
                raise OSError('temporary historical API failure')
        else:
            payload = dict(symbol='NVDAUSDT', time=at - 100, bidPrice='150', askPrice='150')
        return Response(200, {}, json.dumps(payload).encode())


def feed_for(harness, router, market, **kwargs):
    client = FuturesClient(environment=Environment.DEMO, transport=market,
        clock=lambda: harness.now().timestamp(), monotonic=lambda: harness.now().timestamp())
    feed = DemoMarketFeed(client, router, state_path=harness.root / 'recovery-feed.sqlite',
        consumer_id='s04', clock=harness.now, **kwargs)
    # Model the supervisor's continuously collected book while jumping bar time.
    feed.collection = SimpleNamespace(check=lambda: None, receive_recent=lambda *_, **__: SimpleNamespace(
        payload=client.request('GET', '/fapi/v1/ticker/bookTicker', dict(symbol='NVDAUSDT'), signed=False)))
    router.base.supervisor.feed = feed
    return feed


@pytest.mark.parametrize('mode', ['target', 'orders'])
def test_pause_recovery_uses_version_watermark_without_historical_posts(tmp_path, harness, mode):
    manifest = package(tmp_path, harness, mode)
    with harness.open() as router:
        router.store.request(manifest, promote_candidate(manifest, 'start'))
        router.before_poll()
        market = Market(harness)
        with closing(feed_for(harness, router, market)) as feed:
            harness.set_time(1, 1)
            feed.poll()
            runner = router._load_decision_runner(manifest['package_id'])[2]
            harness.control.state = 'PAUSED'
            for minute in (2, 3):
                harness.set_time(minute, 1)
                router.before_poll()
                feed.poll()
            assert runner._runtime()['bars'][-1]['closed_at'] == make_bar(1, '150').closed_at
            assert feed.watermarks()[0] == int(make_bar(3, '150').closed_at.timestamp() * 1000)
            before = len(writes(harness.requests))
            harness.control.state = 'RUNNING'
            router.before_poll()
            feed.poll()
            assert len(writes(harness.requests)) == before
            assert runner._runtime()['bars'][-1]['closed_at'] == make_bar(2, '150').closed_at
            if mode == 'orders':
                assert runner.journal._latest() == dict(live_bars=1)
            harness.set_time(4, 1)
            router.before_poll()
            feed.poll()
            assert runner._runtime()['bars'][-1]['closed_at'] == make_bar(4, '150').closed_at
            assert 'BAR_SEQUENCE_GAP' != runner._runtime().get('reason')
            if mode == 'orders':
                assert runner.journal._latest()['entry_issued']
                assert runner.journal._latest()['live_bars'] == 2
                assert len(writes(harness.requests)) == before + 1
            market.prices[int(make_bar(5, '151').opened_at.timestamp() * 1000)] = '151'
            harness.set_time(5, 1)
            router.before_poll()
            feed.poll()
            assert runner._runtime()['bars'][-1]['closed_at'] == make_bar(5, '151').closed_at
            if mode == 'target':
                assert runner._runtime()['last_signal']['direction'] == 'BUY'


@pytest.mark.parametrize('bad', ['missing', 'duplicate', 'reversed', 'failure'])
def test_invalid_recovery_retries_without_fabricating_history(tmp_path, harness, bad):
    manifest = package(tmp_path, harness, 'orders')
    with harness.open() as router:
        router.store.request(manifest, promote_candidate(manifest, 'start'))
        router.before_poll()
        market = Market(harness)
        with closing(feed_for(harness, router, market)) as feed:
            harness.set_time(1, 1)
            feed.poll()
            harness.control.state = 'PAUSED'
            harness.set_time(4, 1)
            router.before_poll()
            feed.poll()
            harness.control.state = 'RUNNING'
            router.before_poll()
            market.bad = bad
            before = len(writes(harness.requests))
            with pytest.raises((FeedPaused, OSError, DataQualityError)):
                feed.poll()
            assert router.history_watermark() == make_bar(1, '150').closed_at
            assert not router.live_bar
            assert len(writes(harness.requests)) == before
            market.bad = None
            feed.poll()
            assert router.history_watermark() == make_bar(3, '150').closed_at
            harness.set_time(5, 1)
            router.before_poll()
            feed.poll()
            assert router.history_watermark() == make_bar(5, '150').closed_at


def test_sqlite_restart_resumes_partial_recovery(tmp_path, harness):
    manifest = package(tmp_path, harness, 'orders')
    market = Market(harness)
    with harness.open() as router:
        router.store.request(manifest, promote_candidate(manifest, 'start'))
        router.before_poll()
        with closing(feed_for(harness, router, market, max_backfill=1, catch_up=True)) as feed:
            harness.set_time(1, 1)
            feed.poll()
            harness.control.state = 'PAUSED'
            harness.set_time(10, 1)
            router.before_poll()
            feed.poll()
            feed.poll()
            harness.control.state = 'RUNNING'
            router.before_poll()
            feed.poll()
            assert router.history_watermark() == make_bar(5, '150').closed_at
            assert feed.reason == 'CATCHING_UP'
    count = len(writes(harness.requests))
    with harness.open() as router:
        router.before_poll()
        runner = router._load_decision_runner(manifest['package_id'])[2]
        assert router.history_watermark() == make_bar(5, '150').closed_at
        with closing(feed_for(harness, router, market, max_backfill=1, catch_up=True)) as feed:
            feed.poll()
            assert router.history_watermark() == make_bar(9, '150').closed_at
            assert runner.journal._latest()['live_bars'] == 1
            assert len(writes(harness.requests)) == count


@pytest.mark.parametrize('mode', ['target', 'orders'])
@pytest.mark.parametrize('fault', [False, True])
def test_reactivation_and_fault_rollback_restore_history(tmp_path, harness, monkeypatch, mode, fault):
    a = package(tmp_path, harness, mode, 'a')
    b = package(tmp_path, harness, mode, 'b')
    with harness.open() as router:
        router.store.request(a, promote_candidate(a, 'a'))
        router.before_poll()
        market = Market(harness)
        with closing(feed_for(harness, router, market)) as feed:
            harness.set_time(1, 1)
            feed.poll()
            router.store.request(b, promote_candidate(b, 'b', expected_active=a['package_id']))
            router.before_poll()
            for minute in (2, 3):
                harness.set_time(minute, 1)
                router.before_poll()
                feed.poll()
            if fault:
                runner = router._load_decision_runner(b['package_id'])[2]
                def fail(bar):
                    raise StrategyWorkerError('deterministic strategy fault')
                monkeypatch.setattr(runner, 'closed_bar', fail)
                harness.set_time(4, 1)
                router.before_poll()
                feed.poll()
                assert router.store.pending()['phase'] == 'ROLLBACK'
            else:
                router.store.request(a, promote_candidate(a, 'a-return', expected_active=b['package_id']))
            orders = dict(router.bridge.gateway.state.orders)
            cash = dict(router.bridge.gateway.state.cash)
            before = len(writes(harness.requests))
            router.before_poll()
            assert router.store.snapshot()['active'] == a['package_id']
            feed.poll()
            assert len(writes(harness.requests)) == before
            assert dict(router.bridge.gateway.state.orders) == orders
            assert dict(router.bridge.gateway.state.cash) == cash
            harness.set_time(5, 1)
            router.before_poll()
            feed.poll()
            assert router.history_watermark() == make_bar(5, '150').closed_at


def test_partial_fill_and_durable_cancel_progress_when_history_unavailable(tmp_path, harness):
    manifest = package(tmp_path, harness, 'orders')
    with harness.open() as router:
        router.store.request(manifest, promote_candidate(manifest, 'start'))
        router.before_poll()
        market = Market(harness)
        with closing(feed_for(harness, router, market)) as feed:
            for minute in (1, 2, 3):
                harness.set_time(minute, 1)
                router.before_poll()
                feed.poll()
            runner = router._load_decision_runner(manifest['package_id'])[2]
            key = writes(harness.requests)[0]['newClientOrderId'][0]
            harness.fake.trade_update(key, '.02', '145')
            runner.controller._atomic(lambda: runner._pending(Decision(cancellations=(key,)), harness.now()))
            harness.control.state = 'PAUSED'
            harness.set_time(4, 1)
            router.before_poll()
            feed.poll()
            assert router.bridge.session.status()['positions'][0]['quantity'] == D('.02')
            assert runner.has_unresolved_work()['pending_cancellations'] == [key]
            posts = len(writes(harness.requests))
            harness.control.state = 'RUNNING'
            harness.set_time(6, 1)
            router.before_poll()
            assert writes(harness.requests, 'DELETE')[-1]['origClientOrderId'] == [key]
            market.bad = 'missing'
            with pytest.raises(FeedPaused):
                feed.poll()
            assert len(writes(harness.requests)) == posts
            router.before_poll()
            assert router.bridge.session.status()['orders'][key]['status'] == 'CANCELED'
            assert router.bridge.session.status()['positions'][0]['quantity'] == D('.02')
            assert not runner.has_unresolved_work()['pending_cancellations']
            market.bad = None
            feed.poll()
            assert len(writes(harness.requests)) == posts


def test_unknown_order_is_reconciled_before_history_recovery(tmp_path, harness):
    manifest = package(tmp_path, harness, 'orders')
    with harness.open() as router:
        router.store.request(manifest, promote_candidate(manifest, 'start'))
        router.before_poll()
        market = Market(harness)
        with closing(feed_for(harness, router, market)) as feed:
            for minute in (1, 2):
                harness.set_time(minute, 1)
                router.before_poll()
                feed.poll()
            harness.fake.timeout_next = True
            harness.set_time(3, 1)
            router.before_poll()
            feed.poll()
            key = writes(harness.requests)[0]['newClientOrderId'][0]
            assert router.bridge.session.status()['unknown'] == [key]
            harness.control.state = 'PAUSED'
            harness.set_time(4, 1)
            router.before_poll()
            feed.poll()
            harness.control.state = 'RUNNING'
            harness.set_time(6, 1)
            router.before_poll()
            assert not router.bridge.session.status()['unknown']
            feed.poll()
            assert router.history_watermark() == make_bar(5, '150').closed_at
            harness.set_time(7, 1)
            router.before_poll()
            feed.poll()
            assert router.history_watermark() == make_bar(7, '150').closed_at
            assert len(writes(harness.requests)) == 1
            assert key in router.bridge.session.status()['orders']
