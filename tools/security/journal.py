"""Durable phase observations for long acceptance runs; no approval semantics."""
import hashlib
import json
import os

# Test bodies deliberately replace builtins.open and other process globals.
# Reporting runs after the assertion, before monkeypatch teardown. Keep the
# observer's own I/O independent from the functions exercised by those attacks.
_OPEN = open
_DUMPS = json.dumps
_SHA256 = hashlib.sha256
_DESTINATION = os.environ.get("FACTORLAB_TEST_JOURNAL")


def pytest_runtest_logreport(report):
    if not _DESTINATION:
        return
    item = {"nodeid": report.nodeid, "phase": report.when, "outcome": report.outcome,
            "duration": report.duration,
            "failure_trace_sha256": _SHA256(str(report.longrepr).encode()).hexdigest()
                if report.failed else None}
    with _OPEN(_DESTINATION, "a", encoding="utf-8") as handle:
        handle.write(_DUMPS(item) + "\n")
