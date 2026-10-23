"""Configurable mainland A-share daily price-limit approximations.

The execution simulator only has daily bars.  It can therefore identify an
opening price at (or very close to) a statutory price limit, but it cannot know
whether an order-book queue would actually have filled.  These helpers keep
that approximation explicit and date effective.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
import math
from typing import Iterable, Optional, Tuple

import pandas as pd

from qbot.research.data.schema import normalize_symbol
from qbot.research.errors import ConfigurationError


MAIN_BOARD = "main"
CHINEXT_BOARD = "chinext"
STAR_BOARD = "star"
SUPPORTED_BOARDS = (MAIN_BOARD, CHINEXT_BOARD, STAR_BOARD)


def _as_date(value: object, field: str) -> date:
    try:
        timestamp = pd.Timestamp(value)
    except (TypeError, ValueError) as exc:
        raise ConfigurationError("invalid %s: %r" % (field, value)) from exc
    if pd.isna(timestamp):
        raise ConfigurationError("%s must not be null" % field)
    if timestamp.tzinfo is not None:
        timestamp = timestamp.tz_localize(None)
    return timestamp.date()


def _finite_positive(value: object, field: str) -> float:
    if isinstance(value, bool):
        raise ConfigurationError("%s must be finite and positive" % field)
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigurationError("%s must be finite and positive" % field) from exc
    if not math.isfinite(result) or result <= 0:
        raise ConfigurationError("%s must be finite and positive" % field)
    return result


def classify_board(symbol: object) -> str:
    """Classify a canonical SH/SZ equity into the supported trading boards."""

    canonical = normalize_symbol(symbol)
    code, exchange = canonical.split(".", 1)
    if exchange == "SZ" and code.startswith(("300", "301")):
        return CHINEXT_BOARD
    if exchange == "SH" and code.startswith(("688", "689")):
        return STAR_BOARD
    return MAIN_BOARD


@dataclass(frozen=True)
class PriceLimitRule:
    """Inclusive, date-effective price-limit rule for one trading board.

    Percentages are expressed as decimal fractions: ``0.10`` means 10%.
    ``st_limit_pct`` remains independent because main-board ST securities use
    a different band, while post-reform ChiNext and STAR securities do not.
    """

    board: str
    effective_from: date
    effective_to: Optional[date] = None
    normal_limit_pct: float = 0.10
    st_limit_pct: float = 0.05
    tick_size: float = 0.01

    def __post_init__(self) -> None:
        board = str(self.board).strip().lower()
        if board not in SUPPORTED_BOARDS:
            raise ConfigurationError("unsupported price-limit board: %s" % board)
        start = _as_date(self.effective_from, "effective_from")
        end = (
            None
            if self.effective_to is None
            else _as_date(self.effective_to, "effective_to")
        )
        if end is not None and end < start:
            raise ConfigurationError(
                "price-limit effective_to must be on or after effective_from"
            )
        normal = _finite_positive(self.normal_limit_pct, "normal_limit_pct")
        st = _finite_positive(self.st_limit_pct, "st_limit_pct")
        tick = _finite_positive(self.tick_size, "tick_size")
        if normal >= 1 or st >= 1:
            raise ConfigurationError("price-limit percentages must be below 1")
        object.__setattr__(self, "board", board)
        object.__setattr__(self, "effective_from", start)
        object.__setattr__(self, "effective_to", end)
        object.__setattr__(self, "normal_limit_pct", normal)
        object.__setattr__(self, "st_limit_pct", st)
        object.__setattr__(self, "tick_size", tick)

    def contains(self, value: object) -> bool:
        day = _as_date(value, "trade_date")
        return self.effective_from <= day and (
            self.effective_to is None or day <= self.effective_to
        )

    def percentage(self, is_st: bool) -> float:
        return self.st_limit_pct if bool(is_st) else self.normal_limit_pct


@dataclass(frozen=True)
class PriceLimitBounds:
    board: str
    percentage: float
    previous_close: float
    lower: float
    upper: float


def _round_to_tick(value: float, tick_size: float) -> float:
    value_decimal = Decimal(str(value))
    tick_decimal = Decimal(str(tick_size))
    ticks = (value_decimal / tick_decimal).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return float(ticks * tick_decimal)


class PriceLimitSchedule:
    """Resolve historical price-limit bands without hidden global state."""

    def __init__(self, rules: Iterable[PriceLimitRule]):
        ordered = tuple(
            sorted(rules, key=lambda rule: (rule.board, rule.effective_from))
        )
        if not ordered:
            raise ConfigurationError("PriceLimitSchedule requires at least one rule")
        for board in SUPPORTED_BOARDS:
            board_rules = [rule for rule in ordered if rule.board == board]
            for previous, current in zip(board_rules, board_rules[1:]):
                if (
                    previous.effective_to is None
                    or previous.effective_to >= current.effective_from
                ):
                    raise ConfigurationError(
                        "price-limit rules for %s must not overlap" % board
                    )
        self._rules: Tuple[PriceLimitRule, ...] = ordered

    @property
    def rules(self) -> Tuple[PriceLimitRule, ...]:
        return self._rules

    @classmethod
    def from_config(cls, periods: Iterable[object]) -> "PriceLimitSchedule":
        """Build a schedule from typed ``PriceLimitPeriod`` objects."""

        required = (
            "board",
            "start",
            "end",
            "normal_limit_pct",
            "st_limit_pct",
            "tick_size",
        )
        rules = []
        for number, period in enumerate(periods):
            missing = [name for name in required if not hasattr(period, name)]
            if missing:
                raise ConfigurationError(
                    "price-limit config item %d missing fields: %s"
                    % (number, ", ".join(missing))
                )
            rules.append(
                PriceLimitRule(
                    board=period.board,
                    effective_from=period.start,
                    effective_to=period.end,
                    normal_limit_pct=period.normal_limit_pct,
                    st_limit_pct=period.st_limit_pct,
                    tick_size=period.tick_size,
                )
            )
        return cls(rules)

    @classmethod
    def a_share_default(cls) -> "PriceLimitSchedule":
        """Return historical bands needed by the FactorLab research period.

        The ChiNext registration reform widened its band from 10% (5% for ST)
        to 20% on 2020-08-24.  STAR has used 20% since its first trading day.
        Main-board securities retain 10%, or 5% when designated ST.
        """

        return cls(
            (
                PriceLimitRule(
                    board=MAIN_BOARD,
                    effective_from=date(1990, 1, 1),
                    normal_limit_pct=0.10,
                    st_limit_pct=0.05,
                ),
                PriceLimitRule(
                    board=CHINEXT_BOARD,
                    effective_from=date(2009, 10, 30),
                    effective_to=date(2020, 8, 23),
                    normal_limit_pct=0.10,
                    st_limit_pct=0.05,
                ),
                PriceLimitRule(
                    board=CHINEXT_BOARD,
                    effective_from=date(2020, 8, 24),
                    normal_limit_pct=0.20,
                    st_limit_pct=0.20,
                ),
                PriceLimitRule(
                    board=STAR_BOARD,
                    effective_from=date(2019, 7, 22),
                    normal_limit_pct=0.20,
                    st_limit_pct=0.20,
                ),
            )
        )

    def rule_for(self, symbol: object, trade_date: object) -> PriceLimitRule:
        board = classify_board(symbol)
        matches = [
            rule
            for rule in self._rules
            if rule.board == board and rule.contains(trade_date)
        ]
        if len(matches) != 1:
            raise ConfigurationError(
                "price-limit schedule must cover %s on %s exactly once"
                % (normalize_symbol(symbol), _as_date(trade_date, "trade_date"))
            )
        return matches[0]

    def bounds(
        self,
        symbol: object,
        trade_date: object,
        previous_close: object,
        is_st: bool = False,
    ) -> PriceLimitBounds:
        close = _finite_positive(previous_close, "previous_close")
        rule = self.rule_for(symbol, trade_date)
        percentage = rule.percentage(is_st)
        return PriceLimitBounds(
            board=rule.board,
            percentage=percentage,
            previous_close=close,
            lower=_round_to_tick(close * (1.0 - percentage), rule.tick_size),
            upper=_round_to_tick(close * (1.0 + percentage), rule.tick_size),
        )

    def blocks(
        self,
        symbol: object,
        trade_date: object,
        side: str,
        open_price: object,
        previous_close: object,
        is_st: bool = False,
        tolerance_bps: float = 5.0,
    ) -> Tuple[bool, PriceLimitBounds]:
        """Return whether a daily-bar order is conservatively unfillable."""

        direction = str(side).strip().lower()
        if direction not in {"buy", "sell"}:
            raise ConfigurationError("trade side must be buy or sell")
        price = _finite_positive(open_price, "open_price")
        if isinstance(tolerance_bps, bool):
            raise ConfigurationError("tolerance_bps must be finite and non-negative")
        try:
            tolerance = float(tolerance_bps)
        except (TypeError, ValueError) as exc:
            raise ConfigurationError(
                "tolerance_bps must be finite and non-negative"
            ) from exc
        if not math.isfinite(tolerance) or tolerance < 0 or tolerance >= 10_000:
            raise ConfigurationError(
                "tolerance_bps must be finite, non-negative and below 10000"
            )
        bounds = self.bounds(symbol, trade_date, previous_close, is_st=is_st)
        fraction = tolerance / 10_000.0
        if direction == "buy":
            return price >= bounds.upper * (1.0 - fraction), bounds
        return price <= bounds.lower * (1.0 + fraction), bounds


__all__ = [
    "CHINEXT_BOARD",
    "MAIN_BOARD",
    "STAR_BOARD",
    "PriceLimitBounds",
    "PriceLimitRule",
    "PriceLimitSchedule",
    "classify_board",
]
