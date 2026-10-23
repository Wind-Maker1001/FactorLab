"""Clock policy for honest A-share forward-signal publication."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from qbot.research.errors import FactorLabError


class ForwardTimingError(FactorLabError):
    """Raised when a signal would be future-dated or mislabelled as timely."""


@dataclass(frozen=True)
class ForwardTiming:
    signal_date: date
    generated_at: datetime
    timeliness: str
    reissue_reason: Optional[str]


def _date(value: object, name: str) -> date:
    if isinstance(value, datetime):
        raise ForwardTimingError("%s must be a date without a time" % name)
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise ForwardTimingError("%s must be an ISO date" % name) from exc


def _now(value: Optional[datetime]) -> datetime:
    result = value or datetime.now(timezone.utc)
    if not isinstance(result, datetime) or result.tzinfo is None:
        raise ForwardTimingError("generated_at must be timezone-aware")
    try:
        if result.utcoffset() is None:
            raise ForwardTimingError("generated_at must be timezone-aware")
    except (TypeError, ValueError) as exc:
        raise ForwardTimingError("generated_at has an invalid timezone") from exc
    return result


def classify_forward_timing(
    signal_date: object,
    *,
    activation_date: object,
    generated_at: Optional[datetime] = None,
    timezone_name: str = "Asia/Shanghai",
    market_close_hour: int = 15,
    market_close_minute: int = 30,
    reissue_reason: Optional[str] = None,
) -> ForwardTiming:
    """Classify a publication using the configured market-local clock.

    A current-day signal is accepted only after the configured close.  An old
    signal date is never presented as timely: it requires an explicit reason
    and is labelled ``LATE_REISSUE``.  Future dates and dates before the
    pre-registered activation boundary are always rejected.
    """

    day = _date(signal_date, "signal_date")
    activation = _date(activation_date, "activation_date")
    if day < activation:
        raise ForwardTimingError(
            "signal_date predates the registered forward activation date"
        )
    try:
        market_timezone = ZoneInfo(timezone_name)
    except (TypeError, ZoneInfoNotFoundError) as exc:
        raise ForwardTimingError("unknown forward timezone: %s" % timezone_name) from exc
    if not isinstance(market_close_hour, int) or isinstance(market_close_hour, bool):
        raise ForwardTimingError("market_close_hour must be an integer")
    if not isinstance(market_close_minute, int) or isinstance(
        market_close_minute, bool
    ):
        raise ForwardTimingError("market_close_minute must be an integer")
    try:
        close_time = time(market_close_hour, market_close_minute)
    except ValueError as exc:
        raise ForwardTimingError("configured market close time is invalid") from exc

    market_now = _now(generated_at).astimezone(market_timezone)
    local_day = market_now.date()
    if day > local_day:
        raise ForwardTimingError("future-dated forward signals are forbidden")
    if day == local_day:
        if market_now.time().replace(tzinfo=None) < close_time:
            raise ForwardTimingError(
                "same-day forward signal cannot be generated before market close"
            )
        if reissue_reason not in (None, ""):
            raise ForwardTimingError(
                "same-day ON_TIME signal must not include a reissue reason"
            )
        return ForwardTiming(day, market_now, "ON_TIME", None)

    if not isinstance(reissue_reason, str) or not reissue_reason.strip():
        raise ForwardTimingError(
            "an old signal date requires --reissue-reason and is never ON_TIME"
        )
    if reissue_reason != reissue_reason.strip():
        raise ForwardTimingError("reissue_reason must not have surrounding whitespace")
    return ForwardTiming(day, market_now, "LATE_REISSUE", reissue_reason)


__all__ = [
    "ForwardTiming",
    "ForwardTimingError",
    "classify_forward_timing",
]
