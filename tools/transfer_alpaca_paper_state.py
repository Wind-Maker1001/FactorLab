"""Transfer stopped-owner SQLite snapshots without discarding cloud observation state."""
import argparse
from pathlib import Path
import sqlite3
import subprocess
import tempfile
from uuid import uuid4


def transfer(args):
    source = Path(args.source).resolve()
    ssh = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=15', '-i', args.key,
           f'{args.user}@{args.host}']
    remote = '/tmp/factorlab-alpaca-handover-' + uuid4().hex
    subprocess.run(ssh + [f'umask 077; mkdir {remote}'], check=True)
    with tempfile.TemporaryDirectory(prefix='alpaca-handover-') as directory:
        snapshots = []
        for name in ('broker.sqlite', 'portfolio.sqlite'):
            target = Path(directory) / name
            with sqlite3.connect((source / name).as_uri() + '?mode=ro', uri=True) as original:
                with sqlite3.connect(target) as snapshot:
                    original.backup(snapshot)
            snapshots.append(str(target))
        subprocess.run(['scp', '-q', '-i', args.key, *snapshots,
                        f'{args.user}@{args.host}:{remote}/'], check=True)
    # An explicit stop suppresses systemd automatic restart during handover.
    # The observer journal is retained, not overwritten without a backup.
    script = f'''set -eu
root=/var/lib/factorlab-alpaca-paper
test ! -e "$root/portfolio.sqlite3"
systemctl stop factorlab-alpaca-paper.service
install -d -o factorlab-alpaca -g factorlab-alpaca -m 700 "$root/handover"
exec 9>"$root/owner.lock"
flock -n 9
if [ -f "$root/account.sqlite3" ]; then
    mv "$root/account.sqlite3" "$root/handover/observer-account.sqlite3"
fi
install -o factorlab-alpaca -g factorlab-alpaca -m 600 {remote}/broker.sqlite "$root/account.sqlite3"
install -o factorlab-alpaca -g factorlab-alpaca -m 600 {remote}/portfolio.sqlite "$root/portfolio.sqlite3"
flock -u 9
systemctl start factorlab-alpaca-paper.service
'''
    subprocess.run(ssh + ['sudo sh -s'], input=script.encode(), check=True)
    print('Transferred broker and portfolio journals; previous observation journal retained.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True)
    parser.add_argument('--host', required=True)
    parser.add_argument('--key', required=True)
    parser.add_argument('--user', default='azureuser')
    transfer(parser.parse_args())


if __name__ == '__main__':
    main()
