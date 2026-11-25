"""Shared-package multi-instance host; one existing Alpaca execution journal."""
from decimal import Decimal as D
from pathlib import Path
from uuid import uuid4

from factorlab.strategies.decision_runtime import InstanceDecisionRuntime
from factorlab.strategies.deployment import DeploymentController, DeploymentStore
from factorlab.strategies.loader import LoadedStrategy
from factorlab.trading.portfolio_execution import PortfolioStore
from .http import AlpacaError
from .adapter import OrderNotFound
from .portfolio import AlpacaPortfolioRunner
from .strategy_assets import StrategyAssets
from .strategy_data import StrategyData
from .strategy_deployment import AlpacaDeployment
from .strategy_execution import AlpacaDecisionExecution
from .strategy_runtime import AlpacaInstance
from .corporate_actions import CorporateReceiptJournal
from .cloud_execution import PreparationIncomplete
from .market_data.cache import MarketCache


class StrategyInstances:
    def __init__(self, root, adapter, configuration, *, clock, revision='unknown'):
        self.root, self.adapter, self.clock = Path(root), adapter, clock
        self.store = PortfolioStore(self.root / 'portfolio.sqlite3')
        self.runner = AlpacaPortfolioRunner(adapter, self.store, order_not_found=OrderNotFound,
                                           rejection_error=AlpacaError, clock=clock)
        self.deployments = DeploymentStore(self.root / 'deployments.sqlite3')
        self.cache = MarketCache(self.root / 'history')
        self.releases = Path(configuration['releases'])
        if configuration['trusted_python'] is not True:
            raise ValueError('explicit trusted_python approval required for strategy packages')
        self.boot_id = uuid4().hex
        self.instances, self.views, self.controllers = {}, {}, {}
        self.native = {}
        self.loaded = {}
        self.identity = None
        self.state = {'state': 'RECOVERING', 'instances': {}}
        self.execution = {'enabled': True, 'state': 'RECOVERING'}
        account = adapter.recover()
        facts = adapter.read_account_details()
        for registration in configuration['instances']:
            binding = registration['binding']
            if binding['account_id'] != facts.account_id:
                raise ValueError('configured instance belongs to another Alpaca account')
            owner = binding['instance_id']
            if owner in self.views:
                raise ValueError('duplicate instance registration')
            self.deployments.register_instance(binding)
            view = self.deployments.for_instance(owner)
            self.views[owner] = view
            funding = registration['funding']
            if 'adoption_receipt_id' in funding:
                self.runner.executor.adopt_allocation(account_id=facts.account_id, strategy_id=owner,
                    allocation_id=binding['allocation_id'], capital=D(funding['capital']),
                    current_cash=D(funding['current_cash']), official_equity=facts.equity,
                    receipt_id=funding['adoption_receipt_id'])
            else:
                self.runner.executor.allocate(account_id=facts.account_id, strategy_id=owner,
                    allocation_id=binding['allocation_id'], capital=D(funding['capital']),
                    initial_cash=D(funding['initial_cash']), official_equity=facts.equity)
            if registration.get('native_v69', False):
                from .native_v69 import NativeWeeklyExecution
                native = NativeWeeklyExecution(self.root, adapter, clock=clock, revision=revision,
                    runner=self.runner, deployments=self.deployments)
                if binding != native.view.binding():
                    raise ValueError('native V69 registration must preserve its original binding')
                self.native[owner] = native
                continue
            snapshot = view.snapshot()
            pending = view.pending()
            package = snapshot['active'] or (pending['new'] if pending else None)
            if package is None:
                self.state['instances'][owner] = {'state': 'WAITING_PACKAGE_ACTIVATION'}
                continue
            self.attach(owner, package, account)

    def attach(self, owner, package, account):
        self._bind(owner, package, account)
        deployment = AlpacaDeployment(self.instances[owner], self.views[owner], boot_id=self.boot_id)
        self.controllers[owner] = DeploymentController(self.views[owner], deployment, load_strategy=self.load)

    def load(self, package_id):
        if package_id not in self.loaded:
            self.loaded[package_id] = LoadedStrategy.from_strategy_package(
                self.releases / package_id, trusted=True)
        return self.loaded[package_id]

    def _bind(self, owner, package_id, account):
        view = self.views[owner]
        strategy = self.load(package_id)
        assets = StrategyAssets(self.adapter, self.store, clock=self.clock)
        instance = AlpacaInstance(self.root, view.binding(), package_id, strategy.spec,
            None, self.runner, StrategyData(assets, clock=self.clock), clock=self.clock)
        allocation = instance.allocation(account)
        execution = AlpacaDecisionExecution(self.runner.executor, assets, strategy.spec,
                                           view.binding(), allocation, clock=self.clock)
        instance.runtime = InstanceDecisionRuntime(strategy, view, execution, allocation=allocation)
        self.instances[owner] = instance

    @property
    def symbols(self):
        symbols = {symbol for instance in self.instances.values() for symbol in instance.spec.symbols}
        symbols.update(symbol for native in self.native.values() for symbol in native.strategy.spec.symbols)
        symbols.update(symbol for symbol, in self.store.db.execute(
            'SELECT DISTINCT symbol FROM portfolio_claims WHERE account=?',
            (self.adapter.read_account_details().account_id,)))
        return tuple(sorted(symbols))

    def corporate_actions(self, session, account_id):
        first = self.store.db.execute('SELECT MIN(session) FROM portfolio_plans WHERE account=?',
                                      (account_id,)).fetchone()[0]
        since = first or session
        announcements = self.adapter.http.request('GET', '/v2/corporate_actions/announcements',
            params={'ca_types': 'dividend,merger,spinoff,split', 'since': since,
                    'until': session, 'date_type': 'ex_date'})
        if len(announcements) >= 1000:
            raise PreparationIncomplete('SATURATED_CORPORATE_ACTION_WINDOW')
        receipts = self.adapter.read_corporate_activities(after=since + 'T00:00:00Z')
        return CorporateReceiptJournal(self.store, account_id).reconcile_account(
            self.runner.executor, announcements=announcements, receipts=receipts)

    def tick(self, service, window, at, facts):
        from zoneinfo import ZoneInfo
        service.cache = self.cache
        account = self.adapter.recover()
        for owner, view in self.views.items():
            if owner not in self.instances and owner not in self.native:
                pending = view.pending()
                package = view.snapshot()['active'] or (pending['new'] if pending else None)
                if package is not None:
                    self.attach(owner, package, account)
        blocked = self.corporate_actions(at.astimezone(ZoneInfo('America/New_York')).date().isoformat(),
                                         facts.account_id)
        for owner, native in self.native.items():
            if service.client.feed != 'iex':
                raise ValueError('original V69 input provenance requires IEX')
            affected = set(native.strategy.spec.symbols) & blocked.keys()
            if affected:
                native.state.update(state='PREPARATION_INCOMPLETE',
                                    reason={symbol: blocked[symbol] for symbol in affected})
            else:
                native.tick(service, window, at, facts)
            self.state['instances'][owner] = dict(native.state)
        for owner, instance in tuple(self.instances.items()):
            affected = set(instance.spec.symbols) & blocked.keys()
            if affected:
                instance.state.update(state='PREPARATION_INCOMPLETE',
                                      reason={symbol: blocked[symbol] for symbol in affected})
                self.state['instances'][owner] = dict(instance.state)
                continue
            view, controller = self.views[owner], self.controllers[owner]
            if view.pending() is not None or view.activation_callback() is not None:
                for receipt in view.outstanding(instance.package_id):
                    instance.runtime.adapter.execute(receipt)
                    view.acknowledge(receipt)
                snapshot = controller.step()
                controller.adapter.instance = instance
                if view.pending() is not None or view.activation_callback() is not None:
                    instance.state.update(state='SWITCHING', switch=snapshot['switch'])
                    self.state['instances'][owner] = dict(instance.state)
                    continue
                if snapshot['active'] != instance.package_id:
                    self._bind(owner, snapshot['active'], account)
                    instance = self.instances[owner]
                    controller.adapter.instance = instance
            required_feed = instance.spec.data.get('feed')
            if required_feed is not None and required_feed != service.client.feed:
                raise ValueError('strategy requires its declared real market-data feed')
            instance.tick(service, at, account)
            self.state['instances'][owner] = dict(instance.state)
        blocked_states = {'PREPARATION_INCOMPLETE', 'PREVIOUS_TASK_RECONCILIATION_REQUIRED',
                          'WAITING_PACKAGE_ACTIVATION', 'SWITCHING'}
        blocked_instances = sorted(owner for owner, state in self.state['instances'].items()
                                   if state['state'] in blocked_states)
        self.state['state'] = 'BLOCKED_INSTANCES' if blocked_instances else 'RUNNING'
        self.execution.update(state=self.state['state'], blocked_instances=blocked_instances)

    def report(self):
        from dataclasses import asdict
        account_id = self.adapter.read_account_details().account_id
        reports = {}
        for owner, view in self.views.items():
            row = self.store.db.execute(
                'SELECT id FROM portfolio_plans WHERE account=? AND strategy=? ORDER BY rowid DESC LIMIT 1',
                (account_id, owner)).fetchone()
            reports[owner] = dict(binding=view.binding(), deployment=view.snapshot(),
                state=self.state['instances'].get(owner),
                cash=self.store.cash(account_id, owner), positions=self.store.holdings(account_id, owner),
                task=None if row is None else row[0],
                accounting=None if row is None else asdict(self.runner.report(row[0])))
        return reports

    def close(self):
        for native in self.native.values():
            native.close()
        for view in self.views.values():
            view.close()
        self.deployments.close()
        self.store.close()
