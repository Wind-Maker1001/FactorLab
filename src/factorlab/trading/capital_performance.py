"""Performance of a strategy with externally timed capital movements."""
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from math import exp, expm1, fsum, log
from sys import float_info

from .capital_apportionment import exact_sum

ZERO = Decimal(0)
ONE = Decimal(1)
SECONDS_PER_YEAR = 365 * 24 * 60 * 60


@dataclass(frozen=True)
class CapitalNavPoint:
    observed_at: datetime | None
    equity: Decimal
    cash_flow: Decimal
    index: Decimal | None


@dataclass(frozen=True)
class CapitalPerformance:
    initial_capital: Decimal
    capital_in: Decimal
    capital_out: Decimal
    absolute_pnl: Decimal
    twr: Decimal | None
    xirr: float | None
    nav: tuple[CapitalNavPoint, ...]


def _link_return(index, previous_equity, next_equity):
    if index is None or previous_equity < ZERO or next_equity < ZERO:
        return None
    if previous_equity == ZERO:
        return index if next_equity == ZERO else None
    return index * (next_equity / previous_equity)


def _unique_root(amounts):
    sign_changes = sum((left < ZERO) != (right < ZERO)
                       for left, right in zip(amounts, amounts[1:]))
    if sign_changes == 1:
        return True
    # With nonpositive running investor cash, positive time-value discounts
    # have one crossing when total PnL is nonnegative. For a loss, the same
    # argument applies to the reversed, sign-inverted cash flows.
    profit = exact_sum(amounts)
    ordered = amounts[:-1] if profit >= ZERO else reversed(amounts[1:])
    balance = ZERO
    for amount in ordered:
        balance = exact_sum((balance, amount if profit >= ZERO else amount.copy_negate()))
        if balance > ZERO:
            return False
    return True


def _xirr(nav):
    start, finish = nav[0], nav[-1]
    if start.observed_at is None or start.observed_at >= finish.observed_at or finish.equity < ZERO:
        return None
    # The investor funds the initial equity and subsequent contributions, and
    # receives withdrawals plus the marked liquidation value at the end.
    cash = {}
    for point in nav[:-1]:
        cash[point.observed_at] = exact_sum((cash.get(point.observed_at, ZERO), point.cash_flow.copy_negate()))
    cash[finish.observed_at] = exact_sum((cash.get(finish.observed_at, ZERO), finish.equity))
    rows = [(at, amount) for at, amount in sorted(cash.items()) if amount]
    if len(rows) < 2 or rows[0][1] >= ZERO or rows[-1][1] <= ZERO:
        return None
    amounts = [amount for _, amount in rows]
    if not _unique_root(amounts):
        return None
    if exact_sum(amounts) == ZERO:
        return 0.0
    scale = max(amount.copy_abs() for amount in amounts)
    terms = [((at - rows[0][0]).total_seconds() / SECONDS_PER_YEAR, float(amount / scale))
             for at, amount in rows]

    def npv(rate):
        shift = max(0, -rate * terms[-1][0])
        return fsum(amount * exp(-rate * years - shift) for years, amount in terms)

    # Solve in log(1 + r) to cover losses near -100% and large short-horizon
    # annualized returns without overflow in the discounted cash flows.
    low, high = log(float_info.epsilon / 2), log(float_info.max)
    if npv(low) < 0 or npv(high) > 0:
        return None
    for _ in range(64):
        middle = (low + high) / 2
        value = npv(middle)
        if value == 0:
            return expm1(middle)
        if value > 0:
            low = middle
        else:
            high = middle
    return expm1((low + high) / 2)


def build_capital_performance(*, initial_capital, started_at, observations, current):
    """Chain returns through pre-flow marks and solve actual/365 investor XIRR.

    Positive cash flow contributes capital. Negative cash flow withdraws it.
    Observations are in posting order, with a valuation immediately before
    each flow. Zero balances between full redemption and recapitalization
    preserve the previous index. Equity losses from a zero base have no return.
    """
    index, previous_equity = ONE, initial_capital
    nav = [CapitalNavPoint(started_at, initial_capital, initial_capital, index)]
    capital_in, capital_out = ZERO, ZERO
    for observed_at, equity_before, cash_flow in observations:
        index = _link_return(index, previous_equity, equity_before)
        previous_equity = exact_sum((equity_before, cash_flow))
        nav.append(CapitalNavPoint(observed_at, previous_equity, cash_flow, index))
        if cash_flow > ZERO:
            capital_in = exact_sum((capital_in, cash_flow))
        else:
            capital_out = exact_sum((capital_out, cash_flow.copy_negate()))
    observed_at, equity = current
    index = _link_return(index, previous_equity, equity)
    nav.append(CapitalNavPoint(observed_at, equity, ZERO, index))
    pnl = exact_sum((equity, initial_capital.copy_negate(), capital_in.copy_negate(), capital_out))
    return CapitalPerformance(initial_capital, capital_in, capital_out, pnl,
                              None if index is None else index - ONE, _xirr(nav), tuple(nav))
