from datetime import datetime, timedelta, timezone
import sqlite3

from factorlab.integrations.alpaca.cloud_runtime import StreamRetention
from tools.evidence.runtime_capacity import retention_workload, snapshot


def test_continuous_retention_boundary_and_hourly_throttle():
    with sqlite3.connect(':memory:') as db:
        db.execute('CREATE TABLE events (received_at TEXT)')
        start = datetime(2026, 1, 10, tzinfo=timezone.utc)
        cutoff = start - timedelta(days=7)
        db.executemany('INSERT INTO events VALUES (?)',
                       [(value.isoformat(),) for value in (cutoff - timedelta(seconds=1), cutoff, start)])
        retention = StreamRetention(db)
        retention.prune(start)
        assert db.execute('SELECT count(*) FROM events').fetchone()[0] == 2
        retention.prune(start + timedelta(minutes=59))
        assert db.execute('SELECT count(*) FROM events').fetchone()[0] == 2
        retention.prune(start + timedelta(hours=1))
        assert db.execute('SELECT received_at FROM events').fetchall() == [(start.isoformat(),)]


def test_retention_reuses_freelist_without_vacuum():
    evidence = retention_workload()
    assert max(row['rows'] for row in evidence['samples']) == 8 * 512
    assert evidence['steady_page_span'] <= 2
    assert any(row['free_pages'] > 0 for row in evidence['samples'][10:])


def test_linux_snapshot_units_and_counters(tmp_path):
    (tmp_path / 'meminfo').write_text('MemAvailable: 1024 kB\nSwapTotal: 512 kB\nSwapFree: 128 kB\n')
    (tmp_path / 'vmstat').write_text('pswpin 3\npswpout 7\noom_kill 2\n')
    (tmp_path / 'stat').write_text('cpu  1 2 3 4 5 6 7 8 0 0\n')
    boot = tmp_path / 'sys/kernel/random'
    boot.mkdir(parents=True)
    (boot / 'boot_id').write_text('test-boot')
    process = tmp_path / '123'
    task = process / 'task/123'
    task.mkdir(parents=True)
    (task / 'children').write_text('')
    (process / 'status').write_text('VmRSS: 80 kB\nVmHWM: 90 kB\n')
    (process / 'smaps_rollup').write_text('Pss: 60 kB\n')
    result = snapshot(tmp_path, pids=[123])
    assert result['mem_available_kib'] == 1024
    assert result['swap_used_kib'] == 384
    assert result['swap_in_pages'] == 3
    assert result['swap_out_pages'] == 7
    assert result['oom_kill'] == 2
    assert result['processes'] == [dict(pid=123, role='parent', rss_kib=80, peak_rss_kib=90, pss_kib=60)]
    assert result['azure_cpu_credits']['status'] == 'UNKNOWN'
