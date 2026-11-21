"""Exercise the installed offline acceptance unit on a disposable Linux state root."""
import json
import os
from pathlib import Path
import platform
import signal
import subprocess
from datetime import datetime, timezone


ROOT = '/var/lib/factorlab-demo'
PYTHON = '/opt/factorlab/venv/bin/python'
MODULE = 'factorlab.integrations.binance_futures_trading.demo_cloud_runtime'


def systemctl(*args):
    return subprocess.check_output(['sudo', 'systemctl', *args], text=True).strip()


def as_service(*args):
    return subprocess.check_output(['sudo', '-u', 'factorlab-demo', 'env',
        f'FACTORLAB_DEMO_STATE_ROOT={ROOT}', PYTHON, *args], text=True).strip()


def snapshot():
    return json.loads(as_service('-c',
        "from pathlib import Path; print(Path('/var/lib/factorlab-demo/cloud-health.json').read_text())"))


def main():
    signal.alarm(180)
    unit = 'factorlab-demo-ci.service'
    evidence = dict(commit=os.environ['GITHUB_SHA'], kernel=platform.release(),
        platform=platform.platform(), python=as_service('--version'),
        systemd=subprocess.check_output(['systemctl', '--version'], text=True).splitlines()[0],
        started_at=datetime.now(timezone.utc).isoformat(), mode='OFFLINE',
        cloud='CLOUD_NOT_DEPLOYED', observation='NOT_OBSERVED')
    journal = subprocess.Popen(['sudo', 'journalctl', '--follow', '--lines=0',
        '--output=cat', '--unit', unit], stdout=subprocess.PIPE, text=True)
    try:
        as_service('-c', '''
from contextlib import closing
from decimal import Decimal
import json
from factorlab.integrations.binance_futures_trading.demo_runtime import DemoRuntime, state_root
with closing(DemoRuntime(state_root())) as runtime:
    binding = dict(account_id='offline-systemd-fixture', configuration={})
    authorization = runtime.start_continuous(binding, opening_equity=Decimal('1000'),
        stop_epoch=runtime.stop_epoch())
    campaign = dict(id='offline-systemd-fixture', journal_id=authorization['journal_id'], smoke=False)
    with runtime.db:
        runtime.db.execute('INSERT INTO campaigns VALUES (?, ?)', (campaign['id'], json.dumps(campaign)))
        runtime.db.execute('INSERT INTO dispatches VALUES (?, ?, ?, ?, NULL)',
            (campaign['id'], 'POST', 'historical-fixture', json.dumps(dict(quantity='1', price='5'))))
''')
        systemctl('start', unit)

        def heartbeat(previous_pid=None, status='OFFLINE'):
            for line in journal.stdout:
                if line.startswith('{'):
                    value = json.loads(line)
                    if (value.get('event') == 'cloud_health' and value['status'] == status
                            and str(value['pid']) != previous_pid):
                        return value
            raise RuntimeError('journal ended before worker heartbeat')

        heartbeat()
        before = snapshot()
        assert before['requests'] == dict(GET=0, POST=0, DELETE=0)
        # The real unit must exclude a second process under the same service identity.
        contender = subprocess.run(['sudo', '-u', 'factorlab-demo', PYTHON, '-m', MODULE,
            'run', '--state-root', ROOT, '--offline', '--seconds', '0.1'],
            capture_output=True, text=True, timeout=20)
        assert contender.returncode == 78, contender.stderr
        evidence['second_worker_exit'] = contender.returncode
        pid = systemctl('show', unit, '--property=MainPID', '--value')
        # Consume an actual SIGKILL, not a clean service stop.
        systemctl('kill', '--signal=SIGKILL', '--kill-whom=main', unit)
        heartbeat(pid)
        after = snapshot()
        assert str(after['pid']) != pid
        assert after['starts'] == before['starts'] + 1
        assert after['desired'] == before['desired'] == 'READ_ONLY'
        assert after['requests'] == dict(GET=0, POST=0, DELETE=0)
        consumed = dict(posts=1, submitted_notional='5')
        assert before['global_budget']['consumed'] == after['global_budget']['consumed'] == consumed
        assert after['global_budget']['remaining_posts'] == 99
        assert int(systemctl('show', unit, '--property=NRestarts', '--value')) >= 1
        systemctl('stop', unit)
        assert systemctl('show', unit, '--property=ActiveState', '--value') == 'inactive'
        assert systemctl('show', unit, '--property=ExecMainStatus', '--value') == '0'
        stopped = snapshot()
        assert stopped['status'] == 'EXITED'
        assert stopped['desired'] == 'READ_ONLY'
        integrity = as_service('-c', '''
from contextlib import closing
from pathlib import Path
import json, sqlite3
root = Path('/var/lib/factorlab-demo')
results = {}
for path in root.rglob('*.sqlite3'):
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
        results[path.relative_to(root).as_posix()] = db.execute('PRAGMA integrity_check').fetchall()
print(json.dumps(results))
''')
        integrity = json.loads(integrity)
        assert integrity and all(result == [['ok']] for result in integrity.values())
        assert as_service('-c', f"import os; print(os.access('{PYTHON}', os.W_OK))") == 'False'
        installation = json.loads(as_service('-c', '''
import importlib.metadata, json, os
from pathlib import Path
import factorlab
root = Path(factorlab.__file__).parent
paths = [root, *root.rglob('*')]
print(json.dumps(dict(version=importlib.metadata.version('factorlab'),
    writable=[str(path.relative_to(root)) for path in paths if os.access(path, os.W_OK)])))
'''))
        assert installation['writable'] == []
        assert as_service('-c', f"import os; print(oct(os.stat('{ROOT}').st_mode & 0o777))") == '0o700'
        # Verify durable STOP through a manual systemd start, without credentials.
        as_service('-c', '''
from contextlib import closing
from factorlab.integrations.binance_futures_trading.demo_runtime import DemoRuntime, state_root
with closing(DemoRuntime(state_root())) as runtime:
    runtime.stop()
''')
        systemctl('start', '--wait', unit)
        heartbeat(status='STOPPED')
        assert systemctl('show', unit, '--property=ActiveState', '--value') == 'inactive'
        assert systemctl('show', unit, '--property=ExecMainStatus', '--value') == '0'
        assert systemctl('show', unit, '--property=Result', '--value') == 'success'
        # systemd resets NRestarts on a manual start; zero proves this start did not restart.
        assert systemctl('show', unit, '--property=NRestarts', '--value') == '0'
        stop_report = snapshot()
        assert stop_report['desired'] == 'STOPPED'
        assert stop_report['requests'] == dict(GET=0, POST=0, DELETE=0)
        assert stop_report['global_budget']['consumed'] == consumed
        evidence.update(systemd_sigkill_restart=True, systemd_sigterm_stop=True,
            lock_exclusion=True, code_not_service_writable=True, state_mode='0700',
            persisted_consumption=consumed, installation=installation, operator_stop_natural_exit=True,
            before=before, after_restart=after, after_sigterm=stopped,
            after_operator_stop=stop_report, sqlite_integrity=integrity,
            ended_at=datetime.now(timezone.utc).isoformat())
        print(json.dumps(evidence))
    finally:
        evidence['service_state'] = systemctl('show', unit, '--property=ActiveState', '--value')
        Path('cloud-systemd-acceptance.json').write_text(json.dumps(evidence, indent=2) + '\n')
        systemctl('stop', unit)
        journal.terminate()
        journal.communicate(timeout=10)


if __name__ == '__main__':
    main()
