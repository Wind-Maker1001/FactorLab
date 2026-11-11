"""Authoritative cumulative executions, retaining unknown fees and settlement."""
from dataclasses import dataclass
from decimal import Decimal

from .protocol import Unavailable, amount

STATUS = {"NEW": "SUBMITTED", "ACCEPTED": "SUBMITTED", "PENDING_NEW": "SUBMITTED",
          "PARTIALLY_FILLED": "PARTIALLY_FILLED", "FILLED": "FILLED",
          "CANCELED": "CANCELED", "CANCELLED": "CANCELED", "REJECTED": "REJECTED"}


@dataclass(frozen=True)
class Report:
    order_id: str
    status: str
    filled: Decimal
    total: Decimal
    fee: Decimal | None
    updated_at: int
    symbol: str
    side: str
    currency: str
    client_order_id: str | None = None
    settlement: str | None = None

    @classmethod
    def detail(cls, row):
        return cls(row["orderId"], STATUS.get(row["status"].upper(), "UNKNOWN"),
                   amount(row["filledQty"]), amount(row["filledTotal"]),
                   None if row.get("fee") is None else amount(row["fee"]), row["updatedAt"],
                   row["symbol"], row["side"], row["quote"], row.get("clientOrderId"))

    @classmethod
    def stream(cls, row):
        if row["e"] != "orderReport" or not row["b"].startswith("EQ_"):
            raise ValueError("not an underlying equity order event")
        # No fee field is supplied by orderReport. tc is a buy cost, not a sell fee.
        return cls(row["i"], STATUS.get(row["s"].upper(), "UNKNOWN"),
                   amount(row["fq"]), amount(row["FN"]), None, row["U"],
                   row["b"][3:], row["S"].upper(), row["q"])

    @property
    def identity(self):
        return self.order_id, self.updated_at, self.status, self.filled, self.total, self.fee


def incremental(previous, report):
    """Return delta shares, execution price and fee; never price a delta at VWAP."""
    if report.status == "UNKNOWN":
        raise Unavailable("UNKNOWN_ORDER_RECONCILE_REQUIRED")
    if type(report.updated_at) is not int or report.updated_at < 0:
        raise ValueError("invalid report timestamp")
    if previous is not None:
        if (previous.order_id, previous.symbol, previous.side, previous.currency) != (
                report.order_id, report.symbol, report.side, report.currency):
            raise Unavailable("ORDER_IDENTITY_CONFLICT")
        if previous.client_order_id and report.client_order_id and previous.client_order_id != report.client_order_id:
            raise Unavailable("CLIENT_IDENTITY_CONFLICT")
        if report.updated_at < previous.updated_at:
            raise Unavailable("REPORT_REGRESSION")
        transitions = {"SUBMITTED": {"SUBMITTED", "PARTIALLY_FILLED", "FILLED", "CANCELED", "REJECTED"},
                       "PARTIALLY_FILLED": {"PARTIALLY_FILLED", "FILLED", "CANCELED"},
                       "CANCELED": {"CANCELED", "FILLED"}, "FILLED": {"FILLED"}, "REJECTED": {"REJECTED"}}
        if report.status not in transitions.get(previous.status, set()):
            raise Unavailable("ORDER_STATE_CONFLICT")
    old_qty = previous.filled if previous else Decimal(0)
    old_total = previous.total if previous else Decimal(0)
    qty, total = report.filled - old_qty, report.total - old_total
    if qty < 0 or total < 0 or (qty == 0) != (total == 0):
        raise Unavailable("CUMULATIVE_EXECUTION_CONFLICT")
    if report.status in {"SUBMITTED", "REJECTED"} and report.filled:
        raise Unavailable("UNFILLED_STATUS_CONFLICT")
    if report.status in {"FILLED", "PARTIALLY_FILLED"} and not report.filled:
        raise Unavailable("FILLED_STATUS_WITHOUT_EXECUTION")
    if previous and previous.status in {"FILLED", "REJECTED"} and qty:
        raise Unavailable("TERMINAL_EXECUTION_CONFLICT")
    fee = None
    if report.fee is not None and (previous is None or previous.fee is not None):
        fee = report.fee - (previous.fee if previous else Decimal(0))
        if fee < 0 or (qty == 0 and fee != 0):
            raise Unavailable("CUMULATIVE_FEE_CONFLICT")
    return qty, total / qty if qty else None, fee


def contract_event(intent, report, previous=None):
    """Export a verified share-based report without changing v1 quantity semantics."""
    from datetime import datetime, timezone
    import hashlib
    from factorlab.trading.contracts import OrderEvent, OrderStatus

    delta, price, fee = incremental(previous, report)
    if report.currency != "USD" or report.symbol != intent.instrument_id or report.side != intent.side.value:
        raise Unavailable("CONTRACT_IDENTITY_MISMATCH")
    if report.client_order_id is not None and report.client_order_id != intent.client_order_id:
        raise Unavailable("CLIENT_IDENTITY_CONFLICT")
    if report.fee is None or fee is None:
        raise Unavailable("CONFIRMED_FEE_REQUIRED")
    if report.filled > intent.quantity or (report.status == "FILLED" and report.filled != intent.quantity):
        # TODO: support variable-share notional fills after a compatible shared contract exists.
        raise Unavailable("NOTIONAL_FILL_INCOMPATIBLE_WITH_FIXED_SHARE_CONTRACT")
    identity = hashlib.sha256(repr(report.identity).encode()).hexdigest()
    return OrderEvent(intent.client_order_id, report.order_id, OrderStatus(report.status),
                      report.filled, price, datetime.fromtimestamp(report.updated_at / 1000, timezone.utc),
                      "binance:stocks:order/detail", identity, report.fee)


def trades(payload):
    """Trade history has no fee or settlement field; do not infer either."""
    return tuple(dict(execution_id=r["executionId"], order_id=r["orderId"], symbol=r["symbol"],
                      currency=r["quote"], side=r["side"], quantity=amount(r["qty"]),
                      price=amount(r["price"]), notional=amount(r["total"]),
                      event_time=r["executionAt"], fee=None, settlement=None)
                 for r in payload["rows"])
