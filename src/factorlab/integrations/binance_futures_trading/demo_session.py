"""Compose authenticated Demo sessions over the existing controller and journals."""

from contextlib import closing
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
import json
import re
from urllib.parse import parse_qs

from factorlab.trading import Market, OrderIntent, OrderType, Side
from factorlab.trading.contracts import TERMINAL
from factorlab.trading.control import RiskLimits, TradingController
from factorlab.trading.paper import Instrument, Quote
from factorlab.trading.storage import PaperStore

from .adapter import FuturesAdapter, STATUS
from .demo_preflight import BOOK, DEMO, SYMBOL, _book, _positions
from .execution import DemoExecutionGateway, DemoFuturesAdapter, observe_demo_rules
from .network import DemoNetworkTransport
from .protocol import ExchangeError, FuturesClient, SafetyBlocked, rejection_message
from .rules import symbol_row


def _new_request_counts():
    return dict(GET=0, POST=0, DELETE=0)


def _demo_client(credential, transport):
    if credential is None or credential.environment is not DEMO:
        raise SafetyBlocked('Demo credentials required')
    return FuturesClient(credential, environment=DEMO, transport=transport)


def _read_account(client, *, expected_id=None):
    with closing(PaperStore(':memory:')) as store:
        adapter = FuturesAdapter(client, store, account_id=DEMO.account_id)
        if expected_id is not None and adapter.account_id != expected_id:
            raise SafetyBlocked('persisted Demo account binding mismatch')
        client.sync_time()
        return adapter.read_facts()


def observe_demo_account(credential, *, request_counts=None):
    """Confirm a Demo credential against the exchange before binding local state."""
    requests = _new_request_counts() if request_counts is None else request_counts

    def observe(request, response):
        requests[request.method] += 1

    client = _demo_client(credential, DemoNetworkTransport(observe_response=observe))
    return _read_account(client), requests


def demo_account_configuration(facts):
    _positions(facts)
    if facts.hedge_mode is not False or not facts.shared().assets:
        raise SafetyBlocked('complete one-way Demo account required')
    config = symbol_row(list(facts.symbol_config), SYMBOL)
    leverage, margin_type = config.get('leverage'), config.get('marginType')
    if type(leverage) is not int or leverage < 1 or margin_type not in {'CROSSED', 'ISOLATED'}:
        raise SafetyBlocked('Demo leverage or margin configuration unavailable')
    return dict(symbol=SYMBOL, position_mode='ONE_WAY', leverage=leverage, margin_type=margin_type)


def _order_observables(payload):
    key = payload.get('orderId')
    order_id = str(key) if type(key) is int and key > 0 else None
    status = payload.get('status')
    filled_quantity = payload.get('executedQty')
    # Only accept Binance fixed-point numeric quantities at the receipt boundary.
    if not isinstance(filled_quantity, str) or len(filled_quantity) > 32 or not re.fullmatch(
            r'[0-9]+(?:\.[0-9]+)?', filled_quantity):
        filled_quantity = None
    return dict(order_id=order_id, status=status if isinstance(status, str) and status in STATUS else None,
                filled_quantity=filled_quantity)


class DemoSession:
    """A caller-owned session whose persistent write guard is independent of recovery."""

    def __init__(self, credential, *, protocol_store, controller_store, account_id, configuration,
                 authorization=None, limits=RiskLimits(), write_guard=None, receipt_sink=None, request_counts=None):
        if (authorization is None) != (write_guard is None):
            raise SafetyBlocked('Demo writes require authorization and a persistent guard')
        self.requests = _new_request_counts() if request_counts is None else request_counts
        self.receipts = []
        self.receipt_error = False
        self.write_uncertain = False
        self.configuration = configuration
        self._write_guard = write_guard
        self._receipt_sink = receipt_sink
        self._order_quote_at = None
        transport = DemoNetworkTransport(authorization=authorization, observe_response=self._observe_response,
            write_guard=self._admit_write if write_guard is not None else None)
        self.client = _demo_client(credential, transport)
        facts = _read_account(self.client, expected_id=account_id)
        if authorization is not None and demo_account_configuration(facts) != configuration:
            raise SafetyBlocked('persisted Demo account configuration mismatch')

        self.adapter = DemoFuturesAdapter(self.client, protocol_store, clock=self._now)
        observation = observe_demo_rules(self.adapter, source_version=DEMO.host, on_symbol=self._check_symbol)
        self.adapter.facts = facts
        balance = facts.usdt()['walletBalance']
        if balance is None:
            raise SafetyBlocked('Demo wallet balance unavailable')
        leverage = configuration['leverage']
        self.gateway = DemoExecutionGateway(self.adapter, observation, (Instrument(SYMBOL, 'USDT'),),
            {'USDT': balance}, leverage=leverage, max_leverage=leverage)
        self.controller = TradingController(self.gateway, controller_store, clock=self._now, limits=limits)
        self.gateway.bind_controller(self.controller)

    def _now(self):
        return datetime.now(timezone.utc) + timedelta(milliseconds=self.client.offset_ms)

    def _check_symbol(self, symbol):
        if self._write_guard is not None and 'GTC' not in symbol.get('timeInForce', ()):
            raise SafetyBlocked('Demo symbol lacks current GTC support')

    def _admit_write(self, method, params, plan):
        if self.receipt_error or self.write_uncertain:
            raise SafetyBlocked('previous Demo write requires GET-only recovery')
        if any(receipt['http_status'] == 400 for receipt in self.receipts):
            raise SafetyBlocked('Demo HTTP 400 stopped further writes')
        now = self._now()
        if not timedelta(0) <= now - self.adapter.facts.observed_at <= timedelta(seconds=5):
            raise SafetyBlocked('STALE_ACCOUNT at Demo dispatch')
        if method == 'POST':
            if not timedelta(0) <= now - self._order_quote_at <= timedelta(seconds=5):
                raise SafetyBlocked('STALE_QUOTE at Demo dispatch')
        self._write_guard(method, params, plan)

    def _observe_response(self, request, response):
        method = request.method
        self.requests[method] += 1
        if method == 'GET':
            return
        client_id_key = 'newClientOrderId' if method == 'POST' else 'origClientOrderId'
        client_id = parse_qs(request.query)[client_id_key][0]
        status = response.status if response is not None else None
        payload = {}
        if response is not None and len(response.body) <= 2 * 1024 * 1024:
            try:
                body = json.loads(response.body)
            except (ValueError, UnicodeError):
                body = None
            if isinstance(body, dict):
                payload = body
        code = payload.get('code')
        receipt = dict(method=method, http_status=status, client_order_id=client_id,
            code=code if type(code) is int else None,
            message=rejection_message(payload, credentials=self.client.credentials, request=request),
            **_order_observables(payload))
        self.receipts.append(receipt)
        if self._receipt_sink is not None:
            try:
                self._receipt_sink(receipt)
            except Exception:
                # Let the adapter reconcile the actual response even when reporting fails.
                self.receipt_error = True

    def _fresh_book(self):
        book = _book(self.client.request('GET', BOOK, {'symbol': SYMBOL}, signed=False))
        if not timedelta(0) <= self._now() - book['time_utc'] <= timedelta(seconds=5):
            raise SafetyBlocked('STALE_QUOTE: Demo top of book')
        return book

    def _protocol_ids(self):
        return tuple(row[0] for row in self.adapter.db.execute('SELECT id FROM futures_orders ORDER BY rowid'))

    def recover(self):
        """Reconcile the existing journals and account via GET only."""
        if self.gateway.state.quotes.get(SYMBOL) is None and self._protocol_ids():
            book = self._fresh_book()
            self.controller.quote(Quote(SYMBOL, book['askPrice'], book['time_utc']))
        self.gateway.recover(self.controller)
        return self.status()

    def _require_absent(self, client_order_id):
        try:
            self.client.request('GET', '/fapi/v1/order', {'symbol': SYMBOL, 'origClientOrderId': client_order_id})
        except ExchangeError as error:
            if error.code == -2013 and error.category == 'ORDER_NOT_FOUND':
                return
            raise
        raise SafetyBlocked('Demo clientOrderId already exists; use GET recovery')

    def _fresh_bound_facts(self):
        facts = self.adapter.read_facts()
        if demo_account_configuration(facts) != self.configuration:
            raise SafetyBlocked('persisted Demo account configuration mismatch')
        return facts

    def _fresh_facts_for_submission(self):
        facts = self._fresh_bound_facts()
        if facts.open_orders or self.gateway.read_account().open_orders:
            raise SafetyBlocked('active Demo orders prevent adding another order')
        confirmed = {p.instrument_id: (p.quantity, p.entry_price) for p in facts.shared().positions}
        local = {p.instrument_id: (p.quantity, p.entry_price) for p in self.gateway.state.positions.values()}
        if confirmed != local or any(symbol != SYMBOL for symbol in confirmed):
            raise SafetyBlocked('Demo account position changed; use GET recovery')

    def prepare_limit_book(self):
        """Refresh bound account facts and the book used for target sizing."""
        self._fresh_facts_for_submission()
        return self._fresh_book()

    @staticmethod
    def limit_price(rules, side, book):
        """Round the executable inside price onto the observed tick grid."""
        tick = Decimal(rules.filters['PRICE_FILTER']['tickSize'])
        if tick <= 0:
            raise SafetyBlocked('positive Demo price tick required')
        inside, rounding = (book['askPrice'], ROUND_CEILING) if side is Side.BUY else (book['bidPrice'], ROUND_FLOOR)
        return (inside / tick).to_integral_value(rounding=rounding) * tick

    def submit_limit(self, client_order_id, side, quantity, *, reduce_only=False, book=None):
        """Submit once from the current inside market through the shared controller."""
        book = self._fresh_book() if book is None else book
        price = self.limit_price(self.gateway.rules, side, book)
        intent = OrderIntent(Market.BINANCE_USDM, SYMBOL, side, OrderType.LIMIT, quantity, price,
                             client_order_id, position_side='BOTH', reduce_only=reduce_only)
        return self.submit_intent(intent, book=book)

    def submit_intent(self, intent, *, book=None):
        """Validate and deliver an exact LIMIT intent without repricing or resizing."""
        if (intent.market is not Market.BINANCE_USDM or intent.instrument_id != SYMBOL
                or intent.order_type is not OrderType.LIMIT or intent.position_side not in (None, 'BOTH')):
            raise SafetyBlocked('Demo runtime requires NVDAUSDT one-way LIMIT')
        client_order_id, side, reduce_only = intent.client_order_id, intent.side, intent.reduce_only
        if self._write_guard is None or not self.client.transport.permitted():
            raise SafetyBlocked('active Demo runtime authorization required')
        if (side is not Side.BUY and side is not Side.SELL) or reduce_only is not (side is Side.SELL):
            raise SafetyBlocked('Demo runtime admits BUY LIMIT and reduce-only SELL LIMIT')
        if client_order_id in self._protocol_ids():
            raise SafetyBlocked('Demo clientOrderId already journaled; use GET recovery')
        if not self.gateway.read_account().reconciled:
            raise SafetyBlocked('reconcile Demo orders and account before submission')
        self._require_absent(client_order_id)
        self._fresh_facts_for_submission()
        book = self._fresh_book() if book is None else book
        if not timedelta(0) <= self._now() - book['time_utc'] <= timedelta(seconds=5):
            raise SafetyBlocked('STALE_QUOTE: Demo prepared book')
        self._order_quote_at = book['time_utc']
        self.controller.quote(Quote(SYMBOL, book['askPrice'], self._order_quote_at))
        if self.controller.submit(intent) is None:
            self.write_uncertain = True
        return self.adapter.order(client_order_id)

    def cancel(self, client_order_id):
        """Cancel only a known active order, subject to the same persistent guard."""
        if self._write_guard is None or not self.client.transport.permitted():
            raise SafetyBlocked('active Demo runtime authorization required')
        order = self.adapter.order(client_order_id)
        if order.reconcile_required or order.exchange_order_id is None or order.status in TERMINAL:
            raise SafetyBlocked('Demo cancellation requires a known active order')
        facts = self._fresh_bound_facts()
        if not any(row['symbol'] == SYMBOL and row['clientOrderId'] == client_order_id
                   and str(row['orderId']) == order.exchange_order_id for row in facts.open_orders):
            raise SafetyBlocked('Demo order no longer observed active; use GET recovery')
        if self.controller.cancel(client_order_id) is None:
            self.write_uncertain = True
        return self.adapter.order(client_order_id)

    def status(self):
        """Selected account and protocol facts with no full credentials or binding."""
        orders = {}
        for client_id in self._protocol_ids():
            record, filled_notional = self.adapter._load(client_id)
            order = record.snapshot
            diagnostic = self.adapter.db.execute('SELECT payload FROM futures_rejections WHERE id=?', (client_id,)).fetchone()
            event = self.gateway.state.confirmations.get(client_id)
            orders[client_id] = dict(order_id=order.exchange_order_id, side=order.intent.side.value,
                status=order.status.value, quantity=order.intent.quantity, limit_price=order.intent.limit_price,
                filled_quantity=order.cumulative_filled_quantity, fee_usdt=order.cumulative_fee,
                average_fill_price=filled_notional / order.cumulative_filled_quantity if order.cumulative_filled_quantity else None,
                exchange_updated_at=record.observed_at if order.exchange_order_id is not None else None,
                reduce_only=order.intent.reduce_only, reconcile_required=order.reconcile_required,
                event_id=event.event_id if event is not None else None,
                rejection=json.loads(diagnostic[0]) if diagnostic is not None else None)
        facts = self.adapter.facts
        positions = [row for row in facts.position_details() if row['quantity'] != 0] if facts is not None else None
        open_orders = [dict(client_order_id=row['clientOrderId'], symbol=row['symbol'],
            side=row.get('side'), **_order_observables(row)) for row in facts.open_orders] if facts is not None else None
        return dict(reconciled=facts is not None and bool(facts.shared().assets) and self.gateway.read_account().reconciled,
            account_observed_at=facts.observed_at if facts is not None else None,
            can_trade=facts.can_trade if facts is not None else None,
            position_mode=None if facts is None or facts.hedge_mode is None else 'HEDGE' if facts.hedge_mode else 'ONE_WAY',
            balances=facts.usdt() if facts is not None else None, positions=positions, open_orders=open_orders,
            orders=orders, unknown=[key for key, value in orders.items() if value['reconcile_required']],
            requests=dict(self.requests), receipt_error=self.receipt_error)
