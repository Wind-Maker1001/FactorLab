from dataclasses import replace
from decimal import Decimal as D

import pytest

from test_binance_demo_grid_network import network_grid, observe, writes
from test_binance_demo_network import NetworkFixture, wired
from factorlab.errors import DataQualityError
from factorlab.trading.control import RiskRejected
from factorlab.integrations.binance_futures_trading.demo_runtime import open_grid_session
from factorlab.integrations.binance_futures_trading.protocol import SafetyBlocked
from factorlab.integrations.binance_futures_trading.stream import UserDataEvents
from factorlab.trading import OrderStatus as S, Side


@pytest.mark.parametrize(('fault', 'reason'), [
    ('missing', 'incomplete account trades'),
    ('non_usdt', 'non-USDT commission'),
    ('notional', 'trade notional does not match'),
    ('identity', 'order response identity mismatch'),
])
def test_fill_history_fault_blocks_projection_until_authoritative_repair(
        network_grid, fault, reason):
    root, store, factory, enable, fake, requests = network_grid
    enable()
    with open_grid_session(root, store, factory) as bridge:
        bridge.recover()
        observe(bridge)
        key = bridge.grid.active[0].intent.client_order_id
        fake.trade_update(key, '.04', '146')
        confirmed = dict(fake.trades[0])
        if fault == 'missing':
            fake.trades.clear()
        elif fault == 'non_usdt':
            fake.trades[0]['commissionAsset'] = 'BNB'
        elif fault == 'identity':
            fake.orders[key]['side'] = 'SELL'
        else:
            fake.trades[0]['quoteQty'] = '5.85'
        with pytest.raises(DataQualityError, match=reason):
            bridge.recover()
        assert bridge.gateway.adapter.order(key).reconcile_required
        assert bridge.grid.active[0].cumulative_filled_quantity == 0
        assert bridge.account().inventory == 0
        assert bridge.account().turnover == 0
        with pytest.raises(SafetyBlocked, match='RECONCILE_REQUIRED'):
            bridge.dispatch()
        fake.trades[:] = [confirmed]
        fake.orders[key]['side'] = 'BUY'
        bridge.recover()
        assert bridge.account().inventory == D('.04')
        assert bridge.account().turnover == D('5.84')
        assert bridge.gateway.adapter.order(key).cumulative_fee == D('.00584')
        bridge.recover()
        assert bridge.account().turnover == D('5.84')
    with open_grid_session(root, store, factory) as bridge:
        bridge.recover()
        assert bridge.account().inventory == D('.04')
        assert bridge.account().turnover == D('5.84')
    assert len(writes(requests)) == 1


def test_duplicate_trade_message_before_and_after_restart(network_grid):
    root, store, factory, enable, fake, requests = network_grid
    enable()
    with open_grid_session(root, store, factory) as bridge:
        bridge.recover()
        observe(bridge)
        key = bridge.grid.active[0].intent.client_order_id
        message = fake.trade_update(key, '.02', '146')
        stream = UserDataEvents(bridge.gateway.adapter)
        bridge.controller.replay(stream.parse(message))
        assert not bridge.controller.replay(stream.parse(message))
        bridge.recover()
        assert bridge.account().inventory == D('.02')
        assert bridge.account().turnover == D('2.92')
        events = list(bridge.controller.store.order_events())
    with open_grid_session(root, store, factory) as bridge:
        bridge.recover()
        stream = UserDataEvents(bridge.gateway.adapter)
        assert not bridge.controller.replay(stream.parse(message))
        bridge.recover()
        assert bridge.account().inventory == D('.02')
        assert bridge.account().turnover == D('2.92')
        assert bridge.controller.account().assets[0].total == D('999.99708')
        assert list(bridge.controller.store.order_events()) == events
    assert len(writes(requests)) == 1


def test_cumulative_regression_retains_confirmed_partial_fill(network_grid):
    root, store, factory, enable, fake, requests = network_grid
    enable()
    with open_grid_session(root, store, factory) as bridge:
        bridge.recover()
        observe(bridge)
        key = bridge.grid.active[0].intent.client_order_id
        fake.trade_update(key, '.02', '146')
        bridge.recover()
        confirmed = dict(fake.orders[key])
        fake.orders[key].update(executedQty='0', cumQuote='0', status='NEW')
        with pytest.raises(DataQualityError, match='cumulative execution regression'):
            bridge.recover()
        assert bridge.account().inventory == D('.02')
        assert bridge.account().turnover == D('2.92')
        assert bridge.grid.reverse_remainders == {'SELL:1': D('.02')}
        with pytest.raises(SafetyBlocked, match='RECONCILE_REQUIRED'):
            bridge.dispatch()
        fake.orders[key] = confirmed
        bridge.recover()
        assert bridge.account().inventory == D('.02')
        assert bridge.account().turnover == D('2.92')
        assert not bridge.gateway.adapter.order(key).reconcile_required
        assert bridge.dispatch() is None
    assert len(writes(requests)) == 1


def test_lost_ack_and_failed_get_never_duplicate_post(network_grid, monkeypatch):
    root, store, factory, enable, fake, requests = network_grid
    enable()
    original = NetworkFixture.__call__
    fail_get = False

    def unavailable(self, request, timeout):
        if fail_get and request.method == 'GET' and request.path == '/fapi/v1/order':
            raise TimeoutError('synthetic order GET unavailable')
        return original(self, request, timeout)

    monkeypatch.setattr(NetworkFixture, '__call__', unavailable)
    with open_grid_session(root, store, factory) as bridge:
        bridge.recover()
        fake.timeout_next = True
        result = observe(bridge)
        key = result.intent.client_order_id
        assert result.status is S.UNKNOWN
        fake.trade_update(key, '.04', '146')
        fail_get = True
        with pytest.raises((DataQualityError, TimeoutError)):
            bridge.recover()
        assert bridge.account().inventory == 0
        assert bridge.account().turnover == 0
        with pytest.raises(SafetyBlocked):
            bridge.dispatch()
        fail_get = False
        bridge.recover()
        assert bridge.account().inventory == D('.04')
        assert bridge.account().turnover == D('5.84')
        bridge.recover()
        assert bridge.account().turnover == D('5.84')
    assert len(writes(requests)) == 1


def test_partial_fill_during_cancel_is_booked_before_risk_release(network_grid, monkeypatch):
    root, store, factory, enable, fake, requests = network_grid
    enable()
    original = NetworkFixture.__call__

    def fill_before_cancel(self, request, timeout):
        if request.method == 'DELETE' and request.path == '/fapi/v1/order':
            self.trade_update(key, '.02', '146')
        return original(self, request, timeout)

    monkeypatch.setattr(NetworkFixture, '__call__', fill_before_cancel)
    with open_grid_session(root, store, factory) as bridge:
        bridge.recover()
        observe(bridge)
        key = bridge.grid.active[0].intent.client_order_id
        bridge.observe(dict(id='breakout', time='2026-11-11T12:00:01', price='180', volume=1))
        order = bridge.gateway.adapter.order(key)
        assert order.status is S.CANCELED
        assert order.cumulative_filled_quantity == D('.02')
        assert order.cumulative_fee == D('.00292')
        assert bridge.account().inventory == D('.02')
        assert bridge.account().turnover == D('2.92')
        assert not bridge.controller.account().open_orders
        bridge.recover()
        assert bridge.account().inventory == D('.02')
        assert bridge.account().turnover == D('2.92')
    with open_grid_session(root, store, factory) as bridge:
        bridge.recover()
        assert bridge.account().inventory == D('.02')
        assert bridge.account().turnover == D('2.92')
    assert len(writes(requests)) == len(writes(requests, 'DELETE')) == 1


def test_interrupted_grid_confirmation_replays_once_after_restart(network_grid, monkeypatch):
    root, store, factory, enable, fake, requests = network_grid
    enable()
    with open_grid_session(root, store, factory) as bridge:
        bridge.recover()
        observe(bridge)
        key = bridge.grid.active[0].intent.client_order_id
        fake.trade_update(key, '.04', '146')
        ledger = bridge.controller.store
        original = ledger.record_metadata

        def interrupted(event_id, payload):
            result = original(event_id, payload)
            if event_id.startswith(bridge.prefix + 'command:') and payload['action'] == 'confirm':
                raise RuntimeError('synthetic Grid confirmation transaction interruption')
            return result

        monkeypatch.setattr(ledger, 'record_metadata', interrupted)
        with pytest.raises(RuntimeError, match='synthetic Grid confirmation'):
            bridge.recover()
        assert bridge.gateway.adapter.order(key).cumulative_filled_quantity == D('.04')
        assert bridge.grid.active[0].intent.client_order_id == key
        assert bridge.grid.active[0].cumulative_filled_quantity == 0
        with pytest.raises(SafetyBlocked, match='RECONCILE_REQUIRED'):
            bridge.dispatch()
    with open_grid_session(root, store, factory) as bridge:
        bridge.recover()
        assert bridge.account().inventory == D('.04')
        assert bridge.account().turnover == D('5.84')
        assert len(bridge.grid.active) == 1
        assert bridge.grid.active[0].intent.side is Side.SELL
        assert bridge.grid.active[0].intent.quantity == D('.04')
        bridge.recover()
        assert bridge.account().turnover == D('5.84')
        assert len(bridge.grid.active) == 1
    assert len(writes(requests)) == 1


@pytest.mark.parametrize('boundary', ['accepted_before_controller_commit', 'fill_before_controller_commit'])
def test_protocol_commit_survives_controller_rollback(network_grid, monkeypatch, boundary):
    root, store, factory, enable, fake, requests = network_grid
    enable()
    with open_grid_session(root, store, factory) as bridge:
        bridge.recover()
        if boundary == 'fill_before_controller_commit':
            observe(bridge)
            key = bridge.grid.active[0].intent.client_order_id
            fake.trade_update(key, '.04', '146')
        ledger = bridge.controller.store
        save = ledger.save

        def interrupted(state):
            save(state)
            if state.orders and (boundary == 'accepted_before_controller_commit'
                    or any(record.snapshot.cumulative_filled_quantity for record in state.orders.values())):
                raise RuntimeError('synthetic controller commit interruption')

        monkeypatch.setattr(ledger, 'save', interrupted)
        with pytest.raises(RuntimeError, match='synthetic controller commit'):
            if boundary == 'accepted_before_controller_commit':
                observe(bridge)
            else:
                bridge.recover()
        key, = fake.orders
        assert bridge.gateway.adapter.order(key).exchange_order_id == str(fake.orders[key]['orderId'])
        assert bridge.account().inventory == 0
    with open_grid_session(root, store, factory) as bridge:
        bridge.recover()
        assert bridge.gateway.adapter.order(key).intent.client_order_id == key
        expected = D('.04') if boundary == 'fill_before_controller_commit' else D(0)
        assert bridge.account().inventory == expected
        assert bridge.controller.account().assets[0].total == D('1000') - expected * D('146') * D('.001')
        turnover = bridge.account().turnover
        bridge.recover()
        assert bridge.account().turnover == turnover == expected * D('146')
    assert len(writes(requests)) == 1


def test_excess_reduce_only_sell_is_blocked_before_post(network_grid):
    root, store, factory, enable, fake, requests = network_grid
    enable()
    with open_grid_session(root, store, factory) as bridge:
        bridge.recover()
        observe(bridge)
        fake.trade_update(bridge.grid.active[0].intent.client_order_id, '.04', '146')
        bridge.recover()
        sell = bridge.grid.active[0].intent
        assert sell.side is Side.SELL and sell.reduce_only
        excessive = replace(sell, quantity=D('.05'))
        bridge.expect(excessive)
        with pytest.raises(RiskRejected, match='INVALID_REDUCE_ONLY'):
            bridge.session.submit_intent(excessive)
        assert bridge.account().inventory == D('.04')
    assert len(writes(requests)) == 1
