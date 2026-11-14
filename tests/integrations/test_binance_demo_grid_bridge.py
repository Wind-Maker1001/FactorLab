from decimal import Decimal as D

import pytest

from factorlab.integrations.binance_futures_trading.demo_grid_bridge import DemoGridBridge, minimum_quantity
from factorlab.integrations.binance_futures_trading.fake import NOW
from factorlab.integrations.binance_futures_trading.protocol import SafetyBlocked
from factorlab.strategies.grid import GridConfig
from factorlab.strategies.grid_execution import GridExecution, GridRules
from factorlab.trading import Market, OrderStatus as S
from factorlab.trading.paper import Quote
from test_binance_demo_execution import setup


def grid(account_id, **config):
    return GridExecution(GridConfig(lower=145, upper=155, levels=1, initial_price=150,
        initial_inventory=0, quantity=1, max_inventory=1, max_cash_committed=7,
        breakout='pause', **config), GridRules(D('.04'), D('.01'), D('.01'), D('.01'), D('5')),
        deployment_id='grid-demo', account_id=account_id, market=Market.BINANCE_USDM,
        instrument_id='NVDAUSDT', initial_equity=D('1000'))


def bridge(protocol=':memory:', paper=':memory:', fake=None, **config):
    a, g, c, fake = setup(protocol, paper, fake)
    c.quote(Quote('NVDAUSDT', D('150'), NOW))
    b = DemoGridBridge(grid(a.account_id, **config), c)
    b.recover()
    return b, fake


def observe(b):
    return b.observe(dict(id='quote-1', time='2026-11-11T12:00:00', price='150', volume=1))


def test_round_trip_restart_and_duplicate(tmp_path):
    protocol, paper = tmp_path / 'protocol.db', tmp_path / 'controller.db'
    b, fake = bridge(protocol, paper)
    assert observe(b).status is S.SUBMITTED
    key = b.grid.active[0].intent.client_order_id
    post = next(p for m, _, p in fake.calls if m == 'POST')
    assert post['price'] == ['145.0']
    fake.trade_update(key, '.04', '145')
    b.gateway.adapter.store.close()
    b.controller.store.close()
    b, fake = bridge(protocol, paper, fake)
    assert b.account().inventory == D('.04')
    assert b.dispatch().status is S.SUBMITTED
    sell = next(o for o in b.grid.active if o.intent.side.value == 'SELL')
    assert sell.intent.reduce_only
    fake.trade_update(sell.intent.client_order_id, '.04', '155')
    b.recover()
    assert b.account().inventory == 0
    turnover = b.account().turnover
    b.recover()
    assert b.account().turnover == turnover == D('12')
    assert len([1 for m, _, _ in fake.calls if m == 'POST']) == 2
    assert b.dispatch().status is S.SUBMITTED


def test_fragmented_fill_remainder_survives_restart(tmp_path):
    protocol, paper = tmp_path / 'protocol.db', tmp_path / 'controller.db'
    b, fake = bridge(protocol, paper)
    observe(b)
    key = b.grid.active[0].intent.client_order_id
    fake.trade_update(key, '.02', '145')
    b.recover()
    assert b.grid.reverse_remainders == {'SELL:1': D('.02')}
    assert b.dispatch() is None
    b.gateway.adapter.store.close()
    b.controller.store.close()
    fake.trade_update(key, '.02', '145')
    b, fake = bridge(protocol, paper, fake)
    assert b.account().inventory == D('.04')
    assert not b.grid.reverse_remainders
    assert b.dispatch().status is S.SUBMITTED
    sell = next(o.intent for o in b.grid.active if o.intent.side.value == 'SELL')
    assert sell.quantity == D('.04')
    assert sell.limit_price == D('155')
    assert sell.reduce_only
    assert len([1 for m, _, _ in fake.calls if m == 'POST']) == 2


def test_unknown_is_recovered_without_resubmit():
    b, fake = bridge()
    fake.timeout_next = True
    assert observe(b) is None
    with pytest.raises(SafetyBlocked):
        b.dispatch()
    b.recover()
    assert b.dispatch() is None
    assert len([1 for m, _, _ in fake.calls if m == 'POST']) == 1


def test_partial_cancel_and_duplicate():
    b, fake = bridge()
    observe(b)
    key = b.grid.active[0].intent.client_order_id
    message = fake.trade_update(key, '.02', '145')
    from factorlab.integrations.binance_futures_trading.stream import UserDataEvents
    event = UserDataEvents(b.gateway.adapter).parse(message)
    b.controller.replay(event)
    b.recover()
    assert b.account().inventory == D('.02')
    assert b.dispatch() is None
    turnover = b.account().turnover
    b.controller.replay(event)
    b.observe(dict(id='cancel', time='2026-11-11T12:00:01', price='150', volume=1,
        action='cancel', order_id=key))
    assert b.gateway.adapter.order(key).status is S.CANCELED
    assert b.account().inventory == D('.02')
    assert b.account().turnover == turnover == D('2.90')
    assert not b.grid.active


@pytest.mark.parametrize('status', ['EXPIRED', 'EXPIRED_IN_MATCH'])
def test_exchange_expiry(status):
    b, fake = bridge()
    observe(b)
    key = b.grid.active[0].intent.client_order_id
    fake.orders[key]['status'] = status
    fake.orders[key]['updateTime'] += 1
    b.recover()
    assert b.gateway.adapter.order(key).status is S.CANCELED
    assert not b.grid.active
    assert b.account().inventory == 0


def test_rejection_does_not_create_inventory():
    b, fake = bridge()
    from factorlab.integrations.binance.client import Response
    fake.response_next = Response(400, {}, b'{"code":-2019}')
    assert observe(b).status is S.REJECTED
    assert b.account().inventory == 0
    assert not b.grid.active
    assert b.dispatch() is None


@pytest.mark.parametrize('field,value', [('step_size', D('.001')), ('tick_size', D('.1')),
                                         ('min_notional', D('1'))])
def test_rules_must_match_observation(field, value):
    from dataclasses import replace
    b, _ = bridge()
    fresh = grid(b.grid.account_id)
    fresh.rules = replace(fresh.rules, **{field: value})
    with pytest.raises(ValueError, match='rules differ'):
        DemoGridBridge(fresh, b.controller)


def test_no_decisions_before_recovery():
    a, g, c, fake = setup()
    b = DemoGridBridge(grid(a.account_id), c)
    with pytest.raises(SafetyBlocked, match='recover'):
        observe(b)
    assert not [call for call in fake.calls if call[0] == 'POST']


def test_no_sell_without_inventory():
    b, fake = bridge()
    observe(b)
    assert all(o.intent.side.value == 'BUY' for o in b.grid.active)
    assert len([1 for method, _, _ in fake.calls if method == 'POST']) == 1


def test_queued_expiry_withdraws_without_exchange_cancel():
    from dataclasses import replace
    a, g, c, fake = setup()
    c.quote(Quote('NVDAUSDT', D('150'), NOW))
    fresh = grid(a.account_id)
    fresh.config = replace(fresh.config, order_ttl=1)
    b = DemoGridBridge(fresh, c)
    b.recover()
    # Persist observation, as if the process stopped before dispatch.
    b._command('observe', dict(id='first', time='2026-11-11T12:00:00', price='150', volume=1))
    b.observe(dict(id='second', time='2026-11-11T12:00:01', price='150', volume=1))
    assert not b.grid.active
    assert not [call for call in fake.calls if call[0] != 'GET']


def test_pending_atr_bar_recovery_is_durable(tmp_path):
    protocol, paper = tmp_path / 'protocol.db', tmp_path / 'controller.db'
    config = dict(mode='atr', atr_window=2, atr_multiplier=1)
    b, fake = bridge(protocol, paper, **config)
    first = dict(id='bar-1', time='2026-11-11T12:00:00', price='150', volume=1,
                 completed_bar=True, high=153, low=147, close=150)
    b._command('observe', first)
    assert b.grid.pending_bar is not None
    assert not b.grid.true_ranges
    b.gateway.adapter.store.close()
    b.controller.store.close()
    b, fake = bridge(protocol, paper, fake, **config)
    assert b.grid.pending_bar is None
    assert list(b.grid.true_ranges) == [6]
    delivery = b.grid.delivery
    b.recover()
    assert b.grid.delivery == delivery
    b.gateway.adapter.store.close()
    b.controller.store.close()
    b, fake = bridge(protocol, paper, fake, **config)
    assert b.grid.delivery == delivery
    assert list(b.grid.true_ranges) == [6]
    b.observe(dict(first, id='bar-2', time='2026-11-11T12:00:01', high=154, low=146))
    assert list(b.grid.true_ranges) == [6, 8]
    orders = tuple(b.grid.orders)
    posts = len([call for call in fake.calls if call[0] == 'POST'])
    b.gateway.adapter.store.close()
    b.controller.store.close()
    b, fake = bridge(protocol, paper, fake, **config)
    assert list(b.grid.true_ranges) == [6, 8]
    assert tuple(b.grid.orders) == orders
    b.dispatch()
    assert len([call for call in fake.calls if call[0] == 'POST']) == posts


def test_pending_atr_waits_for_reconciliation(monkeypatch):
    from dataclasses import replace
    b, fake = bridge(mode='atr', atr_window=2)
    b._command('observe', dict(id='pending', time='2026-11-11T12:00:00', price='150',
        volume=1, completed_bar=True, high=153, low=147, close=150))
    account = b.account()
    delivery = b.grid.delivery
    with monkeypatch.context() as patch:
        patch.setattr(b.gateway, 'recover', lambda _: None)
        patch.setattr(b, 'account', lambda: replace(account, reconciled=False))
        with pytest.raises(SafetyBlocked, match='RECONCILE_REQUIRED'):
            b.recover()
        assert not b.ready
        assert b.grid.pending_bar is not None
        assert b.grid.delivery == delivery
        assert not b.grid.true_ranges
    b.recover()
    assert b.grid.pending_bar is None
    assert list(b.grid.true_ranges) == [6]
    assert not [call for call in fake.calls if call[0] == 'POST']


def test_over_budget_recovery_observation_and_cancel(tmp_path):
    protocol, paper = tmp_path / 'protocol.db', tmp_path / 'controller.db'
    b, fake = bridge(protocol, paper)
    observe(b)
    key = b.grid.active[0].intent.client_order_id
    b.controller.quote(Quote('NVDAUSDT', D('180'), NOW))
    assert b.account().committed == D('7.2')
    b.recover()
    assert b.account().committed == D('7.2')
    b.gateway.adapter.store.close()
    b.controller.store.close()
    a, g, c, fake = setup(protocol, paper, fake)
    c.quote(Quote('NVDAUSDT', D('180'), NOW))
    b = DemoGridBridge(grid(a.account_id), c)
    b.recover()
    assert b.account().committed == D('7.2')
    b.observe(dict(id='breakout', time='2026-11-11T12:00:01', price='180', volume=1))
    assert b.gateway.adapter.order(key).status is S.CANCELED
    assert b.account().committed == 0
    assert len([call for call in fake.calls if call[0] == 'POST']) == 1


def test_new_buy_still_checks_market_budget():
    b, fake = bridge()
    b._command('observe', dict(id='queued', time='2026-11-11T12:00:00', price='150', volume=1))
    b.controller.quote(Quote('NVDAUSDT', D('180'), NOW))
    with pytest.raises(SafetyBlocked, match='budget'):
        b.dispatch()
    assert not [call for call in fake.calls if call[0] == 'POST']
    b.controller.quote(Quote('NVDAUSDT', D('150'), NOW))
    assert b.dispatch().status is S.SUBMITTED
    assert b.account().committed == D('6')


def test_minimum_quantity_and_config_budget():
    b, _ = bridge()
    assert minimum_quantity(b.grid.rules, D('145')) == D('.04')
    assert minimum_quantity(b.grid.rules, D('100')) == D('.05')
    g = grid(b.grid.account_id)
    from dataclasses import replace
    g.config = replace(g.config, max_inventory=2)
    with pytest.raises(SafetyBlocked, match='budget'):
        DemoGridBridge(g, b.controller)
