"""Offline lifecycle tests; these do not allocate Colab runtimes."""

import importlib.util
import json
from pathlib import Path
import subprocess

import pytest


spec = importlib.util.spec_from_file_location(
    "colab_job", Path(__file__).parents[2] / "tools/research/colab_job.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


@pytest.fixture
def job(tmp_path):
    data = {"session": "fl-test", "colab": "/isolated/bin/colab", "gpu": "L4",
            "budget_cu": 2, "timeout": 1800, "wheel": "/tmp/factorlab-0.1.0-py3-none-any.whl",
            "script": "/tmp/script.py", "commit": "a" * 40, "paths": 1_000_000,
            "steps": 252, "seed": 0}
    (tmp_path / "job.json").write_text(json.dumps(data))
    return module.Job(tmp_path)


def test_usage_and_deadline():
    account = module.usage("Current balance: 200.00 compute units\nUsage rate: 2.33/hr\nActive assignments: 1")
    assert account == {"balance": 200, "rate_per_hour": 2.33, "assignments": 1}
    assert module.budget_seconds(0.5, 200, 2.5, 20, 1800) == 580


@pytest.mark.parametrize("budget,balance,rate,elapsed,timeout", [
    (1, 0.5, 2, 0, 1800), (1, 10, 0, 0, 1800),
    (float("nan"), 10, 2, 0, 1800), (0.01, 10, 10, 0, 1800),
])
def test_unfunded_or_unbounded_work_refused(budget, balance, rate, elapsed, timeout):
    with pytest.raises((ValueError, TimeoutError)):
        module.budget_seconds(budget, balance, rate, elapsed, timeout)


def test_usage_format_change_refused():
    with pytest.raises(ValueError):
        module.usage("Remaining 200 CU")


def test_cli_argv_and_live_log(job, monkeypatch):
    def run(argv, **kwargs):
        assert argv == ["/isolated/bin/colab", "--auth", "adc", "--config",
                        str(job.root / "sessions.json"), "new", "-s", "fl-test", "--gpu", "L4"]
        assert kwargs["env"]["REQUEST_TIMEOUT"] == "60"
        kwargs["stdout"].write("allocated\n")
        return subprocess.CompletedProcess(argv, 0)
    monkeypatch.setattr(module.subprocess, "run", run)
    assert job.cli("new", "-s", "fl-test", "--gpu", "L4") == "allocated\n"
    assert "allocated" in (job.root / "controller.log").read_text()


def test_nonzero_cli_fails(job, monkeypatch):
    monkeypatch.setattr(module.subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess(a, 1))
    with pytest.raises(subprocess.CalledProcessError):
        job.cli("new")


def test_release_verified_by_endpoint_not_name(job, monkeypatch):
    job.save(allocation_attempted=True, endpoint="vm-123")
    monkeypatch.setattr(job, "cli", lambda *a, **kw: "[?] vm-123 | Hardware: L4")
    with pytest.raises(RuntimeError, match="remains assigned"):
        job.release()
    assert module.Job(job.root).data["state"] == "cleanup_failed"


def test_release_and_recovery_are_idempotent(job, monkeypatch):
    job.save(allocation_attempted=True, endpoint="vm-123")
    calls = []
    def cli(*args, **kwargs):
        calls.append(args)
        if len(calls) == 1:
            return "[fl-test] vm-123 | Hardware: L4"
        return "[colab] No active sessions found on server."
    monkeypatch.setattr(job, "cli", cli)
    job.release()
    job.release()
    assert calls == [("sessions",), ("stop", "-s", "fl-test"), ("sessions",)]
    assert module.Job(job.root).data["released"] is True


def test_interrupted_allocation_preserves_orphan_evidence(job, monkeypatch):
    job.save(allocation_attempted=True)
    monkeypatch.setattr(job, "cli", lambda *a, **kw: "[?] unknown | Hardware: L4")
    with pytest.raises(RuntimeError, match="Unidentified"):
        job.release()
    assert job.data["released"] is False


@pytest.mark.parametrize("failure_at", ["exec", "download"])
def test_failures_release_runtime(job, monkeypatch, failure_at):
    calls = []
    def cli(*args, **kwargs):
        calls.append(args)
        if args[0] == "version":
            return "Version: 0.8.0\n"
        if args[0] == "usage":
            count = 1 if job.data.get("endpoint") and not job.data.get("released") else 0
            return f"Current balance: 200.00 compute units\nUsage rate: 2.33/hr\nActive assignments: {count}"
        if args[0] == "sessions":
            if any(x[0] == "stop" for x in calls):
                return "[colab] No active sessions found on server."
            return "[fl-test] vm-123 | Hardware: L4"
        if args[0] == failure_at:
            raise subprocess.TimeoutExpired(args, 1)
        if args[0] == "exec":
            return 'FACTORLAB_HARDWARE={"gpu":"NVIDIA L4"}\n'
        return ""
    monkeypatch.setattr(job, "cli", cli)
    with pytest.raises(subprocess.TimeoutExpired):
        job.run()
    assert ("stop", "-s", "fl-test") in calls
    assert job.data["released"] is True
    assert job.data["state"] == "failed"


def test_cpu_or_missing_gpu_stops_before_install(job, monkeypatch):
    calls = []
    def cli(*args, **kwargs):
        calls.append(args)
        if args[0] == "version":
            return "Version: 0.8.0"
        if args[0] == "usage":
            count = int(bool(job.data.get("endpoint")) and not job.data.get("released"))
            return f"Current balance: 200.00 compute units\nUsage rate: 2.33/hr\nActive assignments: {count}"
        if args[0] == "sessions":
            return "" if any(c[0] == "stop" for c in calls) else "[fl-test] vm-123 | Hardware: CPU"
        if args[0] == "exec":
            raise subprocess.CalledProcessError(1, "nvidia-smi")
        return ""
    monkeypatch.setattr(job, "cli", cli)
    with pytest.raises(subprocess.CalledProcessError):
        job.run()
    assert not any(c[0] == "upload" for c in calls)
    assert job.data["released"]
