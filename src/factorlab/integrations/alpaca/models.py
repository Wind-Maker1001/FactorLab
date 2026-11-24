"""Alpaca account facts and cash-only order validation."""
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
import re

from factorlab.trading.contracts import Market

D = Decimal
ZERO = D("0")


def instant(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


@dataclass(frozen=True)
class AlpacaAccount:
    account_id: str
    equity: Decimal
    cash: Decimal
    buying_power: Decimal
    status: str
    trading_blocked: bool
    account_blocked: bool
    observed_at: datetime

    @classmethod
    def from_payload(cls, row):
        if row["currency"] != "USD":
            raise ValueError("Alpaca account must use USD")
        return cls(row["id"], D(row["equity"]), D(row["cash"]), D(row["buying_power"]),
                   row["status"], row["trading_blocked"], row["account_blocked"],
                   datetime.now(timezone.utc))


def order_parameters(intent, asset):
    if intent.market != Market.ALPACA_US_EQUITY:
        raise ValueError("Alpaca market identity required")
    if not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,14}", intent.instrument_id):
        raise ValueError("invalid equity symbol")
    if not re.fullmatch(r"[A-Za-z0-9_\-]{1,48}", intent.client_order_id):
        raise ValueError("invalid Alpaca client_order_id")
    if asset["symbol"] != intent.instrument_id or asset["class"] != "us_equity":
        raise ValueError("asset identity mismatch")
    if asset["status"] != "active" or not asset["tradable"]:
        raise ValueError("asset is not tradable")
    if intent.reduce_only or intent.position_side is not None:
        raise ValueError("Alpaca spot orders do not accept derivatives flags")
    fractional = intent.quantity != intent.quantity.to_integral_value()
    if intent.quantity.as_tuple().exponent < -9:
        raise ValueError("quantity supports at most nine decimal places")
    if fractional and (not asset["fractionable"] or intent.time_in_force != "DAY"):
        raise ValueError("fractional equity orders require a fractionable asset and DAY")
    if intent.limit_price is not None:
        tick = D("0.01") if intent.limit_price >= 1 else D("0.0001")
        if intent.limit_price % tick:
            raise ValueError("limit price violates Alpaca tick size")
    return {"symbol": intent.instrument_id, "qty": str(intent.quantity),
            "side": intent.side.value.lower(), "type": intent.order_type.value.lower(),
            "time_in_force": intent.time_in_force.lower(), "client_order_id": intent.client_order_id,
            **({"limit_price": str(intent.limit_price)} if intent.limit_price is not None else {})}
