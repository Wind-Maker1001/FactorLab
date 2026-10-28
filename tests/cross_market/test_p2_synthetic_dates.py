"""Date-effective synthetic fee scenarios cover exactly the requested interval."""
import pandas as pd
import pytest

from factorlab.cross_market.adapters import FeePeriod
from factorlab.cross_market.contracts import ContractError
from factorlab.cross_market.synthetic import build_us


@pytest.mark.parametrize('start,end', [
    ('2024-01-02', '2024-05-24'),
    ('2024-06-03', '2025-06-30'),
    ('2024-05-24', '2024-05-29'),
    ('2024-05-01', '2024-05-27'),
    ('2024-05-28', '2024-06-07'),
    ('2024-05-28', '2024-05-28'),
    ('2024-05-24', '2024-05-24'),
    ('2000-01-01', '2025-06-30'),
])
def test_fee_intersections_have_no_gaps_or_overlaps_and_query_every_session(start, end):
    bars, a = build_us(start, end, count=10)
    periods = a.fees.periods
    assert periods and a.fees.version == 'SYNTHETIC_FEE_SCENARIO_NOT_US_TARIFF'
    assert pd.Timestamp(periods[0].start) == pd.Timestamp(start)
    assert pd.Timestamp(periods[-1].end) == pd.Timestamp(end)
    for p in periods:
        assert pd.Timestamp(start) <= pd.Timestamp(p.start) <= pd.Timestamp(p.end) <= pd.Timestamp(end)
    for left, right in zip(periods, periods[1:]):
        assert pd.Timestamp(left.end) + pd.Timedelta(days=1) == pd.Timestamp(right.start)
    # Include holidays and civil boundaries as well as every actual session.
    for day in pd.date_range(start, end):
        assert sum(pd.Timestamp(p.start) <= day <= pd.Timestamp(p.end) for p in periods) == 1
    sessions = a.calendar.sessions(start, end).index
    assert len(sessions) and set(bars.date) <= set(sessions)
    for day in sessions:
        before = day < pd.Timestamp('2024-05-28')
        assert a.fees.charge(day, 'buy', 100, 10) == pytest.approx(.5 if before else .2)
        assert a.fees.charge(day, 'sell', 100, 10) == pytest.approx(.51 if before else .23)
    with pytest.raises(ContractError, match='does not cover'):
        a.fees.charge(pd.Timestamp(start) - pd.Timedelta(days=1), 'buy', 1, 10)
    with pytest.raises(ContractError, match='does not cover'):
        a.fees.charge(pd.Timestamp(end) + pd.Timedelta(days=1), 'sell', 1, 10)


def test_inverted_research_interval_and_fee_period_still_refuse():
    with pytest.raises(ValueError):
        build_us('2024-06-07', '2024-06-03', count=10)
    with pytest.raises(ContractError, match='invalid fee period'):
        FeePeriod('2024-05-28', '2024-05-27')
