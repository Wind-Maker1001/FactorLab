"""Offline contracts for the CSI500 point-in-time universe and filters."""

import hashlib
import json
from datetime import datetime, timezone

import pandas as pd
import pytest

from factorlab.errors import PointInTimeUniverseError
from factorlab.universe.csi500 import (
    DataFramePointInTimeUniverse,
    _universe_write_lock,
    canonical_symbol,
    collect_point_in_time_universe,
    compress_membership_intervals,
    expand_membership_intervals,
    import_point_in_time_universe,
    validate_daily_membership,
)
from factorlab.universe.filters import (
    UniverseFilterConfig,
    apply_universe_filters,
    build_filter_flags,
)


def _membership(dates, members_by_date):
    rows = []
    for date, symbols in zip(dates, members_by_date):
        rows.extend(
            {"date": date, "symbol": symbol, "is_member": True}
            for symbol in symbols
        )
    return pd.DataFrame(rows)


def test_canonical_symbol_accepts_source_forms_and_rejects_invalid_values():
    assert canonical_symbol("sh.600000") == "600000.SH"
    assert canonical_symbol("000001.sz") == "000001.SZ"
    assert canonical_symbol("600000") == "600000.SH"
    assert canonical_symbol("830001") == "830001.BJ"
    with pytest.raises(PointInTimeUniverseError, match="POINT-IN-TIME UNIVERSE REQUIRED"):
        canonical_symbol("AAPL")


def test_daily_membership_requires_exact_dates_counts_and_unique_keys():
    dates = pd.to_datetime(["2024-01-02", "2024-01-03"])
    valid = _membership(dates, [["600000.SH", "000001.SZ"]] * 2)
    result = validate_daily_membership(valid, dates, expected_member_count=2)
    assert result.groupby("date")["symbol"].nunique().tolist() == [2, 2]

    with pytest.raises(PointInTimeUniverseError, match="snapshot coverage mismatch"):
        validate_daily_membership(valid[valid["date"] == dates[0]], dates, 2)
    with pytest.raises(PointInTimeUniverseError, match="expected 2 unique members"):
        validate_daily_membership(valid.iloc[:-1], dates, 2)
    with pytest.raises(PointInTimeUniverseError, match="duplicate memberships"):
        validate_daily_membership(pd.concat([valid, valid.iloc[[0]]]), dates, 2)


def test_membership_flags_are_parsed_strictly_instead_of_using_truthiness():
    date = pd.Timestamp("2024-01-02")
    frame = pd.DataFrame(
        {
            "date": [date, date],
            "symbol": ["600000.SH", "000001.SZ"],
            "is_member": [1.0, "0"],
        }
    )
    result = validate_daily_membership(frame, [date], expected_member_count=1)
    assert result["symbol"].tolist() == ["600000.SH"]

    frame.loc[1, "is_member"] = "not-a-boolean"
    with pytest.raises(PointInTimeUniverseError, match="invalid is_member"):
        validate_daily_membership(frame, [date], expected_member_count=1)


def test_membership_intervals_preserve_exit_and_reentry_boundaries():
    dates = pd.bdate_range("2024-01-02", periods=5)
    daily = _membership(
        dates,
        [
            ["600000.SH"],
            ["600000.SH"],
            ["000001.SZ"],
            ["600000.SH"],
            ["600000.SH"],
        ],
    )
    intervals = compress_membership_intervals(daily)
    stock = intervals[intervals["symbol"] == "600000.SH"].reset_index(drop=True)
    assert stock[["entry_date", "exit_date", "interval"]].to_dict("records") == [
        {"entry_date": dates[0], "exit_date": dates[1], "interval": 1},
        {"entry_date": dates[3], "exit_date": dates[4], "interval": 2},
    ]

    universe = DataFramePointInTimeUniverse(daily)
    assert universe.get_members(dates[1]) == ["600000.SH"]
    assert universe.get_members(dates[2]) == ["000001.SZ"]
    with pytest.raises(PointInTimeUniverseError, match="no snapshot"):
        universe.get_members(dates[-1] + pd.offsets.BDay())


def test_collected_and_imported_manifests_record_source_provenance(tmp_path):
    dates = pd.bdate_range("2024-01-02", periods=2)
    daily = _membership(dates, [["600000.SH", "000001.SZ"]] * len(dates))

    class FixtureProvider:
        def get_constituents(self, index_symbol, date):
            assert index_symbol == "000905.SH"
            return daily.loc[daily["date"] == pd.Timestamp(date)].copy()

    collected_dir = tmp_path / "collected"
    collected = collect_point_in_time_universe(
        FixtureProvider(),
        "000905.SH",
        dates,
        collected_dir,
        expected_member_count=2,
        retries=1,
        request_interval_seconds=0.0,
    )
    assert collected["schema_version"] == 3
    assert collected["source"] == "FixtureProvider"
    assert collected["source_version"] == "baostock-query_zz500_stocks-daily-v2"
    assert collected["index_symbol"] == "000905.SH"
    assert collected["expected_member_count"] == 2
    assert json.loads(
        (collected_dir / "manifest.json").read_text(encoding="utf-8")
    ) == collected

    source = tmp_path / "pit.csv"
    daily.to_csv(source, index=False)
    imported_dir = tmp_path / "imported"
    imported = import_point_in_time_universe(
        source,
        imported_dir,
        dates,
        expected_member_count=2,
        index_symbol="000905.SH",
    )
    assert imported["schema_version"] == 3
    assert imported["source"] == "local-import"
    assert imported["source_version"] == "user-pit-import-v2"
    assert imported["input_layout"] == "daily"
    assert imported["membership_semantics"] == "exact-trading-date-snapshot"
    assert imported["index_symbol"] == "000905.SH"
    assert imported["source_file_sha256"] == hashlib.sha256(
        source.read_bytes()
    ).hexdigest()
    assert imported["expected_member_count"] == 2
    assert json.loads(
        (imported_dir / "manifest.json").read_text(encoding="utf-8")
    ) == imported


def test_interval_import_expands_inclusive_boundaries_and_reentry(tmp_path):
    dates = pd.bdate_range("2024-01-02", periods=4)
    intervals = pd.DataFrame(
        {
            "symbol": ["600000.SH", "600000.SH", "000001.SZ", "000002.SZ"],
            "entry_date": [
                "2024-01-02",
                "2024-01-05",
                "2023-12-01",
                "2024-01-04",
            ],
            "exit_date": [
                "2024-01-03",
                "2024-12-31",
                "2024-12-31",
                "2024-01-04",
            ],
        }
    )
    expected = _membership(
        dates,
        [
            ["600000.SH", "000001.SZ"],
            ["600000.SH", "000001.SZ"],
            ["000001.SZ", "000002.SZ"],
            ["600000.SH", "000001.SZ"],
        ],
    )
    expanded = expand_membership_intervals(intervals, dates, 2)
    pd.testing.assert_frame_equal(
        expanded,
        validate_daily_membership(expected, dates, 2),
    )

    source = tmp_path / "pit_intervals.csv"
    intervals.to_csv(source, index=False)
    output = tmp_path / "imported"
    manifest = import_point_in_time_universe(
        source, output, dates, expected_member_count=2
    )
    assert manifest["schema_version"] == 3
    assert manifest["source_version"] == "user-pit-import-v2"
    assert manifest["input_layout"] == "inclusive-intervals"
    assert (
        manifest["membership_semantics"]
        == "entry_date <= trading_date <= exit_date"
    )
    assert manifest["daily_sha256"] == manifest["sha256"]
    assert len(manifest["intervals_sha256"]) == 64
    materialised = pd.read_parquet(output / "csi500_daily.parquet")
    pd.testing.assert_frame_equal(materialised, expanded)
    recompressed = pd.read_parquet(output / "csi500_intervals.parquet")
    stock = recompressed.loc[recompressed["symbol"] == "600000.SH"]
    assert stock[["entry_date", "exit_date"]].to_dict("records") == [
        {"entry_date": dates[0], "exit_date": dates[1]},
        {"entry_date": dates[3], "exit_date": dates[3]},
    ]


def test_equivalent_daily_and_interval_imports_have_same_universe_hash(tmp_path):
    dates = pd.bdate_range("2024-01-02", periods=3)
    daily = _membership(dates, [["600000.SH", "000001.SZ"]] * len(dates))
    intervals = pd.DataFrame(
        {
            "symbol": ["600000.SH", "000001.SZ"],
            "entry_date": ["2024-01-02", "2024-01-02"],
            "exit_date": ["2024-01-04", "2024-01-04"],
        }
    )
    daily_source = tmp_path / "daily.csv"
    interval_source = tmp_path / "intervals.csv"
    daily.to_csv(daily_source, index=False)
    intervals.to_csv(interval_source, index=False)
    daily_manifest = import_point_in_time_universe(
        daily_source, tmp_path / "daily", dates, expected_member_count=2
    )
    interval_manifest = import_point_in_time_universe(
        interval_source,
        tmp_path / "interval",
        iter(dates),
        expected_member_count=2,
    )
    assert daily_manifest["sha256"] == interval_manifest["sha256"]


def test_interval_import_rejects_overlap_and_ambiguous_source_conventions(tmp_path):
    dates = pd.bdate_range("2024-01-02", periods=4)
    overlapping = pd.DataFrame(
        {
            "symbol": ["600000.SH", "600000.SH"],
            "entry_date": ["2024-01-02", "2024-01-03"],
            "exit_date": ["2024-01-03", "2024-01-05"],
        }
    )
    with pytest.raises(PointInTimeUniverseError, match="overlapping inclusive"):
        expand_membership_intervals(overlapping, dates, expected_member_count=None)
    duplicated = pd.concat([overlapping.iloc[[0]], overlapping.iloc[[0]]])
    with pytest.raises(PointInTimeUniverseError, match="duplicate membership intervals"):
        expand_membership_intervals(duplicated, dates, expected_member_count=None)

    ambiguous = pd.DataFrame(
        {
            "symbol": ["600000.SH"],
            "opt-in": ["2024-01-02"],
            "opt-out": ["2024-01-05"],
        }
    )
    with pytest.raises(PointInTimeUniverseError, match="opt-in/opt-out dates are ambiguous"):
        source = tmp_path / "ambiguous.csv"
        ambiguous.to_csv(source, index=False)
        import_point_in_time_universe(
            source,
            tmp_path / "output",
            dates,
            expected_member_count=1,
        )


@pytest.mark.parametrize(
    "bad_value,match",
    [
        (None, "must not be missing"),
        (20240102, "numeric timestamp"),
        (datetime(2024, 1, 2, 12, 0), "time-of-day"),
        (datetime(2024, 1, 2, tzinfo=timezone.utc), "timezone-naive"),
        (pd.Timestamp("2024-01-02 00:00:00.000000001"), "time-of-day"),
        ("02/01/2024", "canonical YYYY-MM-DD"),
    ],
)
def test_interval_dates_reject_ambiguous_or_non_date_values(bad_value, match):
    dates = pd.bdate_range("2024-01-02", periods=3)
    intervals = pd.DataFrame(
        {
            "symbol": ["600000.SH"],
            "entry_date": [bad_value],
            "exit_date": ["2024-01-04"],
        }
    )
    with pytest.raises(PointInTimeUniverseError, match=match):
        expand_membership_intervals(intervals, dates, expected_member_count=1)


def test_interval_boundary_inside_span_must_be_benchmark_session():
    dates = pd.to_datetime(["2024-01-05", "2024-01-08", "2024-01-09"])
    intervals = pd.DataFrame(
        {
            "symbol": ["600000.SH"],
            "entry_date": ["2024-01-06"],
            "exit_date": ["2024-01-09"],
        }
    )
    with pytest.raises(PointInTimeUniverseError, match="benchmark trading date"):
        expand_membership_intervals(intervals, dates, expected_member_count=1)


def test_interval_import_rejects_inverted_and_mixed_layouts(tmp_path):
    dates = pd.bdate_range("2024-01-02", periods=3)
    inverted = pd.DataFrame(
        {
            "symbol": ["600000.SH"],
            "entry_date": ["2024-01-04"],
            "exit_date": ["2024-01-02"],
        }
    )
    with pytest.raises(PointInTimeUniverseError, match="on or after entry_date"):
        expand_membership_intervals(inverted, dates, expected_member_count=1)

    mixed = inverted.assign(date="2024-01-02")
    source = tmp_path / "mixed.csv"
    mixed.to_csv(source, index=False)
    with pytest.raises(PointInTimeUniverseError, match="layout is ambiguous"):
        import_point_in_time_universe(
            source, tmp_path / "output", dates, expected_member_count=1
        )


@pytest.mark.parametrize(
    "value,match",
    [
        ("2024-01-02T23:30:00-05:00", "canonical YYYY-MM-DD"),
        ("2024-01-02 12:00:00", "canonical YYYY-MM-DD"),
        (20240102, "numeric timestamp"),
    ],
)
def test_daily_import_rejects_noncanonical_dates(tmp_path, value, match):
    dates = pd.bdate_range("2024-01-02", periods=1)
    source = tmp_path / "daily.csv"
    pd.DataFrame({"date": [value], "symbol": ["600000.SH"]}).to_csv(
        source, index=False
    )
    with pytest.raises(PointInTimeUniverseError, match=match):
        import_point_in_time_universe(
            source, tmp_path / "output", dates, expected_member_count=1
        )


def test_interval_import_requires_explicit_exit_and_rejects_unknown_columns(
    tmp_path,
):
    dates = pd.bdate_range("2024-01-02", periods=2)
    missing_exit = pd.DataFrame(
        {
            "symbol": ["600000.SH"],
            "entry_date": ["2024-01-02"],
            "exit_date": [None],
        }
    )
    with pytest.raises(PointInTimeUniverseError, match="exit_date must not be missing"):
        expand_membership_intervals(missing_exit, dates, expected_member_count=1)

    source = tmp_path / "daily_with_unknown_column.csv"
    pd.DataFrame(
        {
            "date": ["2024-01-02", "2024-01-03"],
            "symbol": ["600000.SH", "600000.SH"],
            "source_note": ["ignored?", "ignored?"],
        }
    ).to_csv(source, index=False)
    with pytest.raises(PointInTimeUniverseError, match="unsupported daily import columns"):
        import_point_in_time_universe(
            source, tmp_path / "output", dates, expected_member_count=1
        )


def test_inclusive_rebalance_overlap_is_rejected_by_daily_member_count():
    dates = pd.bdate_range("2024-01-02", periods=3)
    intervals = pd.DataFrame(
        {
            "symbol": ["600000.SH", "000001.SZ"],
            "entry_date": ["2024-01-02", "2024-01-03"],
            "exit_date": ["2024-01-03", "2024-01-04"],
        }
    )
    with pytest.raises(PointInTimeUniverseError, match="expected 1 unique members"):
        expand_membership_intervals(intervals, dates, expected_member_count=1)


def test_interval_expansion_is_prefix_invariant():
    dates = pd.bdate_range("2024-01-02", periods=5)
    intervals = pd.DataFrame(
        {
            "symbol": ["600000.SH", "000001.SZ"],
            "entry_date": ["2023-12-01", "2024-01-08"],
            "exit_date": ["2024-12-31", "2024-12-31"],
        }
    )
    prefix = expand_membership_intervals(
        intervals, dates[:3], expected_member_count=None
    )
    full = expand_membership_intervals(intervals, dates, expected_member_count=None)
    pd.testing.assert_frame_equal(
        prefix,
        full.loc[full["date"].isin(dates[:3])].reset_index(drop=True),
    )


def test_import_writer_lock_prevents_concurrent_publication(tmp_path):
    dates = pd.bdate_range("2024-01-02", periods=2)
    source = tmp_path / "daily.csv"
    _membership(dates, [["600000.SH"], ["600000.SH"]]).to_csv(
        source, index=False
    )
    output = tmp_path / "universe"
    output.mkdir()
    lock = output / ".universe-write.lock"
    lock.write_text("existing writer\n", encoding="utf-8")

    with pytest.raises(PointInTimeUniverseError, match="writer lock already exists"):
        import_point_in_time_universe(
            source, output, dates, expected_member_count=1
        )
    assert lock.read_text(encoding="utf-8") == "existing writer\n"


def test_writer_does_not_remove_a_lock_it_no_longer_owns(tmp_path):
    output = tmp_path / "universe"
    with _universe_write_lock(output) as lock:
        lock.write_text("replacement owner\n", encoding="utf-8")

    assert lock.read_text(encoding="utf-8") == "replacement owner\n"
    lock.unlink()


def test_collector_never_launders_unprovenanced_cached_snapshots(tmp_path):
    dates = pd.bdate_range("2024-01-02", periods=2)
    output = tmp_path / "pit"
    snapshots = output / "snapshots"
    snapshots.mkdir(parents=True)
    stale = _membership(
        dates,
        [["600000.SH", "000001.SZ"], ["600000.SH", "000001.SZ"]],
    )
    for day in dates:
        stale.loc[stale["date"] == day].to_parquet(
            snapshots / (day.strftime("%Y-%m-%d") + ".parquet"),
            index=False,
        )

    expected = _membership(
        dates,
        [["600000.SH", "000001.SZ"], ["600000.SH", "000002.SZ"]],
    )

    class CountingProvider:
        def __init__(self):
            self.calls = []

        def get_constituents(self, index_symbol, date):
            self.calls.append(pd.Timestamp(date))
            return expected.loc[expected["date"] == pd.Timestamp(date)].copy()

    provider = CountingProvider()
    collect_point_in_time_universe(
        provider,
        "000905.SH",
        dates,
        output,
        expected_member_count=2,
        retries=1,
        request_interval_seconds=0.0,
    )
    assert provider.calls == list(dates)
    daily = pd.read_parquet(output / "csi500_daily.parquet")
    assert "000002.SZ" in daily.loc[daily["date"] == dates[1], "symbol"].tolist()
    assert len(list(snapshots.glob("*.json"))) == len(dates)

    provider.calls.clear()
    collect_point_in_time_universe(
        provider,
        "000905.SH",
        dates,
        output,
        expected_member_count=2,
        retries=1,
        request_interval_seconds=0.0,
    )
    assert provider.calls == []


def test_collector_retry_count_means_retries_after_the_initial_attempt(tmp_path):
    dates = pd.bdate_range("2024-01-02", periods=1)
    expected = _membership(dates, [["600000.SH", "000001.SZ"]])

    class OnceFlakyProvider:
        def __init__(self):
            self.calls = 0

        def get_constituents(self, index_symbol, date):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("temporary fixture failure")
            return expected.copy()

    provider = OnceFlakyProvider()
    manifest = collect_point_in_time_universe(
        provider,
        "000905.SH",
        dates,
        tmp_path / "pit",
        expected_member_count=2,
        retries=1,
        backoff_seconds=(0.0,),
        request_interval_seconds=0.0,
    )

    assert provider.calls == 2
    assert manifest["trading_dates"] == 1


def test_old_pit_contract_is_requeried_instead_of_relabelled(tmp_path):
    dates = pd.bdate_range("2024-01-02", periods=1)
    expected = _membership(dates, [["600000.SH", "000001.SZ"]])

    class Provider:
        def __init__(self):
            self.calls = 0

        def get_constituents(self, index_symbol, date):
            self.calls += 1
            return expected.copy()

    provider = Provider()
    output = tmp_path / "pit"
    collect_point_in_time_universe(
        provider, "000905.SH", dates, output,
        expected_member_count=2, retries=0, request_interval_seconds=0.0,
    )
    metadata_path = output / "snapshots" / "2024-01-02.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["source_version"] = "baostock-query_zz500_stocks-daily-v1"
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    manifest = collect_point_in_time_universe(
        provider, "000905.SH", dates, output,
        expected_member_count=2, retries=0, request_interval_seconds=0.0,
    )

    assert provider.calls == 2
    assert manifest["source_version"] == "baostock-query_zz500_stocks-daily-v2"
    assert json.loads(metadata_path.read_text(encoding="utf-8"))[
        "source_version"
    ] == manifest["source_version"]


def test_filter_flags_use_only_past_windows_and_current_status():
    dates = pd.bdate_range("2024-01-02", periods=6)
    symbols = ["600000.SH", "000001.SZ"]
    rows = []
    for date in dates:
        for symbol in symbols:
            rows.append(
                {
                    "date": date,
                    "symbol": symbol,
                    "open": 10.0,
                    "high": 11.0,
                    "low": 9.0,
                    "close": 10.0,
                    "volume": 100.0,
                }
            )
    bars = pd.DataFrame(rows)
    # A future missing observation must not affect an earlier day's flag.
    bars.loc[(bars["date"] == dates[4]) & (bars["symbol"] == "600000.SH"), "close"] = None
    # Two consecutive zero-volume observations invalidate only when both are in-window.
    bars.loc[(bars["date"].isin(dates[3:5])) & (bars["symbol"] == "000001.SZ"), "volume"] = 0.0
    status = pd.DataFrame(
        [
            {
                "date": date,
                "symbol": symbol,
                "trade_status": 1,
                "is_st": False,
            }
            for date in dates
            for symbol in symbols
        ]
    )
    status.loc[(status["date"] == dates[3]) & (status["symbol"] == "600000.SH"), "is_st"] = True
    status.loc[(status["date"] == dates[5]) & (status["symbol"] == "000001.SZ"), "trade_status"] = 0
    listings = pd.DataFrame(
        {"symbol": symbols, "listing_date": [dates[0], dates[0]]}
    )
    config = UniverseFilterConfig(
        min_listed_days=2,
        zero_volume_window=2,
        quality_window=3,
        min_quality_ratio=2.0 / 3.0,
    )

    flags = build_filter_flags(bars, status, listings, dates, config).set_index(
        ["date", "symbol"]
    )
    assert not flags.loc[(dates[0], "600000.SH"), "eligible"]
    assert flags.loc[(dates[2], "600000.SH"), "eligible"]
    assert not flags.loc[(dates[3], "600000.SH"), "eligible"]  # current ST
    assert flags.loc[(dates[4], "600000.SH"), "eligible"]  # 2/3 valid, no future use
    assert not flags.loc[(dates[4], "000001.SZ"), "eligible"]  # last two volumes zero
    assert not flags.loc[(dates[5], "000001.SZ"), "eligible"]  # suspended today


def test_filters_are_applied_after_strict_pit_validation():
    dates = pd.to_datetime(["2024-01-02"])
    membership = _membership(dates, [["600000.SH", "000001.SZ"]])
    flags = pd.DataFrame(
        {
            "date": [dates[0], dates[0]],
            "symbol": ["600000.SH", "000001.SZ"],
            "eligible": [True, False],
        }
    )
    filtered = apply_universe_filters(membership, flags, dates, expected_member_count=2)
    assert filtered["symbol"].tolist() == ["600000.SH"]

    with pytest.raises(PointInTimeUniverseError, match="expected 2 unique members"):
        apply_universe_filters(membership.iloc[:1], flags.iloc[:1], dates, 2)
