"""Shared-package multi-instance host; one existing Alpaca execution journal."""
from decimal import Decimal as D
from pathlib import Path
from uuid import uuid4

from factorlab.strategies.decision_runtime import InstanceDecisionRuntime
from factorlab.strategies.deployment import DeploymentController, DeploymentStore
from factorlab.strategies.loader import LoadedStrategy, StrategyPackageError
from factorlab.strategies.package import manifest_spec
from factorlab.trading.portfolio_execution import PortfolioStore
from .http import AlpacaError
from .adapter import OrderNotFound
from .portfolio import AlpacaPortfolioRunner
from .strategy_assets import StrategyAssetIneligible, StrategyAssets
from .strategy_data import StrategyData
from .strategy_deployment import AlpacaDeployment
from .strategy_execution import AlpacaDecisionExecution
from .strategy_runtime import AlpacaInstance
from .corporate_actions import CorporateReceiptJournal
from .cloud_execution import PreparationIncomplete
from .market_data.cache import MarketCache


class StrategyInstances:
    def __init__(self, root, adapter, configuration, *, clock, revision='unknown', reference_provider=None):
        self.root, self.adapter, self.clock = Path(root), adapter, clock
        self.reference_provider = reference_provider
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
        self.capital.pending_commitments = self.pending_commitments
        self.loaded = {}
        self.identity = None
        self.state = {'state': 'RECOVERING', 'instances': {}}
        self.execution = {'enabled': True, 'state': 'RECOVERING'}
        account = adapter.recover()
        facts = adapter.read_account_details()
        policy = configuration.get('capital_policy')
        if policy is not None:
            self.store.capital_policy.configure(account_id=facts.account_id,
                currency=policy['currency'], budget=D(policy['budget']), fee_rate=D(policy['fee_rate']),
                mode=policy.get('mode'))
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
            if policy is not None:
                self.store.capital_policy.bind(account_id=facts.account_id, strategy_id=owner,
                    instance_id=owner, allocation_id=binding['allocation_id'],
                    logical_strategy=binding['strategy_id'])
            if self.store.capital_balance(facts.account_id, owner)[3]:
                self.state['instances'][owner] = {'state': 'RETIRED'}
                continue
            if registration.get('native_v69', False):
                from .native_v69 import NativeWeeklyExecution
                native = NativeWeeklyExecution(self.root, adapter, clock=clock, revision=revision,
                    runner=self.runner, deployments=self.deployments)
                if binding != native.view.binding():
                    raise ValueError('native V69 registration must preserve its original binding')
                if policy is not None:
                    self.store.capital_policy.qualify(native.strategy.spec, binding)
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
        spec = manifest_spec(view.manifest(package_id))
        assets = StrategyAssets(self.adapter, self.store, clock=self.clock)
        data = StrategyData(assets, clock=self.clock, reference_provider=self.reference_provider)
        instance = AlpacaInstance(self.root, view.binding(), package_id, spec,
            None, self.runner, data, clock=self.clock)
        try:
            strategy = self.load(package_id)
            allocation = instance.allocation(account)
            allocation['capital'] = self.store.allocation_basis(account.account_id, owner)['capital']
            execution = AlpacaDecisionExecution(self.runner.executor, assets, spec,
                                               view.binding(), allocation, clock=self.clock)
            instance.runtime = InstanceDecisionRuntime(strategy, view, execution, allocation=allocation)
        except (StrategyPackageError, StrategyAssetIneligible) as error:
            instance.state.update(state='PACKAGE_BLOCKED', reason=str(error))
        self.instances[owner] = instance
        self.state['instances'][owner] = dict(instance.state)
        return instance

    @property
    def capital(self):
        return self.runner.executor.capital

    def pending_commitments(self, account_id):
        # The capital command holds the portfolio writer lock. Orders already
        # materialized by a plan reserve their exact residual through store.cash.
        pending = self.store.unprepared_buy_owners(account_id)
        for owner, view in self.views.items():
            if owner in pending or view.binding()['account_id'] != account_id:
                continue
            active = view.snapshot()['active']
            if active is None:
                continue
            execution = (self.native[owner].runtime.adapter if owner in self.native else AlpacaDecisionExecution)
            for receipt in view.outstanding(active):
                if self.store.plan(execution.plan_id(receipt)) is None:
                    pending.add(owner)
                    break
        return dict.fromkeys(pending)

    @property
    def symbols(self):
        symbols = {symbol for instance in self.instances.values() if instance.runtime is not None
                   for symbol in instance.spec.symbols}
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

    def _tick_instance(self, owner, instance, service, at, account):
        view, controller = self.views[owner], self.controllers[owner]
        controller.adapter.market_service = service
        if instance.runtime is not None:
            instance.runtime.adapter.bind_market(service, instance.data)
        if view.pending() is not None or view.activation_callback() is not None:
            outstanding = view.outstanding(instance.package_id)
            if instance.runtime is None and outstanding:
                return instance
            for receipt in outstanding:
                instance.runtime.adapter.execute(receipt)
                view.acknowledge(receipt)
            snapshot = controller.step()
            if view.pending() is not None or view.activation_callback() is not None:
                instance.state.update(state='SWITCHING', switch=snapshot['switch'])
                return instance
            if snapshot['active'] != instance.package_id or instance.runtime is None:
                instance = self._bind(owner, snapshot['active'], account)
                controller.adapter.instance = instance
                if instance.runtime is not None:
                    instance.runtime.adapter.bind_market(service, instance.data)
        if instance.runtime is None:
            snapshot = view.snapshot()
            if snapshot['previous'] is not None and not view.outstanding(instance.package_id):
                view.rollback('PACKAGE_BLOCKED', expected_active=instance.package_id)
            return instance
        required_feed = instance.spec.data.get('feed')
        if required_feed is not None and required_feed != service.client.feed:
            instance.state.update(state='PREPARATION_INCOMPLETE',
                                  reason='strategy requires its declared Alpaca feed: ' + required_feed)
            return instance
        instance.tick(service, at, account)
        return instance

    def tick(self, service, window, at, facts):
        from zoneinfo import ZoneInfo
        from factorlab.trading.capital_cli import process_command
        service.cache = self.cache
        if self.store.portfolio_risk.config(facts.account_id) is None:
            account = self.adapter.recover()
        else:
            account = self.runner.reconcile_account(facts.account_id)
        blocked = self.corporate_actions(at.astimezone(ZoneInfo('America/New_York')).date().isoformat(),
                                         facts.account_id)
        if not blocked:
            process_command(self.capital, facts.account_id, operating_errors=(AlpacaError,))
        retired = {owner for owner in self.views if self.store.capital_balance(facts.account_id, owner)[3]}
        for owner, view in self.views.items():
            if owner in retired:
                self.state['instances'][owner] = {'state': 'RETIRED'}
                continue
            if owner not in self.instances and owner not in self.native:
                pending = view.pending()
                package = view.snapshot()['active'] or (pending['new'] if pending else None)
                if package is not None:
                    self.attach(owner, package, account)
        self.runner.executor.enforce_portfolio_risk(facts.account_id, account=account)
        for owner, native in self.native.items():
            if owner in retired:
                continue
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
            if owner in retired:
                continue
            affected = set(instance.spec.symbols) & blocked.keys()
            if affected:
                instance.state.update(state='PREPARATION_INCOMPLETE',
                                      reason={symbol: blocked[symbol] for symbol in affected})
                self.state['instances'][owner] = dict(instance.state)
                continue
            instance = self._tick_instance(owner, instance, service, at, account)
            self.state['instances'][owner] = dict(instance.state)
        blocked_states = {'PREPARATION_INCOMPLETE', 'PREVIOUS_TASK_RECONCILIATION_REQUIRED',
                          'WAITING_PACKAGE_ACTIVATION', 'SWITCHING', 'PACKAGE_BLOCKED',
                          'STRATEGY_FAILED', 'WAITING_MODEL_EFFECTIVE'}
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
