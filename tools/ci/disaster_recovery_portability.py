"""Produce x64 SQLite backups and restore the same bytes on the other architecture."""

import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import platform
from random import Random
import resource
import sqlite3
from time import monotonic, process_time

from factorlab.integrations.binance_futures_trading import demo_cloud_storage as storage


EXPECTED = ('HALTED', '23.45678901', 17, 'already-dispatched-client-id')
MARKETS = ('binance', 'alpaca')


def export(root, revision, payload_mib):
    result = {}
    payload = Random(904).randbytes(1024)
    for market in MARKETS:
        state = storage._root(root / (market + '-source'), create=True)
        with closing(sqlite3.connect(state / 'runtime.sqlite3')) as db, db:
            db.execute('CREATE TABLE workflow(control TEXT, owned_cash TEXT, spent INTEGER, order_id TEXT)')
            db.execute('INSERT INTO workflow VALUES(?,?,?,?)', EXPECTED)
            db.execute('CREATE TABLE sample_dispatch_evidence(receipt BLOB)')
            db.executemany('INSERT INTO sample_dispatch_evidence VALUES(?)',
                           ((payload,) for _ in range(payload_mib * 1024)))
        started = monotonic()
        snapshot = storage.recovery_snapshot(state, root / market, market, revision)
        result[market] = {
            'snapshot_seconds': monotonic() - started,
            'snapshot_bytes': sum(path.stat().st_size for path in snapshot.rglob('*') if path.is_file()),
        }
    return result


def restore(root, payload_mib):
    result = {}
    for market in MARKETS:
        snapshot = root / market
        started = monotonic()
        manifest = storage.verify_snapshot(snapshot)
        restored = storage.restore_verified_state(snapshot, root / (market + '-restored'), manifest)
        with closing(sqlite3.connect(restored / 'runtime.sqlite3')) as db:
            assert db.execute('SELECT * FROM workflow').fetchone() == EXPECTED
            assert db.execute('SELECT count(*) FROM sample_dispatch_evidence').fetchone() == (payload_mib * 1024,)
        assert (restored / storage._QUARANTINE).is_file()
        result[market] = {'verify_restore_seconds': monotonic() - started}
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('export', 'restore'))
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--revision')
    parser.add_argument('--payload-mib', type=int, default=1)
    args = parser.parse_args()
    os.umask(0o077)
    root = storage._root(args.root, create=True)
    cpu = process_time()
    result = (export(root, args.revision, args.payload_mib) if args.command == 'export'
              else restore(root, args.payload_mib))
    result.update(architecture=platform.machine(), sqlite=sqlite3.sqlite_version,
                  max_rss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                  cpu_seconds=process_time() - cpu)
    print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    main()
