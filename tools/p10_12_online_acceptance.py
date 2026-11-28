"""Export read-only runtime evidence; never treat a heartbeat as a trade receipt.

Run with the execution host's Python. Output is a single JSON snapshot on stdout.
No credentials or account identifiers are exported.
"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import subprocess


SERVICES = ('factorlab-demo-execute.service', 'factorlab-alpaca-paper.service',
            'factorlab-demo-tradfi-observer.service')
TABLES = ('portfolio_allocations', 'capital_accounts', 'capital_instances',
          'capital_events', 'capital_commands', 'portfolio_risk_config',
          'portfolio_risk_nav', 'instance_deployment', 'instance_decisions',
          'instance_runtime_admissions', 'portfolio_orders', 'portfolio_confirmations')
HEALTH_FIELDS = ('revision', 'mode', 'healthy', 'status', 'account_recovered',
                 'heartbeat', 'last_reconciled_at', 'unknown_count', 'schedule',
                 'strategy', 'execution', 'requests', 'global_budget', 'campaign')


def database_summary(path):
    """Count existing journals through a read-only connection, including live WAL."""
    db = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)
    try:
        names = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        return {name: db.execute('SELECT count(*) FROM "' + name + '"').fetchone()[0]
                for name in TABLES if name in names}
    finally:
        db.close()


def root_snapshot(root, health_name):
    health = json.loads((root / health_name).read_text(encoding='utf-8'))
    databases = {}
    for relative in ('portfolio.sqlite3', 'portfolio-inbox/portfolio.sqlite3',
                     'strategy-deployment.sqlite3', 'deployments.sqlite3'):
        path = root / relative
        if path.is_file():
            databases[relative] = database_summary(path)
    sizes = {}
    # Current runtime roots contain at most three levels of SQLite journals.
    for pattern in ('*.sqlite3*', '*/*.sqlite3*', '*/*/*.sqlite3*'):
        for path in root.glob(pattern):
            if path.is_file():
                sizes[str(path.relative_to(root))] = path.stat().st_size
    return dict(health={key: health[key] for key in HEALTH_FIELDS if key in health},
                journal_counts=databases, sqlite_file_bytes=sizes)


def service_snapshot(unit):
    fields = ('MainPID', 'ActiveState', 'SubState', 'NRestarts', 'CPUUsageNSec',
              'MemoryCurrent', 'MemoryPeak', 'MemorySwapCurrent')
    result = subprocess.run(['systemctl', 'show', unit, *['--property=' + key for key in fields]],
                            check=True, capture_output=True, text=True)
    values = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
    pid = int(values['MainPID'])
    if pid:
        rollup = Path('/proc') / str(pid) / 'smaps_rollup'
        values['process_memory'] = {key: value.strip() for line in rollup.read_text().splitlines()
                                   if ':' in line for key, value in [line.split(':', 1)]
                                   if key in {'Rss', 'Pss', 'Swap', 'SwapPss'}}
    return values


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binance-root', type=Path, required=True)
    parser.add_argument('--alpaca-root', type=Path, required=True)
    args = parser.parse_args(argv)
    evidence = dict(schema_version=1, captured_at=datetime.now(timezone.utc).isoformat(),
                    evidence_kind='RUNTIME_SNAPSHOT',
                    binance=root_snapshot(args.binance_root, 'cloud-health.json'),
                    alpaca=root_snapshot(args.alpaca_root, 'health.json'),
                    services={unit: service_snapshot(unit) for unit in SERVICES})
    print(json.dumps(evidence, indent=2))


if __name__ == '__main__':
    main()
