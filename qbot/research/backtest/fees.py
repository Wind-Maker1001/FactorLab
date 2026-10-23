"""Date-effective transaction fee schedules for mainland A-share research."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import math
from typing import Iterable, Optional, Tuple

import pandas as pd

from qbot.research.errors import ConfigurationError


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


@dataclass(frozen=True)
class FeeRule:
    """Inclusive effective-date rule; all percentage rates are in basis points."""

    effective_from: date
    effective_to: Optional[date] = None
    buy_fee_bps: float = 0.0
    sell_fee_bps: float = 0.0
    stamp_duty_bps: float = 0.0
    transfer_fee_bps: float = 0.0
    commission_bps: float = 3.0
    minimum_commission_cny: float = 5.0
    slippage_bps: float = 5.0

    def __post_init__(self) -> None:
        start = _as_date(self.effective_from, "effective_from")
        end = None if self.effective_to is None else _as_date(self.effective_to, "effective_to")
        object.__setattr__(self, "effective_from", start)
        object.__setattr__(self, "effective_to", end)
        if end is not None and end < start:
            raise ConfigurationError("fee effective_to must be on or after effective_from")
        for field in (
            "buy_fee_bps",
            "sell_fee_bps",
            "stamp_duty_bps",
            "transfer_fee_bps",
            "commission_bps",
            "minimum_commission_cny",
            "slippage_bps",
        ):
            value = getattr(self, field)
            if isinstance(value, bool):
                raise ConfigurationError("%s must be a finite number" % field)
            try:
                numeric = float(value)
            except (TypeError, ValueError) as exc:
                raise ConfigurationError("%s must be a finite number" % field) from exc
            if not math.isfinite(numeric) or numeric < 0:
                raise ConfigurationError(
                    "%s must be finite and non-negative" % field
                )
            object.__setattr__(self, field, numeric)

    def contains(self, value: object) -> bool:
        day = _as_date(value, "trade_date")
        return self.effective_from <= day and (
            self.effective_to is None or day <= self.effective_to
        )


@dataclass(frozen=True)
class FeeBreakdown:
    commission: float = 0.0
    stamp_duty: float = 0.0
    transfer_fee: float = 0.0
    side_fee: float = 0.0
    slippage: float = 0.0

    @property
    def total(self) -> float:
        return float(
            self.commission
            + self.stamp_duty
            + self.transfer_fee
            + self.side_fee
            + self.slippage
        )

    def to_dict(self) -> dict:
        return {
            "commission": self.commission,
            "stamp_duty": self.stamp_duty,
            "transfer_fee": self.transfer_fee,
            "side_fee": self.side_fee,
            "slippage": self.slippage,
            "transaction_cost": self.total,
        }


class FeeSchedule:
    """Select and apply a non-overlapping date-effective fee rule."""

    def __init__(self, rules: Iterable[FeeRule]):
        ordered = tuple(sorted(rules, key=lambda rule: rule.effective_from))
        if not ordered:
            raise ConfigurationError("FeeSchedule requires at least one rule")
        for previous, current in zip(ordered, ordered[1:]):
            if previous.effective_to is None or previous.effective_to >= current.effective_from:
                raise ConfigurationError("fee schedule rules must not overlap")
        self._rules: Tuple[FeeRule, ...] = ordered

    @property
    def rules(self) -> Tuple[FeeRule, ...]:
        return self._rules

    @classmethod
    def from_config(cls, config: object) -> "FeeSchedule":
        """Build rules from the typed ``FeesConfig`` without global state."""

        required = (
            "commission_bps",
            "minimum_commission_cny",
            "slippage_bps",
            "transfer_fee_bps",
            "buy_fee_bps",
            "sell_fee_bps",
            "stamp_duty",
        )
        missing = [name for name in required if not hasattr(config, name)]
        if missing:
            raise ConfigurationError(
                "fee config missing fields: %s" % ", ".join(missing)
            )
        common = {
            "commission_bps": config.commission_bps,
            "minimum_commission_cny": config.minimum_commission_cny,
            "slippage_bps": config.slippage_bps,
            "transfer_fee_bps": config.transfer_fee_bps,
            "buy_fee_bps": config.buy_fee_bps,
            "sell_fee_bps": config.sell_fee_bps,
        }
        return cls(
            FeeRule(
                effective_from=period.start,
                effective_to=period.end,
                stamp_duty_bps=period.sell_bps,
                **common,
            )
            for period in config.stamp_duty
        )

    @classmethod
    def a_share_default(
        cls,
        commission_bps: float = 3.0,
        minimum_commission_cny: float = 5.0,
        slippage_bps: float = 5.0,
        transfer_fee_bps: float = 0.0,
    ) -> "FeeSchedule":
        """Build the research default with the 2023-08-28 stamp-duty cut.

        Stamp duty is sell-only: 10 bps through 2023-08-27 and 5 bps from
        2023-08-28.  Other rates remain independent arguments.
        """

        common = {
            "commission_bps": commission_bps,
            "minimum_commission_cny": minimum_commission_cny,
            "slippage_bps": slippage_bps,
            "transfer_fee_bps": transfer_fee_bps,
        }
        return cls(
            (
                FeeRule(
                    effective_from=date(1900, 1, 1),
                    effective_to=date(2023, 8, 27),
                    stamp_duty_bps=10.0,
                    **common,
                ),
                FeeRule(
                    effective_from=date(2023, 8, 28),
                    effective_to=None,
                    stamp_duty_bps=5.0,
                    **common,
                ),
            )
        )

    def rule_for(self, trade_date: object) -> FeeRule:
        matches = [rule for rule in self._rules if rule.contains(trade_date)]
        if len(matches) != 1:
            raise ConfigurationError(
                "fee schedule must cover trade date %s exactly once" % pd.Timestamp(trade_date).date()
            )
        return matches[0]

    def calculate(self, trade_date: object, side: str, notional: float) -> FeeBreakdown:
        """Return explicit costs for one buy or sell order."""

        direction = str(side).strip().lower()
        if direction not in {"buy", "sell"}:
            raise ConfigurationError("trade side must be buy or sell")
        if isinstance(notional, bool):
            raise ConfigurationError("trade notional must be a finite number")
        try:
            amount = float(notional)
        except (TypeError, ValueError) as exc:
            raise ConfigurationError("trade notional must be a finite number") from exc
        if not math.isfinite(amount) or amount < 0:
            raise ConfigurationError(
                "trade notional must be finite and non-negative"
            )
        if amount == 0:
            return FeeBreakdown()
        rule = self.rule_for(trade_date)
        bps = 1.0 / 10_000.0
        commission = max(rule.minimum_commission_cny, amount * rule.commission_bps * bps)
        stamp = amount * rule.stamp_duty_bps * bps if direction == "sell" else 0.0
        side_rate = rule.buy_fee_bps if direction == "buy" else rule.sell_fee_bps
        return FeeBreakdown(
            commission=commission,
            stamp_duty=stamp,
            transfer_fee=amount * rule.transfer_fee_bps * bps,
            side_fee=amount * side_rate * bps,
            slippage=amount * rule.slippage_bps * bps,
        )


__all__ = ["FeeBreakdown", "FeeRule", "FeeSchedule"]
