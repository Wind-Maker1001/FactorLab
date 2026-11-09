from datetime import date

import pytest

from factorlab.backtest.fees import FeeRule, FeeSchedule
from factorlab.backtest.pairs import BorrowTerms, PairAccount


def account(cash=1000., *, commission=0., margin=.5, borrow=True, leverage=2.):
    fees = FeeSchedule([FeeRule(date(2000, 1, 1), commission_bps=commission,
                               minimum_commission_cny=0, slippage_bps=0)])
    return PairAccount(cash, fees, {"S": BorrowTerms(100, .365)} if borrow else {},
                       margin_rate=margin, max_gross_leverage=leverage)


def enter(book, *, fills=None, qty=None):
    return book.open_pair("2024-01-02", qty or {"L": 10, "S": -10}, {"L": 20., "S": 10.},
                          decision_date="2024-01-01", fills=fills)


def test_equity_cash_margin_and_pnl_hand_calculation():
    book = account()
    assert enter(book)
    state = book.values({"L": 22., "S": 9.})
    assert state["cash"] == 900
    assert state["restricted_short_proceeds"] == 100
    assert state["margin"] == 45
    assert state["available_cash"] == 755
    assert state["equity"] == 1030
    assert state["net_exposure"] == 130
    assert state["gross_exposure"] == 310
    assert state["long_pnl"] == 20
    assert state["short_pnl"] == 10
    assert book.accrue_borrow({"L": 22., "S": 9.}, 3) == pytest.approx(.27)
    assert book.close_pair("2024-01-05", {"L": 22., "S": 9.}, decision_date="2024-01-04", reason="exit")
    state = book.values({"L": 22., "S": 9.})
    assert state["equity"] == pytest.approx(1029.73)
    assert state["cash"] == state["equity"]
    assert state["gross_exposure"] == 0
    assert state["restricted_short_proceeds"] == 0


def test_fees_and_borrow_reconcile_to_equity():
    book = account(commission=10)
    assert enter(book)
    book.accrue_borrow({"L": 20., "S": 10.}, 1)
    book.close_pair("2024-01-03", {"L": 21., "S": 11.}, decision_date="2024-01-02", reason="exit")
    state = book.values({"L": 21., "S": 11.})
    assert book.transaction_cost == pytest.approx(.3 + .32)
    assert book.borrow_cost == pytest.approx(.1)
    assert state["equity"] == pytest.approx(1000 - .62 - .1)
    assert state["equity"] == pytest.approx(1000 + state["long_pnl"] + state["short_pnl"]
                                             - book.transaction_cost - book.borrow_cost)


def test_short_proceeds_do_not_create_buying_power():
    book = account(cash=100., margin=.5, leverage=10.)
    assert not enter(book, qty={"L": 6, "S": -10})
    assert book.longs.total_shares("L") == 0
    assert book.shorts.total_shares("S") == 10
    assert book.values({"L": 20., "S": 10.})["available_cash"] == 50
    assert book.failures[-1]["reason"] == "INSUFFICIENT_FUNDS_OR_INVENTORY"


def test_borrow_is_required_and_limited_before_long_submission():
    book = account(borrow=False)
    assert not enter(book)
    assert book.flat
    assert len(book.failures) == 1
    assert book.failures[0]["reason"] == "BORROW_UNAVAILABLE"
    book = account()
    assert not enter(book, qty={"L": 10, "S": -101})
    assert book.flat


def test_partial_short_stops_long_and_is_not_rewritten_atomic():
    book = account()
    assert not enter(book, fills={"S": .4})
    assert book.shorts.total_shares("S") == 4
    assert book.longs.total_shares("L") == 0
    assert len(book.trades) == 1
    assert book.trades[0]["shares"] == 4
    assert book.failures[0]["filled"] == 4
    book.close_pair("2024-01-03", {"L": 20., "S": 11.}, decision_date="2024-01-02", reason="leg_failure")
    assert book.flat
    assert book.cash == 996


def test_second_leg_failure_preserves_short_and_partial_close_exposure():
    book = account()
    assert not enter(book, fills={"L": 0})
    assert book.shorts.total_shares("S") == 10
    assert not book.close_pair("2024-01-03", {"L": 20., "S": 10.}, decision_date="2024-01-02",
                               reason="leg_failure", fills={"S": .5})
    assert book.shorts.total_shares("S") == 5
    assert book.values({"S": 10.})["restricted_short_proceeds"] == 50
    assert book.close_pair("2024-01-04", {"L": 20., "S": 10.}, decision_date="2024-01-03", reason="leg_failure")
    assert book.cash == 1000


def test_full_entry_and_exit_submit_order():
    book = account()
    assert enter(book)
    assert [trade["action"] for trade in book.trades] == ["short", "buy"]
    assert book.close_pair("2024-01-03", {"L": 20., "S": 10.}, decision_date="2024-01-02", reason="exit")
    assert [trade["action"] for trade in book.trades] == ["short", "buy", "sell", "cover"]


def test_gross_limit_and_unfunded_cover_are_reported_without_phantom_cash():
    book = account(leverage=.1)
    assert not enter(book)
    assert book.shorts.total_shares("S") == 10
    assert book.failures[-1]["reason"] == "GROSS_LIMIT"
    assert not book.close_pair("2024-01-03", {"L": 20., "S": 1000.}, decision_date="2024-01-02", reason="max_loss")
    assert book.shorts.total_shares("S") == 10
    assert book.cash == 1100
    assert book.failures[-1]["reason"] == "INSUFFICIENT_FUNDS_OR_INVENTORY"
