from decimal import Decimal

import pytest

from factorlab.errors import DataQualityError
from factorlab.experiments.mc_grid_tail_f04c_data import HOUR, MINUTE, _aggregate, _normalize
from factorlab.integrations.binance.market import milliseconds


START = milliseconds('2026-08-05')


def _row(start, *, duration=MINUTE, low='97.00', volume='0.12', count=2):
    return [start, '98.00', '101.00', low, '100.00', volume, start + duration - 1,
            '11.90', count, '0.01', '0.99', '0']


@pytest.mark.parametrize('indices', [(0,), (0, 0), (1, 0)])
def test_incomplete_duplicate_or_unordered_minutes_fail(indices):
    rows = [_row(START + index * MINUTE) for index in indices]
    with pytest.raises(DataQualityError, match='raw buckets missing, duplicate, unordered'):
        _normalize(rows, START, START + 2 * MINUTE, '1m', START + 2 * MINUTE)


def test_minute_must_be_aligned_and_closed_at_receipt():
    end = START + MINUTE
    raw = _row(START)
    with pytest.raises(DataQualityError, match='buckets missing'):
        _normalize([raw], START, end, '1m', end - 1)
    raw[6] = end
    with pytest.raises(DataQualityError, match='not aligned'):
        _normalize([raw], START, end, '1m', end + 1)


def test_exact_hourly_aggregation_uses_all_completed_minutes():
    end = START + HOUR
    raw_minutes = [_row(START + index * MINUTE) for index in range(60)]
    raw_minutes[17][3] = '96.49'
    minutes = _normalize(raw_minutes, START, end, '1m', end)
    raw_hour = _row(START, duration=HOUR, low='96.49', volume='7.20', count=120)
    raw_hour[7], raw_hour[9], raw_hour[10] = '714.00', '0.60', '59.40'
    hours = _normalize([raw_hour], START, end, '1h', end)
    result = _aggregate(minutes, hours, 60, 'minute to hour')
    assert result['ohlcv_matches'] == 1
    assert all(value == 0 for value in result['auxiliary_mismatches'].values())
    assert minutes[0]['time'] == hours[0]['time'] == '2026-08-05T00:00:00+00:00'
    assert minutes[-1]['available_at'] == hours[0]['available_at'] == '2026-08-05T01:00:00+00:00'

    wrong_hour = hours[0] | {'volume': Decimal('7.19')}
    with pytest.raises(DataQualityError, match='minute to hour volume mismatch'):
        _aggregate(minutes, (wrong_hour,), 60, 'minute to hour')
