"""Exact Stocks request matrix and observation-based admission."""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import StrEnum
import re

from factorlab.integrations.binance_equity.client import symbol
from factorlab.integrations.binance_equity.market import decimal_text

D = Decimal


class Unavailable(ValueError):
    """A required permission, rule or authoritative observation is unavailable."""


class Session(StrEnum):
    REGULAR = "REGULAR"
    PRE_MARKET = "PRE_MARKET"
    POST_MARKET = "POST_MARKET"
    OVERNIGHT = "OVERNIGHT"
    CLOSED = "CLOSED"
    UNKNOWN = "UNKNOWN"


PHASES = {"O": Session.REGULAR, "PRE": Session.PRE_MARKET,
          "POST": Session.POST_MARKET, "ON": Session.OVERNIGHT, "C": Session.CLOSED}


def amount(value, *, positive=False):
    number = D(decimal_text(str(value) if isinstance(value, D) else value))
    if positive and number == 0:
        raise ValueError("positive decimal required")
    return number


def fresh(event_time, received_at, now, max_age):
    """Both observation times must be UTC; a missing source time is rejected."""
    for t in (received_at, now):
        if not isinstance(t, datetime) or t.utcoffset() != timedelta(0):
            raise ValueError("UTC observation required")
    if max_age <= timedelta(0):
        raise ValueError("positive observation age required")
    if event_time is None:
        raise Unavailable("MISSING_SOURCE_TIME")
    if type(event_time) is not int or event_time < 0:
        raise ValueError("millisecond source timestamp required")
    source = datetime.fromtimestamp(event_time / 1000, timezone.utc)
    if not source <= received_at <= now or now - source > max_age or now - received_at > max_age:
        raise Unavailable("STALE_OR_FUTURE_OBSERVATION")


@dataclass(frozen=True)
class Rules:
    symbol: str
    tradability: str
    step: Decimal
    min_qty: Decimal
    max_qty: Decimal
    min_notional: Decimal
    max_notional: Decimal
    fractionable: bool
    fractionable_eh: bool
    extended: bool
    overnight: bool
    max_orders: int
    multiplier_down: Decimal
    multiplier_up: Decimal

    @classmethod
    def from_exchange_info(cls, row):
        required = {'symbol', 'tradability', 'fractionable', 'fractionableEh', 'extendedSession',
                    'overnightSupported', 'stepSize', 'minQty', 'maxQty', 'minNotional',
                    'maxNotional', 'maxNumOrders', 'multiplierDown', 'multiplierUp'}
        missing = sorted(k for k in required if row.get(k) is None)
        if missing:
            raise Unavailable('INCOMPLETE_SYMBOL_RULES:' + ','.join(missing))
        flags = [row[k] for k in ("fractionable", "fractionableEh", "extendedSession", "overnightSupported")]
        if any(type(f) is not bool for f in flags):
            raise Unavailable("INCOMPLETE_SYMBOL_RULES")
        values = [amount(row[k], positive=True) for k in
                  ("stepSize", "minQty", "maxQty", "minNotional", "maxNotional")]
        if values[1] > values[2] or values[3] > values[4]:
            raise ValueError("inverted rule bounds")
        count = row["maxNumOrders"]
        if type(count) is not int or count <= 0:
            raise Unavailable("OPEN_ORDER_LIMIT_UNAVAILABLE")
        down, up = (amount(row[k], positive=True) for k in ("multiplierDown", "multiplierUp"))
        if down > up:
            raise ValueError("inverted price bounds")
        return cls(symbol(row["symbol"]), row["tradability"], *values, *flags, count, down, up)


@dataclass(frozen=True)
class StockOrder:
    symbol: str
    side: str
    order_type: str
    client_order_id: str
    quantity: Decimal | None = None
    notional: Decimal | None = None
    price: Decimal | None = None
    trading_session: str | None = None
    time_in_force: str = "DAY"

    def params(self):
        symbol(self.symbol)
        for value in (self.quantity, self.notional, self.price):
            if value is not None and (not isinstance(value, D) or not value.is_finite() or value <= 0):
                raise ValueError("order amounts require finite positive Decimal")
        if not re.fullmatch(r"[a-zA-Z0-9_-]{32,36}", self.client_order_id):
            raise ValueError("Stocks clientOrderId requires 32–36 ASCII identifier characters")
        if self.side not in {"BUY", "SELL"} or self.order_type not in {"MARKET", "LIMIT"}:
            raise ValueError("unsupported stock order")
        if self.time_in_force not in {"DAY", "GTC"}:
            raise ValueError("unsupported timeInForce")
        # TODO: resolve SDK-default USDC versus server-allowed payment assets before live admission.
        p = dict(symbol=self.symbol, side=self.side, orderType=self.order_type,
                 clientOrderId=self.client_order_id, quoteAsset="USD", tokenize="false",
                 timeInForce=self.time_in_force)
        if self.order_type == "MARKET":
            if self.price is not None or self.trading_session is not None or self.time_in_force != "DAY":
                raise ValueError("MARKET forbids price/tradingSession/GTC")
            if self.side == "BUY":
                if self.quantity is not None or self.notional is None:
                    raise ValueError("MARKET BUY requires notional and forbids quantity")
                p["notional"] = format(amount(str(self.notional), positive=True), "f")
            else:
                if self.notional is not None or self.quantity is None:
                    raise ValueError("MARKET SELL requires quantity and forbids notional")
                p["quantity"] = format(amount(str(self.quantity), positive=True), "f")
        else:
            if self.notional is not None or self.price is None or self.quantity is None:
                raise ValueError("LIMIT requires price/quantity and forbids notional")
            if self.trading_session not in {"RTH", "EXTENDED", "24H"}:
                raise ValueError("LIMIT requires tradingSession")
            price = amount(str(self.price), positive=True)
            if price % D("0.01"):
                raise ValueError("stock price exceeds two decimal places")
            qty = amount(str(self.quantity), positive=True)
            if qty % 1 and self.time_in_force == "GTC" and self.trading_session == "RTH":
                raise ValueError("fractional GTC requires EXTENDED or 24H")
            p.update(price=format(price, "f"), quantity=format(qty, "f"), tradingSession=self.trading_session)
        return p


def admit(order, rules, phase, *, price, reference_price, open_orders=0, permission=False):
    """Only fresh caller-supplied authoritative observations may reach this rule gate."""
    order.params()
    if not permission:
        raise Unavailable("STOCKS_PERMISSION_UNAVAILABLE")
    if order.symbol != rules.symbol or rules.tradability not in {"BUY_SELL", order.side}:
        raise Unavailable("SYMBOL_NOT_TRADABLE")
    if phase in {Session.UNKNOWN, Session.CLOSED} or not isinstance(phase, Session):
        raise Unavailable("SESSION_NOT_TRADABLE")
    if open_orders >= rules.max_orders:
        raise Unavailable("OPEN_ORDER_LIMIT")
    if order.order_type == "MARKET" and phase != Session.REGULAR:
        # TODO: enable other market sessions only with verified execution eligibility rules.
        raise Unavailable("MARKET_SESSION_RULE_UNVERIFIED")
    allowed = {"RTH": {Session.REGULAR}, "EXTENDED": {Session.REGULAR, Session.PRE_MARKET, Session.POST_MARKET},
               "24H": {Session.REGULAR, Session.PRE_MARKET, Session.POST_MARKET, Session.OVERNIGHT}}
    if order.order_type == "LIMIT" and phase not in allowed[order.trading_session]:
        raise Unavailable("ORDER_SESSION_MISMATCH")
    if phase in {Session.PRE_MARKET, Session.POST_MARKET} and not rules.extended:
        raise Unavailable("EXTENDED_NOT_SUPPORTED")
    if phase == Session.OVERNIGHT and not rules.overnight:
        raise Unavailable("OVERNIGHT_NOT_SUPPORTED")
    price = amount(str(price), positive=True)
    reference_price = amount(str(reference_price), positive=True)
    qty = order.quantity
    fractional = order.notional is not None or qty % 1 != 0
    if fractional and not (rules.fractionable if phase == Session.REGULAR else rules.fractionable_eh):
        raise Unavailable("FRACTIONAL_NOT_SUPPORTED")
    if phase == Session.OVERNIGHT and fractional:
        # TODO: verify overnight fractional eligibility independently of fractionableEh.
        raise Unavailable("OVERNIGHT_FRACTIONAL_RULE_UNVERIFIED")
    if qty is not None and (not rules.min_qty <= qty <= rules.max_qty or qty % rules.step):
        raise ValueError("quantity outside exchange bounds/step")
    notional = order.notional if order.notional is not None else qty * (order.price or price)
    if not rules.min_notional <= notional <= rules.max_notional:
        raise ValueError("notional outside exchange bounds")
    if order.price is not None and not reference_price * rules.multiplier_down <= order.price <= reference_price * rules.multiplier_up:
        raise ValueError("limit price outside exchange multipliers")
    return order.params()
