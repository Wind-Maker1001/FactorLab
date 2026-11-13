"""Demo execution admission over offline injection or a separately authorized boundary."""
from dataclasses import dataclass, field

from factorlab.errors import DataQualityError
from .adapter import FuturesAdapter
from .gateway import FuturesPaperGateway
from .protocol import Environment, OfflineTransport, SafetyBlocked, WRITE_PATHS, READ_PATHS
from .rules import RuleObservation, SUPPORTED_NETWORK_FILTERS, order_parameters, symbol_row
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


def demo_symbol_info(info, symbol):
    if not isinstance(info, dict):
        raise DataQualityError('malformed exchangeInfo')
    return symbol_row(info.get('symbols'), symbol)


def read_demo_symbol(client, symbol):
    info = client.request('GET', '/fapi/v1/exchangeInfo', signed=False)
    return demo_symbol_info(info, symbol)


def observe_demo_rules(adapter, symbol='NVDAUSDT', *, source_version, on_symbol=None):
    client = adapter.client
    if client.environment is not Environment.DEMO:
        raise SafetyBlocked('Demo rules require Demo client')
    if symbol != 'NVDAUSDT':
        raise SafetyBlocked('Demo execution contract currently supports NVDAUSDT only')
    symbol_info = read_demo_symbol(client, symbol)
    if on_symbol is not None:
        on_symbol(symbol_info)
    brackets = client.request('GET', '/fapi/v1/leverageBracket', {'symbol': symbol})
    bracket = symbol_row([brackets] if isinstance(brackets, dict) else brackets, symbol)
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
        self._admitted_cancel_id = None
        self.db.execute('CREATE TABLE IF NOT EXISTS demo_quote_receipts (id TEXT PRIMARY KEY, policy TEXT NOT NULL, receipt TEXT NOT NULL)')
        self.db.execute('CREATE TABLE IF NOT EXISTS demo_cancel_attempts (id TEXT PRIMARY KEY)')
        self.db.execute('CREATE TABLE IF NOT EXISTS demo_quote_rejections (id TEXT PRIMARY KEY, receipt TEXT NOT NULL)')

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
            if self._admitted_cancel_id != client_order_id:
                raise SafetyBlocked('Demo cancellation requires shared controller admission')
            order = self.order(client_order_id)
            plan = transport.authorization.plan
            if (plan.account_id != self.account_id or order.intent.instrument_id != plan.symbol
                    or not client_order_id.startswith(plan.client_id_prefix)):
                raise SafetyBlocked('cancel outside approved identity scope')
            params = dict(symbol=plan.symbol, origClientOrderId=client_order_id)
            transport._admission['request'] = ('DELETE', params, self.account_id)
        elif type(transport.authorization) is not DemoExecutionAuthorization:
            raise SafetyBlocked('Demo injected writes disabled by default')
        if type(transport) is DemoNetworkTransport and transport.authorization.plan.quote_policy is not None:
            with self.store.transaction():
                if self.db.execute('SELECT 1 FROM demo_cancel_attempts WHERE id=?', (client_order_id,)).fetchone():
                    transport._admission.clear()
                    raise SafetyBlocked('bounded DELETE opportunity already consumed; GET reconcile only')
                self.db.execute('INSERT INTO demo_cancel_attempts VALUES(?)', (client_order_id,))
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
        self._bounded_observation = None
        self.quote_receipt = None

    def prepare_bounded_quote(self, observation):
        from .bounded_quote import bounded_quote
        transport = self.adapter.client.transport
        if type(transport) is not DemoNetworkTransport or transport.authorization is None:
            raise SafetyBlocked('bounded quote requires independently authorized Demo plan')
        plan = transport.authorization.plan
        if plan.quote_policy is None:
            raise SafetyBlocked('bounded policy required')
        key = plan.quote_policy.client_order_id
        if (self.adapter.db.execute('SELECT 1 FROM futures_orders WHERE id=?', (key,)).fetchone()
                or self.adapter.db.execute('SELECT 1 FROM demo_quote_receipts WHERE id=?', (key,)).fetchone()):
            raise SafetyBlocked('bounded identity already consumed; GET reconcile only')
        if plan.quote_policy.fee_rate < self.state.fee_rate:
            raise SafetyBlocked('bounded fee reserve below shared gateway fee rate')
        try:
            intent, receipt = bounded_quote(plan, observation, facts=self.adapter.facts, rules=self.rules, now=self.clock())
        except SafetyBlocked as error:
            self.quote_receipt = error.receipt
            import json
            with self.adapter.store.transaction():
                self.adapter.db.execute('INSERT INTO demo_quote_rejections VALUES(?,?) '
                    'ON CONFLICT(id) DO UPDATE SET receipt=excluded.receipt',
                    (key, json.dumps(error.receipt, sort_keys=True)))
            raise
        self._bounded_observation = observation
        self.quote_receipt = receipt
        return intent

    def _accepts_network_transport(self, client):
        return type(client.transport) is DemoNetworkTransport

    def read_account(self, *, exclude_order_id=None):
        from dataclasses import replace
        from factorlab.trading.contracts import TERMINAL
        facts = self.adapter.facts
        if facts is None:
            raise SafetyBlocked('Demo account evidence required')
        if not facts.shared().assets:
            raise SafetyBlocked('UNKNOWN Demo wallet or available balance')
        orders = tuple(r.snapshot for key, r in self.state.orders.items()
                       if key != exclude_order_id and r.snapshot.status not in TERMINAL)
        local = super().read_account(exclude_order_id=exclude_order_id)
        remote_ids = {row[0] for row in self.adapter.db.execute('SELECT id FROM futures_orders')}
        consistent = remote_ids == set(self.state.orders)
        if consistent:
            for key, record in self.state.orders.items():
                confirmed = self.adapter.order(key)
                local_order = record.snapshot
                if (local_order.reconcile_required or confirmed.reconcile_required
                        or (local_order.status, local_order.cumulative_filled_quantity, local_order.cumulative_fee)
                        != (confirmed.status, confirmed.cumulative_filled_quantity, confirmed.cumulative_fee)):
                    consistent = False
                    break
        remote = facts.shared(orders, reconciled=self.state.reconciled and self.adapter.reconciled and consistent)
        from factorlab.trading.contracts import AssetBalance
        assets = tuple(AssetBalance(a.currency, a.total,
            a.total - min(a.available, next(b.available for b in remote.assets if b.currency == a.currency)))
            for a in local.assets)
        return replace(local, assets=assets, reconciled=remote.reconciled)

    def reconcile(self):
        """Import authenticated account authority after all execution deltas are booked."""
        exchange = self.adapter.reconcile()
        remote_ids = {row[0] for row in self.adapter.db.execute('SELECT id FROM futures_orders')}
        if remote_ids != set(self.state.orders):
            self.state.reconciled = False
            return self.read_account()
        for key, record in tuple(self.state.orders.items()):
            remote = self.adapter.order(key)
            if (remote.cumulative_filled_quantity, remote.cumulative_fee) != (
                    record.snapshot.cumulative_filled_quantity, record.snapshot.cumulative_fee):
                self.state.reconciled = False
                return self.read_account()
        from dataclasses import replace
        for key, record in tuple(self.state.orders.items()):
            self.state.orders[key] = replace(record, snapshot=self.adapter.order(key))
        self.state.cash = {a.currency: a.total for a in exchange.assets}
        self.state.positions = {p.instrument_id: p for p in exchange.positions}
        self.state.reconciled = exchange.reconciled
        return self.read_account()

    def apply(self, event):
        """Book exchange-confirmed fees rather than the Paper fee estimate."""
        from factorlab.trading.state import advance
        if not self.record_event(event):
            return False
        record = self.state.orders[event.client_order_id]
        updated = advance(record, event)
        delta = updated.snapshot.cumulative_filled_quantity - record.snapshot.cumulative_filled_quantity
        fee = updated.snapshot.cumulative_fee - record.snapshot.cumulative_fee
        if delta:
            self._account_fill(record.snapshot.intent, delta, event.fill_price, fee)
        self.state.orders[event.client_order_id] = updated
        self.state.confirmations[event.client_order_id] = event
        return True

    def bind_controller(self, controller):
        from factorlab.trading.control import TradingController
        if not isinstance(controller, TradingController) or controller.broker is not self:
            raise SafetyBlocked('existing shared controller binding required')
        self._controller = controller

    def submit_order(self, intent):
        if not self.read_account().reconciled:
            raise SafetyBlocked('RECONCILE_REQUIRED: confirmed Demo account projection required')
        if intent.instrument_id != 'NVDAUSDT':
            raise SafetyBlocked('Demo execution contract currently supports NVDAUSDT only')
        if type(self.adapter.client.transport) is DemoNetworkTransport:
            self._network_admission(intent)
        elif type(self.adapter.client.transport.authorization) is not DemoExecutionAuthorization:
            raise SafetyBlocked('Demo injected writes disabled by default')
        if (type(self.adapter.client.transport) is DemoNetworkTransport
                and self.adapter.client.transport.authorization.plan.quote_policy is not None):
            import json
            policy = self.adapter.client.transport.authorization.plan.quote_policy
            self.quote_receipt['dispatch'] = 'POSSIBLE_DISPATCH_GET_ONLY'
            with self.adapter.store.transaction():
                if self.adapter.db.execute('SELECT 1 FROM demo_quote_receipts WHERE id=?', (intent.client_order_id,)).fetchone():
                    raise SafetyBlocked('bounded POST opportunity already consumed')
                self.adapter.db.execute('INSERT INTO demo_quote_receipts VALUES(?,?,?)',
                    (intent.client_order_id, policy.digest, json.dumps(self.quote_receipt, sort_keys=True)))
        if (type(self.adapter.client.transport) is DemoNetworkTransport
                and self.adapter.client.transport.authorization.plan.quote_policy is not None):
            from .bounded_quote import bounded_quote
            approved_plan = self.adapter.client.transport.authorization.plan
            def dispatch_guard():
                if self.adapter.client.transport.authorization.plan != approved_plan:
                    raise SafetyBlocked('bounded execution plan changed at dispatch')
                expected, _ = bounded_quote(approved_plan, self._bounded_observation,
                    facts=self.adapter.facts, rules=self.rules, now=self.clock())
                if expected != intent:
                    raise SafetyBlocked('bounded identity changed at dispatch')
                if self.state.kill_switch:
                    raise SafetyBlocked('KILL_SWITCH: no new Demo orders')
            self.adapter.client.transport._admission['bounded_guard'] = dispatch_guard
        self.adapter._admitted_intent = intent
        try:
            return super().submit_order(intent)
        finally:
            self.adapter._admitted_intent = None

    def cancel_order(self, client_order_id):
        if type(self.adapter.client.transport) is not DemoNetworkTransport:
            return super().cancel_order(client_order_id)
        controller = self._controller
        if (controller is None or controller.broker is not self
                or controller._admitted_cancel_id != client_order_id):
            raise SafetyBlocked('Demo cancellation requires shared controller admission')
        self.adapter._admitted_cancel_id = client_order_id
        try:
            return super().cancel_order(client_order_id)
        finally:
            self.adapter._admitted_cancel_id = None

    def fill(self, *args, **kwargs):
        raise SafetyBlocked('Demo fills require exchange confirmation')

    def reject(self, *args, **kwargs):
        raise SafetyBlocked('Demo rejection requires exchange confirmation')

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
        if plan.quote_policy is not None:
            from .bounded_quote import bounded_quote
            if self._bounded_observation is None:
                raise SafetyBlocked('immutable bounded observation required')
            expected, receipt = bounded_quote(plan, self._bounded_observation,
                facts=facts, rules=self.rules, now=self.clock())
            if expected != intent:
                raise SafetyBlocked('bounded exact intent mismatch')
            if self.quote_receipt['plan_digest'] != receipt['plan_digest']:
                raise SafetyBlocked('bounded approved plan changed')
            reference = self._bounded_observation.mark
            quote = state.quotes.get(plan.symbol)
            if quote is None or quote.price != max(self._bounded_observation.ask, reference):
                raise SafetyBlocked('shared quote differs from bounded observation')
            self.quote_receipt = receipt
        else:
            mark = adapter.client.request('GET', '/fapi/v1/premiumIndex', {'symbol': plan.symbol}, signed=False)
            if (mark.get('symbol') != plan.symbol or type(mark.get('time')) is not int
                    or not timedelta(0) <= self.clock() - instant(mark['time']) <= timedelta(seconds=5)):
                raise SafetyBlocked('UNKNOWN or stale Demo mark')
            reference = Decimal(mark['markPrice'])
        filters = self.rules.filters
        if set(filters) - SUPPORTED_NETWORK_FILTERS:
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
