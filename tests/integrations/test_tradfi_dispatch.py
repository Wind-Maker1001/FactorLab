import pandas as pd

from factorlab.cross_market.calendar import us_calendar
from factorlab.integrations.binance_futures_trading.tradfi_dispatch import dispatch
from factorlab.integrations.binance_futures_trading.tradfi_schedule import WeeklySchedule
from factorlab.integrations.binance_futures_trading.tradfi_tasks import TaskMailbox


CONFIG = {'revision': 'v1', 'equity_usdt': '20', 'weights': {'QQQUSDT': '.4', 'SPYUSDT': '.4'}}


def test_dispatch_observes_session_then_recovers_same_task(tmp_path):
    schedule = WeeklySchedule(us_calendar('2026-11-01', '2026-12-31'))
    window = schedule.next_window('2026-11-30T14:00:00Z')
    box = TaskMailbox(tmp_path / 'tasks.sqlite3')
    assert dispatch(box, CONFIG, window, {}, '2026-11-30T14:00:00Z')['status'] == 'WAITING_SESSION'
    old = pd.Timestamp('2026-11-30T14:29:59Z').value // 1_000_000
    quotes = {s: {'time': old} for s in CONFIG['weights']}
    assert dispatch(box, CONFIG, window, quotes, '2026-11-30T14:30:01Z')['status'] == 'WAITING_SESSION_QUOTES'
    observed = pd.Timestamp('2026-11-30T14:30:01Z').value // 1_000_000
    quotes = {s: {'time': observed} for s in CONFIG['weights']}
    assert dispatch(box, CONFIG, window, quotes, '2026-11-30T14:31:00Z')['status'] == 'WAITING_FRESH_QUOTES'
    published = dispatch(box, CONFIG, window, quotes, '2026-11-30T14:30:02Z')
    assert published['status'] == 'QUEUED'
    payload = box.outstanding()[0]['task']
    box.close()
    recovered = TaskMailbox(tmp_path / 'tasks.sqlite3')
    assert dispatch(recovered, CONFIG, window, {}, '2026-11-30T15:00:00Z') == published
    assert recovered.outstanding()[0]['task'] == payload
    recovered.close()


def test_acceptance_is_explicit_and_distinct_from_scheduled_target(tmp_path):
    schedule = WeeklySchedule(us_calendar('2026-11-01', '2026-12-31'))
    window = schedule.next_window('2026-11-24T15:00:00Z')
    at = pd.Timestamp('2026-11-24T15:00:00Z')
    quotes = {s: {'time': at.value // 1_000_000} for s in CONFIG['weights']}
    box = TaskMailbox(tmp_path / 'tasks.sqlite3')
    assert dispatch(box, CONFIG, window, quotes, at)['status'] == 'WAITING_SESSION'
    result = dispatch(box, CONFIG, window, quotes, at, acceptance_task_id='acceptance:001')
    assert result == {'task_id': 'acceptance:001', 'status': 'QUEUED'}
    assert box.outstanding()[0]['task']['task_type'] == 'ACCEPTANCE'
    box.close()
