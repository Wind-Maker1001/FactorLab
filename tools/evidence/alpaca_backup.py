"""Measure the existing Alpaca backup coordinator and its economic cut on Linux."""
import argparse
from contextlib import closing
import ctypes
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import select
import sqlite3
import struct
import subprocess
import sys
import threading
from time import monotonic

from factorlab.integrations import disaster_recovery as recovery
from factorlab.integrations.binance_futures_trading.demo_cloud_backup import SystemdRunner


def ledger(root):
    result = {}
    for path in sorted(root.rglob('*.sqlite3')):
        if path.name in {'runtime.sqlite3', 'market-stream.sqlite3'}:
            continue
        tables = {}
        with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as db:
            db.execute('PRAGMA query_only=ON')
            db.execute('BEGIN')
            for (name,) in db.execute("SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"):
                quoted = '"' + name.replace('"', '""') + '"'
                rows = []
                for row in db.execute('SELECT * FROM ' + quoted):
                    values = [[type(v).__name__, v.hex() if isinstance(v, bytes) else repr(v)] for v in row]
                    rows.append(json.dumps(values, separators=(',', ':')))
                digest = sha256()
                for row in sorted(rows):
                    digest.update(row.encode() + b'\n')
                tables[name] = {'rows': len(rows), 'sha256': digest.hexdigest()}
        result[path.relative_to(root).as_posix()] = tables
    return result


def first_healthy(root, resumed, receipt, events, previous_pid, timeout=180, *, watching=None):
    """Wait on atomic health-file publication events, not periodic file polling."""
    libc = ctypes.CDLL(None, use_errno=True)
    fd = libc.inotify_init1(os.O_CLOEXEC)
    if fd < 0:
        raise OSError(ctypes.get_errno(), 'inotify_init1')
    try:
        if libc.inotify_add_watch(fd, os.fsencode(root), 0x00000080) < 0:
            raise OSError(ctypes.get_errno(), 'inotify_add_watch')
        if watching is not None:
            watching.set()
        resumed.wait()
        if 'resume_requested' not in events:
            receipt['status'] = 'NO_RESTART'
            return
        restart = events['resume_requested']
        deadline = restart['monotonic'] + timeout
        while select.select([fd], [], [], max(0, deadline - monotonic()))[0]:
            events = os.read(fd, 65536)
            offset = 0
            while offset < len(events):
                _, _, _, length = struct.unpack_from('iIII', events, offset)
                name = events[offset + 16:offset + 16 + length].split(b'\0', 1)[0]
                offset += 16 + length
                if name == b'health.json' and resumed.is_set():
                    health = json.loads((root / 'health.json').read_bytes())
                    if (health['healthy'] and health['pid'] != previous_pid
                            and datetime.fromisoformat(health['heartbeat']) >= datetime.fromisoformat(restart['at'])):
                        receipt.update(heartbeat=health['heartbeat'], revision=health['revision'],
                                       pid=health['pid'], observed_monotonic=monotonic(),
                                       observed_at=datetime.now(timezone.utc).isoformat())
                        return
        receipt['status'] = 'NO_HEALTHY_PUBLICATION_WITHIN_WINDOW'
    finally:
        os.close(fd)


class MeasuredRunner(SystemdRunner):
    def __init__(self, root):
        super().__init__(service_user='factorlab-alpaca')
        self.root = root
        self.events = {}
        self.resumed = threading.Event()
        self.cut_ledger = None

    def mark(self, name):
        self.events[name] = {'at': datetime.now(timezone.utc).isoformat(), 'monotonic': monotonic()}

    def read_ledger(self, root):
        import pwd
        account = pwd.getpwnam(self.service_user)
        output = subprocess.check_output([self.python_executable, str(Path(__file__).resolve()),
            'ledger', '--state-root', str(root)], text=True, user=account.pw_uid,
            group=account.pw_gid, extra_groups=os.getgrouplist(account.pw_name, account.pw_gid))
        return json.loads(output)

    def stop_unit(self, unit):
        self.mark('stop_requested')
        super().stop_unit(unit)
        self.mark('writers_stopped')
        self.cut_ledger = self.read_ledger(self.root)
        self.mark('precut_ledger_recorded')

    def start_unit(self, unit):
        self.mark('resume_requested')
        self.resumed.set()
        super().start_unit(unit)
        self.mark('service_start_returned')

    def run_python_module(self, module, args, **kwargs):
        if args[0] == 'snapshot':
            self.mark('snapshot_started')
            resume = kwargs['on_snapshot_ready']
            def ready():
                self.mark('consistent_cut_ready')
                resume()
            kwargs['on_snapshot_ready'] = ready
            result = super().run_python_module(module, args, **kwargs)
            self.mark('snapshot_worker_completed')
            return result
        return super().run_python_module(module, args, **kwargs)


def measure(args):
    runner = MeasuredRunner(args.state_root)
    if not runner.is_unit_active(recovery.ALPACA_UNIT):
        raise RuntimeError('measurement requires the existing active Paper executor')
    healthy = {}
    previous_pid = json.loads((args.state_root / 'health.json').read_bytes())['pid']
    watching = threading.Event()
    def observe():
        try:
            first_healthy(args.state_root, runner.resumed, healthy, runner.events, previous_pid,
                          watching=watching)
        except Exception as error:
            healthy.update(status='OBSERVER_FAILED', error=repr(error))
            watching.set()
    watcher = threading.Thread(target=observe)
    watcher.start()
    try:
        watching.wait()
        if healthy.get('status') == 'OBSERVER_FAILED':
            raise RuntimeError(healthy['error'])
        result = recovery.scheduled_alpaca_backup(args.state_root, args.backup_root,
            args.release, args.recovery_file, args.keep, runner)
        runner.mark('snapshot_verified')
        backup = Path(result['path'])
        restored_ledger = runner.read_ledger(backup)
        equal = runner.cut_ledger == restored_ledger
        live = runner.read_ledger(args.state_root)
        changed = {db: sorted(table for table in set(tables) | set(live.get(db, {}))
                   if tables.get(table) != live.get(db, {}).get(table))
                   for db, tables in runner.cut_ledger.items()}
        changed = {db: tables for db, tables in changed.items() if tables}
        watcher.join()
        stopped = runner.events['writers_stopped']['monotonic']
        started = runner.events['stop_requested']['monotonic']
        returned = runner.events['service_start_returned']['monotonic']
        receipt = dict(measured_at=datetime.now(timezone.utc).isoformat(), synthetic=False,
            revision=args.release.resolve().name, events=runner.events, first_healthy=healthy,
            stop_request_to_start_return_seconds=returned - started,
            stopped_writers_to_start_return_seconds=returned - stopped,
            stop_request_to_observed_healthy_seconds=(healthy['observed_monotonic'] - started
                if 'observed_monotonic' in healthy else None),
            stop_request_to_first_healthy_heartbeat_seconds=(
                (datetime.fromisoformat(healthy['heartbeat']) - datetime.fromisoformat(
                    runner.events['stop_requested']['at'])).total_seconds()
                if 'heartbeat' in healthy else None),
            measurement_ledger_scan_seconds=runner.events['precut_ledger_recorded']['monotonic'] - stopped,
            backup=result, cut_ledger_equal=equal, cut_ledger=runner.cut_ledger,
            live_after=live, live_changed_tables=changed)
        args.output.write_text(json.dumps(receipt, indent=2) + '\n')
        if not equal:
            raise RuntimeError('backup economic cut differs from stopped source')
        if 'heartbeat' not in healthy:
            raise RuntimeError('no healthy post-resume heartbeat observed')
        print(json.dumps(receipt))
    finally:
        runner.resumed.set()
        watcher.join()


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('ledger', 'measure'))
    parser.add_argument('--state-root', type=Path, required=True)
    parser.add_argument('--backup-root', type=Path)
    parser.add_argument('--release', type=Path)
    parser.add_argument('--recovery-file', type=Path)
    parser.add_argument('--keep', type=int, default=14)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if args.mode == 'ledger':
        print(json.dumps(ledger(args.state_root)))
    else:
        if not all((args.backup_root, args.release, args.recovery_file, args.output)):
            parser.error('measure requires backup-root, release, recovery-file and output')
        measure(args)


if __name__ == '__main__':
    main()
