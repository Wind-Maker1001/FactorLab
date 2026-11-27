"""UTC instants for persisted timestamps and deadline comparisons."""
from dataclasses import dataclass
from datetime import UTC, datetime
import re

# Match the wall-clock fraction, excluding any fraction in the UTC offset.
_FRACTION = re.compile(r"^[^Tt ]+[Tt ][0-9:]+[.,]([0-9]+)")


@dataclass(frozen=True, order=True)
class UTCInstant:
    _time: datetime
    _nanosecond: int

    def isoformat(self) -> str:
        """Use the mailbox's canonical UTC format with 0, 6 or 9 fraction digits."""
        if self._nanosecond == 0:
            return self._time.isoformat()
        wall_time, offset = self._time.isoformat(timespec="microseconds").split("+")
        return f"{wall_time}{self._nanosecond:03d}+{offset}"


def utc_instant(value: str | datetime) -> UTCInstant:
    """Parse an aware ISO timestamp or datetime without losing its nanoseconds."""
    if isinstance(value, str):
        text = value.strip()
        instant = datetime.fromisoformat(text)
        fraction = _FRACTION.match(text)
        # Match the existing mailbox parser's truncation beyond nine digits.
        nanosecond = 0 if fraction is None else int(fraction[1][6:9].ljust(3, "0"))
    else:
        nanosecond = getattr(value, "nanosecond", 0)
        # Use a stdlib datetime even when a producer passes a pandas Timestamp.
        instant = datetime.combine(value.date(), value.timetz())
    if instant.utcoffset() is None:
        raise ValueError("timezone-aware instant required")
    return UTCInstant(instant.astimezone(UTC), nanosecond)
