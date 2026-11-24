import pandas as pd
import pytest

from factorlab.cross_market.calendar import us_calendar
from factorlab.integrations.binance_futures_trading.tradfi_schedule import WeeklySchedule


@pytest.fixture(scope='module')
def schedule():
    return WeeklySchedule(us_calendar('2026-01-01', '2027-01-31'))


def test_monday_holiday_and_completed_history(schedule):
    window = schedule.next_window('2026-09-07T12:00:00Z')
    assert window.session == '2026-09-08'
    assert window.history_through == '2026-09-04'
    assert window.opens_at == pd.Timestamp('2026-09-08T13:30:00Z')


def test_dst_changes_utc_open(schedule):
    before = schedule.next_window('2026-03-02T12:00:00Z')
    after = schedule.next_window('2026-03-09T12:00:00Z')
    assert before.opens_at.hour == 14
    assert after.opens_at.hour == 13


def test_quote_must_be_after_open_and_before_close(schedule):
    window = schedule.next_window('2026-11-23T12:00:00Z')
    assert not window.accepts('2026-11-23T14:29:59Z', '2026-11-23T14:31:00Z')
    assert window.accepts('2026-11-23T14:30:01Z', '2026-11-23T14:31:00Z')
    assert not window.accepts('2026-11-23T14:32:00Z', '2026-11-23T14:31:00Z')
    assert not window.accepts('2026-11-23T20:59:00Z', '2026-11-23T21:00:00Z')


def test_no_late_catchup_or_restart_duplicate(schedule):
    assert schedule.next_window('2026-11-23T21:00:00Z').session == '2026-11-30'
    assert schedule.next_window('2026-11-23T15:00:00Z', completed_session='2026-11-23').session == '2026-11-30'


def test_early_close_and_year_boundary(schedule):
    cal = schedule.calendar
    assert cal.at('2026-11-27') == pd.Timestamp('2026-11-27T18:00:00Z')
    assert schedule.next_window('2026-12-31T23:00:00Z').session == '2027-01-04'


def test_naive_time_and_incomplete_coverage_refused(schedule):
    with pytest.raises(ValueError, match='timezone-aware'):
        schedule.next_window('2026-11-23')
    with pytest.raises(ValueError, match='coverage'):
        schedule.next_window('2028-01-01T12:00:00Z')
