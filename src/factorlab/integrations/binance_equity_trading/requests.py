"""Offline signatures and injected signed reads; no network transport is supplied."""
from dataclasses import dataclass, field
import hashlib
import hmac
import os
from urllib.parse import urlencode

from .protocol import Unavailable

PREFIX = "/sapi/v1/equity/"
READS = {"detail": "order/detail", "open": "order/open-orders", "orders": "order/history", "trades": "trade/history"}


@dataclass(frozen=True)
class SignedRequest:
    method: str
    path: str
    query: str = field(repr=False)
    headers: dict = field(repr=False)


@dataclass(frozen=True)
class Credentials:
    key: str = field(repr=False)
    secret: str = field(repr=False)

    @classmethod
    def from_environment(cls):
        key = os.environ.get("BINANCE_EQUITY_API_KEY", "")
        secret = os.environ.get("BINANCE_EQUITY_API_SECRET", "")
        if not key or not secret or not key.isascii() or not key.isalnum():
            raise Unavailable("LOCAL_STOCKS_CREDENTIALS_UNAVAILABLE")
        return cls(key, secret)

    def prepare_order(self, order, *, timestamp):
        """Sign an offline USD template, not evidence of USD payment eligibility."""
        return self.sign("POST", PREFIX + "order/place", order.params(), timestamp=timestamp)

    def prepare_cancel(self, order_id, *, timestamp):
        if not isinstance(order_id, str) or not order_id:
            raise ValueError("cancel requires exchange orderId")
        return self.sign("POST", PREFIX + "order/cancel", {"orderId": order_id}, timestamp=timestamp)

    def sign(self, method, path, params, *, timestamp, recv_window=5000):
        if path not in {PREFIX + p for p in (*READS.values(), "order/place", "order/cancel")}:
            raise ValueError("unsupported Stocks endpoint")
        expected = "GET" if path in {PREFIX + p for p in READS.values()} else "POST"
        if method != expected or type(timestamp) is not int or timestamp < 0:
            raise ValueError("invalid request method/timestamp")
        if type(recv_window) is not int or not 0 < recv_window <= 60000:
            raise ValueError("invalid recvWindow")
        if any(k in params for k in ("timestamp", "recvWindow", "signature")):
            raise ValueError("reserved signing fields")
        query = urlencode({**params, "timestamp": timestamp, "recvWindow": recv_window})
        signature = hmac.new(self.secret.encode(), query.encode(), hashlib.sha256).hexdigest()
        return SignedRequest(method, path, query + "&signature=" + signature, {"X-MBX-APIKEY": self.key})


class ReadClient:
    """The injected callable consumes a SignedRequest and returns a decoded payload.

    It must not log the query or headers. Transport and upstream exception messages
    never cross this boundary, because they may contain credentials/signatures.
    """

    def __init__(self, credentials, transport):
        self._credentials, self._transport = credentials, transport

    def read(self, operation, *, timestamp, **params):
        allowed = {"detail": {"orderId", "clientOrderId"}, "open": set(),
                   "orders": {"symbol", "side", "orderType", "orderStatus", "startTime", "endTime", "current", "size"},
                   "trades": {"symbol", "side", "orderId", "startTime", "endTime", "current", "size"}}
        if operation not in READS or set(params) - allowed[operation]:
            raise ValueError("unsupported read parameters")
        if operation == "detail" and len(params) != 1:
            raise ValueError("query requires one exchange or client identity")
        if operation in {"orders", "trades"}:
            start, end = params.get("startTime"), params.get("endTime")
            if type(start) is not int or type(end) is not int or not 0 <= start <= end:
                raise ValueError("history requires startTime/endTime milliseconds")
            if type(params.get("current", 1)) is not int or params.get("current", 1) < 1:
                raise ValueError("history current must be a positive page number")
            if type(params.get("size", 20)) is not int or not 1 <= params.get("size", 20) <= 100:
                raise ValueError("history size must be 1–100")
        request = self._credentials.sign("GET", PREFIX + READS[operation], params, timestamp=timestamp)
        try:
            result = self._transport(request)
        except PermissionError:
            raise Unavailable("STOCKS_PERMISSION_UNAVAILABLE") from None
        except TimeoutError:
            raise Unavailable("READ_TIMEOUT_RECONCILE_REQUIRED") from None
        except Exception:
            raise Unavailable("STOCKS_READ_FAILED") from None
        # Decoded objects, including upstream diagnostics, may echo authentication.
        text = repr(result)
        secrets = (self._credentials.key, self._credentials.secret, request.query.split("signature=")[-1])
        if any(value in text for value in secrets):
            raise Unavailable("AUTHENTICATION_MATERIAL_DISCARDED")
        if isinstance(result, dict) and isinstance(result.get("code"), int) and result["code"] < 0:
            raise Unavailable("STOCKS_API_ERROR_OR_PERMISSION_UNAVAILABLE")
        return result

    def read_account(self):
        # TODO: map cash, holdings, sellable quantity and settlement only after an official account endpoint is verified.
        raise Unavailable("OFFICIAL_STOCKS_ACCOUNT_READ_UNAVAILABLE")


def live_submit(*args, **kwargs):
    raise PermissionError("NO LIVE MONEY: stock submission is disabled; USD payment eligibility is unverified")


def live_cancel(*args, **kwargs):
    raise PermissionError("NO LIVE MONEY: stock cancellation is disabled")
