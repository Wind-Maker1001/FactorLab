"""Durable cloud task mailbox; only the account owner executes or acknowledges tasks."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
import json
from pathlib import Path
import sqlite3

from factorlab.time_utils import utc_instant

TEST_ALLOCATION = 'tradfi-qqq-spy-test'
TEST_SYMBOLS = frozenset({'QQQUSDT', 'SPYUSDT'})
RECEIPT_STATES = frozenset({'ACCEPTED', 'RUNNING', 'RECONCILING', 'COMPLETED', 'REJECTED'})
TERMINAL_RECEIPTS = frozenset({'COMPLETED', 'REJECTED'})


@dataclass(frozen=True)
class PortfolioTask:
    task_id: str
    allocation_id: str
    session: str
    observed_at: str
    valid_until: str
    equity_usdt: Decimal
    weights: dict[str, Decimal]
    task_type: str = 'SCHEDULED'

    def payload(self):
        if self.task_type not in {'SCHEDULED', 'ACCEPTANCE'}:
            raise ValueError('explicit scheduled or acceptance task type required')
        if not self.task_id or not self.allocation_id:
            raise ValueError('explicit allocation and stable task identity required')
        if self.allocation_id == TEST_ALLOCATION:
            if set(self.weights) != TEST_SYMBOLS:
                raise ValueError('complete legacy QQQ/SPY allocation required')
        else:
            from factorlab.integrations.binance.market import symbol_name
            if not self.weights or any(symbol_name(symbol) != symbol for symbol in self.weights):
                raise ValueError('explicit Demo USDT symbol targets required')
        values = [self.equity_usdt, *self.weights.values()]
        if any(not isinstance(v, Decimal) or not v.is_finite() or v < 0 for v in values):
            raise ValueError('finite nonnegative Decimal allocation values required')
        if self.equity_usdt == 0 or sum(self.weights.values()) > 1:
            raise ValueError('positive equity and unlevered weights required')
        observed, expires = utc_instant(self.observed_at), utc_instant(self.valid_until)
        if observed >= expires:
            raise ValueError('task must expire after its price observation')
        return {'schema_version': 1, 'task_id': self.task_id, 'allocation_id': self.allocation_id,
                'task_type': self.task_type,
                'session': self.session, 'observed_at': observed.isoformat(),
                'valid_until': expires.isoformat(), 'equity_usdt': str(self.equity_usdt),
                'weights': {s: str(self.weights[s]) for s in sorted(self.weights)}}


class TaskMailbox:
    """A submit acknowledgement is not execution completion.

    F03 consumes outstanding payloads and records receipts only after its own
    durable execution journal has advanced. An expired in-flight task still
    needs account-owner reconciliation; expiry never hides it from recovery.
    """
    def __init__(self, path):
        self.path = Path(path)
        self.db = sqlite3.connect(self.path, timeout=5)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('''CREATE TABLE IF NOT EXISTS portfolio_tasks (
            task_id TEXT PRIMARY KEY, payload TEXT NOT NULL, status TEXT NOT NULL)''')
        self.db.execute('''CREATE TABLE IF NOT EXISTS portfolio_receipts (
            sequence INTEGER PRIMARY KEY, task_id TEXT NOT NULL,
            recorded_at TEXT NOT NULL, status TEXT NOT NULL, detail TEXT NOT NULL)''')
        self.db.commit()

    def submit(self, task: PortfolioTask):
        encoded = json.dumps(task.payload(), sort_keys=True, separators=(',', ':'))
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO portfolio_tasks VALUES (?,?,?)',
                            (task.task_id, encoded, 'QUEUED'))
            previous = self.db.execute('SELECT payload,status FROM portfolio_tasks WHERE task_id=?',
                                       (task.task_id,)).fetchone()
            if previous[0] != encoded:
                raise ValueError('task identity already binds a different immutable target')
        return previous[1]

    def status(self, task_id):
        row = self.db.execute('SELECT status FROM portfolio_tasks WHERE task_id=?', (task_id,)).fetchone()
        return None if row is None else row[0]

    def outstanding(self):
        rows = self.db.execute('''SELECT payload,status FROM portfolio_tasks
            WHERE status NOT IN ('COMPLETED','REJECTED') ORDER BY rowid''').fetchall()
        return [dict(task=json.loads(payload), status=status) for payload, status in rows]

    def receipt(self, task_id, *, recorded_at, status, detail):
        """Account-owner API; detail must contain only sanitized journal references."""
        if status not in RECEIPT_STATES:
            raise ValueError('unknown consumer receipt status')
        at = utc_instant(recorded_at).isoformat()
        encoded = json.dumps(detail, sort_keys=True, separators=(',', ':'), allow_nan=False)
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            row = self.db.execute('SELECT status FROM portfolio_tasks WHERE task_id=?', (task_id,)).fetchone()
            if row is None:
                raise ValueError('unknown task identity')
            if row[0] in TERMINAL_RECEIPTS:
                last = self.db.execute('''SELECT status,detail FROM portfolio_receipts
                    WHERE task_id=? ORDER BY sequence DESC LIMIT 1''', (task_id,)).fetchone()
                if last != (status, encoded):
                    raise ValueError('terminal receipt is immutable')
                return
            self.db.execute('INSERT INTO portfolio_receipts(task_id,recorded_at,status,detail) VALUES (?,?,?,?)',
                            (task_id, at, status, encoded))
            self.db.execute('UPDATE portfolio_tasks SET status=? WHERE task_id=?', (status, task_id))

    def snapshot(self):
        rows = self.db.execute('''SELECT t.task_id,t.status,r.recorded_at,r.detail,t.payload
            FROM portfolio_tasks t LEFT JOIN portfolio_receipts r
            ON r.sequence=(SELECT max(sequence) FROM portfolio_receipts WHERE task_id=t.task_id)
            ORDER BY t.rowid''').fetchall()
        return [dict(task_id=task, task_type=json.loads(payload)['task_type'],
                     session=json.loads(payload)['session'], status=status, recorded_at=at,
                     detail=None if detail is None else json.loads(detail))
                for task, status, at, detail, payload in rows]

    def close(self):
        self.db.close()
