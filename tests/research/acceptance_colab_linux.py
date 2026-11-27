"""Explicit Linux acceptance: python -m pytest tests/research/acceptance_colab_linux.py."""

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


def test_account_lock_competition_and_crash_release(tmp_path):
    import sys
    with module.account_lock(tmp_path):
        contender = subprocess.run([sys.executable, "-c",
            "import fcntl,sys; f=open(sys.argv[1], 'a'); fcntl.flock(f, fcntl.LOCK_EX|fcntl.LOCK_NB)",
            str(tmp_path / "account.lock")], capture_output=True, timeout=10)
        assert contender.returncode != 0
        assert b"BlockingIOError" in contender.stderr
        with pytest.raises(module.AccountBusy):
            with module.account_lock(tmp_path):
                pytest.fail("parallel allocation admitted")
    child = subprocess.Popen([sys.executable, "-c",
        "import fcntl,sys,os; f=open(sys.argv[1], 'a'); fcntl.flock(f, fcntl.LOCK_EX); os._exit(9)",
        str(tmp_path / "account.lock")])
    assert child.wait(timeout=10) == 9
    with module.account_lock(tmp_path):
        pass


def test_busy_run_never_operates_or_cleans_other_job(job, monkeypatch):
    import sys
    monkeypatch.setattr(module.Path, "home", lambda: job.root)
    monkeypatch.setattr(sys, "argv", ["colab_job.py", "run", "--job", str(job.root)])
    monkeypatch.setattr(module, "operate", lambda *a: pytest.fail("busy task operated"))
    with module.account_lock(job.root / ".local/state/factorlab-colab"):
        with pytest.raises(SystemExit) as exc:
            module.main()
        assert exc.value.code == 75
    assert module.Job(job.root).data["state"] == "BUSY"
    assert not job.data.get("allocation_attempted")


