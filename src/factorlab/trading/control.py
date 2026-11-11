"""Fail-closed risk admission and atomic paper execution/recovery."""
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from .contracts import Market, RiskDecision, Side, TERMINAL, ZERO, decimal_value, utc
from .paper import PaperBroker
from .state import unknown
from .storage import PaperStore


@dataclass(frozen=True)
class RiskLimits:
    # Amount caps apply independently per explicit quote currency; no FX netting.
    max_order_notional: Decimal = ZERO
    max_instrument_exposure: Decimal = ZERO
    account_budget: Decimal = ZERO
    orders_per_minute: int = 0
    max_quote_age_seconds: int = 5

    def __post_init__(self):
        for name in ("max_order_notional", "max_instrument_exposure", "account_budget"):
            decimal_value(getattr(self, name), name)
        if type(self.orders_per_minute) is not int or self.orders_per_minute < 0:
            raise ValueError("orders_per_minute must be a nonnegative integer")
        if type(self.max_quote_age_seconds) is not int or self.max_quote_age_seconds <= 0:
            raise ValueError("max_quote_age_seconds must be a positive integer")


class RiskRejected(ValueError):
    def __init__(self, decision):
        self.decision = decision
        super().__init__(decision.reason)


class TradingController:
    """Only the injected paper broker is executable; TradingAdapter is a contract.

    TimeoutError represents an uncertain submit/cancel outcome, not a rejection.
    A broker subclass may inject those outcomes for deterministic testing.
    """
    def __init__(self, broker: PaperBroker, store: PaperStore, *, limits=RiskLimits(),
                 clock=lambda: datetime.now(timezone.utc), live=False):
        if live or not isinstance(broker, PaperBroker):
            raise ValueError("NO LIVE MONEY: only PaperBroker execution is enabled")
        self.broker, self.store, self.limits, self.clock = broker, store, limits, clock
        utc(clock())
        broker.clock, broker.record_event = clock, store.record_event
        with store.transaction():
            restored = store.load()
            if restored is not None:
                expected = broker.state
                if (restored.market, restored.account_id, restored.instruments, restored.fee_rate) != (
                    expected.market, expected.account_id, expected.instruments, expected.fee_rate
                ):
                    raise ValueError("persisted paper account configuration mismatch")
                broker.state = restored
                # Every restart requires explicit account/order reconciliation.
                broker.state.reconciled = False
                for key, record in tuple(broker.state.orders.items()):
                    if record.snapshot.status not in TERMINAL:
                        broker.state.orders[key] = unknown(record)
            store.save(broker.state)

    def _atomic(self, operation):
        before = deepcopy(self.broker.state)
        try:
            with self.store.transaction():
                # Read inside the write transaction so independent controller
                # instances cannot overwrite a newer account projection.
                self.broker.state = self.store.load()
                result = operation()
                self.store.save(self.broker.state)
            return result
        except BaseException:
            self.broker.state = before
            raise

    def quote(self, quote):
        return self._atomic(lambda: self.broker.set_quote(quote))

    def set_kill_switch(self, enabled=True):
        if type(enabled) is not bool:
            raise ValueError("kill switch requires boolean")
        return self._atomic(lambda: setattr(self.broker.state, "kill_switch", enabled))

    def assess(self, intent):
        b, state, now = self.broker, self.broker.state, self.clock()
        utc(now)
        limits = {k: str(v) if isinstance(v, Decimal) else v for k, v in asdict(self.limits).items()}
        limits["kill_switch"] = state.kill_switch

        def result(reason):
            return RiskDecision(reason == "APPROVED", reason, limits)

        if intent.market != state.market or intent.instrument_id not in state.instruments:
            return result("ACCOUNT_OR_INSTRUMENT_MISMATCH")
        if not state.reconciled or any(r.snapshot.reconcile_required for r in state.orders.values()):
            return result("RECONCILE_REQUIRED")
        reduction = b.is_reduction(intent)
        if intent.reduce_only and not reduction:
            return result("INVALID_REDUCE_ONLY")
        if state.kill_switch and not (intent.reduce_only and reduction):
            return result("KILL_SWITCH")
        quote = state.quotes.get(intent.instrument_id)
        if quote is None or not timedelta(0) <= now - quote.observed_at <= timedelta(seconds=self.limits.max_quote_age_seconds):
            return result("STALE_QUOTE")
        recent = [t for t in state.submissions if now - t < timedelta(minutes=1)]
        if len(recent) >= self.limits.orders_per_minute and not (intent.reduce_only and reduction):
            return result("ORDER_RATE_LIMIT")
        price = max(quote.price, intent.limit_price or quote.price)
        amount = intent.quantity * price
        currency = state.instruments[intent.instrument_id].quote_currency
        account = b.read_account()
        asset = next(a for a in account.assets if a.currency == currency)
        if state.market == Market.BINANCE_EQUITY and intent.side == Side.SELL and not reduction:
            return result("INSUFFICIENT_INVENTORY")
        required = ZERO if reduction else amount * (1 + state.fee_rate)
        if required > asset.available:
            return result("INSUFFICIENT_BALANCE")
        if not (intent.reduce_only and reduction):
            relevant = {p.instrument_id for p in account.positions}
            relevant.update(o.intent.instrument_id for o in account.open_orders)
            if any(not timedelta(0) <= now - state.quotes[symbol].observed_at <= timedelta(seconds=self.limits.max_quote_age_seconds)
                   for symbol in relevant):
                return result("STALE_QUOTE")
            if amount > self.limits.max_order_notional:
                return result("ORDER_NOTIONAL_LIMIT")
            # Gross position + all outstanding orders: opposing orders never net
            # away risk before a confirmed execution.
            exposures = {p.instrument_id: abs(p.quantity) * p.mark_price for p in account.positions}
            for record in state.orders.values():
                o = record.snapshot
                if o.status not in TERMINAL:
                    symbol = o.intent.instrument_id
                    mark = state.quotes[symbol].price
                    pending = (o.intent.quantity - o.cumulative_filled_quantity) * max(mark, record.reservation_price)
                    exposures[symbol] = exposures.get(symbol, ZERO) + pending
            if exposures.get(intent.instrument_id, ZERO) + amount > self.limits.max_instrument_exposure:
                return result("INSTRUMENT_EXPOSURE_LIMIT")
            budget = sum((v for symbol, v in exposures.items() if state.instruments[symbol].quote_currency == currency), ZERO)
            if budget + amount > self.limits.account_budget:
                return result("ACCOUNT_BUDGET_LIMIT")
        return result("APPROVED")

    def submit(self, intent):
        def operation():
            old = self.broker.state.orders.get(intent.client_order_id)
            if old is not None:
                if old.snapshot.intent != intent:
                    raise ValueError("client_order_id reused with different intent")
                if old.snapshot.reconcile_required:
                    raise ValueError("RECONCILE_REQUIRED: duplicate submission forbidden")
                return self.broker.query_order(intent.client_order_id)
            decision = self.assess(intent)
            if not decision.approved:
                raise RiskRejected(decision)
            self.broker.state.submissions = [t for t in self.broker.state.submissions
                                             if self.clock() - t < timedelta(minutes=1)]
            self.broker.state.submissions.append(self.clock())
            try:
                return self.broker.submit_order(intent)
            except TimeoutError:
                # Injected paper transport can accept then lose its reply.
                # The paper authority remains durable; never resubmit blindly.
                if intent.client_order_id not in self.broker.state.orders:
                    from .contracts import OrderSnapshot
                    from .state import OrderRecord
                    quote = self.broker.state.quotes[intent.instrument_id]
                    self.broker.state.orders[intent.client_order_id] = OrderRecord(
                        OrderSnapshot(intent), self.clock(), max(quote.price, intent.limit_price or quote.price))
                self.broker.mark_unknown(intent.client_order_id)
                return None
        return self._atomic(operation)

    def cancel(self, order_id):
        def operation():
            order = self.broker.state.orders[order_id].snapshot
            if order.status in TERMINAL:
                return self.broker.query_order(order_id)
            if order.reconcile_required:
                raise ValueError("RECONCILE_REQUIRED")
            try:
                return self.broker.cancel_order(order_id)
            except TimeoutError:
                self.broker.mark_unknown(order_id)
                return None
        return self._atomic(operation)

    def fill(self, order_id, quantity, price=None):
        return self._atomic(lambda: self.broker.fill(order_id, quantity, price))

    def reject(self, order_id):
        return self._atomic(lambda: self.broker.reject(order_id))

    def replay(self, event):
        return self._atomic(lambda: self.broker.apply(event))

    def mark_timeout(self, order_id):
        return self._atomic(lambda: self.broker.mark_unknown(order_id))

    def reconcile(self):
        def operation():
            # No local confirmation means the outcome is still unknown. Do not
            # turn an unconfirmed submission into permission for a replacement.
            if any(key not in self.broker.state.confirmations for key in self.broker.state.orders):
                self.broker.state.reconciled = False
                return self.broker.read_account()
            return self.broker.reconcile()
        return self._atomic(operation)

    def account(self):
        return self.broker.read_account()
