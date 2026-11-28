"""Paired stopped/pinned SQLite snapshot measurement on disposable synthetic state."""

import argparse
from contextlib import closing
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
import sqlite3
import tempfile
from time import perf_counter

from factorlab.integrations.binance_futures_trading import demo_cloud_storage as storage


REVISION = '0' * 40


def peak_rss():
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [('cb', wintypes.DWORD), ('PageFaultCount', wintypes.DWORD),
                       *[(name, ctypes.c_size_t) for name in ('PeakWorkingSetSize', 'WorkingSetSize',
                          'QuotaPeakPagedPoolUsage', 'QuotaPagedPoolUsage', 'QuotaPeakNonPagedPoolUsage',
                          'QuotaNonPagedPoolUsage', 'PagefileUsage', 'PeakPagefileUsage')]]
        counters = Counters()
        counters.cb = ctypes.sizeof(counters)
        process = ctypes.windll.kernel32.GetCurrentProcess
        process.restype = wintypes.HANDLE
        query = ctypes.windll.psapi.GetProcessMemoryInfo
        query.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
        if not query(process(), ctypes.byref(counters), counters.cb):
            raise ctypes.WinError()
        return counters.PeakWorkingSetSize
    import resource
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024


def create(root, size):
    root.mkdir(mode=0o700)
    for name in ('account', 'deployments'):
        with closing(sqlite3.connect(root / f'{name}.sqlite3')) as db, db:
            os.chmod(root / f'{name}.sqlite3', 0o600)
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('CREATE TABLE ledger(id PRIMARY KEY, cash, order_id, checkpoint)')
            db.execute("INSERT INTO ledger VALUES (1, '100000.05', 'original-order', 'original-checkpoint')")
    with closing(sqlite3.connect(root / 'market-stream.sqlite3')) as db:
        os.chmod(root / 'market-stream.sqlite3', 0o600)
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('CREATE TABLE events(id INTEGER PRIMARY KEY, payload)')
        count = (size + 65535) // 65536
        for start in range(0, count, 32):
            db.executemany('INSERT INTO events(payload) VALUES (zeroblob(65536))',
                           [()] * min(32, count - start))
            db.commit()
    storage.atomic_json(root / 'binding.json', {'identity': 'fixture'})


def run(size, parent=None):
    with tempfile.TemporaryDirectory(prefix='p10-13-benchmark-', dir=parent) as temporary:
        base = Path(temporary).resolve()
        root = base / 'state'
        create(root, size)
        original_machine_id = storage.machine_id
        storage.machine_id = lambda: '0' * 32
        try:
            market = 'binance' if os.name == 'nt' else 'alpaca'
            started = perf_counter()
            storage.recovery_snapshot(root, base / 'stopped', market, REVISION)
            stopped = perf_counter() - started
            started = perf_counter()
            with storage.periodic_recovery_snapshot(root, base / 'pinned', market, REVISION) as complete:
                cut = perf_counter() - started
                for name in ('account', 'deployments'):
                    with closing(sqlite3.connect(root / f'{name}.sqlite3')) as db, db:
                        db.execute("INSERT INTO ledger VALUES (2, '-1', 'after-cut-order', 'after-cut-checkpoint')")
                with closing(sqlite3.connect(root / 'market-stream.sqlite3')) as db, db:
                    db.executemany('INSERT INTO events(payload) VALUES (zeroblob(65536))', [()] * 32)
                wal = sum(p.stat().st_size for p in root.glob('*-wal'))
                complete()
            online_total = perf_counter() - started
            verified = storage.verify_snapshot(base / 'pinned')
            restored = storage.restore_verified_state(base / 'pinned', base / 'restore', verified)
            for name in ('account', 'deployments'):
                with closing(sqlite3.connect(restored / f'{name}.sqlite3')) as db:
                    rows = db.execute('SELECT * FROM ledger').fetchall()
                    if rows != [(1, '100000.05', 'original-order', 'original-checkpoint')]:
                        raise ValueError('economic cut changed across backup/restore')
            return dict(measured_at=datetime.now(timezone.utc).isoformat(), platform=platform.platform(),
                        architecture=platform.machine(), python=platform.python_version(),
                        sqlite=sqlite3.sqlite_version, synthetic=True, source_market_bytes=(root / 'market-stream.sqlite3').stat().st_size,
                        stopped_snapshot_seconds=stopped, pinned_cut_seconds=cut,
                        pinned_total_seconds=online_total, peak_process_rss_bytes=peak_rss(),
                        post_cut_wal_bytes=wal, restored_economic_cut_equal=True,
                        services_restarted=False)
        finally:
            storage.machine_id = original_machine_id


def main():
    if os.name != 'nt':
        os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--market-bytes', type=int, default=844_000_000)
    parser.add_argument('--temporary-parent', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = run(args.market_bytes, args.temporary_parent)
    args.output.write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    main()
