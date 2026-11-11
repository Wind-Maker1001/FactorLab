"""Confirmed-event order state machine; no assumed fills."""
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal

from .contracts import OrderEvent, OrderSnapshot, OrderStatus as S, ZERO


@dataclass(frozen=True)
class OrderRecord:
    snapshot: OrderSnapshot
    observed_at: datetime
    reservation_price: Decimal
    confirmed_status: S = S.NEW


def advance(record: OrderRecord, event: OrderEvent) -> OrderRecord:
    order = record.snapshot
    old = record.confirmed_status
    new = event.status
    allowed = {
        S.NEW: {S.SUBMITTED, S.REJECTED},
        S.SUBMITTED: {S.SUBMITTED, S.PARTIALLY_FILLED, S.FILLED, S.CANCELED, S.REJECTED},
        S.PARTIALLY_FILLED: {S.PARTIALLY_FILLED, S.FILLED, S.CANCELED},
        S.CANCELED: {S.CANCELED, S.FILLED},
        S.FILLED: {S.FILLED},
        S.REJECTED: {S.REJECTED},
    }
    if event.client_order_id != order.intent.client_order_id or new not in allowed[old]:
        raise ValueError("illegal order transition")
    if order.exchange_order_id is not None and event.exchange_order_id != order.exchange_order_id:
        raise ValueError("exchange order identity changed")
    if event.event_time < record.observed_at:
        raise ValueError("out-of-order confirmation requires reconciliation")
    filled = event.cumulative_filled_quantity
    delta = filled - order.cumulative_filled_quantity
    if delta < 0 or filled > order.intent.quantity:
        raise ValueError("cumulative fill regression or overfill")
    if (new == S.FILLED) != (filled == order.intent.quantity):
        raise ValueError("FILLED requires exactly the requested quantity")
    if new == S.PARTIALLY_FILLED and not ZERO < filled < order.intent.quantity:
        raise ValueError("PARTIALLY_FILLED requires a partial quantity")
    if new in {S.SUBMITTED, S.REJECTED} and filled != 0:
        raise ValueError("unfilled status contains fills")
    if delta > 0 and event.fill_price is None:
        raise ValueError("new fills require an incremental fill price")
    if event.cumulative_fee < order.cumulative_fee or (delta == 0 and event.cumulative_fee != order.cumulative_fee):
        raise ValueError("fee regression or fee without a fill")
    if old in {S.FILLED, S.REJECTED} and delta != 0:
        raise ValueError("terminal order changed")
    return replace(record, snapshot=replace(
        order, status=new, cumulative_filled_quantity=filled,
        cumulative_fee=event.cumulative_fee, exchange_order_id=event.exchange_order_id,
        reconcile_required=False,
    ), observed_at=event.event_time, confirmed_status=new)


def unknown(record: OrderRecord) -> OrderRecord:
    return replace(record, snapshot=replace(record.snapshot, status=S.UNKNOWN, reconcile_required=True))
