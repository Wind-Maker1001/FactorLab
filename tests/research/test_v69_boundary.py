from datetime import datetime, timezone, timedelta
from decimal import Decimal as D
from io import StringIO
import pytest
from factorlab.strategies.v69 import (ASSETS, Variant, V69Strategy, SourceUnavailable,
    execution_weights, validate_weights, require_causal_execution, write_weight_diff,
    first_session_of_week)


def weights():
    return dict.fromkeys(ASSETS, D('.1'))


@pytest.mark.parametrize('variant', list(Variant))
def test_missing_sources_emit_nothing(variant):
    state = {'held': ['AAPL']}
    with pytest.raises(SourceUnavailable):
        V69Strategy(variant).decide({}, state)
    assert state == {'held': ['AAPL']}


def test_mapping_is_repeatable_and_does_not_redistribute():
    original = weights()
    for _ in range(2):
        assert execution_weights(original, {'TQQQ': 'test-contract'}) == {'test-contract': D('.1')}
    assert original == weights()
    assert execution_weights(original, {}) == {}
    with pytest.raises(ValueError):
        execution_weights(original, {'AAPL': 'x', 'QQQ': 'x'})


@pytest.mark.parametrize('bad', [D('NaN'), D('Infinity'), D('-.1'), .1])
def test_invalid_weights(bad):
    w = weights(); w['AAPL'] = bad
    with pytest.raises(ValueError):
        validate_weights(w)


def test_pool_cannot_shrink():
    w = weights(); del w['SNDK']
    with pytest.raises(ValueError):
        validate_weights(w)


def test_causal_timing():
    at = datetime(2026, 3, 9, 13, 30, tzinfo=timezone.utc)
    with pytest.raises(ValueError):
        require_causal_execution(at, at)
    require_causal_execution(at, at + timedelta(microseconds=1))
    with pytest.raises(ValueError):
        require_causal_execution(at.replace(tzinfo=None), at)


def test_diff_logs_every_stock():
    out = StringIO(); candidate = weights(); candidate['TQQQ'] = D('0')
    write_weight_diff(out, '2026-01-05', Variant.TRADFI, weights(), candidate)
    rows = out.getvalue().splitlines()
    assert len(rows) == 8
    assert 'TQQQ,0.1,0,-0.1' in rows[6]


def test_holiday_and_cross_year_week():
    import pandas as pd
    from factorlab.cross_market.calendar import SessionCalendar
    schedule = pd.DataFrame({'open': ['2025-12-30T14:30Z', '2026-01-02T14:30Z', '2026-01-05T14:30Z'],
                             'close': ['2025-12-30T21:00Z', '2026-01-02T21:00Z', '2026-01-05T21:00Z']},
                            index=pd.to_datetime(['2025-12-30','2026-01-02','2026-01-05']))
    cal = SessionCalendar(schedule, 'US', 'America/New_York', 'test-synthetic')
    cal._start = pd.Timestamp('2025-12-29')
    assert first_session_of_week(cal, '2025-12-30')
    assert not first_session_of_week(cal, '2026-01-02')
    assert first_session_of_week(cal, '2026-01-05')
