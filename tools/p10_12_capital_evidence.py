"""Read typed capital receipts and instance state without exporting account identities."""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3

from factorlab.trading.storage import decode


def redact(value):
    if isinstance(value, dict):
        return {key: redact(item) for key, item in value.items() if key not in {'account', 'account_id'}}
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    return value


def capture(root):
    health = json.loads((root / 'health.json').read_text())
    result = dict(evidence_kind='SHARED_OWNER_INSTANCE_STATE',
        observed_at_utc=datetime.now(timezone.utc).isoformat(),
        health=redact({key: health[key] for key in (
            'revision', 'healthy', 'heartbeat', 'pid', 'account_recovered', 'error',
            'trade_stream', 'market_available', 'instances', 'strategy', 'execution') if key in health}))
    with closing(sqlite3.connect((root / 'portfolio.sqlite3').resolve().as_uri() + '?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row
        result['allocations'] = [dict(row) for row in db.execute(
            'SELECT strategy,allocation,capital,initial_cash FROM portfolio_allocations ORDER BY strategy')]
        result['instances'] = [dict(row) for row in db.execute(
            'SELECT strategy,instance,allocation,logical_strategy,paused FROM capital_instances ORDER BY strategy')]
        result['aggregate_symbol_limits'] = [dict(row) for row in db.execute(
            'SELECT symbol,max_exposure FROM capital_symbol_limits ORDER BY symbol')]
        result['capital_events'] = [dict(event=row[0], request=row[1], payload=redact(decode(json.loads(row[2]))))
            for row in db.execute('SELECT event,request,payload FROM capital_events ORDER BY rowid')]
        result['capital_commands'] = [dict(command=row[0], request=redact(decode(json.loads(row[1]))),
            result=redact(decode(json.loads(row[2]))) if row[2] else None)
            for row in db.execute('SELECT command,request,result FROM capital_commands ORDER BY rowid')]
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-root', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(capture(args.state_root), indent=2, default=str))


if __name__ == '__main__':
    main()
