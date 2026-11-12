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


@pytest.mark.parametrize('change', [
    {'id':''}, {'price':float('nan')}, {'price':-1}, {'volume':-1},
    {'completed_bar':1}, {'completed_bar':True}, {'time':'bad'},
    {'time':'2026-01-01T00:00:00Z'}, {'action':'other'}])
def test_invalid_observation_atomic(change):
    from copy import deepcopy
    engine, account = setup()
    before = deepcopy(engine.__dict__, {id(engine.fees): engine.fees})
    with pytest.raises((ValueError, KeyError)):
        engine.observe(event(1) | change,account)
    assert engine.__dict__ == before


def test_replay_failure_atomic():
    engine, account = setup()
    with pytest.raises(ValueError):
        engine.replay_commands([('observe',event(1),account),('bad',)])
    assert not engine.orders and not engine.journal


@pytest.mark.parametrize('final', [S.CANCELED,S.FILLED])
def test_cancel_late_fill_does_not_seed(final):
    engine, account = setup()
    engine.observe(event(1),account)
    key = next(iter(engine.orders))
    report(engine,key,S.SUBMITTED,0,account,'a')
    engine.observe(event(2,action='cancel',order_id=key),account)
    report(engine,key,S.PARTIALLY_FILLED,1,replace(account,inventory=D(5)),'b','80')
    assert len(engine.orders) == 4
    cumulative = 1 if final == S.CANCELED else 2
    confirmation = report(engine,key,final,cumulative,replace(account,inventory=D(4+cumulative)),
                          'c', '80' if final == S.FILLED else None)
    engine.confirm(confirmation,replace(account,inventory=D(4+cumulative)))
    assert engine.orders[key].status == final
    assert len(engine.orders) == 4


def test_minimum_fee_each_partial_and_slippage():
    from factorlab.backtest.fees import FeeRule, FeeSchedule
    engine, account = setup()
    engine.rules = replace(engine.rules,step_size=D(1))
    engine.fees = FeeSchedule([FeeRule('1900-01-01',commission_bps=0,
        minimum_commission_cny=1,slippage_bps=10)])
    engine.observe(event(1),account)
    assert engine.estimated_cost(Side.BUY,D(90),D(2)) == D('2.18')
    assert engine.fees.calculate('2026-01-01','buy',180).total == 1.18


def test_checkpoint_restore_and_drift(tmp_path):
    from factorlab.trading.paper import PaperBroker, Instrument
    from factorlab.trading.control import TradingController
    from factorlab.trading.storage import PaperStore
    engine, account = setup()
    store = PaperStore(tmp_path/'paper.sqlite')
    broker = PaperBroker(Market.BINANCE_USDM,'test',(Instrument('TESTUSDT','USDT'),),{'USDT':D(10000)})
    TradingController(broker,store)
    engine.observe(event(1),account)
    with store.transaction():
        engine.checkpoint(store)
    restored, _ = setup()
    restored.restore(store)
    assert restored.orders == engine.orders
    assert restored.journal == engine.journal
    drift, _ = setup(quantity=3)
    with pytest.raises(ValueError,match='incompatible'):
        drift.restore(store)
    assert not drift.orders
    with pytest.raises(ValueError,match='transaction'):
        engine.checkpoint(store)
    store.db.close()


def test_futures_projection_not_spot_cash(tmp_path):
    from factorlab.strategies.grid_execution import project_paper_account
    from factorlab.trading.paper import PaperBroker, Instrument, Quote
    from factorlab.trading.control import TradingController, RiskLimits
    from factorlab.trading.storage import PaperStore
    from factorlab.trading.contracts import OrderIntent,OrderType
    now = datetime(2026,1,1,tzinfo=timezone.utc)
    store = PaperStore(tmp_path/'paper.sqlite')
    broker = PaperBroker(Market.BINANCE_USDM,'test',(Instrument('TESTUSDT','USDT'),),{'USDT':D(1000)},fee_rate=D(0))
    controller = TradingController(broker,store,clock=lambda:now,
        limits=RiskLimits(D(1000),D(1000),D(1000),10))
    controller.quote(Quote('TESTUSDT',D(100),now))
    controller.submit(OrderIntent(Market.BINANCE_USDM,'TESTUSDT',Side.BUY,OrderType.LIMIT,D(2),D(100),'buy'))
    controller.fill('buy',D(1),D(100))
    projected = project_paper_account(controller,'TESTUSDT',turnover=D(100))
    assert projected.inventory == 1 and projected.available == 800
    assert projected.committed == 200 and projected.equity == 1000
    assert broker.state.cash['USDT'] == 1000
    store.db.close()


def test_legacy_crossing_sequence_with_partial_feedback():
    engine, account = setup()
    engine.rules = replace(engine.rules,step_size=D(1))
    legacy = GridStrategy(engine.config)
    counter = 0
    for n, price in enumerate([100,90,90,100,110,110,100,80,80,90],1):
        observation = event(n,price)
        engine.observe(observation,account)
        offset = len(legacy.events)
        legacy.on_event(observation | {'volume':1})
        for key, order in list(engine.orders.items()):
            if order.status == S.NEW:
                counter += 1
                report(engine,key,S.SUBMITTED,0,account,f'accept{counter}')
        trades = [e for e in legacy.events[offset:] if e['kind'] == 'TRADE']
        assert len(trades) <= 1
        for trade in trades:
            old_order = legacy.orders[trade['order']]
            key = next(k for k,o in engine.orders.items()
                if engine.targets[k] == D(old_order.price) and engine.levels[k] == old_order.level
                and o.intent.side.value.lower() == old_order.side and o.status not in {S.FILLED,S.CANCELED,S.REJECTED})
            cumulative = engine.orders[key].cumulative_filled_quantity + D(trade['quantity'])
            account = GridAccount(D(legacy.inventory),D(legacy.cash),D(legacy.cash+legacy.inventory*price),
                                  D(legacy.cost_basis),D(legacy.notional))
            counter += 1
            report(engine,key,S.FILLED if cumulative == engine.orders[key].intent.quantity else S.PARTIALLY_FILLED,
                   cumulative,account,f'fill{counter}',str(trade['price']))
        expected = [(o.side,o.price,o.quantity,o.level) for o in legacy.orders.values() if o.status != 'REJECTED']
        actual = [(o.intent.side.value.lower(),float(engine.targets[k]),float(o.intent.quantity),engine.levels[k])
                  for k,o in engine.orders.items()]
        assert actual == expected
        assert engine.prices == legacy.prices
        assert engine.state == legacy.state


@pytest.mark.parametrize('stop', ['drawdown', 'turnover', 'exit'])
def test_risk_stop_discards_async_rebuild_after_late_cancels(stop):
    engine, account = setup(breakout='rebuild', min_rebuild_interval=1, max_drawdown=.01)
    engine.observe(event(1), account)
    keys = list(engine.orders)
    for i, key in enumerate(keys):
        report(engine, key, S.SUBMITTED, 0, account, f'a{i}')
    engine.observe(event(2,125), account)
    assert engine.pending_rebuild is not None
    if stop == 'exit':
        engine._risk('BREAKOUT', exit_position=True)
    else:
        account = replace(account, **({'equity':D(10000)} if stop == 'drawdown'
                                     else {'turnover':D(104000)}))
        report(engine, keys[0], S.SUBMITTED, 0, account, 'risk')
    assert engine.pending_rebuild is None
    for i, key in enumerate(keys):
        report(engine, key, S.CANCELED, 0, account, f'cancel{i}')
    assert engine.rebuilds == 0
    assert not any(o.intent.side == Side.BUY for o in engine.active)
    if stop == 'exit':
        assert engine.state == 'EXITING'
        assert len(engine.active) == 1
        assert engine.active[0].intent.reduce_only
    else:
        assert engine.state == 'PAUSED' and not engine.active
        assert engine.propose(Side.BUY,90,D(1)) is None


@pytest.mark.parametrize('block', ['unknown', 'account'])
def test_blocked_confirmations_record_fills_and_resume_after_reconciliation(block):
    engine, account = setup()
    engine.observe(event(1), account)
    keys = list(engine.orders)
    for i, key in enumerate(keys):
        report(engine, key, S.SUBMITTED, 0, account, f'a{i}')
    if block == 'unknown':
        engine.mark_unknown(keys[0])
    else:
        account = replace(account,reconciled=False)
    filled = replace(account,inventory=D(5),turnover=D(90))
    report(engine, keys[1], S.PARTIALLY_FILLED, 1, filled, 'partial','90')
    assert engine.orders[keys[1]].cumulative_filled_quantity == 1
    assert len(engine.orders) == 4
    assert engine.propose(Side.SELL,100,D(1)) is None
    reconciled = replace(filled,reconciled=True)
    if block == 'unknown':
        report(engine,keys[0],S.SUBMITTED,0,reconciled,'reconcile')
    report(engine, keys[1], S.FILLED, 2, replace(reconciled,inventory=D(6),turnover=D(180)), 'full','90')
    assert len(engine.orders) == 5


def test_async_rebuild_seed_waits_for_reconciled_account():
    engine, account = setup(breakout='rebuild',min_rebuild_interval=1)
    engine.observe(event(1),account)
    keys = list(engine.orders)
    for i,key in enumerate(keys):
        report(engine,key,S.SUBMITTED,0,account,f'a{i}')
    engine.observe(event(2,125),account)
    for i,key in enumerate(keys):
        report(engine,key,S.CANCELED,0,replace(account,reconciled=False),f'c{i}')
    assert engine.rebuilds == 0 and not engine.active
    assert engine.pending_rebuild is not None
    engine.observe(event(3,125),account)
    assert engine.rebuilds == 1 and engine.active
