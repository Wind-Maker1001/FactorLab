"""Serial V2 instance turns inside the existing Demo Execute owner."""
import json

from factorlab.trading.storage import decode
from .protocol import ExchangeError, SafetyBlocked


class DemoInstanceOwner:
    def __init__(self, session, execution):
        if execution.session is not session:
            raise ValueError('instances must share the existing Demo session')
        if getattr(session, 'instance_owner', None) is not None:
            raise ValueError('Demo session already has an instance scheduler')
        self.session, self.execution = session, execution
        session.instance_owner = self
        self.instances = {}
        self.market_sources = None

    @classmethod
    def open(cls, session, store, *, capability):
        """Bind the account's existing capital store without creating any allocation."""
        from factorlab.trading.portfolio_execution import PortfolioExecutor
        from .demo_portfolio_adapter import DemoPortfolioAdapter
        from .demo_instance_execution import DemoInstanceExecutionAdapter
        executor = PortfolioExecutor(DemoPortfolioAdapter(session, capability=capability),
            store, quote_currency='USDT', clock=session._now)
        execution = DemoInstanceExecutionAdapter(session, executor=executor)
        owner = cls(session, execution)
        def admitted_capability(intent, ownership):
            authorization, guard = capability(intent, ownership)
            def admitted_guard(method, params, plan):
                configured = decode(json.loads(execution.store.event_payload(
                    'demo:instance:' + ownership[0])))
                owner.require_dispatch(configured['binding'], method=method, intent=intent)
                guard(method, params, plan)
            return authorization, admitted_guard
        executor.adapter.capability = admitted_capability
        return owner

    @property
    def capital(self):
        return self.execution.executor.capital

    def attach(self, runtime, deployment):
        view = runtime.store
        binding = view.binding()
        if (runtime.adapter is not self.execution or deployment.store is not view
                or binding['account_id'] != self.session.adapter.account_id):
            raise ValueError('instance runtime/deployment must use the account execution owner')
        instance_id = binding['instance_id']
        if instance_id in self.instances:
            raise ValueError('instance already attached')
        self.instances[instance_id] = (runtime, deployment)

    def before_decision(self, instance_id):
        runtime, deployment = self.instances[instance_id]
        view = runtime.store
        binding = view.binding()
        if self.execution.executor.store.capital_balance(binding['account_id'], instance_id)[3]:
            return False
        if view.pending() is not None or view.activation_callback() is not None:
            deployment.step()
            return False
        if view.snapshot()['active'] != runtime.package_id:
            return False
        runtime.recover()
        return True

    def require_dispatch(self, binding, *, method, intent):
        """Recheck the instance switch boundary immediately before account dispatch."""
        attached = self.instances.get(binding['instance_id'])
        if attached is None:
            return
        runtime, _ = attached
        view = runtime.store
        if view.binding() != binding:
            raise SafetyBlocked('instance dispatch binding mismatch')
        if method == 'DELETE':
            return
        pending = view.pending()
        if pending is not None and view.activation_callback() is None and intent.reduce_only:
            raw = self.execution.store.event_payload('demo:deployment-reduction:' + intent.client_order_id)
            if raw is not None:
                maintenance = decode(json.loads(raw))
                if maintenance == dict(binding=binding, switch_id=pending['id']):
                    return
        if (pending is not None or view.activation_callback() is not None
                or view.snapshot()['active'] != runtime.package_id):
            raise SafetyBlocked('instance deployment is not active for dispatch')

    def decide(self, instance_id, event):
        if event['binding']['instance_id'] != instance_id:
            raise SafetyBlocked('event routed to another instance')
        if not self.before_decision(instance_id):
            return None
        return self.instances[instance_id][0].decide(event)

    def accept_task(self, task):
        """Persist a mailbox target using an already qualified instance allocation."""
        from decimal import Decimal
        from factorlab.trading.realtime import identity
        from .tradfi_schedule import utc_instant
        candidates = []
        for raw, in self.execution.store.db.execute(
                "SELECT payload FROM events WHERE id LIKE 'demo:instance:%'"):
            configured = decode(json.loads(raw))
            if configured['binding']['allocation_id'] == task['allocation_id']:
                candidates.append(configured)
        if len(candidates) != 1:
            raise SafetyBlocked('task requires one qualified instance allocation')
        configured = candidates[0]
        binding = configured['binding']
        package_id = 'mailbox-target-v1'
        decision_id = 'demo:decision:' + identity((binding['instance_id'], package_id, task['task_id']))
        existing = self.execution.store.db.execute(
            'SELECT completed,payload FROM demo_instance_outbox WHERE id=?', (decision_id,)).fetchone()
        expired = utc_instant(task['valid_until']) <= self.session._now()
        if existing is not None:
            if decode(json.loads(existing[1]))['receipt']['input_identity'] != identity(task):
                raise SafetyBlocked('decision identity has conflicting content')
            if existing[0]:
                return ('REJECTED' if existing[0] == 2 else 'COMPLETED'), dict(journal=decision_id)
            if expired:
                return 'RECONCILING', dict(reason='EXPIRED_INFLIGHT', journal=decision_id)
        else:
            if expired:
                return 'REJECTED', dict(reason='EXPIRED', journal=decision_id)
            budget = Decimal(task['equity_usdt'])
            allocation = self.execution.executor.store.allocation(binding['account_id'], binding['instance_id'])
            available = allocation['capital']
            if (budget > available or available <= 0
                    or not set(task['weights']) <= set(configured['symbols'])):
                raise SafetyBlocked('task exceeds funded instance declaration')
            receipt = dict(binding=binding, package_id=package_id, event_id=task['task_id'],
                input_identity=identity(task), event=dict(task=task),
                decision=dict(kind='target_weights', targets={symbol: Decimal(weight)
                    for symbol, weight in task['weights'].items()}, orders=[], cancellations=[], state={}))
            self.execution.execute(receipt)
        return 'ACCEPTED', dict(journal=decision_id)

    def reconcile(self):
        """Refresh original execution identities without new decisions or writes to Binance."""
        self.execution.executor.reconcile(self.session.adapter.account_id)

    def poll(self):
        """Acquire current execution facts on the existing supervisor's owner thread."""
        from decimal import Decimal
        from factorlab.trading.portfolio_execution import ExecutionQuote, InstrumentRules
        from factorlab.errors import DataQualityError
        from .demo_market_feed import FeedPaused
        from factorlab.trading.capital_cli import process_command
        process_command(self.capital, self.session.adapter.account_id)
        ready = {instance_id: self.before_decision(instance_id)
                 for instance_id in tuple(self.instances)}
        pending = self.execution.store.db.execute(
            'SELECT id,payload FROM demo_instance_outbox WHERE completed=0').fetchall()
        accepted = {decision_id: decode(json.loads(raw)) for decision_id, raw in pending}
        capital = self.execution.executor.store
        retired, eligible = [], set()
        for decision_id, decision in accepted.items():
            binding = decision['receipt']['binding']
            instance = binding['instance_id']
            if capital.capital_balance(binding['account_id'], instance)[3]:
                retired.append((decision_id,))
            elif ready.get(instance, True):
                eligible.add(decision_id)
        if retired:
            with self.execution.store.transaction():
                self.execution.store.db.executemany(
                    'UPDATE demo_instance_outbox SET completed=2 WHERE id=?', retired)
        if not eligible:
            return {}
        symbols = {intent.instrument_id for decision_id in eligible
                   for intent in accepted[decision_id]['intents']}
        if self.market_sources is not None:
            self.market_sources.require(symbols)
        quotes, rules = {}, {}
        for symbol in sorted(symbols):
            try:
                context = self.session.symbol_context(symbol)
                payload = None if self.market_sources is None else self.market_sources.book(symbol)
                book = self.session._fresh_book(payload, symbol=symbol)
            except ExchangeError:
                raise
            except (DataQualityError, FeedPaused, TimeoutError, ConnectionError):
                continue
            filters = context.rules.filters
            rules[symbol] = InstrumentRules(
                Decimal(filters['LOT_SIZE']['stepSize']),
                Decimal(filters['MIN_NOTIONAL']['notional']),
                price_tick=Decimal(filters['PRICE_FILTER']['tickSize']))
            at = self.session._now()
            quotes[symbol] = ExecutionQuote(symbol, book['bidPrice'], book['askPrice'],
                book['time_utc'], at, at.date().isoformat(), 'OFFICIAL_DEMO')
        facts = self.session.adapter.read_facts()
        funds = facts.usdt()
        return self.advance(equity=funds['walletBalance'] + funds['unrealizedProfit'],
                            quotes=quotes, rules=rules, now=self.session._now(), decision_ids=eligible)

    def _expire_task(self, decision_id, accepted):
        from factorlab.trading.contracts import TERMINAL
        from factorlab.trading.portfolio_execution import SubmissionNotAttempted
        plans = [decode(json.loads(payload)) for payload, in
                 self.execution.executor.store.db.execute('SELECT payload FROM portfolio_plans')]
        matching = [plan for plan in plans if plan['request']['rebalance_id'] == decision_id]
        if matching:
            phases = [self.execution.executor.expire(plan['id']).phase for plan in matching]
            terminal = all(phase == 'COMPLETE' for phase in phases)
            if terminal:
                with self.execution.store.transaction():
                    self.execution.store.db.execute(
                        'UPDATE demo_instance_outbox SET completed=2 WHERE id=?', (decision_id,))
            return
        terminal = True
        for intent in accepted['intents']:
            try:
                event = self.execution.executor.adapter.query_order(intent.client_order_id)
            except SubmissionNotAttempted:
                continue
            if event.status not in TERMINAL:
                event = self.execution.executor.adapter.cancel_order(intent.client_order_id)
            terminal = terminal and event.status in TERMINAL
        if terminal:
            with self.execution.store.transaction():
                self.execution.store.db.execute(
                    'UPDATE demo_instance_outbox SET completed=2 WHERE id=?', (decision_id,))

    def advance(self, *, equity, quotes, rules, now, decision_ids=None):
        """Healthy symbols continue while unavailable symbols retain original intents."""
        results = {}
        rows = self.execution.store.db.execute(
            'SELECT id,payload FROM demo_instance_outbox WHERE completed=0 ORDER BY rowid').fetchall()
        for decision_id, raw in rows:
            if decision_ids is not None and decision_id not in decision_ids:
                continue
            accepted = decode(json.loads(raw))
            task = accepted.get('receipt', {}).get('event', {}).get('task')
            if task is not None:
                from .tradfi_schedule import utc_instant
                if utc_instant(task['valid_until']) <= now:
                    self._expire_task(decision_id, accepted)
                    continue
            symbols = {intent.instrument_id for intent in accepted['intents']}
            if not symbols <= quotes.keys() or not symbols <= rules.keys():
                continue
            results[decision_id] = self.execution.advance(decision_id,
                equity=equity, quotes=quotes, rules=rules, now=now)
        return results
