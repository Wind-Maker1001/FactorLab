"""Thin P7-T01 integration. Requires factorlab.trading v1, never a private ledger."""
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from factorlab.trading.contracts import Market, OrderType, Side, TERMINAL
from factorlab.trading.paper import PaperBroker, Quote

from .protocol import PHASES, Session, StockOrder, Unavailable, admit, fresh


@dataclass(frozen=True)
class Observations:
    rules: object
    phase: Session
    price: Decimal
    reference_price: Decimal
    quote_time: int | None
    received_at: datetime
    phase_time: int | None
    phase_received_at: datetime
    rules_time: int | None
    rules_received_at: datetime
    permission: bool = False

    def validate(self, now):
        fresh(self.quote_time, self.received_at, now, timedelta(seconds=5))
        fresh(self.phase_time, self.phase_received_at, now, timedelta(hours=12))
        fresh(self.rules_time, self.rules_received_at, now, timedelta(hours=24))

    @classmethod
    def from_equity(cls, quote, session, rules, *, rules_time, rules_received_at, permission=False, reference_price, side="BUY"):
        """Consume binance_equity.market normalized rows, not perpetual quotes."""
        if (quote["asset_class"], quote["currency"], quote["symbol"]) != ("STOCK_SPOT", "USD", rules.symbol):
            raise ValueError("quote source identity mismatch")
        if quote["availability"] != "AVAILABLE":
            raise Unavailable("QUOTE_UNAVAILABLE")
        def received(value):
            return datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
        bid, ask = Decimal(quote["bid_price"]), Decimal(quote["ask_price"])
        if bid <= 0 or ask < bid:
            raise Unavailable("INVALID_QUOTE")
        if side not in {"BUY", "SELL"}:
            raise ValueError("invalid stock side")
        return cls(rules, Session(session["session"]), ask if side == "BUY" else bid, reference_price,
                   quote["event_time"], received(quote["acquired_at"]),
                   session["event_time"], received(session["acquired_at"]),
                   rules_time, rules_received_at, permission)


class StockPaperBroker(PaperBroker):
    """Only explicit synthetic USD snapshots execute here.

    MARKET BUY is filled at its admitted fixed simulation price. This preserves
    both requested notional and v1's fixed-share intent; it is not a claim about
    exchange execution or settlement. No real account or event is booked here.
    """
    def __init__(self, account_id, instruments, balances, **kwargs):
        if any(i.quote_currency != "USD" for i in instruments) or set(balances) != {"USD"}:
            raise ValueError("stock paper account requires explicit USD")
        super().__init__(Market.BINANCE_EQUITY, account_id, instruments, balances, **kwargs)
        self.observations = {}
        self.parameters = {}

    def bind(self, intent, observation, *, notional=None, trading_session=None):
        if intent.market != Market.BINANCE_EQUITY or intent.reduce_only or intent.position_side is not None:
            raise ValueError("stock intent identity mismatch")
        observation.validate(self.clock())
        market_buy = intent.order_type == OrderType.MARKET and intent.side == Side.BUY
        if market_buy and notional != intent.quantity * observation.price:
            raise ValueError("explicit notional must match fixed-price simulated shares")
        order = StockOrder(intent.instrument_id, intent.side.value, intent.order_type.value,
                           intent.client_order_id, None if market_buy else intent.quantity,
                           notional, intent.limit_price, trading_session,
                           "DAY" if intent.order_type == OrderType.MARKET else intent.time_in_force)
        params = order.params()
        old = self.parameters.get(intent.client_order_id)
        if old is not None and old != params:
            raise ValueError("clientOrderId reused with different stock parameters")
        existing = self.state.orders.get(intent.client_order_id)
        if existing is not None and old is None:
            raise Unavailable("RESTORED_ORDER_QUERY_ONLY_NO_PROTOCOL_CONTEXT")
        if existing is not None and existing.snapshot.intent != intent:
            raise ValueError("clientOrderId reused with different intent")
        self.parameters[intent.client_order_id] = params
        self.observations[intent.client_order_id] = observation, order
        return params

    def submit_order(self, intent):
        observation, order = self.observations[intent.client_order_id]
        observation.validate(self.clock())
        opened = sum(r.snapshot.intent.instrument_id == intent.instrument_id
                     and r.snapshot.status not in TERMINAL for r in self.state.orders.values())
        admit(order, observation.rules, observation.phase, price=observation.price,
              reference_price=observation.reference_price, open_orders=opened, permission=observation.permission)
        quote = self.state.quotes[intent.instrument_id]
        if quote.price != observation.price or quote.observed_at != datetime.fromtimestamp(observation.quote_time / 1000, timezone.utc):
            raise ValueError("risk quote differs from admitted source observation")
        return super().submit_order(intent)

    def fill(self, order_id, quantity, price=None):
        if order_id not in self.observations:
            raise Unavailable("RESTORED_ORDER_QUERY_ONLY_NO_PROTOCOL_CONTEXT")
        observation, order = self.observations[order_id]
        observation.validate(self.clock())
        admit(order, observation.rules, observation.phase, price=observation.price,
              reference_price=observation.reference_price, permission=observation.permission)
        actual = self.state.quotes[order.symbol].price if price is None else price
        if order.notional is not None and actual != observation.price:
            raise Unavailable("NOTIONAL_SIMULATION_REQUIRES_FIXED_PRICE")
        return super().fill(order_id, quantity, price)

    def synthetic_holdings(self):
        """Paper inventory availability is not a real settlement assertion."""
        account = self.read_account()
        return tuple(dict(symbol=p.instrument_id, quantity=p.quantity,
                          sellable=p.quantity - sum((o.intent.quantity - o.cumulative_filled_quantity
                              for o in account.open_orders if o.intent.instrument_id == p.instrument_id
                              and o.intent.side == Side.SELL), Decimal(0)), settlement=None)
                     for p in account.positions)


class StockAdapter:
    """TradingAdapter v1 façade over an injected paper controller."""
    def __init__(self, controller):
        if not isinstance(controller.broker, StockPaperBroker):
            raise ValueError("NO LIVE MONEY: StockPaperBroker required")
        self.controller = controller

    def submit_order(self, intent):
        return self.controller.submit(intent)

    def cancel_order(self, client_order_id):
        return self.controller.cancel(client_order_id)

    def query_order(self, client_order_id):
        return self.controller.broker.query_order(client_order_id)

    def read_account(self):
        return self.controller.account()

    def reconcile(self, expected):
        """Compare a separately supplied paper authority snapshot before unfreezing."""
        actual = self.read_account()
        def projection(account):
            return (account.market, account.account_id, account.assets, account.positions,
                    tuple((o.intent, o.cumulative_filled_quantity, o.cumulative_fee, o.exchange_order_id)
                          for o in account.open_orders))
        confirmations = self.controller.broker.state.confirmations
        consistent_orders = all(o.intent.client_order_id in confirmations
            and confirmations[o.intent.client_order_id].status == o.status for o in expected.open_orders)
        if projection(actual) != projection(expected) or not consistent_orders:
            for key, record in tuple(self.controller.broker.state.orders.items()):
                if record.snapshot.status not in TERMINAL:
                    self.controller.mark_timeout(key)
            self.controller.set_kill_switch()
            raise Unavailable("CASH_HOLDINGS_OR_ORDERS_CONFLICT")
        return self.controller.reconcile()

    def disconnect(self):
        for key, record in tuple(self.controller.broker.state.orders.items()):
            if record.snapshot.status not in TERMINAL:
                self.controller.mark_timeout(key)
        self.controller.set_kill_switch()
