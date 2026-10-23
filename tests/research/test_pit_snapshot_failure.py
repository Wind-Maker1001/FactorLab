"""Daily PIT persistence failures must never become successful coverage."""

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from factorlab.cli import _load_verified_membership
from factorlab.errors import PointInTimeUniverseError
import factorlab.universe.csi500 as pit


class SyntheticSnapshotProvider:
    def __init__(self):
        self.calls = []

    def get_constituents(self, index_symbol, date):
        self.calls.append((index_symbol, pd.Timestamp(date)))
        return pd.DataFrame(
            {"date": [date], "symbol": ["600000.SH"], "is_member": [True]}
        )


def _collect(provider, dates, output, retries=2):
    return pit.collect_point_in_time_universe(
        provider,
        "000905.SH",
        dates,
        output,
        expected_member_count=1,
        retries=retries,
        backoff_seconds=(0.0,),
        request_interval_seconds=0.0,
    )


def _config(output):
    return SimpleNamespace(
        paths=SimpleNamespace(universe_dir=output),
        universe=SimpleNamespace(
            provider="baostock", index_symbol="000905.SH", expected_member_count=1
        ),
    )


def _read_published_diagnostic(output, manifest):
    """Verify generic artifacts without promoting their source to formal PIT."""

    for key in ("daily", "intervals"):
        assert hashlib.sha256((output / manifest[key + "_file"]).read_bytes()).hexdigest() == manifest[key + "_file_sha256"]
    daily = pd.read_parquet(output / manifest["daily_file"])
    intervals = pd.read_parquet(output / manifest["intervals_file"])
    assert pit.dataframe_sha256(daily) == manifest["sha256"]
    assert pit.dataframe_sha256(intervals) == manifest["intervals_sha256"]
    pd.testing.assert_frame_equal(intervals, pit.compress_membership_intervals(daily))
    return daily


def _inject_daily_write_failure(monkeypatch, kind, failures_before_success=None):
    attribute = "_atomic_parquet" if kind == "parquet" else "_atomic_json"
    original = getattr(pit, attribute)
    attempts = []

    def write(value, destination):
        path = Path(destination)
        if path.parent.name == "snapshots":
            attempts.append(path)
            if failures_before_success is None or len(attempts) <= failures_before_success:
                raise OSError("injected daily %s persistence failure" % kind)
        return original(value, destination)

    monkeypatch.setattr(pit, attribute, write)
    return attempts


@pytest.mark.parametrize("kind", ["parquet", "json"])
def test_persistent_daily_write_failure_is_reported_without_a_final_publication(
    tmp_path, monkeypatch, kind
):
    dates = pd.DatetimeIndex(["2024-01-02", "2024-01-03"])
    output = tmp_path / "universe"
    provider = SyntheticSnapshotProvider()
    attempts = _inject_daily_write_failure(monkeypatch, kind)

    with pytest.raises(PointInTimeUniverseError, match="2/2 daily snapshots unavailable"):
        _collect(provider, dates, output)

    assert len(provider.calls) == 6
    assert len(attempts) == 6
    assert not (output / "manifest.json").exists()
    assert not list(output.glob("csi500_daily.*.parquet"))
    assert not list(output.glob("csi500_intervals.*.parquet"))
    failed = json.loads((output / "failures.json").read_text(encoding="utf-8"))
    assert set(failed["failed_dates"]) == {"2024-01-02", "2024-01-03"}
    assert all("persistence failure" in value for value in failed["failed_dates"].values())


@pytest.mark.parametrize("kind", ["parquet", "json"])
def test_intermittent_daily_write_failure_retries_until_both_cache_files_exist(
    tmp_path, monkeypatch, kind
):
    dates = pd.DatetimeIndex(["2024-01-02"])
    output = tmp_path / "universe"
    provider = SyntheticSnapshotProvider()
    attempts = _inject_daily_write_failure(monkeypatch, kind, failures_before_success=2)

    first = _collect(provider, dates, output)

    assert len(attempts) == 3
    assert len(provider.calls) == 3
    parquet = output / "snapshots" / "2024-01-02.parquet"
    metadata = output / "snapshots" / "2024-01-02.json"
    assert parquet.is_file() and metadata.is_file()
    stored = pd.read_parquet(parquet)
    receipt = json.loads(metadata.read_text(encoding="utf-8"))
    assert receipt["sha256"] == pit.dataframe_sha256(stored)
    with pytest.raises(PointInTimeUniverseError, match="EFFECTIVE MEMBERSHIP SEMANTICS UNVERIFIED"):
        _load_verified_membership(_config(output), dates)
    daily = _read_published_diagnostic(output, first)
    assert len(daily) == 1
    pd.testing.assert_frame_equal(daily, stored)
    second = _collect(provider, dates, output)
    assert len(provider.calls) == 3, "completed snapshot must be reused without another query"
    assert len(attempts) == 3, "completed snapshot must not be rewritten"
    assert first["generation_id"] != second["generation_id"]
    assert first["sha256"] == second["sha256"]


@pytest.mark.parametrize("kind", ["parquet", "json"])
def test_failed_new_date_snapshot_keeps_previously_published_generation_usable(
    tmp_path, monkeypatch, kind
):
    previous_dates = pd.DatetimeIndex(["2024-01-02"])
    extended_dates = pd.DatetimeIndex(["2024-01-02", "2024-01-03"])
    output = tmp_path / "universe"
    provider = SyntheticSnapshotProvider()
    published = _collect(provider, previous_dates, output)
    pointer = (output / "manifest.json").read_bytes()
    original_data = (output / published["daily_file"]).read_bytes()
    attempts = _inject_daily_write_failure(monkeypatch, kind)

    with pytest.raises(PointInTimeUniverseError, match="1/2 daily snapshots unavailable"):
        _collect(provider, extended_dates, output)

    assert len(attempts) == 3
    assert len(provider.calls) == 4
    assert provider.calls[1:] == [("000905.SH", pd.Timestamp("2024-01-03"))] * 3
    assert (output / "manifest.json").read_bytes() == pointer
    assert (output / published["daily_file"]).read_bytes() == original_data
    with pytest.raises(PointInTimeUniverseError, match="EFFECTIVE MEMBERSHIP SEMANTICS UNVERIFIED"):
        _load_verified_membership(_config(output), previous_dates)
    daily = _read_published_diagnostic(output, published)
    assert len(daily) == 1
    assert set(daily["date"]) == set(previous_dates)
    assert set(daily["date"]) != set(extended_dates), "failed new date must not become successful coverage"
    assert daily["symbol"].eq("600000.SH").all()
    failed = json.loads((output / "failures.json").read_text(encoding="utf-8"))
    assert set(failed["failed_dates"]) == {"2024-01-03"}
    with pytest.raises(PointInTimeUniverseError, match="EFFECTIVE MEMBERSHIP SEMANTICS UNVERIFIED"):
        _load_verified_membership(_config(output), extended_dates)
