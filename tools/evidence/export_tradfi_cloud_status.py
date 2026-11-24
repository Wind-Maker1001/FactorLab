"""Read-only F04 service/mailbox evidence; no credentials or exchange requests."""
import json
from pathlib import Path
import sqlite3
import subprocess
from datetime import datetime, timezone

ROOT = Path('/var/lib/factorlab-tradfi-observer')
EXECUTE = Path('/var/lib/factorlab-demo/execute-state')


def services():
    units = ('factorlab-demo-execute.service', 'factorlab-demo-tradfi-observer.service',
             'factorlab-demo-tradfi-health.timer', 'factorlab-demo-health.timer')
    result = {}
    for unit in units:
        output = subprocess.check_output([
            'systemctl', 'show', unit, '-p', 'ActiveState', '-p', 'SubState', '-p', 'MainPID',
            '-p', 'NRestarts', '-p', 'ExecMainStartTimestamp', '-p', 'ExecMainStatus',
            '-p', 'UnitFileState', '-p', 'ExecStart', '-p', 'ReadWritePaths'], text=True)
        result[unit] = dict(line.split('=', 1) for line in output.splitlines())
    return result


def export():
    observed = json.loads((ROOT / 'tradfi-health.json').read_text())
    account = json.loads((EXECUTE / 'cloud-health.json').read_text())
    with sqlite3.connect((ROOT / 'observations.sqlite3').as_uri() + '?mode=ro', uri=True) as db:
        count, first, last = db.execute('SELECT count(*),min(observed_at),max(observed_at) FROM samples').fetchone()
        lifecycle = db.execute('SELECT starts,disconnects,recoveries,disconnected FROM lifecycle').fetchone()
    with sqlite3.connect((ROOT / 'task-mailbox/tasks.sqlite3').as_uri() + '?mode=ro', uri=True) as db:
        tasks = [dict(task_id=i, payload=json.loads(p), status=s) for i, p, s in
                 db.execute('SELECT task_id,payload,status FROM portfolio_tasks ORDER BY rowid')]
        receipts = [dict(sequence=i, task_id=t, recorded_at=a, status=s, detail=json.loads(d))
                    for i, t, a, s, d in db.execute('SELECT * FROM portfolio_receipts ORDER BY sequence')]
    return {'captured_at': datetime.now(timezone.utc).isoformat(), 'host': 'factorlab-demo-01',
            'services': services(), 'observer': observed,
            'observations': {'count': count, 'first_at': first, 'last_at': last,
                             'starts': lifecycle[0], 'disconnects': lifecycle[1],
                             'recoveries': lifecycle[2], 'disconnected': bool(lifecycle[3])},
            'execute': {k: account.get(k) for k in ('heartbeat_at', 'pid', 'status', 'desired',
                'account_recovered', 'account_age_seconds', 'unknown_count', 'reason', 'nav_usdt',
                'open_orders', 'market_fresh', 'last_reconciled_at')},
            'mailbox': {'tasks': tasks, 'receipts': receipts},
            'observer_release': str(Path('/opt/factorlab/tradfi-observer/current').resolve())}


if __name__ == '__main__':
    print(json.dumps(export(), sort_keys=True))
