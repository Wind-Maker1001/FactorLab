"""BaoStock query dates never substitute for effective PIT membership proof."""

import json
from pathlib import Path

import pandas as pd
import pytest

from factorlab.errors import PointInTimeUniverseError
from factorlab.universe import csi500


QUERY_DATE = pd.Timestamp("2024-01-02")


class SyntheticQuery:
    """An explicit artificial source, never evidence of actual CSI500 members."""

    error_code = "0"
    error_msg = ""

    def __init__(self, update_dates=("2024-01-02",), include_update_date=True):
        self.date = QUERY_DATE.strftime("%Y-%m-%d")
        self.fields = ["code", "code_name"]
        if include_update_date:
            self.fields.insert(0, "updateDate")
        self.rows = []
        for number in range(500):
            row = ["sh.%06d" % (600000 + number), "synthetic"]
            if include_update_date:
                row.insert(0, update_dates[number % len(update_dates)])
            self.rows.append(row)
        self.position = -1

    def next(self):
        self.position += 1
        return self.position < len(self.rows)

    def get_row_data(self):
        return self.rows[self.position]


class SyntheticClient:
    def __init__(self, update_dates=("2024-01-02",), include_update_date=True):
        self.update_dates = update_dates
        self.include_update_date = include_update_date
        self.calls = []

    def query_zz500_stocks(self, date):
        self.calls.append(date)
        assert date == QUERY_DATE.strftime("%Y-%m-%d")
        return SyntheticQuery(self.update_dates, self.include_update_date)


class SubclassedBaoStock(csi500.BaoStockPointInTimeProvider):
    pass


def _provider(provider_class=csi500.BaoStockPointInTimeProvider, client=None):
    return provider_class(
        client=client or SyntheticClient(), retry_count=0, request_interval=0.0
    )


def _existing_cache(output, provider):
    """Materialise a legacy synthetic v2 cache to exercise cached bypasses."""

    output.mkdir()
    snapshots = output / "snapshots"
    snapshots.mkdir()
    frame = pd.DataFrame(
        {
            "date": QUERY_DATE,
            "symbol": ["%06d.SH" % (600000 + number) for number in range(500)],
            "is_member": True,
        }
    )
    frame.to_parquet(snapshots / "2024-01-02.parquet", index=False)
    metadata = csi500._snapshot_metadata(provider, "000905.SH", QUERY_DATE, 500, frame)
    (snapshots / "2024-01-02.json").write_text(json.dumps(metadata), encoding="utf-8")
    frame.to_parquet(output / "csi500_daily.parquet", index=False)
    csi500.compress_membership_intervals(frame).to_parquet(
        output / "csi500_intervals.parquet", index=False
    )
    manifest = {
        **metadata,
        "start": "2024-01-02",
        "end": "2024-01-02",
        "trading_dates": 1,
    }
    (output / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


@pytest.mark.parametrize("provider_class", [csi500.BaoStockPointInTimeProvider, SubclassedBaoStock])
@pytest.mark.parametrize("existing_cache", [False, True], ids=["no-cache", "v2-cache"])
@pytest.mark.parametrize("claimed_verified", [False, True], ids=["unverified", "fake-verified"])
def test_formal_collection_rejects_baostock_before_any_io(
    tmp_path, monkeypatch, provider_class, existing_cache, claimed_verified
):
    client = SyntheticClient()
    provider = _provider(provider_class, client)
    if claimed_verified:
        # Neither arbitrary flags nor a user-assigned contract can prove source
        # effective dates. In particular subclasses must not evade the guard.
        provider.verified = True
        provider.effective_membership_verified = True
        provider.membership_semantics = "exact-trading-date-snapshot"
    output = tmp_path / "pit"
    if existing_cache:
        _existing_cache(output, provider)
    before = {path.relative_to(tmp_path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}

    def forbidden(*args, **kwargs):
        raise AssertionError("Unverified BaoStock must fail before queries/cache/filesystem publication")

    monkeypatch.setattr(Path, "mkdir", forbidden)
    monkeypatch.setattr(csi500, "_load_cached_snapshot", forbidden)
    monkeypatch.setattr(csi500, "_publish_pit_generation", forbidden)
    monkeypatch.setattr(provider, "get_constituents", forbidden)

    with pytest.raises(PointInTimeUniverseError) as raised:
        csi500.collect_point_in_time_universe(
            provider, "000905.SH", [QUERY_DATE], output,
            expected_member_count=500, retries=0, request_interval_seconds=0,
        )
    assert str(raised.value) == csi500.BAOSTOCK_PIT_UNVERIFIED_REASON
    assert client.calls == []
    assert {path.relative_to(tmp_path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()} == before
    if not existing_cache:
        assert not output.exists()


@pytest.mark.parametrize("update_date", ["2023-12-25", "2024-01-02", "2026-10-09"])
def test_matching_query_date_and_500_members_do_not_establish_effective_dates(tmp_path, update_date):
    client = SyntheticClient(update_dates=(update_date,))
    provider = _provider(client=client)
    diagnostic = provider.get_constituents("000905.SH", QUERY_DATE)
    assert len(diagnostic) == diagnostic["symbol"].nunique() == 500
    assert diagnostic["date"].eq(QUERY_DATE).all()
    assert diagnostic.attrs["baostock_evidence"] == {
        "response_query_date": "2024-01-02",
        "reported_update_dates": (update_date,),
        "membership_semantics": "unverified-query-date-snapshot",
    }
    assert list(diagnostic.columns) == ["date", "symbol", "is_member"]
    with pytest.raises(PointInTimeUniverseError) as raised:
        csi500.collect_point_in_time_universe(
            provider, "000905.SH", [QUERY_DATE], tmp_path / "pit",
            expected_member_count=500, retries=0, request_interval_seconds=0,
        )
    assert str(raised.value) == csi500.BAOSTOCK_PIT_UNVERIFIED_REASON
    assert client.calls == ["2024-01-02"]  # No additional publication-stage query.
    assert not (tmp_path / "pit").exists()


def test_diagnostic_retains_multiple_reported_update_dates_without_reinterpreting_them():
    provider = _provider(client=SyntheticClient(update_dates=("2024-01-01", "2023-12-25")))
    result = provider.get_constituents("000905.SH", QUERY_DATE)
    assert result.attrs["baostock_evidence"]["reported_update_dates"] == (
        "2023-12-25", "2024-01-01"
    )
    assert result["date"].eq(QUERY_DATE).all()


def test_missing_update_date_is_explicitly_unavailable_not_inferred_from_query_date():
    provider = _provider(client=SyntheticClient(include_update_date=False))
    result = provider.get_constituents("000905.SH", QUERY_DATE)
    assert result.attrs["baostock_evidence"]["reported_update_dates"] is None
    assert result.attrs["baostock_evidence"]["membership_semantics"] == "unverified-query-date-snapshot"


def test_baostock_gate_runs_before_calendar_consumption(tmp_path):
    def forbidden_calendar():
        raise AssertionError("Unverified provider must be rejected before reading the calendar")
        yield QUERY_DATE

    with pytest.raises(PointInTimeUniverseError) as raised:
        csi500.collect_point_in_time_universe(
            _provider(), "000905.SH", forbidden_calendar(), tmp_path / "pit"
        )
    assert str(raised.value) == csi500.BAOSTOCK_PIT_UNVERIFIED_REASON
    assert not (tmp_path / "pit").exists()
