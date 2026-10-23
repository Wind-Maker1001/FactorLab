"""Actual financial gate counterexamples; all inputs remain synthetic only."""

from dataclasses import replace
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd
import pytest

from factorlab import cli
from factorlab.data.admission import _same_rows, assert_formal_market_admission
from factorlab.data.market_qualification import publish_qualification
from factorlab.data.providers.base import MarketQualificationEvidence
from factorlab.data.providers.local import LocalParquetProvider, publish_local_market_data
from factorlab.errors import FactorLabError, ProviderError
from factorlab.exploratory.contract import assert_exploratory_only, REQUIRED_MARKERS
from tests.research.market_data_fixture import qualification_inputs
from tests.research.test_cli_phase2 import _config, _inputs, _Run, _write_verified_inputs


@pytest.fixture(scope="module")
def package(tmp_path_factory):
    root = tmp_path_factory.mktemp("unqualified-evidence")
    inputs, keys, dates, stocks, benchmark = qualification_inputs(root)
    manifest = publish_qualification(
        inputs, keys, dates, stocks=stocks, benchmark=benchmark,
        output_root=root / "qualification",
    )
    evidence = MarketQualificationEvidence(
        manifest, hashlib.sha256(manifest.read_bytes()).hexdigest(),
        inputs.source_version, stocks, benchmark,
    )
    return inputs, keys, dates, stocks, benchmark, evidence


def request(package):
    inputs, _, dates, stocks, benchmark, _ = package
    return dict(membership=inputs.membership, calendar=dates, bars=stocks,
                benchmark=benchmark, research_start=dates[0], research_end=dates[-1])


@pytest.mark.parametrize("fake", [None, True, {}, {"formal_admission": True}, SimpleNamespace(formal_admission=True)])
def test_provider_flags_and_empty_packages_never_grant_admission(package, fake):
    provider = SimpleNamespace(get_research_qualification=lambda: fake)
    with pytest.raises(ProviderError, match="QUALIFICATION REQUIRED"):
        assert_formal_market_admission(provider, **request(package))


def test_unknown_nonlocal_provider_fails_closed(package):
    with pytest.raises(ProviderError, match="no qualification package"):
        assert_formal_market_admission(SimpleNamespace(verified=True), **request(package))


def test_provider_name_and_old_approval_method_are_not_an_authority(package):
    impostor = type("LocalParquetProvider", (), {"formal_admission": True,
                    "assert_research_admission": lambda *a: True})()
    with pytest.raises(ProviderError, match="no qualification package"):
        assert_formal_market_admission(impostor, **request(package))


@pytest.mark.parametrize("change", ["pin", "source", "stocks", "benchmark", "missing", "empty"])
def test_incomplete_or_identity_mismatched_nonlocal_evidence_is_rejected(package, tmp_path, change):
    evidence = package[-1]
    if change == "pin":
        evidence = replace(evidence, manifest_sha256="0" * 64)
    elif change == "source":
        evidence = replace(evidence, source_version="unrelated-source-v1")
    elif change == "stocks":
        evidence = replace(evidence, stocks=evidence.stocks.assign(amount=evidence.stocks.amount * 2))
    elif change == "benchmark":
        evidence = replace(evidence, benchmark=evidence.benchmark.assign(adj_open=1.0))
    else:
        target = tmp_path / "manifest.json"
        if change == "empty":
            target.write_text("{}", encoding="utf-8")
        evidence = replace(evidence, manifest=target,
                           manifest_sha256=hashlib.sha256(b"{}").hexdigest())
    provider = SimpleNamespace(get_research_qualification=lambda: evidence)
    with pytest.raises(ProviderError, match="QUALIFICATION REQUIRED"):
        assert_formal_market_admission(provider, **request(package))


@pytest.mark.parametrize("execution", [False, True])
def test_complete_synthetic_recipe_is_never_a_financial_positive_control(package, execution):
    provider = SimpleNamespace(get_research_qualification=lambda: package[-1])
    with pytest.raises(ProviderError, match="SOFTWARE_ONLY"):
        assert_formal_market_admission(provider, require_execution=execution, **request(package))


@pytest.mark.parametrize("change", ["calendar_gap", "duplicate_day", "range", "pit", "stock", "benchmark", "status", "listing", "status_alone", "symbol"])
def test_actual_gate_binds_requested_scope_before_rejecting_synthetic_qualification(package, change):
    values = request(package)
    inputs = package[0]
    if change == "calendar_gap":
        values["calendar"] = values["calendar"].append(pd.DatetimeIndex([values["calendar"][-1] + pd.offsets.BDay()]))
    elif change == "duplicate_day":
        values["calendar"] = values["calendar"].append(values["calendar"][-1:])
    elif change == "range":
        values["research_start"] = values["research_start"] - pd.Timedelta(days=1)
    elif change == "pit":
        values["membership"] = values["membership"].assign(is_member=False)
    elif change == "stock":
        values["bars"] = values["bars"].assign(adj_close=1.0)
    elif change == "benchmark":
        values["benchmark"] = values["benchmark"].assign(adj_close=1.0)
    elif change == "symbol":
        values["required_symbols"] = ["000001.SZ"]
    else:
        values.update(status=inputs.status, listings=inputs.listings)
        if change == "status":
            values["status"] = inputs.status.assign(is_st=True)
        elif change == "listing":
            values["listings"] = inputs.listings.assign(listing_date=pd.Timestamp("2000-01-01"))
        else:
            values["listings"] = None
    provider = SimpleNamespace(get_research_qualification=lambda: package[-1])
    with pytest.raises(ProviderError) as failure:
        assert_formal_market_admission(provider, **values)
    assert "SOFTWARE_ONLY" not in str(failure.value)


def test_resealed_pass_flag_cannot_override_financial_replay(package, tmp_path):
    import shutil
    from factorlab.data.evidence import json_bytes, sha256
    target = tmp_path / "copy"
    shutil.copytree(package[-1].manifest.parent, target)
    manifest = target / "manifest.json"
    value = json.loads(manifest.read_bytes())
    value["report"].update(formal_admission=True, execution_admission=True, synthetic_only=False)
    value["generation"] = sha256(json_bytes({k: v for k, v in value.items() if k != "generation"}))
    manifest.write_bytes(json_bytes(value))
    evidence = replace(package[-1], manifest=manifest,
                       manifest_sha256=hashlib.sha256(manifest.read_bytes()).hexdigest())
    with pytest.raises(ProviderError, match="report disagrees"):
        assert_formal_market_admission(SimpleNamespace(get_research_qualification=lambda: evidence), **request(package))


def test_unqualified_local_engineering_reads_remain_usable(package, tmp_path):
    _, _, dates, stocks, benchmark, _ = package
    stock_path, benchmark_path = tmp_path / "stocks.parquet", tmp_path / "benchmark.parquet"
    stocks.to_parquet(stock_path, index=False)
    benchmark.to_parquet(benchmark_path, index=False)
    publish_local_market_data(stock_path, benchmark_path, tmp_path / "bundle", "SYNTHETIC_ONLY", "synthetic-v1")
    provider = LocalParquetProvider(tmp_path / "bundle")
    pd.testing.assert_frame_equal(provider.get_daily_bars("600000.SH", dates[0], dates[-1]), stocks)
    with pytest.raises(ProviderError, match="v2 qualification"):
        provider.assert_research_admission(package[0].membership, dates, stocks)
    with pytest.raises(ProviderError, match="v2 qualification"):
        provider.assert_execution_admission()


def test_explicit_exploratory_contract_remains_nonfinancial():
    metadata = dict(purpose="EXPLORATORY", qualification="UNVERIFIED",
                    formal_admission=False, markers=list(REQUIRED_MARKERS))
    assert_exploratory_only(metadata, purpose="EXPLORATORY")
    assert metadata["formal_admission"] is False
    with pytest.raises(ValueError):
        assert_exploratory_only(metadata, purpose="FORMAL")


@pytest.mark.parametrize("kind", ["price", "benchmark", "status", "listing"])
@pytest.mark.parametrize("change", ["value", "missing", "duplicate"])
def test_exact_request_binding_rejects_changed_or_missing_rows_without_faking_qualification(package, kind, change):
    inputs, keys, _, stocks, benchmark, _ = package
    frames = {"price": stocks, "benchmark": benchmark, "status": inputs.status, "listing": inputs.listings}
    expected = frames[kind]
    columns = ["symbol"] if kind == "listing" else ["date", "symbol"]
    index = pd.MultiIndex.from_frame(expected[columns])
    actual = expected.copy(deep=True)
    if change == "value":
        field = {"price": "adj_close", "benchmark": "adj_open", "status": "trade_status", "listing": "listing_date"}[kind]
        actual.loc[0, field] = actual.loc[0, field] + (pd.Timedelta(days=1) if kind == "listing" else 1)
    elif change == "missing":
        actual = actual.iloc[1:]
    else:
        actual = pd.concat([actual, actual.iloc[:1]], ignore_index=True)
    with pytest.raises(ProviderError):
        _same_rows(actual, expected, columns, index, kind)
    _same_rows(expected, expected, columns, index, kind)


@pytest.fixture(scope="module")
def cached(tmp_path_factory):
    root = tmp_path_factory.mktemp("original-p0-cache")
    config = _config(root)
    _, dataset, bars, benchmark = _inputs()
    # Prepare the original unqualified cache, not a financial certificate.
    with patch.object(cli, "assert_formal_market_admission", return_value={"formal_admission": False}):
        _, membership, _ = _write_verified_inputs(config, dataset, bars=bars, benchmark=benchmark)
    return config, membership


def test_original_3900_row_probe_is_rejected(cached):
    config, membership = cached
    assert len(pd.read_parquet(config.paths.processed_dir / "dataset.parquet")) == 3900
    with pytest.raises(ProviderError, match="no qualification package"):
        cli._load_bar_inputs(config, membership)


@pytest.mark.parametrize("pinned", [False, True])
def test_dataset_consumption_cannot_trust_supplied_bar_snapshots(cached, pinned):
    config, membership = cached
    benchmark = cli._load_benchmark_with_warmup(config)
    calendar = cli.TradingCalendar.from_benchmark_bars(benchmark).dates
    supplied = None
    if pinned:
        with patch.object(cli, "assert_formal_market_admission", return_value={"formal_admission": False}):
            supplied = cli._load_bar_inputs(config, membership)
    with pytest.raises(ProviderError, match="no qualification package"):
        cli._load_verified_dataset(config, cli._research_calendar(config, calendar),
                                   full_calendar=calendar, bar_inputs=supplied)


@pytest.mark.parametrize("command", ["build", "evaluate", "factor_score", "ridge", "lightgbm", "xgboost"])
def test_rejected_formal_commands_do_not_publish_successful_artifacts(cached, tmp_path, command):
    import shutil
    config, _ = cached
    data_root = tmp_path / "data"
    shutil.copytree(config.paths.data_root, data_root,
                    ignore=shutil.ignore_patterns("processed", "features"))
    config = replace(config, paths=replace(config.paths, data_root=data_root,
                                          results_root=tmp_path / "results"))
    run = _Run("rejected-" + command)
    operation = {"build": cli.command_build_dataset, "evaluate": cli.command_evaluate}.get(command)
    with pytest.raises(ProviderError, match="no qualification package"):
        operation(config, run) if operation else cli.command_walk_forward(config, run, command)
    assert run.outputs == {}
    assert not config.paths.results_root.exists()
    assert not config.paths.processed_dir.exists()
    assert not config.paths.features_dir.exists()


def test_cli_failure_manifest_is_failed_and_has_no_success_outputs(cached, tmp_path, monkeypatch):
    config, _ = cached
    config = replace(config, paths=replace(config.paths, results_root=tmp_path / "results"))
    monkeypatch.setattr(cli, "load_config", lambda path: config)
    assert cli.main(["walk-forward", "--config", "synthetic.yaml", "--model", "ridge"]) != 0
    manifests = list(config.paths.runs_dir.glob("*/manifest.json"))
    assert len(manifests) == 1
    payload = json.loads(manifests[0].read_bytes())
    assert payload["status"] == "failed" and payload["outputs"] == {}
    assert "QUALIFICATION REQUIRED" in payload["error"]


@pytest.mark.parametrize("operation", ["freeze", "signal"])
def test_forward_consumers_reject_before_fitting_or_predicting(cached, tmp_path, monkeypatch, operation):
    from datetime import datetime, timedelta, timezone
    config, _ = cached
    forward = replace(config.forward, enabled=True, model_version="SYNTHETIC_TEST_ONLY",
                      training_asof=config.data.end,
                      activation_date=config.data.end + timedelta(days=2))
    config = replace(config, forward=forward,
                     paths=replace(config.paths, results_root=tmp_path / "results"))
    def forbidden(*args, **kwargs):
        pytest.fail("Unqualified inputs reached fitting, prediction or publication")
    activation = SimpleNamespace(activation_date=config.data.start)
    monkeypatch.setattr(cli, "_registered_forward_context", lambda *a: ({}, "a" * 64, activation, "b" * 64))
    monkeypatch.setattr(cli, "_forward_now", lambda: datetime(2021, 6, 30, 8, tzinfo=timezone.utc))
    monkeypatch.setattr(cli, "fit_final_model", forbidden)
    monkeypatch.setattr(cli, "save_model_bundle", forbidden)
    monkeypatch.setattr(cli, "_load_registered_forward_bundle", lambda *a: SimpleNamespace(predict=forbidden))
    run = _Run("unqualified-forward-" + operation)
    with pytest.raises(ProviderError, match="no qualification package"):
        if operation == "freeze":
            cli.command_freeze_forward_model(config, run)
        else:
            cli.command_forward_signal(config, run, config.data.end, refresh_data=False,
                                       generated_at=datetime(2021, 6, 30, 8, tzinfo=timezone.utc))
    assert run.outputs == {}
    assert not config.paths.forward_models_dir.exists()
    assert not (config.paths.forward_dir / "signals").exists()


def test_forward_realised_price_evaluation_cannot_skip_qualification(cached):
    config, membership = cached
    with patch.object(cli, "assert_formal_market_admission", return_value={"formal_admission": False}):
        inputs = cli._load_bar_inputs(config, membership)
    calendar = cli.TradingCalendar.from_benchmark_bars(inputs[0]).dates
    with pytest.raises(ProviderError, match="no qualification package"):
        cli._admit_forward_evaluation(config, inputs.provider, inputs[0], inputs[1], calendar,
                                      config.data.start, config.data.end, sorted(membership.symbol.unique()))


def test_default_five_year_protocol_keeps_pit_blocked():
    from factorlab.config import load_config
    config = load_config(Path(__file__).resolve().parents[2] / "configs/research/csi500_5y.yaml")
    dates = pd.bdate_range(config.data.start, periods=3)
    with pytest.raises(FactorLabError, match="POINT-IN-TIME UNIVERSE REQUIRED"):
        cli._load_verified_membership(config, dates)
