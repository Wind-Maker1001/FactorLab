"""Demo execution contract over offline injection only; no network writer exists."""
from dataclasses import dataclass, field

from .adapter import FuturesAdapter
from .gateway import FuturesPaperGateway
from .protocol import Environment, OfflineTransport, SafetyBlocked, WRITE_PATHS, READ_PATHS
from .rules import RuleObservation


class DemoExecutionAuthorization:
    """Process-local opt-in for injected Demo writes, never network permission."""
    def __reduce__(self):
        raise TypeError('execution authorization cannot be serialized')


@dataclass(frozen=True, kw_only=True)
class DemoExecutionTransport(OfflineTransport):
    """No sockets, URLs or redirects: the delegate must be an offline transport."""
    delegate: OfflineTransport = field(repr=False)
    authorization: DemoExecutionAuthorization | None = field(default=None, repr=False)
    environment: Environment = field(default=Environment.DEMO, init=False)

    def __post_init__(self):
        if not isinstance(self.delegate, OfflineTransport):
            raise SafetyBlocked('Demo execution requires offline injection')
        if self.authorization is not None and type(self.authorization) is not DemoExecutionAuthorization:
            raise SafetyBlocked('process-local Demo authorization required')

    @property
    def host(self):
        return Environment.DEMO.host

    def __call__(self, request, timeout):
        if request.environment is not Environment.DEMO:
            raise SafetyBlocked('Demo execution cannot use production requests')
        if request.method == 'GET' and request.path in READ_PATHS:
            return self.delegate(request, timeout)
        if (request.method, request.path) not in WRITE_PATHS:
            raise SafetyBlocked('Demo endpoint not allowlisted')
        if type(self.authorization) is not DemoExecutionAuthorization:
            raise SafetyBlocked('Demo injected writes disabled by default')
        return self.delegate(request, timeout)


@dataclass(frozen=True)
class DemoRuleObservation:
    """Rules and account identity acquired together from the Demo client."""
    rules: RuleObservation
    account_id: str


def observe_demo_rules(adapter, symbol='NVDAUSDT', *, source_version):
    client = adapter.client
    if client.environment is not Environment.DEMO:
        raise SafetyBlocked('Demo rules require Demo client')
    if symbol != 'NVDAUSDT':
        raise SafetyBlocked('Demo execution contract currently supports NVDAUSDT only')
    info = client.request('GET', '/fapi/v1/exchangeInfo', signed=False)
    symbol_info = next(row for row in info['symbols'] if row['symbol'] == symbol)
    brackets = client.request('GET', '/fapi/v1/leverageBracket', {'symbol': symbol})
    bracket = next((row for row in brackets if row['symbol'] == symbol), None)
    rules = RuleObservation.from_exchange(symbol_info, bracket, adapter.clock(), source_version)
    return DemoRuleObservation(rules, adapter.account_id)


class DemoFuturesAdapter(FuturesAdapter):
    """Reuse persisted identities/state machine; dispatch only injected Demo IO."""
    def __init__(self, client, store, **kwargs):
        if (client.environment is not Environment.DEMO
                or not isinstance(client.transport, DemoExecutionTransport)
                or client.credentials is None
                or client.credentials.environment is not Environment.DEMO):
            raise SafetyBlocked('isolated Demo credentials and injected execution transport required')
        super().__init__(client, store, account_id=Environment.DEMO.account_id, **kwargs)
        self._admitted_intent = None

    def submit_order(self, intent):
        if intent is not self._admitted_intent:
            raise SafetyBlocked('Demo submission requires shared controller and rule admission')
        return super().submit_order(intent)

    def cancel_order(self, client_order_id):
        if type(self.client.transport.authorization) is not DemoExecutionAuthorization:
            raise SafetyBlocked('Demo injected writes disabled by default')
        return super().cancel_order(client_order_id)


class DemoExecutionGateway(FuturesPaperGateway):
    """Existing shared controller/risk ledger, not a second economic ledger."""
    def __init__(self, adapter, observation, instruments, balances, **kwargs):
        if not isinstance(adapter, DemoFuturesAdapter):
            raise SafetyBlocked('Demo adapter required')
        if (not isinstance(observation, DemoRuleObservation)
                or observation.account_id != adapter.account_id):
            raise SafetyBlocked('Demo account-bound rule observation required')
        super().__init__(adapter, observation.rules, instruments, balances, **kwargs)

    def submit_order(self, intent):
        if intent.instrument_id != 'NVDAUSDT':
            raise SafetyBlocked('Demo execution contract currently supports NVDAUSDT only')
        if type(self.adapter.client.transport.authorization) is not DemoExecutionAuthorization:
            raise SafetyBlocked('Demo injected writes disabled by default')
        self.adapter._admitted_intent = intent
        try:
            return super().submit_order(intent)
        finally:
            self.adapter._admitted_intent = None
