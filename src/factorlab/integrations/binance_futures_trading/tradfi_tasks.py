"""Durable cloud task mailbox; only the account owner executes or acknowledges tasks."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
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


def _encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _receipt_required(db, task_id, status, detail):
    row = db.execute('SELECT status FROM portfolio_tasks WHERE task_id=?', (task_id,)).fetchone()
    if row is None:
        raise ValueError('unknown task identity')
    if row[0] in TERMINAL_RECEIPTS:
        last = db.execute('SELECT status,detail FROM portfolio_receipts WHERE task_id=? '
                          'ORDER BY sequence DESC LIMIT 1', (task_id,)).fetchone()
        if last is None or tuple(last) != (status, detail):
            raise ValueError('terminal receipt is immutable')
        return False
    return True


def reduce_task_mailbox(db, event):
    """Apply original task/receipt data without creating execution decisions."""
    if event['kind'] == 'PORTFOLIO_TASK_SUBMITTED':
        payload = event['task']
        encoded = _encoded(payload)
        saved = db.execute('SELECT payload FROM portfolio_tasks WHERE task_id=?',
                           (payload['task_id'],)).fetchone()
        if saved is not None and saved[0] != encoded:
            raise ValueError('task identity already binds a different immutable target')
        db.execute('INSERT OR IGNORE INTO portfolio_tasks VALUES (?,?,?)',
                   (payload['task_id'], encoded, 'QUEUED'))
    elif event['kind'] == 'PORTFOLIO_TASK_RECEIPT':
        if event['status'] not in RECEIPT_STATES:
            raise ValueError('unknown consumer receipt status')
        detail = _encoded(event['detail'])
        if _receipt_required(db, event['task_id'], event['status'], detail):
            db.execute('INSERT INTO portfolio_receipts(task_id,recorded_at,status,detail) VALUES (?,?,?,?)',
                       (event['task_id'], event['recorded_at'], event['status'], detail))
            db.execute('UPDATE portfolio_tasks SET status=? WHERE task_id=?',
                       (event['status'], event['task_id']))
    else:
        raise ValueError('unsupported mailbox event')


class TaskMailbox:
    """A submit acknowledgement is not execution completion.

    F03 consumes outstanding payloads and records receipts only after its own
    durable execution journal has advanced. An expired in-flight task still
    needs account-owner reconciliation; expiry never hides it from recovery.
    """
    def __init__(self, path, *, event_writer_factory=None):
        self.path = Path(path)
        self.db = sqlite3.connect(self.path, timeout=5)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('''CREATE TABLE IF NOT EXISTS portfolio_tasks (
            task_id TEXT PRIMARY KEY, payload TEXT NOT NULL, status TEXT NOT NULL)''')
        self.db.execute('''CREATE TABLE IF NOT EXISTS portfolio_receipts (
            sequence INTEGER PRIMARY KEY, task_id TEXT NOT NULL,
            recorded_at TEXT NOT NULL, status TEXT NOT NULL, detail TEXT NOT NULL)''')
        self.db.commit()
        self.db.execute('PRAGMA synchronous=FULL')
        self.event_writer = None if event_writer_factory is None else event_writer_factory(self.db)
        if self.event_writer is not None:
            self.event_writer.resume()

    def _apply_event(self, event):
        if self.event_writer is not None:
            return self.event_writer.submit(event, committed_at=datetime.now(timezone.utc).isoformat())
        if self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='dr_replay_identity'").fetchone():
            raise PermissionError('replicated mailbox changes require their account event writer')
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            reduce_task_mailbox(self.db, event)

    def submit(self, task: PortfolioTask):
        payload = task.payload()
        if self.event_writer is not None:
            self.event_writer.resume()
        previous = self.db.execute('SELECT payload,status FROM portfolio_tasks WHERE task_id=?',
                                   (task.task_id,)).fetchone()
        if previous is not None:
            if previous[0] != _encoded(payload):
                raise ValueError('task identity already binds a different immutable target')
            return previous[1]
        self._apply_event(dict(event_id=_encoded(['task-submit', task.task_id]), targets=['mailbox'],
                               kind='PORTFOLIO_TASK_SUBMITTED', task=payload))
        return self.status(task.task_id)

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
        encoded = _encoded(detail)
        if self.event_writer is not None:
            self.event_writer.resume()
        if not _receipt_required(self.db, task_id, status, encoded):
            return
        original = dict(kind='PORTFOLIO_TASK_RECEIPT', task_id=task_id, recorded_at=at,
                        status=status, detail=json.loads(encoded))
        identity = sha256(_encoded(original).encode('utf-8')).hexdigest()
        self._apply_event(dict(original, event_id='task-receipt:' + identity, targets=['mailbox']))

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
