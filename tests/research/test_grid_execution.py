"""Deterministic legacy-decision and confirmation seam checks."""
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal as D

import pytest

from factorlab.strategies.grid import GridConfig, GridStrategy
from factorlab.strategies.grid_execution import GridAccount, GridExecution, GridRules
from factorlab.trading.contracts import Market, OrderEvent, OrderStatus as S, Side


def setup(**changes):
    config = replace(GridConfig(lower=80, upper=120, levels=4, initial_inventory=4,
        quantity=2, fee_bps=0, slippage_bps=0, max_drawdown=1, breakout='pause'), **changes)
    rules = GridRules(D(1), D('.1'), D('.1'), D('.1'), D(1))
    engine = GridExecution(config, rules, deployment_id='test', market=Market.BINANCE_USDM,
        instrument_id='TESTUSDT', initial_equity=D(10400))
    return engine, GridAccount(D(4), D(10000), D(10400), D(400), D(0))


def event(n, price=100, **extra):
    return dict(id=str(n), time=f'2026-01-{n:02d}', price=price, volume=0, **extra)


def report(engine, order, status, filled, account, identity, price=None):
    confirmation = OrderEvent(order, 'paper:'+order, status, D(filled),
        D(price) if price else None, datetime(2026, 1, 1, tzinfo=timezone.utc), 'paper', identity)
    engine.confirm(confirmation, account)
    return confirmation


@pytest.mark.parametrize('mode', ['arithmetic', 'geometric'])
def test_legacy_initial_proposals(mode):
    engine, account = setup(mode=mode)
    legacy = GridStrategy(engine.config)
    legacy.on_event(event(1))
    engine.observe(event(1), account)
    assert engine.prices == legacy.prices
    assert [(o.intent.side.value.lower(), float(engine.targets[o.intent.client_order_id]), float(o.intent.quantity))
            for o in engine.active] == [(o.side, o.price, o.quantity) for o in legacy.active_orders]


def test_partial_full_duplicate_and_replay():
    engine, account = setup()
    engine.observe(event(1), account)
    key = next(k for k,o in engine.orders.items() if o.intent.limit_price == 90)
    accepted = report(engine,key,S.SUBMITTED,0,account,'a')
    filled_account = replace(account, inventory=D(5), turnover=D(90))
    partial = report(engine,key,S.PARTIALLY_FILLED,1,filled_account,'b','90')
    count = len(engine.orders)
    engine.confirm(partial,filled_account)
    assert len(engine.orders) == count
    assert engine.orders[key].intent.quantity-engine.orders[key].cumulative_filled_quantity == 1
    assert any(o.intent.side == Side.SELL and o.intent.limit_price == 100 for o in engine.active)
    report(engine,key,S.FILLED,2,replace(filled_account,inventory=D(6)),'c','90')
    replay, _ = setup()
    replay.replay_commands(engine.journal)
    assert replay.orders == engine.orders
    assert replay.targets == engine.targets


def test_expiry_waits_for_confirmation_and_unknown_blocks():
    engine, account = setup(order_ttl=1)
    engine.observe(event(1),account)
    key = next(iter(engine.orders))
    report(engine,key,S.SUBMITTED,0,account,'a')
    engine.mark_unknown(key)
    with pytest.raises(ValueError,match='RECONCILE_REQUIRED'):
        engine.observe(event(2),account)
    report(engine,key,S.SUBMITTED,0,account,'b')
    engine.observe(event(2),account)
    assert key in engine.expirations and key in engine.cancellations
    assert engine.orders[key].status == S.SUBMITTED
    report(engine,key,S.CANCELED,0,account,'c')
    assert key not in engine.cancellations


def test_atr_legacy_range():
    engine, account = setup(mode='atr',atr_window=2,atr_multiplier=2)
    legacy = GridStrategy(engine.config)
    for e in [event(1,completed_bar=True,high=102,low=98,close=100),
              event(2,completed_bar=True,high=103,low=97,close=100)]:
        engine.observe(e,account)
        legacy.on_event(e)
    assert engine.prices == legacy.prices
    assert engine.rebuilds == legacy.rebuilds == 1


@pytest.mark.parametrize('changes,account_changes', [
    ({'max_drawdown':.01},{'equity':D(10000)}),
    ({'max_turnover':1},{'turnover':D(10400)})])
def test_risk_matches_legacy_reason(changes,account_changes):
    engine, account = setup(**changes)
    engine.observe(event(1),account)
    engine.observe(event(2),replace(account,**account_changes))
    assert engine.state == 'PAUSED'
    assert engine.cancellations == set(engine.orders)


def test_fractional_rules_and_inventory():
    engine, account = setup()
    engine.rules = GridRules(D('.15'),D('.1'),D('3'),D('.1'),D(1))
    engine.observe(event(1),replace(account,inventory=D('.1')))
    assert all(o.intent.quantity == D('.3') for o in engine.active)
    assert all(o.intent.side == Side.BUY for o in engine.active)
    assert all(o.intent.limit_price % 3 == 0 for o in engine.active)
    assert any(engine.targets[k] != o.intent.limit_price for k,o in engine.orders.items())


def test_insufficient_available_and_committed():
    engine, account = setup()
    engine.observe(event(1),replace(account,available=D(0)))
    assert all(o.intent.side == Side.SELL for o in engine.active)


def test_rebuild_retains_reservations_until_cancel():
    engine, account = setup(breakout='rebuild',min_rebuild_interval=1)
    engine.observe(event(1),account)
    keys = list(engine.orders)
    for i,key in enumerate(keys):
        report(engine,key,S.SUBMITTED,0,account,f'a{i}')
    engine.observe(event(2,125),account)
    assert engine.pending_rebuild is not None and engine.rebuilds == 0
    for i,key in enumerate(keys):
        report(engine,key,S.CANCELED,0,account,f'c{i}')
    assert engine.pending_rebuild is None and engine.rebuilds == 1
    assert engine.center == 125
