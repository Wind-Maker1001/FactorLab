"""Observe real pytest collection and reports without changing test execution."""

import hashlib
import json
from pathlib import Path
import time

import pytest


def pytest_addoption(parser):
    parser.addoption("--ci-evidence-dir", help="Directory for actual pytest evidence")


def pytest_configure(config):
    destination = config.getoption("--ci-evidence-dir")
    if destination:
        config.pluginmanager.register(
            Observer(config, Path(destination)), "ci-observer"
        )


class Observer:
    def __init__(self, config, destination):
        self.config = config
        self.destination = destination
        self.worker = getattr(config, "workerinput", {}).get("workerid", "controller")
        self.started = time.perf_counter()
        self.collection_seconds = 0.0
        self.all_ids = []
        self.selected_ids = []
        self.deselected_ids = []
        self.worker_collections = {}
        self.worker_errors = []
        self.coverage_worker_receipts = {}
        self.reports = []
        self.rootpath = str(config.rootpath.resolve())
        self.inipath = str(config.inipath.resolve()) if config.inipath else None
        self.ini_sha256 = (
            hashlib.sha256(config.inipath.read_bytes()).hexdigest()
            if config.inipath
            else None
        )

    @pytest.hookimpl(hookwrapper=True)
    def pytest_collection(self):
        started = time.perf_counter()
        yield
        self.collection_seconds += time.perf_counter() - started

    @pytest.hookimpl(hookwrapper=True, tryfirst=True)
    def pytest_collection_modifyitems(self, items):
        self.all_ids = [item.nodeid for item in items]
        yield
        self.selected_ids = [item.nodeid for item in items]

    def pytest_deselected(self, items):
        self.deselected_ids.extend(item.nodeid for item in items)

    @pytest.hookimpl(optionalhook=True)
    def pytest_xdist_node_collection_finished(self, node, ids):
        self.worker_collections[node.gateway.id] = list(ids)

    @pytest.hookimpl(optionalhook=True)
    def pytest_testnodedown(self, node, error):
        self.coverage_worker_receipts[node.gateway.id] = getattr(
            node, "workeroutput", {}
        ).get("cov_worker_node_id")
        if error:
            self.worker_errors.append({"worker": node.gateway.id, "error": str(error)})

    def pytest_runtest_logreport(self, report):
        self.reports.append(
            {
                "nodeid": report.nodeid,
                "when": report.when,
                "outcome": report.outcome,
                "seconds": report.duration,
                "worker": getattr(report, "worker_id", self.worker),
                "wasxfail": getattr(report, "wasxfail", None),
                "skip_reason": (
                    str(report.longrepr[2])
                    if report.skipped and isinstance(report.longrepr, tuple)
                    else None
                ),
            }
        )

    @pytest.hookimpl(hookwrapper=True, tryfirst=True)
    def pytest_sessionfinish(self, exitstatus):
        started = time.perf_counter()
        yield
        self.destination.mkdir(parents=True, exist_ok=True)
        payload = {
            "kind": "actual_pytest_observation_not_financial_certification",
            "worker": self.worker,
            "exitstatus": int(exitstatus),
            "elapsed_seconds": time.perf_counter() - self.started,
            "collection_seconds": self.collection_seconds,
            "report_generation_and_sessionfinish_seconds": time.perf_counter()
            - started,
            "all_ids": self.all_ids,
            "selected_ids": self.selected_ids,
            "deselected_ids": self.deselected_ids,
            "worker_collections": self.worker_collections,
            "worker_errors": self.worker_errors,
            "coverage_worker_receipts": self.coverage_worker_receipts,
            "coverage_failed_workers": [
                str(getattr(getattr(node, "gateway", None), "id", node))
                for node in getattr(
                    getattr(
                        self.config.pluginmanager.getplugin("_cov"),
                        "cov_controller",
                        None,
                    ),
                    "failed_workers",
                    [],
                )
            ],
            "reports": self.reports,
            "rootpath": self.rootpath,
            "inipath": self.inipath,
            "pytest_ini_sha256": self.ini_sha256,
        }
        (self.destination / ("pytest-" + self.worker + ".json")).write_text(
            json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8"
        )
