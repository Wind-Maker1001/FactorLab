"""Give each pytest process a private Matplotlib cache before test imports."""

import json
import os
from pathlib import Path
import re
import sys

import pytest


class Resources:
    def __init__(self, worker, directory, output):
        self.worker = worker
        self.directory = directory
        self.output = output

    def save(self, config, finished):
        matplotlib = sys.modules.get("matplotlib")
        actual = str(matplotlib.get_cachedir()) if matplotlib else None
        expected = str(self.directory / "matplotlib")
        if actual is not None and actual != expected:
            raise RuntimeError("Matplotlib did not use its isolated worker cache")
        record = {
            "worker": self.worker,
            "directory": str(self.directory),
            "MPLCONFIGDIR": os.environ["MPLCONFIGDIR"],
            "matplotlib_cache": actual,
            "configured_before_matplotlib_import": True,
            "pytest_cache_directory": str(config.cache._cachedir),
            "pytest_cache_writer": self.worker == "controller",
            "finished": finished,
        }
        self.output.write_text(
            json.dumps(record, sort_keys=True, indent=2) + "\n", encoding="utf-8"
        )

    def pytest_sessionfinish(self, session):
        self.save(session.config, True)


@pytest.hookimpl(tryfirst=True)
def pytest_load_initial_conftests(early_config):
    root = os.environ.get("CI_WORKER_STATE_ROOT")
    output = os.environ.get("CI_WORKER_EVIDENCE_DIR")
    if not root or not output:
        raise RuntimeError(
            "Worker resource isolation requires the benchmark controller"
        )
    if "matplotlib" in sys.modules:
        raise RuntimeError("Matplotlib was imported before worker resource isolation")
    worker = os.environ.get("PYTEST_XDIST_WORKER", "controller")
    if not re.fullmatch(r"controller|gw[0-9]+", worker):
        raise RuntimeError("Unexpected pytest worker identity")
    directory = Path(root) / worker
    directory.mkdir(parents=True, exist_ok=False)
    os.environ["MPLCONFIGDIR"] = str(directory / "matplotlib")
    early_config._ci_resources = Resources(
        worker, directory, Path(output) / ("worker-resources-" + worker + ".json")
    )


@pytest.hookimpl(trylast=True)
def pytest_configure(config):
    resources = config._ci_resources
    config.pluginmanager.register(resources, "ci-private-resources")
    resources.save(config, False)
