"""Budget policy causality and authoritative Grid lifecycle regressions."""
from copy import deepcopy
from dataclasses import replace
import json

import numpy as np
import pandas as pd
import pytest

from factorlab.experiments.grid_regime_budget import BudgetGrid, RegimeBudgetPolicy, make_budget_factory
from factorlab.strategies.grid_execution import D
from factorlab.trading.contracts import Market, OrderStatus as S, Side
from tests.research.test_grid_execution import setup, event, observe, report, make_controller, paper_observe


def budget(*, policy='dynamic', scale=1., prices=None, threshold=.01, **changes):
    original, account = setup(**changes)
    factory = make_budget_factory(volatility_threshold=threshold,
        seed_prices=prices if prices is not None else [100.] * 25,
        policy=policy, static_scale=scale)
    kwargs = dict(deployment_id=original.deployment_id, account_id=original.account_id,
        market=original.market, instrument_id=original.instrument_id, initial_equity=original.initial_equity)
    return factory(original.config, original.rules, **kwargs), account


@pytest.mark.parametrize('mode', ['arithmetic', 'geometric', 'atr'])
def test_neutral_matches_core(mode):
    core, account = setup(mode=mode, atr_window=2)
    engine, _ = budget(policy='neutral', mode=mode, atr_window=2)
    for n in range(1, 5):
        value = event(n, completed_bar=True, high=101, low=99, close=100)
        observe(core, value, account)
        observe(engine, value, account)
        if mode == 'atr':
            core.complete_bar(delivery=core.delivery + 1)
            engine.complete_bar(delivery=engine.delivery + 1)
        assert engine.orders == core.orders
        assert engine.cancellations == core.cancellations
        assert engine.state == core.state
    json.dumps(engine.budget_events)


@pytest.mark.parametrize('up,high,state,weight,entry,pause', [
    (True, False, 'low_up', 1., 1., False),
    (False, False, 'low_down', .75, .75, False),
    (True, True, 'high_up', .75, .5, False),
    (False, True, 'high_down', .5, .25, True),
])
def test_frozen_table(up, high, state, weight, entry, pause):
    returns = np.tile([.03, -.02], 12) if high else np.full(24, .001)
    if not up:
        returns = -returns
    prices = 100 * np.exp(np.r_[0, np.cumsum(returns)])
    calendar = pd.date_range('2025-01-01', periods=25)
    result = RegimeBudgetPolicy(.01).generate_targets(
        pd.DataFrame(dict(date=calendar, symbol='TEST', close=prices)), calendar).iloc[0]
    assert (result.state, result.target_weight, result.entry_factor, result.pause_buys) == (state, weight, entry, pause)


def test_prefix_only_protocol_lag_unfinished_and_replay(monkeypatch):
    engine, account = budget(initial_inventory=0)
    calls = []
    original = RegimeBudgetPolicy.generate_targets

    def spy(self, bars, calendar):
        calls.append(bars.close.tolist())
        return original(self, bars, calendar)

    monkeypatch.setattr(RegimeBudgetPolicy, 'generate_targets', spy)
    first = event(1, price=90, completed_bar=True, high=101, low=89, close=90)
    observe(engine, first, account)
    engine.observe(first, account, delivery=1)
    observe(engine, event(2, price=95), account)
    observe(engine, event(3), account)
    assert calls == [[100.] * 25, [100.] * 24 + [90.], [100.] * 24 + [90.]]
    assert len(engine.budget_events) == 3
    other, _ = budget(initial_inventory=0)
    observe(other, first, account)
    assert other.budget_events == engine.budget_events[:1]


def test_static_shrinks_seed_and_reserves_new_orders():
    engine, account = budget(policy='static', scale=.5, initial_inventory=0, max_inventory=4)
    observe(engine, event(1), account)
    buys = [o.intent for o in engine.active if o.intent.side == Side.BUY]
    assert [o.quantity for o in buys] == [D(1), D(1)]
    assert sum(o.quantity for o in buys) == engine.inventory_cap
    assert engine.propose(Side.BUY, D(90), D(100)) is None
    assert all(o.quantity <= D(engine.config.quantity) for o in buys)


def test_min_notional_and_zero_budget():
    engine, account = budget(policy='static', scale=.1, initial_inventory=0)
    engine.rules = replace(engine.rules, min_notional=D(30))
    observe(engine, event(1), account)
    assert not engine.active
    engine, account = budget(policy='static', scale=0, initial_inventory=0)
    observe(engine, event(1), account)
    assert not engine.active


def test_stage_isolation_and_failed_observation_rollback():
    engine, account = budget()
    staged = engine._stage()
    staged.budget_prices.append(9)
    staged.budget_events.append({'x': []})
    staged.budget_policy.static_scale = .2
    assert len(engine.budget_prices) == 25 and not engine.budget_events
    assert engine.budget_policy.static_scale == 1
    observe(engine, event(2), account)
    before = engine.budget_prices.copy()
    with pytest.raises(ValueError, match='regression'):
        observe(engine, event(1), account)
    assert engine.budget_prices == before and len(engine.budget_events) == 1


@pytest.mark.parametrize('policy', ['neutral', 'dynamic'])
@pytest.mark.parametrize('changes,error', [
    ({'turnover': D(9)}, 'turnover regression'),
    ({'account_id': 'other-account'}, 'account deployment mismatch'),
    ({'instrument_id': 'OTHER'}, 'account deployment mismatch'),
    ({'market': Market.BINANCE_EQUITY}, 'account deployment mismatch'),
])
def test_observe_rejects_invalid_account_atomically(policy, changes, error, monkeypatch):
    engine, account = budget(policy=policy)
    account = replace(account, turnover=D(10))
    engine.observe(event(1, price=90, completed_bar=True, high=101, low=89, close=90), account, delivery=1)
    before = deepcopy({k: v for k, v in engine.__dict__.items() if k not in {'budget_policy', 'fees'}})
    policy_before = vars(engine.budget_policy).copy()
    decisions = []
    original = RegimeBudgetPolicy.generate_targets

    def spy(self, bars, calendar):
        decisions.append(True)
        return original(self, bars, calendar)

    monkeypatch.setattr(RegimeBudgetPolicy, 'generate_targets', spy)
    with pytest.raises(ValueError, match=error):
        engine.observe(event(2), replace(account, **changes), delivery=2)
    assert not decisions
    assert engine.account is account
    assert {k: v for k, v in engine.__dict__.items() if k not in {'budget_policy', 'fees'}} == before
    assert vars(engine.budget_policy) == policy_before
    # A rejected delivery remains available for a valid observation.
    engine.observe(event(2), account, delivery=2)
    assert engine.delivery == 2


@pytest.mark.parametrize('policy', ['neutral', 'dynamic'])
@pytest.mark.parametrize('turnover', [D(10), D(11)])
def test_observe_accepts_non_decreasing_turnover(policy, turnover):
    engine, account = budget(policy=policy)
    account = replace(account, turnover=D(10))
    engine.observe(event(1), account, delivery=1)
    updated = replace(account, turnover=turnover)
    engine.observe(event(2), updated, delivery=2)
    assert engine.account is updated
    assert engine.delivery == 2 and engine.sequence == 2


def test_cancel_race_fill_remains_authoritative_and_full_exit():
    engine, account = budget(initial_inventory=0, max_inventory=2)
    observe(engine, event(1), account)
    key = next(iter(engine.orders))
    report(engine, key, S.SUBMITTED, 0, account, 'accepted')
    # A newly selected static budget withdraws the working buy, not its authority.
    engine.budget_policy.policy = 'static'
    engine.budget_policy.static_scale = .25
    observe(engine, event(2), account)
    assert key in engine.cancellations
    partial = replace(account, inventory=D(1), committed=D(80), turnover=D(80))
    report(engine, key, S.PARTIALLY_FILLED, 1, partial, 'partial', '80')
    assert engine.account.inventory == 1
    assert engine.state == 'EXITING' and engine.reason == 'REGIME_INVENTORY_BUDGET'
    assert not any(o.intent.side == Side.SELL for o in engine.active)
    report(engine, key, S.CANCELED, 1, partial, 'canceled')
    sell = next(o for o in engine.active if o.intent.side == Side.SELL)
    assert sell.intent.quantity == 1 and sell.intent.reduce_only
    report(engine, sell.intent.client_order_id, S.SUBMITTED, 0, partial, 'exit-accepted')
    half = replace(partial, inventory=D('.5'), turnover=D(130))
    report(engine, sell.intent.client_order_id, S.PARTIALLY_FILLED, '.5', half, 'exit-partial', '100')
    assert engine.state == 'EXITING'
    flat = replace(half, inventory=D(0), turnover=D(180))
    report(engine, sell.intent.client_order_id, S.FILLED, 1, flat, 'exit-filled', '100')
    assert engine.state == 'EXITED'
    observe(engine, event(3), flat)
    assert engine.state == 'EXITED' and not engine.active


def test_checkpoint_restore_and_next_prefix(tmp_path):
    engine, _ = budget(initial_inventory=0, initial_cash=1000)
    controller = make_controller(tmp_path / 'budget.sqlite', engine.market)
    paper_observe(engine, controller, event(1, price=90, completed_bar=True, high=100, low=90, close=90))
    restored, _ = budget(initial_inventory=0, initial_cash=1000)
    restored.restore(controller)
    assert restored.budget_prices == engine.budget_prices
    assert restored.budget_events == engine.budget_events
    assert restored.inventory_multiplier == engine.inventory_multiplier
    paper_observe(restored, controller, event(2))
    assert restored.budget_events[-1]['state'] == 'high_down'
    assert len(restored.budget_events) == 2


def test_committed_budget_reserves_new_and_submitted_buys():
    engine, account = budget(policy='static', scale=.5, initial_inventory=0,
                             max_cash_committed=300)
    observe(engine, event(1), account)
    pending = sum(o.intent.quantity * max(D(100), o.intent.limit_price)
                  for o in engine.active if o.intent.side == Side.BUY)
    assert pending <= D(150)
    key = next(iter(engine.orders))
    reserved = replace(account, committed=D(150), available=account.available-D(150))
    report(engine, key, S.SUBMITTED, 0, reserved, 'accepted')
    assert engine.propose(Side.BUY, D(80), D(1)) is None


def test_factory_accounts_do_not_share_history():
    core, account = setup()
    factory = make_budget_factory(volatility_threshold=.01, seed_prices=[100.] * 25)
    kwargs = dict(deployment_id='test', account_id=core.account_id, market=core.market,
        instrument_id=core.instrument_id, initial_equity=core.initial_equity)
    first, second = [factory(core.config, core.rules, **kwargs) for _ in range(2)]
    observe(first, event(1, price=90, completed_bar=True, high=100, low=90, close=90), account)
    assert second.budget_prices == [100.] * 25 and second.budget_events == []
    assert first.deployment()['regime_budget']['policy'] == 'dynamic'
