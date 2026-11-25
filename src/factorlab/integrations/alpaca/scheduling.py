"""Causal Alpaca schedule windows anchored to official regular sessions."""
from dataclasses import dataclass
from datetime import datetime

import pandas as pd


@dataclass(frozen=True)
class ScheduleWindow:
    event_id: str
    session: str
    signal_cutoff: datetime
    opened_at: datetime
    closed_at: datetime
    interval_seconds: int | None


def schedule_window(calendar, schedule, at):
    """Return the latest eligible event; a host journals its identity before execution.

    Interval bars start at the official session open. A short final segment is
    not a full bar, and no overnight or extended-hours interval is synthesized.
    """
    at = pd.Timestamp(at)
    day = at.tz_convert(calendar.timezone).tz_localize(None).normalize()
    rows = calendar.sessions(day, day)
    if rows.empty:
        return None
    opened, closed = rows.iloc[0].open, rows.iloc[0].close
    if not opened <= at < closed:
        return None
    if schedule['kind'] == 'calendar':
        if (schedule['calendar_id'] != 'alpaca' or schedule['timezone'] != 'America/New_York'
                or schedule['event'] not in {'session_open', 'natural_week_open'}):
            raise ValueError('unsupported Alpaca calendar schedule')
        if schedule['event'] == 'natural_week_open' and calendar.first_session(day) != day:
            return None
        previous = calendar.schedule.index[calendar.schedule.index < day]
        if previous.empty:
            raise ValueError('calendar does not cover signal cutoff')
        cutoff = calendar.at(previous[-1], 'close')
        event = schedule['event'] + ':' + day.date().isoformat()
        return ScheduleWindow(event, day.date().isoformat(), cutoff.to_pydatetime(),
                              opened.to_pydatetime(), closed.to_pydatetime(), None)
    if schedule['kind'] != 'interval':
        raise ValueError('unsupported Alpaca schedule')
    seconds = schedule['interval_seconds']
    if type(seconds) is not int or seconds < 60 or seconds % 60 or seconds > 3600:
        raise ValueError('Alpaca closed-bar intervals must be whole minutes up to one hour')
    count = int((at - opened).total_seconds()) // seconds
    if not count:
        return None
    cutoff = opened + pd.Timedelta(seconds=count * seconds)
    start = cutoff - pd.Timedelta(seconds=seconds)
    return ScheduleWindow('closed-bar:' + cutoff.isoformat() + ':' + str(seconds),
                          day.date().isoformat(), cutoff.to_pydatetime(),
                          start.to_pydatetime(), closed.to_pydatetime(), seconds)


def aggregate_minutes(frame, *, calendar, interval_seconds, now):
    """Aggregate complete contiguous RTH minutes without filling absent observations."""
    if type(interval_seconds) is not int or interval_seconds < 60 or interval_seconds % 60:
        raise ValueError('whole-minute interval required')
    now = pd.Timestamp(now)
    eligible = frame.loc[frame.complete & (frame.closed_at <= now) & (frame.received_at <= now)]
    records = []
    for (instrument_id, session), group in eligible.groupby(['instrument_id', 'session'], sort=True):
        opened, closed = calendar.at(session, 'open'), calendar.at(session, 'close')
        group = group.loc[(group.opened_at >= opened) & (group.closed_at <= closed)].copy()
        group['bucket'] = ((group.opened_at - opened).dt.total_seconds() // interval_seconds).astype(int)
        for bucket, minutes in group.groupby('bucket', sort=True):
            start = opened + pd.Timedelta(seconds=int(bucket) * interval_seconds)
            end = start + pd.Timedelta(seconds=interval_seconds)
            expected = pd.date_range(start, end, freq='min', inclusive='left')
            minutes = minutes.sort_values('opened_at')
            if (end > closed or end > now or len(minutes) != interval_seconds // 60
                    or list(minutes.opened_at) != list(expected)):
                continue
            if len(set(minutes.feed)) != 1 or len(set(minutes.adjustment)) != 1:
                raise ValueError('mixed bar provenance')
            first, last = minutes.iloc[0], minutes.iloc[-1]
            row = first.to_dict()
            row.pop('bucket')
            row.update(event_at=start, opened_at=start, closed_at=end,
                       received_at=minutes.received_at.max(), timeframe=str(interval_seconds) + 'Sec',
                       complete=True)
            prefix = '' if first.adjustment == 'raw' else 'adjusted_'
            for name, value in (('open', first[prefix + 'open']), ('high', minutes[prefix + 'high'].max()),
                                ('low', minutes[prefix + 'low'].min()), ('close', last[prefix + 'close']),
                                ('volume', minutes[prefix + 'volume'].sum())):
                row[prefix + name] = value
                if first.adjustment == 'raw':
                    row['raw_' + name] = value
            records.append(row)
    columns = list(frame.columns)
    return pd.DataFrame(records, columns=columns).sort_values(['event_at', 'instrument_id']).reset_index(drop=True)
