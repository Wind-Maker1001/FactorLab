"""Bind synthetic stage diagnostics to producer inputs and verified bytes.

These fixtures prove software invariants, not actual CSI500 membership,
factor efficacy, or model superiority.
"""

from dataclasses import replace
from datetime import date
import io
import json
from pathlib import Path
import shutil
import warnings

import numpy as np
import pandas as pd
import pytest

from qbot.research import cli
from qbot.research.backtest.fast import FastBacktestResult
from qbot.research.backtest.execution import ExecutionBacktestResult
from qbot.research.config import (
    DataConfig, EvaluationConfig, PathsConfig, PortfolioConfig,
    ProjectConfig, ResearchConfig, UniverseConfig, ValidationConfig,
)
from qbot.research.errors import FactorLabError
from qbot.research.metrics import evaluate_predictions
from qbot.research.reports import write_model_report
from qbot.research.validation import ExpandingWalkForwardSplitter, WalkForwardRunner


@pytest.fixture(scope="module")
def source(tmp_path_factory):
    root = tmp_path_factory.mktemp("synthetic-stage-source")
    days = pd.bdate_range("2020-01-02", "2020-06-30")
    frame = pd.DataFrame({
        "date": np.repeat(days.to_numpy(), 6),
        "symbol": ["%06d.SZ" % (number + 1) for number in range(6)] * len(days),
    })
    frame["REV1"] = np.tile(np.linspace(-0.5, 0.5, 6), len(days))
    frame["MOM20"] = np.cos(np.repeat(np.arange(len(days)) / 9, 6)
                                + np.tile(np.arange(6), len(days)))
    frame["EXCESS_RET5"] = 0.02 * frame["REV1"] - 0.01 * frame["MOM20"]
    frame["entry_date"] = frame["date"].map(
        pd.Series(list(days[1:]) + [pd.NaT], index=days)
    )
    frame["exit_date"] = frame["date"].map(
        pd.Series(list(days[6:]) + [pd.NaT] * 6, index=days)
    )
    frame.loc[frame["exit_date"].isna(), "EXCESS_RET5"] = np.nan
    frame.loc[0, "EXCESS_RET5"] = np.nan
    config = ResearchConfig(
        project=ProjectConfig(name="synthetic-stage-test-only"),
        data=DataConfig(start=date(2020, 1, 2), end=date(2020, 6, 30)),
        universe=UniverseConfig(provider="local", expected_member_count=6),
        factors=("REV1", "MOM20"),
        paths=PathsConfig(data_root=root / "data", results_root=root / "results"),
        evaluation=EvaluationConfig(quantiles=4, min_cross_section=4),
        portfolio=PortfolioConfig(top_k=2, rebalance_days=5),
        validation=ValidationConfig(
            initial_train_months=1, validation_months=1, test_months=1,
            step_months=2, purge_days=5, embargo_days=5,
        ),
    )
    object.__setattr__(config, "repo_root", root)
    config.validate()
    output = root / "ridge" / "source-run"
    runner = WalkForwardRunner(
        ExpandingWalkForwardSplitter(
            initial_train_months=1, validation_months=1, test_months=1,
            step_months=2, purge_days=5, embargo_days=5,
        ),
        model_name="ridge", feature_names=config.factors,
        top_k=2, minimum_cross_section=4, random_seed=config.seed,
        ridge_alphas=(0.1, 1.0), model_directory=output / "fold_models",
    )
    result = runner.run(frame, calendar=days)
    cli._bind_diagnostic_sources(config, runner, result, frame, days)
    prediction = evaluate_predictions(result.predictions, top_k=2, minimum_cross_section=4)
    empty = FastBacktestResult(
        nav=pd.DataFrame(), trades=pd.DataFrame(), positions=pd.DataFrame(),
        signals=pd.DataFrame(),
    )
    empty_execution = ExecutionBacktestResult(
        nav=pd.DataFrame(), trades=pd.DataFrame(), positions=pd.DataFrame(),
        signals=pd.DataFrame(), rejections=pd.DataFrame(),
    )
    write_model_report(
        "ridge", result, prediction, empty, {}, output,
        disclaimer="SYNTHETIC SOFTWARE TEST ONLY - NOT A RESEARCH RESULT",
        execution_backtest=empty_execution, execution_metrics={},
    )
    return config, runner, result, frame, days, output


@pytest.fixture
def bundle(source, tmp_path):
    config, runner, original, frame, days, source_output = source
    output = tmp_path / "ridge" / "verified-run"
    shutil.copytree(source_output, output)
    # Relocate saved-model evidence without refitting, predicting, or changing
    # any source cohort, label, score, selected parameter, or model bytes.
    fold_summary = original.fold_summary.copy(deep=True)
    audit = original.diagnostic_audit.copy(deep=True)
    for table in (fold_summary, audit):
        table["model_path"] = table["model_path"].map(
            lambda value: str(output / Path(value).relative_to(source_output))
        )
    result = replace(
        original, fold_summary=fold_summary, diagnostic_audit=audit,
        model_paths=tuple(output / path.relative_to(source_output) for path in original.model_paths),
    )
    for name, table in (
        ("walk_forward_results", fold_summary), ("stage_prediction_audit", audit),
    ):
        table.to_parquet(output / (name + ".parquet"), index=False)
        table.to_csv(output / (name + ".csv"), index=False)
    inventory = cli._artifact_sha256(output)
    manifest = {
        "config": config.to_dict(), "dataset_sha256": "d" * 64,
        "universe_version": "e" * 64, "stage_diagnostics_version": 1,
        "model_artifact_sha256": inventory,
        "predictions_sha256": inventory["predictions.parquet"],
    }
    manifest["stage_diagnostic_provenance"] = cli._stage_manifest_binding(
        result, output, inventory, manifest["dataset_sha256"], manifest["universe_version"],
    )
    return config, runner, result, frame, days, output, manifest


def _snapshots(bundle):
    return cli._verify_model_artifacts("ridge", bundle[-2], bundle[-1])


def test_complete_stage_evidence_binds_same_input_models_and_verified_tables(bundle):
    config, runner, result, frame, days, output, manifest = bundle
    cli._bind_diagnostic_sources(config, runner, result, frame, days)
    before = frame.copy(deep=True)
    snapshots = _snapshots(bundle)
    cohort = cli._verified_stage_cohort("ridge", output, manifest, snapshots)
    assert len(cohort) == len(result.fold_summary) * 3
    assert set(cohort["stage"]) == {"TRAIN_IN_SAMPLE", "VALIDATION_SELECTION", "HISTORICAL_TEST"}
    assert "stage_predictions.csv" not in snapshots  # Streamed, not duplicated in RAM.
    assert "stage_predictions.parquet" in snapshots
    assert manifest["stage_diagnostic_provenance"]["table_rows"]["stage_predictions"] == len(result.diagnostic_predictions)
    assert len(manifest["stage_diagnostic_provenance"]["saved_model_sha256"]) == len(result.fold_summary)
    assert "score" not in cohort and "selected_parameters" not in cohort
    pd.testing.assert_frame_equal(frame, before)


@pytest.mark.parametrize("column", ["REV1", "EXCESS_RET5", "symbol"])
def test_source_binding_rejects_changed_features_labels_or_members(source, column):
    config, runner, result, frame, days, _ = source
    altered = frame.copy(deep=True)
    if column == "symbol":
        altered.loc[0, column] = "600999.SH"
    else:
        altered.loc[6, column] += 0.123
    with pytest.raises(FactorLabError, match="source/config differs"):
        cli._bind_diagnostic_sources(config, runner, result, altered, days)


@pytest.mark.parametrize("field", ["seed", "factors", "top_k", "minimum_cross_section"])
def test_source_binding_rejects_protocol_changes(source, field):
    config, runner, result, frame, days, _ = source
    if field == "seed":
        changed = replace(config, seed=43)
    elif field == "factors":
        changed = replace(config, factors=tuple(reversed(config.factors)))
    elif field == "top_k":
        changed = replace(config, portfolio=replace(config.portfolio, top_k=3))
    else:
        changed = replace(config, evaluation=replace(config.evaluation, min_cross_section=5))
    with pytest.raises(FactorLabError, match="differ"):
        cli._bind_diagnostic_sources(changed, runner, result, frame, days)


def test_source_reverification_does_not_fit_predict_or_change_data(source, monkeypatch):
    config, runner, result, frame, days, _ = source
    from qbot.research.models.ridge import RidgeResearchModel
    def forbidden(*args, **kwargs):
        raise AssertionError("source checks must not fit/predict")
    monkeypatch.setattr(RidgeResearchModel, "fit", forbidden)
    monkeypatch.setattr(RidgeResearchModel, "predict", forbidden)
    before = frame.copy(deep=True)
    cli._bind_diagnostic_sources(config, runner, result, frame, days)
    pd.testing.assert_frame_equal(before, frame)


@pytest.mark.parametrize("filename", sorted(cli._STAGE_ARTIFACTS))
def test_declared_stage_report_cannot_drop_required_artifacts_and_reseal(bundle, filename):
    output, manifest = bundle[-2:]
    (output / filename).unlink()
    manifest["model_artifact_sha256"] = cli._artifact_sha256(output)
    with pytest.raises(FactorLabError, match="inventory is missing"):
        _snapshots(bundle)


@pytest.mark.parametrize("version", [True, False, "1", 2, None])
def test_unknown_or_boolean_stage_version_is_rejected(bundle, version):
    bundle[-1]["stage_diagnostics_version"] = version
    with pytest.raises(FactorLabError, match="unknown stage diagnostic policy/version"):
        _snapshots(bundle)


def test_stage_artifacts_cannot_be_relabelled_as_legacy(bundle):
    bundle[-1].pop("stage_diagnostics_version")
    with pytest.raises(FactorLabError, match="lack producer provenance"):
        _snapshots(bundle)


@pytest.mark.parametrize("dataset_id,universe_id", [("unknown", "e" * 64), ("d" * 64, None)])
def test_producer_binding_requires_verified_input_identities(bundle, dataset_id, universe_id):
    result, output, manifest = bundle[2], bundle[-2], bundle[-1]
    with pytest.raises(FactorLabError, match="verified dataset/universe identities"):
        cli._stage_manifest_binding(result, output, manifest["model_artifact_sha256"], dataset_id, universe_id)


def test_saved_model_hash_must_match_hashed_inventory(bundle):
    result, output, manifest = bundle[2], bundle[-2], bundle[-1]
    inventory = dict(manifest["model_artifact_sha256"])
    model_name = next(name for name in inventory if name.startswith("fold_models/"))
    inventory[model_name] = "f" * 64
    with pytest.raises(FactorLabError, match="saved model SHA-256 differs"):
        cli._stage_manifest_binding(result, output, inventory, "d" * 64, "e" * 64)


def test_saved_model_path_cannot_escape_report_root(bundle):
    result, output, manifest = bundle[2], bundle[-2], bundle[-1]
    audit, folds = result.diagnostic_audit.copy(), result.fold_summary.copy()
    escaped = str(output.parent / "outside.pkl")
    audit["model_path"] = escaped
    folds["model_path"] = escaped
    altered = replace(result, diagnostic_audit=audit, fold_summary=folds)
    with pytest.raises(FactorLabError, match="path escapes"):
        cli._stage_manifest_binding(altered, output, manifest["model_artifact_sha256"], "d" * 64, "e" * 64)


def test_changed_source_digest_resealed_bytes_do_not_replace_producer_evidence(bundle):
    output, manifest = bundle[-2:]
    path = output / "stage_prediction_audit.parquet"
    audit = pd.read_parquet(path)
    audit["source_sha256"] = "f" * 64  # Valid syntax is not the original input identity.
    audit.to_parquet(path, index=False)
    audit.to_csv(output / "stage_prediction_audit.csv", index=False)
    manifest["model_artifact_sha256"] = cli._artifact_sha256(output)
    snapshots = _snapshots(bundle)
    with pytest.raises(FactorLabError, match="differ from producer provenance"):
        cli._verified_stage_cohort("ridge", output, manifest, snapshots)


@pytest.mark.parametrize("field,bad", [("seed", 43), ("factors", ["MOM20", "REV1"])])
def test_pinned_stage_protocol_must_match_producer_config(bundle, field, bad):
    output, manifest = bundle[-2:]
    manifest["config"][field] = bad
    snapshots = _snapshots(bundle)
    with pytest.raises(FactorLabError, match="differ.*from producer config"):
        cli._verified_stage_cohort("ridge", output, manifest, snapshots)


def test_stage_reader_uses_same_verified_bytes_after_paths_change(bundle, monkeypatch):
    output, manifest = bundle[-2:]
    snapshots = _snapshots(bundle)
    expected = cli._verified_stage_cohort("ridge", output, manifest, snapshots)
    for name in list(cli._STAGE_TABLE_ATTRIBUTES) + ["predictions", "walk_forward_results"]:
        (output / (name + ".parquet")).write_bytes(b"replaced after verified snapshot")
        (output / (name + ".csv")).write_bytes(b"changed human-readable view after snapshot")
    original_reader = pd.read_parquet
    original_csv_reader = pd.read_csv
    def pinned_reader(source, *args, **kwargs):
        assert isinstance(source, io.BytesIO), "reader reopened a mutable path"
        return original_reader(source, *args, **kwargs)
    def pinned_csv_reader(source, *args, **kwargs):
        assert isinstance(source, io.BytesIO), "CSV check reopened a mutable path"
        return original_csv_reader(source, *args, **kwargs)
    def forbidden(*args, **kwargs):
        raise AssertionError("comparison re-read a path or model")
    monkeypatch.setattr(pd, "read_parquet", pinned_reader)
    monkeypatch.setattr(pd, "read_csv", pinned_csv_reader)
    monkeypatch.setattr(Path, "read_bytes", forbidden)
    monkeypatch.setattr(cli, "_file_sha256", forbidden)
    actual = cli._verified_stage_cohort("ridge", output, manifest, snapshots)
    pd.testing.assert_frame_equal(actual, expected)


@pytest.mark.parametrize("name,column", [
    ("stage_prediction_daily", "prediction_ic"),
    ("stage_prediction_summary", "prediction_rank_ic_mean"),
    ("stage_prediction_audit", "source_sha256"),
])
def test_resealed_csv_cannot_contradict_verified_parquet_values(bundle, name, column):
    output, manifest = bundle[-2:]
    path = output / (name + ".csv")
    changed = pd.read_csv(path, dtype=str, keep_default_na=False)
    changed.loc[0, column] = "f" * 64 if column.endswith("sha256") else "-0.98765"
    changed.to_csv(path, index=False)
    manifest["model_artifact_sha256"] = cli._artifact_sha256(output)
    snapshots = _snapshots(bundle)
    with pytest.raises(FactorLabError, match="CSV/Parquet values differ"):
        cli._verified_stage_cohort("ridge", output, manifest, snapshots)


@pytest.mark.parametrize("name,date_column", [
    ("stage_prediction_daily", "date"),
    ("stage_prediction_audit", "signal_start"),
])
def test_stage_comparison_uses_lossless_typed_verified_parquet(bundle, monkeypatch, name, date_column):
    output, manifest = bundle[-2:]
    snapshots = _snapshots(bundle)
    cli._verified_stage_cohort("ridge", output, manifest, snapshots)
    expected = pd.read_parquet(io.BytesIO(snapshots[name + ".parquet"]))
    (output / (name + ".parquet")).write_bytes(b"invalid replacement")
    def forbidden(*args, **kwargs):
        raise AssertionError("stage comparison parsed CSV or reopened a mutable path")
    monkeypatch.setattr(pd, "read_csv", forbidden)
    monkeypatch.setattr(Path, "read_bytes", forbidden)
    actual = cli._combine_model_tables(
        {"ridge": output}, name + ".parquet", required=True,
        artifact_snapshots={"ridge": snapshots},
    )
    assert pd.api.types.is_datetime64_ns_dtype(actual[date_column])
    assert actual["source_run_id"].eq(output.name).all()
    pd.testing.assert_frame_equal(actual.drop(columns="source_run_id"), expected)


def test_optional_stage_alpha_has_explicit_type_without_dropping_missing_evidence(tmp_path):
    # This isolates concat schema handling; not a producer or market-data fixture.
    fixed = pd.DataFrame({"fold": [0], "model_name": ["factor_score"], "selected_alpha": [None]})
    ridge = pd.DataFrame({"fold": [0], "model_name": ["ridge"], "selected_alpha": [0.1]})
    snapshots = {}
    for model_name, table in (("factor_score", fixed), ("ridge", ridge)):
        buffer = io.BytesIO()
        table.to_parquet(buffer, index=False)
        snapshots[model_name] = {"stage_prediction_audit.parquet": buffer.getvalue()}
    with warnings.catch_warnings(record=True) as recorded:
        warnings.simplefilter("always", FutureWarning)
        actual = cli._combine_model_tables(
            {name: tmp_path / name for name in snapshots},
            "stage_prediction_audit.parquet", required=True, artifact_snapshots=snapshots,
        )
    assert not [item for item in recorded if issubclass(item.category, FutureWarning)]
    assert str(actual["selected_alpha"].dtype) == "float64"
    assert pd.isna(actual.iloc[0]["selected_alpha"])
    assert actual.iloc[1]["selected_alpha"] == 0.1


def test_formal_source_binding_rejects_empty_legacy_diagnostics(source):
    config, runner, result, frame, days, _ = source
    legacy = replace(result, **{name: pd.DataFrame() for name in cli._STAGE_TABLE_ATTRIBUTES.values()})
    with pytest.raises(FactorLabError, match="requires complete stage diagnostics"):
        cli._bind_diagnostic_sources(config, runner, legacy, frame, days)
    assert cli._verified_stage_cohort("ridge", Path("unused"), {}, {}) is None


def test_manifest_policy_does_not_claim_train_validation_are_independent_holdouts(bundle):
    evidence = bundle[-1]["stage_diagnostic_provenance"]
    policy = evidence["policy"]
    assert policy["fit_scope"] == "train_only"
    assert policy["train"] == "TRAIN_IN_SAMPLE:diagnostic_only"
    assert policy["validation"] == "VALIDATION_SELECTION:selection_and_diagnostic"
    assert policy["test"] == "HISTORICAL_TEST:evaluation_only"
    assert policy["validation_prediction"] == "reuse_selected_candidate"
    assert policy["test_predictions_per_fold"] == 1
    assert policy["refit_train_plus_validation"] is False
    assert "separately" in policy["pooling"]
    json.dumps(evidence, allow_nan=False)
