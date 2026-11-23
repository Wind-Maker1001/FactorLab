"""Exercise F11 limit prices and authoritative fills under the offline cost model."""
from dataclasses import replace
from decimal import Decimal as D

import pytest

from factorlab.experiments.grid_f11_f08_offline import (
    DEFAULT_DEMO_WALLET,
    DemoGuardrails,
    execution_profile,
    run_offline_lifecycle,
)
from factorlab.experiments.grid_monte_carlo import close_bars
from factorlab.trading.contracts import OrderStatus, Side
from factorlab.trading.storage import PaperStore


def _replay(store_path, prices, assumptions, guardrails):
    bars = close_bars(prices, "2026-08-13T00:00:00+00:00")
    result = run_offline_lifecycle(
        bars, assumptions=assumptions, guardrails=guardrails,
        store_path=store_path, record_hourly_series=True,
    )
    store = PaperStore(store_path, continuous_capacity=True)
    try:
        orders = store.load().orders
        fills = [
            (event, orders[event.client_order_id].snapshot.intent)
            for _, event in store.order_events()
            if event.fill_price is not None
        ]
    finally:
        store.close()
    assert result["reconciled_zero_residual"]
    return result, fills


@pytest.mark.parametrize(("slippage", "expected_limit"), [
    (D("0"), D("131.40")),
    (D("0.0002"), D("131.37")),
])
def test_immediate_f11_exit_books_adverse_price_once(tmp_path, slippage, expected_limit):
    profile = replace(execution_profile("IDEAL_LIMIT_FILL"), slippage=slippage)
    result, fills = _replay(
        tmp_path / "marketable.sqlite3", [132.89, 131.40], profile,
        DemoGuardrails(enable_f08=False),
    )
    (buy, buy_intent), (sell, sell_intent) = fills
    assert buy_intent.side is Side.BUY
    assert sell_intent.side is Side.SELL and sell_intent.reduce_only
    assert sell.event_time == buy.event_time
    assert sell_intent.limit_price == expected_limit
    quote = D("131.40")
    quantity = D("0.04")
    assert buy.fill_price == quote * (1 + slippage)
    assert sell.fill_price == quote * (1 - slippage)
    assert sell.fill_price >= sell_intent.limit_price
    gross = (sell.fill_price - buy.fill_price) * quantity
    fees = (buy.fill_price + sell.fill_price) * quantity * profile.fee_rate
    assert result["slippage_usdt"] == pytest.approx(float(-gross))
    assert result["realized_trade_gross_usdt"] == pytest.approx(float(gross))
    assert D(result["final_wallet_usdt"]) == pytest.approx(
        DEFAULT_DEMO_WALLET + gross - fees, abs=D("1e-8"),
    )


@pytest.mark.parametrize("rebound", [False, True])
def test_resting_f11_exit_waits_for_adverse_bid_to_recover(tmp_path, rebound):
    profile = replace(
        execution_profile("IDEAL_LIMIT_FILL"), exit_marketable_at_submit=False,
        exit_fill_delay_bars=1,
    )
    prices = [132.89, 131.40, 130.00, 131.40]
    if rebound:
        prices.append(131.43)
    result, fills = _replay(
        tmp_path / "resting.sqlite3", prices, profile,
        DemoGuardrails(enable_f08=False, exit_reprice_seconds=36000),
    )
    assert result["exit_orders_submitted"] == 1
    assert [bar["inventory"] for bar in result["hourly_series"][:4]] == [0, 0.04, 0.04, 0.04]
    if rebound:
        (_, _), (sell, intent) = fills
        assert sell.fill_price == intent.limit_price == D("131.40")
        assert result["exit_orders_filled"] == 1
        assert result["residual_inventory"] == 0
    else:
        assert len(fills) == 1
        assert result["exit_orders_filled"] == 0
        assert result["residual_inventory"] == 0.04


def test_partial_f11_exit_preserves_limit_after_bid_drop(tmp_path):
    profile = replace(execution_profile("IDEAL_LIMIT_FILL"), capacity_per_bar=D("0.06"))
    result, fills = _replay(
        tmp_path / "partial.sqlite3", [132.89, 131.40, 131.37, 131.41], profile,
        DemoGuardrails(enable_f08=False),
    )
    (buy, _), (partial, intent), (completion, resting_intent) = fills
    assert intent.client_order_id == resting_intent.client_order_id
    assert intent.limit_price == D("131.37")
    assert partial.status is OrderStatus.PARTIALLY_FILLED
    assert partial.cumulative_filled_quantity == D("0.02")
    assert partial.fill_price == D("131.37372")
    assert completion.status is OrderStatus.FILLED
    assert completion.cumulative_filled_quantity == D("0.04")
    assert completion.fill_price == intent.limit_price
    assert [bar["inventory"] for bar in result["hourly_series"]] == [0, 0.02, 0.02, 0]
    assert result["dust_protections_triggered"] == 1
    expected_gross = (partial.fill_price + completion.fill_price) * D("0.02") - buy.fill_price * D("0.04")
    assert result["realized_trade_gross_usdt"] == pytest.approx(float(expected_gross))
