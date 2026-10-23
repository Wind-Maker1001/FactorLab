import pandas as pd
import pytest

from qbot.research.data.providers.akshare import AKShareProvider
from qbot.research.universe.csi500 import (
    BaoStockPointInTimeProvider,
    validate_daily_membership,
)


pytestmark = pytest.mark.network


def test_real_akshare_single_security_and_benchmark():
    provider = AKShareProvider(request_interval=0.1)
    stock = provider.get_daily_bars("600000.SH", "2024-01-02", "2024-01-12")
    benchmark = provider.get_index_bars("000905.SH", "2024-01-02", "2024-01-12")

    assert not stock.empty
    assert not benchmark.empty
    assert stock["symbol"].eq("600000.SH").all()
    assert stock["raw_close"].notna().all()
    assert stock["adj_close"].notna().all()
    assert pd.DatetimeIndex(benchmark["date"]).is_monotonic_increasing


def test_real_baostock_historical_csi500_snapshot():
    date = pd.Timestamp("2024-01-05")
    with BaoStockPointInTimeProvider() as provider:
        snapshot = provider.get_constituents("000905.SH", date)
    validated = validate_daily_membership(
        snapshot, requested_dates=[date], expected_member_count=500
    )

    assert validated["symbol"].nunique() == 500
