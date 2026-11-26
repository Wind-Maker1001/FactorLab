"""Alpaca facts-to-portfolio bridge; networking stays in the official adapter."""
from datetime import datetime, timezone
from decimal import Decimal as D
from hashlib import sha256

from factorlab.trading.contracts import OrderEvent, OrderStatus, Side
from factorlab.trading.control import RiskRejected
from factorlab.trading.portfolio_execution import (
    ExecutionQuote, OrderAbsent, PortfolioExecutor, SubmissionNotAttempted,
)
from factorlab.trading.storage import dumps


def portfolio_quote(quote, *, instrument_id, symbol):
    """Bind ALP-E02 InstrumentQuote to an explicit stable-ID/vendor-symbol pair."""
    if quote.instrument.instrument_id != instrument_id or quote.instrument.symbol != symbol:
        raise ValueError('execution quote identity mapping mismatch')
    session = quote.session.date().isoformat() if quote.session is not None else ''
    return ExecutionQuote(symbol, quote.bid, quote.ask, quote.event_at, quote.received_at,
        session, quote.source + ':' + quote.feed, quote.valid,
        ','.join(quote.validity_reasons), tuple(quote.decision_window))


class AlpacaPortfolioAdapter:
    """Use execution activities, not a different consumer's incremental cursor.

    order_not_found is ALP-E01's public OrderNotFound exception type. It is
    injected so offline tests use the same boundary without a second HTTP client.
    PortfolioStore owns the consumed cumulative notional across process restarts.
    """

    def __init__(self, adapter, store, *, order_not_found, rejection_error=(),
                 clock=lambda: datetime.now(timezone.utc)):
        self.adapter, self.store = adapter, store
        self.order_not_found, self.rejection_error, self.clock = order_not_found, rejection_error, clock

    def read_account(self):
        # Original-order recovery can observe a fill after the runner's initial
        # account recovery, invalidating its inventory/cash admission snapshot.
        return self.adapter.recover()

    def query_order(self, client_order_id):
        try:
            details = self.adapter.read_order_details(client_order_id)
            executions = self.adapter.read_order_executions(client_order_id)
        except self.order_not_found:
            rejection = self.adapter.store.get('rejection:' + client_order_id)
            if rejection is not None:
                row = self.store.db.execute('SELECT plan FROM portfolio_orders WHERE id=?',
                                            (client_order_id,)).fetchone()
                order = next(o for o in self.store.orders(row[0])
                    if o['record'].snapshot.intent.client_order_id == client_order_id)
                return OrderEvent(client_order_id, None, OrderStatus.REJECTED, D(0), None,
                    order['record'].observed_at, 'alpaca-paper-http-rejection:' + str(rejection['status']),
                    'alpaca-paper-http-rejection:' + client_order_id)
            # ALP-E01 commits this admission journal before every HTTP POST.
            # Its absence proves the process stopped before entering submission;
            # an existing journal plus REST 404 remains genuinely uncertain.
            if self.adapter.store.order(client_order_id) is None:
                raise SubmissionNotAttempted(client_order_id) from None
            raise OrderAbsent(client_order_id) from None
        if details['client_order_id'] != client_order_id:
            raise ValueError('Alpaca client order identity mismatch')
        row = self.store.db.execute('SELECT plan FROM portfolio_orders WHERE id=?',
                                    (client_order_id,)).fetchone()
        plan = self.store.plan(row[0])
        order = next(o for o in self.store.orders(plan['id'])
                     if o['record'].snapshot.intent.client_order_id == client_order_id)
        intent = order['record'].snapshot.intent
        unique = {}
        for execution in executions:
            identity = execution['id']
            if identity in unique and unique[identity] != execution:
                raise ValueError('Alpaca execution identity changed')
            if (execution['order_id'] != details['exchange_order_id']
                    or execution['symbol'] != intent.instrument_id
                    or Side(execution['side'].upper()) != intent.side):
                raise ValueError('Alpaca execution belongs to another instrument or order')
            unique[identity] = execution
        quantity = sum((D(row['qty']) for row in unique.values()), D(0))
        notional = sum((D(row['qty']) * D(row['price']) for row in unique.values()), D(0))
        # Order state and execution activities are asynchronously published.
        # Do not invent an execution price from a rounded cumulative average.
        if quantity != details['filled_quantity']:
            raise TimeoutError('Alpaca execution activities have not reconciled to order quantity')
        previous = order['record'].snapshot.cumulative_filled_quantity
        delta = quantity - previous
        if delta < 0:
            raise ValueError('Alpaca cumulative execution regression')
        amount_delta = notional - order.get('filled_notional', D(0))
        if delta == 0 and amount_delta != 0:
            raise ValueError('Alpaca execution cash flow changed without a new fill')
        price = amount_delta / delta if delta else None
        # The event identity includes the consumer baseline because fill_price
        # describes the delta, not the full cumulative quantity.
        event_id = 'alpaca-portfolio:' + sha256(dumps([
            client_order_id, details['status'], quantity, notional, previous,
            details['observed_at']]).encode()).hexdigest()
        return OrderEvent(client_order_id, details['exchange_order_id'], details['status'],
            quantity, price, details['observed_at'], 'alpaca-paper-execution-activities', event_id,
            cumulative_filled_notional=notional)

    def submit_risk_checked(self, intent, admit):
        return self.submit_order(intent, risk_admission=admit)

    def submit_order(self, intent, *, risk_admission=None):
        price = intent.limit_price
        if price is None:
            row = self.store.db.execute('SELECT plan FROM portfolio_orders WHERE id=?',
                                        (intent.client_order_id,)).fetchone()
            price = next(order['reservation_price'] for order in self.store.orders(row[0])
                         if order['record'].snapshot.intent.client_order_id == intent.client_order_id)
        try:
            if risk_admission is None:
                self.adapter.submit_order(intent, reservation_price=price)
            else:
                self.adapter.submit_order(intent, reservation_price=price, risk_admission=risk_admission)
        except RiskRejected as error:
            return OrderEvent(intent.client_order_id, None, OrderStatus.REJECTED, D(0), None,
                self.clock(), 'alpaca-local-admission:' + error.decision.reason,
                'alpaca-local-rejection:' + intent.client_order_id)
        except self.rejection_error:
            if self.adapter.store.get('rejection:' + intent.client_order_id) is None:
                raise
            return self.query_order(intent.client_order_id)
        return self.query_order(intent.client_order_id)

    def cancel_order(self, client_order_id):
        self.adapter.cancel_order(client_order_id)
        return self.query_order(client_order_id)


class AlpacaPortfolioRunner:
    """Coordinate official account recovery with the market-agnostic executor."""

    def __init__(self, adapter, store, *, order_not_found, rejection_error=(),
                 clock=lambda: datetime.now(timezone.utc)):
        self.adapter, self.store, self.clock = adapter, store, clock
        self.bridge = AlpacaPortfolioAdapter(adapter, store, order_not_found=order_not_found,
                                            rejection_error=rejection_error, clock=clock)
        self.executor = PortfolioExecutor(self.bridge, store, quote_currency='USD', clock=clock)

    def run_once(self, identity, *, quotes, rules, not_before=None):
        self.adapter.recover()
        facts = self.adapter.read_account_details()
        request = self.store.plan(identity)['request']
        if facts.account_id != request['account']:
            raise ValueError('Alpaca account differs from persisted portfolio identity')
        refresh = quotes if callable(quotes) else None
        if refresh is not None:
            quotes = refresh()
        return self.executor.run_once(identity, equity=facts.equity, quotes=quotes, rules=rules,
                                      now=self.clock(), not_before=not_before,
                                      refresh_quotes=refresh)

    def report(self, identity, *, adjustments=()):
        self.adapter.recover()
        facts = self.adapter.read_account_details()
        return self.executor.report(identity, official_equity=facts.equity, adjustments=adjustments)
