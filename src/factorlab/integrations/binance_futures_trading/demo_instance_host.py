"""Restore registered Demo instances inside the existing account supervisor."""
from contextlib import closing
from decimal import Decimal
from dataclasses import replace
import json
from pathlib import Path
from uuid import uuid4

from factorlab.strategies.decision_runtime import InstanceDecisionRuntime
from factorlab.strategies.deployment import DeploymentController
from factorlab.strategies.loader import LoadedStrategy
from factorlab.trading import Market
from factorlab.trading.portfolio_execution import PortfolioStore
from factorlab.trading.storage import decode
from .demo_instance_capability import DemoInstanceCapability
from .demo_instance_owner import DemoInstanceOwner
from .demo_runtime import durable_control_state
from .protocol import SafetyBlocked


class DemoInstanceHost:
    def __init__(self, session, runtime, registry, releases, directory, stack, control):
        self.session, self.runtime, self.registry = session, runtime, registry
        self.releases, self.stack, self.control = Path(releases), stack, control
        self.boot_id = uuid4().hex
        self.controllers, self.adapters = {}, {}
        path = Path(directory) / 'portfolio.sqlite3'
        from .demo_legacy_migration import account_history_source, start_account_history
        if not path.is_file() and account_history_source(session) is None:
            raise SafetyBlocked('existing account portfolio capital journal required')
        capital = stack.enter_context(closing(PortfolioStore(path)))
        self.owner = DemoInstanceOwner.open(session, capital,
            capability=DemoInstanceCapability(session, runtime, dispatch_gate=self.dispatch_gate))
        start_account_history(session, self.owner.execution.executor)
        from .demo_strategy_context import DemoStrategyContext
        self.context = DemoStrategyContext(self.owner.execution.store)
        self.owner.execution.store.db.execute('CREATE TABLE IF NOT EXISTS demo_instance_inputs ('
            'instance_id TEXT NOT NULL, package_id TEXT NOT NULL, event_id TEXT NOT NULL, '
            'payload TEXT NOT NULL, PRIMARY KEY(instance_id,package_id,event_id))')
        self.owner.execution.store.db.execute('CREATE TABLE IF NOT EXISTS demo_instance_bars ('
            'symbol TEXT NOT NULL, interval_seconds INTEGER NOT NULL, closed_at TEXT NOT NULL, '
            'payload TEXT NOT NULL, PRIMARY KEY(symbol,interval_seconds,closed_at))')
        self.owner.execution.store.db.execute('CREATE TABLE IF NOT EXISTS demo_instance_order_cursors ('
            'instance_id TEXT NOT NULL, package_id TEXT NOT NULL, cursor INTEGER NOT NULL, '
            'PRIMARY KEY(instance_id,package_id))')
        self.refresh()

    def closed_bar(self, bar, *, historical=False):
        from dataclasses import asdict
        from datetime import timedelta
        from factorlab.strategies.protocol import V2
        from factorlab.trading.storage import dumps
        from factorlab.trading.realtime import PersistenceConflict, identity
        from .demo_market_feed import SOURCE
        if self.control.desired() != 'RUNNING' or durable_control_state(self.runtime) is not None:
            return {}
        if not bar.complete or bar.source != SOURCE or bar.closed_at > self.session._now():
            raise SafetyBlocked('confirmed causal Demo bar required')
        store = self.owner.execution.store
        mark = bar.closed_at.isoformat()
        key = (bar.symbol, bar.interval_seconds, mark)
        payload = asdict(bar)
        with store.transaction():
            row = store.db.execute('SELECT payload FROM demo_instance_bars '
                'WHERE symbol=? AND interval_seconds=? AND closed_at=?', key).fetchone()
            if row is None:
                store.db.execute('INSERT INTO demo_instance_bars VALUES(?,?,?,?)', (*key, dumps(payload)))
            elif decode(json.loads(row[0])) != payload:
                raise PersistenceConflict('closed bar identity changed')
        results = {}
        if historical or self.session._now() - bar.closed_at >= timedelta(seconds=bar.interval_seconds):
            return results
        for instance_id, (runtime, _) in tuple(self.owner.instances.items()):
            spec = runtime.strategy.spec
            if (bar.symbol not in spec.symbols or 'closed_bar' not in spec.data['data_types']
                    or spec.schedule['kind'] != 'interval'
                    or spec.schedule['interval_seconds'] != bar.interval_seconds):
                continue
            event_id = 'bar:' + identity((bar.interval_seconds, mark, sorted(spec.symbols)))
            frozen = store.db.execute('SELECT payload FROM demo_instance_inputs '
                'WHERE instance_id=? AND package_id=? AND event_id=?',
                (instance_id, runtime.package_id, event_id)).fetchone()
            if frozen is not None:
                results[instance_id] = self.deliver(instance_id, decode(json.loads(frozen[0])))
                continue
            symbols = {}
            for symbol in spec.symbols:
                rows = store.db.execute('SELECT payload FROM demo_instance_bars '
                    'WHERE symbol=? AND interval_seconds=? AND closed_at<=? '
                    'ORDER BY closed_at DESC LIMIT ?',
                    (symbol, bar.interval_seconds, mark, spec.data['history_limit'])).fetchall()
                history = [decode(json.loads(row[0])) for row in reversed(rows)]
                if (not history or history[-1]['closed_at'] != bar.closed_at
                        or any(previous['closed_at'] != current['opened_at']
                               for previous, current in zip(history, history[1:]))):
                    break
                observations = [dict(observed_at=value['closed_at'], effective_at=value['closed_at'],
                    source=SOURCE, payload=value) for value in history]
                symbols[symbol] = dict(watermark=mark, observations={'closed_bar': observations[-1]},
                    history={'closed_bar': observations})
            if len(symbols) != len(spec.symbols):
                continue
            supplemental = self.context.symbols(spec.symbols, at=bar.closed_at,
                history_limit=spec.data['history_limit'])
            declared = set(spec.data['data_types']) - {'closed_bar'}
            for symbol, context in symbols.items():
                extra = supplemental[symbol]
                context['observations'].update({kind: value for kind, value in extra['observations'].items()
                                                if kind in declared})
                context['history'].update({kind: value for kind, value in extra['history'].items()
                                          if kind in declared})
                if len(context['history']) > 1:
                    context['watermark'] = identity(context['history'])
            binding = runtime.store.binding()
            allocation = self.owner.execution.executor.store.allocation(binding['account_id'], instance_id)
            event = dict(protocol=V2, binding=binding,
                event_id=event_id,
                event_time=bar.closed_at, data_type='closed_bar', symbols=symbols,
                account=self.session.status(), allocation=allocation)
            if spec.decision_kind == 'orders':
                from .demo_grid_context import grid_context
                context = grid_context(self, runtime)
                if context is None:
                    continue
                event['context'] = context
            results[instance_id] = self.deliver(instance_id, event)
        return results

    def deliver(self, instance_id, event):
        """Freeze a causal input before strategy execution; retries retain its projections."""
        from factorlab.strategies.events import validate_event
        from factorlab.trading.storage import dumps
        from factorlab.trading.realtime import PersistenceConflict
        self.dispatch_gate(event['binding'])
        if not self.owner.before_decision(instance_id):
            return None
        runtime, _ = self.owner.instances[instance_id]
        validate_event(event, runtime.strategy.spec)
        if event['binding'] != runtime.store.binding():
            raise SafetyBlocked('event routed to another instance binding')
        if event['event_time'] > self.session._now():
            raise SafetyBlocked('future instance event')
        store = self.owner.execution.store
        key = (instance_id, runtime.package_id, event['event_id'])
        with store.transaction():
            row = store.db.execute('SELECT payload FROM demo_instance_inputs '
                'WHERE instance_id=? AND package_id=? AND event_id=?', key).fetchone()
            if row is None:
                store.db.execute('INSERT INTO demo_instance_inputs VALUES(?,?,?,?)', (*key, dumps(event)))
            else:
                saved = decode(json.loads(row[0]))
                source = lambda value: {k: v for k, v in value.items() if k not in {'account', 'allocation'}}
                if source(saved) != source(event):
                    raise PersistenceConflict('instance market input identity changed')
                event = saved
        for raw, in store.db.execute('SELECT payload FROM demo_instance_outbox WHERE completed=0'):
            receipt = decode(json.loads(raw))['receipt']
            if (receipt['binding']['instance_id'] == instance_id
                    and (receipt['package_id'], receipt['event_id']) != (runtime.package_id, event['event_id'])
                    and (runtime.strategy.spec.decision_kind != 'orders' or receipt['decision']['kind'] != 'orders')):
                from factorlab.strategies.identity import content_identity
                return runtime.store.decision(runtime.package_id, event['event_id'], content_identity(event))
        return runtime.decide(event)

    @staticmethod
    def registered(registry, account_id):
        return [row for row in registry.instances() if row['binding'] is not None
                and row['binding']['market'] == Market.BINANCE_USDM.value
                and row['binding']['account_id'] == account_id]

    def dispatch_gate(self, binding):
        if durable_control_state(self.runtime) is not None or self.control.desired() != 'RUNNING':
            raise SafetyBlocked('account execution is not running')

    def load(self, view, version):
        return LoadedStrategy.from_strategy_package(self.releases / version,
            manifest=view.manifest(version), trusted=True)

    def refresh(self):
        store = self.owner.execution.executor.store
        account = self.session.adapter.account_id
        amounts = [store.allocation(account, owner)['capital'] for owner, in store.db.execute(
            'SELECT strategy FROM portfolio_allocations WHERE account=?', (account,))]
        total = sum(amounts, Decimal(0))
        if total > Decimal(27):
            raise SafetyBlocked('funded instances exceed original 7/20 USDT account capital')
        if amounts:
            limits = self.session.controller.limits
            self.session.controller.limits = replace(limits, max_order_notional=max(amounts),
                max_instrument_exposure=total, account_budget=total)
            self.session.shared_account = True
        for row in self.registered(self.registry, self.session.adapter.account_id):
            instance_id = row['instance_id']
            if instance_id in self.controllers or store.capital_balance(account, instance_id)[3]:
                continue
            view = self.stack.enter_context(closing(self.registry.for_instance(instance_id)))
            adapter = DemoInstanceDeployment(self, view)
            controller = DeploymentController(view, adapter,
                load_strategy=lambda version, view=view: self.load(view, version))
            adapter.controller = controller
            self.controllers[instance_id], self.adapters[instance_id] = controller, adapter
            active = view.snapshot()['active']
            if active is not None:
                adapter.prepare(active)
                self.install(adapter)

    def install(self, adapter):
        runtime = adapter.decision_runtime
        instance_id = adapter.view.instance_id
        if instance_id in self.owner.instances:
            self.owner.instances[instance_id] = runtime, adapter.controller
        else:
            self.owner.attach(runtime, adapter.controller)

    def collect_order_events(self):
        """Deliver original confirmations only to their persisted instance owner."""
        from dataclasses import asdict
        from factorlab.trading.contracts import OrderEvent
        from factorlab.strategies.protocol import V2
        from factorlab.trading.realtime import identity
        if self.control.desired() != 'RUNNING' or durable_control_state(self.runtime) is not None:
            return
        store = self.owner.execution.store
        for instance_id, (runtime, _) in tuple(self.owner.instances.items()):
            if 'order_event' not in runtime.strategy.spec.data['data_types']:
                continue
            binding = runtime.store.binding()
            key = instance_id, runtime.package_id
            saved = store.db.execute('SELECT cursor FROM demo_instance_order_cursors '
                'WHERE instance_id=? AND package_id=?', key).fetchone()
            cursor = 0 if saved is None else saved[0]
            native_clients = None
            if runtime.strategy.spec.decision_kind == 'orders':
                from .demo_grid_context import native_orders
                native_clients = set(native_orders(store, binding, runtime.package_id).values())
            rows = store.db.execute('SELECT rowid,payload FROM events WHERE rowid>? ORDER BY rowid',
                                    (cursor,)).fetchall()
            for position, raw in rows:
                report = decode(json.loads(raw))
                if isinstance(report, OrderEvent):
                    owner = self.owner.execution.owners.owner(report.client_order_id)
                    if (owner is not None and owner[:2] == (instance_id, binding['allocation_id'])
                            and (native_clients is None or report.client_order_id in native_clients)):
                        symbol = owner[2]
                        event_id = 'order:' + identity(report.event_id)
                        frozen = store.db.execute('SELECT payload FROM demo_instance_inputs '
                            'WHERE instance_id=? AND package_id=? AND event_id=?', (*key, event_id)).fetchone()
                        if frozen is None:
                            at = self.session._now()
                            observation = dict(observed_at=at, effective_at=report.event_time,
                                source=report.source, payload=asdict(report))
                            event = dict(protocol=V2, binding=binding, event_id=event_id,
                                event_time=at, data_type='order_event',
                                symbols={symbol: dict(watermark=report.event_id,
                                    observations={'order_event': observation}, history={})},
                                account=self.session.status(), allocation=self.owner.execution.executor.store.allocation(
                                    binding['account_id'], instance_id))
                            if runtime.strategy.spec.decision_kind == 'orders':
                                from .demo_grid_context import grid_context
                                context = grid_context(self, runtime)
                                if context is None:
                                    break
                                event['context'] = context
                        else:
                            event = decode(json.loads(frozen[0]))
                        if self.deliver(instance_id, event) is None:
                            break
                store.db.execute('INSERT INTO demo_instance_order_cursors VALUES(?,?,?) '
                    'ON CONFLICT(instance_id,package_id) DO UPDATE SET cursor=excluded.cursor',
                    (*key, position))

    def collect_funding(self):
        from factorlab.errors import DataQualityError
        from .demo_market_feed import FeedPaused
        from .protocol import ExchangeError
        sources = self.owner.market_sources
        if sources is None:
            return
        symbols = {symbol for runtime, _ in self.owner.instances.values()
                   if {'funding_settlement', 'funding_forecast'} & set(runtime.strategy.spec.data['data_types'])
                   for symbol in runtime.strategy.spec.symbols}
        for symbol in sorted(symbols):
            rows = self.owner.execution.store.db.execute('SELECT payload FROM demo_strategy_observations '
                "WHERE symbol=? AND kind='funding_settlement'", (symbol,)).fetchall()
            latest = max((decode(json.loads(raw))['effective_at'] for raw, in rows), default=None)
            after = None if latest is None else round(latest.timestamp()*1000) + 1
            try:
                observations = sources.funding(symbol, after=after)
            except ExchangeError:
                raise
            except (DataQualityError, FeedPaused, TimeoutError, ConnectionError):
                continue
            if observations is not None:
                for observation in observations:
                    self.context.observe(symbol, **observation)

    def collect_bars(self):
        from datetime import datetime
        from factorlab.errors import DataQualityError
        from .demo_market_feed import FeedPaused, INTERVALS
        from .protocol import ExchangeError
        sources = self.owner.market_sources
        if sources is None:
            return
        intervals = {seconds: interval for interval, seconds in INTERVALS.items()}
        requirements = {}
        for runtime, _ in tuple(self.owner.instances.values()):
            spec = runtime.strategy.spec
            if 'closed_bar' not in spec.data['data_types'] or spec.schedule['kind'] != 'interval':
                continue
            seconds = spec.schedule['interval_seconds']
            if seconds not in intervals:
                raise SafetyBlocked('unsupported Demo closed-bar interval')
            for symbol in spec.symbols:
                key = symbol, seconds
                requirements[key] = max(requirements.get(key, 0), spec.data['history_limit'])
        store = self.owner.execution.store
        for (symbol, seconds), history_limit in requirements.items():
            latest = store.db.execute('SELECT MAX(closed_at) FROM demo_instance_bars '
                'WHERE symbol=? AND interval_seconds=?', (symbol, seconds)).fetchone()[0]
            after = None if latest is None else round(datetime.fromisoformat(latest).timestamp() * 1000)
            try:
                bars = sources.bars(symbol, intervals[seconds], after=after, history_limit=history_limit)
            except ExchangeError:
                raise
            except (DataQualityError, FeedPaused, TimeoutError, ConnectionError):
                continue
            if bars:
                for bar in bars:
                    self.closed_bar(bar)
            elif latest is not None:
                from factorlab.trading.strategy import ClosedBar
                payload = store.db.execute('SELECT payload FROM demo_instance_bars '
                    'WHERE symbol=? AND interval_seconds=? AND closed_at=?',
                    (symbol, seconds, latest)).fetchone()[0]
                self.closed_bar(ClosedBar(**decode(json.loads(payload))))

    def step(self):
        if durable_control_state(self.runtime) is not None or self.control.desired() != 'RUNNING':
            return
        self.refresh()
        for instance_id, controller in tuple(self.controllers.items()):
            if instance_id not in self.owner.instances:
                controller.step()
        self.collect_order_events()
        self.collect_funding()
        self.collect_bars()


class DemoInstanceDeployment:
    def __init__(self, host, view):
        self.host, self.view = host, view
        self.boot_id = host.boot_id
        self.decision_runtime = None
        self.controller = None

    def prepare(self, version):
        strategy = self.host.load(self.view, version)
        binding = self.view.binding()
        allocation = self.host.owner.execution.executor.store.allocation(
            binding['account_id'], binding['instance_id'])
        if allocation is None or allocation['allocation_id'] != binding['allocation_id']:
            raise SafetyBlocked('registered instance requires its original funded allocation')
        self.decision_runtime = InstanceDecisionRuntime(strategy, self.view,
            self.host.owner.execution, allocation=allocation)

    def activated(self, version):
        if self.decision_runtime is None or self.decision_runtime.package_id != version:
            self.prepare(version)
        self.host.install(self)

    def _pending(self):
        store = self.host.owner.execution.store
        return [(decision_id, decode(json.loads(raw))) for decision_id, raw in store.db.execute(
            'SELECT id,payload FROM demo_instance_outbox WHERE completed=0 ORDER BY rowid')
            if decode(json.loads(raw))['receipt']['binding']['instance_id'] == self.view.instance_id]

    def quiesce(self):
        owner = self.host.owner
        for decision_id, accepted in self._pending():
            if accepted['receipt']['event'].get('deployment_switch') is None:
                owner._expire_task(decision_id, accepted)

    def reconcile(self):
        session, owner = self.host.session, self.host.owner
        account = session.gateway.recover(session.controller)
        owner.reconcile()
        binding = self.view.binding()
        owners = owner.execution.owners
        positions = [dict(symbol=symbol, quantity=quantity) for symbol, in owners.store.db.execute(
            'SELECT DISTINCT symbol FROM demo_order_owners WHERE instance_id=?', (self.view.instance_id,))
            if (quantity := owners.inventory(self.view.instance_id, symbol))]
        orders = []
        for client_id, in owners.store.db.execute(
                'SELECT client_id FROM demo_order_owners WHERE instance_id=? AND allocation_id=?',
                (self.view.instance_id, binding['allocation_id'])):
            record = session.gateway.state.orders.get(client_id)
            if record is not None:
                orders.append(dict(client_id=client_id, status=record.snapshot.status.value,
                                   reduce_only=record.snapshot.intent.reduce_only))
        status = session.status()
        untracked = [row['clientOrderId'] for row in session.adapter.facts.open_orders
                     if owners.owner(row['clientOrderId']) is None]
        return dict(reconciled=account.reconciled, unknown=status['unknown'], positions=positions,
                    orders=orders, untracked_orders=untracked)

    def watermark(self):
        store = self.host.owner.execution.store
        cursor = store.db.execute('SELECT COALESCE(MAX(rowid),0) FROM events').fetchone()[0]
        return str(cursor)

    def cancel(self, client_id):
        binding = self.view.binding()
        self.host.owner.execution.owners.require_cancel(client_id,
            instance_id=binding['instance_id'], allocation_id=binding['allocation_id'])
        self.host.owner.execution.executor.adapter.cancel_order(client_id)

    def reduce(self, switch_id, version):
        owner = self.host.owner
        pending = self._pending()
        if not pending:
            store = owner.execution.store
            prefix = 'deployment:' + str(switch_id) + ':'
            attempts = sum(1 for raw, in store.db.execute('SELECT payload FROM demo_instance_outbox')
                if (receipt := decode(json.loads(raw))['receipt'])['binding']['instance_id'] == self.view.instance_id
                and receipt['event_id'].startswith(prefix))
            binding = self.view.binding()
            configured = decode(json.loads(store.event_payload('demo:instance:' + binding['instance_id'])))
            receipt = dict(binding=binding, package_id=self.view.snapshot()['active'],
                event_id=prefix + str(attempts), input_identity=prefix + str(attempts),
                event=dict(deployment_switch=switch_id), decision=dict(kind='target_quantities',
                    targets={symbol: Decimal(0) for symbol in configured['symbols']},
                    orders=[], cancellations=[], state={}))
            owner.execution.execute(receipt)
            pending = self._pending()
        for decision_id, accepted in pending:
            if accepted['receipt']['event'].get('deployment_switch') != switch_id:
                raise SafetyBlocked('previous strategy decision must finish cleanup before deployment reduction')
            for intent in accepted['intents']:
                owner.execution.store.record_metadata('demo:deployment-reduction:' + intent.client_order_id,
                    dict(binding=self.view.binding(), switch_id=switch_id))
            self._advance(decision_id, accepted)

    def _advance(self, decision_id, accepted):
        from factorlab.trading.portfolio_execution import ExecutionQuote, InstrumentRules
        from factorlab.errors import DataQualityError
        from .demo_market_feed import FeedPaused
        from .protocol import ExchangeError
        session, owner = self.host.session, self.host.owner
        quotes, rules = {}, {}
        symbols = {intent.instrument_id for intent in accepted['intents']}
        if owner.market_sources is not None:
            owner.market_sources.require(symbols)
        for symbol in symbols:
            try:
                context = session.symbol_context(symbol)
                payload = None if owner.market_sources is None else owner.market_sources.book(symbol)
                book = session._fresh_book(payload, symbol=symbol)
            except ExchangeError:
                raise
            except (DataQualityError, FeedPaused, TimeoutError, ConnectionError):
                return
            at = session._now()
            quotes[symbol] = ExecutionQuote(symbol, book['bidPrice'], book['askPrice'],
                book['time_utc'], at, accepted['receipt']['event_id'], 'OFFICIAL_DEMO')
            filters = context.rules.filters
            rules[symbol] = InstrumentRules(Decimal(filters['LOT_SIZE']['stepSize']),
                Decimal(filters['MIN_NOTIONAL']['notional']), price_tick=Decimal(filters['PRICE_FILTER']['tickSize']))
        funds = session.adapter.read_facts().usdt()
        owner.execution.advance(decision_id, equity=funds['walletBalance'] + funds['unrealizedProfit'],
                                quotes=quotes, rules=rules, now=session._now())
