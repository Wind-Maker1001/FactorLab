"""SQLite paper metadata, one atomic account snapshot and event journal."""
from contextlib import contextmanager
from dataclasses import fields, is_dataclass
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
import json
import sqlite3
import time

from . import contracts
from .paper import Instrument, PaperState, Quote
from .state import OrderRecord

_TYPES = {c.__name__: c for c in (
    contracts.OrderIntent, contracts.OrderEvent, contracts.OrderSnapshot,
    contracts.Position, Instrument, PaperState, Quote, OrderRecord,
)}
_ENUMS = {c.__name__: c for c in (contracts.Market, contracts.Side, contracts.OrderType, contracts.OrderStatus)}


def encode(value):
    if isinstance(value, Enum):
        return {"enum": type(value).__name__, "value": value.value}
    if isinstance(value, Decimal):
        return {"decimal": str(value)}
    if isinstance(value, datetime):
        return {"utc": value.isoformat()}
    if is_dataclass(value):
        return {"type": type(value).__name__, "fields": {f.name: encode(getattr(value, f.name)) for f in fields(value)}}
    if isinstance(value, dict):
        return {"map": [[k, encode(v)] for k, v in value.items()]}
    if isinstance(value, (tuple, list)):
        return [encode(v) for v in value]
    return value


def decode(value):
    if isinstance(value, list):
        return [decode(v) for v in value]
    if not isinstance(value, dict):
        return value
    if "map" in value:
        return {k: decode(v) for k, v in value["map"]}
    if "enum" in value:
        return _ENUMS[value["enum"]](value["value"])
    if "decimal" in value:
        return Decimal(value["decimal"])
    if "utc" in value:
        return datetime.fromisoformat(value["utc"])
    if "type" in value:
        return _TYPES[value["type"]](**{k: decode(v) for k, v in value["fields"].items()})
    return {k: decode(v) for k, v in value.items()}


def dumps(value):
    return json.dumps(encode(value), sort_keys=True, separators=(",", ":"), allow_nan=False)


class PaperStore:
    """One controller per file; caps stop writes rather than deleting identity history.

    Defaults bound lifetime use to 1,000 orders, 10,000 events and a 32 MiB
    snapshot. Explicit continuous capacity persists in the journal and removes
    these lifetime limits on subsequent opens without deleting history;
    per-event size and identity checks remain unchanged.
    """
    def __init__(self, path, *, max_orders=1000, max_events=10000, continuous_capacity=False):
        if type(continuous_capacity) is not bool:
            raise ValueError("continuous_capacity must be a bool")
        if not 1 <= max_orders <= 1000 or not 1 <= max_events <= 10000:
            raise ValueError("storage limits exceed paper metadata budget")
        self.continuous_capacity = continuous_capacity
        self.max_orders = None if continuous_capacity else max_orders
        self.max_events = None if continuous_capacity else max_events
        self.transaction_count = 0
        self.last_transaction = None
        self.last_transaction_times = None
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("CREATE TABLE IF NOT EXISTS snapshot (id INTEGER PRIMARY KEY CHECK(id=1), version INTEGER NOT NULL, payload TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS events (id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
        capacity_key = 'paper-store:continuous-capacity'
        if continuous_capacity:
            self.record_metadata(capacity_key, True)
        elif self.event_payload(capacity_key) is not None:
            self.continuous_capacity = True
            self.max_orders = None
            self.max_events = None

    @contextmanager
    def transaction(self):
        started_at = datetime.now(timezone.utc)
        started = time.perf_counter()
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        finally:
            self.transaction_count += 1
            self.last_transaction = time.perf_counter() - started
            self.last_transaction_times = (started_at, datetime.now(timezone.utc))

    def load(self):
        row = self.db.execute("SELECT version,payload FROM snapshot WHERE id=1").fetchone()
        if row is None:
            return None
        if row[0] != contracts.CONTRACT_VERSION:
            raise ValueError("unsupported persisted contract version")
        return decode(json.loads(row[1]))

    def save(self, state):
        if self.max_orders is not None and len(state.orders) > self.max_orders:
            raise ValueError("paper order storage budget exhausted")
        payload = dumps(state)
        if not self.continuous_capacity and len(payload.encode("utf-8")) > 32 * 1024 * 1024:
            raise ValueError("paper snapshot storage budget exhausted")
        self.db.execute("INSERT INTO snapshot VALUES(1,?,?) ON CONFLICT(id) DO UPDATE SET version=excluded.version,payload=excluded.payload",
                        (contracts.CONTRACT_VERSION, payload))

    def record_event(self, event):
        return self.record_metadata(event.event_id, event)

    def event_payload(self, event_id):
        row = self.db.execute("SELECT payload FROM events WHERE id=?", (event_id,)).fetchone()
        return row[0] if row is not None else None

    def event_cursor(self):
        return self.db.execute("SELECT COALESCE(MAX(rowid), 0) FROM events").fetchone()[0]

    def order_events(self):
        """Yield authoritative reports in journal insertion order."""
        for cursor, payload in self.db.execute("SELECT rowid, payload FROM events ORDER BY rowid"):
            event = decode(json.loads(payload))
            if isinstance(event, contracts.OrderEvent):
                yield cursor, event

    def record_metadata(self, event_id, value):
        """Append strategy evidence to the existing journal, without economic posting."""
        payload = dumps(value)
        saved = self.event_payload(event_id)
        if saved is not None:
            if saved != payload:
                raise ValueError("event identity reused with a different payload")
            return False
        if len(payload.encode("utf-8")) > 8192:
            raise ValueError("event metadata too large")
        if self.max_events is not None and self.db.execute("SELECT count(*) FROM events").fetchone()[0] >= self.max_events:
            raise ValueError("paper event storage budget exhausted")
        self.db.execute("INSERT INTO events VALUES(?,?)", (event_id, payload))
        return True

    def close(self):
        self.db.close()
