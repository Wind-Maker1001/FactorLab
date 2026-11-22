"""Exercise the backup recovery hook under a real systemd manager."""

import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
from uuid import uuid4

import pytest

from factorlab.integrations.binance_futures_trading import demo_cloud_backup as backup


pytestmark = pytest.mark.skipif(
    sys.platform != 'linux' or os.environ.get('FACTORLAB_TEST_SYSTEMD') != '1',
    reason='requires opt-in on an isolated Linux systemd host with root privileges',
)


def _systemctl(*arguments):
    return subprocess.check_output(['systemctl', *arguments], text=True, timeout=30).strip()


def _main_pid(unit):
    return int(_systemctl('show', '-p', 'MainPID', '--value', unit))


_HANGING_CHILD = '''
import fcntl
import signal
import sys

signal.signal(signal.SIGTERM, signal.SIG_IGN if sys.argv[2] == 'timeout' else signal.SIG_DFL)
with open(sys.argv[1], 'a') as lock:
    fcntl.flock(lock, fcntl.LOCK_EX)
    print('locked', flush=True)
    signal.pause()
'''


class _FaultRunner(backup.SystemdRunner):
    def read_health(self, state_root, *, unit=None):
        pid = _main_pid(unit)
        assert pid > 0
        return {
            'mode': 'EXECUTE',
            'status': 'RUNNING',
            'desired': 'RUNNING',
            'pid': pid,
            'alive': True,
            'ready': True,
            'execute_healthy': True,
            'account_recovered': True,
            'market_fresh': True,
            'unknown_count': 0,
            'global_budget': {'consumed': {'posts': 7, 'submitted_notional': '41.3308000'}},
            'systemd': {'active_state': 'active', 'sub_state': 'running', 'main_pid': pid},
        }

    def run_python_module(self, module, args, **kwargs):
        if args[0] != 'backup':
            return super().run_python_module(module, args, **kwargs)

        mode = os.environ['C07_FAULT_TEST_MODE']
        fault_dir = Path(os.environ['C07_FAULT_TEST_DIR'])
        if mode == 'timeout':
            def record_sigterm(*_):
                (fault_dir / 'received-sigterm').touch()

            signal.signal(signal.SIGTERM, record_sigterm)
        else:
            signal.signal(signal.SIGTERM, signal.SIG_DFL)

        lock_path = fault_dir / 'backup.lock'
        with subprocess.Popen(
            [sys.executable, '-c', _HANGING_CHILD, str(lock_path), mode],
            stdout=subprocess.PIPE,
            text=True,
        ) as child:
            assert child.stdout.readline().strip() == 'locked'
            if mode == 'sigterm':
                os.kill(os.getpid(), signal.SIGTERM)
            child.wait()
        raise AssertionError('backup child exited before systemd terminated the service')


@pytest.fixture
def transient_units():
    suffix = uuid4().hex
    backup_unit = f'factorlab-c07-test-backup-{suffix}.service'
    owner_unit = f'factorlab-c07-test-owner-{suffix}.service'
    try:
        yield backup_unit, owner_unit
    finally:
        # Let ExecStopPost finish before tearing down the unit it can restore.
        for unit in (backup_unit, owner_unit):
            subprocess.run(['systemctl', 'stop', unit], check=False, capture_output=True, timeout=30)
        subprocess.run(['systemctl', 'reset-failed', backup_unit], check=False, capture_output=True, timeout=30)


@pytest.mark.parametrize(('mode', 'expected_result', 'expected_signal'), [
    ('sigterm', 'signal', signal.SIGTERM),
    ('timeout', 'timeout', signal.SIGKILL),
])
def test_systemd_restores_original_unit_after_backup_process_is_killed(
    tmp_path, transient_units, mode, expected_result, expected_signal,
):
    import fcntl

    backup_unit, owner_unit = transient_units
    state_root = tmp_path / 'state'
    backup_root = tmp_path / 'backups'
    state_root.mkdir(mode=0o700)
    backup_root.mkdir(mode=0o700)
    subprocess.run([
        'systemd-run', f'--unit={owner_unit}', '--property=Type=exec', '--',
        sys.executable, '-c', 'import signal; signal.pause()',
    ], check=True, capture_output=True, text=True, timeout=30)
    original_pid = _main_pid(owner_unit)

    source_root = Path(__file__).resolve().parents[2] / 'src'
    python_path = os.pathsep.join(filter(None, (str(source_root), os.environ.get('PYTHONPATH'))))
    helper = [sys.executable, str(Path(__file__).resolve())]
    common = [
        '--state-root', str(state_root), '--backup-root', str(backup_root),
        '--service-user', 'root', '--reconcile-timeout', '5',
    ]
    post_stop = shlex.join([*helper, 'recover-scheduled', *common])
    result = subprocess.run([
        'systemd-run', '--wait', '--pipe', f'--unit={backup_unit}',
        '--property=Type=oneshot', '--property=KillMode=control-group',
        # Keep the stopped transient owner loaded until ExecStopPost can start it again.
        f'--property=Wants={owner_unit}',
        f'--property=RuntimeDirectory={backup_unit.removesuffix(".service")}',
        '--property=RuntimeDirectoryMode=0700',
        '--property=TimeoutStartSec=8s', '--property=TimeoutStartFailureMode=abort',
        '--property=WatchdogSignal=SIGTERM', '--property=TimeoutAbortSec=1s',
        '--property=TimeoutStopSec=20s', f'--property=ExecStopPost={post_stop}',
        f'--setenv=PYTHONPATH={python_path}', f'--setenv=C07_FAULT_TEST_MODE={mode}',
        f'--setenv=C07_FAULT_TEST_DIR={tmp_path}', '--',
        *helper, 'run-scheduled', *common, '--units', owner_unit, '--min-free-bytes', '0',
    ], capture_output=True, text=True, timeout=35)
    assert result.returncode != 0
    unit_result = _systemctl('show', '-p', 'Result', '-p', 'ExecMainStatus', backup_unit)
    assert f'Result={expected_result}' in unit_result.splitlines(), result.stderr
    assert f'ExecMainStatus={expected_signal.value}' in unit_result.splitlines(), result.stderr
    if mode == 'timeout':
        assert (tmp_path / 'received-sigterm').is_file()
    with (tmp_path / 'backup.lock').open('r') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)

    status = json.loads((backup_root / backup.STATUS_FILENAME).read_text(encoding='utf-8'))
    assert status['status'] == 'FAILED'
    assert status['error_category']
    maintenance = status['maintenance_window']
    assert maintenance['interrupted_unit'] == owner_unit
    assert maintenance['restart_succeeded'] is True
    assert maintenance['reconcile_verified'] is True
    assert maintenance['pre_health']['pid'] == original_pid
    recovered_pid = _main_pid(owner_unit)
    assert recovered_pid != original_pid
    assert maintenance['post_health']['pid'] == recovered_pid
    assert maintenance['post_health']['unknown_count'] == 0
    assert maintenance['post_health']['consumed_posts'] == 7
    assert maintenance['post_health']['consumed_submitted_notional'] == '41.3308000'


if __name__ == '__main__':
    backup.SystemdRunner = _FaultRunner
    raise SystemExit(backup.main([
        *sys.argv[1:], '--recovery-directory', os.environ['RUNTIME_DIRECTORY'],
    ]))
