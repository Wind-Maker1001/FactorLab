"""Session labels and timezone-aware instants are separate contracts."""
from datetime import datetime, time
from zoneinfo import ZoneInfo
import pandas as pd
from .contracts import ContractError, session_date


class SessionCalendar:
    def __init__(self, schedule, market, timezone, version):
        self.market, self.timezone, self.version = market, timezone, version
        self.schedule = schedule.copy()
        self.schedule.index = pd.DatetimeIndex([session_date(x) for x in self.schedule.index])
        if self.schedule.empty or self.schedule.index.has_duplicates or not self.schedule.index.is_monotonic_increasing:
            raise ContractError('nonempty unique sorted session schedule required')
        for col in ('open', 'close'):
            self.schedule[col] = pd.to_datetime(self.schedule[col], utc=True)
        if (self.schedule.open >= self.schedule.close).any():
            raise ContractError('invalid session times')

    def sessions(self, start, end):
        start, end = session_date(start), session_date(end)
        # Coverage is a closed civil-date envelope, including boundary holidays.
        if start < self.coverage_start or end > self.coverage_end or start > end:
            raise ContractError('calendar coverage insufficient')
        return self.schedule.loc[(self.schedule.index >= start) & (self.schedule.index <= end)].copy()

    @property
    def coverage_start(self):
        return getattr(self, '_start', self.schedule.index[0])

    @property
    def coverage_end(self):
        return getattr(self, '_end', self.schedule.index[-1])

    def is_session(self, day):
        self.sessions(day, day)
        return session_date(day) in self.schedule.index

    def shift(self, date, count):
        day = session_date(date)
        if day not in self.schedule.index:
            raise ContractError('not a trading session')
        pos = self.schedule.index.get_loc(day) + count
        if not 0 <= pos < len(self.schedule):
            raise ContractError('calendar does not cover execution/settlement horizon')
        return self.schedule.index[pos]

    def at(self, date, which='close'):
        return self.schedule.loc[session_date(date), which]


def us_calendar(start, end):
    import exchange_calendars as xcals
    import importlib.metadata
    start, end = session_date(start), session_date(end)
    cal = xcals.get_calendar('XNYS', start=start, end=end)
    schedule = cal.schedule[['open', 'close', 'break_start', 'break_end']].copy()
    schedule.index = schedule.index.tz_localize(None)
    result = SessionCalendar(schedule, 'US', 'America/New_York', 'exchange_calendars:' + importlib.metadata.version('exchange_calendars') + ':XNYS')
    result._start, result._end = start, end
    # Descriptions only. Actual execution is restricted to core sessions.
    result.extended_hours = {'XNAS': {'pre': ('04:00', '09:30'), 'post': ('16:00', '20:00')},
                             'XNYS': {'pre_open_orders': ('06:30', '09:30')}}
    return result


def cn_calendar(dates, version):
    labels = pd.DatetimeIndex([session_date(x) for x in dates])
    def local(hour, minute):
        return pd.DatetimeIndex([datetime.combine(d.date(), time(hour, minute), ZoneInfo('Asia/Shanghai')) for d in labels])
    schedule = pd.DataFrame({'open': local(9, 30), 'close': local(15, 0),
                             'break_start': local(11, 30), 'break_end': local(13, 0)}, index=labels)
    return SessionCalendar(schedule, 'CN', 'Asia/Shanghai', version)


def align_sessions(calendars, start, end):
    """Return union of UTC opens; never convert a session label into UTC midnight."""
    frames = [c.sessions(start, end).assign(market=c.market, session=lambda f: f.index) for c in calendars]
    return pd.concat(frames).sort_values('open')
