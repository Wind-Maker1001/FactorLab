"""Demo execution admission over offline injection or a separately authorized boundary."""
from dataclasses import dataclass, field

from .adapter import FuturesAdapter
from .gateway import FuturesPaperGateway
from .protocol import Environment, OfflineTransport, SafetyBlocked, WRITE_PATHS, READ_PATHS
from .rules import RuleObservation, order_parameters
from .network import DemoNetworkTransport, DemoNetworkAuthorization, demo_network_permitted


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
    client: object = field(default=None, repr=False, compare=False)


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
    observation = DemoRuleObservation(rules, adapter.account_id, client)
    adapter._rule_observation = observation
    return observation


class DemoFuturesAdapter(FuturesAdapter):
    """Reuse persisted identities/state machine with separately admitted Demo IO."""
    def __init__(self, client, store, **kwargs):
        if (client.environment is not Environment.DEMO
                or not (isinstance(client.transport, DemoExecutionTransport) or type(client.transport) is DemoNetworkTransport)
                or client.credentials is None
                or client.credentials.environment is not Environment.DEMO):
            raise SafetyBlocked('isolated Demo credentials and injected execution transport required')
        super().__init__(client, store, account_id=Environment.DEMO.account_id, **kwargs)
        self._admitted_intent = None

    def submit_order(self, intent):
        if intent is not self._admitted_intent:
            raise SafetyBlocked('Demo submission requires shared controller and rule admission')
        transport = self.client.transport
        if type(transport) is DemoNetworkTransport:
            transport._admission['request'] = ('POST', order_parameters(intent), self.account_id)
        try:
            return super().submit_order(intent)
        finally:
            if type(transport) is DemoNetworkTransport:
                transport._admission.clear()

    def cancel_order(self, client_order_id):
        transport = self.client.transport
        if type(transport) is DemoNetworkTransport:
            if not demo_network_permitted(self.client):
                raise SafetyBlocked('Demo network authorization required')
            order = self.order(client_order_id)
            plan = transport.authorization.plan
            if (plan.account_id != self.account_id or order.intent.instrument_id != plan.symbol
                    or not client_order_id.startswith(plan.client_id_prefix)):
                raise SafetyBlocked('cancel outside approved identity scope')
            params = dict(symbol=plan.symbol, origClientOrderId=client_order_id)
            transport._admission['request'] = ('DELETE', params, self.account_id)
        elif type(transport.authorization) is not DemoExecutionAuthorization:
            raise SafetyBlocked('Demo injected writes disabled by default')
        try:
            return super().cancel_order(client_order_id)
        finally:
            if type(transport) is DemoNetworkTransport:
                transport._admission.clear()


class DemoExecutionGateway(FuturesPaperGateway):
    """Existing shared controller/risk ledger, not a second economic ledger."""
    def __init__(self, adapter, observation, instruments, balances, **kwargs):
        if not isinstance(adapter, DemoFuturesAdapter):
            raise SafetyBlocked('Demo adapter required')
        if (not isinstance(observation, DemoRuleObservation)
                or observation.account_id != adapter.account_id):
            raise SafetyBlocked('Demo account-bound rule observation required')
        if type(adapter.client.transport) is DemoNetworkTransport:
            if (observation.client is not adapter.client
                    or getattr(adapter, '_rule_observation', None) is not observation):
                raise SafetyBlocked('current-process Demo rules acquisition required')
        super().__init__(adapter, observation.rules, instruments, balances, **kwargs)
        self._initial_equity = None
        self._controller = None

    def bind_controller(self, controller):
        from factorlab.trading.control import TradingController
        if not isinstance(controller, TradingController) or controller.broker is not self:
            raise SafetyBlocked('existing shared controller binding required')
        self._controller = controller

    def submit_order(self, intent):
        if intent.instrument_id != 'NVDAUSDT':
            raise SafetyBlocked('Demo execution contract currently supports NVDAUSDT only')
        if type(self.adapter.client.transport) is DemoNetworkTransport:
            self._network_admission(intent)
        elif type(self.adapter.client.transport.authorization) is not DemoExecutionAuthorization:
            raise SafetyBlocked('Demo injected writes disabled by default')
        self.adapter._admitted_intent = intent
        try:
            return super().submit_order(intent)
        finally:
            self.adapter._admitted_intent = None

    def _network_admission(self, intent):
        from decimal import Decimal
        from datetime import timedelta
        from .account import instant
        from factorlab.trading.contracts import TERMINAL
        adapter, state = self.adapter, self.state
        transport = adapter.client.transport
        if not demo_network_permitted(adapter.client):
            raise SafetyBlocked('Demo network authorization missing or expired')
        plan = transport.authorization.plan
        if plan.account_id != adapter.account_id or state.account_id != adapter.account_id:
            raise SafetyBlocked('execution plan account mismatch')
        if self._controller is None or self._controller.broker is not self:
            raise SafetyBlocked('shared controller binding required')
        if self._controller._admitted_intent is not intent:
            raise SafetyBlocked('order must pass shared controller risk admission')
        if state.kill_switch:
            raise SafetyBlocked('KILL_SWITCH: no new Demo orders')
        if (intent.order_type.value not in plan.order_types or intent.quantity > plan.max_quantity
                or not intent.client_order_id.startswith(plan.client_id_prefix)):
            raise SafetyBlocked('order outside execution plan')
        observation = getattr(adapter, '_rule_observation', None)
        if observation is None or observation.client is not adapter.client or observation.rules is not self.rules:
            raise SafetyBlocked('current Demo rules binding required')
        facts = adapter.facts
        if facts is None or facts.account_id != adapter.account_id:
            raise SafetyBlocked('Demo account evidence required')
        if facts.can_trade is not True:
            raise SafetyBlocked('UNKNOWN or blocked account trading eligibility')
        if facts.quantitative is not None and facts.quantitative.applicable_locks(intent.instrument_id):
            raise SafetyBlocked('applicable quantitative trading lock')
        if facts.quantitative is None or facts.quantitative.error:
            raise SafetyBlocked('quantitative observation UNKNOWN; refresh account evidence')
        if not timedelta(0) <= self.clock() - facts.observed_at <= timedelta(seconds=5):
            raise SafetyBlocked('STALE_ACCOUNT')
        mark = adapter.client.request('GET', '/fapi/v1/premiumIndex', {'symbol': plan.symbol}, signed=False)
        if (mark.get('symbol') != plan.symbol or type(mark.get('time')) is not int
                or not timedelta(0) <= self.clock() - instant(mark['time']) <= timedelta(seconds=5)):
            raise SafetyBlocked('UNKNOWN or stale Demo mark')
        reference = Decimal(mark['markPrice'])
        filters = self.rules.filters
        supported = {'LOT_SIZE', 'MARKET_LOT_SIZE', 'PRICE_FILTER', 'MIN_NOTIONAL',
                     'PERCENT_PRICE', 'MAX_NUM_ORDERS', 'POSITION_RISK_CONTROL'}
        if set(filters) - supported:
            raise SafetyBlocked('unsupported exchange filter')
        if filters.get('POSITION_RISK_CONTROL', {}).get('positionControlSide') not in (None, 'NONE'):
            raise SafetyBlocked('position risk restriction')
        band = filters.get('PERCENT_PRICE', {})
        if any(band.get(k) is None for k in ('multiplierUp', 'multiplierDown')):
            raise SafetyBlocked('UNKNOWN price band')
        low, high = reference * Decimal(band['multiplierDown']), reference * Decimal(band['multiplierUp'])
        price = intent.limit_price if intent.limit_price is not None else high
        if not reference.is_finite() or reference <= 0 or not low <= price <= high:
            raise SafetyBlocked('Demo price band')
        quote = state.quotes.get(plan.symbol)
        if quote is None or not low <= quote.price <= high:
            raise SafetyBlocked('Demo quote outside mark band')
        notional = intent.quantity * max(price, quote.price)
        if notional > plan.max_notional:
            raise SafetyBlocked('plan notional limit')
        rows = [adapter.order(row[0]) for row in adapter.db.execute('SELECT id FROM futures_orders')]
        scoped = [o for o in rows if o.intent.client_order_id.startswith(plan.client_id_prefix)]
        if len(scoped) >= plan.max_orders:
            raise SafetyBlocked('plan total order limit')
        pending = [o for o in rows if o.status not in TERMINAL]
        exchange_limit = filters.get('MAX_NUM_ORDERS', {}).get('limit')
        if exchange_limit is None:
            raise SafetyBlocked('UNKNOWN exchange order limit')
        if len(pending) >= min(plan.max_open_orders, int(exchange_limit)):
            raise SafetyBlocked('plan open order limit')
        funds = facts.usdt()
        keys = ('walletBalance', 'unrealizedProfit', 'initialMargin', 'availableBalance')
        if any(funds[k] is None for k in keys):
            raise SafetyBlocked('UNKNOWN account equity or margin')
        equity = funds['walletBalance'] + funds['unrealizedProfit']
        if self._initial_equity is None:
            self._initial_equity = equity
        local_pnl = sum((p.quantity * (reference - p.entry_price)
                         for p in state.positions.values()), Decimal('0'))
        local_equity = (sum((a.total for a in self.read_account().assets), Decimal('0'))
                        + min(local_pnl, funds['unrealizedProfit']))
        if self._initial_equity - min(equity, local_equity) >= plan.max_loss:
            raise SafetyBlocked('plan virtual loss limit')
        # Full notional reservation is conservative for the one-way supported path.
        reserved = sum(((o.intent.quantity - o.cumulative_filled_quantity)
                        * max(o.intent.limit_price or quote.price, quote.price) for o in pending), Decimal('0'))
        collateral = sum((abs(p.quantity) * max(p.entry_price, reference, quote.price)
                          for p in state.positions.values()), Decimal('0'))
        if max(funds['initialMargin'], collateral) + reserved + notional > plan.max_margin:
            raise SafetyBlocked('plan margin limit')
