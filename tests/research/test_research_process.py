"""Timeout must stop computation rather than just stop its caller waiting."""
import json
from pathlib import Path
import subprocess
import sys

import pytest

from factorlab.experiments.research_process import run_process


def test_timeout_terminates_descendants(tmp_path):
    import psutil
    pid = tmp_path/'child.json'
    script = tmp_path/'parent.py'
    script.write_text('import json, subprocess, sys, time\n'
        "child = subprocess.Popen([sys.executable, '-I', '-c', 'import time; time.sleep(60)'])\n"
        f"open({str(pid)!r}, 'w').write(json.dumps(child.pid))\n"
        'time.sleep(60)\n')
    with pytest.raises(subprocess.TimeoutExpired):
        run_process([sys.executable, '-I', str(script)], timeout=2)
    child_id = json.loads(pid.read_text())
    if psutil.pid_exists(child_id):
        child = psutil.Process(child_id)
        assert child.status() == psutil.STATUS_ZOMBIE
