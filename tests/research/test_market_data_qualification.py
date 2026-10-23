"""Replay, source binding, formal rejection, and v1 compatibility scenarios."""

from dataclasses import replace
import hashlib
import json
from pathlib import Path

import pandas as pd
import pytest
import yaml

from factorlab.data.corporate_actions.sources.akshare import parse_sina_factors
from factorlab.data.market_qualification import load_qualification, publish_qualification, qualify_market_data
from factorlab.data.providers.local import LocalParquetProvider, publish_local_market_data
from factorlab.data.units import convert_units
from factorlab.errors import DataQualityError, ProviderError
from factorlab.universe.identity import IdentitySnapshot, SecurityIdentity
from tests.research.market_data_fixture import cutoffs, qualification_inputs


def package(tmp_path, periods=8):
    inputs, keys, dates, stocks, benchmark = qualification_inputs(tmp_path, periods)
    path = publish_qualification(inputs, keys, dates, stocks=stocks, benchmark=benchmark,
                                 output_root=tmp_path / "qualification")
    return inputs, keys, dates, stocks, benchmark, path


def local_bundle(tmp_path, stocks, benchmark, qualification):
    sp, bp = tmp_path / "stocks.parquet", tmp_path / "benchmark.parquet"
    stocks.to_parquet(sp, index=False)
    benchmark.to_parquet(bp, index=False)
    result = publish_local_market_data(sp, bp, tmp_path / "bundle", "TDX-SYNTHETIC-SOFTWARE-TEST",
                                       "synthetic-v1", qualification_manifest=qualification)
    return result, sp, bp


def test_all_software_stages_never_imply_real_admission(tmp_path):
    i, k, c, s, b = qualification_inputs(tmp_path)
    report = qualify_market_data(i, k, c, stocks=s, benchmark=b)
    assert report.stages == {"RAW": "PASS", "ADJUSTMENT": "PASS", "STATUS": "PASS", "PIT": "PASS",
                             "SOURCE_BINDING": "SOFTWARE_ONLY", "FORMAL": "SOFTWARE_ONLY"}
    assert report.synthetic_only and not report.formal_admission and not report.execution_admission
    # Changing just the top-level flag cannot relabel nested evidence as real.
    report2 = qualify_market_data(replace(i, synthetic_only=False), k, c, stocks=s, benchmark=b)
    assert report2.synthetic_only and not report2.formal_admission


@pytest.mark.parametrize("change,stage", [
    ("missing_units", "ADJUSTMENT"), ("unit_unknown", "ADJUSTMENT"), ("missing_identity", "ADJUSTMENT"),
    ("status_gap", "STATUS"), ("pit_gap", "PIT"), ("turnover_gap", "ADJUSTMENT"),
    ("calendar_gap", "PIT"), ("raw_sources_missing", "RAW"), ("reference_mismatch", "ADJUSTMENT"),
    ("trading_price_gap", "STATUS"), ("suspended_vendor_fill", "STATUS"),
])
def test_incomplete_stage_is_not_promoted(tmp_path, change, stage):
    i, k, c, s, b = qualification_inputs(tmp_path)
    if change == "missing_units":
        i = replace(i, units=())
    elif change == "unit_unknown":
        i = replace(i, units=(replace(i.units[0], volume_unit="UNKNOWN"),))
    elif change == "missing_identity":
        i = replace(i, identity=replace(i.identity, intervals=()))
    elif change == "status_gap":
        i = replace(i, status=i.status.iloc[1:].copy())
    elif change == "pit_gap":
        i = replace(i, membership=i.membership.drop(index=3).reset_index(drop=True))
    elif change == "turnover_gap":
        i = replace(i, turnover=i.turnover.iloc[1:].copy())
    elif change == "calendar_gap":
        i = replace(i, calendar_reference=i.calendar_reference.iloc[1:].copy())
    elif change == "raw_sources_missing":
        i = replace(i, raw_sources=())
    elif change == "reference_mismatch":
        spec = i.adjustments["fixture-security"]
        bad = spec.reference.copy()
        bad.loc[0, "adj_open"] += 1
        from factorlab.data.evidence import table_sha256
        spec = replace(spec, reference=bad, contract=replace(spec.contract, reference_sha256=table_sha256(bad)))
        i = replace(i, adjustments={"fixture-security": spec})
    elif change == "trading_price_gap":
        s = s.iloc[1:].copy()
    elif change == "suspended_vendor_fill":
        status = i.status.copy()
        status.loc[2, "trade_status"] = 0
        i = replace(i, status=status)
    report = qualify_market_data(i, k, c, stocks=s, benchmark=b)
    assert report.stages[stage] == "INCOMPLETE"
    assert not report.formal_admission


def test_qualification_package_replays_and_reimports_idempotently(tmp_path):
    i, k, c, s, b, path = package(tmp_path)
    before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in path.parent.iterdir()}
    second = publish_qualification(i, k, c, stocks=s, benchmark=b, output_root=tmp_path / "qualification")
    assert second == path
    assert {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in path.parent.iterdir()} == before
    report, restored, keys, calendar, digest = load_qualification(path, stocks=s, benchmark=b)
    assert report.synthetic_only and not report.formal_admission
    pd.testing.assert_frame_equal(keys, k, check_exact=True)
    assert calendar.equals(c)
    assert digest == hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.parametrize("target", ["manifest", "recipe", "source", "table"])
def test_tampered_qualification_is_rejected(tmp_path, target):
    i, k, c, s, b, path = package(tmp_path)
    if target == "manifest":
        value = json.loads(path.read_text())
        value["report"]["formal_admission"] = True
        path.write_text(json.dumps(value))
    else:
        victim = path.parent / "recipe.json" if target == "recipe" else next(path.parent.glob("source-*.bin" if target == "source" else "table-*.parquet"))
        victim.write_bytes(b"tampered")
    with pytest.raises(DataQualityError, match="hash"):
        load_qualification(path, stocks=s, benchmark=b)


def test_editing_report_and_rehashing_manifest_still_cannot_forge_pass(tmp_path):
    i, k, c, s, b, path = package(tmp_path)
    from factorlab.data.evidence import json_bytes, sha256
    value = json.loads(path.read_text())
    value["report"]["formal_admission"] = True
    value["generation"] = sha256(json_bytes({key: val for key, val in value.items() if key != "generation"}))
    path.write_bytes(json_bytes(value))
    with pytest.raises(DataQualityError, match="report disagrees"):
        load_qualification(path, stocks=s, benchmark=b)


def test_implementation_change_requires_new_qualification_generation(tmp_path, monkeypatch):
    i, k, c, s, b, path = package(tmp_path)
    import factorlab.data.market_qualification as qualification_module
    monkeypatch.setattr(qualification_module, "implementation_sha256", lambda paths: "a" * 64)
    with pytest.raises(DataQualityError, match="report disagrees"):
        load_qualification(path, stocks=s, benchmark=b)


def test_v2_is_self_contained_pinned_and_source_version_changes(tmp_path):
    i, k, c, s, b, path = package(tmp_path)
    first, sp, bp = local_bundle(tmp_path, s, b, path)
    provider = LocalParquetProvider(tmp_path / "bundle")
    assert provider.source_version.startswith("local-parquet-v2;")
    original = provider.get_daily_bars("600000.SH", str(c.min().date()), str(c.max().date()))
    identity = provider.source_version
    i.raw_sources[0].read()  # Explicitly prove the original input still exists before mutation.
    Path(i.raw_sources[0].path).write_text("original source changed after publication")
    assert LocalParquetProvider(tmp_path / "bundle").qualification_report["synthetic_only"]
    with pytest.raises(DataQualityError, match="hash"):
        i.raw_sources[0].read()
    assert load_qualification(path, stocks=s, benchmark=b)[0].synthetic_only
    assert provider.source_version == identity
    pd.testing.assert_frame_equal(provider.get_daily_bars("600000.SH", str(c.min().date()), str(c.max().date())), original)
    assert first["qualification_report"]["formal_admission"] is False


def test_failed_v2_publish_keeps_previous_pointer(tmp_path, monkeypatch):
    i, k, c, s, b, path = package(tmp_path)
    first, sp, bp = local_bundle(tmp_path, s, b, path)
    pointer = tmp_path / "bundle/manifest.json"
    original = pointer.read_bytes()
    from factorlab.data.providers import local
    def fail_pointer(*args):
        raise OSError("injected publication failure")
    monkeypatch.setattr(local, "_atomic_manifest", fail_pointer)
    with pytest.raises(ProviderError, match="injected publication failure"):
        publish_local_market_data(sp, bp, tmp_path / "bundle", "TDX-SYNTHETIC-SOFTWARE-TEST", "synthetic-v1", qualification_manifest=path)
    assert pointer.read_bytes() == original
    assert LocalParquetProvider(tmp_path / "bundle").bundle_version == first["bundle_version"]


def test_changed_qualification_contract_invalidates_old_cache_even_with_same_prices(tmp_path):
    from factorlab.data.cache import ParquetBarCache
    from factorlab.errors import CacheError
    i, k, c, s, b, path = package(tmp_path)
    first, sp, bp = local_bundle(tmp_path, s, b, path)
    old_provider = LocalParquetProvider(tmp_path / "bundle")
    old_version = old_provider.cache_source_version("600000.SH", False)
    cache = ParquetBarCache(tmp_path / "cache")
    cache.update("600000.SH", s, c.min(), c.max(), source="LocalParquetProvider", source_version=old_version)
    spec = i.adjustments["fixture-security"]
    revised = replace(i, source_version="synthetic-v2", units=(replace(i.units[0], source_version="synthetic-v2"),),
                      adjustments={"fixture-security": replace(spec, contract=replace(spec.contract, source_version="golden-v2"))})
    new_package = publish_qualification(revised, k, c, stocks=s, benchmark=b, output_root=tmp_path / "qualification")
    second = publish_local_market_data(sp, bp, tmp_path / "bundle", "TDX-SYNTHETIC-SOFTWARE-TEST", "synthetic-v2", qualification_manifest=new_package)
    new_provider = LocalParquetProvider(tmp_path / "bundle")
    assert second["bundle_version"] != first["bundle_version"]
    assert old_provider.cache_source_version("600000.SH", False) == old_version
    with pytest.raises(CacheError, match="source"):
        cache.load_snapshot("600000.SH", expected_source="LocalParquetProvider",
                            expected_source_version=new_provider.cache_source_version("600000.SH", False))
    assert (tmp_path / "bundle" / first["stocks_file"]).exists()


def test_v1_tdx_declaration_cannot_promote_staging(tmp_path):
    i, k, c, s, b = qualification_inputs(tmp_path)
    sp, bp = tmp_path / "s.parquet", tmp_path / "b.parquet"
    s.to_parquet(sp, index=False)
    b.to_parquet(bp, index=False)
    with pytest.raises(ProviderError, match="v2 qualification"):
        publish_local_market_data(sp, bp, tmp_path / "bundle", "TDX-RAW", "synthetic-v1")
    assert not (tmp_path / "bundle").exists()


def test_synthetic_v2_is_rejected_by_formal_market_input_loader(tmp_path, monkeypatch):
    from factorlab import cli
    from factorlab.config import load_config
    i, k, c, s, b, path = package(tmp_path, periods=85)
    local_bundle(tmp_path, s, b, path)
    document = yaml.safe_load((Path(__file__).resolve().parents[2] / "configs/research/csi500_local_smoke.yaml").read_text(encoding="utf-8"))
    document["project"]["name"] = "SYNTHETIC_SOFTWARE_ONLY_V2_REJECTION"
    document["paths"] = {"data_root": str(tmp_path / "cache"), "results_root": str(tmp_path / "results"), "market_input_root": str(tmp_path / "bundle")}
    document["data"].update(start=str(c[-20].date()), end=str(c[-1].date()), warmup_trading_days=65)
    document["universe"].update(expected_member_count=1, min_listed_days=60)
    cfg = tmp_path / "synthetic.yaml"
    cfg.write_text(yaml.safe_dump(document))
    config = load_config(cfg)
    for symbol, frame, cache, is_index in [("000905.SH", b, cli._benchmark_cache(config), True), ("600000.SH", s, cli._stock_cache(config), False)]:
        provider = cli._provider(config)
        cache.update(symbol, frame, c.min(), c.max(), source=provider.__class__.__name__, source_version=provider.cache_source_version(symbol, is_index))
    with pytest.raises(ProviderError, match="SOFTWARE_ONLY"):
        cli._load_bar_inputs(config, i.membership.loc[i.membership.date.isin(c[-20:])])


def test_identity_change_is_explicit_and_different_entity_not_concatenated(tmp_path):
    i, k, c, s, b = qualification_inputs(tmp_path)
    old = i.identity.intervals[0]
    first = replace(old, end=str(c[3].date()))
    second = replace(old, symbol="000001.SZ", start=str(c[4].date()))
    identities = replace(i.identity, intervals=(first, second))
    keys = k.copy()
    keys.loc[keys.date.ge(c[4]), "symbol"] = "000001.SZ"
    resolved = identities.resolve(keys, cutoffs(c))
    assert resolved.security_id.nunique() == 1
    with pytest.raises(DataQualityError, match="separate identity contract"):
        replace(identities, intervals=(first, replace(second, relationship="MERGER"))).resolve(keys, cutoffs(c))
    with pytest.raises(DataQualityError, match="delisting"):
        replace(i.identity, intervals=(replace(old, delisting_date=str(c[3].date())),)).validate()
    with pytest.raises(DataQualityError, match="gap"):
        replace(identities, intervals=(first,)).resolve(keys, cutoffs(c))
    with pytest.raises(DataQualityError, match="A_SHARE"):
        replace(i.identity, intervals=(replace(old, security_type="UNKNOWN"),)).validate()


def test_unknown_or_wrong_unit_scope_does_not_use_hardcoded_tdx_multiplier(tmp_path):
    i, k, c, s, b = qualification_inputs(tmp_path)
    for units, version in [((replace(i.units[0], volume_unit="UNKNOWN"),), "synthetic-v1"), (i.units, "different-source")]:
        with pytest.raises(DataQualityError):
            convert_units(i.raw, units, source_version=version, signal_as_of=i.signal_as_of)


def test_sentinel_cannot_be_promoted_to_a_historical_security_anchor(tmp_path):
    i, k, c, s, b = qualification_inputs(tmp_path)
    spec = i.adjustments["fixture-security"]
    bad = replace(spec, anchor=replace(spec.anchor, date="1900-01-01"))
    report = qualify_market_data(replace(i, adjustments={"fixture-security": bad}), k, c, stocks=s, benchmark=b)
    assert report.stages["ADJUSTMENT"] == "INCOMPLETE"
    assert any("sentinel" in issue for issue in report.issues)


@pytest.mark.parametrize("bad", [
    b'var x={"total":1,"data":[{"d":"2024-01-04","f":"2"}]};alert(1)',
    b'var x=__import__("os").system("fake")',
    b'var x={"total":1,"data":[{"d":"2024-01-04","f":"NaN"}]}',
    b'var x={"total":1,"data":[{"d":"2024-01-04","f":"-2"}]}',
])
def test_factor_payload_is_never_executed_or_coerced(bad):
    with pytest.raises(DataQualityError):
        parse_sina_factors(bad)


def test_factor_sentinel_is_retained_as_observation_only():
    rows = parse_sina_factors(b'var hfq={"total":2,"data":[{"d":"2024-01-04","f":"2"},{"d":"1900-01-01","f":"1"}]};/* observed source footer */')
    assert rows[-1]["kind"] == "SENTINEL"
    assert rows[0]["kind"] == "UNVERIFIED_FACTOR_OBSERVATION"
