"""Durable V2 decisions over the existing account portfolio outbox."""
from decimal import Decimal as D

from factorlab.strategies.protocol import read_decision, validate_binding
from factorlab.trading.contracts import TERMINAL
from factorlab.trading.storage import decode, dumps
from hashlib import sha256
from dataclasses import asdict
import json


class AlpacaDecisionExecution:
    def __init__(self, executor, assets, spec, binding, allocation, *, clock, quote_provider=None):
        self.executor, self.assets, self.spec = executor, assets, spec
        self.binding, self.allocation, self.clock = binding, allocation, clock
        self.quote_provider = quote_provider

    def bind_market(self, service, data):
        def quotes(symbols):
            at = self.clock()
            session = service.calendar.regular_session(at)
            if session is None:
                raise TimeoutError('market-order reservation requires an open session')
            return data.quotes(service, symbols, session=session, decision_at=at)
        self.quote_provider = quotes

    def reservation_quote(self, receipt, symbol):
        store = self.executor.store
        identity = 'alpaca-reservation-quote:' + sha256(dumps([
            self.binding, receipt['package_id'], receipt['event_id'], symbol]).encode()).hexdigest()
        saved = store.event_payload(identity)
        if saved is not None:
            return decode(json.loads(saved))['ask']
        quotes = self.quote_provider((symbol,))
        quote = quotes.get(symbol)
        if quote is None or not quote.valid:
            raise TimeoutError('market-order reservation requires a valid execution quote: ' + symbol)
        with store.transaction():
            store.record_metadata(identity, asdict(quote))
        return quote.ask

    def qualify(self, spec, binding, allocation):
        self.assets.qualify(spec, binding, allocation)
        if binding != self.binding:
            raise ValueError('execution adapter belongs to another instance')
        if self.executor.capital_policy.is_configured(binding['account_id']):
            self.executor.capital_policy.qualify(spec, binding, allocation)

    def _cancel(self, cancellation):
        store = self.executor.store
        row = store.db.execute(
            'SELECT o.plan,o.payload,p.strategy,p.account FROM portfolio_orders o '
            'JOIN portfolio_plans p ON p.id=o.plan WHERE o.id=?',
            (cancellation['client_order_id'],)).fetchone()
        if row is None:
            raise ValueError('cancellation has no owned durable intent')
        plan, payload, owner, account = row
        intent = decode(json.loads(payload))['record'].snapshot.intent
        if (owner != self.binding['instance_id'] or account != self.binding['account_id']
                or intent.instrument_id != cancellation['symbol']):
            raise ValueError('cancellation belongs to another instance or asset')
        self.executor.cancel(plan, cancellation['client_order_id'])
        child = next(child for child in store.orders(plan)
                     if child['record'].snapshot.intent.client_order_id == cancellation['client_order_id'])
        if child['uncertain'] or child['record'].snapshot.status not in TERMINAL:
            raise TimeoutError('instance cancellation awaits original-order reconciliation')

    def execute(self, receipt):
        binding = receipt['binding']
        validate_binding(binding)
        if binding != self.binding or receipt['event']['binding'] != binding:
            raise ValueError('receipt belongs to another instance')
        decision = read_decision(receipt['decision']).validate(self.spec)
        for cancellation in decision.cancellations:
            self._cancel(cancellation)
        event = receipt['event']
        positions = event['allocation']['positions']
        weights, deltas, direct = {}, None, None
        if decision.kind in {'target_weight', 'target_weights'}:
            weights = decision.targets
        elif decision.kind == 'quantity_deltas':
            deltas = decision.targets
        elif decision.kind == 'target_quantities':
            deltas = {symbol: quantity - D(str(positions.get(symbol, 0)))
                      for symbol, quantity in decision.targets.items()}
        else:
            direct = []
            for intent in decision.orders:
                price = intent.limit_price
                if price is None:
                    price = self.reservation_quote(receipt, intent.instrument_id)
                direct.append((intent, price))
        self.executor.create(account_id=binding['account_id'], strategy_id=binding['instance_id'],
            strategy_version=self.spec.version, trading_session=event['context']['execution_session'],
            rebalance_id=receipt['package_id'] + ':' + receipt['event_id'],
            weights=weights, owned_symbols=(tuple(intent.instrument_id for intent, _ in direct)
                if direct is not None else tuple(weights if deltas is None else deltas)), now=self.clock(),
            quantity_deltas=deltas, direct_orders=direct,
            signal_inputs=dict(package_id=receipt['package_id'], event_id=receipt['event_id'],
                               input_identity=receipt['input_identity']))
