"""Incremental order recovery classification, archive contract, and historical audit."""
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
import hashlib
import json

from factorlab.errors import DataQualityError
from factorlab.integrations.binance.market import _decimal, symbol_name
from factorlab.trading import OrderStatus as S, Side
from factorlab.trading.contracts import TERMINAL
from factorlab.trading.storage import decode
from .account import instant

from .protocol import ExchangeError

ZERO = Decimal('0')

TERMINAL_ARCHIVE_SETTLE_SECONDS = 60
CANCEL_RACE_SETTLE_SECONDS = 120
MIN_TERMINAL_CONFIRMATIONS = 2

TRADE_OVERLAP_IDS = 50
TRADE_PAGE_LIMIT = 1000
TRADE_MAX_PAGES = 50

INCOME_PAGE_LIMIT = 1000
INCOME_MAX_PAGES = 50

MAX_QUERY_WINDOW_MS = 7 * 24 * 3600 * 1000
UNFILLED_CANCEL_RETENTION_SECONDS = 3 * 24 * 3600
EXCHANGE_HISTORY_RETENTION_SECONDS = 90 * 24 * 3600

HISTORY_AUDIT_INTERVAL_SECONDS = 300
HISTORY_AUDIT_INTERVAL_ROUNDS = 30
HISTORY_AUDIT_BATCH_SIZE = 5

NON_TERMINAL_RAW_STATUSES = frozenset({'NEW', 'PARTIALLY_FILLED', 'PENDING_CANCEL'})
TERMINAL_STATUS_VALUES = tuple(status.value for status in TERMINAL)


class OrderRecoveryClass(StrEnum):
    UNKNOWN = 'UNKNOWN'
    ACTIVE = 'ACTIVE'
    ECONOMIC_INCOMPLETE = 'ECONOMIC_INCOMPLETE'
    ARCHIVED_TERMINAL = 'ARCHIVED_TERMINAL'


@dataclass(frozen=True)
class OrderRecoveryConfig:
    terminal_settle_seconds: int | float = TERMINAL_ARCHIVE_SETTLE_SECONDS
    cancel_settle_seconds: int | float = CANCEL_RACE_SETTLE_SECONDS
    min_terminal_confirmations: int = MIN_TERMINAL_CONFIRMATIONS
    trade_overlap_ids: int = TRADE_OVERLAP_IDS
    trade_page_limit: int = TRADE_PAGE_LIMIT
    trade_max_pages: int = TRADE_MAX_PAGES
    history_audit_interval_seconds: int | float = HISTORY_AUDIT_INTERVAL_SECONDS
    history_audit_interval_rounds: int = HISTORY_AUDIT_INTERVAL_ROUNDS
    history_audit_batch_size: int = HISTORY_AUDIT_BATCH_SIZE

    def __post_init__(self):
        if self.terminal_settle_seconds < 0 or self.cancel_settle_seconds < 0:
            raise ValueError('settle seconds must be nonnegative')
        if type(self.min_terminal_confirmations) is not int or self.min_terminal_confirmations < 1:
            raise ValueError('min_terminal_confirmations must be >= 1')
        if type(self.trade_overlap_ids) is not int or self.trade_overlap_ids < 1:
            raise ValueError('trade_overlap_ids must be >= 1')
        if type(self.trade_page_limit) is not int or not 1 <= self.trade_page_limit <= 1000:
            raise ValueError('trade_page_limit must be in 1..1000')
        if type(self.trade_max_pages) is not int or self.trade_max_pages < 1:
            raise ValueError('trade_max_pages must be >= 1')
        if self.history_audit_interval_seconds <= 0:
            raise ValueError('history_audit_interval_seconds must be positive')
        if type(self.history_audit_interval_rounds) is not int or self.history_audit_interval_rounds < 1:
            raise ValueError('history_audit_interval_rounds must be >= 1')
        if type(self.history_audit_batch_size) is not int or self.history_audit_batch_size < 0:
            raise ValueError('history_audit_batch_size must be >= 0')


def init_recovery_schema(db):
    db.execute('''CREATE TABLE IF NOT EXISTS futures_order_recovery (
        client_order_id TEXT PRIMARY KEY,
        symbol TEXT NOT NULL,
        exchange_order_id TEXT,
        raw_status TEXT,
        confirmed_status TEXT NOT NULL,
        confirmed_filled_qty TEXT NOT NULL,
        confirmed_quote TEXT NOT NULL,
        confirmed_fee TEXT NOT NULL,
        trade_count INTEGER NOT NULL DEFAULT 0,
        last_trade_id INTEGER,
        trades_digest TEXT,
        trades_reconciled INTEGER NOT NULL DEFAULT 0,
        last_get_at TEXT,
        last_get_source TEXT,
        exchange_updated_at TEXT,
        terminal_first_get_at TEXT,
        terminal_confirmations INTEGER NOT NULL DEFAULT 0,
        reconcile_required INTEGER NOT NULL DEFAULT 1,
        archive_eligible INTEGER NOT NULL DEFAULT 0,
        archived INTEGER NOT NULL DEFAULT 0,
        archived_at TEXT,
        last_audit_at TEXT,
        audit_reason TEXT
    )''')
    db.execute('''CREATE INDEX IF NOT EXISTS idx_futures_order_recovery_state
        ON futures_order_recovery (archived, reconcile_required, confirmed_status)''')
    db.execute('''CREATE INDEX IF NOT EXISTS idx_futures_order_recovery_exchange_id
        ON futures_order_recovery (symbol, exchange_order_id)''')
    db.execute('''CREATE TABLE IF NOT EXISTS futures_recovery_state (
        scope TEXT PRIMARY KEY,
        payload TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )''')


def load_state(db, scope):
    row = db.execute('SELECT payload FROM futures_recovery_state WHERE scope=?', (scope,)).fetchone()
    return json.loads(row[0]) if row is not None else None


def save_state(db, scope, payload, now_iso):
    encoded = json.dumps(payload, sort_keys=True, separators=(',', ':'))
    db.execute('''INSERT INTO futures_recovery_state VALUES (?,?,?)
        ON CONFLICT(scope) DO UPDATE SET payload=excluded.payload, updated_at=excluded.updated_at''',
        (scope, encoded, now_iso))


def default_global_state():
    return dict(
        has_unknown=False,
        unknown_count=0,
        active_count=0,
        incomplete_count=0,
        archived_count=0,
        realtime_rounds=0,
        rounds_since_audit=0,
        last_realtime_reconcile_at=None,
        last_history_audit_at=None,
        first_archive_at=None,
        history_audit_status='NEVER_RUN',
        history_audit_error=None,
        audit_round=0,
        clock_rollback_count=0,
        pruned_by_exchange_retention=0,
    )


def ensure_global_state(db, now_iso):
    state = load_state(db, 'global')
    if state is None:
        state = default_global_state()
        save_state(db, 'global', state, now_iso)
    else:
        merged = default_global_state()
        merged.update(state)
        state = merged
    return state


def canonical_trade(trade):
    if not isinstance(trade, dict):
        raise DataQualityError('invalid trade row shape')
    trade_id = trade.get('id')
    if type(trade_id) is not int or trade_id < 0:
        raise DataQualityError('invalid trade id')
    symbol = trade.get('symbol')
    if not isinstance(symbol, str) or not symbol:
        raise DataQualityError('invalid trade symbol')
    order_id = trade.get('orderId')
    if order_id is None or type(order_id) is bool or str(order_id) == '':
        raise DataQualityError('invalid trade orderId')
    commission_asset = trade.get('commissionAsset')
    if not isinstance(commission_asset, str) or not commission_asset:
        raise DataQualityError('invalid trade commissionAsset')
    qty = _decimal(trade['qty'])
    quote_qty = _decimal(trade['quoteQty'])
    commission = _decimal(trade['commission'])
    time_ms = trade.get('time')
    if time_ms is not None and (type(time_ms) is not int or time_ms < 0):
        raise DataQualityError('invalid trade timestamp')
    return (
        symbol,
        trade_id,
        str(order_id),
        str(qty),
        str(quote_qty),
        str(commission),
        commission_asset,
    )


def trades_digest(trades):
    canonical = sorted((canonical_trade(trade) for trade in trades), key=lambda row: row[1])
    if len({row[1] for row in canonical}) != len(canonical):
        raise DataQualityError('duplicate actual trades')
    encoded = json.dumps(canonical, separators=(',', ':')).encode()
    return hashlib.sha256(encoded).hexdigest()


def _parse_iso(value):
    return datetime.fromisoformat(value) if value else None


def _increment_clock_rollback(db, now_iso):
    state = ensure_global_state(db, now_iso)
    state['clock_rollback_count'] = int(state.get('clock_rollback_count', 0)) + 1
    save_state(db, 'global', state, now_iso)


def _fetch_recovery_row(db, client_order_id):
    row = db.execute('''SELECT
        client_order_id, symbol, exchange_order_id, raw_status, confirmed_status,
        confirmed_filled_qty, confirmed_quote, confirmed_fee,
        trade_count, last_trade_id, trades_digest, trades_reconciled,
        last_get_at, last_get_source, exchange_updated_at,
        terminal_first_get_at, terminal_confirmations,
        reconcile_required, archive_eligible, archived, archived_at,
        last_audit_at, audit_reason
        FROM futures_order_recovery WHERE client_order_id=?''', (client_order_id,)).fetchone()
    if row is None:
        return None
    columns = (
        'client_order_id', 'symbol', 'exchange_order_id', 'raw_status', 'confirmed_status',
        'confirmed_filled_qty', 'confirmed_quote', 'confirmed_fee',
        'trade_count', 'last_trade_id', 'trades_digest', 'trades_reconciled',
        'last_get_at', 'last_get_source', 'exchange_updated_at',
        'terminal_first_get_at', 'terminal_confirmations',
        'reconcile_required', 'archive_eligible', 'archived', 'archived_at',
        'last_audit_at', 'audit_reason',
    )
    return dict(zip(columns, row))


def _upsert_recovery_row(db, values):
    existing = _fetch_recovery_row(db, values['client_order_id'])
    if existing is not None and existing == values:
        return
    db.execute('''INSERT INTO futures_order_recovery VALUES (
        :client_order_id, :symbol, :exchange_order_id, :raw_status, :confirmed_status,
        :confirmed_filled_qty, :confirmed_quote, :confirmed_fee,
        :trade_count, :last_trade_id, :trades_digest, :trades_reconciled,
        :last_get_at, :last_get_source, :exchange_updated_at,
        :terminal_first_get_at, :terminal_confirmations,
        :reconcile_required, :archive_eligible, :archived, :archived_at,
        :last_audit_at, :audit_reason
    ) ON CONFLICT(client_order_id) DO UPDATE SET
        symbol=excluded.symbol,
        exchange_order_id=excluded.exchange_order_id,
        raw_status=excluded.raw_status,
        confirmed_status=excluded.confirmed_status,
        confirmed_filled_qty=excluded.confirmed_filled_qty,
        confirmed_quote=excluded.confirmed_quote,
        confirmed_fee=excluded.confirmed_fee,
        trade_count=excluded.trade_count,
        last_trade_id=excluded.last_trade_id,
        trades_digest=excluded.trades_digest,
        trades_reconciled=excluded.trades_reconciled,
        last_get_at=excluded.last_get_at,
        last_get_source=excluded.last_get_source,
        exchange_updated_at=excluded.exchange_updated_at,
        terminal_first_get_at=excluded.terminal_first_get_at,
        terminal_confirmations=excluded.terminal_confirmations,
        reconcile_required=excluded.reconcile_required,
        archive_eligible=excluded.archive_eligible,
        archived=excluded.archived,
        archived_at=excluded.archived_at,
        last_audit_at=excluded.last_audit_at,
        audit_reason=excluded.audit_reason''', values)


def sync_order_recovery_on_save(db, record, quote):
    """Keep futures_order_recovery consistent whenever futures_orders is written."""
    snap = record.snapshot
    client_id = snap.intent.client_order_id
    existing = _fetch_recovery_row(db, client_id)
    needs_unarchive = snap.reconcile_required or snap.status not in TERMINAL or record.confirmed_status not in TERMINAL
    if existing is None:
        if not needs_unarchive:
            # Direct/legacy terminal save without authoritative GET metadata stays unarchived
            # until authoritative GET verifies it (handled by LEFT JOIN / classification).
            return
        values = dict(
            client_order_id=client_id,
            symbol=snap.intent.instrument_id,
            exchange_order_id=snap.exchange_order_id,
            raw_status=None,
            confirmed_status=record.confirmed_status.value,
            confirmed_filled_qty=str(snap.cumulative_filled_quantity),
            confirmed_quote=str(quote),
            confirmed_fee=str(snap.cumulative_fee),
            trade_count=0,
            last_trade_id=None,
            trades_digest=None,
            trades_reconciled=0,
            last_get_at=None,
            last_get_source=None,
            exchange_updated_at=record.observed_at.isoformat() if snap.exchange_order_id is not None else None,
            terminal_first_get_at=None,
            terminal_confirmations=0,
            reconcile_required=1 if snap.reconcile_required else 0,
            archive_eligible=0,
            archived=0,
            archived_at=None,
            last_audit_at=None,
            audit_reason='PENDING_AUTHORITATIVE_GET',
        )
        _upsert_recovery_row(db, values)
        return

    updated = dict(existing)
    updated['symbol'] = snap.intent.instrument_id
    updated['exchange_order_id'] = snap.exchange_order_id
    updated['confirmed_status'] = record.confirmed_status.value
    updated['confirmed_filled_qty'] = str(snap.cumulative_filled_quantity)
    updated['confirmed_quote'] = str(quote)
    updated['confirmed_fee'] = str(snap.cumulative_fee)
    updated['reconcile_required'] = 1 if snap.reconcile_required else 0
    if needs_unarchive:
        updated['archive_eligible'] = 0
        updated['archived'] = 0
        updated['archived_at'] = None
        if snap.reconcile_required:
            updated['audit_reason'] = 'RECONCILE_REQUIRED'
    _upsert_recovery_row(db, updated)


def sync_order_recovery_on_apply(db, record, quote, *, now, config, recovery_meta=None):
    """Atomically advance per-order recovery metadata inside the same transaction as _apply."""
    snap = record.snapshot
    client_id = snap.intent.client_order_id
    now_iso = now.isoformat()
    existing = _fetch_recovery_row(db, client_id)

    # Local rejection before exchange acceptance (no exchange_order_id).
    if snap.status == S.REJECTED and snap.exchange_order_id is None and not snap.reconcile_required:
        values = dict(
            client_order_id=client_id,
            symbol=snap.intent.instrument_id,
            exchange_order_id=None,
            raw_status='REJECTED',
            confirmed_status=S.REJECTED.value,
            confirmed_filled_qty=str(snap.cumulative_filled_quantity),
            confirmed_quote=str(quote),
            confirmed_fee=str(snap.cumulative_fee),
            trade_count=0,
            last_trade_id=None,
            trades_digest=trades_digest(()),
            trades_reconciled=1,
            last_get_at=now_iso,
            last_get_source='LOCAL_REJECT',
            exchange_updated_at=record.observed_at.isoformat(),
            terminal_first_get_at=now_iso,
            terminal_confirmations=config.min_terminal_confirmations,
            reconcile_required=0,
            archive_eligible=1,
            archived=1,
            archived_at=existing['archived_at'] if existing and existing['archived_at'] else now_iso,
            last_audit_at=now_iso,
            audit_reason='LOCAL_REJECTED_NO_EXCHANGE_ID',
        )
        _upsert_recovery_row(db, values)
        return values

    source = recovery_meta.get('source') if recovery_meta else None
    raw_status = recovery_meta.get('raw_status') if recovery_meta else snap.status.value
    trades = recovery_meta.get('trades') if recovery_meta else None
    exchange_updated_at = (
        recovery_meta['exchange_updated_at'].isoformat()
        if recovery_meta and recovery_meta.get('exchange_updated_at') is not None
        else (record.observed_at.isoformat() if snap.exchange_order_id is not None else None)
    )

    if source == 'GET':
        trade_list = list(trades or ())
        digest = trades_digest(trade_list)
        trade_count = len(trade_list)
        last_trade_id = max((t['id'] for t in trade_list), default=None)
        trades_reconciled = 1

        local_clock_rollback = False
        prev_get = _parse_iso(existing['last_get_at']) if existing else None
        if prev_get is not None and now < prev_get:
            local_clock_rollback = True
            _increment_clock_rollback(db, now_iso)

        is_terminal = (
            snap.status in TERMINAL
            and record.confirmed_status in TERMINAL
            and not snap.reconcile_required
            and raw_status not in NON_TERMINAL_RAW_STATUSES
            and snap.exchange_order_id is not None
        )

        same_terminal_snapshot = False
        if is_terminal and existing is not None and existing['last_get_source'] == 'GET':
            same_terminal_snapshot = (
                existing['confirmed_status'] == record.confirmed_status.value
                and existing['raw_status'] == raw_status
                and existing['exchange_order_id'] == snap.exchange_order_id
                and _decimal(existing['confirmed_filled_qty']) == snap.cumulative_filled_quantity
                and _decimal(existing['confirmed_quote']) == quote
                and _decimal(existing['confirmed_fee']) == snap.cumulative_fee
                and existing['trades_digest'] == digest
                and existing['trades_reconciled'] == 1
            )

        if is_terminal:
            if same_terminal_snapshot:
                prev_conf = int(existing['terminal_confirmations'])
                if existing.get('archived') == 1 and existing.get('last_get_at') == now_iso:
                    terminal_confirmations = prev_conf
                else:
                    terminal_confirmations = prev_conf + 1
                terminal_first_get_at = existing['terminal_first_get_at'] or now_iso
            else:
                terminal_confirmations = 1
                terminal_first_get_at = now_iso
        else:
            terminal_confirmations = 0
            terminal_first_get_at = None

        settle_seconds = (
            config.cancel_settle_seconds if record.confirmed_status == S.CANCELED
            else config.terminal_settle_seconds
        )
        exchange_updated_dt = _parse_iso(exchange_updated_at)
        first_get_dt = _parse_iso(terminal_first_get_at)
        exchange_age = (now - exchange_updated_dt).total_seconds() if exchange_updated_dt is not None else -1
        observation_age = (now - first_get_dt).total_seconds() if first_get_dt is not None else 0
        if observation_age < 0 and not local_clock_rollback:
            local_clock_rollback = True
            _increment_clock_rollback(db, now_iso)

        archive_eligible = int(
            is_terminal
            and not local_clock_rollback
            and trades_reconciled == 1
            and terminal_confirmations >= config.min_terminal_confirmations
            and exchange_age >= settle_seconds
            and observation_age >= 0
        )
        archived = archive_eligible
        archived_at = (
            (existing['archived_at'] if existing and existing['archived_at'] else now_iso)
            if archived else None
        )
        audit_reason = (
            'CONFIRMED_TERMINAL_ARCHIVED' if archived
            else 'CLOCK_ROLLBACK_HOLD' if (is_terminal and local_clock_rollback)
            else 'TERMINAL_SETTLE_WINDOW' if is_terminal
            else 'ACTIVE_ORDER'
        )
        last_get_at = prev_get.isoformat() if (local_clock_rollback and prev_get is not None) else now_iso
        values = dict(
            client_order_id=client_id,
            symbol=snap.intent.instrument_id,
            exchange_order_id=snap.exchange_order_id,
            raw_status=raw_status,
            confirmed_status=record.confirmed_status.value,
            confirmed_filled_qty=str(snap.cumulative_filled_quantity),
            confirmed_quote=str(quote),
            confirmed_fee=str(snap.cumulative_fee),
            trade_count=trade_count,
            last_trade_id=last_trade_id,
            trades_digest=digest,
            trades_reconciled=trades_reconciled,
            last_get_at=last_get_at,
            last_get_source='GET',
            exchange_updated_at=exchange_updated_at,
            terminal_first_get_at=terminal_first_get_at,
            terminal_confirmations=terminal_confirmations,
            reconcile_required=1 if snap.reconcile_required else 0,
            archive_eligible=archive_eligible,
            archived=archived,
            archived_at=archived_at,
            last_audit_at=now_iso if not local_clock_rollback else (existing['last_audit_at'] if existing else None),
            audit_reason=audit_reason,
        )
        _upsert_recovery_row(db, values)
        if archived:
            state = ensure_global_state(db, now_iso)
            if state.get('first_archive_at') is None:
                state['first_archive_at'] = now_iso
                save_state(db, 'global', state, now_iso)
        return values

    # Non-GET confirmations (WRITE_ACK / WS): record progress, never archive until GET confirms.
    values = dict(
        client_order_id=client_id,
        symbol=snap.intent.instrument_id,
        exchange_order_id=snap.exchange_order_id,
        raw_status=raw_status,
        confirmed_status=record.confirmed_status.value,
        confirmed_filled_qty=str(snap.cumulative_filled_quantity),
        confirmed_quote=str(quote),
        confirmed_fee=str(snap.cumulative_fee),
        trade_count=len(trades) if trades is not None else (existing['trade_count'] if existing else 0),
        last_trade_id=(max((t['id'] for t in trades), default=None) if trades is not None
                       else (existing['last_trade_id'] if existing else None)),
        trades_digest=(trades_digest(trades) if trades is not None
                       else (existing['trades_digest'] if existing else None)),
        trades_reconciled=0,
        last_get_at=existing['last_get_at'] if existing else None,
        last_get_source=source or (existing['last_get_source'] if existing else 'NON_GET'),
        exchange_updated_at=exchange_updated_at,
        terminal_first_get_at=None,
        terminal_confirmations=0,
        reconcile_required=1 if snap.reconcile_required else 0,
        archive_eligible=0,
        archived=0,
        archived_at=None,
        last_audit_at=existing['last_audit_at'] if existing else None,
        audit_reason='AWAITING_AUTHORITATIVE_GET',
    )
    _upsert_recovery_row(db, values)
    return values


def mark_order_unarchived(db, client_order_id, reason):
    db.execute('''UPDATE futures_order_recovery
        SET archived=0, archive_eligible=0, terminal_confirmations=0,
            archived_at=NULL, audit_reason=?
        WHERE client_order_id=?''', (reason, client_order_id))


def latest_receipt_for_order(db, client_order_id):
    row = db.execute('''SELECT observed_at, payload FROM futures_receipts
        WHERE client_id=? ORDER BY rowid DESC LIMIT 1''', (client_order_id,)).fetchone()
    if row is None:
        return None
    return row[0], json.loads(row[1])


def verify_persisted_receipt(db, client_order_id, record, quote):
    """Verify latest persisted GET receipt matches the confirmed order and trade totals."""
    latest = latest_receipt_for_order(db, client_order_id)
    if latest is None:
        return False, (), None, None
    _, receipt = latest
    if receipt.get('source') != 'GET':
        return False, (), None, None
    order = receipt.get('order')
    trades = receipt.get('trades')
    if not isinstance(order, dict) or not isinstance(trades, list):
        return False, (), None, None
    snap = record.snapshot
    if (order.get('clientOrderId') != client_order_id
            or str(order.get('orderId')) != snap.exchange_order_id
            or order.get('symbol') != snap.intent.instrument_id):
        return False, (), None, None
    try:
        executed_qty = _decimal(order['executedQty'])
        cum_quote = _decimal(order['cumQuote'])
        if executed_qty != snap.cumulative_filled_quantity or cum_quote != quote:
            return False, (), None, None
        digest = trades_digest(trades)
        if executed_qty > ZERO:
            if any(str(t['orderId']) != str(order['orderId']) or t['symbol'] != order['symbol']
                   or t['commissionAsset'] != 'USDT' for t in trades):
                return False, (), None, None
            if sum((_decimal(t['qty']) for t in trades), ZERO) != executed_qty:
                return False, (), None, None
            if sum((_decimal(t['quoteQty']) for t in trades), ZERO) != cum_quote:
                return False, (), None, None
            if sum((_decimal(t['commission']) for t in trades), ZERO) != snap.cumulative_fee:
                return False, (), None, None
        elif trades or snap.cumulative_fee != ZERO:
            return False, (), None, None
    except (KeyError, ValueError, TypeError, DataQualityError):
        return False, (), None, None
    last_trade_id = max((t['id'] for t in trades), default=None)
    return True, tuple(trades), digest, last_trade_id


def classify_orders(db):
    """Classify persisted orders into UNKNOWN, ACTIVE, ECONOMIC_INCOMPLETE, ARCHIVED_TERMINAL.

    Archived terminal orders with valid SQL metadata are not JSON-decoded on the hot path,
    keeping real-time classification cost O(active + unknown + incomplete).
    """
    unknown_ids = []
    active_entries = []
    incomplete_ids = []
    archived_exchange_ids = []
    local_rejected_ids = []

    rows = db.execute('''SELECT
            o.rowid, o.id, o.quote,
            r.client_order_id, r.exchange_order_id, r.raw_status, r.confirmed_status,
            r.confirmed_quote, r.trades_reconciled, r.last_get_source,
            r.reconcile_required, r.archive_eligible, r.archived
        FROM futures_orders o
        LEFT JOIN futures_order_recovery r ON o.id = r.client_order_id
        ORDER BY o.rowid''').fetchall()

    need_decode_ids = []
    for (rowid, client_id, order_quote, has_meta, ex_id, raw_status, confirmed_status,
         confirmed_quote, trades_reconciled, last_get_source,
         reconcile_required, archive_eligible, archived) in rows:
        if (has_meta is not None
                and archived == 1
                and archive_eligible == 1
                and reconcile_required == 0
                and confirmed_status in TERMINAL_STATUS_VALUES
                and raw_status not in NON_TERMINAL_RAW_STATUSES
                and trades_reconciled == 1
                and order_quote == confirmed_quote):
            if confirmed_status == S.REJECTED.value and ex_id is None and last_get_source == 'LOCAL_REJECT':
                local_rejected_ids.append(client_id)
                continue
            if ex_id is not None and last_get_source == 'GET':
                archived_exchange_ids.append(client_id)
                continue
        need_decode_ids.append((rowid, client_id, has_meta, raw_status))

    if need_decode_ids:
        placeholders = ','.join('?' for _ in need_decode_ids)
        payloads = dict(db.execute(
            f'SELECT id, payload FROM futures_orders WHERE id IN ({placeholders})',
            [item[1] for item in need_decode_ids],
        ).fetchall())
        for rowid, client_id, _, raw_status in need_decode_ids:
            record = decode(json.loads(payloads[client_id]))
            snap = record.snapshot
            if snap.status == S.REJECTED and snap.exchange_order_id is None and not snap.reconcile_required:
                local_rejected_ids.append(client_id)
            elif snap.reconcile_required or snap.status == S.UNKNOWN or (
                record.confirmed_status == S.NEW and snap.exchange_order_id is None
            ):
                unknown_ids.append(client_id)
            elif snap.status not in TERMINAL or raw_status in NON_TERMINAL_RAW_STATUSES:
                # Preserve F11 active reduce-only SELL reconciliation priority.
                priority = 0 if (snap.intent.reduce_only and snap.intent.side is Side.SELL) else 1
                active_entries.append((priority, rowid, client_id))
            else:
                incomplete_ids.append(client_id)

    active_entries.sort(key=lambda item: (item[0], item[1]))
    active_ids = [client_id for _, _, client_id in active_entries]
    return dict(
        unknown=tuple(unknown_ids),
        active=tuple(active_ids),
        economic_incomplete=tuple(incomplete_ids),
        archived=tuple(archived_exchange_ids) + tuple(local_rejected_ids),
        archived_exchange=tuple(archived_exchange_ids),
        local_rejected=tuple(local_rejected_ids),
    )


def order_consistency_rows(db):
    """Single-query order summary for gateway consistency checks without decoding archived JSON."""
    rows = db.execute('''SELECT
            o.id, o.payload,
            r.archived, r.reconcile_required, r.confirmed_status,
            r.confirmed_filled_qty, r.confirmed_fee, r.exchange_order_id,
            r.last_get_source, r.trades_reconciled
        FROM futures_orders o
        LEFT JOIN futures_order_recovery r ON o.id = r.client_order_id''').fetchall()
    result = {}
    for (client_id, payload, archived, rec_req, conf_status,
         conf_filled, conf_fee, ex_id, last_source, trades_rec) in rows:
        if (archived == 1 and rec_req == 0 and conf_status in TERMINAL_STATUS_VALUES
                and trades_rec == 1 and last_source in {'GET', 'LOCAL_REJECT'}):
            result[client_id] = (
                S(conf_status),
                _decimal(conf_filled),
                _decimal(conf_fee),
                False,
                ex_id,
            )
        else:
            snap = decode(json.loads(payload)).snapshot
            result[client_id] = (
                snap.status,
                snap.cumulative_filled_quantity,
                snap.cumulative_fee,
                snap.reconcile_required,
                snap.exchange_order_id,
            )
    return result


def account_baseline(facts):
    """Extract comparable wallet + position signature from complete AccountFacts."""
    if facts is None or not facts.source_complete:
        return None
    wallet = facts.usdt().get('walletBalance')
    if wallet is None:
        return None
    try:
        positions = facts.confirmed_position_amounts()
    except DataQualityError:
        return None
    return dict(
        wallet=str(wallet),
        positions=[[symbol, side, str(qty)] for (symbol, side), qty in sorted(positions.items())],
        observed_at=facts.observed_at.isoformat(),
    )


def baseline_deltas(previous, current):
    """Return (position_deltas_by_symbol, wallet_delta) between two account baselines."""
    prev_pos = {(symbol, side): _decimal(qty) for symbol, side, qty in previous['positions']}
    curr_pos = {(symbol, side): _decimal(qty) for symbol, side, qty in current['positions']}
    all_keys = set(prev_pos) | set(curr_pos)
    pos_deltas = {}
    for symbol, side in all_keys:
        diff = curr_pos.get((symbol, side), ZERO) - prev_pos.get((symbol, side), ZERO)
        if diff != ZERO:
            pos_deltas[symbol] = pos_deltas.get(symbol, ZERO) + diff
    wallet_delta = _decimal(current['wallet']) - _decimal(previous['wallet'])
    return pos_deltas, wallet_delta


def audit_due(db, *, now, config):
    """Decide whether a periodic historical audit should run."""
    archived_exchange = db.execute('''SELECT 1 FROM futures_order_recovery
        WHERE archived=1 AND exchange_order_id IS NOT NULL LIMIT 1''').fetchone()
    if archived_exchange is None:
        return False, None, False
    state = load_state(db, 'global')
    if state is None:
        return False, None, False
    if state.get('history_audit_status') in {'CONFLICT', 'DEGRADED_GAP'}:
        return True, 'PRIOR_AUDIT_CONFLICT', False
    last_audit = _parse_iso(state.get('last_history_audit_at'))
    if last_audit is not None:
        elapsed = (now - last_audit).total_seconds()
        if elapsed < 0:
            return True, 'CLOCK_ROLLBACK', True
        if elapsed >= config.history_audit_interval_seconds:
            return True, 'INTERVAL_SECONDS', False
    else:
        first_archive = _parse_iso(state.get('first_archive_at'))
        if first_archive is not None:
            elapsed = (now - first_archive).total_seconds()
            if elapsed < 0:
                return True, 'CLOCK_ROLLBACK', True
            if elapsed >= config.history_audit_interval_seconds:
                return True, 'INITIAL_AUDIT_INTERVAL', False
    if int(state.get('rounds_since_audit', 0)) >= config.history_audit_interval_rounds:
        return True, 'INTERVAL_ROUNDS', False
    return False, None, False


def fetch_user_trades_paginated(adapter, symbol, *, from_id=None, start_time=None, end_time=None,
                                limit=TRADE_PAGE_LIMIT, max_pages=TRADE_MAX_PAGES):
    """Paginate GET /fapi/v1/userTrades within official Binance USD-M constraints.

    Enforces:
    - fromId cannot be combined with startTime/endTime.
    - startTime..endTime windows cannot exceed 7 days; longer ranges are chunked explicitly.
    - Intra-page duplicate trade IDs fail closed.
    - Cross-page overlapping trade IDs deduplicate only when payloads are identical; conflicts fail closed.
    - Time bounds survive the switch to fromId; bounded scans require increasing IDs
      and nondecreasing timestamps, including every row in the final page.
    - Non-advancing saturated pages or exceeding max_pages fail closed.
    """
    target_symbol = symbol_name(symbol)
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError('userTrades limit must be in 1..1000')
    if type(max_pages) is not int or max_pages < 1:
        raise ValueError('max_pages must be >= 1')
    if from_id is not None and (start_time is not None or end_time is not None):
        raise ValueError('Binance userTrades forbids combining fromId with startTime/endTime')
    if start_time is not None and end_time is not None and end_time < start_time:
        raise ValueError('endTime precedes startTime')

    seen = {}
    ordered = []
    pages = 0
    time_bounded = start_time is not None or end_time is not None
    last_new_id = None
    last_new_time = None

    def ingest_page(rows):
        nonlocal last_new_id, last_new_time
        if not isinstance(rows, list) or len(rows) > limit:
            raise DataQualityError('invalid userTrades page shape')
        page_ids = set()
        page_last_id = None
        page_last_time = None
        for row in rows:
            canon = canonical_trade(row)
            if canon[0] != target_symbol:
                raise DataQualityError('userTrades symbol mismatch')
            trade_id = canon[1]
            if trade_id in page_ids:
                raise DataQualityError('duplicate actual trades')
            page_ids.add(trade_id)
            time_ms = row.get('time')
            if time_bounded and time_ms is None:
                raise DataQualityError('missing trade timestamp in bounded history')
            if page_last_id is not None and trade_id <= page_last_id:
                raise DataQualityError('trade history incomplete; trade IDs are not ordered')
            if time_bounded and page_last_time is not None and time_ms < page_last_time:
                raise DataQualityError('trade history incomplete; timestamps are not monotonic')
            page_last_id, page_last_time = trade_id, time_ms
            value = (canon, time_ms)
            if trade_id in seen:
                if seen[trade_id] != value:
                    raise DataQualityError('conflicting duplicate trade across pages')
            else:
                if ((last_new_id is not None and trade_id <= last_new_id)
                        or (from_id is not None and trade_id < from_id)):
                    raise DataQualityError('trade history incomplete; pagination did not advance')
                if time_bounded and last_new_time is not None and time_ms < last_new_time:
                    raise DataQualityError('trade history incomplete; timestamps are not monotonic')
                seen[trade_id] = value
                last_new_id, last_new_time = trade_id, time_ms
                if ((start_time is None or time_ms >= start_time)
                        and (end_time is None or time_ms <= end_time)):
                    ordered.append(row)
        return page_ids

    if start_time is not None and end_time is not None and end_time - start_time > MAX_QUERY_WINDOW_MS:
        cursor_start = start_time
        while cursor_start <= end_time:
            chunk_end = min(end_time, cursor_start + MAX_QUERY_WINDOW_MS)
            chunk_rows, chunk_pages = fetch_user_trades_paginated(
                adapter, target_symbol, start_time=cursor_start, end_time=chunk_end,
                limit=limit, max_pages=max_pages - pages,
            )
            pages += chunk_pages
            for row in chunk_rows:
                canon = canonical_trade(row)
                trade_id = canon[1]
                value = (canon, row['time'])
                if trade_id in seen:
                    if seen[trade_id] != value:
                        raise DataQualityError('conflicting duplicate trade across windows')
                else:
                    if ordered and trade_id <= ordered[-1]['id']:
                        raise DataQualityError('trade history incomplete; trade IDs are not ordered across windows')
                    seen[trade_id] = value
                    ordered.append(row)
            cursor_start = chunk_end + 1
            if pages >= max_pages and cursor_start <= end_time:
                raise DataQualityError('trade history incomplete; pagination page cap exceeded')
        ordered.sort(key=lambda row: row['id'])
        return tuple(ordered), pages

    current_from_id = from_id
    use_time_first = (from_id is None and (start_time is not None or end_time is not None))
    previous_max_id = None

    while True:
        if pages >= max_pages:
            raise DataQualityError('trade history incomplete; pagination page cap exceeded')
        params = {'symbol': target_symbol, 'limit': limit}
        if use_time_first:
            if start_time is not None:
                params['startTime'] = start_time
            if end_time is not None:
                params['endTime'] = end_time
            use_time_first = False
        elif current_from_id is not None:
            params['fromId'] = current_from_id
        rows = adapter.client.request('GET', '/fapi/v1/userTrades', params)
        pages += 1
        page_ids = ingest_page(rows)
        # Validate the WHOLE page before stopping: a later row with an earlier
        # timestamp invalidates this proof of coverage. Equality is insufficient
        # because further IDs can share end_time. No time bound is invented for
        # pure fromId scans.
        if end_time is not None and rows and rows[-1]['time'] > end_time:
            break
        if len(rows) < limit:
            break
        max_id = max(page_ids)
        if previous_max_id is not None and max_id <= previous_max_id:
            raise DataQualityError('trade history incomplete; pagination did not advance')
        previous_max_id = max_id
        current_from_id = max_id + 1

    ordered.sort(key=lambda row: row['id'])
    return tuple(ordered), pages


INCOME_BUSINESS_FIELDS = ('symbol', 'incomeType', 'income', 'asset', 'time', 'tranId', 'tradeId', 'info')


def canonical_tran_id(value):
    if type(value) is int and value > 0:
        return str(value)
    if isinstance(value, str):
        text = value.strip()
        if text and text == value and len(text) <= 128:
            return text
    raise DataQualityError('invalid income tranId')


def sanitize_income_row(row):
    return {key: row[key] for key in INCOME_BUSINESS_FIELDS if key in row}


def validate_income_row(row):
    if not isinstance(row, dict):
        raise DataQualityError('invalid income response row')
    time_ms = row.get('time')
    instant(time_ms)
    income_type = row.get('incomeType')
    if not isinstance(income_type, str) or not income_type.strip():
        raise DataQualityError('invalid income type')
    raw_amount = row.get('income')
    if type(raw_amount) is bool or raw_amount is None:
        raise DataQualityError('invalid income amount')
    amount = _decimal(raw_amount)
    asset = row.get('asset')
    if not isinstance(asset, str) or not asset.strip():
        raise DataQualityError('invalid income asset')
    symbol = row.get('symbol')
    if not isinstance(symbol, str):
        raise DataQualityError('invalid income symbol')
    if income_type == 'FUNDING_FEE' and not symbol.strip():
        raise DataQualityError('funding income requires nonempty symbol')
    tran_id = canonical_tran_id(row.get('tranId'))
    raw_trade_id = row.get('tradeId', '')
    if raw_trade_id is None:
        trade_id = ''
    elif type(raw_trade_id) is bool or not isinstance(raw_trade_id, (str, int)):
        raise DataQualityError('invalid income tradeId')
    else:
        trade_id = str(raw_trade_id)
    return time_ms, income_type, amount, asset, symbol, tran_id, trade_id, sanitize_income_row(row)


def fetch_income_paginated(adapter, symbol=None, *, income_type=None, start_time=None, end_time=None,
                           limit=INCOME_PAGE_LIMIT, max_pages=INCOME_MAX_PAGES):
    """Paginate GET /fapi/v1/income independently of order/trade fee accounting."""
    target_symbol = symbol_name(symbol) if symbol is not None else None
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError('income limit must be in 1..1000')
    if type(max_pages) is not int or max_pages < 1:
        raise ValueError('max_pages must be >= 1')
    if start_time is not None and end_time is not None and end_time < start_time:
        raise ValueError('endTime precedes startTime')

    seen = {}
    non_funding_seen = Counter()
    non_funding_scan = Counter()
    ordered = []
    pages = 0
    cursor_start = start_time
    page_num = None
    previous_max_time = None
    boundary_keys = set()
    boundary_non_funding = Counter()

    while True:
        if pages >= max_pages:
            raise DataQualityError('income history incomplete; max pages (page cap) exceeded')
        params = {'limit': limit}
        if target_symbol is not None:
            params['symbol'] = target_symbol
        if income_type is not None:
            params['incomeType'] = income_type
        if cursor_start is not None:
            params['startTime'] = cursor_start
        if end_time is not None:
            params['endTime'] = end_time
        if page_num is not None:
            params['page'] = page_num
        rows = adapter.client.request('GET', '/fapi/v1/income', params)
        pages += 1
        if not isinstance(rows, list) or len(rows) > limit:
            raise DataQualityError('invalid income page shape')
        page_keys = set()
        page_non_funding = set()
        times = []
        new_in_page = 0
        for row in rows:
            time_ms, row_type, amount, asset, row_symbol, tran_id, trade_id, sanitized = validate_income_row(row)
            if income_type is not None and row_type != income_type:
                raise DataQualityError('unexpected incomeType in filtered income response')
            if target_symbol is not None and row_symbol != target_symbol:
                raise DataQualityError('unexpected symbol in filtered income response')
            if ((cursor_start is not None and time_ms < cursor_start)
                    or (end_time is not None and time_ms > end_time)):
                raise DataQualityError('income timestamp outside requested window')
            if times and time_ms < times[-1]:
                raise DataQualityError('income history incomplete; timestamps are not monotonic')
            times.append(time_ms)
            identity = (row_type, tran_id)
            canonical = (time_ms, amount, asset, row_symbol, trade_id)
            if row_type != 'FUNDING_FEE':
                # Mixed income reads are also evidence for fee reconciliation.
                # Preserve multiplicity while removing the inclusive rescan.
                key = (identity, canonical)
                page_non_funding.add(key)
                non_funding_scan[key] += 1
                if non_funding_scan[key] > non_funding_seen[key]:
                    non_funding_seen[key] += 1
                    ordered.append(sanitized)
                    new_in_page += 1
                continue
            if identity in page_keys:
                raise DataQualityError('duplicate income entries within single page')
            page_keys.add(identity)
            if identity in seen:
                if seen[identity] != canonical:
                    raise DataQualityError('conflicting duplicate income entry across pages')
            else:
                seen[identity] = canonical
                ordered.append(sanitized)
                new_in_page += 1
        boundary_keys.difference_update(page_keys)
        if boundary_keys or boundary_non_funding:
            crossed_boundary = len(rows) < limit or times[-1] > cursor_start
            if crossed_boundary and (boundary_keys or boundary_non_funding - non_funding_scan):
                raise DataQualityError('income pagination incomplete: boundary rows missing between pages')
        # Re-reading page 1 at the inclusive timestamp boundary is expected.
        # Try page 2 before declaring a stall. A short page proves exhaustion,
        # including the already seen tail of an inclusive boundary window.
        if len(rows) < limit:
            break
        if new_in_page == 0 and page_num is not None:
            raise DataQualityError('income window incomplete; pagination did not advance')
        max_time = max(times)
        if previous_max_time is not None and max_time < previous_max_time:
            raise DataQualityError('income window incomplete; pagination did not advance')
        if new_in_page == 0 and max_time != previous_max_time:
            raise DataQualityError('income window incomplete; pagination did not advance')
        if previous_max_time is not None and max_time == previous_max_time:
            page_num = 2 if page_num is None else page_num + 1
        else:
            boundary_keys = {identity for identity in page_keys if seen[identity][0] == max_time}
            boundary_non_funding = Counter({
                key: non_funding_seen[key] for key in page_non_funding if key[1][0] == max_time
            })
            cursor_start = max_time
            non_funding_scan.clear()
            page_num = None
            previous_max_time = max_time

    ordered.sort(key=lambda row: (row['time'], str(row['tranId']), row['incomeType']))
    return tuple(ordered), pages


def _latch_audit_failure(adapter, status, error_text):
    now_iso = adapter.clock().isoformat()
    with adapter.store.transaction():
        adapter.db.execute('UPDATE futures_meta SET reconciled=0')
        state = ensure_global_state(adapter.db, now_iso)
        state['history_audit_status'] = status
        state['history_audit_error'] = error_text
        save_state(adapter.db, 'global', state, now_iso)


def _is_exchange_retention_pruned(row_meta, now):
    updated_at = _parse_iso(row_meta.get('exchange_updated_at'))
    if updated_at is None:
        return False
    age = (now - updated_at).total_seconds()
    if age < 0:
        return False
    if age > EXCHANGE_HISTORY_RETENTION_SECONDS:
        return True
    if (row_meta.get('confirmed_status') == S.CANCELED.value
            and _decimal(row_meta.get('confirmed_filled_qty', '0')) == ZERO
            and age > UNFILLED_CANCEL_RETENTION_SECONDS):
        return True
    return False


def audit_history(adapter, *, symbols=None, audit_batch_size=None, include_income=False,
                  force=False, reason='SCHEDULED', expand_full_trades=False):
    """Tier-2 historical economic ledger audit.

    Discovers late fills, fee/commission corrections, and persisted-record conflicts
    across archived terminal orders using bounded incremental GET /fapi/v1/userTrades
    (with overlap and full pagination) plus O(batch_size) rolling archived-order checks.

    Watermark updates commit strictly AFTER all affected orders' economic events commit.
    """
    adapter._validate_network_identity()
    config = adapter.recovery_config
    now = adapter.clock()
    now_iso = now.isoformat()
    now_ms = int(now.timestamp() * 1000)

    if symbols is None:
        rows = adapter.db.execute(
            'SELECT DISTINCT symbol FROM futures_order_recovery ORDER BY symbol'
        ).fetchall()
        symbol_list = [row[0] for row in rows]
        if not symbol_list:
            orders = adapter.db.execute('SELECT payload FROM futures_orders').fetchall()
            symbol_list = sorted({decode(json.loads(raw)).snapshot.intent.instrument_id for (raw,) in orders})
    else:
        symbol_list = [symbol_name(s) for s in symbols]

    batch_size = config.history_audit_batch_size if audit_batch_size is None else audit_batch_size
    summary = dict(
        reason=reason,
        started_at=now_iso,
        symbols=tuple(symbol_list),
        trade_pages=0,
        trades_scanned=0,
        orders_rechecked=0,
        rolling_orders_audited=0,
        late_discoveries=[],
        pruned_by_exchange_retention=[],
        income_sync=None,
        expanded_scan=False,
        clock_rollback_detected=False,
        coverage_complete=True,
    )

    pending_watermarks = {}

    for sym in symbol_list:
        scope = f'trades:{sym}'
        watermark = load_state(adapter.db, scope) or dict(
            last_trade_id=None,
            last_trade_time_ms=None,
            last_scan_at=None,
            overlap_ids=config.trade_overlap_ids,
            coverage_complete=True,
            coverage_note=None,
        )
        prev_last_id = watermark.get('last_trade_id')
        prev_last_time_ms = watermark.get('last_trade_time_ms')
        prev_scan_at = _parse_iso(watermark.get('last_scan_at'))

        if (prev_scan_at is not None and now < prev_scan_at) or (
                prev_last_time_ms is not None and now_ms < prev_last_time_ms):
            summary['clock_rollback_detected'] = True
            with adapter.store.transaction():
                _increment_clock_rollback(adapter.db, now_iso)

        if expand_full_trades or prev_last_id is None:
            query_from_id = 0
            summary['expanded_scan'] = True
        else:
            query_from_id = max(0, prev_last_id - config.trade_overlap_ids + 1)

        try:
            fetched_trades, pages = fetch_user_trades_paginated(
                adapter, sym, from_id=query_from_id,
                limit=config.trade_page_limit, max_pages=config.trade_max_pages,
            )
        except (DataQualityError, ValueError, ExchangeError) as exc:
            _latch_audit_failure(adapter, 'DEGRADED_GAP', str(exc))
            raise

        summary['trade_pages'] += pages
        summary['trades_scanned'] += len(fetched_trades)

        # Retention gap detection: if offline longer than 90 days and exchange IDs skipped past our watermark.
        if (prev_last_id is not None and prev_last_time_ms is not None
                and now_ms - prev_last_time_ms > EXCHANGE_HISTORY_RETENTION_SECONDS * 1000
                and fetched_trades and min(t['id'] for t in fetched_trades) > prev_last_id + 1):
            summary['coverage_complete'] = False
            _latch_audit_failure(adapter, 'DEGRADED_GAP', 'historical trade retention boundary exceeded')
            raise DataQualityError('historical trade retention boundary exceeded; completeness unprovable')

        # Index local orders by exchange_order_id for this symbol.
        ex_to_client = {}
        local_meta_by_client = {}
        for row in adapter.db.execute('''SELECT
                client_order_id, exchange_order_id, confirmed_status,
                confirmed_filled_qty, confirmed_quote, confirmed_fee,
                trade_count, last_trade_id, trades_digest, trades_reconciled,
                exchange_updated_at, archived
            FROM futures_order_recovery WHERE symbol=?''', (sym,)).fetchall():
            cid = row[0]
            local_meta_by_client[cid] = dict(
                client_order_id=cid, exchange_order_id=row[1], confirmed_status=row[2],
                confirmed_filled_qty=row[3], confirmed_quote=row[4], confirmed_fee=row[5],
                trade_count=row[6], last_trade_id=row[7], trades_digest=row[8],
                trades_reconciled=row[9], exchange_updated_at=row[10], archived=row[11],
            )
            if row[1] is not None:
                ex_to_client[str(row[1])] = cid

        # Also check any legacy futures_orders without recovery rows.
        for cid, raw in adapter.db.execute('''SELECT o.id, o.payload FROM futures_orders o
                LEFT JOIN futures_order_recovery r ON o.id = r.client_order_id
                WHERE r.client_order_id IS NULL''').fetchall():
            snap = decode(json.loads(raw)).snapshot
            if snap.intent.instrument_id == sym and snap.exchange_order_id is not None:
                ex_to_client[str(snap.exchange_order_id)] = cid

        fetched_by_order = {}
        fetched_by_id = {}
        for trade in fetched_trades:
            canon = canonical_trade(trade)
            fetched_by_id[canon[1]] = canon
            fetched_by_order.setdefault(canon[2], []).append(canon)

        affected_clients = []
        unmatched_new_trades = []
        for ex_id, canons in fetched_by_order.items():
            cid = ex_to_client.get(ex_id)
            if cid is None:
                for canon in canons:
                    if prev_last_id is not None and canon[1] > prev_last_id:
                        unmatched_new_trades.append(canon)
                continue
            latest = latest_receipt_for_order(adapter.db, cid)
            persisted_map = {}
            if latest is not None and isinstance(latest[1].get('trades'), list):
                for saved_trade in latest[1]['trades']:
                    saved_canon = canonical_trade(saved_trade)
                    persisted_map[saved_canon[1]] = saved_canon
            needs_query = False
            for canon in canons:
                if canon[1] not in persisted_map or persisted_map[canon[1]] != canon:
                    needs_query = True
                    break
            if needs_query:
                affected_clients.append((cid, 'INCREMENTAL_TRADE_DELTA'))

        # Reverse check: detect confirmed trades that disappeared from exchange within scan window.
        for cid, meta in local_meta_by_client.items():
            filled_qty = _decimal(meta['confirmed_filled_qty'])
            if meta['exchange_order_id'] is None:
                continue
            if meta['last_trade_id'] is None and filled_qty == ZERO:
                continue
            if _is_exchange_retention_pruned(meta, now):
                continue
            latest = latest_receipt_for_order(adapter.db, cid)
            if latest is None:
                affected_clients.append((cid, 'MISSING_PERSISTED_RECEIPT'))
                continue
            saved_trades = latest[1].get('trades') or []
            if filled_qty > ZERO and not saved_trades:
                affected_clients.append((cid, 'MISSING_PERSISTED_TRADES'))
                continue
            for saved_trade in saved_trades:
                saved_canon = canonical_trade(saved_trade)
                if saved_canon[1] >= query_from_id:
                    if saved_canon[1] not in fetched_by_id or fetched_by_id[saved_canon[1]] != saved_canon:
                        affected_clients.append((cid, 'CONFIRMED_TRADE_MISSING_OR_CHANGED'))
                        break

        # Deduplicate affected_clients preserving order.
        dedup_affected = []
        seen_affected = set()
        for cid, why in affected_clients:
            if cid not in seen_affected:
                seen_affected.add(cid)
                dedup_affected.append((cid, why))

        # Re-query affected orders authoritatively BEFORE advancing trade watermark.
        for cid, why in dedup_affected:
            before_record, before_quote = adapter._load(cid)
            with adapter.store.transaction():
                mark_order_unarchived(adapter.db, cid, why)
            try:
                adapter.query_order(cid)
            except (DataQualityError, ValueError, ExchangeError) as exc:
                adapter._uncertain(cid)
                _latch_audit_failure(adapter, 'CONFLICT', f'{cid}:{why}:{exc}')
                raise
            after_record, after_quote = adapter._load(cid)
            summary['orders_rechecked'] += 1
            if (after_record.snapshot.status != before_record.snapshot.status
                    or after_record.snapshot.cumulative_filled_quantity != before_record.snapshot.cumulative_filled_quantity
                    or after_record.snapshot.cumulative_fee != before_record.snapshot.cumulative_fee
                    or after_quote != before_quote):
                summary['late_discoveries'].append(dict(
                    client_order_id=cid, reason=why,
                    before_status=before_record.snapshot.status.value,
                    after_status=after_record.snapshot.status.value,
                    before_filled=str(before_record.snapshot.cumulative_filled_quantity),
                    after_filled=str(after_record.snapshot.cumulative_filled_quantity),
                    before_fee=str(before_record.snapshot.cumulative_fee),
                    after_fee=str(after_record.snapshot.cumulative_fee),
                ))

        if unmatched_new_trades:
            # First ensure no unresolved UNKNOWN order was waiting to receive that exchange order ID.
            buckets = classify_orders(adapter.db)
            if not buckets['unknown']:
                _latch_audit_failure(adapter, 'CONFLICT', 'unmanaged exchange trade detected')
                raise DataQualityError('unmanaged exchange trade detected during historical audit')

        # Compute next trade watermark (committed atomically ONLY at end of audit).
        next_last_id = prev_last_id
        next_last_time_ms = prev_last_time_ms
        if fetched_trades:
            max_fetched_id = max(t['id'] for t in fetched_trades)
            next_last_id = max_fetched_id if next_last_id is None else max(next_last_id, max_fetched_id)
            times = [t['time'] for t in fetched_trades if type(t.get('time')) is int]
            if times:
                max_time = max(times)
                next_last_time_ms = max_time if next_last_time_ms is None else max(next_last_time_ms, max_time)
            elif next_last_time_ms is None:
                next_last_time_ms = now_ms
        elif next_last_time_ms is None:
            next_last_time_ms = now_ms

        pending_watermarks[scope] = dict(
            last_trade_id=next_last_id,
            last_trade_time_ms=next_last_time_ms,
            last_scan_at=now_iso if not summary['clock_rollback_detected'] else watermark.get('last_scan_at'),
            overlap_ids=config.trade_overlap_ids,
            coverage_complete=True,
            coverage_note=None,
        )

    # Bounded O(batch_size) rolling archived-order verification via GET /fapi/v1/order.
    if batch_size > 0:
        candidates = adapter.db.execute('''SELECT
                client_order_id, exchange_order_id, confirmed_status,
                confirmed_filled_qty, confirmed_quote, confirmed_fee,
                exchange_updated_at, last_audit_at
            FROM futures_order_recovery
            WHERE archived=1 AND exchange_order_id IS NOT NULL
            ORDER BY COALESCE(last_audit_at, '') ASC, client_order_id ASC
            LIMIT ?''', (batch_size,)).fetchall()
        for row in candidates:
            cid = row[0]
            meta = dict(
                client_order_id=cid, exchange_order_id=row[1], confirmed_status=row[2],
                confirmed_filled_qty=row[3], confirmed_quote=row[4], confirmed_fee=row[5],
                exchange_updated_at=row[6], last_audit_at=row[7],
            )
            record, quote = adapter._load(cid)
            # First verify persisted record integrity against receipt before calling exchange.
            receipt_ok, _, _, _ = verify_persisted_receipt(adapter.db, cid, record, quote)
            if (not receipt_ok
                    or record.confirmed_status.value != meta['confirmed_status']
                    or record.snapshot.cumulative_filled_quantity != _decimal(meta['confirmed_filled_qty'])
                    or quote != _decimal(meta['confirmed_quote'])
                    or record.snapshot.cumulative_fee != _decimal(meta['confirmed_fee'])):
                adapter._uncertain(cid)
                _latch_audit_failure(adapter, 'CONFLICT', f'persisted recovery record mismatch for {cid}')
                raise DataQualityError(f'persisted order recovery record inconsistent for {cid}')

            try:
                adapter._validate_network_identity()
                raw_order = adapter.client.request(
                    'GET', '/fapi/v1/order',
                    dict(symbol=record.snapshot.intent.instrument_id, origClientOrderId=cid),
                )
            except ExchangeError as exc:
                if exc.code == -2013 and exc.category == 'ORDER_NOT_FOUND' and _is_exchange_retention_pruned(meta, now):
                    with adapter.store.transaction():
                        adapter.db.execute('''UPDATE futures_order_recovery
                            SET last_audit_at=?, audit_reason='EXCHANGE_RETENTION_PRUNED'
                            WHERE client_order_id=?''', (now_iso, cid))
                    summary['pruned_by_exchange_retention'].append(cid)
                    summary['rolling_orders_audited'] += 1
                    continue
                adapter._uncertain(cid)
                _latch_audit_failure(adapter, 'CONFLICT', f'rolling audit failed for {cid}: {exc}')
                raise
            except (DataQualityError, ValueError, KeyError, TypeError) as exc:
                adapter._uncertain(cid)
                _latch_audit_failure(adapter, 'CONFLICT', f'rolling audit failed for {cid}: {exc}')
                raise

            try:
                adapter._rest_order(raw_order, cid)
            except (DataQualityError, ValueError, KeyError, TypeError) as exc:
                adapter._uncertain(cid)
                _latch_audit_failure(adapter, 'CONFLICT', f'rolling audit conflict for {cid}: {exc}')
                raise

            after_record, after_quote = adapter._load(cid)
            summary['rolling_orders_audited'] += 1
            summary['orders_rechecked'] += 1
            if (after_record.snapshot.status != record.snapshot.status
                    or after_record.snapshot.cumulative_filled_quantity != record.snapshot.cumulative_filled_quantity
                    or after_record.snapshot.cumulative_fee != record.snapshot.cumulative_fee
                    or after_quote != quote):
                summary['late_discoveries'].append(dict(
                    client_order_id=cid, reason='ROLLING_ARCHIVE_AUDIT',
                    before_status=record.snapshot.status.value,
                    after_status=after_record.snapshot.status.value,
                    before_filled=str(record.snapshot.cumulative_filled_quantity),
                    after_filled=str(after_record.snapshot.cumulative_filled_quantity),
                    before_fee=str(record.snapshot.cumulative_fee),
                    after_fee=str(after_record.snapshot.cumulative_fee),
                ))

    if include_income:
        funding = adapter.sync_funding()
        summary['income_sync'] = dict(
            covered_from_ms=funding.covered_from_ms,
            covered_through_ms=funding.covered_through_ms,
            complete=funding.complete,
            pages=funding.pages,
            newly_recorded=len(funding.newly_recorded),
        )

    # Commit watermarks & global audit state atomically AFTER all economic events succeeded.
    buckets = classify_orders(adapter.db)
    with adapter.store.transaction():
        for scope, payload in pending_watermarks.items():
            save_state(adapter.db, scope, payload, now_iso)
        global_state = ensure_global_state(adapter.db, now_iso)
        global_state.update(
            has_unknown=bool(buckets['unknown']),
            unknown_count=len(buckets['unknown']),
            active_count=len(buckets['active']),
            incomplete_count=len(buckets['economic_incomplete']),
            archived_count=len(buckets['archived']),
            rounds_since_audit=0,
            last_history_audit_at=now_iso,
            history_audit_status='OK',
            history_audit_error=None,
            audit_round=int(global_state.get('audit_round', 0)) + 1,
            pruned_by_exchange_retention=int(global_state.get('pruned_by_exchange_retention', 0))
            + len(summary['pruned_by_exchange_retention']),
        )
        save_state(adapter.db, 'global', global_state, now_iso)

    summary['ended_at'] = adapter.clock().isoformat()
    adapter.last_audit_summary = summary
    return summary


def recovery_status(db):
    buckets = classify_orders(db)
    global_state = load_state(db, 'global') or default_global_state()
    watermarks = {}
    for scope, payload in db.execute('SELECT scope, payload FROM futures_recovery_state ORDER BY scope').fetchall():
        if scope.startswith('trades:'):
            watermarks[scope] = json.loads(payload)
    return dict(
        counts=dict(
            unknown=len(buckets['unknown']),
            active=len(buckets['active']),
            economic_incomplete=len(buckets['economic_incomplete']),
            archived_terminal=len(buckets['archived']),
            archived_exchange=len(buckets['archived_exchange']),
            local_rejected=len(buckets['local_rejected']),
        ),
        has_unknown=bool(buckets['unknown']),
        history_audit_status=global_state.get('history_audit_status'),
        history_audit_error=global_state.get('history_audit_error'),
        last_realtime_reconcile_at=global_state.get('last_realtime_reconcile_at'),
        last_history_audit_at=global_state.get('last_history_audit_at'),
        realtime_rounds=global_state.get('realtime_rounds', 0),
        rounds_since_audit=global_state.get('rounds_since_audit', 0),
        audit_round=global_state.get('audit_round', 0),
        clock_rollback_count=global_state.get('clock_rollback_count', 0),
        pruned_by_exchange_retention=global_state.get('pruned_by_exchange_retention', 0),
        watermarks=watermarks,
    )
