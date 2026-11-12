"""Small, fully collateralized paper account. No sockets, credentials or live mode."""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from decimal import Decimal
from typing import Callable

from .contracts import (
    AccountSnapshot, AssetBalance, Market, OrderEvent, OrderIntent, OrderSnapshot,
    OrderStatus as S, OrderType, Position, Side, TERMINAL, ZERO, decimal_value, utc,
)
from .state import OrderRecord, advance, unknown


@dataclass(frozen=True)
class Instrument:
    instrument_id: str
    quote_currency: str

    def __post_init__(self):
        if not self.instrument_id or self.quote_currency not in {"USD", "USDT"}:
            raise ValueError("instrument requires an explicit USD or USDT quote currency")


@dataclass(frozen=True)
class Quote:
    instrument_id: str
    price: Decimal
    observed_at: datetime

    def __post_init__(self):
        decimal_value(self.price, "price", positive=True)
        utc(self.observed_at)


@dataclass
class PaperState:
    market: Market
    account_id: str
    instruments: dict[str, Instrument]
    cash: dict[str, Decimal]
    fee_rate: Decimal
    orders: dict[str, OrderRecord] = field(default_factory=dict)
    positions: dict[str, Position] = field(default_factory=dict)
    quotes: dict[str, Quote] = field(default_factory=dict)
    confirmations: dict[str, OrderEvent] = field(default_factory=dict)
    submissions: list[datetime] = field(default_factory=list)
    reconciled: bool = True
    kill_switch: bool = False
    sequence: int = 0
    runtime: dict = field(default_factory=dict)


class PaperBroker:
    """Inject into TradingController; the controller owns transactions.

    Equity uses cash purchases and owned-inventory sales. USD-M uses linear,
    one-way positions with 100% entry-notional collateral, no leverage,
    liquidation, funding, settlement conversion or exchange fee claims.
    """
    def __init__(self, market: Market, account_id: str, instruments: tuple[Instrument, ...],
                 balances: dict[str, Decimal], *, fee_rate: Decimal = Decimal("0.001")):
        if not isinstance(market, Market) or not account_id or not instruments:
            raise ValueError("market, account and instruments required")
        if len({i.instrument_id for i in instruments}) != len(instruments):
            raise ValueError("duplicate instrument")
        decimal_value(fee_rate, "fee_rate")
        if fee_rate >= 1:
            raise ValueError("fee_rate must be less than one")
        for currency, balance in balances.items():
            if currency not in {"USD", "USDT"}:
                raise ValueError("unsupported currency")
            decimal_value(balance, "balance")
        if any(i.quote_currency not in balances for i in instruments):
            raise ValueError("each instrument needs its own quote-currency balance")
        self.state = PaperState(market, account_id, {i.instrument_id: i for i in instruments}, dict(balances), fee_rate)
        self.clock: Callable[[], datetime]
        self.record_event: Callable[[OrderEvent], bool]

    def set_quote(self, quote: Quote) -> None:
        if quote.instrument_id not in self.state.instruments:
            raise ValueError("unknown instrument")
        utc(self.clock())
        if quote.observed_at > self.clock():
            raise ValueError("future quote")
        previous = self.state.quotes.get(quote.instrument_id)
        if previous is not None and quote.observed_at < previous.observed_at:
            raise ValueError("quote time regression")
        self.state.quotes[quote.instrument_id] = quote
        position = self.state.positions.get(quote.instrument_id)
        if position is not None:
            self.state.positions[quote.instrument_id] = replace(position, mark_price=quote.price)

    def _event(self, order_id: str, status: S, filled: Decimal, price: Decimal | None, fee: Decimal) -> OrderEvent:
        self.state.sequence += 1
        return OrderEvent(order_id, f"paper:{order_id}", status, filled, price,
                          self.clock(), "paper", f"paper:{self.state.sequence}", fee)

    def apply(self, event: OrderEvent) -> bool:
        """Replay a confirmed paper report atomically with the account projection."""
        if not self.record_event(event):
            return False
        record = self.state.orders[event.client_order_id]
        updated = advance(record, event)
        intent = record.snapshot.intent
        delta = event.cumulative_filled_quantity - record.snapshot.cumulative_filled_quantity
        fee = event.cumulative_fee - record.snapshot.cumulative_fee
        if delta:
            price = event.fill_price
            if fee != delta * price * self.state.fee_rate:
                raise ValueError("paper fee does not match incremental fill")
            if intent.order_type == OrderType.LIMIT and (
                (intent.side == Side.BUY and price > intent.limit_price)
                or (intent.side == Side.SELL and price < intent.limit_price)
            ):
                raise ValueError("fill violates limit price")
            self._account_fill(intent, delta, price, fee)
        self.state.orders[event.client_order_id] = updated
        self.state.confirmations[event.client_order_id] = event
        if any(asset.available < 0 for asset in self.read_account().assets):
            raise ValueError("insufficient collateral at simulated fill price")
        return True

    def _account_fill(self, intent: OrderIntent, quantity: Decimal, price: Decimal, fee: Decimal) -> None:
        symbol = intent.instrument_id
        currency = self.state.instruments[symbol].quote_currency
        old = self.state.positions.get(symbol, Position(symbol, ZERO, ZERO, price, currency))
        signed = quantity if intent.side == Side.BUY else -quantity
        new_quantity = old.quantity + signed
        if intent.reduce_only and (old.quantity * signed >= 0 or abs(signed) > abs(old.quantity)):
            raise ValueError("reduce-only fill would increase risk")
        if self.state.market == Market.BINANCE_EQUITY:
            if new_quantity < 0:
                raise ValueError("paper equity does not support short sales")
            self.state.cash[currency] -= signed * price + fee
        else:
            realized = ZERO
            if old.quantity * signed < 0:
                closed = min(abs(old.quantity), abs(signed))
                realized = closed * (price - old.entry_price) * (1 if old.quantity > 0 else -1)
            self.state.cash[currency] += realized - fee
        if not new_quantity:
            self.state.positions.pop(symbol, None)
            return
        if old.quantity * signed >= 0:
            entry = (abs(old.quantity) * old.entry_price + quantity * price) / abs(new_quantity)
        elif old.quantity * new_quantity > 0:
            entry = old.entry_price
        else:
            entry = price
        mark = self.state.quotes[symbol].price
        self.state.positions[symbol] = Position(symbol, new_quantity, entry, mark, currency)

    def submit_order(self, intent: OrderIntent) -> OrderEvent:
        price = self.state.quotes[intent.instrument_id].price
        reservation = max(price, intent.limit_price or price)
        self.state.orders[intent.client_order_id] = OrderRecord(OrderSnapshot(intent), self.clock(), reservation)
        event = self._event(intent.client_order_id, S.SUBMITTED, ZERO, None, ZERO)
        self.apply(event)
        return event

    def fill(self, order_id: str, quantity: Decimal, price: Decimal | None = None) -> OrderEvent:
        decimal_value(quantity, "fill quantity", positive=True)
        order = self.state.orders[order_id].snapshot
        if order.status in TERMINAL or order.reconcile_required:
            raise ValueError("order is not executable")
        price = self.state.quotes[order.intent.instrument_id].price if price is None else price
        decimal_value(price, "fill price", positive=True)
        total = order.cumulative_filled_quantity + quantity
        status = S.FILLED if total == order.intent.quantity else S.PARTIALLY_FILLED
        event = self._event(order_id, status, total, price,
                            order.cumulative_fee + quantity * price * self.state.fee_rate)
        self.apply(event)
        return event

    def reject(self, order_id: str) -> OrderEvent:
        order = self.state.orders[order_id].snapshot
        event = self._event(order_id, S.REJECTED, order.cumulative_filled_quantity, None, order.cumulative_fee)
        self.apply(event)
        return event

    def cancel_order(self, client_order_id: str) -> OrderEvent:
        order = self.state.orders[client_order_id].snapshot
        if order.status in TERMINAL:
            return self.query_order(client_order_id)
        if order.reconcile_required:
            raise ValueError("RECONCILE_REQUIRED")
        event = self._event(client_order_id, S.CANCELED, order.cumulative_filled_quantity, None, order.cumulative_fee)
        self.apply(event)
        return event

    def query_order(self, client_order_id: str) -> OrderEvent:
        return self.state.confirmations[client_order_id]

    def mark_unknown(self, order_id: str) -> None:
        self.state.orders[order_id] = unknown(self.state.orders[order_id])
        self.state.reconciled = False

    def read_account(self, *, exclude_order_id=None) -> AccountSnapshot:
        """Project collateral; fill admission may release its own pending reservation."""
        reserved = {currency: ZERO for currency in self.state.cash}
        if self.state.market == Market.BINANCE_USDM:
            for position in self.state.positions.values():
                reserved[position.quote_currency] += abs(position.quantity) * position.entry_price
        closable = {symbol: abs(p.quantity) for symbol, p in self.state.positions.items()}
        for record in self.state.orders.values():
            order = record.snapshot
            if order.status in TERMINAL or order.intent.client_order_id == exclude_order_id:
                continue
            intent = order.intent
            symbol = intent.instrument_id
            currency = self.state.instruments[symbol].quote_currency
            remaining = intent.quantity - order.cumulative_filled_quantity
            position = self.state.positions.get(symbol)
            opposite = position is not None and ((intent.side == Side.BUY) != (position.quantity > 0))
            closing = min(remaining, closable[symbol]) if opposite else ZERO
            if closing:
                closable[symbol] -= closing
            # Owned quantity may collateralize only one outstanding close.
            # Closing fees are funded by released collateral or sale proceeds.
            opening = remaining - closing
            reserved[currency] += opening * record.reservation_price * (1 + self.state.fee_rate)
        return AccountSnapshot(self.state.market, self.state.account_id,
            tuple(AssetBalance(c, total, reserved[c]) for c, total in sorted(self.state.cash.items())),
            tuple(self.state.positions[k] for k in sorted(self.state.positions)),
            tuple(r.snapshot for r in self.state.orders.values() if r.snapshot.status not in TERMINAL
                  and r.snapshot.intent.client_order_id != exclude_order_id),
            self.clock(), self.state.reconciled)

    def is_reduction(self, intent: OrderIntent, *, include_pending: bool = True) -> bool:
        position = self.state.positions.get(intent.instrument_id)
        signed = intent.quantity if intent.side == Side.BUY else -intent.quantity
        if position is None or position.quantity * signed >= 0:
            return False
        pending = ZERO
        if include_pending:
            pending = sum((r.snapshot.intent.quantity - r.snapshot.cumulative_filled_quantity
                for r in self.state.orders.values()
                if r.snapshot.status not in TERMINAL
                and r.snapshot.intent.instrument_id == intent.instrument_id
                and r.snapshot.intent.side == intent.side), ZERO)
        return intent.quantity <= abs(position.quantity) - pending

    def reconcile(self) -> AccountSnapshot:
        """Local paper authority only; never infer an exchange order is absent."""
        for order_id, record in tuple(self.state.orders.items()):
            confirmation = self.query_order(order_id)
            # The paper account projection and this confirmation commit together.
            # A query confirms status without replaying its already booked fill.
            self.state.orders[order_id] = replace(record, snapshot=replace(
                record.snapshot, status=confirmation.status, reconcile_required=False))
        self.state.reconciled = True
        account = self.read_account()
        if any(a.available < 0 for a in account.assets):
            self.state.reconciled = False
            return self.read_account()
        return account
