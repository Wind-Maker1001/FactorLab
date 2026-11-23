"""Deploy a pinned FactorLab runtime to the existing Azure Demo service over SSH.

Build a Git archive before invoking. The remote installer retains all service controls.
Package publication is separate: azure_strategy_deploy accepts the selector envelope.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shlex
import subprocess
import tempfile


def run(argv, **kwargs):
    result = subprocess.run(argv, text=True, capture_output=True, **kwargs)
    if result.returncode:
        raise RuntimeError(f'{Path(argv[0]).name} failed ({result.returncode}): {result.stderr[-1500:]}')
    return result.stdout


def deploy(host, user, key, commit, repo):
    if len(commit) != 40 or any(c not in '0123456789abcdef' for c in commit):
        raise ValueError('full runtime commit required')
    ssh = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=15', '-i', str(key), f'{user}@{host}']
    with tempfile.TemporaryDirectory(prefix='factorlab-release-') as directory:
        archive = Path(directory) / (commit + '.tar.gz')
        run(['git', '-C', str(repo), 'archive', '--format=tar.gz', '-o', str(archive), commit])
        fingerprint = hashlib.sha256(archive.read_bytes()).hexdigest()
        remote = '/tmp/factorlab-' + commit + '.tar.gz'
        for attempt in range(3):
            try:
                run(['scp', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=15', '-i', str(key),
                     str(archive), f'{user}@{host}:{remote}'])
                break
            except RuntimeError:
                if attempt == 2: raise
        installer = Path(__file__).with_name('install_demo_strategy_runtime.py')
        run(['scp', '-i', str(key), str(installer), f'{user}@{host}:/tmp/install_demo_strategy_runtime.py'])
        output = run(ssh + ['sudo', '/opt/factorlab/python/cpython-3.13-linux-x86_64-gnu/bin/python3',
            '/tmp/install_demo_strategy_runtime.py', '--commit', commit, '--archive', remote,
            '--sha256', fingerprint])
    return json.loads(output)


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--host', required=True)
    p.add_argument('--user', default='azureuser')
    p.add_argument('--key', type=Path, required=True)
    p.add_argument('--commit', required=True)
    p.add_argument('--repo', type=Path, default=Path.cwd())
    args = p.parse_args()
    print(json.dumps(deploy(args.host, args.user, args.key, args.commit, args.repo), sort_keys=True))
