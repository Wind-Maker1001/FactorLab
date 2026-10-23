"""Resealed counterexamples must fail in the real four-model comparison entry."""

import copy

import pandas as pd
import pytest

pytestmark = pytest.mark.usefixtures("synthetic_cli_admission")

from factorlab import cli
from factorlab.errors import FactorLabError
from factorlab.diagnostics.model_readiness.synthetic import MODELS
from tests.research.test_cli_phase2 import _Run, _config, _inputs, _write_verified_inputs


@pytest.fixture(scope="module")
def comparison_bundle(tmp_path_factory):
    root = tmp_path_factory.mktemp("synthetic-comparison")
    _, dataset, bars, benchmark = _inputs()
    config = _config(root)
    _, membership, _ = _write_verified_inputs(config, dataset)
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(cli, "_load_benchmark_with_warmup", lambda current: benchmark)
        monkeypatch.setattr(cli, "_load_stock_bars", lambda current, symbols: bars)
        monkeypatch.setattr(cli, "_load_verified_membership", lambda current, calendar: membership)
        outputs, manifests = {}, {}
        for number, model in enumerate(MODELS):
            run = _Run("2020010%dT000000-readiness-%s" % (number + 1, model))
            cli.command_walk_forward(config, run, model)
            manifest = dict(run.payload, status="success", run_id=run.run_id,
                            command="walk-forward", model=model, config=config.to_dict())
            path = config.paths.runs_dir / run.run_id / "manifest.json"
            cli._atomic_json(manifest, path)
            outputs[model] = config.paths.model_results_dir / model / run.run_id
            manifests[model] = (path, manifest)
        # Positive control uses the same fully validated producers as mutations.
        cli.command_compare_models(config, _Run("readiness-positive-control"))
        yield config, outputs, manifests


@pytest.mark.parametrize("model", MODELS)
@pytest.mark.parametrize("field", [
    "git_commit", "package_versions", "dataset_sha256", "universe_version",
    "bar_data_version", "backtest_stock_bars_sha256", "backtest_benchmark_sha256",
    "execution_bars_sha256", "security_data_manifest_sha256", "config",
])
def test_changed_version_or_input_fails_before_output(comparison_bundle, monkeypatch, model, field):
    config, _, manifests = comparison_bundle
    path, original = manifests[model]
    changed = copy.deepcopy(original)
    if field == "package_versions":
        changed[field]["numpy"] = "different-synthetic-runtime"
    elif field == "config":
        changed[field]["project"]["name"] = "changed"
    else:
        changed[field] = "b" * (40 if field == "git_commit" else 64)
    cli._atomic_json(changed, path)
    run = _Run("counterexample-%s-%s" % (model, field))
    try:
        with pytest.raises(FactorLabError):
            cli.command_compare_models(config, run)
        assert not (config.paths.report_dir / run.run_id).exists()
    finally:
        cli._atomic_json(original, path)


@pytest.mark.parametrize("model", MODELS)
@pytest.mark.parametrize("mutation", ["missing_date", "security_set", "label_value", "label_exit", "fold"])
def test_resealed_oos_identity_mismatch_is_rejected(comparison_bundle, model, mutation):
    config, outputs, manifests = comparison_bundle
    path, original = manifests[model]
    output = outputs[model]
    prediction_path = output / "predictions.parquet"
    saved = prediction_path.read_bytes()
    predictions = pd.read_parquet(prediction_path)
    if mutation == "missing_date":
        predictions = predictions.loc[predictions.date.ne(predictions.date.min())]
    elif mutation == "security_set":
        predictions.loc[predictions.symbol.eq(predictions.symbol.iloc[0]), "symbol"] = "600099.SH"
    elif mutation == "label_value":
        predictions.loc[0, "EXCESS_RET5"] += .123
    elif mutation == "label_exit":
        predictions.loc[0, "exit_date"] += pd.offsets.BDay(1)
    else:
        predictions["fold"] += 10
    predictions.to_parquet(prediction_path, index=False)
    changed = copy.deepcopy(original)
    # Hashes are intentionally recomputed. Semantic equality must still fail.
    changed["model_artifact_sha256"] = cli._artifact_sha256(output)
    changed["predictions_sha256"] = changed["model_artifact_sha256"]["predictions.parquet"]
    cli._atomic_json(changed, path)
    run = _Run("counterexample-%s-%s" % (model, mutation))
    try:
        # Mutating the baseline can trip its own stage-cohort binding before
        # the next model is reached; either semantic guard is valid evidence.
        with pytest.raises(FactorLabError, match="identical out-of-sample|Stage diagnostic|Independent Test predictions"):
            cli.command_compare_models(config, run)
        assert not (config.paths.report_dir / run.run_id).exists()
    finally:
        prediction_path.write_bytes(saved)
        cli._atomic_json(original, path)


def test_unsealed_prediction_bytes_rejected(comparison_bundle):
    config, outputs, _ = comparison_bundle
    path = outputs["ridge"] / "predictions.parquet"
    saved = path.read_bytes()
    try:
        path.write_bytes(saved + b"tampered")
        with pytest.raises(FactorLabError, match="SHA-256 mismatch"):
            cli.command_compare_models(config, _Run("counterexample-byte-tamper"))
    finally:
        path.write_bytes(saved)


def test_successful_synthetic_producers_cannot_authorize_formal_comparison(
    comparison_bundle, monkeypatch,
):
    from factorlab.data.admission import assert_formal_market_admission

    config, _, _ = comparison_bundle
    # Restore the real boundary after software-only producer preparation.
    monkeypatch.setattr(cli, "assert_formal_market_admission", assert_formal_market_admission)
    run = _Run("unqualified-historical-comparison")
    with pytest.raises(FactorLabError, match="MARKET DATA QUALIFICATION REQUIRED.*no qualification package"):
        cli.command_compare_models(config, run)
    assert run.outputs == {}
    assert not (config.paths.report_dir / run.run_id).exists()
