"""Deterministic legacy-decision and confirmation seam checks."""
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal as D
import sqlite3

import pytest

from factorlab.strategies.grid import GridConfig, GridStrategy
from factorlab.strategies.grid_execution import (
    GridAccount, GridConfirmationConflict, GridExecution, GridRules, project_paper_account,
)
from factorlab.trading.contracts import Market, OrderEvent, OrderIntent, OrderStatus as S, OrderType, Side
from factorlab.trading.control import RiskLimits, TradingController
from factorlab.trading.paper import Instrument, PaperBroker, Quote
from factorlab.trading.storage import PaperStore, dumps


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
SYMBOL = 'TESTUSDT'
ACCOUNT = 'paper-grid'


def setup(*, market=Market.BINANCE_USDM, account_id=ACCOUNT, deployment_id='test', **changes):
    config = replace(GridConfig(lower=80, upper=120, levels=4, initial_inventory=4,
        quantity=2, fee_bps=0, slippage_bps=0, max_drawdown=1, breakout='pause'), **changes)
    rules = GridRules(D(1), D('.1'), D('.1'), D('.1'), D(1))
    entry = D(config.initial_inventory) * D(config.initial_price)
    equity = D(config.initial_cash) + entry
    engine = GridExecution(config, rules, deployment_id=deployment_id, account_id=account_id,
        market=market, instrument_id=SYMBOL, initial_equity=equity)
    return engine, GridAccount(D(config.initial_inventory), D(config.initial_cash), equity, entry,
                               D(0), account_id, SYMBOL, market)


def event(n, price=100, volume=0, **extra):
    return dict(id=str(n), time=f'2026-01-{n:02d}', price=price, volume=volume) | extra


def observe(engine, value, account):
    engine.observe(value, account, delivery=engine.delivery + 1)


def confirmation(order, status, filled, identity, price=None, *, at=NOW):
    return OrderEvent(order, 'paper:'+order, status, D(filled), D(price) if price is not None else None,
                      at, 'paper', identity)


def report(engine, order, status, filled, account, identity, price=None):
    value = confirmation(order, status, filled, identity, price)
    engine.confirm(value, account, delivery=engine.delivery + 1)
    return value


@pytest.mark.parametrize('mode', ['arithmetic', 'geometric'])
def test_legacy_initial_proposals(mode):
    engine, account = setup(mode=mode)
    legacy = GridStrategy(engine.config)
    legacy.on_event(event(1))
    observe(engine, event(1), account)
    assert engine.prices == legacy.prices
    assert [(o.intent.side.value.lower(), float(engine.targets[o.intent.client_order_id]), float(o.intent.quantity))
            for o in engine.active] == [(o.side, o.price, o.quantity) for o in legacy.active_orders]


def test_partial_full_duplicate():
    engine, account = setup()
    observe(engine, event(1), account)
    key = next(k for k,o in engine.orders.items() if o.intent.limit_price == 90)
    report(engine,key,S.SUBMITTED,0,account,'a')
    filled_account = replace(account, inventory=D(5), turnover=D(90))
    partial = report(engine,key,S.PARTIALLY_FILLED,1,filled_account,'b','90')
    count = engine.issued_orders
    engine.confirm(partial, filled_account, delivery=engine.delivery)
    assert engine.issued_orders == count
    assert engine.orders[key].intent.quantity-engine.orders[key].cumulative_filled_quantity == 1
    assert any(o.intent.side == Side.SELL and o.intent.limit_price == 100 for o in engine.active)
    report(engine,key,S.FILLED,2,replace(filled_account,inventory=D(6),turnover=D(180)),'c','90')
    assert key not in engine.orders


def test_expiry_waits_for_confirmation_and_unknown_blocks():
    engine, account = setup(order_ttl=1)
    observe(engine, event(1), account)
    key = next(iter(engine.orders))
    report(engine,key,S.SUBMITTED,0,account,'a')
    engine.mark_unknown(key, delivery=engine.delivery + 1)
    with pytest.raises(ValueError,match='RECONCILE_REQUIRED'):
        observe(engine, event(2), account)
    report(engine,key,S.SUBMITTED,0,account,'b')
    observe(engine, event(2), account)
    assert key in engine.expirations and key in engine.cancellations
    assert engine.orders[key].status == S.SUBMITTED
    report(engine,key,S.CANCELED,0,account,'c')
    assert key not in engine.cancellations


def test_atr_legacy_range():
    engine, account = setup(mode='atr',atr_window=2,atr_multiplier=2)
    legacy = GridStrategy(engine.config)
    for e in [event(1,completed_bar=True,high=102,low=98,close=100),
              event(2,completed_bar=True,high=103,low=97,close=100)]:
        observe(engine, e, account)
        engine.complete_bar(delivery=engine.delivery + 1)
        legacy.on_event(e)
    assert engine.prices == legacy.prices
    assert engine.rebuilds == legacy.rebuilds == 1


@pytest.mark.parametrize('changes,account_changes', [
    ({'max_drawdown':.01},{'equity':D(10000)}),
    ({'max_turnover':1},{'turnover':D(10400)})])
def test_risk_matches_legacy_reason(changes,account_changes):
    engine, account = setup(**changes)
    observe(engine, event(1), account)
    observe(engine, event(2), replace(account, **account_changes))
    assert engine.state == 'PAUSED'
    assert engine.cancellations == set(engine.orders)


def test_fractional_rules_and_inventory():
    engine, account = setup()
    engine.rules = GridRules(D('.15'),D('.1'),D('3'),D('.1'),D(1))
    observe(engine, event(1), replace(account, inventory=D('.1')))
    assert all(o.intent.quantity == D('.3') for o in engine.active)
    assert all(o.intent.side == Side.BUY for o in engine.active)
    assert all(o.intent.limit_price % 3 == 0 for o in engine.active)
    assert any(engine.targets[k] != o.intent.limit_price for k,o in engine.orders.items())


def test_insufficient_available_and_committed():
    engine, account = setup()
    observe(engine, event(1), replace(account, available=D(0)))
    assert all(o.intent.side == Side.SELL for o in engine.active)


def test_rebuild_retains_reservations_until_cancel():
    engine, account = setup(breakout='rebuild',min_rebuild_interval=1)
    observe(engine, event(1), account)
    keys = list(engine.orders)
    for i,key in enumerate(keys):
        report(engine,key,S.SUBMITTED,0,account,f'a{i}')
    observe(engine, event(2,125), account)
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
        observe(engine, event(1) | change, account)
    assert engine.__dict__ == before


def test_cross_stream_order_and_account_binding():
    engine, account = setup()
    with pytest.raises(ValueError, match='account deployment mismatch'):
        observe(engine, event(1), replace(account, account_id='another-account'))
    observe(engine, event(2), account)
    key = next(iter(engine.orders))
    value = confirmation(key, S.SUBMITTED, 0, 'submitted', at=NOW.replace(hour=1))
    with pytest.raises(ValueError, match='consecutive'):
        engine.confirm(value, account, delivery=3)
    with pytest.raises(ValueError, match='payload conflict'):
        engine.confirm(value, account, delivery=1)
    engine.confirm(value, account, delivery=2)
    engine.confirm(value, account, delivery=2)
    with pytest.raises(ValueError, match='consecutive'):
        engine.observe(event(2), account, delivery=1)
    with pytest.raises(ValueError, match='observation time regression'):
        observe(engine, event(1), account)
    with pytest.raises(ValueError, match='out-of-order confirmation'):
        report(engine, key, S.CANCELED, 0, account, 'old-cancel')
    assert engine.delivery == 2 and engine.orders[key].status == S.SUBMITTED


@pytest.mark.parametrize('final', [S.CANCELED,S.FILLED])
def test_cancel_late_fill_does_not_seed(final):
    engine, account = setup()
    observe(engine, event(1), account)
    key = next(iter(engine.orders))
    report(engine,key,S.SUBMITTED,0,account,'a')
    observe(engine, event(2,action='cancel',order_id=key), account)
    report(engine,key,S.PARTIALLY_FILLED,1,replace(account,inventory=D(5)),'b','80')
    assert len(engine.orders) == 4
    cumulative = 1 if final == S.CANCELED else 2
    confirmation = report(engine,key,final,cumulative,replace(account,inventory=D(4+cumulative)),
                          'c', '80' if final == S.FILLED else None)
    engine.confirm(confirmation, replace(account, inventory=D(4+cumulative)), delivery=engine.delivery)
    assert key not in engine.orders
    assert engine.issued_orders == 4


def test_minimum_fee_each_partial_and_slippage():
    from factorlab.backtest.fees import FeeRule, FeeSchedule
    engine, account = setup()
    engine.rules = replace(engine.rules,step_size=D(1))
    engine.fees = FeeSchedule([FeeRule('1900-01-01',commission_bps=0,
        minimum_commission_cny=1,slippage_bps=10)])
    observe(engine, event(1), account)
    assert engine.estimated_cost(Side.BUY,D(90),D(2)) == D('2.18')
    assert engine.fees.calculate('2026-01-01','buy',180).total == 1.18


def make_controller(path, market, *, fee_rate=D(0)):
    currency = 'USDT' if market == Market.BINANCE_USDM else 'USD'
    store = PaperStore(path)
    broker = PaperBroker(market, ACCOUNT, (Instrument(SYMBOL, currency),), {currency: D(1000)}, fee_rate=fee_rate)
    return TradingController(broker, store, clock=lambda: NOW,
        limits=RiskLimits(D(1000), D(1000), D(1000), 10))


def setup_paper(**changes):
    engine, _ = setup(initial_inventory=0, initial_cash=1000, **changes)
    return engine


def paper_observe(engine, controller, observation):
    engine.observe_paper(controller, observation, delivery=engine.delivery + 1, observed_at=NOW)


def paper_confirm(engine, controller, value):
    engine.confirm_paper(controller, value, delivery=engine.delivery + 1)


def test_paper_commit_rollback_and_restart(tmp_path, monkeypatch):
    path = tmp_path/'paper.sqlite'
    engine = setup_paper()
    controller = make_controller(path, engine.market)
    store = controller.store
    paper_observe(engine, controller, event(1))
    key, other = engine.orders
    engine.submit_paper(controller, key, delivery=2)
    submitted = controller.broker.state.confirmations[key]
    fill = confirmation(key, S.PARTIALLY_FILLED, 1, 'partial', '75')
    before = store.load()

    def deny_commit(action, argument, *_):
        if action == sqlite3.SQLITE_TRANSACTION and argument == 'COMMIT':
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    # Set the authorizer after writing the account/checkpoint to exercise COMMIT failure.
    save = store.save

    def fail_commit(state):
        save(state)
        store.db.set_authorizer(deny_commit)

    with monkeypatch.context() as patch:
        patch.setattr(store, 'save', fail_commit)
        with pytest.raises(sqlite3.DatabaseError, match='authorized'):
            paper_confirm(engine, controller, fill)
    store.db.set_authorizer(None)
    assert controller.broker.state == store.load() == before
    assert store.event_payload(fill.event_id) is None
    assert engine.delivery == 2 and engine.orders[key].status == S.SUBMITTED

    paper_confirm(engine, controller, fill)
    assert engine.account.inventory == 1 and engine.account.turnover == 75

    def lose_submission(intent):
        raise TimeoutError(intent.client_order_id)

    with monkeypatch.context() as patch:
        patch.setattr(controller.broker, 'submit_order', lose_submission)
        engine.submit_paper(controller, other, delivery=4)
    assert engine.orders[other].status == S.UNKNOWN
    assert other not in controller.broker.state.confirmations
    working = list(engine.orders)
    store.close()

    controller = make_controller(path, engine.market)
    store = controller.store
    restored = setup_paper()
    restored.restore(controller)
    assert list(restored.orders) == working
    assert restored.orders[key].status == restored.orders[other].status == S.UNKNOWN
    assert restored.orders[key].cumulative_filled_quantity == 1
    assert restored.account == project_paper_account(controller, SYMBOL)
    assert restored.delivery == 4 and restored.issued_orders == 3
    assert not restored.account.reconciled
    with pytest.raises(ValueError, match='RECONCILE_REQUIRED'):
        paper_observe(restored, controller, event(2))
    assert not controller.reconcile().reconciled
    paper_confirm(restored, controller, confirmation(other, S.SUBMITTED, 0, 'resolved-submission'))
    assert controller.reconcile().reconciled
    paper_observe(restored, controller, event(2))
    paper_confirm(restored, controller, submitted)  # Exact journal retry after a partial fill.
    assert restored.orders[key].status == S.PARTIALLY_FILLED
    paper_confirm(restored, controller, confirmation(key, S.FILLED, 2, 'full', '78'))
    assert key not in restored.orders and restored.issued_orders == 4
    assert restored.account.inventory == 2 and restored.account.turnover == 153
    assert restored.account == project_paper_account(controller, SYMBOL)
    store.close()


def test_paper_rejects_drift_stale_writer_and_corrupt_checkpoint(tmp_path):
    engine = setup_paper()
    controller = make_controller(tmp_path/'paper.sqlite', engine.market)
    store = controller.store
    paper_observe(engine, controller, event(1))
    with pytest.raises(ValueError, match='incompatible Grid checkpoint deployment'):
        setup_paper(quantity=3).restore(controller)
    with pytest.raises(ValueError, match='incompatible Grid checkpoint deployment'):
        setup_paper(deployment_id='second-grid').restore(controller)
    limits = controller.limits
    controller.limits = replace(limits, orders_per_minute=20)
    with pytest.raises(ValueError, match='incompatible Grid checkpoint deployment'):
        setup_paper().restore(controller)
    controller.limits = limits
    stale = setup_paper()
    stale.restore(controller)
    key = next(iter(engine.orders))
    engine.submit_paper(controller, key, delivery=2)
    with pytest.raises(ValueError, match='stale Grid checkpoint revision'):
        paper_observe(stale, controller, event(2))

    # An independently booked fill invalidates the Grid checkpoint.
    checkpoint = store.load()
    controller.fill(key, D(1), D(75))
    with pytest.raises(ValueError, match='authority reconciliation'):
        setup_paper().restore(controller)
    with pytest.raises(ValueError, match='authority reconciliation'):
        paper_observe(engine, controller, event(2))

    with store.transaction():
        checkpoint.runtime['grid_execution']['checkpoint']['decision']['state'] = 'PAUSED'
        store.save(checkpoint)
    fresh = setup_paper()
    with pytest.raises(ValueError, match='corrupt Grid checkpoint'):
        fresh.restore(controller)
    assert fresh.delivery == 0 and not fresh.orders
    store.close()


@pytest.mark.parametrize('market', [Market.BINANCE_USDM, Market.BINANCE_EQUITY])
def test_projection_and_turnover_through_full_account_lifecycle(tmp_path, market):
    controller = make_controller(tmp_path/'paper.sqlite', market, fee_rate=D('.01'))
    futures = market == Market.BINANCE_USDM
    controller.quote(Quote(SYMBOL, D(100), NOW))

    def project(inventory, available, equity, committed, turnover, *, reconciled=True):
        assert project_paper_account(controller, SYMBOL) == GridAccount(
            D(inventory), D(available), D(equity), D(committed), D(turnover), ACCOUNT, SYMBOL, market, reconciled)

    def submit(key, side, quantity):
        intent = OrderIntent(market, SYMBOL, side, OrderType.LIMIT, D(quantity), D(100), key,
                             reduce_only=side == Side.SELL)
        controller.submit(intent)

    submit('buy', Side.BUY, 3)
    controller.fill('buy', D(1), D(90))
    project(1, '707.10', '1009.10', 292, 90)
    controller.fill('buy', D(1), D(98))
    project(2, '709.12', '1010.12', 289, 188)
    controller.mark_timeout('buy')
    project(2, '709.12', '1010.12', 289, 188, reconciled=False)
    controller.reconcile()
    controller.cancel('buy')
    project(2, '810.12', '1010.12', 188, 188)
    submit('reduce', Side.SELL, 1)
    controller.fill('reduce', D(1), D(110))
    project(1, '919.02', '1019.02', 94, 298)
    assert controller.broker.state.cash['USDT' if futures else 'USD'] == D('1013.02' if futures else '919.02')
    submit('close', Side.SELL, 1)
    controller.fill('close', D(1), D(100))
    project(0, '1018.02', '1018.02', 0, 398)
    # Attaching after funded history starts strategy turnover at the deployment's baseline.
    engine, _ = setup(market=market, initial_cash=D('1018.02'), initial_inventory=0)
    paper_observe(engine, controller, event(1))
    assert engine.account.turnover == 0
    controller.store.close()


def test_canceled_order_late_fill_blocks_and_survives_restart(tmp_path):
    path = tmp_path/'paper.sqlite'
    engine = setup_paper()
    controller = make_controller(path, engine.market)
    paper_observe(engine, controller, event(1))
    key, proposal = engine.orders
    engine.submit_paper(controller, key, delivery=2)
    paper_confirm(engine, controller, confirmation(key, S.PARTIALLY_FILLED, 1, 'partial', '80'))
    canceled = confirmation(key, S.CANCELED, 1, 'canceled')
    paper_confirm(engine, controller, canceled)
    paper_confirm(engine, controller, canceled)
    before = project_paper_account(controller, SYMBOL)
    conflict = confirmation(key, S.FILLED, 2, 'late-fill', '80')
    with pytest.raises(GridConfirmationConflict, match='late-fill'):
        paper_confirm(engine, controller, conflict)
    assert controller.store.event_payload(conflict.event_id) is None
    assert controller.broker.state.orders[key].snapshot.status == S.CANCELED
    assert project_paper_account(controller, SYMBOL) == before
    assert engine.issued_orders == 3
    controller.store.close()

    controller = make_controller(path, engine.market)
    controller.reconcile()
    restored = setup_paper()
    restored.restore(controller)
    with pytest.raises(GridConfirmationConflict, match='late-fill'):
        restored.submit_paper(controller, proposal, delivery=restored.delivery + 1)
    with pytest.raises(GridConfirmationConflict, match='late-fill'):
        paper_observe(restored, controller, event(2))
    assert project_paper_account(controller, SYMBOL) == before
    controller.store.close()


def test_checkpoint_size_tracks_working_orders_and_atr_window(tmp_path):
    controller = make_controller(tmp_path/'paper.sqlite', Market.BINANCE_USDM)
    engine = setup_paper(mode='atr', atr_window=2, order_ttl=1)
    for n in range(1, 241):
        observation = event(n, completed_bar=True, high=103, low=97, close=100, time='2026-01-01')
        paper_observe(engine, controller, observation)
        if n == 2:
            engine = setup_paper(mode='atr', atr_window=2, order_ttl=1)
            engine.restore(controller)
        engine.complete_bar_paper(controller, delivery=engine.delivery + 1)
        if n == 4:
            first_size = len(dumps(controller.broker.state.runtime['grid_execution']))
    payload = controller.broker.state.runtime['grid_execution']
    assert len(dumps(payload)) <= first_size + 32
    assert len(payload['checkpoint']['true_ranges']) == 2
    assert not payload['checkpoint']['working'] and not engine.orders
    assert not engine.records and not engine.cancellations and not engine.expirations
    assert engine.delivery == 480 and controller.store.event_cursor() == 0
    controller.store.close()


def test_legacy_crossing_sequence_with_partial_feedback():
    engine, account = setup()
    engine.rules = replace(engine.rules,step_size=D(1))
    legacy = GridStrategy(engine.config)
    counter = 0
    for n, price in enumerate([100,90,90,100,110,110,100,80,80,90],1):
        observation = event(n,price)
        observe(engine, observation, account)
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
            account = legacy_account(engine, legacy, price)
            counter += 1
            report(engine,key,S.FILLED if cumulative == engine.orders[key].intent.quantity else S.PARTIALLY_FILLED,
                   cumulative,account,f'fill{counter}',str(trade['price']))
        expected = [(o.side,o.price,o.quantity,o.level) for o in legacy.active_orders]
        actual = [(o.intent.side.value.lower(),float(engine.targets[k]),float(o.intent.quantity),engine.levels[k])
                  for k,o in engine.orders.items()]
        assert actual == expected
        assert engine.prices == legacy.prices
        assert engine.state == legacy.state


@pytest.mark.parametrize('stop', ['drawdown', 'turnover', 'exit'])
def test_risk_stop_discards_async_rebuild_after_late_cancels(stop):
    engine, account = setup(breakout='rebuild', min_rebuild_interval=1, max_drawdown=.01)
    observe(engine, event(1), account)
    keys = list(engine.orders)
    for i, key in enumerate(keys):
        report(engine, key, S.SUBMITTED, 0, account, f'a{i}')
    observe(engine, event(2, 125), account)
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
    observe(engine, event(1), account)
    keys = list(engine.orders)
    for i, key in enumerate(keys):
        report(engine, key, S.SUBMITTED, 0, account, f'a{i}')
    if block == 'unknown':
        engine.mark_unknown(keys[0], delivery=engine.delivery + 1)
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
    assert engine.issued_orders == 5


def test_async_rebuild_seed_waits_for_reconciled_account():
    engine, account = setup(breakout='rebuild',min_rebuild_interval=1)
    observe(engine, event(1), account)
    keys = list(engine.orders)
    for i,key in enumerate(keys):
        report(engine,key,S.SUBMITTED,0,account,f'a{i}')
    observe(engine, event(2, 125), account)
    for i,key in enumerate(keys):
        report(engine,key,S.CANCELED,0,replace(account,reconciled=False),f'c{i}')
    assert engine.rebuilds == 0 and not engine.active
    assert engine.pending_rebuild is not None
    observe(engine, event(3, 125), account)
    assert engine.rebuilds == 1 and engine.active


def legacy_account(engine, legacy, price):
    return GridAccount(D(legacy.inventory), D(legacy.cash), D(legacy.cash + legacy.inventory * price),
                       D(legacy.cost_basis), D(legacy.notional), engine.account_id, engine.instrument_id, engine.market)


def assert_legacy_decision(engine, legacy):
    def signature(side, market, target, quantity, level, remaining):
        return side, market, 0 if market else float(target), quantity, level, remaining

    actual = []
    for key, order in engine.orders.items():
        intent = order.intent
        actual.append(signature(intent.side.value.lower(), intent.order_type == OrderType.MARKET,
                                engine.targets[key], intent.quantity, engine.levels[key],
                                intent.quantity - order.cumulative_filled_quantity))
    expected = [signature(o.side, o.market, o.price, o.quantity, o.level, o.remaining)
                for o in legacy.active_orders]
    assert actual == expected
    assert (engine.prices, engine.center, engine.state, engine.rebuilds) == (
        legacy.prices, legacy.center, legacy.state, legacy.rebuilds)


def accept_external_proposals(engine, account):
    for key, order in list(engine.orders.items()):
        if order.status == S.NEW:
            report(engine, key, S.SUBMITTED, 0, account, f'accepted-{key}')


def settle_external_cancels(engine, account):
    for key in list(engine.orders):
        if key in engine.cancellations:
            order = engine.orders[key]
            report(engine, key, S.CANCELED, order.cumulative_filled_quantity, account, f'canceled-{key}')


def test_legacy_user_cancel_and_expiry():
    engine, account = setup(order_ttl=2)
    legacy = GridStrategy(engine.config)
    first = event(1)
    legacy.on_event(first)
    observe(engine, first, account)
    assert_legacy_decision(engine, legacy)
    accept_external_proposals(engine, account)
    buy = next(key for key, order in engine.orders.items() if order.intent.limit_price == 90)
    old = next(order.id for order in legacy.active_orders if order.price == 90)

    user_cancel = event(2, 90, volume=2, action='cancel', order_id=old)
    legacy.on_event(user_cancel)
    observe(engine, user_cancel | {'order_id': buy}, legacy_account(engine, legacy, 90))
    assert engine.cancellations == {buy} and not engine.expirations
    settle_external_cancels(engine, engine.account)
    assert_legacy_decision(engine, legacy)

    expiry = event(3, 80, volume=2)
    legacy.on_event(expiry)
    observe(engine, expiry, legacy_account(engine, legacy, 80))
    assert engine.expirations == set(engine.orders)
    settle_external_cancels(engine, engine.account)
    assert legacy.notional == engine.account.turnover == 0
    assert_legacy_decision(engine, legacy)


def test_legacy_atr_rebuild_after_same_observation_fill():
    engine, _ = setup(mode='atr', atr_window=2, rebuild_threshold=1, min_rebuild_interval=2, breakout='rebuild')
    legacy = GridStrategy(engine.config)
    bars = [(100, 102, 98), (100, 103, 97), (102, 103, 101), (108, 109, 107),
            (108, 109, 107), (130, 131, 129)]
    for n, (close, high, low) in enumerate(bars, 1):
        observation = event(n, close, volume=int(n == 4), completed_bar=True, high=high, low=low, close=close)
        pre_fill = legacy_account(engine, legacy, close)
        observe(engine, observation, pre_fill)
        offset = len(legacy.events)
        legacy.on_event(observation)
        trades = [entry for entry in legacy.events[offset:] if entry['kind'] == 'TRADE']
        assert len(trades) == int(n == 4)
        for trade in trades:
            key = next(key for key, order in engine.orders.items() if order.intent.limit_price == trade['price'])
            report(engine, key, S.PARTIALLY_FILLED, trade['quantity'], legacy_account(engine, legacy, close),
                   'before-rebuild', trade['price'])
            assert not engine.cancellations and engine.rebuilds == 1
            accept_external_proposals(engine, engine.account)
        engine.complete_bar(delivery=engine.delivery + 1)
        settle_external_cancels(engine, engine.account)
        if n == len(bars):
            # The breakout already chose a spacing from the prior ATR window.
            assert engine.center == 130 and engine.rebuilds == 3
        assert_legacy_decision(engine, legacy)
        accept_external_proposals(engine, engine.account)
        if n == 4:
            assert engine.rebuilds == 2 and engine.center == 108


def test_legacy_breakout_exit_across_paper_restart(tmp_path):
    path = tmp_path/'paper.sqlite'
    engine = setup_paper(breakout='exit')
    controller = make_controller(path, engine.market)
    controller.quote(Quote(SYMBOL, D(100), NOW))
    controller.submit(OrderIntent(engine.market, SYMBOL, Side.BUY, OrderType.LIMIT, D(4), D(100), 'funding'))
    controller.fill('funding', D(4), D(100))
    legacy = GridStrategy(replace(engine.config, initial_cash=600, initial_inventory=4))

    first = event(1)
    legacy.on_event(first)
    paper_observe(engine, controller, first)
    for key in engine.orders:
        engine.submit_paper(controller, key, delivery=engine.delivery + 1)
    assert_legacy_decision(engine, legacy)

    breakout = event(2, 125)
    legacy.on_event(breakout)
    paper_observe(engine, controller, breakout)
    assert engine.state == 'EXITING' and set(engine.orders) == engine.cancellations
    controller.store.close()

    controller = make_controller(path, engine.market)
    engine = setup_paper(breakout='exit')
    engine.restore(controller)
    assert all(order.status == S.UNKNOWN for order in engine.active)
    controller.reconcile()
    for key in list(engine.cancellations):
        paper_confirm(engine, controller, confirmation(key, S.CANCELED, 0, f'canceled-{key}'))
    assert_legacy_decision(engine, legacy)
    exit_order, = engine.active
    assert exit_order.intent.reduce_only and exit_order.intent.order_type == OrderType.MARKET
    key = exit_order.intent.client_order_id
    engine.submit_paper(controller, key, delivery=engine.delivery + 1)
    next_tick = event(3, 125, volume=4)
    legacy.on_event(next_tick)
    paper_observe(engine, controller, next_tick)
    paper_confirm(engine, controller, confirmation(key, S.FILLED, 4, 'liquidated', '125'))
    assert_legacy_decision(engine, legacy)
    assert engine.account.inventory == legacy.inventory == 0
    assert engine.account.turnover == legacy.notional == 500
    assert engine.account.equity == legacy.cash == 1100
    controller.store.close()


@pytest.mark.parametrize('risk', ['MAX_DRAWDOWN', 'MAX_TURNOVER'])
def test_legacy_settled_risk_after_fill(risk):
    if risk == 'MAX_DRAWDOWN':
        engine, account = setup(max_drawdown=.0043)
        price, target, volume = 89, 90, 2
    else:
        engine, account = setup(initial_cash=9600, max_turnover=.016)
        price, target, volume = 80, 80, 3
    legacy = GridStrategy(engine.config)
    first = event(1)
    legacy.on_event(first)
    observe(engine, first, account)
    accept_external_proposals(engine, account)
    second = event(2, price, volume=volume)
    observe(engine, second, legacy_account(engine, legacy, price))
    assert engine.state == 'RUNNING'
    legacy.on_event(second)
    key = next(key for key, order in engine.orders.items() if order.intent.limit_price == target)
    report(engine, key, S.FILLED, 2, legacy_account(engine, legacy, price), 'risk-fill', target)
    settle_external_cancels(engine, engine.account)
    assert_legacy_decision(engine, legacy)
    assert engine.state == 'PAUSED' and engine.reason == risk
    assert [entry['reason'] for entry in legacy.events if entry['kind'] == 'RISK'] == [risk]
