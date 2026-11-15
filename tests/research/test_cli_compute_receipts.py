"""Completed CLI manifests retain all native tree execution receipts."""

from dataclasses import asdict
import json

import pytest

from factorlab import cli
from factorlab.compute import BackendStatus, ComputeConfig
from factorlab.models import tree_compute
from tests.research.test_cli_phase2 import _config, _inputs, _write_verified_inputs


pytestmark = pytest.mark.usefixtures("synthetic_cli_admission")


@pytest.mark.parametrize("model_name", ["xgboost", "lightgbm"])
@pytest.mark.parametrize("device_request", [None, "gpu", "auto"])
def test_completed_cli_manifest_retains_all_compute_receipts(
    tmp_path, monkeypatch, model_name, device_request,
):
    config = _config(tmp_path)
    if device_request is not None:
        object.__setattr__(config, "compute", ComputeConfig(device=device_request, allow_fallback=True))
    monkeypatch.setattr(
        cli.AKShareProvider, "cache_source_version",
        lambda self, symbol, is_index: "synthetic-cli-cache-v1",
    )
    _, dataset, bars, benchmark = _inputs()
    _write_verified_inputs(config, dataset, bars=bars, benchmark=benchmark)

    def unavailable_gpu(*args):
        return BackendStatus(False, "GPU unavailable in receipt persistence test")

    monkeypatch.setattr(tree_compute, "_probe", unavailable_gpu)
    real_run = cli.WalkForwardRunner.run
    observed = {}

    def record_result(runner, *args, **kwargs):
        result = real_run(runner, *args, **kwargs)
        observed["result"] = result
        return result

    monkeypatch.setattr(cli.WalkForwardRunner, "run", record_result)
    run = cli.RunManifest(config, "walk-forward", {"model": model_name})
    cli.command_walk_forward(config, run, model_name)
    run.finish()

    manifest = json.loads(run.path.read_text(encoding="utf-8"))
    result = observed["result"]
    records = manifest["environment"]["compute"]
    assert manifest["status"] == "success"
    assert records == list(result.compute_records)
    assert manifest["config"]["compute"] == asdict(config.compute)
    assert manifest["hyperparameters"] == cli._model_hyperparameters(config, model_name)
    assert {record["fold"] for record in records} == set(result.fold_summary["fold"])
    for fold in result.fold_summary["fold"]:
        fold_records = [record for record in records if record["fold"] == fold]
        training = [record for record in fold_records if record["evaluation_stage"] == "TRAIN"]
        validation = [record for record in fold_records
                      if record["evaluation_stage"] == "VALIDATION_SELECTION"]
        assert len(training) == len(validation) == 2
        assert {record["candidate"] for record in training} == {
            record["candidate"] for record in validation
        }
        assert {record["evaluation_stage"] for record in fold_records} == {
            "TRAIN", "VALIDATION_SELECTION", "HISTORICAL_TEST", "TRAIN_IN_SAMPLE",
        }
    for record in records:
        assert record["model_name"] == model_name
        assert record["device"] == "cpu"
        assert record["backend"] == model_name + "-cpu"
        assert record["probe"]["version"]
        assert record["executed"]
        assert record["execution"]
        assert record["stage"] in {"fit", "predict"}
        # LightGBM predictions have an independent CPU policy even after GPU requests.
        expected = config.compute if model_name == "xgboost" or record["stage"] == "fit" else ComputeConfig()
        assert record["requested"] == asdict(expected)
        assert record["fallback"] == (expected.device == "gpu")
        if record["fallback"]:
            assert "GPU unavailable in receipt persistence test" in record["fallback_reason"]
        else:
            assert record["fallback_reason"] is None
