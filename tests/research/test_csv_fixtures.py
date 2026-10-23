from pathlib import Path

import pandas as pd

from factorlab.data.schema import canonicalize_bars
from factorlab.universe.csi500 import validate_daily_membership


FIXTURES = Path(__file__).parents[1] / "fixtures" / "research"


def test_committed_csv_fixtures_follow_canonical_contracts():
    bars = canonicalize_bars(pd.read_csv(FIXTURES / "bars.csv"))
    benchmark = canonicalize_bars(pd.read_csv(FIXTURES / "benchmark.csv"))
    calendar = pd.DatetimeIndex(benchmark["date"])
    membership = validate_daily_membership(
        pd.read_csv(FIXTURES / "pit_universe.csv"),
        requested_dates=calendar,
        expected_member_count=2,
    )

    assert bars["symbol"].nunique() == 2
    assert benchmark["symbol"].unique().tolist() == ["000905.SH"]
    assert membership.groupby("date")["symbol"].nunique().eq(2).all()
