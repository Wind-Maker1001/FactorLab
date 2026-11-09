"""Deterministic borrowed-stock simulation, not an A-share execution contract.

Borrow availability is a caller-declared software assumption. Short proceeds are
restricted until cover; additional margin is reserved against current liability.
Opening orders submit the short leg first. An incomplete leg stops remaining
submissions and leaves actual fills outstanding for next-session liquidation.
"""
from __future__ import annotations

from dataclasses import dataclass
import math

import pandas as pd

from factorlab.backtest.execution import PositionBook
from factorlab.backtest.fees import FeeSchedule


@dataclass(frozen=True)
class BorrowTerms:
    max_shares: int
    annual_rate: float

    def __post_init__(self):
        if isinstance(self.max_shares, bool) or not isinstance(self.max_shares, int) or self.max_shares < 0:
            raise ValueError("max_shares must be a nonnegative integer")
        if not math.isfinite(self.annual_rate) or self.annual_rate < 0:
            raise ValueError("annual_rate must be finite and nonnegative")


class PairAccount:
    """Single-pair account with positive inventory books on both sides.

    ``cash`` includes restricted short-sale proceeds. Available cash excludes
    those proceeds and ``margin_rate * current_short_liability``. No interest,
    reuse of collateral, fractional shares or unsecured financing is assumed.
    """

    def __init__(self, initial_cash: float, fees: FeeSchedule, borrow: dict[str, BorrowTerms],
                 *, margin_rate: float = .5, max_gross_leverage: float = 1.0):
        if not math.isfinite(initial_cash) or initial_cash <= 0:
            raise ValueError("initial_cash must be positive")
        if not math.isfinite(margin_rate) or margin_rate < 0:
            raise ValueError("margin_rate must be nonnegative")
        if not math.isfinite(max_gross_leverage) or max_gross_leverage <= 0:
            raise ValueError("max_gross_leverage must be positive")
        self.initial_cash = self.cash = float(initial_cash)
        self.fees, self.borrow = fees, dict(borrow)
        self.margin_rate, self.max_gross_leverage = margin_rate, max_gross_leverage
        self.longs, self.shorts = PositionBook(), PositionBook()
        self.short_proceeds: dict[str, float] = {}
        self.long_basis: dict[str, float] = {}
        self.short_basis: dict[str, float] = {}
        self.realized_long = self.realized_short = 0.0
        self.transaction_cost = self.borrow_cost = 0.0
        self.trades: list[dict] = []
        self.failures: list[dict] = []

    @property
    def flat(self):
        return not self.longs.symbols() and not self.shorts.symbols()

    def values(self, prices):
        long_value = sum(self.longs.total_shares(s) * prices[s] for s in self.longs.symbols())
        short_value = sum(self.shorts.total_shares(s) * prices[s] for s in self.shorts.symbols())
        restricted = sum(self.short_proceeds.values())
        equity = self.cash + long_value - short_value
        return dict(cash=self.cash, restricted_short_proceeds=restricted,
                    margin=self.margin_rate * short_value,
                    available_cash=self.cash - restricted - self.margin_rate * short_value,
                    long_value=long_value, short_liability=short_value, equity=equity,
                    net_exposure=long_value - short_value, gross_exposure=long_value + short_value,
                    leverage=(long_value + short_value) / equity if equity > 0 else float("nan"),
                    long_pnl=self.realized_long + long_value - sum(self.long_basis.values()),
                    short_pnl=self.realized_short + sum(self.short_basis.values()) - short_value,
                    cumulative_transaction_cost=self.transaction_cost,
                    cumulative_borrow_cost=self.borrow_cost)

    def accrue_borrow(self, prices, days: int):
        cost = sum(self.shorts.total_shares(s) * prices[s] * self.borrow[s].annual_rate * days / 365
                   for s in self.shorts.symbols())
        self.cash -= cost
        self.borrow_cost += cost
        return cost

    def _failure(self, date, symbol, action, requested, filled, reason):
        self.failures.append(dict(date=pd.Timestamp(date), symbol=symbol, action=action,
                                  requested=requested, filled=filled, reason=reason))

    def _fill(self, date, symbol, action, quantity, prices, fraction, decision_date, reason):
        if not math.isfinite(fraction) or not 0 <= fraction <= 1:
            raise ValueError("fill fraction must be in [0, 1]")
        actual = int(quantity * fraction)
        price = float(prices[symbol])
        if not math.isfinite(price) or price <= 0:
            raise ValueError("execution price must be finite and positive")
        if actual == 0:
            self._failure(date, symbol, action, quantity, 0, "NO_FILL")
            return 0
        side = "sell" if action in {"short", "sell"} else "buy"
        cost = self.fees.calculate(date, side, actual * price).total
        state = self.values(prices)
        if action == "short":
            terms = self.borrow.get(symbol)
            if terms is None or self.shorts.total_shares(symbol) + actual > terms.max_shares:
                self._failure(date, symbol, action, quantity, 0, "BORROW_UNAVAILABLE")
                return 0
            affordable = state["available_cash"] >= actual * price * self.margin_rate + cost
        elif action == "buy":
            affordable = state["available_cash"] >= actual * price + cost
        elif action == "cover":
            total = self.shorts.total_shares(symbol)
            remaining_restricted = state["restricted_short_proceeds"] - self.short_proceeds[symbol] * actual / total
            affordable = self.cash - actual * price - cost >= remaining_restricted
        else:
            affordable = self.longs.total_shares(symbol) >= actual
        if not affordable:
            self._failure(date, symbol, action, quantity, 0, "INSUFFICIENT_FUNDS_OR_INVENTORY")
            return 0
        if action in {"short", "buy"} and (
            state["gross_exposure"] + actual * price > self.max_gross_leverage * (state["equity"] - cost)
        ):
            self._failure(date, symbol, action, quantity, 0, "GROSS_LIMIT")
            return 0
        notional = actual * price
        self.cash += notional * (1 if side == "sell" else -1) - cost
        self.transaction_cost += cost
        if action == "short":
            self.shorts.buy(symbol, date, actual)
            self.short_proceeds[symbol] = self.short_proceeds.get(symbol, 0) + notional
            self.short_basis[symbol] = self.short_basis.get(symbol, 0) + notional
        elif action == "buy":
            self.longs.buy(symbol, date, actual)
            self.long_basis[symbol] = self.long_basis.get(symbol, 0) + notional
        elif action == "sell":
            basis = self.long_basis[symbol] * actual / self.longs.total_shares(symbol)
            self.realized_long += notional - basis
            self.long_basis[symbol] -= basis
            self.longs.sell(symbol, date, actual, t_plus_one=False)
        else:
            ratio = actual / self.shorts.total_shares(symbol)
            basis = self.short_basis[symbol] * ratio
            self.realized_short += basis - notional
            self.short_basis[symbol] -= basis
            self.short_proceeds[symbol] *= 1 - ratio
            self.shorts.sell(symbol, date, actual, t_plus_one=False)
        self.trades.append(dict(execution_date=pd.Timestamp(date), decision_date=pd.Timestamp(decision_date),
                                symbol=symbol, action=action, shares=actual, price=price,
                                trade_notional=notional, transaction_cost=cost, reason=reason))
        if actual != quantity:
            self._failure(date, symbol, action, quantity, actual, "PARTIAL_FILL")
        return actual

    def open_pair(self, date, quantities, prices, *, decision_date, fills=None):
        """Submit short first; retain any actual incomplete fills without rollback."""
        if not self.flat:
            raise ValueError("opening requires a flat account")
        if len(quantities) != 2 or sum(q < 0 for q in quantities.values()) != 1 or sum(q > 0 for q in quantities.values()) != 1:
            raise ValueError("pair requires one positive and one negative integer quantity")
        fills = fills or {}
        before = len(self.failures)
        for symbol, signed in sorted(quantities.items(), key=lambda item: item[1]):
            if isinstance(signed, bool) or not isinstance(signed, int):
                raise ValueError("pair quantities must be integers")
            actual = self._fill(date, symbol, "short" if signed < 0 else "buy", abs(signed), prices,
                                fills.get(symbol, 1.0), decision_date, "entry")
            if actual != abs(signed):
                break
        return len(self.failures) == before

    def close_pair(self, date, prices, *, decision_date, reason, fills=None):
        """Sell long inventory before covering, preserving failed liquidation legs."""
        fills = fills or {}
        for book, action in ((self.longs, "sell"), (self.shorts, "cover")):
            for symbol in book.symbols():
                self._fill(date, symbol, action, book.total_shares(symbol), prices,
                           fills.get(symbol, 1.0), decision_date, reason)
        return self.flat

    def positions(self, date, prices):
        return [dict(date=pd.Timestamp(date), symbol=s, side=side,
                     shares=book.total_shares(s), signed_shares=sign * book.total_shares(s),
                     market_value=sign * book.total_shares(s) * prices[s])
                for book, side, sign in ((self.longs, "long", 1), (self.shorts, "short", -1))
                for s in book.symbols()]
