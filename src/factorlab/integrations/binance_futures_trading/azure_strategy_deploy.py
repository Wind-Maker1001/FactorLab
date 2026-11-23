"""Azure Demo strategy inbox controller. No credentials, budgets, or ledger restores."""
import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import subprocess
import sys

from .strategy_deployment import DeploymentStore
from .strategy_package import build_package, install_package, load_package
from .champion_release_adapter import adapt_selection


def accept(root, archive, candidate, *, mode='hot'):
    release, manifest = install_package(archive, root / 'strategy-releases')
    with closing(DeploymentStore(root / 'strategy-deployment.sqlite3')) as store:
        return store.request(manifest, candidate, mode=mode)


def cycle(root, inbox, *, mode='hot'):
    results = []
    for candidate_file in sorted(inbox.glob('*.candidate.json')):
        # Selector publishes complete envelopes by atomic rename. No destructive consumption.
        try:
            envelope = json.loads(candidate_file.read_bytes())
            if 'selection' in envelope and envelope['selection'].get('promoted') is not True:
                adapt_selection(envelope['selection'], {})
                results.append(dict(file=candidate_file.name, accepted=False, reason='RETAIN_CHAMPION'))
                continue
            archive_name = envelope['archive']
            if Path(archive_name).name != archive_name or not archive_name.endswith('.zip'):
                raise ValueError('inbox archive must be a local zip filename')
            archive = inbox / archive_name
            if 'source' in envelope:
                # A trusted producer may publish an already-staged source tree instead of a zip.
                source = (inbox / envelope['source']).resolve()
                if not source.is_relative_to(inbox.resolve()) or source == inbox.resolve():
                    raise ValueError('source must be below the trusted inbox')
                if not archive.exists():
                    build_package(source, envelope['manifest'], archive)
            if 'selection' in envelope:
                _, manifest = install_package(archive, root / 'strategy-releases')
                candidate = adapt_selection(envelope['selection'], manifest)
                if candidate is None:
                    results.append(dict(file=candidate_file.name, accepted=False, reason='RETAIN_CHAMPION'))
                    continue
            else:
                candidate = envelope['candidate']
            switch = accept(root, archive, candidate, mode=mode)
        except Exception as error:
            results.append(dict(file=candidate_file.name, accepted=False, reason=type(error).__name__))
        else:
            results.append(dict(file=candidate_file.name, accepted=True, switch=switch))
    return results


def drive(root, inbox, mode):
    """Root timer delegates all package/SQLite work to the existing service user."""
    subprocess.run(['runuser', '-u', 'factorlab-demo', '--', sys.executable, '-m', __package__ +
        '.azure_strategy_deploy', 'cycle', '--state-root', str(root), '--inbox', str(inbox),
        '--mode', mode], check=True, capture_output=True, text=True)
    import sqlite3
    path = root / 'strategy-deployment.sqlite3'
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as db:
        row = db.execute("SELECT boot FROM switches WHERE phase='READY_RESTART'").fetchone()
    if row:
        # Once the PID changes, the owner completes the switch after GET recovery.
        pid = subprocess.check_output(['systemctl', 'show', 'factorlab-demo-execute.service',
                                       '-p', 'MainPID', '--value'], text=True).strip()
        if pid != '0' and pid == row[0]:
            subprocess.run(['systemctl', 'restart', 'factorlab-demo-execute.service'], check=True)
            return dict(event='STRATEGY_CONTROLLED_RESTART', old_pid=pid)
    return dict(event='STRATEGY_INBOX_CYCLE')


def main(argv=None):
    if os.name != 'nt':
        os.umask(0o077)
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('action', choices=('build', 'accept', 'cycle', 'drive', 'status', 'rollback'))
    p.add_argument('--state-root', type=Path)
    p.add_argument('--source', type=Path)
    p.add_argument('--manifest', type=Path)
    p.add_argument('--archive', type=Path)
    p.add_argument('--candidate', type=Path)
    p.add_argument('--inbox', type=Path)
    p.add_argument('--mode', choices=('hot', 'restart'), default='hot')
    args = p.parse_args(argv)
    if args.action == 'build':
        result = build_package(args.source, json.loads(args.manifest.read_bytes()), args.archive)
    elif args.action == 'accept':
        result = dict(switch=accept(args.state_root, args.archive,
                      json.loads(args.candidate.read_bytes()), mode=args.mode))
    elif args.action == 'cycle':
        result = cycle(args.state_root, args.inbox, mode=args.mode)
    elif args.action == 'drive':
        result = drive(args.state_root, args.inbox, args.mode)
    else:
        with closing(DeploymentStore(args.state_root / 'strategy-deployment.sqlite3')) as store:
            if args.action == 'rollback':
                store.rollback('OPERATOR_RESTORE_PREVIOUS_STABLE')
            result = store.snapshot()
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
