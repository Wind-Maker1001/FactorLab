"""Bounded research subprocesses, including their benchmark descendants."""
from contextlib import contextmanager
from pathlib import Path
import os
import signal
import subprocess


@contextmanager
def gpu_window(index):
    from factorlab.strategies.controller_lock import controller_lock
    root = Path.home()/'.cache/factorlab'
    root.mkdir(parents=True, exist_ok=True)
    with controller_lock(root/'research-gpu', str(index)) as acquired:
        if not acquired:
            raise RuntimeError('Another research task owns this GPU measurement window')
        yield


def run_process(command, *, timeout, **kwargs):
    options = ({'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == 'nt'
               else {'start_new_session': True})
    with subprocess.Popen(command, **options, **kwargs) as process:
        try:
            return process.wait(timeout=timeout)
        except BaseException:
            if os.name == 'nt':
                # taskkill traverses the Windows process tree, unlike Popen.kill().
                subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
            else:
                import psutil
                try:
                    descendants = psutil.Process(process.pid).children(recursive=True)
                except psutil.NoSuchProcess:
                    descendants = []
                for child in reversed(descendants):
                    try:
                        child.kill()
                    except psutil.NoSuchProcess:
                        pass  # Descendants may finish while the timeout is handled.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass  # Completion can race expiry of the parent's deadline.
            process.wait()
            raise
