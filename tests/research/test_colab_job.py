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


def test_native_session_config_used(job, monkeypatch):
    job.save(session_config="/native/private/sessions.json")
    def run(argv, **kwargs):
        assert argv[4] == "/native/private/sessions.json"
        return subprocess.CompletedProcess(argv, 0)
    monkeypatch.setattr(module.subprocess, "run", run)
    job.cli("sessions")


def test_session_storage_failure_prevents_allocation(job, monkeypatch):
    calls = []
    def cli(*args, **kwargs):
        calls.append(args[0])
        if args[0] == "version":
            return "Version: 0.8.0"
        raise PermissionError("session lock permissions")
    monkeypatch.setattr(job, "cli", cli)
    with pytest.raises(PermissionError):
        job.run()
    assert "new" not in calls
    assert not job.data.get("allocation_attempted")


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


@pytest.mark.parametrize("failure_at", ["exec", "download", "receipt"])
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
            if failure_at == "receipt":
                return "CalledProcessError: research child failed\n"
            import re
            receipt = re.search(r"FACTORLAB_EXEC_OK=[a-f0-9]+", (job.root / "payload.py").read_text())[0]
            return 'FACTORLAB_HARDWARE={"gpu":"NVIDIA L4"}\n' + receipt + '\n'
        return ""
    monkeypatch.setattr(job, "cli", cli)
    expected = RuntimeError if failure_at == "receipt" else subprocess.TimeoutExpired
    with pytest.raises(expected):
        job.run()
    assert ("stop", "-s", "fl-test") in calls
    assert job.data["released"] is True
    assert job.data["state"] == "failed"


@pytest.mark.parametrize("download_failure,cleanup_failure", [
    (False, False), (True, False), (True, True),
])
def test_failed_research_retrieves_evidence_before_release(
        job, monkeypatch, download_failure, cleanup_failure):
    calls = []
    original = subprocess.CalledProcessError(7, "research")

    def cli(*args, **kwargs):
        calls.append(args)
        if args[0] == "version":
            return "Version: 0.8.0"
        if args[0] == "usage":
            count = int(bool(job.data.get("endpoint")) and not job.data.get("released"))
            return f"Current balance: 200.00 compute units\nUsage rate: 2.33/hr\nActive assignments: {count}"
        if args[0] == "sessions":
            return "" if any(c[0] == "stop" for c in calls) else "[fl-test] vm-123 | Hardware: L4"
        if args[0] == "download":
            assert not any(c[0] == "stop" for c in calls)
            if download_failure and str(args[-2]).endswith("samples.npz"):
                raise OSError("artifact transfer failed")
            Path(args[-1]).write_text("retained evidence")
        if args[0] == "stop" and cleanup_failure:
            raise RuntimeError("release failed")
        return ""

    executions = []
    def execute(code):
        executions.append(code)
        if len(executions) == 1:
            return 'FACTORLAB_HARDWARE={"gpu":"NVIDIA L4"}'
        if len(executions) == 3:
            raise original
        return ""

    monkeypatch.setattr(job, "cli", cli)
    monkeypatch.setattr(job, "execute", execute)
    with pytest.raises(subprocess.CalledProcessError) as caught:
        job.run()
    assert caught.value is original
    assert (job.root / "results/report.json").read_text() == "retained evidence"
    assert [str(c[-2]) for c in calls if c[0] == "download"] == [
        "/content/factorlab-result/report.json", "/content/factorlab-result/samples.npz"]
    saved = module.Job(job.root).data
    assert saved["error"].startswith("CalledProcessError:")
    assert saved["state"] == ("cleanup_failed" if cleanup_failure else "failed")
    if cleanup_failure:
        assert saved["cleanup_error"] == "RuntimeError: release failed"
    else:
        assert saved["released"] is True
    if download_failure:
        assert saved["failure_artifact_error"] == "OSError: artifact transfer failed"
        assert not saved.get("failure_artifacts_downloaded")
    else:
        assert saved["failure_artifacts_downloaded"] is True
        assert (job.root / "results/samples.npz").read_text() == "retained evidence"


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


def test_fresh_process_ignores_preloaded_numpy(tmp_path, monkeypatch):
    import sys
    import types
    import numpy
    loaded = numpy.__version__
    monkeypatch.setitem(sys.modules, "numpy", types.SimpleNamespace(__version__="0.0.0-old-kernel"))
    output = tmp_path / "loaded.txt"
    script = tmp_path / "research.py"
    script.write_text("import numpy; from pathlib import Path; Path(" + repr(str(output)) + ").write_text(numpy.__version__)")
    exec(module.research_code([str(script)]), {})
    assert output.read_text() == loaded


def test_research_child_failure_propagates(tmp_path):
    script = tmp_path / "failure.py"
    script.write_text("raise RuntimeError('research failed')")
    with pytest.raises(subprocess.CalledProcessError):
        exec(module.research_code([str(script)]), {})


def test_missing_success_receipt_is_failure(job, monkeypatch):
    monkeypatch.setattr(job, "cli", lambda *a, **kw: "remote traceback but exit zero")
    with pytest.raises(RuntimeError, match="success receipt"):
        job.execute("raise RuntimeError()")


@pytest.mark.parametrize("numpy_version,accepted", [("2.3.5", True), ("2.4.6", False)])
def test_loaded_dependency_constraints(numpy_version, accepted, monkeypatch):
    from email.message import Message
    path = Path(__file__).parents[2] / "tools/research/colab_monte_carlo.py"
    spec = importlib.util.spec_from_file_location("colab_mc", path)
    research = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(research)
    package = Message()
    package["Requires-Python"] = ">=3.12,<3.15"
    package["Requires-Dist"] = "numpy>=1.26.4,<2.4"
    package["Requires-Dist"] = 'cupy-cuda13x>=14,<15; extra == "monte-carlo-cuda"'
    monkeypatch.setattr(research, "metadata", lambda _: package)
    versions = {"numpy": numpy_version, "cupy-cuda13x": "14.0.1"}
    if accepted:
        research.validate_dependencies(versions)
    else:
        with pytest.raises(RuntimeError, match="numpy 2.4.6 violates"):
            research.validate_dependencies(versions)


@pytest.mark.parametrize("action", ["run", "recover"])
def test_busy_run_never_operates_or_cleans_other_job(job, monkeypatch, action):
    import sys
    monkeypatch.setattr(module.Path, "home", lambda: job.root)
    monkeypatch.setattr(sys, "argv", ["colab_job.py", action, "--job", str(job.root)])
    monkeypatch.setattr(module, "operate", lambda *a: pytest.fail("busy task operated"))
    from contextlib import contextmanager
    @contextmanager
    def busy_lock(_):
        raise module.AccountBusy
        yield
    monkeypatch.setattr(module, "account_lock", busy_lock)
    with pytest.raises(SystemExit) as exc:
        module.main()
    assert exc.value.code == 75
    if action == "run":
        assert module.Job(job.root).data["state"] == "BUSY"
    else:
        assert module.Job(job.root).data == job.data
    assert not job.data.get("allocation_attempted")


def test_resources_summary(tmp_path, monkeypatch):
    import threading
    spec = importlib.util.spec_from_file_location(
        "colab_resources", Path(__file__).parents[2] / "tools/research/colab_resources.py")
    resources = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(resources)
    cgroup = tmp_path / "cgroup"
    cgroup.mkdir()
    for name, text in {"memory.peak": "123456", "memory.events": "oom 0\noom_kill 0\n",
                       "cpu.stat": "usage_usec 2000000\n", "memory.pressure": "some total=0",
                       "cpu.pressure": "some total=1"}.items():
        (cgroup / name).write_text(text)
    measurement = resources.Resources.__new__(resources.Resources)
    measurement.root = tmp_path
    measurement.cgroup = cgroup
    measurement.stop = threading.Event()
    measurement.thread = threading.Thread(target=lambda: None)
    measurement.thread.start()
    measurement.error = None
    measurement.before = {"time": 10, "available_kib": 500}
    measurement.minimum = 300
    measurement.samples = 2
    monkeypatch.setattr(resources, "snapshot", lambda: {"time": 20, "available_kib": 400})
    measurement.finish()
    report = json.loads((tmp_path / "resources.json").read_text())
    assert report["minimum_available_kib"] == 300
    assert report["cpu_percent_one_core"] == 20
    assert report["memory_peak_bytes"] == 123456
    measurement.error = OSError("procfs read failed")
    with pytest.raises(RuntimeError, match="Resource sampling failed"):
        measurement.finish()


def test_submit_forwards_proxy_by_name_without_exposing_value(tmp_path, monkeypatch):
    import os
    import shutil
    import sys
    wheel = tmp_path / "factorlab-0.1.0-py3-none-any.whl"
    wheel.touch()
    script = tmp_path / "research.py"
    script.touch()
    monkeypatch.setenv("HTTPS_PROXY", "http://private-proxy:10808")
    monkeypatch.setattr(os, "getuid", lambda: 1000, raising=False)
    monkeypatch.setattr(shutil, "which", lambda _: "/isolated/bin/colab")
    monkeypatch.setattr(sys, "argv", ["colab_job.py", "submit", "--job", str(tmp_path / "job"),
        "--wheel", str(wheel), "--script", str(script), "--commit", "a" * 40, "--budget-cu", "2"])
    def run(command, **kwargs):
        assert "--setenv=HTTPS_PROXY" in command
        preserved = next(arg for arg in command if arg.startswith("--preserve-env="))
        assert "HTTPS_PROXY" in preserved.split("=", 1)[1].split(",")
        assert not any("private-proxy" in argument for argument in command)
        assert command[-3] == "run"
        return subprocess.CompletedProcess(command, 0)
    monkeypatch.setattr(module.subprocess, "run", run)
    module.main()


@pytest.fixture
def batch_manifest(tmp_path):
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    (inputs / "history.npy").write_bytes(b"explicit input")
    (inputs / "unlisted.json").write_text("not uploaded")
    manifest = {"schema_version": 1, "commit": "a" * 40, "timeout_seconds": 1800,
                "budget_cu": 2, "tasks": [{
                    "id": "bootstrap", "entry": "bootstrap", "parameters": {},
                    "inputs": {"returns": "inputs/history.npy"}, "device": "colab",
                    "memory_budget_mb": 512, "gpu_memory_limit_mb": None,
                    "timeout_seconds": 60, "outputs": ["report.json", "samples.npz"]}]}
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest))
    return path


def test_batch_deploy_installs_once_and_uploads_only_declared(job, batch_manifest, monkeypatch):
    job.save(manifest=str(batch_manifest))
    calls, executions = [], []
    monkeypatch.setattr(job, "cli", lambda *a, **kw: calls.append(a))
    monkeypatch.setattr(job, "execute", lambda code: executions.append(code))
    job.deploy()
    assert len(executions) == 2
    assert executions[0].count("'install'") == 1
    assert "[monte-carlo-cuda,monte-carlo-benchmark,monte-carlo-native]" in executions[0]
    assert "root.mkdir(mode=0o700)" in executions[1]
    assert [str(c[-1]) for c in calls] == [
        "/content/factorlab.whl", "/content/factorlab-input/manifest.json",
        "/content/factorlab-input/inputs/history.npy"]
    assert all(c[0] == "upload" for c in calls)


def test_batch_submit_uses_manifest_limits(job, batch_manifest, monkeypatch):
    import os
    import shutil
    import sys
    wheel = job.root / "factorlab-0.1.0-py3-none-any.whl"
    wheel.touch()
    destination = job.root / "submitted"
    monkeypatch.setattr(os, "getuid", lambda: 1000, raising=False)
    monkeypatch.setattr(shutil, "which", lambda _: "/isolated/bin/colab")
    monkeypatch.setattr(sys, "argv", ["colab_job.py", "submit", "--job", str(destination),
                                    "--wheel", str(wheel), "--manifest", str(batch_manifest)])
    commands = []
    monkeypatch.setattr(module.subprocess, "run", lambda command, **kw: commands.append(command))
    module.main()
    saved = module.Job(destination).data
    assert saved["manifest"] == str(batch_manifest.resolve())
    assert saved["timeout"] == 1800 and saved["budget_cu"] == 2
    assert saved["commit"] == "a" * 40
    assert len(commands) == 1


@pytest.mark.parametrize("name", ["../outside.npy", "/tmp/outside.npy", "inputs/secret.npy",
                                   "inputs/.env", "inputs/history.py"])
def test_batch_rejects_unsafe_inputs(batch_manifest, name):
    document = json.loads(batch_manifest.read_text())
    document["tasks"][0]["inputs"]["returns"] = name
    batch_manifest.write_text(json.dumps(document))
    with pytest.raises(ValueError):
        module.manifest_inputs(batch_manifest)


def test_release_never_stops_reassigned_name(job, monkeypatch):
    job.save(allocation_attempted=True, endpoint="vm-owned")
    calls = []
    def cli(*args, **kwargs):
        calls.append(args)
        return "[fl-test] vm-foreign | Hardware: L4"
    monkeypatch.setattr(job, "cli", cli)
    with pytest.raises(RuntimeError, match="different runtime"):
        job.release()
    assert calls == [("sessions",)]


@pytest.mark.parametrize("batch", [False, True])
def test_expired_work_deadline_salvages_before_release(job, batch_manifest, monkeypatch, batch):
    if batch:
        job.save(manifest=str(batch_manifest))
    calls, executions = [], []
    def cli(*args, **kwargs):
        calls.append(args)
        if args[0] == "version":
            return "Version: 0.8.0"
        if args[0] == "usage":
            count = int(bool(job.data.get("endpoint")) and not job.data.get("released"))
            return f"Current balance: 200.00 compute units\nUsage rate: 2.33/hr\nActive assignments: {count}"
        if args[0] == "sessions":
            return "" if any(c[0] == "stop" for c in calls) else "[fl-test] vm-123 | Hardware: L4"
        if args[0] == "download":
            remaining = job.deadline - module.time.monotonic()
            assert 0 < remaining <= module.SALVAGE_SECONDS
            assert not any(c[0] == "stop" for c in calls)
            Path(args[-1]).write_text("salvaged")
        return ""
    def execute(code):
        executions.append(code)
        if len(executions) == 1:
            return 'FACTORLAB_HARDWARE={"gpu":"NVIDIA L4"}'
        if "pip" in code or "root.mkdir" in code:
            return ""
        job.deadline = module.time.monotonic() - 1
        raise TimeoutError("expired work")
    monkeypatch.setattr(job, "cli", cli)
    monkeypatch.setattr(job, "execute", execute)
    with pytest.raises(TimeoutError, match="expired work"):
        job.run()
    assert job.data["failure_artifacts_downloaded"]
    assert job.data["outcome"] == "failed"
    assert job.data["cleanup_outcome"] == "released"
    assert set(job.data["timings"]) == {"deployment", "execution", "download", "release"}
    if batch:
        remote = executions[-1]
        assert "factorlab.experiments.research_batch" in remote
        assert "--timeout" in remote and "--cu-rate-per-hour" in remote and "--cu-balance" in remote
        assert sum("pip" in code for code in executions) == 1
    assert ("stop", "-s", "fl-test") in calls
