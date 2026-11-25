"""Adopt the original V69 journal into shared package decisions without new order IDs."""
from dataclasses import asdict
from decimal import Decimal as D
import json
import os

from factorlab.strategies.decision_runtime import InstanceDecisionRuntime
from factorlab.strategies.deployment import DeploymentController, DeploymentStore
from factorlab.strategies.loader import LoadedStrategy
from factorlab.strategies.package import build_package, install_package
from factorlab.strategies.v69 import ASSETS
from factorlab.strategies.v69_alpaca import create_alpaca_plan
from factorlab.strategies.v69_runtime import V69AlpacaStrategy
from factorlab.trading.storage import decode, dumps
from factorlab.trading.portfolio_execution import plan_identity
from .cloud_execution import PreparationIncomplete, WeeklyExecution
from .strategy_deployment import AlpacaDeployment


class NativeDeployment(AlpacaDeployment):
    def prepare(self, version):
        strategy = self.instance.load(version)
        NativeExecution(self.instance).qualify(strategy.spec, self.instance.binding, {})

    def activated(self, version):
        self.instance.bind_package(version)
        super().activated(version)


def freeze(path, value):
    temporary = path.with_suffix('.tmp')
    with temporary.open('w', encoding='utf-8') as output:
        output.write(dumps(value) + '\n')
        output.flush()
        os.fsync(output.fileno())
    temporary.replace(path)


class NativeExecution:
    def __init__(self, owner):
        self.owner = owner

    def qualify(self, spec, binding, allocation):
        spec.require_binding(binding)
        if spec != V69AlpacaStrategy(binding['account_id']).describe():
            raise ValueError('native V69 declaration must preserve original execution semantics')
        if binding['instance_id'] != 'V69_CAUSAL' or binding['allocation_id'] != 'v69-original':
            raise ValueError('original V69 execution binding required')
        if self.owner.runner.executor.capital_policy.is_configured(binding['account_id']):
            self.owner.runner.executor.capital_policy.qualify(spec, binding, allocation)

    def execute(self, receipt):
        owner = self.owner
        if receipt['binding'] != owner.view.binding():
            raise ValueError('native V69 receipt binding mismatch')
        original = receipt['decision']['state']['original']
        inputs = dict(receipt['event']['context']['bridge_inputs'], decision=original)
        identity = plan_identity(inputs['account_id'], 'V69_CAUSAL', inputs['session'], inputs['session'])
        if not owner.store.db.execute('SELECT 1 FROM portfolio_plans WHERE id=?', (identity,)).fetchone():
            account = owner.adapter.recover()
            inventory = (owner.runner.executor.projection(account, 'V69_CAUSAL')
                         if owner.store.allocation(inputs['account_id'], 'V69_CAUSAL') is not None else account)
            actual = {p.instrument_id: p.quantity for p in inventory.positions
                      if p.instrument_id in ASSETS and p.quantity}
            expected = {symbol: D(str(quantity)) for symbol, quantity in inputs['raw_holdings'].items() if quantity}
            if not account.reconciled or actual != expected:
                raise PreparationIncomplete('FROZEN_PREPARATION_HOLDINGS_CHANGED')
        owner.identity = create_alpaca_plan(owner.runner.executor, now=owner.clock(), **inputs)


class NativeWeeklyExecution(WeeklyExecution):
    def __init__(self, root, adapter, *, clock, revision, runner=None, deployments=None):
        super().__init__(root, adapter, clock=clock, runner=runner)
        self.owns_deployments = deployments is None
        facts = adapter.read_account_details()
        declaration = V69AlpacaStrategy(facts.account_id).describe()
        source = root / 'native-v69-source'
        source.mkdir(mode=0o700, exist_ok=True)
        entry = source / 'strategy.py'
        content = 'from factorlab.strategies.v69_runtime import V69AlpacaStrategy as Strategy\n'
        fields = asdict(declaration)
        requirements = {key: fields[key] for key in ('data', 'schedule', 'state', 'capital', 'execution')}
        requirements['capital']['requested'] = '0'
        archive = root / ('native-v69-' + revision + '.zip')
        manifest = dict(schema_version=2, interface='FACTORLAB_DECISION_V2',
            strategy_id=declaration.strategy_id, strategy_version=declaration.version,
            market=declaration.market, account_id=facts.account_id, symbols=list(ASSETS),
            execution_mode='quantity_deltas', requirements=requirements, code_commit=revision,
            entrypoint='strategy:Strategy', files={'strategy.py': ''},
            configuration={'account_id': facts.account_id}, dependencies=[],
            evidence={'native_strategy': 'V69_CAUSAL', 'journal': 'portfolio.sqlite3'},
            migration='wait_flat')
        if not archive.exists():
            entry.write_text(content, encoding='utf-8')
            build_package(source, manifest, archive)
        release, manifest = install_package(archive, root / 'strategy-releases')
        self.strategy = LoadedStrategy.from_strategy_package(release, trusted=True)
        self.deployments = (DeploymentStore(root / 'deployments.sqlite3')
                            if deployments is None else deployments)
        binding = dict(market=declaration.market, account_id=facts.account_id,
            strategy_id='V69_CAUSAL', instance_id='V69_CAUSAL', allocation_id='v69-original')
        self.deployments.register_instance(binding)
        self.view = self.deployments.for_instance('V69_CAUSAL')
        last = self.last_plan(facts.account_id)
        checkpoint = {} if last is None else last['request']['signal_inputs']['checkpoint']
        watermark = '0' if last is None else last['request']['session']
        if self.view.snapshot()['active'] is None:
            evidence = dict(journal_id='portfolio.sqlite3', source_code_sha256=self.strategy.code_sha256,
                runtime_sha256=self.strategy.binding['runtime_sha256'], account_id=facts.account_id,
                strategy_id='V69_CAUSAL', latest_plan_id=None if last is None else last['id'])
            def verify(package, bound, state, mark, proof):
                current = self.last_plan(facts.account_id)
                if (bound != binding or package != manifest or proof != evidence
                        or state != {'checkpoint': checkpoint} or mark != watermark
                        or (None if current is None else current['id']) != proof['latest_plan_id']):
                    raise ValueError('native V69 adoption evidence changed')
                account = adapter.recover()
                if not account.reconciled or account.account_id != facts.account_id:
                    raise ValueError('matching reconciled native account required')
            self.view.adopt_existing(manifest, {'checkpoint': checkpoint}, watermark, evidence, verify=verify)
        self.releases = root / 'strategy-releases'
        self.loaded = {manifest['package_id']: self.strategy}
        self.bind_package(self.view.snapshot()['active'])
        self.controller = DeploymentController(self.view,
            NativeDeployment(self, self.view, boot_id=os.urandom(16).hex()), load_strategy=self.load)

    @property
    def binding(self):
        return self.view.binding()

    def plans(self):
        rows = self.store.db.execute(
            'SELECT id FROM portfolio_plans WHERE account=? AND strategy=? ORDER BY rowid',
            (self.binding['account_id'], self.binding['instance_id'])).fetchall()
        return [self.store.plan(identity) for identity, in rows]

    def load(self, package_id):
        if package_id not in self.loaded:
            self.loaded[package_id] = LoadedStrategy.from_strategy_package(
                self.releases / package_id, trusted=True)
        return self.loaded[package_id]

    def bind_package(self, package_id):
        self.strategy = self.load(package_id)
        self.package_id = package_id
        facts = self.adapter.read_account_details()
        self.runtime = InstanceDecisionRuntime(self.strategy, self.view, NativeExecution(self),
                                              allocation={'currency': 'USD', 'cash': facts.cash})

    def tick(self, service, window, at, facts):
        if self.view.pending() is not None or self.view.activation_callback() is not None:
            for receipt in self.view.outstanding(self.package_id):
                self.runtime.adapter.execute(receipt)
                self.view.acknowledge(receipt)
            snapshot = self.controller.step()
            if self.view.pending() is not None or self.view.activation_callback() is not None:
                self.state.update(state='SWITCHING', switch=snapshot['switch'])
                return
        return super().tick(service, window, at, facts)

    def event_path(self, session):
        directory = self.root / 'signals'
        directory.mkdir(mode=0o700, exist_ok=True)
        return directory / (session + '.native.json')

    def dispatch(self, event):
        receipt = self.runtime.decide(event)
        original = receipt['decision']['state']['original']
        inputs = event['context']['bridge_inputs']
        self.identity = (None if original['weights'] is None else
            plan_identity(inputs['account_id'], 'V69_CAUSAL', inputs['session'], inputs['session']))
        return original

    def decide(self, context, checkpoint, *, bridge_inputs):
        calendar = context['calendar']
        rows = [dict(date=str(day.date()), open=row.open.tz_convert('America/New_York').strftime('%H:%M'),
                     close=row.close.tz_convert('America/New_York').strftime('%H:%M'))
                for day, row in calendar.schedule.iterrows()]
        native = dict(calendar=rows, coverage_start=str(calendar.coverage_start.date()),
            coverage_end=str(calendar.coverage_end.date()), session=context['session'],
            raw_holdings=bridge_inputs['raw_holdings'], factors=bridge_inputs['factors'],
            quantity_steps=bridge_inputs['quantity_steps'])
        symbols = {}
        acquired = self.clock()
        for symbol in ASSETS:
            history = [dict(observed_at=acquired, effective_at=calendar.at(day, 'close').to_pydatetime(),
                source='ALPACA:iex', payload=dict(session=str(day.date()),
                    adjusted_close=float(context['close'].loc[day, symbol]),
                    adjusted_volume=float(context['volume'].loc[day, symbol])))
                for day in context['close'].index]
            symbols[symbol] = dict(watermark=context['session'], history={'closed_bar': history},
                                   observations={'closed_bar': history[-1]})
        event = self.envelope(bridge_inputs, native, symbols, D(str(context['cash'])), acquired)
        freeze(self.event_path(context['session']), event)
        return self.dispatch(event)

    def envelope(self, inputs, native, symbols, cash, acquired):
        return dict(protocol='FACTORLAB_DECISION_V2', binding=self.view.binding(),
            event_id=inputs['session'], event_time=acquired, data_type='closed_bar', symbols=symbols,
            account={'account_id': inputs['account_id'], 'market': self.strategy.spec.market},
            allocation={'currency': 'USD', 'cash': cash, 'positions': inputs['raw_holdings']},
            context={'native_v69': native, 'bridge_inputs': inputs})

    def resume_prepared(self, inputs):
        native = dict(frozen_original=inputs['decision'], raw_holdings=inputs['raw_holdings'],
                      factors=inputs['factors'], quantity_steps=inputs['quantity_steps'])
        symbols = {symbol: dict(watermark=inputs['session'], observations={}, history={}) for symbol in ASSETS}
        bridge = {key: value for key, value in inputs.items() if key != 'decision'}
        event = self.envelope(bridge, native, symbols, D(0), self.clock())
        path = self.event_path(inputs['session'])
        if path.exists():
            event = decode(json.loads(path.read_text(encoding='utf-8')))
        else:
            freeze(path, event)
        self.dispatch(event)
        return self.identity

    def prepare(self, service, window, at, facts, *, corporate_evidence=None):
        path = self.event_path(window['execution_session'])
        if path.exists():
            event = decode(json.loads(path.read_text(encoding='utf-8')))
            original = self.dispatch(event)
            inputs = dict(event['context']['bridge_inputs'], decision=original)
            freeze(self.root / 'signals' / (inputs['session'] + '.json'), inputs)
            return self.identity
        return super().prepare(service, window, at, facts, corporate_evidence=corporate_evidence)

    def close(self):
        self.view.close()
        if self.owns_deployments:
            self.deployments.close()
        super().close()
