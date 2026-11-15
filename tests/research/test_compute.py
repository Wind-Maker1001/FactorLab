"""Device policy tests use implementation probes, not CI GPU hardware."""

import json
from pathlib import Path
import subprocess
import sys
from types import ModuleType

import pytest

from factorlab.compute import (
    BackendCapability, BackendStatus, ComputeConfig,
    ComputeUnavailableError, Device, probe_runtime, resolve_compute,
)
from factorlab.config import load_config
from factorlab.errors import ConfigurationError
from tests.research.test_config import _valid_document, _write_config
from tests.research.test_experiment_tracker import definition


def ready(config):
    return BackendStatus(True, version="test-1", runtime_available=True)


CPU = BackendCapability(Device.CPU, "numpy", ready)
GPU = BackendCapability(Device.GPU, "test-cuda", ready)


@pytest.mark.parametrize("config", [ComputeConfig(), ComputeConfig(device="cpu")])
def test_cpu_does_not_probe_gpu(config):
    def forbidden(_):
        pytest.fail("CPU must not initialize GPU")
    selected = resolve_compute((BackendCapability(Device.GPU, "cuda", forbidden), CPU), config)
    assert selected.device == Device.CPU
    assert selected.backend == "numpy"
    assert not selected.fallback


@pytest.mark.parametrize("mode", ["gpu", "auto"])
def test_available_gpu(mode):
    selected = resolve_compute((CPU, GPU), ComputeConfig(device=mode))
    assert selected.device == Device.GPU
    assert selected.status.version == "test-1"
    assert not selected.fallback


@pytest.mark.parametrize("allow", [False, True])
def test_auto_cpu_is_not_explicit_gpu_fallback(allow):
    unavailable = BackendCapability(Device.GPU, "cuda", lambda _: BackendStatus(
        False, "CUDA runtime incompatible", driver_visible=True,
    ))
    selected = resolve_compute((unavailable, CPU), ComputeConfig(device="auto", allow_fallback=allow))
    assert selected.device == Device.CPU
    assert not selected.fallback


@pytest.mark.parametrize("allow", [False, True])
def test_explicit_gpu_failure_policy(allow):
    unavailable = BackendCapability(Device.GPU, "opencl", lambda _: BackendStatus(
        False, "OpenCL target kernel unsupported", runtime_available=True, driver_visible=True,
    ))
    config = ComputeConfig(device="gpu", allow_fallback=allow)
    if not allow:
        with pytest.raises(ComputeUnavailableError, match="opencl: OpenCL target kernel unsupported"):
            resolve_compute((unavailable, CPU), config)
    else:
        selected = resolve_compute((unavailable, CPU), config)
        assert selected.fallback
        assert selected.device == Device.CPU
        assert "OpenCL target kernel unsupported" in selected.fallback_reason


def test_gpu_is_task_specific_and_cpu_must_also_exist():
    with pytest.raises(ComputeUnavailableError, match="gpu: task declares no implementation"):
        resolve_compute((CPU,), ComputeConfig(device="gpu"))
    selected = resolve_compute((CPU,), ComputeConfig(device="gpu", allow_fallback=True))
    assert selected.fallback
    with pytest.raises(ComputeUnavailableError, match="cpu: task declares no implementation"):
        resolve_compute((), ComputeConfig(device="gpu", allow_fallback=True))
    with pytest.raises(ComputeUnavailableError, match="cpu: task declares no implementation"):
        resolve_compute((GPU,))


def test_backend_priority_distinguishes_cuda_and_opencl():
    cuda = BackendCapability(Device.GPU, "cuda", lambda _: BackendStatus(False, "missing runtime"))
    opencl = BackendCapability(Device.GPU, "opencl", ready)
    selected = resolve_compute((cuda, opencl, GPU, CPU), ComputeConfig(device="auto"))
    assert selected.backend == "opencl"
    with pytest.raises(ValueError, match="duplicate backend"):
        resolve_compute((CPU, CPU))


def test_unexpected_probe_failure_is_not_hidden():
    def broken(_):
        raise RuntimeError("implementation bug")
    with pytest.raises(RuntimeError, match="implementation bug"):
        resolve_compute((BackendCapability(Device.GPU, "cuda", broken), CPU),
                        ComputeConfig(device="gpu", allow_fallback=True))


def test_budget_reaches_probe_without_claiming_enforcement():
    def budget_probe(config):
        assert config.gpu_memory_limit_mb == 32
        return BackendStatus(False, "insufficient memory: task requires 64 MiB", runtime_available=True)
    selected = resolve_compute((BackendCapability(Device.GPU, "cuda", budget_probe), CPU),
                               ComputeConfig("gpu", True, 32))
    record = json.loads(json.dumps(selected.to_dict(), allow_nan=False))
    assert record["gpu_memory_limit_mb"] == 32
    assert record["gpu_memory_limit_enforced"] is False
    assert "insufficient memory" in record["fallback_reason"]


@pytest.mark.parametrize("kwargs, message", [
    ({"device": "cuda"}, "compute.device"),
    ({"device": None}, "compute.device"),
    ({"allow_fallback": "false"}, "compute.allow_fallback"),
    ({"gpu_memory_limit_mb": True}, "compute.gpu_memory_limit_mb"),
    ({"gpu_memory_limit_mb": 0}, "compute.gpu_memory_limit_mb"),
    ({"gpu_memory_limit_mb": -1}, "compute.gpu_memory_limit_mb"),
    ({"gpu_memory_limit_mb": 1.5}, "compute.gpu_memory_limit_mb"),
])
def test_invalid_config(kwargs, message):
    with pytest.raises(ConfigurationError, match=message):
        ComputeConfig(**kwargs)


@pytest.mark.parametrize("kwargs", [
    {"available": True},
    {"available": True, "runtime_available": True, "driver_visible": False},
    {"available": False},
    {"available": True, "runtime_available": True, "reason": "failed"},
])
def test_probe_cannot_claim_driver_only_readiness(kwargs):
    with pytest.raises(ValueError):
        BackendStatus(**kwargs)


def test_optional_runtime_import_and_incompatible_backend(monkeypatch):
    import factorlab.compute.backend as backend
    def missing(name):
        raise ModuleNotFoundError(name)
    monkeypatch.setattr(backend, "import_module", missing)
    result = probe_runtime("optional_gpu", lambda _: pytest.fail("runtime absent"))
    assert not result.available and not result.runtime_available
    assert "optional_gpu" in result.reason
    runtime = ModuleType("optional_gpu")
    monkeypatch.setattr(backend, "import_module", lambda _: runtime)
    def incompatible(module):
        assert module is runtime
        return BackendStatus(False, "requires backend version >= 2", version="1", runtime_available=True)
    result = probe_runtime("optional_gpu", incompatible)
    assert result.runtime_available and not result.available
    assert result.version == "1"


def test_runtime_version_and_linker_failure(monkeypatch):
    import factorlab.compute.backend as backend
    runtime = ModuleType("optional_gpu")
    runtime.__version__ = "2.0"
    monkeypatch.setattr(backend, "import_module", lambda _: runtime)
    status = probe_runtime("optional_gpu", lambda _: BackendStatus(True, runtime_available=True))
    assert status.version == "2.0"
    def linker_failure(_):
        raise OSError("incompatible native library")
    monkeypatch.setattr(backend, "import_module", linker_failure)
    status = probe_runtime("optional_gpu", lambda _: pytest.fail("linker failed"))
    assert not status.available
    assert "incompatible native library" in status.reason


def test_cpu_runtime_failure_is_not_accepted():
    unavailable = BackendCapability(Device.CPU, "numpy", lambda _: BackendStatus(False, "numpy absent"))
    with pytest.raises(ComputeUnavailableError, match="numpy absent"):
        resolve_compute((unavailable,))


def test_forbidden_cpu_fallback_is_not_probed():
    def forbidden(_):
        pytest.fail("CPU fallback forbidden")
    unavailable = BackendCapability(Device.GPU, "cuda", lambda _: BackendStatus(False, "unsupported build"))
    with pytest.raises(ComputeUnavailableError, match="unsupported build"):
        resolve_compute((unavailable, BackendCapability(Device.CPU, "python", forbidden)), ComputeConfig("gpu"))


def test_yaml_integration_and_serialization(tmp_path):
    document = _valid_document()
    document["compute"] = {"device": "auto", "allow_fallback": False, "gpu_memory_limit_mb": 128}
    config = load_config(_write_config(tmp_path, document), repo_root=tmp_path)
    assert config.compute == ComputeConfig("auto", False, 128)
    assert config.to_dict()["compute"] == document["compute"]


@pytest.mark.parametrize("compute, message", [
    ({"surprise": 1}, "unknown field.*compute"),
    ({"device": "cuda"}, "compute.device"),
    ({"allow_fallback": 1}, "compute.allow_fallback"),
    ({"gpu_memory_limit_mb": True}, "compute.gpu_memory_limit_mb"),
])
def test_invalid_yaml(compute, message, tmp_path):
    document = _valid_document()
    document["compute"] = compute
    with pytest.raises(ConfigurationError, match=message):
        load_config(_write_config(tmp_path, document), repo_root=tmp_path)


def test_cpu_only_import_and_resolution_in_clean_process():
    source = str(Path(__file__).resolve().parents[2] / "src")
    script = '''
import importlib.abc
import sys
sys.path.insert(0, sys.argv[1])
class BlockGPU(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'cupy', 'torch', 'pyopencl', 'numba', 'numpy', 'xgboost', 'lightgbm'}:
            raise AssertionError('unexpected compute import: ' + fullname)
sys.meta_path.insert(0, BlockGPU())
from factorlab.compute import BackendCapability, BackendStatus, Device, resolve_compute
cpu = BackendCapability(Device.CPU, 'python', lambda _: BackendStatus(True, runtime_available=True))
assert resolve_compute((cpu,)).device == Device.CPU
'''
    subprocess.run([sys.executable, "-I", "-c", script, source], check=True, capture_output=True, text=True)


def test_tracker_accepts_compute_environment(tmp_path, definition):
    from factorlab.experiments import ExperimentTracker
    tracker = ExperimentTracker(tmp_path / "store")
    tracker.create_experiment("compute", definition)
    record = resolve_compute((CPU,)).to_dict()
    tracker.append_run("compute", "run", code={"commit": None, "tree": None},
                       environment={"compute": record}, command=["research.py"], status="SUCCEEDED")
    assert tracker.reproduction("run")["run"]["environment"]["compute"] == record
