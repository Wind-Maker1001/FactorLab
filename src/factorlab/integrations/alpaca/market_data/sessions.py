"""Alpaca civil-date calendars, regular sessions, and injected-clock diagnostics.

Calendar rows contain ``date/open/close`` in New York local time. Coverage is
an explicit inclusive envelope, including holidays; no calendar is inferred.
"""
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Mapping

import pandas as pd

from factorlab.cross_market.calendar import SessionCalendar
from factorlab.cross_market.contracts import ContractError, instant, session_date


class AlpacaSessionCalendar(SessionCalendar):
    def __init__(self, rows: Iterable[Mapping], *, coverage_start, coverage_end,
                 version: str = 'alpaca'):
        start, end = session_date(coverage_start), session_date(coverage_end)
        records = []
        for row in rows:
            day = session_date(row['date'])
            times = [pd.Timestamp(f"{day.date()} {row[key]}").tz_localize('America/New_York')
                     for key in ('open', 'close')]
            records.append((day, *times))
        schedule = pd.DataFrame(records, columns=['session', 'open', 'close']).set_index('session')
        super().__init__(schedule, 'US', 'America/New_York', version)
        if start > self.schedule.index[0] or end < self.schedule.index[-1] or start > end:
            raise ContractError('schedule outside coverage envelope')
        self._start, self._end = start, end

    def regular_session(self, at):
        at = instant(at)
        day = at.tz_convert(self.timezone).tz_localize(None).normalize()
        rows = self.sessions(day, day)
        return day if not rows.empty and rows.iloc[0].open <= at < rows.iloc[0].close else None

    def first_session(self, day, *, period: str = 'week'):
        day = session_date(day)
        self.sessions(day, day)
        if period == 'week':
            start = day - pd.Timedelta(days=day.weekday())
            end = start + pd.Timedelta(days=6)
        elif period == 'year':
            start, end = pd.Timestamp(day.year, 1, 1), pd.Timestamp(day.year, 12, 31)
        else:
            raise ValueError('period must be week or year')
        if start < self.coverage_start:
            raise ContractError('calendar coverage insufficient for period start')
        rows = self.sessions(start, min(end, self.coverage_end))
        if rows.empty:
            raise ContractError('no scheduled session in covered period')
        return rows.index[0]

    def next_session(self, at):
        """Return the next strictly future scheduled open and its aware times."""
        at = instant(at)
        day = at.tz_convert(self.timezone).tz_localize(None).normalize()
        self.sessions(day, day)
        future = self.schedule.loc[self.schedule.open > at]
        if future.empty:
            raise ContractError('calendar does not cover next session')
        row = future.iloc[0]
        return ScheduledSession(future.index[0], row.open, row.close)


@dataclass(frozen=True)
class ScheduledSession:
    session: pd.Timestamp
    open: pd.Timestamp
    close: pd.Timestamp


@dataclass(frozen=True)
class ClockDiagnostic:
    vendor_at: pd.Timestamp
    observed_at: pd.Timestamp
    offset: pd.Timedelta
    reasons: tuple[str, ...]


def diagnose_clock(clock: Mapping, calendar: AlpacaSessionCalendar, *, now: datetime,
                   tolerance=pd.Timedelta(seconds=5)) -> ClockDiagnostic:
    observed, vendor = instant(now), instant(clock['timestamp'])
    reasons = []
    if abs(vendor - observed) > pd.Timedelta(tolerance):
        reasons.append('clock_skew')
    if bool(clock['is_open']) != (calendar.regular_session(observed) is not None):
        reasons.append('session_state_mismatch')
    return ClockDiagnostic(vendor, observed, vendor - observed, tuple(reasons))
