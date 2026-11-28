import importlib.util
import json
from pathlib import Path
import sqlite3


SPEC = importlib.util.spec_from_file_location(
    'runtime_capacity', Path(__file__).parents[1] / 'tools/evidence/runtime_capacity.py')
capacity = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(capacity)


def test_alpaca_unit_collects_from_overridden_state_root(tmp_path, monkeypatch):
    root = tmp_path / 'r01-return'
    root.mkdir()
    (root / 'health.json').write_text(json.dumps(dict(pid=123, heartbeat='now', starts=2, mode='EXECUTE')))
    with sqlite3.connect(root / 'runtime.sqlite3') as db:
        db.execute('CREATE TABLE events(value)')
    collected = {}
    def snapshot(**kwargs):
        collected.update(kwargs)
        return dict(observed_at=100, boot_id='a', cpu_ticks=[0] * 10,
                    swap_in_pages=0, swap_out_pages=0, oom_kill=0, databases=[])
    monkeypatch.setattr(capacity, 'snapshot', snapshot)
    monkeypatch.setenv('FACTORLAB_ALPACA_STATE_ROOT', str(root))
    monitor = tmp_path / 'monitoring'
    monkeypatch.setattr('sys.argv', ['runtime_capacity.py', 'snapshot', '--alpaca', '--monitor-dir', str(monitor)])
    capacity.main()
    assert collected['pids'] == [123]
    assert collected['databases'] == [root / 'runtime.sqlite3']
    row = json.loads((monitor / 'capacity-samples.jsonl').read_text())
    assert row['runtime'] == dict(heartbeat='now', pid=123, starts=2, mode='EXECUTE')
    unit = (Path(__file__).parents[1] / 'deploy/systemd/factorlab-alpaca-paper-health.service').read_text()
    assert 'ExecStopPost=/opt/factorlab-alpaca/current/venv/bin/python /opt/factorlab-alpaca/current/source/tools/evidence/runtime_capacity.py snapshot --alpaca --monitor-dir /var/lib/factorlab-alpaca-paper/monitoring' in unit
    assert 'ReadWritePaths=/var/lib/factorlab-alpaca-paper/monitoring' in unit
    assert 'StateDirectory=factorlab-alpaca-paper/monitoring' in unit


def test_snapshot_descendant_hwm_and_sqlite(tmp_path):
    proc = tmp_path / 'proc'
    proc.mkdir()
    (proc / 'meminfo').write_text('MemAvailable: 200 kB\nSwapTotal: 100 kB\nSwapFree: 90 kB\n')
    (proc / 'vmstat').write_text('pswpin 2\npswpout 3\noom_kill 0\n')
    (proc / 'stat').write_text('cpu 10 0 5 80 5 0 0 0 0 0\n')
    boot = proc / 'sys/kernel/random'
    boot.mkdir(parents=True)
    (boot / 'boot_id').write_text('boot-a')
    for pid, children, hwm in ((10, '11 12', 100), (11, '', 900)):
        root = proc / str(pid)
        task = root / 'task' / str(pid)
        task.mkdir(parents=True)
        (task / 'children').write_text(children)
        (root / 'status').write_text(f'VmRSS: 50 kB\nVmHWM: {hwm} kB\n')
        (root / 'smaps_rollup').write_text('Pss: 40 kB\n')
    database = tmp_path / 'state.sqlite3'
    with sqlite3.connect(database) as db:
        db.execute('CREATE TABLE data(value)')
    result = capacity.snapshot(proc, pids=[10], databases=[database])
    assert [(p['role'], p['peak_rss_kib']) for p in result['processes']] == [('parent', 100), ('worker', 900)]
    assert result['databases'][0]['page_count'] == 2
    assert result['azure_cpu_credits']['status'] == 'UNKNOWN'


def test_bounded_collection_and_boot_safe_deltas(tmp_path):
    first = dict(observed_at=100, boot_id='a', cpu_ticks=[10, 0, 5, 80, 5, 0, 0, 0, 50, 0],
                 swap_in_pages=1, swap_out_pages=2, oom_kill=0,
                 databases=[dict(path='state', page_count=10, freelist_count=4, db_bytes=40960)])
    second = dict(first, observed_at=160, cpu_ticks=[20, 0, 10, 160, 10, 0, 0, 0, 100, 0],
                  swap_in_pages=3, swap_out_pages=5, oom_kill=1,
                  databases=[dict(path='state', page_count=10, freelist_count=2, db_bytes=40960)])
    capacity.record_capacity(first, tmp_path)
    capacity.record_capacity(second, tmp_path)
    rows = [json.loads(line) for line in (tmp_path / 'capacity-samples.jsonl').read_text().splitlines()]
    assert rows[0]['interval'] == {'status': 'BASELINE'}
    interval = rows[1]['interval']
    assert interval['cpu_busy_ratio'] == .15
    assert interval['swap_in_pages_delta'] == 2
    assert interval['swap_out_pages_delta'] == 3
    assert interval['oom_kill_delta'] == 1
    assert interval['databases'][0]['freelist_count_delta'] == -2
    assert interval['databases'][0]['page_count_delta'] == 0
    assert capacity.interval_metrics(second, dict(second, boot_id='b')) == {'status': 'BASELINE'}
    capacity.record_capacity(dict(second, observed_at=100 + 10 * 86400, boot_id='b'), tmp_path)
    assert len((tmp_path / 'capacity-samples.jsonl').read_text().splitlines()) == 1
