"""SQLite paper metadata, one atomic account snapshot and event journal."""
from contextlib import contextmanager
from dataclasses import fields, is_dataclass
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
import json
import sqlite3
import time

from factorlab.errors import DataQualityError

from . import contracts
from .paper import Instrument, PaperState, Quote
from .state import OrderRecord

_TYPES = {c.__name__: c for c in (
    contracts.OrderIntent, contracts.OrderEvent, contracts.FundingEvent, contracts.OrderSnapshot,
    contracts.Position, Instrument, PaperState, Quote, OrderRecord,
)}
_ENUMS = {c.__name__: c for c in (contracts.Market, contracts.Side, contracts.OrderType, contracts.OrderStatus)}
_PRIMITIVE_TYPES = frozenset((str, int, float, bool, type(None)))
_DATACLASS_FIELD_CACHE: dict[type, tuple[str, tuple[str, ...]] | None] = {}
for _cls in _TYPES.values():
    _DATACLASS_FIELD_CACHE[_cls] = (_cls.__name__, tuple(f.name for f in fields(_cls)))


def _dataclass_meta(cls):
    meta = _DATACLASS_FIELD_CACHE.get(cls, False)
    if meta is False:
        if is_dataclass(cls):
            meta = (cls.__name__, tuple(f.name for f in fields(cls)))
        else:
            meta = None
        _DATACLASS_FIELD_CACHE[cls] = meta
    return meta


def encode(value):
    t = type(value)
    if t in _PRIMITIVE_TYPES:
        return value
    if t is Decimal:
        return {"decimal": str(value)}
    if t is datetime:
        return {"utc": value.isoformat()}
    if t is dict:
        return {"map": [[k, encode(v)] for k, v in value.items()]}
    if t is list or t is tuple:
        return [encode(v) for v in value]
    if isinstance(value, Enum):
        return {"enum": t.__name__, "value": value.value}
    if isinstance(value, Decimal):
        return {"decimal": str(value)}
    if isinstance(value, datetime):
        return {"utc": value.isoformat()}
    meta = _dataclass_meta(t)
    if meta is not None:
        name, field_names = meta
        return {"type": name, "fields": {fname: encode(getattr(value, fname)) for fname in field_names}}
    if isinstance(value, dict):
        return {"map": [[k, encode(v)] for k, v in value.items()]}
    if isinstance(value, (tuple, list)):
        return [encode(v) for v in value]
    return value


def decode(value):
    t = type(value)
    if t is dict:
        n = len(value)
        if n == 1:
            if "decimal" in value:
                return Decimal(value["decimal"])
            if "utc" in value:
                return datetime.fromisoformat(value["utc"])
            if "map" in value:
                return {k: decode(v) for k, v in value["map"]}
        elif n == 2:
            if "type" in value and "fields" in value:
                return _TYPES[value["type"]](**{k: decode(v) for k, v in value["fields"].items()})
            if "enum" in value and "value" in value:
                return _ENUMS[value["enum"]](value["value"])
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
    if t is list:
        return [decode(v) for v in value]
    return value


def _clone_runtime(value):
    """Fast copy for PaperState.runtime containers while sharing immutable values."""
    t = type(value)
    if t in _PRIMITIVE_TYPES or t is Decimal or t is datetime or isinstance(value, Enum):
        return value
    if t is dict:
        return {k: _clone_runtime(v) for k, v in value.items()}
    if t is list:
        return [_clone_runtime(v) for v in value]
    if t is tuple:
        return tuple(_clone_runtime(v) for v in value)
    if t is set:
        return {_clone_runtime(v) for v in value}
    if t in (contracts.OrderIntent, contracts.OrderEvent, contracts.OrderSnapshot,
             contracts.Position, Instrument, Quote, OrderRecord):
        return value
    if t is contracts.FundingEvent:
        if not value.metadata:
            return value
        return contracts.FundingEvent(
            value.account_id, value.tran_id, value.symbol, value.asset, value.income_type,
            value.amount, value.event_time, value.source, value.event_id, dict(value.metadata),
        )
    from copy import deepcopy
    return deepcopy(value)


def _clone_paper_state(state: PaperState) -> PaperState:
    """Create an isolated PaperState copy without recursing into immutable records."""
    return PaperState(
        market=state.market,
        account_id=state.account_id,
        instruments=dict(state.instruments),
        cash=dict(state.cash),
        fee_rate=state.fee_rate,
        orders=dict(state.orders),
        positions=dict(state.positions),
        quotes=dict(state.quotes),
        confirmations=dict(state.confirmations),
        submissions=list(state.submissions),
        reconciled=state.reconciled,
        kill_switch=state.kill_switch,
        sequence=state.sequence,
        runtime=_clone_runtime(state.runtime),
        funding=dict(state.funding),
        funding_events=dict(state.funding_events),
    )


_ENCODER = json.JSONEncoder(sort_keys=True, separators=(",", ":"), allow_nan=False)


def dumps(value):
    return _ENCODER.encode(encode(value))


class PaperStore:
    """One controller per file; caps stop writes rather than deleting identity history.

    Defaults bound lifetime use to 1,000 orders, 10,000 events and a 32 MiB
    snapshot. Explicit continuous capacity persists in the journal and removes
    these lifetime limits on subsequent opens without deleting history;
    per-event size and identity checks remain unchanged.
    research_wal selects file-backed WAL with FULL commit synchronization for
    disposable research accounts; it does not relax transaction durability.
    """
    def __init__(self, path, *, max_orders=1000, max_events=10000, continuous_capacity=False,
                 research_wal=False):
        if type(continuous_capacity) is not bool:
            raise ValueError("continuous_capacity must be a bool")
        if not 1 <= max_orders <= 1000 or not 1 <= max_events <= 10000:
            raise ValueError("storage limits exceed paper metadata budget")
        self.continuous_capacity = continuous_capacity
        self.max_orders = None if continuous_capacity else max_orders
        self.max_events = None if continuous_capacity else max_events
        self.commit_seconds = 0.
        self.commit_count = 0
        self.transaction_count = 0
        self.last_transaction = None
        self.last_transaction_times = None
        self._cached_payload = None
        self._cached_state = None
        self._encoded_orders_json = {}
        self._encoded_confirmations_json = {}
        self._encoded_funding_json = {}
        self.db = sqlite3.connect(path, isolation_level=None)
        if research_wal:
            self.db.execute("PRAGMA journal_mode=WAL")
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
            commit_started = time.perf_counter()
            try:
                self.db.execute("COMMIT")
            finally:
                self.commit_seconds += time.perf_counter() - commit_started
                self.commit_count += 1
        except BaseException:
            self.db.execute("ROLLBACK")
            self._cached_payload = None
            self._cached_state = None
            raise
        finally:
            self.transaction_count += 1
            self.last_transaction = time.perf_counter() - started
            self.last_transaction_times = (started_at, datetime.now(timezone.utc))

    def _dumps_paper_state(self, state):
        if type(state) is not PaperState:
            return dumps(state)
        enc = _ENCODER.encode

        ord_cache = self._encoded_orders_json
        order_parts = []
        for k, v in state.orders.items():
            cached = ord_cache.get(k)
            if cached is not None and cached[0] is v and type(v) is OrderRecord:
                order_parts.append(cached[1])
            else:
                frag = "[" + enc(k) + "," + enc(encode(v)) + "]"
                if type(v) is OrderRecord:
                    ord_cache[k] = (v, frag)
                order_parts.append(frag)

        conf_cache = self._encoded_confirmations_json
        conf_parts = []
        for k, v in state.confirmations.items():
            cached = conf_cache.get(k)
            if cached is not None and cached[0] is v and type(v) is contracts.OrderEvent:
                conf_parts.append(cached[1])
            else:
                frag = "[" + enc(k) + "," + enc(encode(v)) + "]"
                if type(v) is contracts.OrderEvent:
                    conf_cache[k] = (v, frag)
                conf_parts.append(frag)

        fund_cache = self._encoded_funding_json
        fund_parts = []
        for k, v in state.funding_events.items():
            cached = fund_cache.get(k)
            if cached is not None and cached[0] is v and type(v) is contracts.FundingEvent:
                fund_parts.append(cached[1])
            else:
                frag = "[" + enc(k) + "," + enc(encode(v)) + "]"
                if type(v) is contracts.FundingEvent:
                    fund_cache[k] = (v, frag)
                fund_parts.append(frag)

        # Keys in strictly alphabetical order to match sort_keys=True byte-for-byte.
        return (
            '{"fields":{'
            '"account_id":' + enc(encode(state.account_id)) + ','
            '"cash":' + enc(encode(state.cash)) + ','
            '"confirmations":{"map":[' + ",".join(conf_parts) + ']},'
            '"fee_rate":' + enc(encode(state.fee_rate)) + ','
            '"funding":' + enc(encode(state.funding)) + ','
            '"funding_events":{"map":[' + ",".join(fund_parts) + ']},'
            '"instruments":' + enc(encode(state.instruments)) + ','
            '"kill_switch":' + enc(encode(state.kill_switch)) + ','
            '"market":' + enc(encode(state.market)) + ','
            '"orders":{"map":[' + ",".join(order_parts) + ']},'
            '"positions":' + enc(encode(state.positions)) + ','
            '"quotes":' + enc(encode(state.quotes)) + ','
            '"reconciled":' + enc(encode(state.reconciled)) + ','
            '"runtime":' + enc(encode(state.runtime)) + ','
            '"sequence":' + enc(encode(state.sequence)) + ','
            '"submissions":' + enc(encode(state.submissions)) +
            '},"type":"PaperState"}'
        )

    def load(self):
        row = self.db.execute("SELECT version,payload FROM snapshot WHERE id=1").fetchone()
        if row is None:
            return None
        if row[0] != contracts.CONTRACT_VERSION:
            raise ValueError("unsupported persisted contract version")
        payload = row[1]
        if self._cached_state is not None and payload == self._cached_payload:
            return _clone_paper_state(self._cached_state)
        loaded = decode(json.loads(payload))
        if type(loaded) is PaperState:
            self._cached_payload = payload
            self._cached_state = _clone_paper_state(loaded)
        return loaded

    def save(self, state):
        if self.max_orders is not None and len(state.orders) > self.max_orders:
            raise ValueError("paper order storage budget exhausted")
        payload = self._dumps_paper_state(state)
        if not self.continuous_capacity and len(payload.encode("utf-8")) > 32 * 1024 * 1024:
            raise ValueError("paper snapshot storage budget exhausted")
        self.db.execute("INSERT INTO snapshot VALUES(1,?,?) ON CONFLICT(id) DO UPDATE SET version=excluded.version,payload=excluded.payload",
                        (contracts.CONTRACT_VERSION, payload))
        if type(state) is PaperState:
            self._cached_payload = payload
            self._cached_state = _clone_paper_state(state)
        else:
            self._cached_payload = None
            self._cached_state = None

    def record_event(self, event):
        if isinstance(event, contracts.FundingEvent):
            saved = self.event_payload(event.event_id)
            if saved is not None:
                previous = decode(json.loads(saved))
                if (not isinstance(previous, contracts.FundingEvent)
                        or contracts.funding_identity_key(previous) != contracts.funding_identity_key(event)):
                    raise DataQualityError("funding event identity conflict")
                return False
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

    def funding_events(self):
        """Yield authoritative confirmed funding settlements in journal insertion order."""
        for cursor, payload in self.db.execute("SELECT rowid, payload FROM events ORDER BY rowid"):
            event = decode(json.loads(payload))
            if isinstance(event, contracts.FundingEvent):
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
