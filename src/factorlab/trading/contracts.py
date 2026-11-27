"""Version 1 trading boundary. Decimal amounts; UTC observations; no network IO."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Protocol

CONTRACT_VERSION = 1
SAFETY_LABELS = ("NO LIVE MONEY", "PIT_UNVERIFIED", "HFQ_UNVERIFIED", "FORMAL_ADMISSION_BLOCKED")
ZERO = Decimal("0")


class Market(StrEnum):
    BINANCE_USDM = "BINANCE_USDM"
    BINANCE_EQUITY = "BINANCE_EQUITY"
    ALPACA_US_EQUITY = "ALPACA_US_EQUITY"


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(StrEnum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"


class OrderStatus(StrEnum):
    NEW = "NEW"
    SUBMITTED = "SUBMITTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"


TERMINAL = frozenset({OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.REJECTED})


def decimal_value(value: Decimal, name: str, *, positive: bool = False) -> None:
    if not isinstance(value, Decimal) or not value.is_finite() or value < 0 or (positive and value == 0):
        raise ValueError(f"{name} must be a finite {'positive' if positive else 'nonnegative'} Decimal")


def signed_decimal_value(value: Decimal, name: str) -> None:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError(f"{name} must be a finite Decimal")


def funding_event_id(income_type: str, tran_id: str) -> str:
    if not isinstance(income_type, str) or not income_type.strip():
        raise ValueError("income_type must be a nonempty string")
    if not isinstance(tran_id, str) or not tran_id.strip():
        raise ValueError("tran_id must be a nonempty string")
    return f"funding:{income_type}:{tran_id}"


def utc(value: datetime) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("timestamps must be timezone-aware UTC")


@dataclass(frozen=True)
class OrderIntent:
    market: Market
    instrument_id: str
    side: Side
    order_type: OrderType
    quantity: Decimal
    limit_price: Decimal | None
    client_order_id: str
    time_in_force: str = "GTC"
    position_side: str | None = None
    reduce_only: bool = False

    def __post_init__(self):
        if not isinstance(self.market, Market) or not isinstance(self.side, Side) or not isinstance(self.order_type, OrderType):
            raise ValueError("market, side and order_type require contract enums")
        if not self.instrument_id or not self.client_order_id or len(self.client_order_id) > 128:
            raise ValueError("nonempty instrument and bounded client_order_id required")
        decimal_value(self.quantity, "quantity", positive=True)
        if self.limit_price is not None:
            decimal_value(self.limit_price, "limit_price", positive=True)
        if (self.order_type == OrderType.LIMIT) != (self.limit_price is not None):
            raise ValueError("only LIMIT orders require limit_price")
        allowed_tif = {"DAY", "GTC", "IOC", "FOK", "OPG", "CLS"} if self.market == Market.ALPACA_US_EQUITY else {"GTC"}
        if self.time_in_force not in allowed_tif or self.position_side not in (None, "BOTH"):
            raise ValueError("unsupported time in force or position side")
        if type(self.reduce_only) is not bool:
            raise ValueError("reduce_only must be boolean")


@dataclass(frozen=True)
class OrderEvent:
    """fill_price prices the delta since the previous cumulative quantity.

    event_id is unique within an account. cumulative_fee is in the instrument's
    explicit quote currency. source identifies the confirming producer.
    """
    client_order_id: str
    exchange_order_id: str | None
    status: OrderStatus
    cumulative_filled_quantity: Decimal
    fill_price: Decimal | None
    event_time: datetime
    source: str
    event_id: str
    cumulative_fee: Decimal = ZERO
    cumulative_filled_notional: Decimal | None = None

    def __post_init__(self):
        utc(self.event_time)
        decimal_value(self.cumulative_filled_quantity, "cumulative_filled_quantity")
        decimal_value(self.cumulative_fee, "cumulative_fee")
        if self.cumulative_filled_notional is not None:
            decimal_value(self.cumulative_filled_notional, "cumulative_filled_notional")
        if self.fill_price is not None:
            decimal_value(self.fill_price, "fill_price", positive=True)
        if not isinstance(self.status, OrderStatus) or not self.client_order_id or not self.source or not self.event_id:
            raise ValueError("status, order identity, source and event_id required")


@dataclass(frozen=True)
class FundingEvent:
    """Exchange-confirmed funding settlement; never an order fill or trade fee.

    event_id is the stable exchange event identity within an account.
    amount is signed in the explicit settlement asset (negative = paid, positive = received).
    """
    account_id: str
    tran_id: str
    symbol: str
    asset: str
    income_type: str
    amount: Decimal
    event_time: datetime
    source: str
    event_id: str
    metadata: dict[str, str | int | bool | None] = field(default_factory=dict)

    def __post_init__(self):
        utc(self.event_time)
        signed_decimal_value(self.amount, "amount")
        for name in ("account_id", "tran_id", "symbol", "asset", "income_type", "source", "event_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip() or len(value) > 256:
                raise ValueError(f"{name} must be a bounded nonempty string")
        if self.income_type != "FUNDING_FEE":
            raise ValueError("FundingEvent supports FUNDING_FEE income_type only")
        if not isinstance(self.metadata, dict):
            raise ValueError("metadata must be a dict")
        for key, value in self.metadata.items():
            if not isinstance(key, str) or not key or not isinstance(value, (str, int, bool, type(None))):
                raise ValueError("metadata requires string keys and scalar values")


def funding_identity_key(event: FundingEvent):
    return (
        event.account_id,
        event.tran_id,
        event.symbol,
        event.asset,
        event.income_type,
        event.amount,
        event.event_time,
    )


@dataclass(frozen=True)
class AssetBalance:
    currency: str
    total: Decimal
    reserved: Decimal = ZERO

    @property
    def available(self) -> Decimal:
        return self.total - self.reserved


@dataclass(frozen=True)
class Position:
    instrument_id: str
    quantity: Decimal
    entry_price: Decimal
    mark_price: Decimal
    quote_currency: str
    available_quantity: Decimal | None = None


@dataclass(frozen=True)
class OrderSnapshot:
    intent: OrderIntent
    status: OrderStatus = OrderStatus.NEW
    cumulative_filled_quantity: Decimal = ZERO
    cumulative_fee: Decimal = ZERO
    exchange_order_id: str | None = None
    reconcile_required: bool = False


@dataclass(frozen=True)
class AccountSnapshot:
    market: Market
    account_id: str
    assets: tuple[AssetBalance, ...]
    positions: tuple[Position, ...]
    open_orders: tuple[OrderSnapshot, ...]
    observed_at: datetime
    reconciled: bool = False
    official_equity: Decimal | None = None
    official_cash: Decimal | None = None
    official_available: Decimal | None = None
    initial_margin: Decimal | None = None
    unrealized_pnl: Decimal | None = None
    cumulative_external_cash_flow: Decimal | None = None
    official_order_ids: tuple[str, ...] | None = None
    official_observed_at: datetime | None = None
    official_positions: tuple[Position, ...] | None = None

    def __post_init__(self):
        utc(self.observed_at)


@dataclass(frozen=True)
class RiskDecision:
    approved: bool
    reason: str
    limits_snapshot: dict[str, str | int | bool] = field(default_factory=dict)


class TradingAdapter(Protocol):
    """No network implementation or live authorization is supplied by v1."""

    def submit_order(self, intent: OrderIntent) -> OrderEvent: ...
    def cancel_order(self, client_order_id: str) -> OrderEvent: ...
    def query_order(self, client_order_id: str) -> OrderEvent: ...
    def read_account(self) -> AccountSnapshot: ...
