"""Risk-focused deterministic grid acceptance."""
from dataclasses import replace
import json

import pytest

from factorlab.backtest.fees import FeeRule, FeeSchedule
from factorlab.errors import ConfigurationError
from factorlab.strategies.grid import GridConfig, GridStrategy


def config(**kwargs):
    return replace(GridConfig(lower=80, upper=120, levels=4, initial_inventory=4,
                              quantity=2, fee_bps=0, slippage_bps=0,
                              max_drawdown=1, breakout="pause"), **kwargs)


def event(n, price=100, volume=100, **kwargs):
    return dict(id=str(n), time=f"2026-01-{n:02d}", price=price, volume=volume, **kwargs)


def start(**kwargs):
    grid = GridStrategy(config(**kwargs))
    grid.on_event(event(1))
    return grid


def account(grid):
    for s in grid.snapshots:
        assert s["cash"] >= -1e-8
        assert 0 <= s["reserved_cash"] <= s["cash"] + 1e-8
        assert 0 <= s["reserved_inventory"] <= s["inventory"] <= grid.config.max_inventory
        assert s["equity"] - grid.initial_equity == pytest.approx(s["realized_pnl"] + s["unrealized_pnl"])
    trades = [e for e in grid.events if e["kind"] == "TRADE"]
    cash = grid.config.initial_cash + sum((1 if t["side"] == "sell" else -1) * t["price"] * t["quantity"] - t["transaction_cost"] for t in trades)
    assert grid.cash == pytest.approx(cash)
    assert grid.inventory == grid.config.initial_inventory + sum((1 if t["side"] == "buy" else -1) * t["quantity"] for t in trades)


def test_arithmetic_manual_prices():
    assert start().prices == [80, 90, 100, 110, 120]


def test_geometric_manual_prices():
    c = config(mode="geometric", lower=64, upper=144, levels=2)
    assert GridStrategy(c).prices == pytest.approx([64, 96, 144])


def test_no_same_event_fill_and_shared_partial_volume():
    g = start()
    assert not [e for e in g.events if e["kind"] == "TRADE"]
    g.on_event(event(2, 85, 1))
    buy = next(o for o in g.orders.values() if o.side == "buy" and o.price == 90)
    assert buy.status == "PARTIALLY_FILLED" and buy.remaining == 1
    assert len([e for e in g.events if e["kind"] == "TRADE"]) == 1
    assert all(o.created == 2 for o in g.active_orders if o.price == 100)
    account(g)


def test_duplicate_event_and_conflicting_identity():
    g = start()
    e = event(2, 90, 1)
    g.on_event(e)
    before = json.dumps(g.events)
    g.on_event(e)
    assert json.dumps(g.events) == before
    with pytest.raises(ConfigurationError, match="identity"):
        g.on_event(dict(e, price=89))


def test_partial_cancel_releases_only_remaining_cash():
    g = start()
    g.on_event(event(2, 90, 1))
    order = next(o for o in g.active_orders if o.side == "buy" and o.price == 90)
    before = g.reserved_cash
    g.cancel(order.id)
    assert before - g.reserved_cash == pytest.approx(90)
    after = g.reserved_cash
    g.cancel(order.id)
    assert g.reserved_cash == after
    account(g)


def test_partial_sell_cancel_inventory_reservation():
    g = start()
    g.on_event(event(2, 110, 1))
    order = next(o for o in g.active_orders if o.side == "sell" and o.price == 110)
    before = g.reserved_inventory
    g.cancel(order.id)
    assert before - g.reserved_inventory == 1
    account(g)


def test_inventory_cannot_be_sold_twice():
    g = start(initial_inventory=1)
    assert g.reserved_inventory == 0
    assert any(o.reason == "INSUFFICIENT_INVENTORY" for o in g.orders.values())
    g.submit("sell", 110, 1)
    rejected = g.submit("sell", 120, 1)
    assert rejected.reason == "INSUFFICIENT_INVENTORY"


@pytest.mark.parametrize("kwargs,reason", [
    ({"initial_cash": 100}, "INSUFFICIENT_CASH"),
    ({"max_inventory": 4}, "MAX_INVENTORY"),
    ({"max_cash_committed": 450}, "MAX_CASH_COMMITTED"),
    ({"minimum_quantity": 3}, "MINIMUM_ORDER"),
    ({"fee_bps": 2000}, "FEE_EROSION"),
])
def test_submission_constraints(kwargs, reason):
    g = start(**kwargs)
    assert any(o.reason == reason for o in g.orders.values())
    account(g)


def test_expiry_releases_reservations():
    g = start(order_ttl=1)
    g.on_event(event(2))
    assert all(o.status == "EXPIRED" for o in g.orders.values())
    assert g.reserved_cash == g.reserved_inventory == 0


@pytest.mark.parametrize("price", [70, 130])
def test_breakout_exit_waits_for_future_liquidity(price):
    g = start(breakout="exit")
    g.on_event(event(2, price, 100))
    assert g.state == "EXITING" and g.inventory == 4
    g.on_event(event(3, price, 1))
    assert g.inventory == 3
    g.on_event(event(4, price, 0))
    assert g.inventory == 3
    g.on_event(event(5, price, 100))
    assert g.inventory == 0 and g.state == "EXITED"
    account(g)


def test_drawdown_pauses_and_cancels():
    g = start(max_drawdown=0.001)
    g.on_event(event(2, 90))
    assert g.state == "PAUSED" and not g.active_orders
    account(g)


def test_atr_uses_completed_history_and_new_orders_wait():
    g = GridStrategy(config(mode="atr", atr_window=2, atr_multiplier=2))
    g.on_event(event(1, completed_bar=True, high=102, low=98, close=100))
    assert not g.orders
    g.on_event(event(2, 100, completed_bar=True, high=103, low=97, close=100))
    assert g.prices == [80, 90, 100, 110, 120]
    assert g.rebuilds == 1
    assert not any(e["kind"] == "TRADE" for e in g.events)
    assert all(o.created == 2 for o in g.active_orders)
    g.on_event(event(3, 90, 1))
    assert g.inventory == 5


def test_ohlc_extrema_do_not_imply_fills():
    g = start()
    g.on_event(event(2, completed_bar=True, high=119, low=81, close=100))
    assert not any(e["kind"] == "TRADE" for e in g.events)
    assert any(e.get("status") == "UNKNOWN" for e in g.events)


def test_rebuild_cancels_old_orders_without_ghost_reservations():
    g = start(breakout="rebuild", min_rebuild_interval=1)
    old = list(g.active_orders)
    g.on_event(event(2, 125))
    assert all(o.status == "CANCELLED" for o in old)
    assert g.rebuilds == 1
    assert all(o.created == 2 for o in g.active_orders)
    account(g)


def test_fee_minimum_every_partial_and_slippage_reconcile():
    fees = FeeSchedule([FeeRule("1900-01-01", commission_bps=0,
                               minimum_commission_cny=1, slippage_bps=10)])
    g = GridStrategy(config(), fees)
    for e in (event(1), event(2, 90, 1), event(3, 90, 1), event(4, 100, 2)):
        g.on_event(e)
    assert g.fees == 4
    assert g.slippage == pytest.approx(.38)
    account(g)


def test_serialized_config_fee_rules_events_replay_full_audit():
    g = start(breakout="rebuild", min_rebuild_interval=1)
    for n, p in enumerate([90, 110, 125, 105, 95], 2):
        g.on_event(event(n, p, 1))
    replay = GridStrategy.replay(json.loads(json.dumps(g.replay_payload())))
    assert replay.events == g.events
    assert replay.snapshots == g.snapshots
    assert replay.orders == g.orders
    account(g)


def test_illegal_order_transition_fails():
    g = start()
    order = g.active_orders[0]
    g.cancel(order.id)
    with pytest.raises(ConfigurationError, match="illegal"):
        g._transition(order, "FILLED")


def test_cancel_input_replays_and_duplicate_cancel_is_idempotent():
    g = start()
    g.on_event(event(2, 90, 1))
    order = next(o for o in g.active_orders if o.price == 90 and o.side == "buy")
    cancel = event(3, 90, 0, action="cancel", order_id=order.id)
    g.on_event(cancel)
    g.on_event(cancel)
    replay = GridStrategy.replay(json.loads(json.dumps(g.replay_payload())))
    assert replay.events == g.events
    assert replay.orders == g.orders
    account(g)


def test_dated_fee_change_cannot_spend_reserved_cash():
    fees = FeeSchedule([
        FeeRule("1900-01-01", effective_to="2026-01-01", commission_bps=0,
                minimum_commission_cny=0, slippage_bps=0),
        FeeRule("2026-01-02", commission_bps=0, minimum_commission_cny=500,
                slippage_bps=0),
    ])
    g = GridStrategy(config(initial_cash=350), fees)
    g.on_event(event(1))
    g.on_event(event(2, 90, 1))
    assert any(o.reason == "INSUFFICIENT_CASH_AT_FILL" for o in g.orders.values())
    account(g)


def test_atr_rebuild_interval_and_stock_survive_cancellation():
    g = GridStrategy(config(mode="atr", atr_window=1, atr_multiplier=1,
                            rebuild_threshold=.1, min_rebuild_interval=3,
                            breakout="rebuild"))
    g.on_event(event(1, completed_bar=True, high=110, low=90, close=100))
    old = list(g.active_orders)
    g.on_event(event(2, 103, 0, completed_bar=True, high=113, low=93, close=103))
    g.on_event(event(3, 106, 0, completed_bar=True, high=116, low=96, close=106))
    assert g.rebuilds == 1
    g.on_event(event(4, 109, 0, completed_bar=True, high=119, low=99, close=109))
    assert g.rebuilds == 2 and g.inventory == 4
    assert all(o.status == "CANCELLED" for o in old)
    account(g)


def test_observed_gap_fills_only_preexisting_orders_at_limits():
    g = start()
    g.on_event(event(2, 81, 100))
    trades = [e for e in g.events if e["kind"] == "TRADE"]
    assert [(t["price"], t["quantity"]) for t in trades] == [(90, 2)]
    assert not any(t["side"] == "sell" for t in trades)
    account(g)


def test_budget_order_sizing_and_initial_commitment_validation():
    g = start(budget=180)
    assert next(o for o in g.orders.values() if o.price == 90).quantity == 2
    with pytest.raises(ConfigurationError, match="initial inventory"):
        config(max_cash_committed=399)


def test_peak_inventory_includes_within_event_round_trip():
    g = start()
    g.on_event(event(2, 110, 2))
    g.on_event(event(3, 90, 2))
    g.on_event(event(4, 100, 4))
    assert max(s["inventory"] for s in g.snapshots) == 4
    assert g.max_inventory_observed == 6
    account(g)


def test_risk_liquidation_not_blocked_by_turnover_or_order_expiry():
    g = start(max_turnover=.001, breakout="exit", order_ttl=1)
    g.on_event(event(2, 70, 0))
    g.on_event(event(3, 70, 1))
    g.on_event(event(4, 70, 100))
    assert g.state == "EXITED"
    assert g.notional > g.initial_equity * g.config.max_turnover
    assert g.realized_grid_pnl == 0
    assert g.realized_pnl == -120
    account(g)


def test_turnover_caps_next_fill_without_overshoot():
    g = start(max_turnover=.01)
    g.on_event(event(2, 90, 100))
    assert g.notional <= .01 * g.initial_equity
    g.on_event(event(3, 90, 100))
    assert g.state == "PAUSED"
    account(g)
