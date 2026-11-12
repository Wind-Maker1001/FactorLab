"""Signed USD-M protocol over an injected transport; mutating network IO absent."""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from enum import Enum
import hashlib
import hmac
import json
import os
import time
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, build_opener

from factorlab.errors import DataQualityError
from factorlab.integrations.binance.client import Response, _NoRedirect

class Environment(Enum):
    PRODUCTION = ('https://fapi.binance.com', 'BINANCE_READONLY', 'FactorLab-ReadOnly')
    DEMO = ('https://demo-fapi.binance.com', 'BINANCE_DEMO', 'FactorLab-Demo')

    @property
    def host(self):
        return self.value[0]

    @property
    def credential_prefix(self):
        return self.value[1]

    @property
    def account_id(self):
        return self.value[2]


READ_PATHS = frozenset({
    '/fapi/v1/exchangeInfo', '/fapi/v3/balance', '/fapi/v1/time', '/fapi/v1/order', '/fapi/v1/openOrders', '/fapi/v1/allOrders',
    '/fapi/v3/account', '/fapi/v3/positionRisk', '/fapi/v1/positionSide/dual',
    '/fapi/v1/leverageBracket', '/fapi/v1/userTrades', '/fapi/v1/income', '/fapi/v1/symbolConfig',
    '/fapi/v1/apiTradingStatus', '/fapi/v1/premiumIndex',
})
WRITE_PATHS = frozenset({('POST', '/fapi/v1/order'), ('DELETE', '/fapi/v1/order')})


class SafetyBlocked(PermissionError):
    pass


class ExchangeError(DataQualityError):
    def __init__(self, code, status, category, retry_after=None):
        self.code, self.status, self.category, self.retry_after = code, status, category, retry_after
        # Never include server messages, URLs, signatures, keys or payloads.
        super().__init__(f'Binance error code={code} HTTP={status} category={category}')


class OutcomeUnknown(TimeoutError):
    pass


@dataclass(frozen=True, repr=False)
class Credentials:
    key: str = field(repr=False)
    secret: str = field(repr=False)
    environment: Environment | None = None

    def __post_init__(self):
        if not self.key or not self.secret:
            raise ValueError('both API key and secret required')

    @classmethod
    def from_env(cls, environment=None):
        if environment is None:
            return cls(os.environ.get('BINANCE_API_KEY', ''), os.environ.get('BINANCE_API_SECRET', ''))
        prefix = environment.credential_prefix
        return cls(os.environ.get(prefix + '_API_KEY', ''),
                   os.environ.get(prefix + '_API_SECRET', ''), environment)

    def __repr__(self):
        return 'Credentials(<redacted>)'


@dataclass(frozen=True, repr=False)
class SignedRequest:
    method: str
    path: str
    query: str = field(repr=False)
    headers: dict = field(repr=False)
    environment: Environment | None = None

    def __repr__(self):
        return f'SignedRequest({self.method}, {self.path}, <redacted>)'


class OfflineTransport:
    """Subclass for a deterministic in-process fake; no networking provided."""
    def __call__(self, request: SignedRequest, timeout: float) -> Response:
        raise NotImplementedError


@dataclass(frozen=True, kw_only=True)
class ReadOnlyNetworkTransport:
    """Explicit opt-in account GETs only, fixed host, no redirects or writes."""
    permit_account_read: bool = False
    environment: Environment = Environment.PRODUCTION

    def __call__(self, request, timeout):
        if not self.permit_account_read or request.method != 'GET' or request.path not in READ_PATHS:
            raise SafetyBlocked('NO LIVE MONEY: network access disabled or non-read request')
        if request.environment is not self.environment:
            raise SafetyBlocked('credential and transport environment mismatch')
        url = self.environment.host + request.path + ('?' + request.query if request.query else '')
        try:
            with build_opener(_NoRedirect()).open(Request(url, headers=request.headers, method='GET'), timeout=timeout) as r:
                return Response(r.status, dict(r.headers), r.read(2 * 1024 * 1024 + 1))
        except HTTPError as exc:
            return Response(exc.code, dict(exc.headers), exc.read(2 * 1024 * 1024 + 1))
        except OSError:
            raise OSError('account read transport failed') from None


class FuturesClient:
    """No automatic retry: callers reconcile writes and respect explicit cooldowns."""
    def __init__(self, credentials=None, *, transport=None, recv_window=5000,
                 timeout=10, clock=time.time, environment=None):
        if type(recv_window) is not int or not 1 <= recv_window <= 60000 or timeout <= 0:
            raise ValueError('invalid recvWindow or timeout')
        from .network import DemoNetworkTransport
        if transport is not None and not isinstance(transport, (OfflineTransport, ReadOnlyNetworkTransport, DemoNetworkTransport)):
            raise SafetyBlocked('transport must be offline fake or explicitly permitted read-only transport')
        self.environment = environment if environment is None else Environment(environment)
        if isinstance(transport, ReadOnlyNetworkTransport):
            if self.environment is None or transport.environment is not self.environment:
                raise SafetyBlocked('explicit matching network environment required')
            if credentials is not None and credentials.environment is not self.environment:
                raise SafetyBlocked('credential and client environment mismatch')
        if getattr(transport, 'environment', None) is Environment.DEMO:
            if self.environment is not Environment.DEMO or (credentials is not None and credentials.environment is not Environment.DEMO):
                raise SafetyBlocked('Demo transport requires matching Demo client and credentials')
        self.credentials, self.transport = credentials, transport
        self.recv_window, self.timeout, self.clock = recv_window, timeout, clock
        self.offset_ms, self.blocked_until = 0, 0.0

    def sync_time(self):
        before = self.clock()
        payload = self.request('GET', '/fapi/v1/time', signed=False)
        after = self.clock()
        if type(payload.get('serverTime')) is not int:
            raise DataQualityError('invalid server time response')
        self.offset_ms = payload['serverTime'] - int((before + after) * 500)
        return self.offset_ms

    def build(self, method, path, params=None, *, signed=True):
        if not ((method == 'GET' and path in READ_PATHS) or (method, path) in WRITE_PATHS):
            raise SafetyBlocked('endpoint is not allowlisted')
        params = dict(params or {})
        if {'signature', 'timestamp', 'recvWindow'} & params.keys():
            raise ValueError('authentication fields are owned by the signer')
        headers = {}
        if signed:
            if self.credentials is None:
                raise SafetyBlocked('credentials required for signed protocol')
            if getattr(self.transport, 'environment', None) is not None:
                if self.credentials.environment is not self.environment or self.transport.environment is not self.environment:
                    raise SafetyBlocked('credential and client environment mismatch')
            params.update(timestamp=int(self.clock() * 1000) + self.offset_ms, recvWindow=self.recv_window)
            headers['X-MBX-APIKEY'] = self.credentials.key
        query = urlencode(sorted(params.items()))
        if signed:
            signature = hmac.new(self.credentials.secret.encode(), query.encode(), hashlib.sha256).hexdigest()
            query += '&signature=' + signature
        return SignedRequest(method, path, query, headers, self.environment)

    def request(self, method, path, params=None, *, signed=True):
        if self.transport is None:
            raise SafetyBlocked('network disabled by default')
        if method != 'GET' and not isinstance(self.transport, OfflineTransport):
            from .network import demo_network_permitted
            if not demo_network_permitted(self):
                raise SafetyBlocked('NO LIVE MONEY: production or unauthorized network write forbidden')
        if isinstance(self.transport, ReadOnlyNetworkTransport) and not self.transport.permit_account_read:
            raise SafetyBlocked('explicit account read permission required')
        if self.clock() < self.blocked_until:
            raise ExchangeError(None, 429, 'RATE_LIMIT', self.blocked_until - self.clock())
        request = self.build(method, path, params, signed=signed)
        try:
            response = self.transport(request, self.timeout)
        except SafetyBlocked:
            raise
        except (OSError, TimeoutError):
            if method != 'GET':
                raise OutcomeUnknown('order outcome unknown; query by clientOrderId, never resend') from None
            raise DataQualityError('account read failed; reconciliation required') from None
        if not isinstance(response, Response) or len(response.body) > 2 * 1024 * 1024:
            if method != 'GET':
                raise OutcomeUnknown('unusable order response; reconcile')
            raise DataQualityError('invalid HTTP response')
        retry_after = None
        if response.status in {418, 429}:
            raw = next((v for k, v in response.headers.items() if k.lower() == 'retry-after'), '60')
            try:
                retry_after = max(0.0, float(raw))
            except ValueError:
                retry_after = max(0.0, parsedate_to_datetime(raw).timestamp() - self.clock())
            self.blocked_until = self.clock() + retry_after
        try:
            payload = json.loads(response.body)
        except (ValueError, UnicodeError):
            if method != 'GET':
                raise OutcomeUnknown('malformed order response; reconcile') from None
            raise DataQualityError('invalid response JSON') from None
        code = payload.get('code') if isinstance(payload, dict) else None
        if response.status == 408 or response.status >= 500 or code in {-1006, -1007, -4116}:
            if method != 'GET':
                raise OutcomeUnknown('exchange execution outcome unknown; reconcile')
        if response.status != 200 or (isinstance(code, int) and code < 0):
            category = ({-1021: 'CLOCK_SKEW', -1022: 'SIGNATURE', -2014: 'AUTH', -2015: 'AUTH',
                         -2019: 'INSUFFICIENT_MARGIN', -2027: 'LEVERAGE_LIMIT', -2022: 'REDUCE_ONLY',
                         -2011: 'ORDER_NOT_FOUND', -2013: 'ORDER_NOT_FOUND', -1013: 'FILTER',
                         -1111: 'PRECISION'}.get(code, 'EXCHANGE_REJECTED'))
            if response.status in {418, 429}:
                category = 'RATE_LIMIT'
            raise ExchangeError(code, response.status, category, retry_after)
        if not isinstance(payload, (dict, list)):
            if method != 'GET':
                raise OutcomeUnknown('unexpected order response; reconcile')
            raise DataQualityError('unexpected response shape')
        return payload
