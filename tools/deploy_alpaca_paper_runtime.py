"""Deploy a fixed Paper release; optional DPAPI secret delivery uses SSH stdin."""
import argparse
import json
from pathlib import Path
import re
import subprocess
import tempfile


def deploy(args):
    repo = Path(args.repo).resolve()
    revision = subprocess.check_output(['git', '-C', str(repo), 'rev-parse', args.commit + '^{commit}'], text=True).strip()
    if not re.fullmatch('[0-9a-f]{40}', revision):
        raise ValueError('full commit required')
    ssh = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=15', '-i', args.key,
           f'{args.user}@{args.host}']
    remote = f'/tmp/factorlab-alpaca-{revision}'
    subprocess.run(ssh + [f'umask 077; mkdir {remote}'], check=True)
    with tempfile.TemporaryDirectory(prefix='factorlab-alpaca-') as directory:
        bundle = Path(directory) / 'release.tar'
        subprocess.run(['git', '-C', str(repo), 'archive', '-o', str(bundle), revision], check=True)
        subprocess.run(['scp', '-q', '-i', args.key, str(bundle),
                        str(repo / 'tools/install_alpaca_paper_runtime.sh'),
                        f'{args.user}@{args.host}:{remote}/'], check=True)
    subprocess.run(ssh + [f'sudo sh {remote}/install_alpaca_paper_runtime.sh {remote}/release.tar '
                          f'{revision} /opt/factorlab/python/cpython-3.13-linux-x86_64-gnu/bin/python3'], check=True)
    if args.credential_file:
        from factorlab.integrations.alpaca.market_data.client import Credentials
        credentials = Credentials.from_dpapi(args.credential_file)
        payload = json.dumps({'APCA_API_KEY_ID': credentials.key,
                              'APCA_API_SECRET_KEY': credentials.secret,
                              'APCA_API_BASE_URL': 'https://paper-api.alpaca.markets'}).encode()
        # No values in argv, environment, stdout, local plaintext files, or logs.
        command = ('sudo sh -c \'set -eu; umask 077; '
                   'test ! -e /etc/factorlab-alpaca-paper/paper-secret.json; '
                   'cat > /etc/factorlab-alpaca-paper/paper-secret.json; '
                   'chmod 600 /etc/factorlab-alpaca-paper/paper-secret.json\'')
        result = subprocess.run(ssh + [command], input=payload, capture_output=True)
        if result.returncode:
            raise RuntimeError('private credential delivery failed')
        subprocess.run(ssh + ['sudo systemctl start factorlab-alpaca-paper.service '
                              'factorlab-alpaca-paper-health.timer'], check=True)
    print(json.dumps({'deployed_revision': revision, 'host': args.host,
                      'credential_delivered': bool(args.credential_file)}))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--repo', required=True)
    parser.add_argument('--commit', required=True)
    parser.add_argument('--host', required=True)
    parser.add_argument('--user', default='azureuser')
    parser.add_argument('--key', required=True)
    parser.add_argument('--credential-file')
    deploy(parser.parse_args())


if __name__ == '__main__':
    main()
