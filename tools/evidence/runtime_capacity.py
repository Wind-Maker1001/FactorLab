"""Bounded local retention experiment and read-only Linux capacity snapshots.

Run from the repository with PYTHONPATH=src. Snapshot twice to obtain counter
changes; sampled worker peaks are lower bounds, not subprocess high-water marks.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import platform
import sqlite3
import tempfile
import time


def counters(path):
    return {parts[0].rstrip(':'): int(parts[1])
            for line in path.read_text().splitlines()
            if len(parts := line.split()) >= 2 and parts[1].isdigit()}


def snapshot(proc=Path('/proc'), *, pids=(), worker_pids=(), databases=(), cache_roots=()):
    mem = counters(proc / 'meminfo')
    vm = counters(proc / 'vmstat')
    processes = []
    parents = set(pids)
    pending = list(parents)
    workers = set(worker_pids)
    while pending:
        pid = pending.pop()
        try:
            children = (proc / str(pid) / 'task' / str(pid) / 'children').read_text().split()
        except FileNotFoundError:  # A worker can finish between proc reads.
            continue
        for child in map(int, children):
            if child not in workers:
                workers.add(child)
                pending.append(child)
    for pid in sorted(parents | workers):
        root = proc / str(pid)
        try:
            status = counters(root / 'status')
            rollup = counters(root / 'smaps_rollup')
        except (FileNotFoundError, ProcessLookupError):
            continue
        processes.append(dict(pid=pid, role='parent' if pid in parents else 'worker',
                              rss_kib=status.get('VmRSS'),
                              peak_rss_kib=status.get('VmHWM'), pss_kib=rollup.get('Pss')))
    cpu = next(line for line in (proc / 'stat').read_text().splitlines() if line.startswith('cpu '))
    return dict(observed_at=time.time(),
                observed_at_utc=datetime.now(timezone.utc).isoformat(),
                boot_id=(proc / 'sys/kernel/random/boot_id').read_text().strip(),
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


def interval_metrics(previous, current):
    """Host counters are comparable only within one boot; CPU guest ticks overlap user ticks."""
    if previous is None or previous['boot_id'] != current['boot_id']:
        return dict(status='BASELINE')
    before, after = previous['cpu_ticks'][:8], current['cpu_ticks'][:8]
    ticks = [b - a for a, b in zip(before, after)]
    total = sum(ticks)
    old_databases = {row['path']: row for row in previous['databases']}
    databases = []
    for row in current['databases']:
        old = old_databases.get(row['path'])
        if old is not None:
            databases.append(dict(path=row['path'], page_count_delta=row['page_count'] - old['page_count'],
                                  freelist_count_delta=row['freelist_count'] - old['freelist_count'],
                                  db_bytes_delta=row['db_bytes'] - old['db_bytes']))
    return dict(status='OBSERVED', seconds=current['observed_at'] - previous['observed_at'],
                cpu_busy_ratio=(total - ticks[3] - ticks[4]) / total if total else None,
                swap_in_pages_delta=current['swap_in_pages'] - previous['swap_in_pages'],
                swap_out_pages_delta=current['swap_out_pages'] - previous['swap_out_pages'],
                oom_kill_delta=current['oom_kill'] - previous['oom_kill'],
                databases=databases)


def record_capacity(result, monitor_dir):
    from factorlab.integrations.binance_futures_trading.demo_cloud_monitor import (
        append_bounded_jsonl, load_jsonl,
    )

    path = monitor_dir / 'capacity-samples.jsonl'
    samples = load_jsonl(path)
    result['interval'] = interval_metrics(samples[-1] if samples else None, result)
    result['worker_peak_scope'] = 'Live descendants VmHWM only; workers exiting between health passes are not observed'
    return append_bounded_jsonl(path, result, max_bytes=8 * 1024 * 1024,
                                retention_seconds=8 * 86400, now=result['observed_at'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('retention', 'snapshot'))
    parser.add_argument('--output', type=Path)
    parser.add_argument('--monitor-dir', type=Path, help='Append bounded capacity evidence alongside existing health samples')
    parser.add_argument('--state-root', type=Path, help='Discover health and SQLite databases under existing runtime state')
    parser.add_argument('--alpaca', action='store_true', help='Use Alpaca health.json and FACTORLAB_ALPACA_STATE_ROOT')
    parser.add_argument('--pid', type=int, action='append', default=[])
    parser.add_argument('--database', type=Path, action='append', default=[])
    parser.add_argument('--cache-root', type=Path, action='append', default=[])
    parser.add_argument('--health', type=Path, help='Existing cloud-health.json for API/restart counters')
    parser.add_argument('--backup-root', type=Path, help='Existing backup metadata; read only')
    args = parser.parse_args()
    if args.output is None and args.monitor_dir is None:
        parser.error('--output or --monitor-dir is required')
    if args.monitor_dir and args.mode != 'snapshot':
        parser.error('--monitor-dir requires snapshot mode')
    started = time.perf_counter()
    if args.alpaca and args.state_root is None:
        args.state_root = Path(os.environ.get('FACTORLAB_ALPACA_STATE_ROOT', '/var/lib/factorlab-alpaca-paper'))
    if args.state_root:
        args.health = args.health or args.state_root / ('health.json' if args.alpaca else 'cloud-health.json')
        args.database.extend(sorted(args.state_root.rglob('*.sqlite3')))
    health = json.loads(args.health.read_text(encoding='utf-8')) if args.health else None
    if health is not None:
        from factorlab.integrations.binance_futures_trading.demo_cloud_monitor import sanitize_sample
        sample = sanitize_sample(health)
        if sample['pid']:
            args.pid.append(sample['pid'])
    result = retention_workload() if args.mode == 'retention' else snapshot(
        pids=args.pid, databases=args.database, cache_roots=args.cache_root)
    if health is not None:
        result['runtime'] = ({key: health.get(key) for key in ('heartbeat', 'pid', 'starts', 'mode')}
                             if args.alpaca else {key: sample[key] for key in (
                                 'observed_at_utc', 'pid', 'starts', 'restart_count', 'systemd',
                                 'api_total_requests', 'api_total_errors', 'request_failure_rate')})
    if args.backup_root:
        from factorlab.integrations.binance_futures_trading.demo_cloud_monitor import load_c07_backup_records
        records = load_c07_backup_records(args.backup_root)
        result['backup'] = records
    result['elapsed_seconds'] = time.perf_counter() - started
    if args.monitor_dir:
        record_capacity(result, args.monitor_dir)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')


if __name__ == '__main__':
    main()
