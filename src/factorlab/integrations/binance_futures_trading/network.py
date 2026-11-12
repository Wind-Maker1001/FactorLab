"""Fixed-host Demo network boundary; no CLI or persisted state grants capability."""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
import time
from urllib.error import HTTPError
from urllib.parse import parse_qs
from urllib.request import Request, build_opener

from .protocol import Environment, SafetyBlocked, READ_PATHS, WRITE_PATHS
from factorlab.integrations.binance.client import Response, _NoRedirect
from factorlab.trading.contracts import utc


@dataclass(frozen=True, kw_only=True)
class DemoExecutionPlan:
    account_id: str
    order_types: frozenset[str]
    max_quantity: Decimal
    max_notional: Decimal
    max_orders: int
    max_open_orders: int
    max_seconds: int
    max_loss: Decimal
    max_margin: Decimal
    expires_at: datetime
    client_id_prefix: str
    cleanup: str
    recovery: str
    environment: Environment = Environment.DEMO
    symbol: str = 'NVDAUSDT'

    def __post_init__(self):
        import re
        utc(self.expires_at)
        if self.environment is not Environment.DEMO or self.symbol != 'NVDAUSDT':
            raise ValueError('Demo NVDAUSDT plan required')
        if not self.account_id.startswith('DEMO:FactorLab-Demo:'):
            raise ValueError('Demo credential-bound account required')
        if type(self.order_types) is not frozenset or not self.order_types or not self.order_types <= {'MARKET', 'LIMIT'}:
            raise ValueError('explicit MARKET/LIMIT order types required')
        for value in (self.max_quantity, self.max_notional, self.max_loss, self.max_margin):
            if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
                raise ValueError('positive Decimal limits required')
        for value in (self.max_orders, self.max_open_orders, self.max_seconds):
            if type(value) is not int or value <= 0:
                raise ValueError('positive integer limits required')
        if self.max_open_orders > self.max_orders:
            raise ValueError('open-order bound exceeds total-order bound')
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,20}', self.client_id_prefix):
            raise ValueError('explicit unique client ID prefix required')
        if self.cleanup != 'CANCEL_KNOWN_THEN_STOP' or self.recovery != 'GET_RECONCILE_NO_RESUBMIT':
            raise ValueError('explicit cleanup and recovery policy required')


class DemoNetworkAuthorization:
    """Independent process-local approval; never restore this from durable data."""
    def __init__(self, plan):
        if type(plan) is not DemoExecutionPlan:
            raise ValueError('validated execution plan required')
        self._plan = plan
        self.started = time.monotonic()
        self.revoked = False

    @property
    def plan(self):
        return self._plan

    def __reduce__(self):
        raise TypeError('network authorization cannot be serialized')

    def active(self):
        return (not self.revoked and datetime.now(timezone.utc) < self.plan.expires_at
                and time.monotonic() - self.started < self.plan.max_seconds)

    def revoke(self):
        self.revoked = True


@dataclass(frozen=True, kw_only=True)
class DemoNetworkTransport:
    authorization: DemoNetworkAuthorization | None = field(default=None, repr=False)
    environment: Environment = field(default=Environment.DEMO, init=False)
    _admission: dict = field(default_factory=dict, init=False, repr=False, compare=False)

    def __post_init__(self):
        if self.authorization is not None and type(self.authorization) is not DemoNetworkAuthorization:
            raise SafetyBlocked('independent Demo network authorization required')

    @property
    def host(self):
        return 'https://demo-fapi.binance.com'

    def permitted(self):
        return type(self.authorization) is DemoNetworkAuthorization and self.authorization.active()

    def __call__(self, request, timeout):
        if request.environment is not Environment.DEMO:
            raise SafetyBlocked('Demo network environment mismatch')
        if request.headers:
            import hashlib
            key = request.headers.get('X-MBX-APIKEY', '')
            if self.authorization is not None:
                expected = 'DEMO:FactorLab-Demo:' + hashlib.sha256(key.encode()).hexdigest()
                if expected != self.authorization.plan.account_id:
                    raise SafetyBlocked('Demo credential account mismatch')
        if request.method == 'GET':
            if request.path == '/fapi/v1/order':
                params = parse_qs(request.query)
                if params.get('symbol') != ['NVDAUSDT'] or len(params.get('origClientOrderId', [])) != 1:
                    raise SafetyBlocked('recovery requires same Demo symbol and client ID')
            if request.path not in READ_PATHS:
                raise SafetyBlocked('read endpoint not allowlisted')
        else:
            admission = self._admission.pop('request', None)
            if not self.permitted() or (request.method, request.path) not in WRITE_PATHS:
                raise SafetyBlocked('Demo network writes disabled')
            if admission is None:
                raise SafetyBlocked('shared risk and durable order admission required')
            method, params, account_id = admission
            actual = parse_qs(request.query, keep_blank_values=True, strict_parsing=True)
            for key in ('timestamp', 'recvWindow', 'signature'):
                if len(actual.pop(key, ())) != 1:
                    raise SafetyBlocked('signed request required')
            if (method != request.method or account_id != self.authorization.plan.account_id
                    or actual != {k: [str(v)] for k, v in params.items()}):
                raise SafetyBlocked('approved order request mismatch')
        url = 'https://demo-fapi.binance.com' + request.path + ('?' + request.query if request.query else '')
        try:
            with build_opener(_NoRedirect()).open(
                    Request(url, headers=request.headers, method=request.method), timeout=timeout) as response:
                return Response(response.status, dict(response.headers), response.read(2 * 1024 * 1024 + 1))
        except HTTPError as error:
            return Response(error.code, dict(error.headers), error.read(2 * 1024 * 1024 + 1))
        except OSError:
            raise OSError('Demo request failed') from None


def demo_network_permitted(client):
    return (type(client.transport) is DemoNetworkTransport and client.environment is Environment.DEMO
            and client.credentials is not None and client.credentials.environment is Environment.DEMO
            and client.transport.permitted())
