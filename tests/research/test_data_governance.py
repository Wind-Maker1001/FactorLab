"""Small, reproducible engineering vintages, not financial certifications."""

from dataclasses import replace

import pandas as pd
import pytest

from factorlab.data.evidence import snapshot_file
from factorlab.data.governance import GovernanceStore
from factorlab.data.governed_adapter import GovernedCorporateActionAdapter, GovernedTableAdapter
from factorlab.data.hub import DataHub, ProviderAdapter, CsmarAdapter, UnsupportedQueryError
from factorlab.data.providers.local import LocalParquetProvider
from factorlab.errors import DataQualityError
from tests.research.test_local_market_provider import inputs, _publish  # noqa: F401
from tests.research.test_datahub import csmar_source
from tests.research.market_data_fixture import evidence, event, snapshot


QUERY = dict(symbol="600000.SH", start="2024-01-02", end="2024-01-08")
ACQUIRED = "2026-11-08T12:00:00Z"


def local_capture(inputs, store, version, predecessor=None, source="local"):
    manifest = _publish(inputs)
    hub = DataHub({source: ProviderAdapter(LocalParquetProvider(inputs.root))})
    artifacts = tuple(snapshot_file(path, manifest["source"], ACQUIRED) for path in (
        inputs.root / "manifest.json", inputs.root / manifest["stocks_file"]))
    result = store.capture(hub, source, "daily_bars", version=version,
                           artifacts=artifacts, acquired_at=ACQUIRED,
                           predecessor=predecessor, **QUERY)
    return hub, artifacts, result


def test_local_archive_reopen_preserves_payload_and_gates(inputs, tmp_path):
    store = GovernanceStore(tmp_path / "archive")
    hub, _, result = local_capture(inputs, store, "v1")
    old = hub.read("local", "daily_bars", **QUERY)
    pd.testing.assert_frame_equal(result.data, old.data, check_exact=True)
    reopened = GovernanceStore(store.root)
    governed = DataHub({"vintage": GovernedTableAdapter(reopened, source="local",
                                                     kind="daily_bars", version="v1")})
    selected = governed.read("vintage", "daily_bars", **QUERY)
    pd.testing.assert_frame_equal(selected.data, old.data, check_exact=True)
    assert selected.metadata.evidence_status == old.metadata.evidence_status
    assert selected.metadata.provider_available_at is None
    assert selected.metadata.provenance["historical_availability"] == "UNKNOWN"
    with pytest.raises(UnsupportedQueryError, match="availability UNKNOWN"):
        governed.read("vintage", "daily_bars", knowledge_as_of="2024-01-08", **QUERY)
    # Archival is self-contained; deleting the captured source is unnecessary.
    assert len(selected.metadata.provenance["original_artifacts"]) == 2


def test_revisions_conflicts_and_explicit_selection(inputs, tmp_path):
    store = GovernanceStore(tmp_path / "archive")
    _, _, original = local_capture(inputs, store, "v1")
    inputs.stocks.loc[inputs.stocks.symbol.eq("600000.SH"), "volume"] = 2000.0
    inputs.stocks.to_csv(inputs.stock_path, index=False)
    local_capture(inputs, store, "v2", predecessor="v1")
    selected = store.read("local", "daily_bars", version="v1", **QUERY)
    pd.testing.assert_frame_equal(selected.data, original.data)
    assert selected.data.volume.eq(1000).all()
    latest = store.read("local", "daily_bars", version="v2", **QUERY)
    assert latest.data.volume.eq(2000).all()
    assert latest.metadata.provenance["predecessor"] == "v1"
    conflicts = store.diagnose("local", "v1", "local", "v2", "daily_bars", **QUERY)
    assert len(conflicts) == 5
    assert all(c["field"] == "volume" and c["type"] == "VALUE" for c in conflicts)
    local_capture(inputs, store, "other-v1", source="other-source")
    cross = store.diagnose("local", "v1", "other-source", "other-v1", "daily_bars", **QUERY)
    assert len(cross) == 5
    assert cross[0]["left"]["source"] == "local"
    assert cross[0]["right"]["source"] == "other-source"
    assert cross[0]["right"]["evidence"]


def test_overwrite_and_unbound_originals_rejected(inputs, tmp_path):
    store = GovernanceStore(tmp_path / "archive")
    hub, artifacts, _ = local_capture(inputs, store, "v1")
    with pytest.raises(DataQualityError, match="cannot be overwritten"):
        store.capture(hub, "local", "daily_bars", version="v1", artifacts=artifacts,
                      acquired_at=ACQUIRED, **QUERY)
    with pytest.raises(DataQualityError, match="exactly match"):
        store.capture(hub, "local", "daily_bars", version="bad", artifacts=artifacts[:1],
                      acquired_at=ACQUIRED, **QUERY)
    with pytest.raises(FileNotFoundError):
        store.capture(hub, "local", "daily_bars", version="orphan", artifacts=artifacts,
                      predecessor="missing", acquired_at=ACQUIRED, **QUERY)


@pytest.mark.parametrize("target", ["tables", "artifacts"])
def test_corruption_is_not_silently_read(inputs, tmp_path, target):
    store = GovernanceStore(tmp_path / "archive")
    local_capture(inputs, store, "v1")
    path = next((store.root / target).glob("*.bin"))
    path.write_bytes(b"corrupt fixture")
    with pytest.raises(DataQualityError, match="hash mismatch"):
        store.read("local", "daily_bars", version="v1", **QUERY)


def test_csmar_status_versions_and_unknown_times(tmp_path, monkeypatch):
    refs, root = csmar_source(tmp_path, monkeypatch)
    hub = DataHub({"csmar": CsmarAdapter(refs, source_root=root, scratch=tmp_path)})
    artifacts = tuple(snapshot_file(root / ref["source_name"], "CSMAR", ACQUIRED) for ref in refs)
    store = GovernanceStore(tmp_path / "archive")
    query = dict(symbol="601857.SH", start="2017-01-01", end="2018-12-31")
    selected = store.capture(hub, "csmar", "security_status", version="v1", artifacts=artifacts,
                             acquired_at=ACQUIRED, **query)
    pd.testing.assert_frame_equal(selected.data, hub.read("csmar", "security_status", **query).data)
    with pytest.raises(UnsupportedQueryError, match="availability UNKNOWN"):
        store.read("csmar", "security_status", version="v1", knowledge_as_of="2018-12-31", **query)


def test_status_missing_and_equal_fields_are_distinguished(tmp_path, monkeypatch):
    from factorlab.data.hub import QueryResult, SourceMetadata
    store = GovernanceStore(tmp_path / "archive")
    left = pd.DataFrame({"date": pd.to_datetime(["2024-01-02", "2024-01-03"]),
                         "symbol": ["600000.SH"] * 2, "is_st": [False, False],
                         "trade_status": [1, 1]})
    right = left.iloc[:1].copy()
    right["is_st"] = True
    results = {"left": left, "right": right}

    def read(source, kind, **query):
        return QueryResult(results[source], SourceMetadata(source, "v1", provenance={
            "original_artifacts": [{"sha256": source + "-synthetic-evidence"}]}))

    monkeypatch.setattr(store, "read", read)
    conflicts = store.diagnose("left", "v1", "right", "v1", "security_status", **QUERY)
    assert [(c["field"], c["type"]) for c in conflicts] == [
        ("is_st", "STATUS"), ("is_st", "MISSING"), ("trade_status", "MISSING")]


def test_archived_company_revisions_use_actual_knowledge_cutoff(tmp_path):
    artifact = evidence(tmp_path)
    first = event(artifact, status="PROPOSED")
    future = replace(first, revision=2, announced_at="2024-02-01", available_at="2024-02-02")
    adapter = GovernedCorporateActionAdapter.capture(snapshot(artifact, [first, future]), tmp_path / "events")
    restored = GovernedCorporateActionAdapter(tmp_path / "events", generation=adapter.generation)
    hub = DataHub({"events": restored})
    query = dict(security_id="fixture-security", start="2024-01-01", end="2024-01-31")
    before = hub.read("events", "corporate_actions", knowledge_as_of="2024-02-01", **query)
    assert before.data == (first,)
    after = hub.read("events", "corporate_actions", knowledge_as_of="2024-02-02", **query)
    assert after.data == (first, future)
    assert all(e.status == "PROPOSED" for e in after.data)
    assert "FORMAL_ADMISSION_BLOCKED" in after.metadata.evidence_status
    assert before.metadata.provenance["synthetic_only"] is True
