"""Durable phase observations for long acceptance runs; no approval semantics."""
import hashlib
import json
import os


def pytest_runtest_logreport(report):
    destination = os.environ.get("FACTORLAB_TEST_JOURNAL")
    if not destination:
        return
    item = {"nodeid": report.nodeid, "phase": report.when, "outcome": report.outcome,
            "duration": report.duration,
            "failure_trace_sha256": hashlib.sha256(str(report.longrepr).encode()).hexdigest()
                if report.failed else None}
    with open(destination, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(item) + "\n")
