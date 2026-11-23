"""C08-compatible versioned runtime installation and same-ledger code rollback."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import time


UNIT = 'factorlab-demo-execute.service'
STATE = Path('/var/lib/factorlab-demo/execute-state')
DROPIN = Path('/etc/systemd/system') / (UNIT + '.d') / '20-release.conf'


def command(*args):
    return subprocess.run(args, check=True, text=True, capture_output=True).stdout.strip()


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--commit', required=True)
    p.add_argument('--archive', type=Path, required=True)
    p.add_argument('--sha256', required=True)
    args = p.parse_args()
    if (len(args.commit) != 40 or any(c not in '0123456789abcdef' for c in args.commit)
            or hashlib.sha256(args.archive.read_bytes()).hexdigest() != args.sha256):
        raise ValueError('runtime archive identity mismatch')
    # A second installer cannot race the service restart or rollback.
    import fcntl
    with open('/var/lock/factorlab-strategy-install.lock', 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        release = Path('/opt/factorlab/releases') / args.commit
        source = release / 'source'
        baseline = DROPIN.read_text()
        old_python = baseline.split('ExecStart=')[-1].split()[0]
        old_venv = Path(old_python).parent.parent
        # C08 uses bin/factorlab-demo; later releases use bin/python -m ... .
        old_pid = int(command('systemctl', 'show', UNIT, '-p', 'MainPID', '--value'))
        before = json.loads((STATE / 'cloud-health.json').read_bytes())
        inodes = {str(f.relative_to(STATE)): f.stat().st_ino for f in STATE.rglob('*.sqlite3')}
        if not source.exists():
            source.mkdir(parents=True)
            with tarfile.open(args.archive) as archive:
                archive.extractall(source, filter='data')
            shutil.copytree(old_venv, release / 'venv', symlinks=True)
            site = next((release / 'venv/lib').glob('python*/site-packages'))
            # Retain the exact existing dependencies, install this source via a dedicated path entry.
            for candidate in site.glob('factorlab*'):
                if candidate.is_dir(): shutil.rmtree(candidate)
                else: candidate.unlink()
            (site / 'factorlab-release.pth').write_text(str(source / 'src') + '\n')
        python = str(release / 'venv/bin/python')
        command('runuser', '-u', 'factorlab-demo', '--', python, '-c',
                'from factorlab.integrations.binance_futures_trading.demo_strategy_release import ReleaseConsumer')
        new_config = ('[Service]\nExecStart=\nExecStart=' + python +
            ' -m factorlab.integrations.binance_futures_trading.demo_grid_supervisor start --execute\n')
        temporary = DROPIN.with_suffix('.tmp')
        temporary.write_text(new_config)
        os.replace(temporary, DROPIN)
        command('systemctl', 'daemon-reload')
        started = time.time()
        try:
            command('systemctl', 'restart', UNIT)
            accepted = None
            for _ in range(45):
                health = json.loads((STATE / 'cloud-health.json').read_bytes())
                pid = int(command('systemctl', 'show', UNIT, '-p', 'MainPID', '--value'))
                if (pid != old_pid and pid == health['pid'] and health.get('account_recovered')
                        and health.get('unknown_count') == 0 and health['heartbeat_at'] >= started):
                    accepted = health
                    break
                time.sleep(2)
            if accepted is None:
                raise RuntimeError('new release failed current-ledger GET recovery')
        except Exception:
            temporary.write_text(baseline)
            os.replace(temporary, DROPIN)
            command('systemctl', 'daemon-reload')
            command('systemctl', 'restart', UNIT)
            # Never copy a database; a failed recovery remains visible to C06.
            raise
        after_inodes = {str(f.relative_to(STATE)): f.stat().st_ino for f in STATE.rglob('*.sqlite3')}
        for unit, action in [('factorlab-demo-health.service', 'monitor'),
                             ('factorlab-demo-daily-report.service', 'report --write-report --markdown')]:
            directory = Path('/etc/systemd/system') / (unit + '.d')
            directory.mkdir(exist_ok=True)
            (directory / '20-strategy-release.conf').write_text('[Service]\nExecStart=\nExecStart=' + python +
                ' -m factorlab.integrations.binance_futures_trading.demo_cloud_runtime ' + action + '\n')
        for name in ('factorlab-demo-strategy-deploy.service', 'factorlab-demo-strategy-deploy.timer'):
            text = (source / 'deploy/systemd' / name).read_text()
            text = text.replace('/opt/factorlab/venv/bin/python', python)
            (Path('/etc/systemd/system') / name).write_text(text)
        command('install', '-d', '-o', 'factorlab-demo', '-g', 'factorlab-demo', '-m', '0700',
                '/var/lib/factorlab-demo/champions')
        command('systemctl', 'daemon-reload')
        command('systemctl', 'enable', '--now', 'factorlab-demo-strategy-deploy.timer')
        result = dict(event='RUNTIME_DEPLOYED', commit=args.commit, old_pid=old_pid, pid=accepted['pid'],
            recovery_seconds=time.time() - started, ledger_inodes_preserved=all(after_inodes.get(k) == v for k,v in inodes.items()),
            before_budget=before.get('global_budget'), after_budget=accepted.get('global_budget'),
            unknown=accepted.get('unknown_count'), status=accepted.get('status'))
        receipt = release / 'deployment-result.json'
        receipt.write_text(json.dumps(result, sort_keys=True) + '\n')
        print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    main()
