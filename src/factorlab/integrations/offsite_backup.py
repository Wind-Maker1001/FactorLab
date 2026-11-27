"""Pull existing coherent snapshots over SSH and publish only verified copies."""

import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from time import monotonic

from .binance_futures_trading import demo_cloud_storage as storage
from .binance_futures_trading.demo_cloud_backup import _BACKUP_NAME_RE, parse_backup_timestamp
from .binance_futures_trading.demo_runtime import operator_lock
from .binance_futures_trading.protocol import SafetyBlocked


def _snapshot_key(name):
    stamp = parse_backup_timestamp(name)
    if stamp is not None:
        return stamp, int(_BACKUP_NAME_RE.fullmatch(name).group(2) or 0)


def _rsync(remote_shell, *args):
    return subprocess.run(
        ['rsync', '--timeout=120', '--stats', f'--rsh={remote_shell}', *args],
        env={**os.environ, 'LC_ALL': 'C'}, capture_output=True, text=True, check=True,
        timeout=900,
    ).stdout


def _stat_bytes(output, label):
    match = re.search(rf'^{label}: ([0-9,]+)', output, re.MULTILINE)
    if match is None:
        raise ValueError(f'rsync statistics missing {label}')
    return int(match.group(1).replace(',', ''))


def _latest_snapshot(listing):
    candidates = []
    for line in listing.splitlines():
        fields = line.split(maxsplit=4)
        if len(fields) == 5 and fields[0].startswith('d') and _snapshot_key(fields[4]):
            candidates.append(fields[4])
    if not candidates:
        raise SafetyBlocked('the offsite source has no published snapshots')
    return max(candidates, key=_snapshot_key)


def _verify_market(path, market):
    manifest = storage.verify_snapshot(path)
    if manifest['market'] != market:
        raise SafetyBlocked('offsite snapshot belongs to a different market')
    return manifest


def _prune(retained, keep):
    for obsolete in retained[keep:]:
        shutil.rmtree(obsolete)


def _use_snapshot(root, source, remote_shell, market, keep, max_snapshot_bytes):
    destination = root / PurePosixPath(source.rsplit(':', 1)[1]).name
    retained = sorted(
        (path for path in root.iterdir() if path.is_dir() and _snapshot_key(path.name)),
        key=lambda path: _snapshot_key(path.name), reverse=True,
    )
    if destination.exists():
        manifest = _verify_market(destination, market)
        if destination == retained[0]:
            _prune(retained, keep)
        return destination, manifest, None, []
    if retained and _snapshot_key(destination.name) <= _snapshot_key(retained[0].name):
        raise SafetyBlocked('offsite source is older than the latest retained snapshot')

    staging = root / ('.incoming-' + destination.name)
    for partial in root.glob('.incoming-backup-*'):
        if partial != staging and _snapshot_key(partial.name.removeprefix('.incoming-')):
            shutil.rmtree(partial)
    storage._root(staging, create=True)

    source = source.rstrip('/') + '/'
    target = str(staging) + os.sep
    inventory = _rsync(remote_shell, '--recursive', '--dry-run', '--', source, target)
    size = _stat_bytes(inventory, 'Total file size')
    if size > max_snapshot_bytes:
        raise SafetyBlocked('offsite snapshot exceeds the configured byte limit')
    # A resumed transfer may have both a partial file and its replacement on disk.
    if shutil.disk_usage(root).free < 2 * size:
        raise SafetyBlocked('insufficient space for the offsite snapshot and transfer workspace')

    copy_basis = [f'--copy-dest={retained[0]}'] if retained else []
    transfer = _rsync(
        remote_shell, '--recursive', '--times', '--perms', '--chmod=D700,F600',
        '--checksum', '--compress', '--partial', '--delete', '--fsync',
        f'--max-size={max_snapshot_bytes}', *copy_basis, '--', source, target,
    )
    manifest = _verify_market(staging, market)
    for directory, _, _ in os.walk(staging, topdown=False, onerror=storage._walk_error):
        storage._sync_directory(Path(directory))
    storage._publish(staging, destination)
    _prune([destination, *retained], keep)
    return destination, manifest, size, [inventory, transfer]


def pull_snapshot(*, root, host, source, market, keep, max_snapshot_bytes, latest=False,
                  ssh_config=None, max_age_hours=None):
    """Receive a published immutable snapshot under the destination's local lock."""
    if keep < 1 or max_snapshot_bytes < 1:
        raise ValueError('offsite retention and byte limit must be positive')
    if max_age_hours is not None and max_age_hours < 1:
        raise ValueError('maximum snapshot age must be positive')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', host):
        raise ValueError('host must be an SSH configuration alias')
    if not re.fullmatch(r'[A-Za-z0-9_./-]+', source) or '..' in PurePosixPath(source).parts:
        raise ValueError('source must be a literal SSH-side snapshot path')
    if not latest and _snapshot_key(PurePosixPath(source).name) is None:
        raise ValueError('source must name a published backup directory')
    remote_shell = (
        'ssh -o BatchMode=yes -o StrictHostKeyChecking=yes -o ClearAllForwardings=yes '
        '-o ConnectTimeout=15 -o ServerAliveInterval=15 -o ServerAliveCountMax=2'
    )
    if ssh_config is not None:
        if not re.fullmatch(r'/[A-Za-z0-9_./-]+', ssh_config):
            raise ValueError('SSH config must be an absolute path without whitespace')
        remote_shell += f' -F {ssh_config}'

    started = monotonic()
    root = storage._root(root, create=True)
    exchanges = []
    with operator_lock(root):
        if latest:
            listing = _rsync(remote_shell, '--list-only', '--', f"{host}:{source.rstrip('/')}/")
            exchanges.append(listing)
            source = str(PurePosixPath(source) / _latest_snapshot(listing))
        path, manifest, size, transfer_exchanges = _use_snapshot(
            root, f'{host}:{source}', remote_shell, market, keep, max_snapshot_bytes,
        )
        exchanges.extend(transfer_exchanges)
    captured = datetime.fromisoformat(manifest['captured_at'])
    if max_age_hours is not None and datetime.now(timezone.utc) - captured > timedelta(hours=max_age_hours):
        raise SafetyBlocked('the latest verified offsite snapshot is overdue')
    return {
        'status': 'PUBLISHED' if size is not None else 'ALREADY_VERIFIED',
        'market': market,
        'snapshot': path.name,
        'captured_at': manifest['captured_at'],
        'snapshot_bytes': size,
        'rsync_sent_bytes': sum(_stat_bytes(output, 'Total bytes sent') for output in exchanges),
        'rsync_received_bytes': sum(_stat_bytes(output, 'Total bytes received') for output in exchanges),
        'elapsed_seconds': monotonic() - started,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    pull = parser.add_subparsers(dest='command', required=True).add_parser('pull')
    pull.add_argument('--root', type=Path, required=True)
    pull.add_argument('--host', required=True)
    pull.add_argument('--source', required=True)
    pull.add_argument('--market', choices=('binance', 'alpaca'), required=True)
    pull.add_argument('--keep', type=int, default=2)
    pull.add_argument('--max-snapshot-bytes', type=int, required=True)
    pull.add_argument('--latest', action='store_true')
    pull.add_argument('--ssh-config')
    pull.add_argument('--max-age-hours', type=int)
    args = vars(parser.parse_args(argv))
    del args['command']

    try:
        result = pull_snapshot(**args)
    except subprocess.CalledProcessError as error:
        result = {'status': 'FAILED', 'error': f'rsync exited {error.returncode}'}
    except subprocess.TimeoutExpired:
        result = {'status': 'FAILED', 'error': 'rsync exceeded transfer time limit'}
    except (OSError, SafetyBlocked, ValueError, sqlite3.DatabaseError) as error:
        result = {'status': 'FAILED', 'error': str(error)}
    if result['status'] == 'FAILED':
        print(json.dumps(result), file=sys.stderr)
        return 1
    print(json.dumps(result))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
