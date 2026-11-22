"""Persistent offline execution adapter implementing the shared one-way contract."""
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, wait
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json

from factorlab.errors import DataQualityError
from factorlab.integrations.binance.market import _decimal, symbol_name
from factorlab.trading import FundingEvent, OrderEvent, OrderSnapshot, OrderStatus as S
from factorlab.trading.contracts import TERMINAL, funding_event_id, funding_identity_key, utc
from factorlab.trading.state import OrderRecord, advance, unknown
from factorlab.trading.storage import dumps, decode

from .account import AccountFacts, instant
from .protocol import (
    Environment, ExchangeError, OutcomeUnknown, SafetyBlocked, OfflineTransport,
    AccountReadExpired, AccountReadWindow, ReadOnlyNetworkTransport, credential_account_id,
)
from .rules import order_parameters

ACCOUNT_READ_SECONDS = 5
ACCOUNT_PATH = '/fapi/v3/account'
ACCOUNT_PATHS = (ACCOUNT_PATH, '/fapi/v3/positionRisk', '/fapi/v1/positionSide/dual',
                 '/fapi/v1/openOrders', '/fapi/v1/symbolConfig')
ACCOUNT_PERMISSION_PATH = '/fapi/v2/account'
QUANTITATIVE_PATH = '/fapi/v1/apiTradingStatus'

INCOME_PAGE_LIMIT = 1000
INCOME_MAX_WINDOW_MS = 7 * 24 * 60 * 60 * 1000
INCOME_OVERLAP_MS = 60 * 60 * 1000
INCOME_MAX_PAGES_PER_WINDOW = 50
INCOME_MAX_WINDOWS = 100
SUPPORTED_FUNDING_ASSETS = frozenset({'USDT'})
INCOME_BUSINESS_FIELDS = ('symbol', 'incomeType', 'income', 'asset', 'time', 'tranId', 'tradeId', 'info')


ZERO = Decimal('0')
STATUS = {'NEW': S.SUBMITTED, 'PARTIALLY_FILLED': S.PARTIALLY_FILLED, 'FILLED': S.FILLED,
          'CANCELED': S.CANCELED, 'REJECTED': S.REJECTED, 'EXPIRED': S.CANCELED,
          'EXPIRED_IN_MATCH': S.CANCELED}


@dataclass(frozen=True)
class IncomeWindowResult:
    """Verified, bounded and paginated /fapi/v1/income window read."""
    start_ms: int
    end_ms: int
    rows: tuple[dict, ...]
    windows: int
    pages: int
    symbol: str | None = None
    income_type: str | None = None
    complete: bool = True


@dataclass(frozen=True)
class FundingSyncResult:
    """Authoritative funding synchronization result with persisted coverage."""
    start_ms: int
    end_ms: int
    covered_from_ms: int
    covered_through_ms: int
    events: tuple[FundingEvent, ...]
    newly_recorded: tuple[FundingEvent, ...]
    windows: int
    pages: int
    complete: bool = True


def _to_ms(value):
    if isinstance(value, datetime):
        utc(value)
        milliseconds = int(round(value.timestamp() * 1000))
        if milliseconds < 0:
            raise DataQualityError('invalid income timestamp bound')
        return milliseconds
    if type(value) is int and value >= 0:
        return value
    raise DataQualityError('invalid income timestamp bound')


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


def _validate_income_row(row):
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


def _funding_event_from_row(account_id, row):
    time_ms, income_type, amount, asset, symbol, tran_id, trade_id, sanitized = _validate_income_row(row)
    if income_type != 'FUNDING_FEE':
        raise DataQualityError(f'unexpected non-funding incomeType: {income_type}')
    if asset not in SUPPORTED_FUNDING_ASSETS:
        raise DataQualityError(f'unsupported funding asset: {asset}')
    metadata = {'endpoint': '/fapi/v1/income'}
    if trade_id:
        metadata['trade_id'] = trade_id
    info = sanitized.get('info')
    if isinstance(info, str) and info:
        metadata['info'] = info[:128]
    event = FundingEvent(
        account_id=account_id,
        tran_id=tran_id,
        symbol=symbol,
        asset=asset,
        income_type=income_type,
        amount=amount,
        event_time=instant(time_ms),
        source='binance-rest-income',
        event_id=funding_event_id(income_type, tran_id),
        metadata=metadata,
    )
    return event, time_ms, sanitized


class FuturesAdapter:
    """Use a dedicated PaperStore for protocol state, separate from paper accounting.

    GET reads may be explicitly authorized. Submit/cancel never use real network.
    Restart/disconnect require full REST account/order reconciliation.
    """
    def __init__(self, client, store, *, account_id='offline', clock=lambda: datetime.now(timezone.utc)):
        if client.environment is not None:
            if account_id != client.environment.account_id:
                raise SafetyBlocked('explicit environment account identity required')
            account_id = (f'{client.environment.name}:{account_id}' if client.credentials is None
                          else credential_account_id(client.environment, client.credentials.key))
        self.client, self.store, self.account_id, self.clock = client, store, account_id, clock
        self.get_confirmed_writes = False
        self.facts = None
        self.quantitative_observation = None
        self._read_v2_permission_early = False
        self._funding_synced = False
        self._last_funding_sync_ms = None
        self._funding_coverage_cache = None
        self.db = store.db
        self.db.execute('CREATE TABLE IF NOT EXISTS futures_orders (id TEXT PRIMARY KEY, payload TEXT NOT NULL, quote TEXT NOT NULL)')
        self.db.execute('CREATE TABLE IF NOT EXISTS futures_events (id TEXT PRIMARY KEY, payload TEXT NOT NULL)')
        self.db.execute('CREATE TABLE IF NOT EXISTS futures_rejections (id TEXT PRIMARY KEY, payload TEXT NOT NULL)')
        self.db.execute('''CREATE TABLE IF NOT EXISTS futures_receipts (
            id TEXT PRIMARY KEY, client_id TEXT NOT NULL, observed_at TEXT NOT NULL, payload TEXT NOT NULL
        )''')
        self.db.execute('''CREATE TABLE IF NOT EXISTS futures_funding (
            id TEXT PRIMARY KEY, tran_id TEXT NOT NULL, event_time_ms INTEGER NOT NULL,
            payload TEXT NOT NULL, raw_payload TEXT NOT NULL, observed_at TEXT NOT NULL
        )''')
        self.db.execute('''CREATE TABLE IF NOT EXISTS futures_income_coverage (
            scope TEXT PRIMARY KEY, covered_from_ms INTEGER NOT NULL, covered_through_ms INTEGER NOT NULL,
            complete INTEGER NOT NULL, updated_at TEXT NOT NULL, detail TEXT NOT NULL
        )''')
        self.db.execute('CREATE TABLE IF NOT EXISTS futures_meta (id INTEGER PRIMARY KEY CHECK(id=1), account TEXT NOT NULL, reconciled INTEGER NOT NULL)')
        with store.transaction():
            row = self.db.execute('SELECT account FROM futures_meta WHERE id=1').fetchone()
            if row is not None and row[0] != account_id:
                raise ValueError('persisted adapter account mismatch')
            self.db.execute('INSERT INTO futures_meta VALUES(1,?,0) ON CONFLICT(id) DO UPDATE SET reconciled=0', (account_id,))
            for key, raw in self.db.execute('SELECT id,payload FROM futures_orders').fetchall():
                record = decode(json.loads(raw))
                if record.snapshot.status not in TERMINAL:
                    self._save(unknown(record), self._load(key)[1])

    @property
    def reconciled(self):
        return bool(self.db.execute('SELECT reconciled FROM futures_meta WHERE id=1').fetchone()[0])

    def _load(self, order_id):
        row = self.db.execute('SELECT payload,quote FROM futures_orders WHERE id=?', (order_id,)).fetchone()
        if row is None:
            raise KeyError(order_id)
        return decode(json.loads(row[0])), Decimal(row[1])

    def _save(self, record, quote):
        self.db.execute('INSERT INTO futures_orders VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload,quote=excluded.quote',
                        (record.snapshot.intent.client_order_id, dumps(record), str(quote)))

    def order(self, order_id):
        return self._load(order_id)[0].snapshot

    def disconnect(self):
        with self.store.transaction():
            self.db.execute('UPDATE futures_meta SET reconciled=0')
            for key, raw in self.db.execute('SELECT id,payload FROM futures_orders').fetchall():
                record = decode(json.loads(raw))
                if record.snapshot.status not in TERMINAL:
                    self._save(unknown(record), self._load(key)[1])

    def _uncertain(self, order_id):
        with self.store.transaction():
            record, quote = self._load(order_id)
            self._save(unknown(record), quote)
            self.db.execute('UPDATE futures_meta SET reconciled=0')

    def submit_order(self, intent):
        from .network import DemoWriteRefused
        if not isinstance(self.client.transport, OfflineTransport):
            from .network import demo_network_permitted
            if not demo_network_permitted(self.client) or not self.client.transport._admission:
                raise SafetyBlocked('NO LIVE MONEY: submit requires offline or admitted Demo transport')
        params = order_parameters(intent)
        with self.store.transaction():
            row = self.db.execute('SELECT id FROM futures_orders WHERE id=?', (intent.client_order_id,)).fetchone()
            if row is not None:
                record, _ = self._load(intent.client_order_id)
                if record.snapshot.intent != intent:
                    raise ValueError('clientOrderId identity conflict')
                if record.snapshot.reconcile_required:
                    raise OutcomeUnknown('RECONCILE_REQUIRED: duplicate submission forbidden')
                # Query is safe; never POST the same client ID again.
                existing = True
            else:
                unresolved = any(decode(json.loads(r[0])).snapshot.reconcile_required
                                 for r in self.db.execute('SELECT payload FROM futures_orders'))
                if not self.reconciled or unresolved:
                    raise SafetyBlocked('RECONCILE_REQUIRED')
                if (self.store.max_orders is not None and
                        self.db.execute('SELECT count(*) FROM futures_orders').fetchone()[0] >= self.store.max_orders):
                    raise ValueError('adapter order capacity exhausted')
                # No confirmed exchange timestamp exists before dispatch.
                record = OrderRecord(OrderSnapshot(intent), instant(0), intent.limit_price or ZERO)
                self._save(unknown(record), ZERO)
                existing = False
        if existing:
            return self.query_order(intent.client_order_id)
        try:
            row = self.client.request('POST', '/fapi/v1/order', params)
            return self._confirm_write(row, intent.client_order_id)
        except DemoWriteRefused:
            # Only this call's placeholder exists, and the transport never attempted HTTP.
            with self.store.transaction():
                self.db.execute('DELETE FROM futures_orders WHERE id=?', (intent.client_order_id,))
            raise
        except ExchangeError as exc:
            if exc.category not in {'RATE_LIMIT', 'ORDER_NOT_FOUND'} and exc.status < 500:
                if self.get_confirmed_writes:
                    try:
                        return self.query_order(intent.client_order_id)
                    except ExchangeError as readback:
                        if readback.code != -2013 or readback.category != 'ORDER_NOT_FOUND':
                            self._uncertain(intent.client_order_id)
                            raise OutcomeUnknown('rejection requires GET confirmation') from None
                event = OrderEvent(intent.client_order_id, None, S.REJECTED, ZERO, None,
                                   self.clock(), 'binance-rest', 'reject:' + intent.client_order_id, ZERO)
                diagnostic = dict(client_order_id=intent.client_order_id, event_id=event.event_id,
                                  http_status=exc.status, code=exc.code, message=exc.message,
                                  category=exc.category)
                return self._apply(event, ZERO, rejection=diagnostic)
            self._uncertain(intent.client_order_id)
            raise OutcomeUnknown('order admission uncertain; reconcile') from None
        except (OutcomeUnknown, DataQualityError, ValueError, KeyError, TypeError):
            self._uncertain(intent.client_order_id)
            raise OutcomeUnknown('order response unconfirmed; reconcile without resubmitting') from None

    def cancel_order(self, client_order_id):
        if not isinstance(self.client.transport, OfflineTransport):
            from .network import demo_network_permitted
            if not demo_network_permitted(self.client) or not self.client.transport._admission:
                raise SafetyBlocked('NO LIVE MONEY: cancel requires offline or admitted Demo transport')
        order = self.order(client_order_id)
        if order.reconcile_required:
            raise OutcomeUnknown('RECONCILE_REQUIRED')
        if order.status in TERMINAL:
            return self.query_order(client_order_id)
        try:
            row = self.client.request('DELETE', '/fapi/v1/order',
                                      dict(symbol=order.intent.instrument_id, origClientOrderId=client_order_id))
            return self._confirm_write(row, client_order_id)
        except (OutcomeUnknown, DataQualityError, ValueError, KeyError, TypeError):
            self._uncertain(client_order_id)
            raise OutcomeUnknown('cancel unconfirmed; reconcile fills before releasing risk') from None

    def _confirm_write(self, row, client_order_id):
        if self.get_confirmed_writes:
            try:
                return self.query_order(client_order_id)
            except ExchangeError:
                raise OutcomeUnknown('order readback unconfirmed; reconcile without resubmitting') from None
        # Binance write acknowledgments can omit cumulative quote. Query Order
        # supplies it for the journaled identity, including after a partial fill.
        if isinstance(row, dict) and 'cumQuote' not in row:
            try:
                return self.query_order(client_order_id)
            except ExchangeError:
                raise OutcomeUnknown('order readback unconfirmed; reconcile without resubmitting') from None
        return self._rest_order(row, client_order_id, source='WRITE_ACK')

    def _validate_network_identity(self):
        from .network import DemoNetworkTransport
        if (type(self.client.transport) is DemoNetworkTransport
                or isinstance(self.client.transport, ReadOnlyNetworkTransport) and self.client.environment is Environment.DEMO):
            credentials = self.client.credentials
            expected = (credential_account_id(Environment.DEMO, credentials.key)
                        if credentials is not None and credentials.environment is Environment.DEMO else None)
            if self.client.environment is not Environment.DEMO or self.account_id != expected:
                raise SafetyBlocked('Demo account identity changed; recovery blocked')

    def query_order(self, client_order_id):
        self._validate_network_identity()
        order = self.order(client_order_id)
        try:
            row = self.client.request('GET', '/fapi/v1/order',
                                      dict(symbol=order.intent.instrument_id, origClientOrderId=client_order_id))
            return self._rest_order(row, client_order_id)
        except (DataQualityError, ValueError, KeyError, TypeError):
            self._uncertain(client_order_id)
            raise

    def _rest_order(self, row, expected_id, *, source='GET'):
        record, previous_quote = self._load(expected_id)
        intent = record.snapshot.intent
        if (not isinstance(row, dict) or row['clientOrderId'] != expected_id or row['symbol'] != intent.instrument_id
                or row['side'] != intent.side.value or row['positionSide'] != 'BOTH'
                or row['type'] != intent.order_type.value or _decimal(row['origQty']) != intent.quantity):
            raise DataQualityError('order response identity mismatch')
        filled, quote = _decimal(row['executedQty']), _decimal(row['cumQuote'])
        delta, delta_quote = filled - record.snapshot.cumulative_filled_quantity, quote - previous_quote
        if delta < 0 or delta_quote < 0 or (delta == 0 and delta_quote != 0):
            raise DataQualityError('cumulative execution regression')
        fee = record.snapshot.cumulative_fee
        matching = []
        if filled:
            trades = self.user_trades(intent.instrument_id, order_id=row['orderId'])
            matching = [t for t in trades if str(t['orderId']) == str(row['orderId'])]
            if any(t['commissionAsset'] != 'USDT' for t in matching):
                raise DataQualityError('non-USDT commission cannot project shared cumulative fee')
            if sum((_decimal(t['qty']) for t in matching), ZERO) != filled:
                raise DataQualityError('incomplete account trades: fee UNKNOWN, reconcile required')
            if sum((_decimal(t['quoteQty']) for t in matching), ZERO) != quote:
                raise DataQualityError('trade notional does not match order cumulative quote')
            fee = sum((_decimal(t['commission']) for t in matching), ZERO)
        event = OrderEvent(expected_id, str(row['orderId']), STATUS[row['status']], filled,
                           delta_quote / delta if delta else None, instant(row['updateTime']),
                           'binance-rest', 'rest:' + hashlib.sha256(json.dumps(row, sort_keys=True).encode()).hexdigest(), fee)
        payload = json.dumps(dict(source=source, order=row, trades=matching), sort_keys=True)
        receipt_id = hashlib.sha256(payload.encode()).hexdigest()
        return self._apply(event, quote, receipt=(receipt_id, expected_id, self.clock().isoformat(), payload))

    def _apply(self, event, quote, *, rejection=None, receipt=None):
        with self.store.transaction():
            if receipt is not None:
                self.db.execute('''INSERT INTO futures_receipts VALUES (?,?,?,?)
                    ON CONFLICT(id) DO UPDATE SET observed_at=excluded.observed_at''', receipt)
            if rejection is not None:
                self.db.execute('INSERT INTO futures_rejections VALUES(?,?)',
                                (event.client_order_id, json.dumps(rejection)))
            payload = dumps(event)
            duplicate = self.db.execute('SELECT payload FROM futures_events WHERE id=?', (event.event_id,)).fetchone()
            if duplicate is not None:
                # Same raw REST query maps to a zero delta on repetition. The
                # already stored event remains authoritative for idempotence.
                previous = decode(json.loads(duplicate[0]))
                if (previous.client_order_id, previous.status, previous.cumulative_filled_quantity, previous.cumulative_fee) != (
                    event.client_order_id, event.status, event.cumulative_filled_quantity, event.cumulative_fee):
                    raise DataQualityError('event identity conflict')
                record, _ = self._load(event.client_order_id)
                if (record.snapshot.reconcile_required and previous.status == record.confirmed_status
                        and previous.cumulative_filled_quantity == record.snapshot.cumulative_filled_quantity
                        and previous.cumulative_fee == record.snapshot.cumulative_fee):
                    self._save(advance(record, previous), quote)
                return previous
            if (self.store.max_events is not None and
                    self.db.execute('SELECT count(*) FROM futures_events').fetchone()[0] >= self.store.max_events):
                raise ValueError('adapter event capacity exhausted')
            record, _ = self._load(event.client_order_id)
            if record.confirmed_status == S.NEW and event.status not in {S.SUBMITTED, S.REJECTED}:
                # A confirmed fill/cancel necessarily confirms acceptance first;
                # RESULT market responses can arrive already FILLED.
                accepted = replace(event, status=S.SUBMITTED, cumulative_filled_quantity=ZERO,
                                   cumulative_fee=ZERO, fill_price=None)
                record = advance(record, accepted)
            updated = advance(record, event)
            self._save(updated, quote)
            self.db.execute('INSERT INTO futures_events VALUES(?,?)', (event.event_id, payload))
        return event

    def open_orders(self, symbol=None):
        return self.client.request('GET', '/fapi/v1/openOrders', {} if symbol is None else {'symbol': symbol_name(symbol)})

    def all_orders(self, symbol, *, limit=100):
        return self.client.request('GET', '/fapi/v1/allOrders', {'symbol': symbol_name(symbol), 'limit': limit})

    def user_trades(self, symbol, *, order_id=None):
        params = {'symbol': symbol_name(symbol), 'limit': 1000}
        if order_id is not None:
            params['orderId'] = order_id
        rows = self.client.request('GET', '/fapi/v1/userTrades', params)
        if not isinstance(rows, list) or len(rows) >= 1000:
            raise DataQualityError('trade history incomplete; explicit pagination required')
        keys = [(t['symbol'], t['id']) for t in rows]
        if len(keys) != len(set(keys)):
            raise DataQualityError('duplicate actual trades')
        return tuple(rows)

    def income(self, symbol=None, *, start_time=None, end_time=None, income_type=None, limit=INCOME_PAGE_LIMIT):
        """Raw single-page /fapi/v1/income read; unwindowed reads never prove history coverage."""
        if type(limit) is not int or not 1 <= limit <= INCOME_PAGE_LIMIT:
            raise DataQualityError('invalid income page limit')
        params = {'limit': limit}
        if symbol is not None:
            params['symbol'] = symbol_name(symbol)
        if income_type is not None:
            if not isinstance(income_type, str) or not income_type.strip():
                raise DataQualityError('invalid incomeType filter')
            params['incomeType'] = income_type
        if start_time is not None:
            params['startTime'] = _to_ms(start_time)
        if end_time is not None:
            params['endTime'] = _to_ms(end_time)
        rows = self.client.request('GET', '/fapi/v1/income', params)
        if not isinstance(rows, list) or len(rows) > limit:
            raise DataQualityError('income window incomplete; explicit pagination required')
        if start_time is None and end_time is None and len(rows) >= limit:
            raise DataQualityError('income window incomplete; explicit pagination required')
        return tuple(rows)

    def income_window(self, *, start_time, end_time, symbol=None, income_type=None,
                      limit=INCOME_PAGE_LIMIT, max_window_ms=INCOME_MAX_WINDOW_MS,
                      max_pages=INCOME_MAX_PAGES_PER_WINDOW):
        """Read a bounded, chunked, paginated /fapi/v1/income interval with explicit coverage checks."""
        self._validate_network_identity()
        if start_time is None or end_time is None:
            raise DataQualityError('explicit start_time and end_time required for income coverage')
        start_ms, end_ms = _to_ms(start_time), _to_ms(end_time)
        if start_ms > end_ms:
            raise DataQualityError('income start_time exceeds end_time')
        if type(limit) is not int or not 1 <= limit <= INCOME_PAGE_LIMIT:
            raise DataQualityError('invalid income page limit')
        if type(max_window_ms) is not int or max_window_ms < 1:
            raise DataQualityError('invalid income max_window_ms')
        if type(max_pages) is not int or max_pages < 1:
            raise DataQualityError('invalid income max_pages')
        expected_symbol = None if symbol is None else symbol_name(symbol)
        if income_type is not None and (not isinstance(income_type, str) or not income_type.strip()):
            raise DataQualityError('invalid incomeType filter')

        chunks = []
        cursor = start_ms
        while cursor <= end_ms:
            chunk_end = min(end_ms, cursor + max_window_ms - 1)
            chunks.append((cursor, chunk_end))
            if len(chunks) > INCOME_MAX_WINDOWS:
                raise DataQualityError('income history span exceeds bounded window budget')
            cursor = chunk_end + 1

        collected = []
        seen_funding = {}
        pages = 0
        for chunk_start, chunk_end in chunks:
            page_cursor = chunk_start
            pages_in_chunk = 0
            prev_boundary_signatures = None
            prev_boundary_non_funding = None
            while True:
                pages_in_chunk += 1
                if pages_in_chunk > max_pages:
                    raise DataQualityError('income pagination incomplete: max pages exceeded')
                pages += 1
                params = {'startTime': page_cursor, 'endTime': chunk_end, 'limit': limit}
                if expected_symbol is not None:
                    params['symbol'] = expected_symbol
                if income_type is not None:
                    params['incomeType'] = income_type
                rows = self.client.request('GET', '/fapi/v1/income', params)
                if not isinstance(rows, list):
                    raise DataQualityError('invalid income response shape')
                if len(rows) > limit:
                    raise DataQualityError('income response exceeded requested page limit')

                validated = []
                for row in rows:
                    item = _validate_income_row(row)
                    time_ms, row_type, amount, asset, row_symbol, tran_id, trade_id, sanitized = item
                    if income_type is not None and row_type != income_type:
                        raise DataQualityError('unexpected incomeType in filtered income response')
                    if expected_symbol is not None and row_symbol != expected_symbol:
                        raise DataQualityError('unexpected symbol in filtered income response')
                    if not page_cursor <= time_ms <= chunk_end:
                        raise DataQualityError('income row timestamp outside requested window')
                    signature = (row_symbol, asset, row_type, amount, time_ms, trade_id)
                    validated.append((time_ms, row_type, tran_id, signature, sanitized))

                if prev_boundary_signatures is not None:
                    current_at_cursor = {}
                    for time_ms, row_type, tran_id, signature, _ in validated:
                        if time_ms != page_cursor:
                            continue
                        key = (row_type, tran_id)
                        if key in current_at_cursor and current_at_cursor[key] != signature:
                            raise DataQualityError('conflicting income row for same tranId')
                        current_at_cursor[key] = signature
                    for key, expected_sig in prev_boundary_signatures.items():
                        if key not in current_at_cursor:
                            raise DataQualityError('income pagination incomplete: boundary rows missing between pages')
                        if current_at_cursor[key] != expected_sig:
                            raise DataQualityError('conflicting income row for same tranId')

                skip_non_funding = Counter(prev_boundary_non_funding) if prev_boundary_non_funding is not None else Counter()
                for time_ms, row_type, tran_id, signature, sanitized in validated:
                    if row_type == 'FUNDING_FEE':
                        existing = seen_funding.get(tran_id)
                        if existing is not None:
                            if existing[0] != signature:
                                raise DataQualityError(f'funding event identity conflict for tranId={tran_id}')
                            continue
                        seen_funding[tran_id] = (signature, sanitized)
                        collected.append((time_ms, tran_id, row_type, sanitized))
                    else:
                        non_funding_key = (row_type, tran_id, signature)
                        if time_ms == page_cursor and skip_non_funding[non_funding_key] > 0:
                            skip_non_funding[non_funding_key] -= 1
                            continue
                        collected.append((time_ms, tran_id, row_type, sanitized))

                if len(rows) < limit:
                    break
                last_time = max(item[0] for item in validated)
                if last_time == page_cursor:
                    raise DataQualityError('income pagination incomplete: page saturated at single timestamp')
                prev_boundary_signatures = {}
                prev_boundary_non_funding = Counter()
                for time_ms, row_type, tran_id, signature, _ in validated:
                    if time_ms != last_time:
                        continue
                    key = (row_type, tran_id)
                    if key in prev_boundary_signatures and prev_boundary_signatures[key] != signature:
                        raise DataQualityError('conflicting income row for same tranId')
                    prev_boundary_signatures[key] = signature
                    if row_type != 'FUNDING_FEE':
                        prev_boundary_non_funding[(row_type, tran_id, signature)] += 1
                page_cursor = last_time

        collected.sort(key=lambda item: (item[0], item[1], item[2]))
        return IncomeWindowResult(
            start_ms=start_ms,
            end_ms=end_ms,
            rows=tuple(item[3] for item in collected),
            windows=len(chunks),
            pages=pages,
            symbol=expected_symbol,
            income_type=income_type,
            complete=True,
        )

    def funding_events(self):
        """Return persisted confirmed funding events ordered by exchange time and tranId."""
        rows = self.db.execute(
            'SELECT payload FROM futures_funding ORDER BY event_time_ms, tran_id'
        ).fetchall()
        return tuple(decode(json.loads(raw)) for (raw,) in rows)

    def funding_coverage(self, scope='FUNDING_FEE'):
        row = self.db.execute(
            'SELECT covered_from_ms, covered_through_ms, complete, updated_at, detail '
            'FROM futures_income_coverage WHERE scope=?',
            (scope,),
        ).fetchone()
        if row is None:
            self._funding_coverage_cache = None
            return None
        persisted = dict(
            scope=scope,
            covered_from_ms=row[0],
            covered_through_ms=row[1],
            complete=bool(row[2]),
            updated_at=row[3],
            detail=json.loads(row[4]),
        )
        cache = self._funding_coverage_cache
        if (cache is not None and cache.get('scope') == scope and persisted['complete']
                and cache['covered_from_ms'] == persisted['covered_from_ms']
                and cache['covered_through_ms'] >= persisted['covered_through_ms']):
            return dict(cache)
        return persisted

    def _earliest_activity_ms(self):
        candidates = []
        for (raw,) in self.db.execute('SELECT payload FROM futures_events'):
            event = decode(json.loads(raw))
            if isinstance(event, OrderEvent):
                ms = _to_ms(event.event_time)
                if ms > 0:
                    candidates.append(ms)
        for (raw,) in self.db.execute('SELECT payload FROM futures_orders'):
            record = decode(json.loads(raw))
            ms = _to_ms(record.observed_at)
            if ms > 0:
                candidates.append(ms)
        for (raw,) in self.db.execute('SELECT payload FROM futures_receipts'):
            receipt = json.loads(raw)
            order = receipt.get('order', {})
            for field in ('time', 'updateTime'):
                value = order.get(field)
                if type(value) is int and value > 0:
                    candidates.append(value)
            for trade in receipt.get('trades', ()):
                value = trade.get('time')
                if type(value) is int and value > 0:
                    candidates.append(value)
        row = self.db.execute('SELECT MIN(event_time_ms) FROM futures_funding').fetchone()
        if row is not None and type(row[0]) is int and row[0] > 0:
            candidates.append(row[0])
        return min(candidates) if candidates else None

    def sync_funding(self, *, start_time=None, end_time=None, overlap_ms=INCOME_OVERLAP_MS,
                     page_limit=INCOME_PAGE_LIMIT, max_window_ms=INCOME_MAX_WINDOW_MS,
                     max_pages=INCOME_MAX_PAGES_PER_WINDOW):
        """Read, validate, and durably persist confirmed FUNDING_FEE settlements via GET only."""
        try:
            return self._sync_funding(
                start_time=start_time,
                end_time=end_time,
                overlap_ms=overlap_ms,
                page_limit=page_limit,
                max_window_ms=max_window_ms,
                max_pages=max_pages,
            )
        except (DataQualityError, ValueError, KeyError, TypeError, SafetyBlocked):
            self._funding_synced = False
            self._last_funding_sync_ms = None
            self._funding_coverage_cache = None
            self.disconnect()
            raise

    def _sync_funding(self, *, start_time, end_time, overlap_ms, page_limit, max_window_ms, max_pages):
        self._validate_network_identity()
        if type(overlap_ms) is not int or overlap_ms < 0:
            raise DataQualityError('invalid income overlap_ms')
        end_ms = _to_ms(self.clock()) if end_time is None else _to_ms(end_time)
        explicit_start_ms = None if start_time is None else _to_ms(start_time)
        if explicit_start_ms is not None and explicit_start_ms > end_ms:
            raise DataQualityError('funding sync start_time exceeds end_time')
        earliest_ms = self._earliest_activity_ms()
        coverage = self.funding_coverage()

        candidates = [value for value in (explicit_start_ms, earliest_ms) if value is not None]
        if coverage is not None and coverage['complete']:
            candidates.append(coverage['covered_from_ms'])
            target_origin_ms = min(candidates)
        else:
            target_origin_ms = min(candidates) if candidates else max(0, end_ms - max_window_ms)
        if target_origin_ms > end_ms:
            target_origin_ms = end_ms

        ranges = []
        if coverage is None or not coverage['complete']:
            ranges.append((target_origin_ms, end_ms))
            new_from_ms, new_through_ms = target_origin_ms, end_ms
        else:
            cov_from, cov_through = coverage['covered_from_ms'], coverage['covered_through_ms']
            inc_end = max(end_ms, cov_through)
            inc_start = max(target_origin_ms, cov_through - overlap_ms)
            if inc_start > inc_end:
                inc_start = inc_end
            if target_origin_ms < cov_from:
                if inc_start <= cov_from:
                    ranges.append((target_origin_ms, inc_end))
                else:
                    ranges.append((target_origin_ms, cov_from))
                    ranges.append((inc_start, inc_end))
            else:
                ranges.append((inc_start, inc_end))
            new_from_ms = min(target_origin_ms, cov_from)
            new_through_ms = max(end_ms, cov_through)

        parsed_by_id = {}
        total_windows = 0
        total_pages = 0
        for r_start, r_end in ranges:
            window = self.income_window(
                start_time=r_start,
                end_time=r_end,
                symbol=None,
                income_type='FUNDING_FEE',
                limit=page_limit,
                max_window_ms=max_window_ms,
                max_pages=max_pages,
            )
            total_windows += window.windows
            total_pages += window.pages
            for row in window.rows:
                event, time_ms, sanitized = _funding_event_from_row(self.account_id, row)
                existing = parsed_by_id.get(event.event_id)
                if existing is not None:
                    if funding_identity_key(existing[0]) != funding_identity_key(event):
                        raise DataQualityError(f'funding event identity conflict for tranId={event.tran_id}')
                    continue
                parsed_by_id[event.event_id] = (event, time_ms, sanitized)

        ordered = sorted(parsed_by_id.values(), key=lambda item: (item[1], item[0].tran_id))
        observed_at = self.clock().isoformat()
        existing_rows = {
            row_id: decode(json.loads(raw))
            for row_id, raw in self.db.execute('SELECT id, payload FROM futures_funding').fetchall()
        }
        newly_recorded = []
        to_insert = []
        for event, time_ms, sanitized in ordered:
            previous = existing_rows.get(event.event_id)
            if previous is not None:
                if (not isinstance(previous, FundingEvent)
                        or funding_identity_key(previous) != funding_identity_key(event)):
                    raise DataQualityError(f'funding event identity conflict for tranId={event.tran_id}')
                continue
            to_insert.append((event, time_ms, sanitized))
            newly_recorded.append(event)

        detail = dict(
            ranges=[[r_start, r_end] for r_start, r_end in ranges],
            windows=total_windows,
            pages=total_pages,
            events_seen=len(ordered),
            newly_recorded=len(newly_recorded),
            overlap_ms=overlap_ms,
            max_window_ms=max_window_ms,
        )
        need_db_write = (
            bool(to_insert)
            or coverage is None
            or not coverage['complete']
            or new_from_ms < coverage['covered_from_ms']
            or new_through_ms > coverage['covered_through_ms']
        )
        if need_db_write:
            with self.store.transaction():
                for event, time_ms, sanitized in to_insert:
                    if (self.store.max_events is not None
                            and self.db.execute('SELECT count(*) FROM futures_funding').fetchone()[0] >= self.store.max_events):
                        raise ValueError('adapter funding event capacity exhausted')
                    self.db.execute(
                        'INSERT INTO futures_funding VALUES(?,?,?,?,?,?)',
                        (event.event_id, event.tran_id, time_ms, dumps(event),
                         json.dumps(sanitized, sort_keys=True), observed_at),
                    )
                self.db.execute(
                    '''INSERT INTO futures_income_coverage VALUES(?,?,?,?,?,?)
                       ON CONFLICT(scope) DO UPDATE SET
                       covered_from_ms=excluded.covered_from_ms,
                       covered_through_ms=excluded.covered_through_ms,
                       complete=excluded.complete,
                       updated_at=excluded.updated_at,
                       detail=excluded.detail''',
                    ('FUNDING_FEE', new_from_ms, new_through_ms, 1, observed_at, json.dumps(detail, sort_keys=True)),
                )
        self._funding_coverage_cache = dict(
            scope='FUNDING_FEE',
            covered_from_ms=new_from_ms,
            covered_through_ms=new_through_ms,
            complete=True,
            updated_at=observed_at,
            detail=detail,
        )
        self._funding_synced = True
        self._last_funding_sync_ms = end_ms

        return FundingSyncResult(
            start_ms=ranges[0][0],
            end_ms=ranges[-1][1],
            covered_from_ms=new_from_ms,
            covered_through_ms=new_through_ms,
            events=self.funding_events(),
            newly_recorded=tuple(newly_recorded),
            windows=total_windows,
            pages=total_pages,
            complete=True,
        )

    def read_activity(self, symbol):
        facts = self.read_facts()
        return replace(facts, trades=self.user_trades(symbol), income=self.income(symbol))

    def read_facts(self):
        try:
            return self._read_facts()
        except (DataQualityError, ValueError, KeyError, TypeError, SafetyBlocked):
            self.facts = None
            self.disconnect()
            raise

    def _read_facts(self):
        from .network import DemoNetworkTransport
        parallel = type(self.client.transport) is DemoNetworkTransport
        self._validate_network_identity()
        if parallel:
            self.quantitative_observation = None
            self.client.demo_time(min_valid_seconds=ACCOUNT_READ_SECONDS)
        started = self.client.monotonic()
        observed_at = self.clock()
        error = None
        try:
            with self._account_reads(observed_at, started, parallel=parallel) as read:
                facts = self._assemble_facts(observed_at, started, read)
            if parallel:
                facts.confirmed_position_amounts()
                facts.open_order_identities()
                self._require_fresh_account_read(observed_at, started, self.clock())
        except Exception as exc:
            error = getattr(exc, 'category', type(exc).__name__)
            if parallel and self.quantitative_observation is None:
                from .quantitative import QuantitativeObservation
                self.quantitative_observation = QuantitativeObservation(error=error)
            raise
        finally:
            if parallel:
                self.client._clock_event('account_read_set', started_at=observed_at.isoformat(),
                    started_monotonic=started, ended_at=self.clock().isoformat(),
                    ended_monotonic=self.client.monotonic(), error=error)
        self.facts = facts
        return facts

    @contextmanager
    def _account_reads(self, observed_at, started, *, parallel):
        if not parallel:
            def read(path):
                payload = self.client.request('GET', path)
                self._require_fresh_account_read(observed_at, started, self.clock())
                return payload
            yield read
            return

        window = AccountReadWindow(started + ACCOUNT_READ_SECONDS)
        early_permission = self._read_v2_permission_early

        def fetch(path):
            reader = self.client.account_reader(window)
            payload = reader.request('GET', path)
            if path == ACCOUNT_PATH:
                permission = None
                if not early_permission and isinstance(payload, dict) and 'canTrade' not in payload:
                    permission = reader.request('GET', ACCOUNT_PERMISSION_PATH)
                return payload, permission
            return payload

        endpoints = (*ACCOUNT_PATHS, QUANTITATIVE_PATH)
        if early_permission:
            endpoints = (ACCOUNT_PERMISSION_PATH, *endpoints)
        with ThreadPoolExecutor(max_workers=6, thread_name_prefix='demo-account') as pool:
            pending = {path: pool.submit(fetch, path) for path in endpoints}
            _, unfinished = wait(pending.values(), timeout=max(0, window.deadline - self.client.monotonic()))
            if unfinished:
                for future in unfinished:
                    future.cancel()
                raise AccountReadExpired('STALE_ACCOUNT: account read set exceeded five seconds')

            def read(path):
                if path == ACCOUNT_PERMISSION_PATH and not early_permission:
                    return pending[ACCOUNT_PATH].result()[1]
                result = pending[path].result()
                return result[0] if path == ACCOUNT_PATH else result

            yield read

    def _assemble_facts(self, observed_at, started, read):
        account, risk, mode, orders, config = (read(path) for path in ACCOUNT_PATHS)
        if not isinstance(account, dict) or not isinstance(mode, dict) or not all(
                isinstance(rows, list) and all(isinstance(row, dict) for row in rows)
                for rows in (risk, orders, config)):
            raise DataQualityError('invalid account response shapes')
        hedge = mode.get('dualSidePosition')
        if hedge is not None and type(hedge) is not bool:
            raise DataQualityError('invalid position mode')
        from .quantitative import account_can_trade, observe_quantitative
        quantitative = None
        can_trade = account_can_trade(account)
        from .network import DemoNetworkTransport
        readonly_demo = (isinstance(self.client.transport, ReadOnlyNetworkTransport)
                         and self.client.environment is Environment.DEMO)
        network_demo = type(self.client.transport) is DemoNetworkTransport
        if network_demo or readonly_demo:
            permission_missing = 'canTrade' not in account
            if network_demo and permission_missing:
                # Remember the response shape. Each round still reads a new permission.
                self._read_v2_permission_early = True
            if permission_missing or (network_demo and self._read_v2_permission_early):
                try:
                    can_trade = account_can_trade(account, read(ACCOUNT_PERMISSION_PATH))
                except DataQualityError:
                    if not readonly_demo:
                        raise
                if network_demo and not permission_missing:
                    if can_trade is None:
                        raise DataQualityError('conflicting Demo account trade permissions')
                    self._read_v2_permission_early = False
            from .quantitative import QuantitativeObservation
            try:
                status = read(QUANTITATIVE_PATH)
            except (DataQualityError, OSError, TimeoutError) as error:
                category = error.category if isinstance(error, ExchangeError) else type(error).__name__
                quantitative = QuantitativeObservation(error=category)
                if not readonly_demo:
                    self.quantitative_observation = quantitative
                    raise
            else:
                quantitative = observe_quantitative(status)
            self.quantitative_observation = quantitative
            if quantitative.error and not readonly_demo:
                self.facts = None
                raise DataQualityError('quantitative observation UNKNOWN: ' + quantitative.error)
        assets, positions = account.get('assets'), account.get('positions')
        assets_known = isinstance(assets, list) and all(isinstance(row, dict) for row in assets)
        positions_known = isinstance(positions, list) and all(isinstance(row, dict) for row in positions)
        self._require_fresh_account_read(observed_at, started, self.clock())
        return AccountFacts(self.account_id, observed_at, tuple(assets) if assets_known else (),
            tuple(positions) if positions_known else (), tuple(risk), tuple(orders), (), (), hedge, tuple(config),
            assets_known and positions_known, can_trade, quantitative, read_started_monotonic=started)

    def _require_fresh_account_read(self, observed_at, started, now):
        if (self.client.environment is Environment.DEMO
                and (self.client.monotonic() - started > ACCOUNT_READ_SECONDS
                     or not 0 <= (now - observed_at).total_seconds() <= ACCOUNT_READ_SECONDS)):
            raise AccountReadExpired('STALE_ACCOUNT: account read set exceeded five seconds')

    def read_account(self):
        return self._account_snapshot(self.read_facts())

    def _account_snapshot(self, facts):
        records = [decode(json.loads(row[0])).snapshot for row in self.db.execute('SELECT payload FROM futures_orders')]
        known = {o.intent.client_order_id for o in records if o.status not in TERMINAL}
        actual = {o['clientOrderId'] for o in facts.open_orders}
        return facts.shared([o for o in records if o.status not in TERMINAL], reconciled=self.reconciled and known == actual)

    def reconcile(self):
        return self._reconcile()

    def _reconcile(self, *, initial_facts=None):
        # Any failure leaves the durable global risk gate closed.
        self.disconnect()
        # TODO: replace lifetime polling only with a contract proving history
        # coverage and visibility of late fills/commission corrections. Neither
        # terminal status nor a forward-only trade ID proves that completeness.
        keys = [row[0] for row in self.db.execute('SELECT id FROM futures_orders')]
        if initial_facts is not None:
            self._validate_network_identity()
            if keys or initial_facts is not self.facts:
                raise SafetyBlocked('initial Demo facts require an unchanged empty order journal')
        for key in keys:
            order = self.order(key)
            if order.status == S.REJECTED and order.exchange_order_id is None:
                continue
            self.query_order(key)
        facts = self.read_facts() if initial_facts is None else initial_facts
        known = {key for key in keys if self.order(key).status not in TERMINAL}
        actual = {o['clientOrderId'] for o in facts.open_orders}
        snapshot = facts.shared(reconciled=known == actual)
        if not snapshot.reconciled:
            raise DataQualityError('account incomplete, hedge mode or unmanaged open orders: reconciliation blocked')
        if initial_facts is not None:
            self._require_fresh_account_read(facts.observed_at, facts.read_started_monotonic, self.clock())
        with self.store.transaction():
            self.db.execute('UPDATE futures_meta SET reconciled=1')
        return self._account_snapshot(facts)
