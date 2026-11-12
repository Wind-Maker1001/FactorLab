"""Bridge shared paper risk/accounting to the offline exchange protocol."""
from dataclasses import replace
from decimal import Decimal
from datetime import timedelta

from factorlab.trading import Market, OrderEvent, OrderSnapshot, OrderStatus as S
from factorlab.trading.storage import decode, dumps
import json
import hashlib
from factorlab.trading.control import RiskRejected
from factorlab.trading.paper import PaperBroker
from factorlab.trading.state import OrderRecord

from .protocol import SafetyBlocked, OfflineTransport

ZERO = Decimal('0')


class FuturesPaperGateway(PaperBroker):
    """Inject into TradingController; requires a separate adapter metadata store.

    Shared risk runs before this broker's submit; observed exchange rules and
    authenticated available margin are then checked before fake POST dispatch.
    """
    def __init__(self, adapter, rules, instruments, balances, *, leverage=1, max_leverage=1,
                 fee_rate=Decimal('0.001')):
        if not isinstance(adapter.client.transport, OfflineTransport):
            from .network import demo_network_permitted
            if not demo_network_permitted(adapter.client):
                raise SafetyBlocked('NO LIVE MONEY: gateway requires offline or authorized Demo transport')
        super().__init__(Market.BINANCE_USDM, adapter.account_id, instruments, balances, fee_rate=fee_rate)
        self.adapter, self.rules = adapter, rules
        self.leverage, self.max_leverage = leverage, max_leverage

    def submit_order(self, intent):
        if not self.adapter.reconciled or self.adapter.facts is None:
            raise SafetyBlocked('futures account must be reconciled')
        remote_ids = {row[0] for row in self.adapter.db.execute('SELECT id FROM futures_orders')}
        if remote_ids != set(self.state.orders):
            raise SafetyBlocked('RECONCILE_REQUIRED: adapter/paper order identities differ')
        facts = self.adapter.facts
        if not timedelta(0) <= self.clock() - facts.observed_at <= timedelta(seconds=5):
            raise SafetyBlocked('STALE_ACCOUNT: reconcile required')
        if facts.hedge_mode is not False:
            raise SafetyBlocked('shared v1 supports only confirmed one-way mode')
        config = next((c for c in facts.symbol_config if c.get('symbol') == intent.instrument_id), {})
        if config.get('leverage') != self.leverage or config.get('marginType') not in {'CROSSED', 'ISOLATED'}:
            raise SafetyBlocked('UNKNOWN or mismatched leverage/margin mode')
        available = facts.usdt()['availableBalance']
        positions = facts.position_details()
        notionals = [p['notional'] for p in positions if p['symbol'] == intent.instrument_id]
        current = None if any(n is None for n in notionals) else sum((abs(n) for n in notionals), ZERO)
        local_position = self.state.positions.get(intent.instrument_id)
        if current is not None and local_position is not None:
            current = max(current, abs(local_position.quantity) * self.state.quotes[intent.instrument_id].price)
        pending = sum(((r.snapshot.intent.quantity - r.snapshot.cumulative_filled_quantity) * r.reservation_price
                       for r in self.state.orders.values()
                       if r.snapshot.intent.instrument_id == intent.instrument_id
                       and r.snapshot.status not in {S.FILLED, S.CANCELED, S.REJECTED}), ZERO)
        # Use both projections conservatively; unexecuted fake orders cannot
        # spend the same reported available margin twice.
        paper_available = next(a.available for a in self.read_account().assets if a.currency == 'USDT')
        margin = None if available is None else min(available, paper_available)
        decision = self.rules.validate(intent, mark_price=self.state.quotes[intent.instrument_id].price,
            leverage=self.leverage, available_margin=margin,
            current_notional=None if current is None else current + pending,
            now=self.clock(), max_leverage=self.max_leverage)
        if not decision.approved:
            raise RiskRejected(decision)
        event = self.adapter.submit_order(intent)
        quote = self.state.quotes[intent.instrument_id].price
        self.state.orders[intent.client_order_id] = OrderRecord(OrderSnapshot(intent), event.event_time, max(quote, intent.limit_price or quote))
        self._confirm(event)
        return event

    def _confirm(self, event):
        record = self.state.orders[event.client_order_id]
        if record.confirmed_status == S.NEW and event.status not in {S.SUBMITTED, S.REJECTED}:
            self.apply(replace(event, event_id=event.event_id + ':accept', status=S.SUBMITTED,
                               cumulative_filled_quantity=ZERO, cumulative_fee=ZERO, fill_price=None))
        self.apply(event)

    def cancel_order(self, client_order_id):
        event = self.adapter.cancel_order(client_order_id)
        self._confirm(event)
        return event

    def query_order(self, client_order_id):
        # Controller's duplicate-intent path must not replay already booked fills.
        return self.state.confirmations[client_order_id]

    def recover(self, controller):
        """Replay only REST-confirmed missing deltas after a disconnected/restarted run.

        No submission/replacement occurs. Adapter's cumulative quote and actual
        trades must have reconciled first. The shared journal supplies the
        already-booked notional, rather than estimating from position cost.
        """
        self.adapter.reconcile()
        remote_ids = {row[0] for row in self.adapter.db.execute('SELECT id FROM futures_orders')}
        if set(self.state.orders) - remote_ids:
            raise SafetyBlocked('RECONCILE_REQUIRED: paper order lacks protocol identity')
        missing = remote_ids - set(self.state.orders)
        if missing:
            def import_confirmed_intents():
                for key in missing:
                    remote, _ = self.adapter._load(key)
                    intent = remote.snapshot.intent
                    quote = self.state.quotes[intent.instrument_id].price
                    self.state.orders[key] = OrderRecord(OrderSnapshot(intent), remote.observed_at,
                                                         max(quote, intent.limit_price or quote))
                self.state.reconciled = False
            # Import already-sent, REST-confirmed identities only, not submit.
            controller._atomic(import_confirmed_intents)
        booked = {}
        for raw, in controller.store.db.execute('SELECT payload FROM events ORDER BY rowid'):
            event = decode(json.loads(raw))
            qty, quote = booked.get(event.client_order_id, (ZERO, ZERO))
            delta = event.cumulative_filled_quantity - qty
            if delta > 0:
                quote += delta * event.fill_price
                qty = event.cumulative_filled_quantity
            booked[event.client_order_id] = qty, quote
        for key in tuple(self.state.orders):
            remote, remote_quote = self.adapter._load(key)
            local = self.state.orders[key]
            qty, quote = booked.get(key, (ZERO, ZERO))
            delta = remote.snapshot.cumulative_filled_quantity - qty
            if delta < 0 or remote_quote < quote:
                raise ValueError('paper/adapter cumulative execution mismatch')
            fingerprint = hashlib.sha256(dumps([remote, remote_quote]).encode()).hexdigest()
            event = OrderEvent(key, remote.snapshot.exchange_order_id, remote.snapshot.status,
                remote.snapshot.cumulative_filled_quantity, (remote_quote - quote) / delta if delta else None,
                remote.observed_at, 'binance-rest-recovery', 'recovery:' + fingerprint,
                remote.snapshot.cumulative_fee)
            if local.confirmed_status == S.NEW:
                # The timeout placeholder has a local timestamp, not a confirmed
                # exchange time; server clock offset must not reject recovery.
                controller._atomic(lambda: self.state.orders.__setitem__(key, replace(
                    self.state.orders[key], observed_at=event.event_time)))
            if local.confirmed_status == S.NEW and event.status not in {S.SUBMITTED, S.REJECTED}:
                controller.replay(replace(event, event_id=event.event_id + ':accept', status=S.SUBMITTED,
                                         cumulative_filled_quantity=ZERO, cumulative_fee=ZERO, fill_price=None))
            existing = controller.store.db.execute('SELECT payload FROM events WHERE id=?', (event.event_id,)).fetchone()
            controller.replay(decode(json.loads(existing[0])) if existing is not None else event)
        return controller.reconcile()

    def reconcile(self):
        exchange = self.adapter.reconcile()
        remote_ids = {row[0] for row in self.adapter.db.execute('SELECT id FROM futures_orders')}
        if remote_ids != set(self.state.orders):
            self.state.reconciled = False
            return self.read_account()
        for key, record in tuple(self.state.orders.items()):
            remote = self.adapter.order(key)
            if (remote.cumulative_filled_quantity, remote.cumulative_fee) != (
                record.snapshot.cumulative_filled_quantity, record.snapshot.cumulative_fee
            ):
                # Cannot reconstruct missing execution prices by assuming a fill.
                self.state.reconciled = False
                return self.read_account()
        local = super().reconcile()
        remote_positions = {p.instrument_id: (p.quantity, p.entry_price) for p in exchange.positions}
        local_positions = {p.instrument_id: (p.quantity, p.entry_price) for p in local.positions}
        remote_assets = {a.currency: a.total for a in exchange.assets}
        local_assets = {a.currency: a.total for a in local.assets}
        if remote_positions != local_positions or remote_assets != local_assets:
            self.state.reconciled = False
        return self.read_account()
