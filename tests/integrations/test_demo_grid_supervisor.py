from datetime import timedelta
from decimal import Decimal as D
from types import SimpleNamespace

import pytest

from factorlab.integrations.binance_futures_trading.demo_cli import parse_args
from factorlab.integrations.binance_futures_trading.demo_grid_supervisor import GridConsumer
from factorlab.integrations.binance_futures_trading.demo_market_feed import FeedPaused, SOURCE
from factorlab.integrations.binance_futures_trading.fake import NOW
from factorlab.integrations.binance_futures_trading.protocol import SafetyBlocked
from factorlab.integrations.binance_futures_trading.demo_session import DemoSession
from factorlab.trading.paper import Quote
from factorlab.trading.strategy import ClosedBar
from test_binance_demo_grid_bridge import bridge
from test_binance_demo_strategy_session import deployment, wired
from test_binance_demo_grid_network import network_grid


SESSION_NOW = DemoSession._now


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
    consumer.supervisor = SimpleNamespace(stop=lambda: None, collection=None)
    consumer.before_poll()
    consume = consumer.closed_bar
    def closed(value):
        consumer.clock = lambda: max(NOW, value.closed_at)
        return consume(value)
    consumer.closed_bar = closed
    return consumer, control


def posts(fake):
    return [params for method, _, params in fake.calls if method == 'POST']


@pytest.mark.parametrize('arguments', [
    ['start', '--new-deployment'],
    ['pause', '--execute', '--new-deployment'],
])
def test_new_deployment_requires_explicit_execution_start(tmp_path, arguments):
    from factorlab.integrations.binance_futures_trading.demo_grid_supervisor import run
    args = parse_args(['grid-supervisor', *arguments])
    with pytest.raises(SafetyBlocked, match='requires start --execute'):
        run(args, root=tmp_path)
    assert not list(tmp_path.iterdir())


def test_new_campaign_factory_retains_history_and_restores_current_grid(network_grid):
    from factorlab.integrations.binance_futures_trading.demo_grid_supervisor import grid_factory
    from contextlib import closing
    from factorlab.integrations.binance_futures_trading.demo_runtime import DemoRuntime, open_grid_session
    from factorlab.trading.storage import decode
    import json

    root, store, factory, enable, fake, requests = network_grid
    enable()
    with open_grid_session(root, store, factory, continuous=True) as b:
        old = b.grid.deployment()
    with closing(DemoRuntime(root)) as runtime:
        runtime.stop()
    enable()
    with pytest.raises(SafetyBlocked, match='campaign differs'):
        with open_grid_session(root, store, grid_factory(None), continuous=True):
            pytest.fail('old campaign restored for execution')
    with open_grid_session(root, store, grid_factory(None, new_deployment=True),
                           new_deployment=True, continuous=True) as b:
        current = b.grid.deployment()
        assert current['deployment_id'] != old['deployment_id']
        rows = b.controller.store.db.execute(
            "SELECT payload FROM events WHERE id LIKE 'demo-grid:deployment:%'").fetchall()
        assert [decode(json.loads(row[0]))['deployment'] for row in rows] == [old, current]
    with open_grid_session(root, store, grid_factory(None), continuous=True) as b:
        assert b.grid.deployment() == current
    with pytest.raises(SafetyBlocked, match='already has a Grid deployment'):
        with open_grid_session(root, store, grid_factory(None, new_deployment=True),
                               new_deployment=True, continuous=True):
            pytest.fail('same campaign initialized twice')
    assert not [request for request in requests if request.get_method() != 'GET']


def test_new_campaign_rejects_existing_activity(network_grid):
    from contextlib import closing
    from factorlab.integrations.binance_futures_trading.demo_grid_supervisor import grid_factory
    from factorlab.integrations.binance_futures_trading.demo_runtime import DemoRuntime, open_grid_session

    root, store, factory, enable, fake, requests = network_grid
    enable()
    with open_grid_session(root, store, factory, continuous=True) as b:
        b.recover()
        b.observe(dict(id='initial', time=NOW.replace(tzinfo=None).isoformat(), price='150', volume=0))
    with closing(DemoRuntime(root)) as runtime:
        runtime.stop()
    enable()
    with pytest.raises(SafetyBlocked, match='reconciled flat account'):
        with open_grid_session(root, store, grid_factory(None, new_deployment=True),
                               new_deployment=True, continuous=True):
            pytest.fail('new deployment admitted with an active order')
    assert sum(request.get_method() == 'POST' for request in requests) == 1


def test_continuous_breakout_rotates_after_flat_recovery_and_restarts(network_grid):
    from contextlib import closing

    from factorlab.integrations.binance_futures_trading.demo_grid_supervisor import grid_factory
    from factorlab.integrations.binance_futures_trading.demo_runtime import DemoRuntime, open_grid_session
    from test_binance_demo_grid_network import observe, writes

    root, store, original_factory, _, _, requests = network_grid
    with closing(DemoRuntime(root)) as runtime:
        policy = runtime.start_continuous(store.read_config(), opening_equity=D('1000'),
                                          stop_epoch=runtime.stop_epoch(), orders=1)
    with open_grid_session(root, store, original_factory, continuous=True) as old:
        old.recover()
        observe(old)
        old.observe(dict(id='breakout', time=NOW.replace(tzinfo=None).isoformat(), price='155', volume=0),
                    dispatch=False)
        assert old.grid.closed_by_breakout
        assert old.rotate_if_terminal(old, running=lambda: True) is old
        assert not writes(requests, 'DELETE')
        old.dispatch()
        original_id = old.grid.deployment_id
        assert len(writes(requests)) == len(writes(requests, 'DELETE')) == 1

    with open_grid_session(root, store, grid_factory(None), continuous=True) as old:
        old.recover()
        events = old.controller.store.event_cursor()
        consumed = old.runtime.consumption(policy)
        old_window = old.runtime.active_campaign()
        consumer = GridConsumer(old, Control(), clock=lambda: NOW,
                                ensure_campaign=old.ensure_campaign, rotate=old.rotate_if_terminal)
        consumer.before_poll()
        new = consumer.bridge
        new_id = new.grid.deployment_id
        assert (new.grid.config.lower, new.grid.config.upper) == (148.5, 151.5)
        assert old.grid.closed_by_breakout and old.grid.deployment_id == original_id
        assert new_id != original_id and len(new_id + '-00000001') <= 36
        assert new.prefix != old.prefix and new.feed_deployment_id == original_id
        assert new.runtime.active_campaign() == old_window
        assert new.runtime.continuous_authorization() == policy
        assert new.runtime.consumption(policy) == consumed
        assert consumed['posts'] == 1 and D(consumed['submitted_notional']) == D('5.84')
        assert new.controller.store.event_cursor() == events + 2
        assert consumer.historical_bar(bar(0, close=D('145'), low=D('144'))) is False
        assert not new.grid.delivery

    with open_grid_session(root, store, grid_factory(None), continuous=True) as new:
        consumer, _ = worker(new)
        consumer.rotate = new.rotate_if_terminal
        consumer.before_poll()
        assert consumer.bridge.grid.deployment_id == new_id
        assert new.feed_deployment_id == original_id
        consumer.closed_bar(bar(1))
        consumer.quote(Quote('NVDAUSDT', D('150'), NOW))
        assert [row['newClientOrderId'][0].rsplit('-', 1)[0] for row in writes(requests)] == [original_id, new_id]


@pytest.mark.parametrize('state', ['STOPPED', 'HALTED'])
def test_continuous_breakout_rechecks_control_before_persisting_replacement(network_grid, state):
    from contextlib import closing

    from factorlab.integrations.binance_futures_trading.demo_runtime import DemoRuntime, open_grid_session
    from test_binance_demo_grid_network import writes

    root, store, factory, _, _, requests = network_grid
    with closing(DemoRuntime(root)) as runtime:
        runtime.start_continuous(store.read_config(), opening_equity=D('1000'),
                                 stop_epoch=runtime.stop_epoch())

    def terminate_during_factory(session, deployment_id):
        if deployment_id.endswith('-00001'):
            with closing(DemoRuntime(root)) as runtime:
                if state == 'STOPPED':
                    runtime.stop()
                else:
                    runtime.halt('test loss limit')
        return factory(session, deployment_id)

    with open_grid_session(root, store, terminate_during_factory, continuous=True) as old:
        old.recover()
        old.observe(dict(id='breakout', time=NOW.replace(tzinfo=None).isoformat(), price='155', volume=0),
                    dispatch=False)
        events = old.controller.store.event_cursor()
        assert old.rotate_if_terminal(old, running=lambda: True) is old
        assert old.controller.store.event_cursor() == events
        assert old.runtime.continuous_authorization()['state'] == state
    assert not writes(requests)


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


def test_historical_replay_cannot_admit_an_order_when_local_clock_lags():
    b, fake = bridge()
    consumer, _ = worker(b)
    consumer.clock = lambda: NOW + timedelta(minutes=2, seconds=57)
    consumer.historical_bar(bar(2))
    assert not consumer.live_bar
    assert not b.grid.active
    consumer.quote(Quote('NVDAUSDT', D('150'), NOW))
    assert not posts(fake)


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


def test_status_reports_unavailable_account_evidence_without_claiming_flat(network_grid):
    from factorlab.integrations.binance_futures_trading.demo_runtime import open_grid_session

    root, store, factory, enable, fake, requests = network_grid
    enable()
    with open_grid_session(root, store, factory, continuous=True) as b:
        consumer, _ = worker(b)
        consumer.disconnect()
        b.gateway.adapter.facts = None
        status = consumer.status()
        assert status['inventory'] is None
        assert status['account']['positions'] is None
        assert not status['account']['reconciled']
        assert status['reason'] == 'RECONCILE_REQUIRED'
    assert not [request for request in requests if request.get_method() != 'GET']


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
    from factorlab.integrations.binance_futures_trading.demo_strategy_supervisor import StrategyControl
    from test_demo_strategy_supervisor import Market
    root, store, _, _ = deployment
    market = Market()
    market.at = NOW + timedelta(milliseconds=2)
    monkeypatch.setattr(module, 'MarketClient', lambda requests: market.client())
    monkeypatch.setattr(module, 'DemoMarketFeed', lambda *args, **kwargs: DemoMarketFeed(
        *args, clock=lambda: market.at, **kwargs))
    args = parse_args(['grid-supervisor', 'start', '--seconds', '1', '--poll-seconds', '.001', '--interval', '1h'])
    path = root / 'grid-preview' / 'grid-supervisor.sqlite3'
    with closing(DemoMarketFeed(market.client(),
            SimpleNamespace(), state_path=path, consumer_id='grid-dry-run', interval='1h')) as legacy:
        legacy._ack('bar', int((NOW - timedelta(days=2)).timestamp() * 1000))
        old_watermarks = legacy.watermarks()
    assert module.run(args, root=root, credentials=store) == 0
    with closing(StrategyControl(path)) as control:
        observed = control.snapshot()['observed']
        assert observed['status'] == 'EXITED'
        assert observed['requests']['GET'] > 0
        assert observed['requests']['POST'] == observed['requests']['DELETE'] == 0
        assert observed['watermarks'][0] is not None
        cursors = control.db.execute('SELECT id, bar, quote FROM cursor ORDER BY id').fetchall()
        assert len(cursors) == 2
        assert any(row[0].endswith(':grid-dry-run') and tuple(row[1:]) == old_watermarks for row in cursors)
        assert any(row[0].endswith(':' + observed['deployment']['deployment_id']) for row in cursors)
    assert module.run(args, root=root, credentials=store) == 0
    with closing(StrategyControl(path)) as control:
        assert control.db.execute('SELECT id, bar, quote FROM cursor ORDER BY id').fetchall() == cursors
        assert control.snapshot()['observed']['watermarks'] == observed['watermarks']
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


@pytest.fixture
def calibrated_worker(network_grid, monkeypatch):
    from factorlab.integrations.binance.client import Response
    from factorlab.integrations.binance_futures_trading import demo_grid_supervisor as module
    from factorlab.integrations.binance_futures_trading import network, protocol
    from test_binance_demo_network import NetworkFixture
    import json
    from urllib.parse import parse_qs

    root, store, factory, enable, fake, requests = network_grid
    state = SimpleNamespace(seconds=.2, wall_offset=0., quote_age=100, fail_time=False,
                            delay_bars=0., consumer=None, feed=None, rule_response=None)
    initial = NOW.timestamp()
    original_init = protocol.FuturesClient.__init__

    def initialize(client, *args, **kwargs):
        original_init(client, *args, **kwargs)
        client.clock = lambda: initial + state.seconds + state.wall_offset
        client.monotonic = lambda: state.seconds

    original_request = NetworkFixture.__call__

    def response(exchange, request, timeout):
        if request.path.endswith('/time') and state.fail_time:
            raise OSError('calibration unavailable')
        if request.path.endswith('/klines'):
            exchange.calls.append((request.method, request.path, parse_qs(request.query)))
            params = parse_qs(request.query)
            start, end = int(params['startTime'][0]), int(params['endTime'][0])
            payload = [[at, '150', '153', '147', '150', '10', at + 59999]
                       for at in range(start, end + 1, 60000)]
            state.seconds += state.delay_bars
            return Response(200, {}, json.dumps(payload).encode())
        result = original_request(exchange, request, timeout)
        if state.rule_response is not None and request.path.endswith(('/exchangeInfo', '/leverageBracket')):
            payload = state.rule_response(request.path, json.loads(result.body))
            return Response(200, {}, json.dumps(payload).encode())
        if request.path.endswith(('/time', '/bookTicker', '/premiumIndex')):
            payload = json.loads(result.body)
            key = 'serverTime' if request.path.endswith('/time') else 'time'
            payload[key] = round((initial + state.seconds) * 1000)
            if request.path.endswith('/bookTicker'):
                payload[key] -= state.quote_age
            return Response(200, {}, json.dumps(payload).encode())
        return result

    original_feed = module.DemoMarketFeed

    def feed(client, consumer, **kwargs):
        state.consumer = consumer
        state.feed = original_feed(client, consumer, **kwargs)
        return state.feed

    monkeypatch.setattr(protocol.FuturesClient, '__init__', initialize)
    monkeypatch.setattr(DemoSession, '_now', SESSION_NOW)
    monkeypatch.setattr(NetworkFixture, '__call__', response)
    monkeypatch.setattr(protocol, 'build_opener', network.build_opener)
    monkeypatch.setattr(module, 'grid_factory', lambda *args, **kwargs: factory)
    monkeypatch.setattr(module, 'DemoMarketFeed', feed)
    monkeypatch.setattr(module.signal, 'signal', lambda *args: None)

    def run(rounds, *, execute=False, offset=0):
        state.wall_offset = offset
        if execute:
            enable()
        def drive(supervisor, **kwargs):
            rounds(supervisor, state)
        monkeypatch.setattr(module.DemoSupervisor, 'run', drive)
        args = parse_args(['grid-supervisor', 'start', '--seconds', '1',
                           *(['--execute'] if execute else [])])
        return module.run(args, root=root, credentials=store)

    return run, state, fake, requests


@pytest.mark.parametrize('failed_get', [None, 'exchangeInfo', 'leverageBracket'])
def test_worker_refreshes_rules_without_resetting_orders_or_budget(calibrated_worker, failed_get):
    from factorlab.errors import DataQualityError
    run, state, fake, _ = calibrated_worker

    def rules(path, payload):
        if failed_get is not None and path.endswith(failed_get) and state.seconds < 400:
            raise OSError('rules temporarily unavailable')
        if path.endswith('/exchangeInfo'):
            filters = {rule['filterType']: rule for rule in payload['symbols'][0]['filters']}
            filters['PRICE_FILTER']['tickSize'] = '.02'
            filters['LOT_SIZE']['stepSize'] = '.02'
        else:
            payload[0]['brackets'][0]['initialLeverage'] = 1
        return payload

    def rounds(supervisor, state):
        consumer = state.consumer
        bridge = consumer.bridge
        session = bridge.session
        runtime = supervisor.feed.runtime
        campaign = runtime.active_campaign()
        original_rules = session.gateway.rules

        def deliver(number):
            supervisor.before_poll()
            consumer.closed_bar(bar(number))
            return consumer.quote(session.gateway.state.quotes['NVDAUSDT'])

        supervisor.before_poll()
        state.seconds = 60.2
        entry = deliver(1)
        fake.trade_update(entry.intent.client_order_id, '.04', '146')
        state.seconds = 360.4
        supervisor.before_poll()
        before = session.status()
        assert before['reconciled'] and before['unknown'] == []
        assert before['orders'][entry.intent.client_order_id]['status'] == 'FILLED'
        assert runtime.post_count(campaign['id']) == 1
        assert session.gateway.rules is original_rules
        state.rule_response = rules

        with pytest.raises(SafetyBlocked, match='already journaled'):
            session.submit_intent(entry.intent)
        if failed_get is not None:
            with pytest.raises(DataQualityError, match='account read failed'):
                deliver(6)
            assert len(posts(fake)) == runtime.post_count(campaign['id']) == 1
            assert session.adapter._rule_observation.rules is session.gateway.rules is original_rules
            assert session.status()['orders'] == before['orders']
            supervisor.on_pause()
            state.seconds = 400.4
            supervisor.before_poll()
            state.seconds = 420.4
            exit_order = deliver(7)
        else:
            exit_order = deliver(6)

        rules_now = session.gateway.rules
        assert session.adapter._rule_observation.rules is rules_now
        assert rules_now.observed_at == session._now()
        assert rules_now.filters['PRICE_FILTER']['tickSize'] == '.02'
        assert rules_now.filters['LOT_SIZE']['stepSize'] == '.02'
        assert rules_now.brackets[0]['initialLeverage'] == 1
        assert exit_order.intent.reduce_only and exit_order.intent.limit_price == D('154')
        assert exit_order.intent.quantity == D('.04')
        assert len(posts(fake)) == runtime.post_count(campaign['id']) == 2
        supervisor.before_poll()
        after = session.status()
        assert after['reconciled'] and after['unknown'] == []
        assert after['balances']['walletBalance'] == before['balances']['walletBalance']
        assert after['orders'][entry.intent.client_order_id] == before['orders'][entry.intent.client_order_id]
        assert after['orders'][exit_order.intent.client_order_id]['order_id'] == exit_order.exchange_order_id

    assert run(rounds, execute=True) == 0


@pytest.mark.parametrize('constraint', ['tick', 'lot', 'leverage'])
def test_expired_session_uses_current_rules_for_new_limit(calibrated_worker, constraint):
    from factorlab.trading import Market, OrderIntent, OrderStatus, OrderType, Side
    from factorlab.trading.control import RiskRejected
    run, state, fake, _ = calibrated_worker

    def rules(path, payload):
        if path.endswith('/exchangeInfo'):
            filters = {rule['filterType']: rule for rule in payload['symbols'][0]['filters']}
            filters['PRICE_FILTER']['tickSize'] = '.07'
            if constraint == 'lot':
                filters['LOT_SIZE']['stepSize'] = '.03'
        elif constraint == 'leverage':
            payload[0]['brackets'][0]['notionalCap'] = '5'
        return payload

    def rounds(supervisor, state):
        consumer = state.consumer
        bridge = consumer.bridge
        session = bridge.session
        supervisor.before_poll()
        state.seconds = 301.2
        supervisor.before_poll()
        consumer.closed_bar(bar(5))
        state.rule_response = rules
        key = bridge.grid.deployment_id + '-00000001'
        bridge.expect(OrderIntent(Market.BINANCE_USDM, 'NVDAUSDT', Side.BUY, OrderType.LIMIT,
            D('.04'), D('150.01'), key))
        if constraint == 'tick':
            order = session.submit_limit(key, Side.BUY, D('.04'))
            assert order.intent.limit_price == D('150.01')
            assert order.status is OrderStatus.SUBMITTED
            assert len(posts(fake)) == 1
        else:
            reason = 'QUANTITY_FILTER' if constraint == 'lot' else 'RISK_BRACKET_LIMIT'
            with pytest.raises(RiskRejected, match=reason):
                session.submit_limit(key, Side.BUY, D('.04'))
            assert not posts(fake)
            assert key not in session.status()['orders']

    assert run(rounds, execute=True) == 0


def test_rules_expiring_during_admission_block_post_and_enter_recovery(calibrated_worker, monkeypatch):
    from factorlab.errors import DataQualityError
    run, state, fake, _ = calibrated_worker

    def rounds(supervisor, state):
        consumer = state.consumer
        session = consumer.bridge.session
        supervisor.before_poll()
        state.seconds = 300.2
        supervisor.before_poll()
        consumer.closed_bar(bar(5))
        mark = session.gateway.fresh_mark_price

        def delayed_mark(symbol):
            price = mark(symbol)
            state.seconds += .1
            return price

        with monkeypatch.context() as patch:
            patch.setattr(session.gateway, 'fresh_mark_price', delayed_mark)
            with pytest.raises(DataQualityError, match='STALE_RULES'):
                consumer.quote(session.gateway.state.quotes['NVDAUSDT'])
        assert not posts(fake)
        assert not session.status()['orders']
        supervisor.on_pause()
        state.seconds = 340.2
        supervisor.before_poll()
        state.seconds = 360.2
        supervisor.before_poll()
        consumer.closed_bar(bar(6))
        assert consumer.quote(session.gateway.state.quotes['NVDAUSDT']).exchange_order_id
        assert len(posts(fake)) == 1

    assert run(rounds, execute=True) == 0


@pytest.mark.parametrize('offset', [-20, 20])
@pytest.mark.parametrize('execute', [False, True])
def test_worker_uses_session_clock_for_live_bar_and_exact_order(calibrated_worker, offset, execute):
    from urllib.parse import parse_qs, urlsplit
    run, state, fake, requests = calibrated_worker

    def rounds(supervisor, state):
        consumer, feed = state.consumer, state.feed
        session = consumer.bridge.session
        assert consumer.clock == session._now
        assert feed.receipt_clock == session._now
        assert feed.exchange_time == session.client.demo_time
        supervisor.before_poll()
        assert feed.poll() == dict(bars=1, quotes=1)
        assert not consumer.live_bar
        assert consumer.reason == 'FRESH_CLOSED_BAR_REQUIRED'
        assert not posts(fake)
        for at in (20.2, 40.2):
            state.seconds = at
            supervisor.before_poll()
            state.feed.poll()
        state.seconds = 60.2
        supervisor.before_poll()
        assert feed.poll() == dict(bars=1, quotes=1)
        assert consumer.live_bar and consumer.reason is None
        assert consumer.last_bar == NOW + timedelta(minutes=1)
        assert feed.status == 'RUNNING'
        assert consumer.bridge.grid.sequence == 2
        assert consumer.clock() == NOW + timedelta(seconds=state.seconds)
        if not execute:
            for method in ('POST', 'DELETE'):
                with pytest.raises(SafetyBlocked):
                    session.client.request(method, '/fapi/v1/order')
        state.seconds += .2
        supervisor.before_poll()
        assert feed.poll() == dict(bars=0, quotes=1)
        assert consumer.bridge.account().inventory == 0

    assert run(rounds, execute=execute, offset=offset) == 0
    writes = [request for request in requests if request.get_method() != 'GET']
    assert len(writes) == int(execute)
    if execute:
        params = parse_qs(urlsplit(writes[0].full_url).query)
        assert params['recvWindow'] == ['5000']
        assert D(params['price'][0]) == D('146')
        assert params['type'] == ['LIMIT'] and params['reduceOnly'] == ['false']


@pytest.mark.parametrize('jump', [-40, 40])
def test_worker_wall_jump_does_not_change_bar_eligibility(calibrated_worker, jump):
    run, state, fake, _ = calibrated_worker

    def rounds(supervisor, state):
        supervisor.before_poll()
        state.feed.poll()
        state.wall_offset += jump
        for at in (20.2, 40.2):
            state.seconds = at
            supervisor.before_poll()
            state.feed.poll()
        state.seconds = 60.2
        supervisor.before_poll()
        state.feed.poll()
        assert state.consumer.live_bar
        assert state.consumer.last_bar == NOW + timedelta(minutes=1)
        assert state.consumer.clock() == NOW + timedelta(seconds=60.2)

    run(rounds)
    assert not posts(fake)


@pytest.mark.parametrize('unavailable', [False, True])
def test_worker_expired_calibration_refreshes_or_disconnects(calibrated_worker, unavailable):
    from factorlab.integrations.binance_futures_trading.protocol import DEMO_TIME_TTL_SECONDS
    run, state, fake, _ = calibrated_worker

    def rounds(supervisor, state):
        supervisor.before_poll()
        state.feed.poll()
        previous = sum(path.endswith('/time') for _, path, _ in fake.calls)
        state.seconds = DEMO_TIME_TTL_SECONDS + .2
        state.fail_time = unavailable
        if unavailable:
            from factorlab.errors import DataQualityError
            with pytest.raises(DataQualityError, match='account read failed'):
                supervisor.before_poll()
                state.feed.poll()
            supervisor.on_pause()
            assert not state.consumer.running and not state.consumer.live_bar
            assert state.consumer.reason == 'RECONCILE_REQUIRED'
        else:
            supervisor.before_poll()
            with pytest.raises(FeedPaused, match='R13_QUOTE_GAP'):
                state.feed.poll()
            supervisor.on_pause()
            assert sum(path.endswith('/time') for _, path, _ in fake.calls) > previous
            supervisor.before_poll()
            state.feed.poll()
            assert not state.consumer.live_bar
            assert not posts(fake)

    run(rounds)


@pytest.mark.parametrize('failure', ['stale_book', 'stale_poll'])
def test_worker_clock_wiring_preserves_market_freshness(calibrated_worker, failure):
    run, state, fake, _ = calibrated_worker

    def rounds(supervisor, state):
        supervisor.before_poll()
        state.feed.poll()
        sequence = state.consumer.bridge.grid.sequence
        for at in (20.2, 40.2):
            state.seconds = at
            supervisor.before_poll()
            state.feed.poll()
        state.seconds = 60.2
        supervisor.before_poll()
        if failure == 'stale_book':
            state.quote_age = 5001
            expected = 'STALE_OR_FUTURE_QUOTE'
        else:
            state.delay_bars = 5.1
            expected = 'STALE_POLL'
        with pytest.raises(FeedPaused, match=expected):
            state.feed.poll()
        supervisor.on_pause()
        assert not state.consumer.live_bar
        assert state.consumer.bridge.grid.sequence == sequence
        assert not posts(fake)
        state.quote_age, state.delay_bars = 100, 0
        supervisor.before_poll()
        state.feed.poll()
        assert not state.consumer.live_bar
        for at in (80.2, 100.2, 120.2):
            state.seconds = at
            supervisor.before_poll()
            state.feed.poll()
        assert state.consumer.live_bar

    run(rounds)


@pytest.mark.parametrize('failure', ['account_delay', 'kill_switch'])
def test_worker_calibrated_execution_still_requires_risk_admission(calibrated_worker, monkeypatch, failure):
    from factorlab.trading.control import RiskRejected
    run, state, fake, requests = calibrated_worker

    def rounds(supervisor, state):
        supervisor.before_poll()
        state.feed.poll()
        for at in (20.2, 40.2):
            state.seconds = at
            supervisor.before_poll()
            state.feed.poll()
        state.seconds = 60.2
        supervisor.before_poll()
        session = state.consumer.bridge.session
        if failure == 'account_delay':
            original = session._fresh_facts_for_submission
            def delayed():
                original()
                state.seconds += 6
            monkeypatch.setattr(session, '_fresh_facts_for_submission', delayed)
            expected, error = 'STALE_ACCOUNT', FeedPaused
        else:
            session.controller.set_kill_switch()
            expected, error = 'KILL_SWITCH', RiskRejected
        with pytest.raises(error, match=expected):
            state.feed.poll()
        assert not posts(fake)
        assert not [request for request in requests if request.get_method() != 'GET']

    run(rounds, execute=True)
