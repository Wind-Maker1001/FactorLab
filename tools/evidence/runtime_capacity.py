"""Bounded local retention experiment and read-only Linux capacity snapshots.

Run from the repository with PYTHONPATH=src. Snapshot twice to obtain counter
changes; sampled worker peaks are lower bounds, not subprocess high-water marks.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import platform
import sqlite3
import tempfile
import time


def counters(path):
    return {parts[0].rstrip(':'): int(parts[1])
            for line in path.read_text().splitlines()
            if len(parts := line.split()) >= 2 and parts[1].isdigit()}


def snapshot(proc=Path('/proc'), *, pids=(), databases=(), cache_roots=()):
    mem = counters(proc / 'meminfo')
    vm = counters(proc / 'vmstat')
    processes = []
    for pid in pids:
        root = proc / str(pid)
        status = counters(root / 'status')
        rollup = counters(root / 'smaps_rollup')
        processes.append(dict(pid=pid, rss_kib=status.get('VmRSS'),
                              peak_rss_kib=status.get('VmHWM'), pss_kib=rollup.get('Pss')))
    cpu = next(line for line in (proc / 'stat').read_text().splitlines() if line.startswith('cpu '))
    return dict(observed_at=datetime.now(timezone.utc).isoformat(),
                mem_available_kib=mem['MemAvailable'],
                swap_used_kib=mem['SwapTotal'] - mem['SwapFree'],
                swap_in_pages=vm['pswpin'], swap_out_pages=vm['pswpout'],
                oom_kill=vm['oom_kill'], cpu_ticks=list(map(int, cpu.split()[1:])),
                processes=processes,
                databases=[database_metrics(p) for p in databases],
                caches=[cache_inventory(p) for p in cache_roots],
                azure_cpu_credits=dict(status='UNKNOWN', reason='Azure Monitor permission not provided'))


def database_metrics(path):
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as db:
        db.execute('PRAGMA query_only=ON')
        pages = {name: db.execute('PRAGMA ' + name).fetchone()[0]
                 for name in ('journal_mode', 'page_count', 'page_size', 'freelist_count')}
    wal = Path(str(path) + '-wal')
    return dict(path=str(path), db_bytes=path.stat().st_size,
                wal_bytes=wal.stat().st_size if wal.exists() else 0, **pages)


def cache_inventory(root):
    generations = [p for p in root.iterdir() if p.is_dir() and (p / 'bars.parquet').is_file()]
    return dict(path=str(root), generations=len(generations),
                generation_bytes=sum(f.stat().st_size for p in generations for f in p.iterdir() if f.is_file()))


def retention_workload(days=24, rows_per_day=512):
    from factorlab.integrations.alpaca.cloud_runtime import StreamRetention

    with tempfile.TemporaryDirectory(prefix='factorlab-retention-') as folder:
        path = Path(folder) / 'market-stream.sqlite3'
        with closing(sqlite3.connect(path)) as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript('CREATE TABLE events (id INTEGER PRIMARY KEY, received_at TEXT NOT NULL, '
                             'channel TEXT NOT NULL, symbol TEXT NOT NULL, payload TEXT NOT NULL);'
                             'CREATE INDEX events_received_at ON events(received_at);')
            retention = StreamRetention(db)
            start = datetime(2026, 1, 1, tzinfo=timezone.utc)
            samples = []
            for day in range(days):
                observed = start + timedelta(days=day)
                with db:
                    db.executemany('INSERT INTO events(received_at,channel,symbol,payload) VALUES (?,?,?,?)',
                                   [(observed.isoformat(), 'bars', 'TEST', 'x' * 512)] * rows_per_day)
                    retention.prune(observed)
                db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
                samples.append(dict(day=day, rows=db.execute('SELECT count(*) FROM events').fetchone()[0],
                                    pages=db.execute('PRAGMA page_count').fetchone()[0],
                                    free_pages=db.execute('PRAGMA freelist_count').fetchone()[0],
                                    db_bytes=path.stat().st_size,
                                    wal_bytes=Path(str(path) + '-wal').stat().st_size))
            return dict(kind='synthetic_local_not_vm', python=platform.python_version(),
                        sqlite=sqlite3.sqlite_version, days=days, rows_per_day=rows_per_day,
                        payload_bytes=512, retention_days=7, cleanup_interval_hours=1,
                        checkpoint='TRUNCATE after each synthetic day; no VACUUM', samples=samples,
                        steady_page_span=max(s['pages'] for s in samples[10:]) - min(s['pages'] for s in samples[10:]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('retention', 'snapshot'))
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--pid', type=int, action='append', default=[])
    parser.add_argument('--database', type=Path, action='append', default=[])
    parser.add_argument('--cache-root', type=Path, action='append', default=[])
    parser.add_argument('--health', type=Path, help='Existing cloud-health.json for API/restart counters')
    parser.add_argument('--backup-root', type=Path, help='Existing backup metadata; read only')
    args = parser.parse_args()
    started = time.perf_counter()
    result = retention_workload() if args.mode == 'retention' else snapshot(
        pids=args.pid, databases=args.database, cache_roots=args.cache_root)
    if args.health:
        from factorlab.integrations.binance_futures_trading.demo_cloud_monitor import sanitize_sample
        sample = sanitize_sample(json.loads(args.health.read_text(encoding='utf-8')))
        result['runtime'] = {key: sample[key] for key in (
            'observed_at_utc', 'pid', 'starts', 'restart_count', 'systemd',
            'api_total_requests', 'api_total_errors', 'request_failure_rate')}
    if args.backup_root:
        from factorlab.integrations.binance_futures_trading.demo_cloud_monitor import load_c07_backup_records
        records = load_c07_backup_records(args.backup_root)
        result['backup'] = records
    result['elapsed_seconds'] = time.perf_counter() - started
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')


if __name__ == '__main__':
    main()
