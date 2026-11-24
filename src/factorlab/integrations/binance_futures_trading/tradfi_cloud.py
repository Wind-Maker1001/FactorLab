"""Credential-free TradFi observation and scheduling alongside the sole Execute owner."""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import signal
import sqlite3
import threading
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import urlopen
from zoneinfo import ZoneInfo

from factorlab.cross_market.calendar import us_calendar
from .demo_cloud_storage import atomic_json
from .tradfi_schedule import WeeklySchedule
from .tradfi_dispatch import dispatch
from .tradfi_tasks import TaskMailbox

DEMO_PUBLIC = 'https://demo-fapi.binance.com'


class ObservationLedger:
    def __init__(self, root):
        self.root = Path(root)
        self.db = sqlite3.connect(self.root / 'observations.sqlite3')
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('''CREATE TABLE IF NOT EXISTS samples (
            observed_at TEXT PRIMARY KEY, day TEXT NOT NULL, payload TEXT NOT NULL)''')
        self.db.execute('CREATE INDEX IF NOT EXISTS samples_day ON samples(day)')
        self.db.execute('''CREATE TABLE IF NOT EXISTS lifecycle (
            id INTEGER PRIMARY KEY CHECK(id=1), starts INTEGER NOT NULL,
            disconnects INTEGER NOT NULL, recoveries INTEGER NOT NULL,
            disconnected INTEGER NOT NULL)''')
        self.db.execute('INSERT OR IGNORE INTO lifecycle VALUES (1,0,0,0,0)')
        self.db.execute('UPDATE lifecycle SET starts=starts+1 WHERE id=1')
        self.db.commit()

    def record(self, snapshot):
        connected = snapshot['market_error'] is None
        with self.db:
            previous = self.db.execute('SELECT disconnected FROM lifecycle WHERE id=1').fetchone()[0]
            if connected and previous:
                self.db.execute('UPDATE lifecycle SET recoveries=recoveries+1, disconnected=0 WHERE id=1')
            elif not connected and not previous:
                self.db.execute('UPDATE lifecycle SET disconnects=disconnects+1, disconnected=1 WHERE id=1')
            starts, disconnects, recoveries = self.db.execute(
                'SELECT starts,disconnects,recoveries FROM lifecycle WHERE id=1').fetchone()
            snapshot.update(starts=starts, disconnects=disconnects, recoveries=recoveries)
            day = datetime.fromisoformat(snapshot['observed_at']).astimezone(
                ZoneInfo('America/New_York')).date().isoformat()
            self.db.execute('INSERT INTO samples VALUES (?,?,?)',
                            (snapshot['observed_at'], day, json.dumps(snapshot)))
        atomic_json(self.root / 'tradfi-health.json', snapshot)
        count, first, failures = self.db.execute(
            '''SELECT count(*), min(observed_at),
               sum(json_extract(payload, '$.market_error') IS NOT NULL)
               FROM samples WHERE day=?''', (day,)).fetchone()
        daily = {'day': day, 'samples': count, 'first_observed_at': first,
                 'last': snapshot, 'market_failure_samples': failures,
                 'execution_owner': 'factorlab-demo-execute.service',
                 'execution_status': snapshot['execution_status'],
                 'stock_reference_return': None, 'contract_strategy_return': None,
                 'execution_cost': None}
        atomic_json(self.root / ('daily-' + day + '.json'), daily)

    def close(self):
        self.db.close()


def public_get(path, params=None):
    query = '' if params is None else '?' + urlencode(params)
    with urlopen(DEMO_PUBLIC + path + query, timeout=15) as response:
        return json.load(response)


def collect_market(symbols):
    quotes = {}
    for symbol in symbols:
        raw = public_get('/fapi/v1/ticker/bookTicker', {'symbol': symbol})
        quotes[symbol] = {k: raw[k] for k in ('symbol', 'bidPrice', 'bidQty', 'askPrice', 'askQty', 'time')}
    return quotes


def account_projection(health_path, now):
    try:
        health = json.loads(Path(health_path).read_text(encoding='utf-8'))
    except FileNotFoundError:
        return {'fresh': False, 'reason': 'EXECUTE_HEALTH_MISSING'}
    heartbeat = health.get('heartbeat_at')
    # Existing Execute heartbeat timestamps are Unix seconds.
    age = None if heartbeat is None else now.timestamp() - heartbeat
    from .adapter import ACCOUNT_READ_SECONDS
    account_age = health.get('account_age_seconds')
    fresh = (age is not None and 0 <= age <= 90
             and health.get('account_recovered') is True
             and account_age is not None and 0 <= account_age + age <= ACCOUNT_READ_SECONDS
             and health['status'] not in {'EXITED', 'STOPPED', 'HALTED'})
    keys = ('status', 'mode', 'desired', 'last_reconciled_at', 'last_valid_market_at',
            'campaign', 'global_budget', 'unknown_count', 'open_orders')
    result = {k: health.get(k) for k in keys}
    result.update(fresh=fresh, heartbeat_age_seconds=age)
    for key in ('equity', 'nav_usdt', 'positions'):
        result[key] = health.get(key) if fresh else None
    return result


def run(args):
    stopped = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stopped.set())
    ledger = ObservationLedger(args.state_root)
    mailbox = None if args.task_db is None else TaskMailbox(args.task_db)
    config = None if args.task_config is None else json.loads(args.task_config.read_text(encoding='utf-8'))
    started = datetime.now(timezone.utc)
    calendar_day = None
    schedule = None
    try:
        while not stopped.is_set():
            now = datetime.now(timezone.utc)
            if calendar_day != now.date():
                schedule = WeeklySchedule(us_calendar(now.date() - timedelta(days=14), now.date() + timedelta(days=400)))
                calendar_day = now.date()
            receipts = [] if mailbox is None else mailbox.snapshot()
            completed_session = max((r['session'] for r in receipts
                                     if r['task_type'] == 'SCHEDULED' and r['status'] == 'COMPLETED'), default=None)
            window = schedule.next_window(now, completed_session=completed_session)
            error = None
            try:
                quotes = collect_market(args.symbols)
            except (HTTPError, URLError, TimeoutError, OSError) as exc:
                quotes = {}
                error = type(exc).__name__
            observed = datetime.now(timezone.utc)
            for quote in quotes.values():
                quote['age_seconds'] = observed.timestamp() - quote['time'] / 1000
                quote['fresh'] = 0 <= quote['age_seconds'] <= 90
            target = None if config is None else dispatch(
                mailbox, config, window, quotes, observed, acceptance_task_id=args.acceptance_task_id)
            receipts = [] if mailbox is None else mailbox.snapshot()
            snapshot = {'schema_version': 1, 'mode': 'READ_ONLY_SIGNAL_OBSERVER' if config is None else 'SIGNAL_PRODUCER',
                        'started_at': started.isoformat(), 'observed_at': observed.isoformat(),
                        'uptime_seconds': (observed - started).total_seconds(),
                        'quotes': quotes, 'market_error': error,
                        'account': account_projection(args.execute_health, observed),
                        'schedule': {'session': window.session, 'opens_at': window.opens_at.isoformat(),
                                     'closes_at': window.closes_at.isoformat(),
                                     'history_through': window.history_through,
                                     'calendar_version': window.calendar_version},
                        'last_successful_rebalance': max((r['recorded_at'] for r in receipts
                                                          if r['status'] == 'COMPLETED' and r['task_type'] == 'SCHEDULED'), default=None),
                        'last_successful_acceptance': max((r['recorded_at'] for r in receipts
                                                           if r['status'] == 'COMPLETED' and r['task_type'] == 'ACCEPTANCE'), default=None),
                        'target': target, 'receipts': receipts,
                        'target_status': 'NO_STRATEGY_CONFIGURED' if target is None else target['status'],
                        'execution_status': 'AWAITING_SHARED_ACCOUNT_EXECUTION_INTERFACE' if not receipts else receipts[-1]['status']}
            ledger.record(snapshot)
            print(json.dumps({'observed_at': snapshot['observed_at'], 'symbols': list(quotes),
                              'market_error': error, 'account_fresh': snapshot['account']['fresh'],
                              'next_session': window.session}), flush=True)
            stopped.wait(args.poll_seconds)
    finally:
        if mailbox is not None:
            mailbox.close()
        ledger.close()


def health(root, now):
    try:
        snapshot = json.loads((Path(root) / 'tradfi-health.json').read_text(encoding='utf-8'))
    except FileNotFoundError:
        return {'healthy': False, 'reason': 'NO_HEARTBEAT'}
    age = (now - datetime.fromisoformat(snapshot['observed_at'])).total_seconds()
    market_fresh = bool(snapshot['quotes']) and all(
        0 <= now.timestamp() - q['time'] / 1000 <= 90 for q in snapshot['quotes'].values())
    return {'healthy': 0 <= age <= 90 and snapshot['market_error'] is None and market_fresh,
            'heartbeat_age_seconds': age, 'market_fresh': market_fresh,
            'mode': snapshot['mode'], 'execution_status': snapshot['execution_status'],
            'target_status': snapshot['target_status']}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-root', type=Path, required=True)
    parser.add_argument('--execute-health', type=Path)
    parser.add_argument('--symbols', nargs='+')
    parser.add_argument('--health', action='store_true')
    parser.add_argument('--poll-seconds', type=float, default=30)
    parser.add_argument('--task-db', type=Path)
    parser.add_argument('--task-config', type=Path)
    parser.add_argument('--acceptance-task-id')
    args = parser.parse_args(argv)
    if args.health:
        result = health(args.state_root, datetime.now(timezone.utc))
        print(json.dumps(result))
        raise SystemExit(0 if result['healthy'] else 1)
    if args.execute_health is None or not args.symbols:
        parser.error('execute-health and symbols are required for observation')
    if args.task_config is not None and args.task_db is None:
        parser.error('task-config requires task-db')
    if args.acceptance_task_id is not None and args.task_config is None:
        parser.error('acceptance-task-id requires explicit task-config')
    if args.poll_seconds <= 0:
        parser.error('poll-seconds must be positive')
    if not args.state_root.is_absolute() or not args.state_root.is_dir():
        parser.error('state-root must be an existing absolute directory')
    if any(not s.isalnum() or not s.endswith('USDT') for s in args.symbols):
        parser.error('explicit USDT symbols required')
    run(args)


if __name__ == '__main__':
    main()
