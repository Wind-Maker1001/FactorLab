"""P3-01 uses existing synthetic recipes, never claims financial admission."""

from dataclasses import replace

import pandas as pd
import pytest

from factorlab.data.corporate_actions import select_events
from factorlab.data.csmar.binding import replay
from factorlab.data.csmar.catalog import catalog_source, references
from factorlab.data.csmar.prepare import market_candidate
from factorlab.data.hub import (
    CorporateActionAdapter, CsmarAdapter, DataHub, MembershipAdapter,
    ProviderAdapter, QueryResult, SecurityDataAdapter, SourceMetadata,
    UnsupportedQueryError,
)
from factorlab.data.providers.local import LocalParquetProvider, publish_local_market_data
from factorlab.dataset.partitioned import ChunkedConfig
from factorlab.errors import DataQualityError, PointInTimeUniverseError
from factorlab.universe.csi500 import DataFramePointInTimeUniverse, dataframe_sha256
from factorlab.universe.status import import_security_data, load_verified_security_data
from tests.research.market_data_fixture import event, evidence, snapshot
from tests.research.test_csmar_local_prepare import source
from tests.research.test_dataset_registry_v2 import builder
from tests.research.test_local_market_provider import _bars, _publish, inputs  # noqa: F401


GATES = ("PIT_UNVERIFIED", "HFQ_UNVERIFIED", "FORMAL_ADMISSION_BLOCKED")
WINDOW = dict(start="2024-01-02", end="2024-01-08")


@pytest.mark.parametrize("kind,symbol,method", [
    ("daily_bars", "600000.SH", "get_daily_bars"),
    ("index_bars", "000905.SH", "get_index_bars"),
    ("calendar", None, "get_trade_calendar"),
])
def test_local_payload_and_pinned_metadata_match_old_reads(inputs, kind, symbol, method):
    manifest = _publish(inputs)
    provider = LocalParquetProvider(inputs.root)
    hub = DataHub({"local": ProviderAdapter(provider)})
    result = hub.read("local", kind, symbol=symbol, **WINDOW)
    old = getattr(provider, method)(*(([symbol] if symbol else []) + list(WINDOW.values())))
    if kind == "calendar":
        pd.testing.assert_index_equal(result.data, old, exact=True)
    else:
        pd.testing.assert_frame_equal(result.data, old, check_exact=True)
        assert "source_version" not in result.data.columns
    assert result.metadata.source_id == manifest["source"]
    assert result.metadata.data_version == provider.source_version
    assert result.metadata.provenance["manifest_sha256"] == provider.manifest_sha256
    assert result.metadata.calendar_kind == "BENCHMARK_SESSIONS"
    assert result.metadata.acquired_at is None
    assert result.metadata.provider_available_at is None
    assert result.metadata.evidence_status == GATES
    with pytest.raises(UnsupportedQueryError, match="availability UNKNOWN"):
        hub.read("local", kind, symbol=symbol, knowledge_as_of="2024-01-08", **WINDOW)
    if kind == "daily_bars":
        result.data.loc[:, "close"] = -1
        assert provider.get_daily_bars(symbol, **WINDOW).close.gt(0).all()


def csmar_source(tmp_path, monkeypatch, codes=("601857",)):
    path, _ = source(tmp_path / "raw", monkeypatch)
    catalog = dict(sources=[catalog_source(path, scratch=tmp_path, scan_families=["TRDNEW_Dalyr"])])
    refs = references(catalog, family="TRDNEW_Dalyr", codes=list(codes), start="2017-01-01", end="2018-12-31")
    return refs, path.parent


def test_csmar_existing_replay_content_status_and_observed_calendar(tmp_path, monkeypatch):
    refs, root = csmar_source(tmp_path, monkeypatch)
    old = market_candidate(replay(refs[0], source_root=root, scratch=tmp_path), "601857.SH")
    adapter = CsmarAdapter(refs, source_root=root, scratch=tmp_path)
    hub = DataHub({"csmar": adapter})
    query = dict(symbol="601857.SH", start="2017-01-01", end="2018-12-31")
    bars = hub.read("csmar", "daily_bars", **query)
    pd.testing.assert_frame_equal(bars.data, old.sort_values("date").reset_index(drop=True), check_exact=True)
    status = hub.read("csmar", "security_status", **query)
    pd.testing.assert_frame_equal(status.data, old[["date", "symbol", "trade_status", "is_st"]], check_exact=True)
    sessions = hub.read("csmar", "observed_sessions", **query)
    pd.testing.assert_index_equal(sessions.data, pd.DatetimeIndex(old.date))
    assert bars.metadata.calendar_kind == "OBSERVED_SECURITY_SESSIONS_ONLY"
    assert bars.metadata.provenance["references"] == tuple(refs)
    assert bars.metadata.evidence_status == GATES
    assert bars.metadata.acquired_at is None
    with pytest.raises(UnsupportedQueryError, match="does not support calendar"):
        hub.read("csmar", "calendar", start=query["start"], end=query["end"])
    with pytest.raises(UnsupportedQueryError, match="availability UNKNOWN"):
        hub.read("csmar", "security_status", knowledge_as_of="2018-12-31", **query)
    with pytest.raises(UnsupportedQueryError, match="no pinned"):
        hub.read("csmar", "daily_bars", **dict(query, symbol="000001.SZ"))
    conflicting = DataHub({"conflict": CsmarAdapter([refs[0], refs[0]], source_root=root, scratch=tmp_path)})
    with pytest.raises(DataQualityError, match="overlapping CSMAR"):
        conflicting.read("conflict", "daily_bars", **query)
    # An overlap outside the requested window cannot conflict with its result.
    outside = conflicting.read("conflict", "daily_bars", **dict(query, start="2018-01-09"))
    assert outside.data.empty and "PINNED_SCOPE_ONLY" in outside.metadata.coverage


def test_csmar_multisecurity_reference_preserves_pins_and_selects_aligned_rows(tmp_path, monkeypatch):
    refs, root = csmar_source(tmp_path, monkeypatch, codes=("601857", "601858"))
    hub = DataHub({"csmar": CsmarAdapter(refs, source_root=root, scratch=tmp_path)})
    query = dict(start="2017-01-01", end="2018-12-31")
    first = hub.read("csmar", "daily_bars", symbol="601857.SH", **query)
    second = hub.read("csmar", "daily_bars", symbol="601858.SH", **query)
    assert first.data.symbol.unique().tolist() == ["601857.SH"]
    assert len(first.data) == 27
    assert second.data.symbol.tolist() == ["601858.SH"]
    assert first.metadata.provenance["references"] == tuple(refs)
    assert second.metadata.provenance["references"] == tuple(refs)
    assert first.metadata.data_version == second.metadata.data_version


@pytest.mark.parametrize("kind", ["membership", "security_status", "announcements", "corporate_actions"])
def test_missing_provider_capabilities_do_not_fabricate_data(inputs, kind):
    hub = DataHub({"local": ProviderAdapter(LocalParquetProvider(inputs.root))})
    with pytest.raises(UnsupportedQueryError, match="does not support"):
        hub.read("local", kind, symbol="600000.SH", **WINDOW)
    assert not inputs.root.exists()


def test_membership_exact_dates_version_and_unknown_knowledge_time():
    dates = pd.bdate_range("2024-01-02", periods=3)
    original = pd.DataFrame({"date": dates, "symbol": "600000.SH", "is_member": True})
    universe = DataFramePointInTimeUniverse(original)
    hub = DataHub({"pit": MembershipAdapter(universe, index_symbol="000905.SH")})
    result = hub.read("pit", "membership", symbol="000905.SH", start="2024-01-02", end="2024-01-04")
    pd.testing.assert_frame_equal(result.data, universe.daily, check_exact=True)
    assert result.metadata.data_version == universe.version()
    assert result.metadata.evidence_status == GATES
    with pytest.raises(PointInTimeUniverseError, match="no snapshot"):
        hub.read("pit", "membership", symbol="000905.SH", **WINDOW)
    with pytest.raises(UnsupportedQueryError, match="availability UNKNOWN"):
        hub.read("pit", "membership", symbol="000905.SH", knowledge_as_of="2024-01-04", **WINDOW)
    with pytest.raises(UnsupportedQueryError, match="index differs"):
        hub.read("pit", "membership", symbol="000300.SH", **WINDOW)


def test_corporate_action_cutoffs_reuse_existing_selector_without_future_snapshot(tmp_path):
    artifact = evidence(tmp_path)
    early = event(artifact, announced_at="2024-01-02T10:00:00+08:00",
                  available_at="2024-01-02T11:00:00+08:00")
    later = replace(early, revision=2, effective_date="2024-02-01",
                    announced_at="2024-01-05T10:00:00+08:00", available_at="2024-01-05T11:00:00+08:00")
    value = snapshot(artifact, [early, later], issues=("SYNTHETIC_ONLY",))
    hub = DataHub({"events": CorporateActionAdapter(value)})
    query = dict(security_id="fixture-security", **WINDOW)
    before = hub.read("events", "corporate_actions", knowledge_as_of="2024-01-02T10:30:00+08:00", **query)
    assert before.data == ()
    visible = hub.read("events", "corporate_actions", knowledge_as_of=early.available_at, **query)
    assert visible.data == (early,)
    old = select_events(value, effective_through=WINDOW["end"], knowledge_as_of=early.available_at)
    assert visible.data == old.events
    assert not hasattr(visible.data, "snapshot")
    assert visible.metadata.evidence_status == GATES
    assert visible.metadata.acquired_at == value.acquired_at
    assert visible.metadata.provider_available_at is None  # Row-specific, not snapshot acquisition.
    assert visible.metadata.provenance["issues"] == value.issues
    # Existing selector preserves rescheduled known revisions; DataHub must not
    # turn a postponed event back into the old effective-date fact.
    after = hub.read("events", "corporate_actions", knowledge_as_of=later.available_at, **query)
    assert after.data == (early, later)
    altered = replace(later, cash=later.cash * 2)
    other = DataHub({"events": CorporateActionAdapter(snapshot(artifact, [early, altered]))})
    assert other.read("events", "corporate_actions", knowledge_as_of=early.available_at, **query).data == visible.data
    with pytest.raises(DataQualityError, match="security_id"):
        hub.read("events", "corporate_actions", symbol="600000.SH", **WINDOW)
    with pytest.raises(UnsupportedQueryError, match="announcements"):
        hub.read("events", "announcements", **query)


def test_missing_action_time_and_duplicate_revision_reuse_existing_rejections(tmp_path):
    artifact = evidence(tmp_path)
    known = event(artifact)
    for action, match in [(replace(known, announced_at=None), "timestamp"),
                          (replace(known, available_at=None), "timestamp")]:
        with pytest.raises(DataQualityError, match=match):
            CorporateActionAdapter(snapshot(artifact, [action]))
    with pytest.raises(DataQualityError, match="duplicate event revision"):
        CorporateActionAdapter(snapshot(artifact, [known, known]))
    empty = DataHub({"events": CorporateActionAdapter(replace(snapshot(artifact), coverage=()))})
    result = empty.read("events", "corporate_actions", security_id="fixture-security", **WINDOW)
    assert result.data == () and result.metadata.provenance["coverage"] == ()
    assert "EMPTY_IS_NOT_PROOF" in result.metadata.coverage


@pytest.mark.parametrize("query,match", [
    (dict(symbol="600000"), "canonical symbol"),
    (dict(start="2024-01-09"), "start must not exceed"),
    (dict(knowledge_as_of="2024-01-02T12:00:00"), "timezone"),
    (dict(security_id="fixture-security"), "security_id"),
])
def test_invalid_query_is_not_normalized_into_another_meaning(inputs, query, match):
    hub = DataHub({"local": ProviderAdapter(LocalParquetProvider(inputs.root))})
    with pytest.raises(DataQualityError, match=match):
        hub.read("local", "daily_bars", **(WINDOW | {"symbol": "600000.SH"} | query))


def test_duplicate_market_keys_are_rejected():
    class DuplicateReader:
        capabilities = frozenset({"daily_bars"})

        def read(self, kind, **kwargs):
            return QueryResult(pd.DataFrame({"date": ["2024-01-02"] * 2, "symbol": ["600000.SH"] * 2}),
                               SourceMetadata("SYNTHETIC_ONLY", None))

    hub = DataHub({"duplicate": DuplicateReader()})
    with pytest.raises(DataQualityError, match="duplicate result keys"):
        hub.read("duplicate", "daily_bars", symbol="600000.SH", **WINDOW)


def test_local_to_registry_dataset_and_tiny_partitioned_smoke(tmp_path):
    dates = pd.bdate_range("2024-01-02", periods=100)
    symbols = ["000001.SZ", "000002.SZ", "600000.SH", "600001.SH"]
    bars = pd.concat([_bars(s, dates, i + 1) for i, s in enumerate(symbols)], ignore_index=True)
    benchmark = _bars("000905.SH", dates)
    bars.to_parquet(tmp_path / "stocks.parquet", index=False)
    benchmark.to_parquet(tmp_path / "benchmark.parquet", index=False)
    publish_local_market_data(tmp_path / "stocks.parquet", tmp_path / "benchmark.parquet", tmp_path / "market",
                              "SYNTHETIC_ONLY", "fixture-v1")
    keys = bars[["date", "symbol"]].sort_values(["date", "symbol"]).reset_index(drop=True)
    members = keys.loc[keys.date.isin(dates[65:])].assign(is_member=True)
    universe = DataFramePointInTimeUniverse(members)
    status = keys.assign(trade_status=1, is_st=False)
    listings = pd.DataFrame({"symbol": symbols, "listing_date": dates[0]})
    status.to_parquet(tmp_path / "status.parquet", index=False)
    listings.to_parquet(tmp_path / "listings.parquet", index=False)
    universe_hash = dataframe_sha256(universe.daily)
    import_security_data(tmp_path / "status.parquet", tmp_path / "listings.parquet",
                         output_dir=tmp_path / "security", calendar=dates,
                         membership=members, universe_sha256=universe_hash)
    provider = LocalParquetProvider(tmp_path / "market")
    hub = DataHub({"market": ProviderAdapter(provider),
                   "members": MembershipAdapter(universe, index_symbol="000905.SH"),
                   "security": SecurityDataAdapter(tmp_path / "security", calendar=dates,
                                                   membership=members, universe_sha256=universe_hash)})
    window = dict(start=str(dates[0].date()), end=str(dates[-1].date()))
    new_bars = pd.concat([hub.read("market", "daily_bars", symbol=s, **window).data for s in symbols], ignore_index=True)
    old_bars = pd.concat([provider.get_daily_bars(s, **window) for s in symbols], ignore_index=True)
    new_benchmark = hub.read("market", "index_bars", symbol="000905.SH", **window).data
    new_calendar = hub.read("market", "calendar", **window).data
    new_members = hub.read("members", "membership", symbol="000905.SH",
                           start=str(dates[65].date()), end=window["end"]).data
    new_status = pd.concat([hub.read("security", "security_status", symbol=s, **window).data for s in symbols], ignore_index=True)
    new_listings = pd.concat([hub.read("security", "listings", symbol=s, **window).data for s in symbols], ignore_index=True)
    old_status, old_listings, manifest = load_verified_security_data(
        tmp_path / "security", calendar=dates, membership=members, universe_sha256=universe_hash)
    obj = builder()
    old = obj.build(old_bars, provider.get_index_bars("000905.SH", **window), universe.daily,
                    old_status, old_listings, dates)
    new = obj.build(new_bars, new_benchmark, new_members, new_status, new_listings, new_calendar)
    pd.testing.assert_frame_equal(new, old, check_exact=True)
    assert new.attrs == old.attrs
    partitioned = obj.build_chunked(new_bars, new_benchmark, new_members, new_status, new_listings,
                                    new_calendar, output_dir=tmp_path / "partitioned",
                                    config=ChunkedConfig(symbols_per_batch=2, factors_per_batch=2, workers=1))
    pd.testing.assert_frame_equal(partitioned.to_frame(), new, check_exact=True)
    result = hub.read("security", "security_status", symbol=symbols[0], **window)
    assert result.metadata.data_version == manifest["security_data_version"]
    assert result.metadata.evidence_status == GATES
    with pytest.raises(UnsupportedQueryError, match="availability UNKNOWN"):
        hub.read("security", "security_status", symbol=symbols[0], knowledge_as_of=window["end"], **window)
