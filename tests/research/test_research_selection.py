"""Portable placement policy tests with synthetic resource snapshots."""

from copy import deepcopy
import subprocess
from unittest.mock import Mock

import pytest

from factorlab.experiments import research_selection as selection

MIB = 1024**2


@pytest.fixture
def task():
    return {"id": "placement", "entry": "gbm",
            "parameters": {"paths": 100000, "steps": 252, "assets": 1,
                           "batch_size": 1024, "seed": 104729},
            "inputs": {}, "device": "auto", "memory_budget_mb": 8192,
            "gpu_memory_limit_mb": 4096, "timeout_seconds": 600,
            "outputs": ["report.json", "samples.npz"]}


@pytest.fixture
def resources():
    return {"host_available_bytes": 32 * 1024 * MIB, "gpu_warm": False,
            "gpus": [{"index": 0, "name": "NVIDIA GeForce RTX 2070",
                      "total_memory_bytes": 8192 * MIB,
                      "free_memory_bytes": 7000 * MIB, "utilization_percent": 0}]}


@pytest.fixture
def cloud():
    return {"authorized": True, "available": True, "balance_cu": 100,
            "budget_cu": 2, "rate_per_hour": 2.33, "startup_seconds": 120,
            "host_memory_bytes": 53000 * MIB, "gpu_memory_bytes": 23034 * MIB}


def test_native_host_memory_probe():
    assert selection._host_available_bytes() > 0


def test_probe_cpu_never_runs_gpu_subprocess(monkeypatch):
    monkeypatch.setattr(selection, "_host_available_bytes", lambda: 12345)
    run = Mock(side_effect=AssertionError("GPU probe on CPU path"))
    monkeypatch.setattr(selection.subprocess, "run", run)
    assert selection.probe_resources(False) == {"host_available_bytes": 12345, "gpus": []}
    run.assert_not_called()


def test_probe_parses_actual_multiple_gpus(monkeypatch):
    monkeypatch.setattr(selection, "_host_available_bytes", lambda: 12345)
    run = Mock(return_value=subprocess.CompletedProcess([], 0,
        "0, NVIDIA RTX 2070, 8192, 6000, 19\n1, NVIDIA L4, 23034, 12000, 65\n", ""))
    monkeypatch.setattr(selection.subprocess, "run", run)
    result = selection.probe_resources()
    assert result["gpus"][0]["free_memory_bytes"] == 6000 * MIB
    assert result["gpus"][1]["utilization_percent"] == 65
    assert result["gpus"][1]["name"] == "NVIDIA L4"
    assert run.call_args.args[0][0] == "nvidia-smi"
    assert "utilization.gpu" in run.call_args.args[0][1]


@pytest.mark.parametrize("error", [FileNotFoundError("absent"),
    subprocess.CalledProcessError(1, "nvidia-smi"),
    subprocess.TimeoutExpired("nvidia-smi", 10)])
def test_gpu_probe_unavailable_is_explicit(monkeypatch, error):
    monkeypatch.setattr(selection, "_host_available_bytes", lambda: 12345)
    monkeypatch.setattr(selection.subprocess, "run", Mock(side_effect=error))
    result = selection.probe_resources()
    assert result["gpus"] == []
    assert result["gpu_probe_error"]


def test_cpu_select_probes_without_gpu(task, monkeypatch):
    task["device"] = "cpu"
    probe = Mock(return_value={"host_available_bytes": 8192 * MIB})
    monkeypatch.setattr(selection, "probe_resources", probe)
    result = selection.select_task(task, None)
    probe.assert_called_once_with(include_gpu=False)
    assert result["device"] == result["compute_device"] == "cpu"
    assert result["estimated_gpu_bytes"] == 0


def test_cold_small_cpu_warm_same_workload_gpu(task, resources):
    cold = selection.select_task(task, resources)
    assert cold["device"] == "cpu"
    assert "cold" in cold["reason"]
    assert cold["evidence"][1]["cpu_seconds"] == 2.467
    resources["gpu_warm"] = True
    warm = selection.select_task(task, resources)
    assert warm["device"] == "local_gpu"
    assert warm["compute_device"] == "gpu"
    assert warm["estimated_seconds"] is None


@pytest.mark.parametrize("model,cpu,gpu", [("gbm", 17.041418, .455560),
                                           ("bootstrap", 14.445657, .288307)])
def test_million_steady_uses_measured_cell(task, resources, model, cpu, gpu):
    task["entry"] = model
    task["parameters"].update(paths=1000000, batch_size=4096)
    resources["gpu_warm"] = True
    result = selection.select_task(task, resources)
    assert result["device"] == "local_gpu"
    assert result["evidence"][1]["cpu_seconds"] == cpu
    assert result["evidence"][1]["local_gpu_seconds"] == gpu
    resources["gpu_warm"] = False
    assert selection.select_task(task, resources)["device"] == "cpu"


@pytest.mark.parametrize("change", [{"assets": 32}, {"steps": 8760},
                                    {"paths": 123456}, {"batch_size": 128},
                                    {"volatility": .2}])
def test_no_generic_speed_extrapolation(task, resources, change):
    resources["gpu_warm"] = True
    task["parameters"].update(change)
    result = selection.select_task(task, resources)
    assert result["device"] == "cpu"
    assert "no matched lifecycle evidence" in result["reason"]


@pytest.mark.parametrize("device", ["cpu", "local_gpu"])
def test_explicit_local_never_falls_back(task, resources, cloud, device):
    task["device"] = device
    resources["host_available_bytes"] = 1
    with pytest.raises(RuntimeError, match="explicit"):
        selection.select_task(task, resources, cloud)


def test_explicit_gpu_busy_still_honored(task, resources):
    task["device"] = "local_gpu"
    resources["gpus"][0]["utilization_percent"] = 95
    assert selection.select_task(task, resources)["device"] == "local_gpu"
    resources["gpus"] = []
    with pytest.raises(RuntimeError, match="explicit local GPU"):
        selection.select_task(task, resources)


def test_gpu_limit_and_free_memory_both_enforced(task, resources):
    task["device"] = "local_gpu"
    for field in ("gpu_memory_limit_mb", "free_memory_bytes"):
        t, r = deepcopy(task), deepcopy(resources)
        if field == "gpu_memory_limit_mb":
            t[field] = 1
        else:
            r["gpus"][0][field] = 1
        with pytest.raises(RuntimeError, match="memory"):
            selection.select_task(t, r)


def test_batch_workspace_bounded_but_results_scale(task, resources):
    task["device"] = "local_gpu"
    small = selection.select_task(task, resources)
    task["parameters"]["paths"] *= 10
    large = selection.select_task(task, resources)
    assert small["estimated_gpu_bytes"] == large["estimated_gpu_bytes"]
    assert large["estimated_host_bytes"] - small["estimated_host_bytes"] == 900000 * 8 * 8
    task["parameters"]["steps"] *= 2
    assert selection.select_task(task, resources)["estimated_gpu_bytes"] > large["estimated_gpu_bytes"]
    task["parameters"]["assets"] = 8
    assert selection.select_task(task, resources)["estimated_gpu_bytes"] > large["estimated_gpu_bytes"]


def test_static_grid_accounts_for_streamed_quote_batch(task, resources):
    task["device"] = "cpu"
    base = selection.select_task(task, resources)
    task["entry"] = "static_grid"
    grid = selection.select_task(task, resources)
    assert grid["estimated_host_bytes"] - base["estimated_host_bytes"] == (task['parameters']['batch_size'] * 253 * 8 + 100000 * 8 * 12)


@pytest.mark.parametrize("change", [{"authorized": False}, {"available": False},
    {"balance_cu": .001}, {"budget_cu": .001}, {"rate_per_hour": 0},
    {"gpu_memory_bytes": 1}, {"host_memory_bytes": 1}])
def test_explicit_cloud_refuses_unavailable_or_unfunded(task, resources, cloud, change):
    task["device"] = "colab"
    cloud.update(change)
    with pytest.raises(RuntimeError, match="explicit Colab"):
        selection.select_task(task, resources, cloud)


def test_cloud_requires_metadata_and_does_not_assume_availability(task, resources):
    task["device"] = "colab"
    for cloud in (None, {}, {"authorized": True}, {"authorized": True, "available": True}):
        with pytest.raises(RuntimeError, match="explicit Colab"):
            selection.select_task(task, resources, cloud)


def test_auto_cloud_busy_threshold(task, resources, cloud):
    resources["gpus"][0]["utilization_percent"] = 19
    assert selection.select_task(task, resources, cloud)["device"] == "cpu"
    resources["gpus"][0]["utilization_percent"] = 20
    result = selection.select_task(task, resources, cloud)
    assert result["device"] == "colab"
    assert result["estimated_seconds"] is None
    assert result["estimated_cost_cu"] is None
    assert result["evidence"][-1]["reserved_cost_cu"] == pytest.approx(720 * 2.33 / 3600)


def test_auto_cloud_for_memory_pressure_or_opt_in(task, resources, cloud):
    resources["gpus"][0]["free_memory_bytes"] = 1
    assert selection.select_task(task, resources, cloud)["device"] == "colab"
    resources["gpus"] = []
    assert selection.select_task(task, resources, cloud)["device"] == "cpu"
    task["throughput_opt_in"] = True
    assert selection.select_task(task, resources, cloud)["device"] == "colab"
    cloud["available"] = False
    assert selection.select_task(task, resources, cloud)["device"] == "cpu"


def test_cloud_lifecycle_advantage_requires_reliable_exact_reports(task, resources, cloud):
    record = {"entry": task["entry"], "parameters": deepcopy(task["parameters"]),
              "source": "measured-report.json", "scope": "full_lifecycle", "reliable": True}
    resources["measurements"] = [dict(record, device="cpu", seconds=300)]
    cloud["measurements"] = [dict(record, device="colab", seconds=200)]
    result = selection.select_task(task, resources, cloud)
    assert result["device"] == "colab"
    assert result["estimated_seconds"] == 200
    assert result["estimated_cost_cu"] == pytest.approx(200 * 2.33 / 3600)
    cloud["measurements"][0]["scope"] = "kernel"
    assert selection.select_task(task, resources, cloud)["device"] == "cpu"
    cloud["measurements"][0]["scope"] = "full_lifecycle"
    cloud["measurements"][0]["parameters"]["assets"] = 32
    assert selection.select_task(task, resources, cloud)["device"] == "cpu"


def test_local_lifecycle_estimate_requires_selected_device(task, resources):
    record = {"entry": task["entry"], "parameters": deepcopy(task["parameters"]),
              "source": "measured-report.json", "scope": "full_lifecycle", "reliable": True,
              "seconds": 2.5, "device": "local_gpu"}
    resources["measurements"] = [record]
    assert selection.select_task(task, resources)["estimated_seconds"] is None
    record["device"] = "cpu"
    assert selection.select_task(task, resources)["estimated_seconds"] == 2.5


def test_no_placement_fits(task, resources):
    resources["host_available_bytes"] = 1
    with pytest.raises(RuntimeError, match="no authorized placement"):
        selection.select_task(task, resources)


@pytest.mark.parametrize("field,value", [("paths", 0), ("steps", -1), ("assets", True),
                                         ("batch_size", 0), ("batch_size", 1.5)])
def test_invalid_dimensions_rejected(task, resources, field, value):
    task["parameters"][field] = value
    with pytest.raises(ValueError):
        selection.select_task(task, resources)


def test_task_and_resource_snapshots_not_mutated(task, resources, cloud):
    before = deepcopy((task, resources, cloud))
    selection.select_task(task, resources, cloud)
    assert (task, resources, cloud) == before
