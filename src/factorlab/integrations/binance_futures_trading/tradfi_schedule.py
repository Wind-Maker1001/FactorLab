"""US core-session scheduling for signal producers, without exchange write access."""
from dataclasses import dataclass
from datetime import timedelta

import pandas as pd

from factorlab.cross_market.calendar import SessionCalendar


@dataclass(frozen=True)
class RebalanceWindow:
    session: str
    opens_at: pd.Timestamp
    closes_at: pd.Timestamp
    history_through: str
    calendar_version: str

    def accepts(self, observed_at, now, *, max_quote_age_seconds=5):
        """Only a fresh quote observed in this session can authorize a target."""
        if max_quote_age_seconds <= 0:
            raise ValueError('positive quote age bound required')
        observed_at, now = utc_instant(observed_at), utc_instant(now)
        return (self.opens_at <= observed_at <= now < self.closes_at
                and (now - observed_at).total_seconds() <= max_quote_age_seconds)


def utc_instant(value):
    instant = pd.Timestamp(value)
    if pd.isna(instant) or instant.tzinfo is None:
        raise ValueError('timezone-aware instant required')
    return instant.tz_convert('UTC')


class WeeklySchedule:
    def __init__(self, calendar: SessionCalendar):
        if calendar.market != 'US' or calendar.timezone != 'America/New_York':
            raise ValueError('US Eastern session calendar required')
        self.calendar = calendar

    def next_window(self, now, *, completed_session=None):
        """Return the current or next weekly window; never catch up after close.

        The calendar must cover the preceding session and civil Monday for each
        candidate, so holidays cannot silently turn a Tuesday into a second run.
        """
        now = utc_instant(now)
        day = now.tz_convert(self.calendar.timezone).tz_localize(None).normalize()
        self.calendar.sessions(day, day)
        completed = None if completed_session is None else pd.Timestamp(completed_session)
        for session, row in self.calendar.sessions(day, self.calendar.coverage_end).iterrows():
            if row['close'] <= now or (completed is not None and session <= completed):
                continue
            monday = session - timedelta(days=session.weekday())
            week = self.calendar.sessions(monday, session)
            if week.index[0] != session:
                continue
            previous = self.calendar.shift(session, -1)
            return RebalanceWindow(str(session.date()), row['open'], row['close'],
                                   str(previous.date()), self.calendar.version)
        raise ValueError('calendar does not cover next weekly rebalance')
