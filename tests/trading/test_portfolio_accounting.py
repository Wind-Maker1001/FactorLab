from datetime import datetime, timezone
from decimal import Decimal as D

import pytest

from factorlab.trading.contracts import (
    AccountSnapshot, AssetBalance, Market, OrderEvent, OrderIntent, OrderStatus,
    OrderType, Position, Side,
)
from factorlab.trading.portfolio_accounting import (
    OwnedPosition, ResearchAdjustment, build_portfolio_report,
)

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def intent(key, side, quantity="10"):
    return OrderIntent(Market.BINANCE_EQUITY, "TQQQ", side, OrderType.MARKET,
                       D(quantity), None, key)


def event(key, identity, quantity, price, fee="0"):
    return OrderEvent(key, "exchange-" + key, OrderStatus.PARTIALLY_FILLED,
                      D(quantity), D(price) if price else None, NOW, "broker", identity, D(fee))


def report(*, orders=(), events=(), owned=None, quantity="10", adjustments=(), targets=None):
    return build_portfolio_report(
        starting_inventory=owned or {}, intents=orders, events=events,
        reference_prices={order.client_order_id: D("100") for order in orders},
        account=AccountSnapshot(Market.BINANCE_EQUITY, "shared",
            (AssetBalance("USD", D("500"), D("50")),),
            (Position("TQQQ", D(quantity), D("999"), D("999"), "USD"),), (), NOW),
        prices={"TQQQ": D("120"), "OTHER": D("1")}, official_equity=D("2000"),
        target_weights=targets or {"TQQQ": D("0.7")}, adjustments=adjustments,
        economic_multipliers={"TQQQ": D("3")},
    )


def test_cumulative_deltas_average_basis_fees_slippage_and_turnover():
    buy, sell = intent("buy", Side.BUY), intent("sell", Side.SELL, "4")
    first = event("buy", "b1", "2", "101", "1")
    result = report(orders=(buy, sell), events=(first, first,
        event("buy", "b2", "5", "102", "2"),
        event("buy", "b3", "5", None, "2"),
        event("sell", "s1", "4", "110", "1")),
        owned={"TQQQ": OwnedPosition(D("5"), D("490"))}, quantity="8")
    row = result.positions[0]
    # Buy basis 490 + 202 + 306 + 2 = 1000 for ten owned shares.
    assert row.owned_quantity == D("6")
    assert row.external_quantity == D("2")
    assert row.owned_cost_basis == D("600")
    assert result.realized_pnl == D("39")
    assert result.unrealized_pnl == D("120")
    assert result.execution_fees == D("3")
    assert result.slippage_cost == D("-32")
    assert result.traded_notional == D("948")
    assert result.turnover == D("0.474")
    assert len(result.fills) == 3
    assert row.actual_weight == D("0.48")
    assert row.economic_exposure_weight == D("1.44")


def test_official_inventory_external_basis_and_signed_research_nav():
    adjustments = (ResearchAdjustment("dividend-ex", "recognized gross tax unknown", D("12.5")),
                   ResearchAdjustment("cost", "research cost", D("-2.5")))
    result = report(adjustments=adjustments)
    row = result.positions[0]
    assert row.actual_quantity == row.external_quantity == D("10")
    assert row.owned_quantity == result.unrealized_pnl == D("0")
    assert result.official_equity == D("2000")
    assert result.economic_nav == D("2010")
    assert result.official_cash == D("500")
    assert result.available_cash == D("450")
    assert result.cash_weight == D("0.25")
    assert row.actual_weight == D("0.6")
    assert result.adjustments == adjustments


def test_dividend_payment_moves_to_official_cash_not_second_adjustment():
    before = report(adjustments=(ResearchAdjustment("receivable", "dividend", D("10")),))
    after = build_portfolio_report(
        starting_inventory={}, intents=(), events=(), reference_prices={},
        account=AccountSnapshot(Market.BINANCE_EQUITY, "shared",
            (AssetBalance("USD", D("510"), D("50")),),
            (Position("TQQQ", D("10"), D("999"), D("999"), "USD"),), (), NOW),
        prices={"TQQQ": D("120")}, official_equity=D("2010"),
        target_weights={"TQQQ": D("0.7")},
    )
    assert before.economic_nav == after.economic_nav
    assert after.available_cash - before.available_cash == D("10")
    assert after.research_adjustment == D("0")


def test_targets_not_capped_or_normalized_and_target_only_position():
    result = report(targets={"TQQQ": D("0.85"), "OTHER": D("0.35")})
    rows = {row.instrument_id: row for row in result.positions}
    assert result.target_cash_weight == D("-0.20")
    assert rows["TQQQ"].target_weight == D("0.85")
    assert rows["OTHER"].actual_quantity == D("0")
    assert rows["OTHER"].target_weight == D("0.35")


def test_complete_sale_preserves_realized_pnl_and_clears_basis():
    result = report(orders=(intent("sell", Side.SELL, "3"),),
        events=(event("sell", "sold", "3", "110", "0.25"),),
        owned={"TQQQ": OwnedPosition(D("3"), D("300.01"))}, quantity="0")
    assert result.realized_pnl == D("29.74")
    assert result.positions[0].owned_cost_basis == result.unrealized_pnl == D("0")


@pytest.mark.parametrize("events, message", [
    ((event("buy", "one", "2", "100"), event("buy", "two", "1", "100")), "regressing"),
    ((event("buy", "one", "2", "100"), event("buy", "one", "3", "100")), "conflicting"),
    ((event("buy", "one", "11", "100"),), "overfill"),
    ((event("buy", "one", "2", None),), "requires price"),
    ((event("buy", "one", "2", "100"), event("buy", "two", "2", None, "1")), "fee change"),
])

def test_invalid_confirmations_rejected(events, message):
    with pytest.raises(ValueError, match=message):
        report(orders=(intent("buy", Side.BUY),), events=events)


def test_external_inventory_cannot_be_sold_as_owned():
    with pytest.raises(ValueError, match="explicitly owned"):
        report(orders=(intent("sell", Side.SELL),), events=(event("sell", "one", "1", "100"),))


def test_owned_holdings_must_reconcile_with_official_inventory():
    with pytest.raises(ValueError, match="exceeds official"):
        report(owned={"TQQQ": OwnedPosition(D("11"), D("100"))})


def test_duplicate_adjustments_rejected_and_float_basis_rejected():
    adjustment = ResearchAdjustment("same", "dividend", D("10"))
    with pytest.raises(ValueError, match="duplicate research"):
        report(adjustments=(adjustment, adjustment))
    with pytest.raises(ValueError, match="Decimal"):
        OwnedPosition(D("1"), 1.0)
