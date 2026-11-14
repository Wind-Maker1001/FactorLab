from datetime import timedelta
from decimal import Decimal as D
from types import SimpleNamespace

import pytest

from factorlab.integrations.binance_futures_trading.demo_cli import parse_args
from factorlab.integrations.binance_futures_trading.demo_grid_supervisor import GridConsumer
from factorlab.integrations.binance_futures_trading.demo_market_feed import FeedPaused, SOURCE
from factorlab.integrations.binance_futures_trading.fake import NOW
from factorlab.integrations.binance_futures_trading.protocol import SafetyBlocked
from factorlab.trading.paper import Quote
from factorlab.trading.strategy import ClosedBar
from test_binance_demo_grid_bridge import bridge
from test_binance_demo_strategy_session import deployment, wired
from test_binance_demo_grid_network import network_grid


class Control:
    state = 'RUNNING'

    def desired(self):
        return self.state


def bar(number, **changes):
    closed = NOW + timedelta(minutes=number)
    values = dict(symbol='NVDAUSDT', opened_at=closed - timedelta(minutes=1), closed_at=closed,
        open=D('150'), high=D('153'), low=D('147'), close=D('150'), volume=D('10'),
        interval_seconds=60, source=SOURCE)
    return ClosedBar(**(values | changes))


def worker(b, *, allowed=lambda: True):
    control = Control()
    consumer = GridConsumer(b, control, authority=allowed, clock=lambda: NOW)
    consumer.supervisor = SimpleNamespace(stop=lambda: None)
    consumer.before_poll()
    consume = consumer.closed_bar
    def closed(value):
        consumer.clock = lambda: max(NOW, value.closed_at)
        return consume(value)
    consumer.closed_bar = closed
    return consumer, control


def posts(fake):
    return [params for method, _, params in fake.calls if method == 'POST']


def test_contiguous_bars_warm_atr_without_quote_time_regression():
    b, fake = bridge(mode='atr', atr_window=2, atr_multiplier=1)
    consumer, _ = worker(b)
    consumer.closed_bar(bar(0))
    b.gateway.clock = lambda: NOW + timedelta(minutes=3)
    consumer.quote(Quote('NVDAUSDT', D('150'), NOW + timedelta(seconds=30)))
    consumer.closed_bar(bar(1))
    consumer.closed_bar(bar(2))
    assert list(b.grid.true_ranges) == [6, 6]
    assert b.grid.sequence == 3
    assert not posts(fake)
    from dataclasses import replace
    b.gateway.adapter.facts = replace(b.gateway.adapter.facts, observed_at=NOW + timedelta(seconds=30))
    b.gateway.clock = b.controller.clock = lambda: NOW + timedelta(seconds=30)
    consumer.quote(Quote('NVDAUSDT', D('150'), NOW + timedelta(seconds=30)))
    assert len(posts(fake)) == 1


def test_backfill_reservations_are_not_promoted_to_live_orders():
    b, fake = bridge()
    consumer, _ = worker(b)
    consumer.closed_bar(bar(-1))
    historical = set(b.grid.withdrawn_orders)
    assert historical and not b.grid.active
    consumer.quote(Quote('NVDAUSDT', D('150'), NOW))
    assert not posts(fake)
    consumer.closed_bar(bar(1))
    consumer.quote(Quote('NVDAUSDT', D('150'), NOW))
    assert len(posts(fake)) == 1
    assert not historical.intersection(b.gateway.state.orders)
    assert posts(fake)[0]['price'] == ['145.0']


def test_delayed_poll_catchup_does_not_submit_old_reservations():
    b, fake = bridge()
    consumer, _ = worker(b)
    consumer.clock = lambda: NOW + timedelta(minutes=3)
    GridConsumer.closed_bar(consumer, bar(1))
    GridConsumer.closed_bar(consumer, bar(2))
    assert not b.grid.active and not consumer.live_bar
    stale = set(b.grid.withdrawn_orders)
    GridConsumer.closed_bar(consumer, bar(3))
    assert b.grid.active and not stale.intersection(order.intent.client_order_id for order in b.grid.active)
    consumer.quote(Quote('NVDAUSDT', D('150'), NOW))
    assert len(posts(fake)) == 1


def test_partial_fills_get_confirmed_then_reduce_only_sell():
    b, fake = bridge()
    consumer, _ = worker(b)
    consumer.closed_bar(bar(1))
    consumer.quote(Quote('NVDAUSDT', D('150'), NOW))
    key = b.grid.active[0].intent.client_order_id
    # Crossing quotes and bar extremes cannot fill a resting order.
    consumer.quote(Quote('NVDAUSDT', D('144'), NOW))
    assert b.account().inventory == 0
    fake.trade_update(key, '.02', '145')
    consumer.before_poll()
    assert b.account().inventory == D('.02')
    consumer.quote(Quote('NVDAUSDT', D('150'), NOW))
    assert len(posts(fake)) == 1
    fake.trade_update(key, '.02', '145')
    consumer.before_poll()
    consumer.quote(Quote('NVDAUSDT', D('150'), NOW))
    assert b.account().inventory == D('.04')
    assert len(posts(fake)) == 2
    assert posts(fake)[1]['side'] == ['SELL']
    assert posts(fake)[1]['reduceOnly'] == ['true']
    assert posts(fake)[1]['price'] == ['155.0']
    consumer.before_poll()
    assert b.account().inventory == D('.04')
    assert len(posts(fake)) == 2


def test_ack_interruption_replay_and_restart(tmp_path):
    protocol, paper = tmp_path / 'protocol.db', tmp_path / 'paper.db'
    b, fake = bridge(protocol, paper)
    consumer, _ = worker(b)
    consumer.closed_bar(bar(1))
    delivery = b.grid.delivery
    assert not consumer.closed_bar(bar(1))
    # Replay must neither generate another observation nor retain an old signal.
    assert b.grid.sequence == 1
    assert b.grid.delivery >= delivery
    assert not b.grid.active
    b.gateway.adapter.store.close()
    b.controller.store.close()
    b, fake = bridge(protocol, paper, fake)
    consumer, _ = worker(b)
    assert not consumer.closed_bar(bar(1))
    assert b.grid.sequence == 1
    consumer.closed_bar(bar(2))
    consumer.quote(Quote('NVDAUSDT', D('150'), NOW))
    assert len(posts(fake)) == 1
    with pytest.raises(ValueError, match='identity conflict'):
        consumer.closed_bar(bar(1, high=D('154')))


def test_pause_resume_disconnect_and_stop_never_cancel():
    b, fake = bridge()
    consumer, control = worker(b)
    consumer.closed_bar(bar(1))
    consumer.quote(Quote('NVDAUSDT', D('150'), NOW))
    control.state = 'PAUSED'
    consumer.before_poll()
    consumer.closed_bar(bar(2))
    consumer.quote(Quote('NVDAUSDT', D('150'), NOW))
    assert len(posts(fake)) == 1
    consumer.disconnect()
    assert not b.ready
    control.state = 'RUNNING'
    consumer.before_poll()
    assert b.ready and not consumer.live_bar
    control.state = 'STOPPED'
    consumer.before_poll()
    assert consumer.reason == 'STOPPED'
    assert not [call for call in fake.calls if call[0] == 'DELETE']
    assert b.controller.account().open_orders


def test_unknown_never_reposts_after_recovery():
    b, fake = bridge()
    consumer, _ = worker(b)
    consumer.closed_bar(bar(1))
    fake.timeout_next = True
    consumer.quote(Quote('NVDAUSDT', D('150'), NOW))
    consumer.disconnect()
    consumer.before_poll()
    consumer.closed_bar(bar(2))
    consumer.quote(Quote('NVDAUSDT', D('150'), NOW))
    assert len(posts(fake)) == 1


def test_authority_expiry_blocks_dispatch():
    allowed = True
    b, fake = bridge()
    consumer, _ = worker(b, allowed=lambda: allowed)
    consumer.closed_bar(bar(1))
    allowed = False
    with pytest.raises(FeedPaused, match='AUTHORIZATION_REQUIRED'):
        consumer.quote(Quote('NVDAUSDT', D('150'), NOW))
    assert not posts(fake)
    consumer.before_poll()
    assert consumer.reason == 'AUTHORIZATION_REQUIRED'
    assert not b.grid.active


def test_preview_keeps_proposals_without_order_writes():
    b, fake = bridge()
    b.preview = True
    consumer, _ = worker(b)
    for number in range(1, 4):
        consumer.closed_bar(bar(number))
        consumer.quote(Quote('NVDAUSDT', D('150'), NOW))
    assert b.grid.active
    assert not [call for call in fake.calls if call[0] in ('POST', 'DELETE')]
    with pytest.raises(SafetyBlocked, match='cannot write'):
        b.dispatch()


def test_continuous_execution_preserves_existing_deployment_and_serial_admission(network_grid):
    from factorlab.integrations.binance_futures_trading.demo_runtime import open_grid_session
    root, store, factory, enable, fake, requests = network_grid
    enable()
    with open_grid_session(root, store, factory) as b:
        b.recover()
        saved = b.grid.deployment()
        b.observe(dict(id='initial', time=NOW.replace(tzinfo=None).isoformat(), price='150', volume=0))
        identity = b.grid.active[0].intent.client_order_id
    before = sum(request.get_method() == 'POST' for request in requests)
    with open_grid_session(root, store, factory, continuous=True) as b:
        assert b.grid.continuous_capacity
        assert b.grid.deployment() == saved
        consumer, _ = worker(b)
        consumer.closed_bar(bar(1))
        consumer.quote(Quote('NVDAUSDT', D('150'), b.gateway.state.quotes['NVDAUSDT'].observed_at))
        assert sum(request.get_method() == 'POST' for request in requests) == before
        fake.trade_update(identity, '.04', '146')
        consumer.before_poll()
        consumer.quote(Quote('NVDAUSDT', D('150'), b.gateway.state.quotes['NVDAUSDT'].observed_at))
        assert b.account().inventory == D('.04')
        assert sum(request.get_method() == 'POST' for request in requests) == before + 1
        assert b.grid.active[0].intent.reduce_only
        assert b.grid.active[0].intent.limit_price == D('154')
        with pytest.raises(SafetyBlocked, match='another Demo session'):
            with open_grid_session(root, store, factory, continuous=True):
                pytest.fail('second execution owner admitted')


def test_manual_management_inherits_continuous_capacity_after_stop(network_grid):
    from contextlib import closing
    from dataclasses import replace
    import pickle
    from pathlib import Path
    import subprocess
    import sys

    from factorlab.integrations.binance_futures_trading.demo_cli import open_session
    from factorlab.integrations.binance_futures_trading.demo_runtime import DemoRuntime, open_grid_session
    from factorlab.trading import OrderStatus

    root, store, factory, enable, fake, requests = network_grid
    enable()
    with open_grid_session(root, store, factory, continuous=True) as b:
        b.recover()
        b.observe(dict(id='initial', time=NOW.replace(tzinfo=None).isoformat(), price='150', volume=0))
        identity = b.grid.active[0].intent.client_order_id
        record, _ = b.session.adapter._load(identity)
        with b.session.adapter.store.transaction():
            for i in range(1001):
                intent = replace(record.snapshot.intent, client_order_id=f'historical-rejected-{i}')
                snapshot = replace(record.snapshot, intent=intent, status=OrderStatus.REJECTED,
                    exchange_order_id=None)
                b.session.adapter._save(replace(record, snapshot=snapshot), D('0'))
        for journal in (b.session.adapter.store, b.controller.store):
            with journal.transaction():
                for i in range(10001):
                    journal.record_metadata('capacity-test:' + str(i), {})
        controller_path = Path(b.controller.store.db.execute('PRAGMA database_list').fetchone()[2])
        cursor = b.controller.store.event_cursor()
    fake.trade_update(identity, '.02', '146')
    exchange = root / 'offline-exchange.pickle'
    exchange.write_bytes(pickle.dumps(fake.__dict__))
    subprocess.run([sys.executable, '-c', '''
import pickle
from decimal import Decimal as D
from pathlib import Path
import sys
import pytest
sys.path.insert(0, sys.argv[1])
sys.path.insert(0, str(Path(sys.argv[1]).parents[1] / 'src'))
from test_binance_demo_network import wired
from test_binance_demo_grid_network import network_grid
from factorlab.integrations.binance_futures_trading.demo_cli import open_session
from factorlab.integrations.binance_futures_trading.demo_runtime import DemoRuntime
from factorlab.trading import Side
from factorlab.integrations.binance_futures_trading.protocol import SafetyBlocked
from contextlib import closing
root = Path(sys.argv[2])
with pytest.MonkeyPatch.context() as patch:
    wire = wired.__wrapped__(patch)
    _, store, _, _, fake, requests = network_grid.__wrapped__(root, patch, wire)
    fake.__dict__.update(pickle.loads((root / 'offline-exchange.pickle').read_bytes()))
    with closing(DemoRuntime(root)) as runtime:
        with open_session(root, store, runtime, dict(GET=0, POST=0, DELETE=0)) as (session, _):
            assert session.adapter.store.continuous_capacity
            assert session.controller.store.continuous_capacity
            report = session.recover()
            assert report['reconciled']
            assert len(session.gateway.state.orders) == 1002
            assert len(session.controller.store.load().orders) == 1002
            assert session.adapter.order(sys.argv[3]).cumulative_filled_quantity == D('.02')
            with pytest.raises(SafetyBlocked):
                session.cancel(sys.argv[3])
            with pytest.raises(SafetyBlocked):
                session.submit_limit('unauthorized', Side.BUY, D('.04'))
    assert all(request.get_method() == 'GET' for request in requests)
''',
        str(Path(__file__).parent), str(root), identity], check=True)
    from factorlab.trading.storage import PaperStore
    with closing(PaperStore(controller_path)) as journal:
        assert journal.event_cursor() > cursor
        assert any(event.client_order_id == identity for _, event in journal.order_events())
    with closing(DemoRuntime(root)) as runtime:
        campaign = runtime.active_campaign()
        with open_session(root, store, runtime, dict(GET=0, POST=0, DELETE=0), campaign=campaign) as (session, _):
            session.recover()
            assert session.cancel(identity).status is OrderStatus.CANCELED
            assert session.adapter.order(identity).status is OrderStatus.CANCELED
            assert session.controller.store.load().orders[identity].snapshot.status is OrderStatus.CANCELED
    assert sum(request.get_method() == 'POST' for request in requests) == 1
    assert sum(request.get_method() == 'DELETE' for request in requests) == 1


def test_http_preview_needs_no_authority_and_does_not_contend_for_execution_lock(deployment):
    from contextlib import closing
    from factorlab.integrations.binance_futures_trading.demo_runtime import (
        DemoRuntime, open_grid_session, operator_lock)
    from factorlab.integrations.binance_futures_trading.demo_grid_supervisor import grid_factory
    root, store, _, fake = deployment
    with closing(DemoRuntime(root)) as runtime:
        runtime.stop()
    counts = dict(GET=0, POST=0, DELETE=0)
    with operator_lock(root):
        with open_grid_session(root, store, grid_factory(None), preview=True,
                continuous=True, request_counts=counts) as b:
            consumer, _ = worker(b)
            consumer.closed_bar(bar(1, open=D('100'), high=D('101'), low=D('99'), close=D('100')))
            consumer.quote(Quote('NVDAUSDT', D('100'), NOW + timedelta(milliseconds=2)))
            deployment_id = b.grid.deployment_id
            assert b.grid.active
            with pytest.raises(SafetyBlocked):
                b.session.submit_intent(b.grid.active[0].intent)
    assert counts['GET'] > 0 and counts['POST'] == counts['DELETE'] == 0
    assert not [call for call in fake.calls if call[0] in ('POST', 'DELETE')]
    assert not list(root.glob('*/controller.sqlite3'))
    with open_grid_session(root, store, grid_factory(None), preview=True, continuous=True) as b:
        b.recover()
        assert b.grid.deployment_id == deployment_id
        assert b.grid.sequence == 1


@pytest.mark.parametrize('method', ['POST', 'DELETE'])
def test_write_receipt_cannot_override_get_order_state(monkeypatch, method):
    import json
    from factorlab.integrations.binance.client import Response
    from test_binance_demo_execution import DemoFixture
    b, fake = bridge()
    b.gateway.adapter.get_confirmed_writes = True
    # A complete, plausible write receipt still must not book an unconfirmed fill.
    original = DemoFixture.__call__
    def receipt(self, request, timeout):
        result = original(self, request, timeout)
        if request.method == method and request.path.endswith('/order'):
            raw = json.loads(result.body)
            raw.update(status='FILLED', executedQty='0.04', cumQuote='5.8', avgPrice='145')
            return Response(200, {}, json.dumps(raw).encode())
        return result
    monkeypatch.setattr(DemoFixture, '__call__', receipt)
    consumer, _ = worker(b)
    consumer.closed_bar(bar(1))
    consumer.quote(Quote('NVDAUSDT', D('150'), NOW))
    key = b.grid.active[0].intent.client_order_id
    if method == 'DELETE':
        b.controller.cancel(key)
    assert b.account().inventory == 0
    if method == 'DELETE':
        assert b.gateway.adapter.order(key).status.value == 'CANCELED'
    else:
        assert b.gateway.adapter.order(key).status.value == 'SUBMITTED'
    writes = [i for i, call in enumerate(fake.calls) if call[0] == method]
    assert writes and any(call[:2] == ('GET', '/fapi/v1/order') for call in fake.calls[writes[-1] + 1:])


def test_incomplete_bar_never_advances_grid():
    b, _ = bridge()
    consumer, _ = worker(b)
    with pytest.raises(FeedPaused, match='INVALID_CLOSED_BAR'):
        consumer.closed_bar(bar(1, complete=False))
    assert b.grid.sequence == 0


def test_worker_end_to_end_persists_status_and_control(deployment, monkeypatch, capsys):
    import json
    from contextlib import closing
    from factorlab.integrations.binance_futures_trading import demo_grid_supervisor as module
    from factorlab.integrations.binance_futures_trading.demo_market_feed import DemoMarketFeed
    from factorlab.integrations.binance_futures_trading.protocol import FuturesClient, Environment
    from factorlab.integrations.binance_futures_trading.demo_strategy_supervisor import StrategyControl
    from test_demo_strategy_supervisor import Market
    root, store, _, _ = deployment
    market = Market()
    market.at = NOW + timedelta(milliseconds=2)
    monkeypatch.setattr(module, 'MarketClient', lambda requests: FuturesClient(
        environment=Environment.DEMO, transport=market))
    monkeypatch.setattr(module, 'DemoMarketFeed', lambda *args, **kwargs: DemoMarketFeed(
        *args, clock=lambda: market.at, **kwargs))
    args = parse_args(['grid-supervisor', 'start', '--seconds', '.01', '--poll-seconds', '.001', '--interval', '1h'])
    assert module.run(args, root=root, credentials=store) == 0
    path = root / 'grid-preview' / 'grid-supervisor.sqlite3'
    with closing(StrategyControl(path)) as control:
        observed = control.snapshot()['observed']
        assert observed['status'] == 'EXITED'
        assert observed['requests']['GET'] > 0
        assert observed['requests']['POST'] == observed['requests']['DELETE'] == 0
        assert observed['watermarks'][0] is not None
    capsys.readouterr()
    args.action = 'pause'
    module.run(args, root=root, credentials=store)
    assert json.loads(capsys.readouterr().out)['desired'] == 'PAUSED'
    args.action = 'resume'
    module.run(args, root=root, credentials=store)
    assert json.loads(capsys.readouterr().out)['desired'] == 'RUNNING'
    args.action = 'stop'
    module.run(args, root=root, credentials=store)
    assert json.loads(capsys.readouterr().out)['desired'] == 'STOPPED'


def test_cli_default_is_preview_and_controls_are_integrated():
    args = parse_args(['grid-supervisor', 'start', '--seconds', '1'])
    assert not args.execute and args.seconds == 1
    args = parse_args(['grid-supervisor', 'stop', '--execute'])
    assert args.execute and args.action == 'stop'
