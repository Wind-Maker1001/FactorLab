"""Integration-level offline tests for point-in-time dataset construction."""

import pandas as pd
import pytest

from qbot.research.dataset.builder import DatasetBuildOptions, DatasetBuilder
from qbot.research.errors import SchemaError
from qbot.research.universe.filters import UniverseFilterConfig


def _dataset_inputs(periods=10):
    dates = pd.bdate_range("2024-01-02", periods=periods)
    symbols = ["000001.SZ", "600000.SH"]
    bars_rows = []
    membership_rows = []
    status_rows = []
    for date_number, date in enumerate(dates):
        for symbol_number, symbol in enumerate(symbols):
            raw = 10.0 + symbol_number + date_number
            adjusted = 20.0 + symbol_number + date_number * (1.0 + symbol_number * 0.1)
            bars_rows.append(
                {
                    "date": date,
                    "symbol": symbol,
                    "open": raw,
                    "high": raw + 1.0,
                    "low": raw - 1.0,
                    "close": raw,
                    "volume": 1000.0,
                    "amount": 1_000_000.0,
                    "turnover": 0.01,
                    "adj_open": adjusted,
                    "adj_close": adjusted,
                }
            )
            membership_rows.append({"date": date, "symbol": symbol, "is_member": True})
            status_rows.append(
                {
                    "date": date,
                    "symbol": symbol,
                    "trade_status": 1,
                    "is_st": False,
                }
            )
    benchmark = pd.DataFrame(
        {
            "date": dates,
            "adj_open": [100.0 + number for number in range(periods)],
            "adj_close": [100.5 + number for number in range(periods)],
        }
    )
    listings = pd.DataFrame(
        {"symbol": symbols, "listing_date": [dates[0], dates[0]]}
    )
    return (
        dates,
        pd.DataFrame(bars_rows),
        benchmark,
        pd.DataFrame(membership_rows),
        pd.DataFrame(status_rows),
        listings,
    )


def _builder():
    return DatasetBuilder(
        DatasetBuildOptions(
            factor_names=["REV1"],
            holding_days=2,
            expected_member_count=2,
            minimum_cross_section=2,
            filter_config=UniverseFilterConfig(
                min_listed_days=1,
                zero_volume_window=1,
                quality_window=1,
                min_quality_ratio=1.0,
            ),
        )
    )


def test_dataset_is_sorted_unique_and_keeps_raw_processed_and_label_audit_columns():
    dates, bars, benchmark, membership, status, listings = _dataset_inputs()
    result = _builder().build(
        bars, benchmark, membership, status, listings, dates
    )
    assert not result.duplicated(["date", "symbol"]).any()
    assert result.equals(result.sort_values(["date", "symbol"]).reset_index(drop=True))
    assert {
        "REV1",
        "raw_REV1",
        "entry_date",
        "exit_date",
        "STOCK_FWD_RET2",
        "BENCHMARK_FWD_RET2",
        "EXCESS_RET2",
        "MARKET_REGIME",
        "INDEX_RET5",
        "INDEX_RET20",
        "INDEX_RET60",
        "INDEX_VOL20",
    }.issubset(result.columns)
    assert len(result) == len(dates) * 2
    assert result.groupby("symbol").tail(3)["EXCESS_RET2"].isna().all()


def test_dataset_never_includes_an_ineligible_pit_member():
    dates, bars, benchmark, membership, status, listings = _dataset_inputs()
    status.loc[
        (status["date"] == dates[4]) & (status["symbol"] == "600000.SH"),
        "is_st",
    ] = True
    result = _builder().build(
        bars, benchmark, membership, status, listings, dates
    )
    selected = result.loc[result["date"] == dates[4], "symbol"].tolist()
    assert selected == ["000001.SZ"]


def test_dataset_fails_instead_of_publishing_zero_eligible_rows():
    dates, bars, benchmark, membership, status, listings = _dataset_inputs()
    status["is_st"] = True

    with pytest.raises(SchemaError, match="no eligible stock-days"):
        _builder().build(bars, benchmark, membership, status, listings, dates)


def test_dataset_artifacts_round_trip_as_parquet(tmp_path):
    dates, bars, benchmark, membership, status, listings = _dataset_inputs()
    result = _builder().build(
        bars, benchmark, membership, status, listings, dates
    )
    dataset_path = tmp_path / "processed" / "dataset.parquet"
    features_path = tmp_path / "features"
    _builder().write_artifacts(result, dataset_path, features_path)

    stored = pd.read_parquet(dataset_path)
    pd.testing.assert_frame_equal(stored, result)
    assert (features_path / "raw_factors.parquet").exists()
    assert (features_path / "processed_factors.parquet").exists()


def test_warmup_calendar_is_used_for_filters_and_factors_but_not_output_rows():
    dates, bars, benchmark, membership, status, listings = _dataset_inputs(periods=10)
    research_dates = dates[5:]
    membership = membership[membership["date"].isin(research_dates)].reset_index(drop=True)
    builder = DatasetBuilder(
        DatasetBuildOptions(
            factor_names=["REV5"],
            holding_days=2,
            expected_member_count=2,
            minimum_cross_section=2,
            filter_config=UniverseFilterConfig(
                min_listed_days=5,
                zero_volume_window=5,
                quality_window=5,
                min_quality_ratio=1.0,
            ),
        )
    )

    result = builder.build(
        bars, benchmark, membership, status, listings, dates
    )

    assert result["date"].drop_duplicates().tolist() == research_dates.tolist()
    first_day = result[result["date"] == research_dates[0]]
    assert len(first_day) == 2
    assert first_day["raw_REV5"].notna().all()
