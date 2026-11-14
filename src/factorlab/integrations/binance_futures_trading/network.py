"""Fixed-host Demo network boundary requiring a process-local capability."""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
import time
from typing import Callable
from urllib.error import HTTPError
from urllib.parse import parse_qs
from urllib.request import Request, build_opener

from .protocol import Environment, SafetyBlocked, SignedRequest, READ_PATHS, WRITE_PATHS, credential_account_id
from factorlab.integrations.binance.client import Response, _NoRedirect
from factorlab.trading.contracts import utc


class DemoWriteRefused(SafetyBlocked):
    """The durable guard refused admission before the transport attempted HTTP."""


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
    quote_policy: 'BoundedQuotePolicy | None' = None

    def __post_init__(self):
        import re
        if self.quote_policy is not None:
            from .bounded_quote import BoundedQuotePolicy
            if type(self.quote_policy) is not BoundedQuotePolicy:
                raise ValueError('validated bounded quote policy required')
            policy = self.quote_policy
            if self.max_orders != 1 or self.max_open_orders != 1:
                raise ValueError('bounded execution requires one order and one open order')
            if (self.order_types != frozenset({'LIMIT'}) or policy.account_id != self.account_id
                    or policy.quantity > self.max_quantity or policy.max_notional > self.max_notional
                    or policy.max_funds > self.max_margin or policy.expires_at > self.expires_at
                    or not policy.client_order_id.startswith(self.client_id_prefix)):
                raise ValueError('bounded quote exceeds execution plan')
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

    def to_json(self):
        from .bounded_quote import canonical_json
        return canonical_json(self)

    @classmethod
    def from_json(cls, text):
        import json
        from .bounded_quote import BoundedQuotePolicy
        values = json.loads(text)
        values['environment'] = Environment(tuple(values['environment']))
        values['order_types'] = frozenset(values['order_types'])
        for key in ('max_quantity', 'max_notional', 'max_loss', 'max_margin'):
            values[key] = Decimal(values[key])
        values['expires_at'] = datetime.fromisoformat(values['expires_at'])
        if values['quote_policy'] is not None:
            values['quote_policy'] = BoundedQuotePolicy.from_json(json.dumps(values['quote_policy']))
        return cls(**values)

    @property
    def digest(self):
        import hashlib
        return hashlib.sha256(self.to_json().encode()).hexdigest()


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
    write_guard: Callable[[str, dict, DemoExecutionPlan], None] | None = field(
        default=None, repr=False, compare=False)
    observe_response: Callable[[SignedRequest, Response | None], None] | None = field(
        default=None, repr=False, compare=False)
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
            key = request.headers.get('X-MBX-APIKEY', '')
            if self.authorization is not None:
                expected = credential_account_id(Environment.DEMO, key)
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
            policy = self.authorization.plan.quote_policy
            if policy is not None:
                if policy.expires_at <= datetime.now(timezone.utc):
                    raise SafetyBlocked('bounded policy expired at dispatch')
                identity_key = 'newClientOrderId' if request.method == 'POST' else 'origClientOrderId'
                if actual.get(identity_key) != [policy.client_order_id]:
                    raise SafetyBlocked('bounded exact dispatch identity mismatch')
            if request.method == 'POST' and policy is not None:
                guard = self._admission.pop('bounded_guard', None)
                if guard is None:
                    raise SafetyBlocked('bounded dispatch observation required')
                guard()
                if not self.permitted():
                    raise SafetyBlocked('Demo authorization expired at dispatch')
            if self.write_guard is not None:
                try:
                    self.write_guard(method, dict(params), self.authorization.plan)
                except SafetyBlocked as error:
                    raise DemoWriteRefused(str(error)) from None
                if not self.permitted():
                    raise DemoWriteRefused('Demo authorization expired at dispatch')
        url = 'https://demo-fapi.binance.com' + request.path + ('?' + request.query if request.query else '')
        response = None
        try:
            with build_opener(_NoRedirect()).open(
                    Request(url, headers=request.headers, method=request.method), timeout=timeout) as reply:
                response = Response(reply.status, dict(reply.headers), reply.read(2 * 1024 * 1024 + 1))
        except HTTPError as error:
            response = Response(error.code, dict(error.headers), error.read(2 * 1024 * 1024 + 1))
        except OSError:
            raise OSError('Demo request failed') from None
        finally:
            if self.observe_response is not None:
                self.observe_response(request, response)
        return response


def demo_network_permitted(client):
    return (type(client.transport) is DemoNetworkTransport and client.environment is Environment.DEMO
            and client.credentials is not None and client.credentials.environment is Environment.DEMO
            and client.transport.permitted())
