from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from factorlab.integrations.alpaca.market_data.inputs import Instrument, normalize_bars
from factorlab.integrations.alpaca.market_data.sessions import AlpacaSessionCalendar
from factorlab.integrations.alpaca.scheduling import aggregate_minutes, schedule_window

NY = ZoneInfo('America/New_York')


def calendar():
    return AlpacaSessionCalendar([
        {'date': '2026-11-20', 'open': '09:30', 'close': '16:00'},
        {'date': '2026-11-23', 'open': '09:30', 'close': '16:00'},
        {'date': '2026-11-24', 'open': '09:30', 'close': '16:00'},
        {'date': '2026-11-25', 'open': '09:30', 'close': '16:00'},
        {'date': '2026-11-27', 'open': '09:30', 'close': '13:00'},
    ], coverage_start='2026-11-16', coverage_end='2026-11-30')


def at(day, hour, minute):
    return datetime(2026, 11, day, hour, minute, tzinfo=NY)


def test_weekly_cutoff_is_previous_official_session_and_daily_is_not_weekly():
    cal = calendar()
    weekly = dict(kind='calendar', calendar_id='alpaca', timezone='America/New_York', event='natural_week_open')
    window = schedule_window(cal, weekly, at(23, 9, 30))
    assert window.session == '2026-11-23'
    assert window.signal_cutoff == at(20, 16, 0)
    assert schedule_window(cal, weekly, at(24, 10, 0)) is None
    daily = dict(weekly, event='session_open')
    assert schedule_window(cal, daily, at(24, 10, 0)).signal_cutoff == at(23, 16, 0)
    assert schedule_window(cal, daily, at(26, 10, 0)) is None


def test_hourly_bars_anchor_to_session_and_do_not_emit_early_close_stub():
    spec = dict(kind='interval', interval_seconds=3600)
    cal = calendar()
    assert schedule_window(cal, spec, at(27, 10, 29)) is None
    assert schedule_window(cal, spec, at(27, 10, 30)).signal_cutoff == at(27, 10, 30)
    window = schedule_window(cal, spec, at(27, 12, 59))
    assert window.opened_at == at(27, 11, 30)
    assert window.signal_cutoff == at(27, 12, 30)
    assert schedule_window(cal, spec, at(27, 13, 0)) is None
    assert schedule_window(cal, spec, at(23, 9, 29)) is None


def test_hourly_anchor_tracks_official_dst_offset():
    cal = AlpacaSessionCalendar([
        {'date': '2026-03-06', 'open': '09:30', 'close': '16:00'},
        {'date': '2026-03-09', 'open': '09:30', 'close': '16:00'},
    ], coverage_start='2026-03-06', coverage_end='2026-03-09')
    schedule = dict(kind='interval', interval_seconds=3600)
    before = schedule_window(cal, schedule, pd.Timestamp('2026-03-06T15:30:00Z'))
    after = schedule_window(cal, schedule, pd.Timestamp('2026-03-09T14:30:00Z'))
    assert before.signal_cutoff == pd.Timestamp('2026-03-06T15:30:00Z')
    assert after.signal_cutoff == pd.Timestamp('2026-03-09T14:30:00Z')
    assert before.opened_at.astimezone(NY).hour == after.opened_at.astimezone(NY).hour == 9


@pytest.mark.parametrize('gap', [False, True])
def test_bar_aggregation_requires_complete_contiguous_minutes_and_keeps_feed(gap):
    cal = calendar()
    start = pd.Timestamp(at(27, 9, 30))
    stamps = pd.date_range(start, periods=210, freq='min')
    rows = [dict(t=t.isoformat(), o=10, h=11, l=9, c=10, v=1) for t in stamps]
    if gap:
        rows.pop(15)
    frame = normalize_bars(rows, Instrument('official-id', 'SPY', start), calendar=cal,
                           timeframe='1Min', received_at=at(27, 13, 0), feed='iex')
    bars = aggregate_minutes(frame, calendar=cal, interval_seconds=3600, now=at(27, 13, 0))
    assert len(bars) == (2 if gap else 3)
    assert list(bars.volume) == [60] * len(bars)
    assert set(bars.instrument_id) == {'official-id'}
    assert set(bars.feed) == {'iex'}
    assert bars.iloc[-1].closed_at == at(27, 12, 30)
