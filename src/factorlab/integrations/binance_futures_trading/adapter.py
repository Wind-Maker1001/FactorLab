"""Persistent offline execution adapter implementing the shared one-way contract."""
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json

from factorlab.errors import DataQualityError
from factorlab.integrations.binance.market import _decimal, symbol_name
from factorlab.trading import OrderEvent, OrderSnapshot, OrderStatus as S
from factorlab.trading.contracts import TERMINAL
from factorlab.trading.state import OrderRecord, advance, unknown
from factorlab.trading.storage import dumps, decode

from .account import AccountFacts, instant
from .protocol import ExchangeError, OutcomeUnknown, SafetyBlocked, OfflineTransport
from .rules import order_parameters

ZERO = Decimal('0')
STATUS = {'NEW': S.SUBMITTED, 'PARTIALLY_FILLED': S.PARTIALLY_FILLED, 'FILLED': S.FILLED,
          'CANCELED': S.CANCELED, 'REJECTED': S.REJECTED, 'EXPIRED': S.CANCELED,
          'EXPIRED_IN_MATCH': S.CANCELED}


class FuturesAdapter:
    """Use a dedicated PaperStore for protocol state, separate from paper accounting.

    GET reads may be explicitly authorized. Submit/cancel never use real network.
    Restart/disconnect require full REST account/order reconciliation.
    """
    def __init__(self, client, store, *, account_id='offline', clock=lambda: datetime.now(timezone.utc)):
        if client.environment is not None:
            if account_id != client.environment.account_id:
                raise SafetyBlocked('explicit environment account identity required')
            account_id = client.environment.name + ':' + account_id
            if client.credentials is not None:
                account_id += ':' + hashlib.sha256(client.credentials.key.encode()).hexdigest()
        self.client, self.store, self.account_id, self.clock = client, store, account_id, clock
        self.facts = None
        self.quantitative_observation = None
        self.db = store.db
        self.db.execute('CREATE TABLE IF NOT EXISTS futures_orders (id TEXT PRIMARY KEY, payload TEXT NOT NULL, quote TEXT NOT NULL)')
        self.db.execute('CREATE TABLE IF NOT EXISTS futures_events (id TEXT PRIMARY KEY, payload TEXT NOT NULL)')
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
                if self.db.execute('SELECT count(*) FROM futures_orders').fetchone()[0] >= self.store.max_orders:
                    raise ValueError('adapter order capacity exhausted')
                # No confirmed exchange timestamp exists before dispatch.
                record = OrderRecord(OrderSnapshot(intent), instant(0), intent.limit_price or ZERO)
                self._save(unknown(record), ZERO)
                existing = False
        if existing:
            return self.query_order(intent.client_order_id)
        try:
            row = self.client.request('POST', '/fapi/v1/order', params)
            return self._rest_order(row, intent.client_order_id)
        except ExchangeError as exc:
            if exc.category not in {'RATE_LIMIT', 'ORDER_NOT_FOUND'} and exc.status < 500:
                event = OrderEvent(intent.client_order_id, None, S.REJECTED, ZERO, None,
                                   self.clock(), 'binance-rest', 'reject:' + intent.client_order_id, ZERO)
                return self._apply(event, ZERO)
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
            return self._rest_order(row, client_order_id)
        except (OutcomeUnknown, DataQualityError, ValueError, KeyError, TypeError):
            self._uncertain(client_order_id)
            raise OutcomeUnknown('cancel unconfirmed; reconcile fills before releasing risk') from None

    def _validate_network_identity(self):
        from .network import DemoNetworkTransport
        from .protocol import Environment
        if type(self.client.transport) is DemoNetworkTransport:
            credentials = self.client.credentials
            expected = ('DEMO:FactorLab-Demo:' + hashlib.sha256(credentials.key.encode()).hexdigest()
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

    def _rest_order(self, row, expected_id):
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
        return self._apply(event, quote)

    def _apply(self, event, quote):
        with self.store.transaction():
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
            if self.db.execute('SELECT count(*) FROM futures_events').fetchone()[0] >= self.store.max_events:
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

    def income(self, symbol=None):
        rows = self.client.request('GET', '/fapi/v1/income', dict(limit=1000, **({} if symbol is None else {'symbol': symbol_name(symbol)})))
        if not isinstance(rows, list) or len(rows) >= 1000:
            raise DataQualityError('income window incomplete; explicit pagination required')
        return tuple(rows)

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
        self._validate_network_identity()
        account = self.client.request('GET', '/fapi/v3/account')
        risk = self.client.request('GET', '/fapi/v3/positionRisk')
        mode = self.client.request('GET', '/fapi/v1/positionSide/dual')
        orders = self.open_orders()
        config = self.client.request('GET', '/fapi/v1/symbolConfig')
        if not isinstance(account, dict) or not isinstance(risk, list) or not isinstance(orders, list) or not isinstance(config, list):
            raise DataQualityError('invalid account response shapes')
        hedge = mode.get('dualSidePosition')
        if hedge is not None and type(hedge) is not bool:
            raise DataQualityError('invalid position mode')
        from .quantitative import account_can_trade, observe_quantitative
        quantitative = None
        can_trade = account_can_trade(account)
        from .network import DemoNetworkTransport
        if type(self.client.transport) is DemoNetworkTransport:
            if 'canTrade' not in account:
                can_trade = account_can_trade(account, self.client.request('GET', '/fapi/v2/account'))
            from .quantitative import QuantitativeObservation
            try:
                status = self.client.request('GET', '/fapi/v1/apiTradingStatus')
            except (DataQualityError, OSError, TimeoutError) as error:
                category = error.category if isinstance(error, ExchangeError) else type(error).__name__
                self.quantitative_observation = QuantitativeObservation(error=category)
                raise
            quantitative = observe_quantitative(status)
            self.quantitative_observation = quantitative
            if quantitative.error:
                self.facts = None
                raise DataQualityError('quantitative observation UNKNOWN: ' + quantitative.error)
        self.facts = AccountFacts(self.account_id, self.clock(), tuple(account.get('assets', ())),
            tuple(account.get('positions', ())), tuple(risk), tuple(orders), (), (), hedge, tuple(config),
            isinstance(account.get('assets'), list) and isinstance(account.get('positions'), list),
            can_trade, quantitative)
        return self.facts

    def read_account(self):
        facts = self.read_facts()
        records = [decode(json.loads(row[0])).snapshot for row in self.db.execute('SELECT payload FROM futures_orders')]
        known = {o.intent.client_order_id for o in records if o.status not in TERMINAL}
        actual = {o['clientOrderId'] for o in facts.open_orders}
        return facts.shared([o for o in records if o.status not in TERMINAL], reconciled=self.reconciled and known == actual)

    def reconcile(self):
        # Any failure leaves the durable global risk gate closed.
        self.disconnect()
        keys = [row[0] for row in self.db.execute('SELECT id FROM futures_orders')]
        for key in keys:
            order = self.order(key)
            if order.status == S.REJECTED and order.exchange_order_id is None:
                continue
            self.query_order(key)
        facts = self.read_facts()
        known = {key for key in keys if self.order(key).status not in TERMINAL}
        actual = {o['clientOrderId'] for o in facts.open_orders}
        snapshot = facts.shared(reconciled=known == actual)
        if not snapshot.reconciled:
            raise DataQualityError('account incomplete, hedge mode or unmanaged open orders: reconciliation blocked')
        with self.store.transaction():
            self.db.execute('UPDATE futures_meta SET reconciled=1')
        return self.read_account()
