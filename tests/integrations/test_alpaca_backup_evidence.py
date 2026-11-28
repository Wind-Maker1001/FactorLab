import sqlite3
from contextlib import closing

from tools.evidence.alpaca_backup import MeasuredRunner, ledger
from tools.evidence import alpaca_backup
from factorlab.integrations.binance_futures_trading.demo_cloud_backup import SystemdRunner


def test_ledger_identity_preserves_economic_content_without_row_order(tmp_path):
    root = tmp_path / 'state'
    root.mkdir()
    with closing(sqlite3.connect(root / 'portfolio.sqlite3')) as db, db:
        db.execute('CREATE TABLE ledger (id, capital, checkpoint)')
        db.executemany('INSERT INTO ledger VALUES (?, ?, ?)', [(2, '20.05', b'b'), (1, '10', b'a')])
    before = ledger(root)
    with closing(sqlite3.connect(root / 'portfolio.sqlite3')) as db, db:
        db.execute('DELETE FROM ledger')
        db.executemany('INSERT INTO ledger VALUES (?, ?, ?)', [(1, '10', b'a'), (2, '20.05', b'b')])
    assert ledger(root) == before
    with closing(sqlite3.connect(root / 'runtime.sqlite3')) as db, db:
        db.execute('CREATE TABLE metadata (starts)')
        db.execute('INSERT INTO metadata VALUES (2)')
    assert ledger(root) == before
    with closing(sqlite3.connect(root / 'portfolio.sqlite3')) as db, db:
        db.execute("UPDATE ledger SET capital='20.06' WHERE id=2")
    assert ledger(root) != before


def test_measurement_separates_stop_cut_restart_and_copy(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(SystemdRunner, 'stop_unit', lambda self, unit: calls.append('stop'))
    monkeypatch.setattr(SystemdRunner, 'start_unit', lambda self, unit: calls.append('start'))
    monkeypatch.setattr(MeasuredRunner, 'read_ledger', lambda self, root: {'economic': 'cut'})
    def snapshot(self, module, args, **kwargs):
        kwargs['on_snapshot_ready']()
        calls.append('copy')
        return 'done'
    monkeypatch.setattr(SystemdRunner, 'run_python_module', snapshot)
    runner = MeasuredRunner(tmp_path)
    runner.stop_unit('executor')
    assert runner.run_python_module('snapshot', ['snapshot'],
        on_snapshot_ready=lambda: runner.start_unit('executor')) == 'done'
    assert calls == ['stop', 'start', 'copy']
    assert runner.cut_ledger == {'economic': 'cut'}
    assert runner.resumed.is_set()
    times = [runner.events[name]['monotonic'] for name in (
        'stop_requested', 'writers_stopped', 'precut_ledger_recorded', 'snapshot_started',
        'consistent_cut_ready', 'resume_requested', 'service_start_returned', 'snapshot_worker_completed')]
    assert times == sorted(times)


def test_first_healthy_is_eligible_before_start_returns(monkeypatch, tmp_path):
    import json
    import struct
    import threading
    from datetime import datetime, timezone
    from time import monotonic
    from types import SimpleNamespace

    events = {'resume_requested': {'at': datetime.now(timezone.utc).isoformat(), 'monotonic': monotonic()}}
    signal = threading.Event()
    signal.set()
    health = dict(healthy=True, pid=42, revision='candidate', heartbeat=datetime.now(timezone.utc).isoformat())
    (tmp_path / 'health.json').write_text(json.dumps(health))
    monkeypatch.setattr(alpaca_backup.os, 'O_CLOEXEC', 0, raising=False)
    monkeypatch.setattr(alpaca_backup.ctypes, 'CDLL', lambda *a, **kw: SimpleNamespace(
        inotify_init1=lambda flags: 99, inotify_add_watch=lambda *a: 1))
    windows = []
    def readable(read, write, errors, timeout):
        windows.append(timeout)
        return ([99], [], [])
    monkeypatch.setattr(alpaca_backup.select, 'select', readable)
    name = b'health.json\0'
    monkeypatch.setattr(alpaca_backup.os, 'read', lambda *a: struct.pack('iIII', 1, 128, 0, len(name)) + name)
    monkeypatch.setattr(alpaca_backup.os, 'close', lambda fd: None)
    receipt = {}
    watching = threading.Event()
    alpaca_backup.first_healthy(tmp_path, signal, receipt, events, previous_pid=41,
                               watching=watching)
    assert watching.is_set()
    assert receipt['pid'] == 42
    assert 'service_start_returned' not in events
    assert 179 < windows[0] <= 180
