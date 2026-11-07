"""Core-only imports/readers work; computation never bypasses resource guards."""

import builtins
import os
from pathlib import Path
import subprocess
import sys

import pytest

from examples.research.dataset_registry_v2_demo import synthetic_inputs
from factorlab.dataset.partitioned import PartitionedDataset, _require_psutil
from factorlab.errors import ConfigurationError
from factorlab.factors.registry_v2 import FactorRegistryV2


@pytest.mark.parametrize("mode", ["import", "help", "run"])
def test_core_only_imports_and_cli_message(tmp_path, mode):
    destination = tmp_path / "unpublished"
    code = '''
import builtins, runpy, sys
original_import = builtins.__import__
def without_psutil(name, *args, **kwargs):
    if name == "psutil" or name.startswith("psutil."):
        raise ModuleNotFoundError("simulated core-only install", name="psutil")
    return original_import(name, *args, **kwargs)
builtins.__import__ = without_psutil
from factorlab.dataset import DatasetBuilder
from factorlab.dataset.partitioned import PartitionedDataset, ChunkedConfig
from examples.research.scalable_factor_computation import configured
if sys.argv[1] != "import":
    sys.argv = ["scalable_factor_computation", "--help"] if sys.argv[1] == "help" else [
        "scalable_factor_computation", "--level", "L0", "--output", sys.argv[2]]
    runpy.run_module("examples.research.scalable_factor_computation", run_name="__main__")
'''
    root = Path(__file__).resolve().parents[2]
    env = dict(os.environ, PYTHONPATH=os.pathsep.join((str(root / "src"), str(root))),
               PYTHONUTF8="1")
    result = subprocess.run([sys.executable, "-c", code, mode, str(destination)],
                            cwd=tmp_path, env=env, capture_output=True, text=True,
                            encoding="utf-8")
    if mode == "run":
        assert result.returncode != 0
        assert "python -m pip install -r requirements/scalable-compute.txt" in result.stderr
    else:
        assert result.returncode == 0, result.stderr
    assert not destination.exists()


def test_verified_reader_needs_no_monitor_but_execution_fails_closed(tmp_path, monkeypatch):
    dates, bars, *_ = synthetic_inputs(80, 2)
    snapshot = FactorRegistryV2.with_builtins().freeze()
    refs = (snapshot.get("REV1", "1").ref,)
    bundle = snapshot.compute_chunked(bars, refs, calendar=dates, output_dir=tmp_path / "complete")
    original_import = builtins.__import__

    def without_psutil(name, *args, **kwargs):
        if name == "psutil":
            raise ModuleNotFoundError("simulated core-only install", name="psutil")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_psutil)
    PartitionedDataset(bundle.path, expected_identity_hash=bundle.identity_hash).verify()
    with pytest.raises(ConfigurationError, match="requirements/scalable-compute.txt"):
        snapshot.compute_chunked(bars, refs, calendar=dates, output_dir=tmp_path / "missing")
    assert not (tmp_path / "missing").exists()
    assert not list(tmp_path.glob("missing.pending-*"))


def test_broken_monitor_install_preserves_original_error(monkeypatch):
    original_import = builtins.__import__

    def broken_native_import(name, *args, **kwargs):
        if name == "psutil":
            raise ModuleNotFoundError("broken native install", name="_psutil_windows")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", broken_native_import)
    with pytest.raises(ModuleNotFoundError, match="broken native install"):
        _require_psutil()
