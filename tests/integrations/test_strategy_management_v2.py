"""Offline packages, instance switches, state migration and receipt recovery."""
from contextlib import ExitStack, closing
from dataclasses import asdict
from datetime import datetime, timezone, timedelta
from decimal import Decimal as D
import json
import sqlite3

import pytest

from factorlab.strategies.package import build_package, install_package, load_package, encoded, digest
from factorlab.strategies.loader import LoadedStrategy, StrategyWorkerError
from factorlab.strategies.protocol import V2
from factorlab.strategies.deployment import DeploymentStore, DeploymentController, OfflineAdapter, LEGACY
from factorlab.strategies.decision_runtime import InstanceDecisionRuntime
from factorlab.strategies.events import validate_event
from test_strategy_protocol_v2 import spec

NOW = datetime(2026, 11, 25, tzinfo=timezone.utc)


def package(tmp_path, version='2', *, kind='quantity_deltas', migration='migrate', from_versions=('1',)):
    root = tmp_path / ('source-' + version)
    root.mkdir()
    declaration = spec(kind, version=version, state=dict(version=version, migration=migration, from_versions=list(from_versions)))
    source = '''from decimal import Decimal
import os
from factorlab.strategies.protocol import StrategySpecV2, DecisionV2
class Portfolio:
    def describe(self):
        return StrategySpecV2(**SPEC)
    def decide(self, event, state):
        if event['account'].get('fail'):
            raise ValueError('strategy failure')
        if event['account'].get('check_environment'):
            assert 'FACTORLAB_TEST_CREDENTIAL' not in os.environ
        return DecisionV2(KIND, targets={'AAPL': Decimal('-2'), 'MSFT': Decimal('3')},
            state=dict(state, decisions=state.get('decisions', 0) + 1))
    def migrate_state(self, state, from_version):
        return dict(state, migrations=state.get('migrations', 0) + 1, migrated_from=from_version)
'''.replace('SPEC', repr(asdict(declaration))).replace('KIND', repr(kind))
    (root / 'strategy.py').write_text(source, encoding='utf-8')
    data = asdict(declaration)
    requirements = {k: data[k] for k in ('data', 'schedule', 'state', 'capital', 'execution')}
    requirements['capital'] = dict(requirements['capital'], requested=str(requirements['capital']['requested']))
    manifest = dict(schema_version=2, interface=V2, strategy_id=declaration.strategy_id,
        strategy_version=version, market=declaration.market, account_id=declaration.account_id,
        symbols=declaration.symbols, execution_mode=kind, requirements=requirements,
        code_commit='a' * 40, entrypoint='strategy:Portfolio', files={'strategy.py': ''},
        configuration={}, dependencies=[], evidence={'record': version}, migration='wait_flat')
    archive = tmp_path / (version + '.zip')
    manifest = build_package(root, manifest, archive)
    release, installed = install_package(archive, tmp_path / 'releases')
    assert installed == manifest
    return manifest, release


def binding(instance):
    return dict(market='ALPACA_US_EQUITY', account_id='paper', instance_id=instance,
                allocation_id='allocation-' + instance, strategy_id='portfolio')


def candidate(manifest, instance, expected=None, selection='selection'):
    return dict(schema='FACTORLAB_CHAMPION_PROMOTION_V2', binding=binding(instance),
        selection_id=selection, decision='PROMOTE', qualified=True, strictly_better=True,
        expected_active=expected, package_id=manifest['package_id'], evidence=manifest['evidence'])


def event(instance, event_id='bar:1', **account):
    observation = dict(observed_at=NOW, effective_at=NOW, source='offline', payload={'close': D('100.01')})
    return dict(protocol=V2, binding=binding(instance), event_id=event_id, event_time=NOW,
        data_type='closed_bar', symbols={symbol: dict(watermark=event_id,
            observations={'closed_bar': observation}, history={}) for symbol in ['AAPL', 'MSFT']},
        account=account, allocation={'currency': 'USD', 'amount': D(1000)})


class Execution:
    def __init__(self):
        self.intents = {}
        self.calls = 0
        self.fail_after_first = False

    def qualify(self, declaration, bound, allocation):
        declaration.require_binding(bound)
        assert allocation['currency'] == 'USD'

    def execute(self, receipt):
        self.calls += 1
        for symbol, delta in receipt['decision']['targets'].items():
            key = (receipt['binding']['instance_id'], receipt['package_id'], receipt['event_id'], symbol)
            self.intents.setdefault(key, delta)
            if self.fail_after_first:
                self.fail_after_first = False
                raise OSError('crash after first durable intent')


def activate(store, manifest, release, instance):
    store.register_instance(binding(instance))
    view = store.for_instance(instance)
    view.request(manifest, candidate(manifest, instance))
    strategy = LoadedStrategy.from_strategy_package(release, trusted=True)
    adapter = OfflineAdapter()
    controller = DeploymentController(view, adapter, load_strategy=lambda _: strategy)
    assert controller.step()['active'] == manifest['package_id']
    return view, strategy, adapter, controller


def test_two_instances_load_decide_and_recover_independently(tmp_path, monkeypatch):
    manifest, release = package(tmp_path)
    monkeypatch.setenv('FACTORLAB_TEST_CREDENTIAL', 'must-not-be-in-worker')
    with ExitStack() as stack:
        store = stack.enter_context(closing(DeploymentStore(tmp_path / 'deploy.db')))
        one, strategy, _, _ = activate(store, manifest, release, 'one')
        two, strategy_two, _, _ = activate(store, manifest, release, 'two')
        stack.enter_context(closing(one)); stack.enter_context(closing(two))
        execution = Execution()
        runtime = InstanceDecisionRuntime(strategy, one, execution, allocation={'currency': 'USD'})
        execution.fail_after_first = True
        with pytest.raises(OSError):
            runtime.decide(event('one', check_environment=True))
        assert one.checkpoint() == {'decisions': 1}
        assert two.checkpoint() == {}
        assert len(execution.intents) == 1
        one.close()
        one = stack.enter_context(closing(store.for_instance('one')))
        runtime = InstanceDecisionRuntime(strategy, one, execution, allocation={'currency': 'USD'})
        runtime.recover()
        assert len(execution.intents) == 2
        receipt = runtime.decide(event('one', check_environment=True))
        assert receipt['decision']['targets'] == {'AAPL': D(-2), 'MSFT': D(3)}
        assert one.checkpoint() == {'decisions': 1}
        assert execution.calls == 2
        other_execution = Execution()
        other = InstanceDecisionRuntime(strategy_two, two, other_execution, allocation={'currency': 'USD'})
        other.decide(event('two'))
        assert len(other_execution.intents) == 2
        assert one.deployment_id_for(manifest['package_id'], 'test') != two.deployment_id_for(manifest['package_id'], 'test')
        with pytest.raises(ValueError, match='identity conflict'):
            runtime.decide(event('one', check_environment=False))
        with pytest.raises(ValueError, match='wrong instance'):
            runtime.decide(event('two'))


def test_parallel_switches_migration_restart_and_rollback(tmp_path):
    first, release = package(tmp_path)
    second, release_two = package(tmp_path, '3', from_versions=('2',))
    with ExitStack() as stack:
        store = stack.enter_context(closing(DeploymentStore(tmp_path / 'deploy.db')))
        one, strategy, adapter, _ = activate(store, first, release, 'one')
        two, strategy_two, adapter_two, _ = activate(store, first, release, 'two')
        stack.enter_context(closing(one)); stack.enter_context(closing(two))
        InstanceDecisionRuntime(strategy, one, Execution(), allocation={'currency': 'USD'}).decide(event('one'))
        one.request(second, candidate(second, 'one', first['package_id'], 'upgrade'), mode='restart', boot=adapter.boot_id)
        two.request(second, candidate(second, 'two', first['package_id'], 'upgrade'))
        newer = LoadedStrategy.from_strategy_package(release_two, trusted=True)
        load = lambda version: newer if version == second['package_id'] else strategy
        control_one = DeploymentController(one, adapter, load_strategy=load)
        assert control_one.step()['switch']['phase'] == 'READY_RESTART'
        assert two.pending()['phase'] == 'REQUESTED'
        adapter.boot_id = 'offline-2'
        assert control_one.step()['active'] == second['package_id']
        assert one.checkpoint() == {'decisions': 1, 'migrations': 1, 'migrated_from': '2'}
        assert two.checkpoint() == {}
        control_two = DeploymentController(two, adapter_two, load_strategy=load)
        adapter_two.failure = 'strategy'
        assert control_two.step()['switch']['phase'] == 'ROLLBACK'
        adapter_two.failure = None
        assert control_two.step()['active'] == first['package_id']
        assert one.snapshot()['active'] == second['package_id']
        # Recovery starts with committed state, not a second migration.
        recovered = stack.enter_context(closing(store.for_instance('one')))
        assert recovered.checkpoint()['migrations'] == 1
        runtime = InstanceDecisionRuntime(newer, recovered, Execution(), allocation={'currency': 'USD'})
        runtime.decide(event('one', 'bar:2'))
        with pytest.raises(StrategyWorkerError):
            runtime.decide(event('one', 'bar:3', fail=True))
        assert recovered.pending()['phase'] == 'ROLLBACK'
        assert two.pending() is None
        rollback = DeploymentController(recovered, adapter, load_strategy=load)
        assert rollback.step()['active'] == first['package_id']
        assert recovered.checkpoint() == {'decisions': 1}


def test_migration_receipt_reused_after_prepare_crash(tmp_path):
    first, release = package(tmp_path)
    second, release_two = package(tmp_path, '3', from_versions=('2',))
    with closing(DeploymentStore(tmp_path / 'deploy.db')) as store:
        view, strategy, adapter, _ = activate(store, first, release, 'one')
        with closing(view):
            runtime = InstanceDecisionRuntime(strategy, view, Execution(), allocation={'currency': 'USD'})
            runtime.decide(event('one'))
            view.request(second, candidate(second, 'one', first['package_id'], 'upgrade'))
            switch = view.pending()
            newer = LoadedStrategy.from_strategy_package(release_two, trusted=True)
            first_result = view.migrated_checkpoint(switch, newer)
            # A second loader process reads the receipt and cannot apply the transform again.
            again = LoadedStrategy.from_strategy_package(release_two, trusted=True)
            assert view.migrated_checkpoint(switch, again) == first_result
            assert first_result['migrations'] == 1
            control = DeploymentController(view, adapter, load_strategy=lambda _: again)
            assert control.step()['checkpoint'] == first_result


def test_binding_and_promotion_mismatches_are_rejected(tmp_path):
    manifest, _ = package(tmp_path)
    with closing(DeploymentStore(tmp_path / 'deploy.db')) as store:
        store.register_instance(binding('one'))
        store.register_instance(binding('one'))
        with pytest.raises(ValueError, match='immutable'):
            store.register_instance(dict(binding('one'), allocation_id='other'))
        with closing(store.for_instance('one')) as view:
            for field, value in [('market', 'BINANCE_USDM'), ('account_id', 'other'), ('instance_id', 'two'), ('allocation_id', 'other')]:
                promotion = candidate(manifest, 'one')
                promotion['binding'] = dict(promotion['binding'], **{field: value})
                with pytest.raises(ValueError):
                    view.request(manifest, promotion)
            assert view.snapshot()['active'] is None
            assert view.pending() is None


def test_failure_before_activation_retains_old_checkpoint(tmp_path):
    first, release = package(tmp_path)
    second, release_two = package(tmp_path, '3', from_versions=('2',))
    with closing(DeploymentStore(tmp_path / 'deploy.db')) as store:
        view, strategy, adapter, _ = activate(store, first, release, 'one')
        with closing(view):
            InstanceDecisionRuntime(strategy, view, Execution(), allocation={'currency': 'USD'}).decide(event('one'))
            request = candidate(second, 'one', first['package_id'], 'upgrade')
            switch_id = view.request(second, request)
            newer = LoadedStrategy.from_strategy_package(release_two, trusted=True)
            control = DeploymentController(view, adapter,
                load_strategy=lambda version: newer if version == second['package_id'] else strategy)
            adapter.failure = 'strategy'
            control.step()
            assert view.pending()['phase'] == 'ROLLBACK'
            adapter.failure = None
            assert control.step()['active'] == first['package_id']
            assert view.checkpoint() == {'decisions': 1}
            assert view.request(second, request) == switch_id


def test_first_activation_failure_does_not_activate_legacy(tmp_path):
    manifest, release = package(tmp_path)
    with closing(DeploymentStore(tmp_path / 'deploy.db')) as store:
        store.register_instance(binding('one'))
        with closing(store.for_instance('one')) as view:
            strategy = LoadedStrategy.from_strategy_package(release, trusted=True)
            view.request(manifest, candidate(manifest, 'one'))
            adapter = OfflineAdapter()
            control = DeploymentController(view, adapter, load_strategy=lambda _: strategy)
            adapter.failure = 'strategy'
            assert control.step()['switch']['phase'] == 'ROLLBACK'
            adapter.failure = None
            state = control.step()
            assert state['active'] is None
            assert state['switch']['reason'] == 'NO_FALLBACK_PACKAGE'
            assert not any(action == ('prepare', LEGACY) for action in adapter.actions)


def test_champion_router_and_order_attribution_are_instance_scoped(tmp_path):
    from factorlab.strategies.promotion import route_promotion
    manifest, _ = package(tmp_path)
    with ExitStack() as stack:
        store = stack.enter_context(closing(DeploymentStore(tmp_path / 'deploy.db')))
        store.register_instance(binding('one'))
        store.register_instance(binding('two'))
        first = route_promotion(store, candidate(manifest, 'one'), manifest)
        second = route_promotion(store, candidate(manifest, 'two'), manifest)
        assert first != second
        one = stack.enter_context(closing(store.for_instance('one')))
        two = stack.enter_context(closing(store.for_instance('two')))
        assert one.pending()['selection'] == two.pending()['selection'] == 'selection'
        one.record_order('one-order', manifest['package_id'], 'bar:1')
        one.record_order('one-order', manifest['package_id'], 'bar:1')
        assert one.order('one-order')['instance_id'] == 'one'
        assert two.order('one-order') is None
        with pytest.raises(ValueError, match='ownership'):
            one.record_order('one-order', manifest['package_id'], 'other-event')
        wrong = candidate(manifest, 'one')
        wrong['binding']['allocation_id'] = 'allocation-two'
        with pytest.raises(ValueError):
            route_promotion(store, wrong, manifest)
        assert store.snapshot()['active'] == LEGACY


def test_explicit_fallback_is_not_implicitly_legacy(tmp_path):
    fallback, old_release = package(tmp_path, '1')
    manifest, release = package(tmp_path, '2')
    with closing(DeploymentStore(tmp_path / 'deploy.db')) as store:
        store.register_package(fallback)
        store.register_instance(binding('one'), fallback_package_id=fallback['package_id'])
        with closing(store.for_instance('one')) as view:
            current = LoadedStrategy.from_strategy_package(release, trusted=True)
            old = LoadedStrategy.from_strategy_package(old_release, trusted=True)
            adapter = OfflineAdapter()
            def prepare(version):
                if version == manifest['package_id']:
                    raise ValueError('invalid new strategy')
                adapter.actions.append(('prepare', version))
            adapter.prepare = prepare
            view.request(manifest, candidate(manifest, 'one'))
            controller = DeploymentController(view, adapter,
                load_strategy=lambda version: current if version == manifest['package_id'] else old)
            assert controller.step()['switch']['new'] == fallback['package_id']
            assert controller.step()['active'] == fallback['package_id']
            assert view.checkpoint() == {}


def test_market_qualification_callback_does_not_grant_foreign_authority(tmp_path):
    from factorlab.integrations.binance_futures_trading.strategy_package import load_package as binance_load
    manifest, release = package(tmp_path)
    calls = []
    assert load_package(release, qualify=lambda m: calls.append(m['market'])) == manifest
    assert calls == ['ALPACA_US_EQUITY']
    with pytest.raises(ValueError, match='Binance'):
        binance_load(release, qualify=lambda _: None)


def test_native_adoption_preserves_checkpoint_and_does_not_rewind(tmp_path):
    manifest, release = package(tmp_path)
    evidence = dict(journal_id='native-ledger', source_code_sha256='b' * 64,
                    latest_plan_id='original-plan', signal_snapshot='frozen.json')
    calls = []
    def verify(m, bound, checkpoint, watermark, proof):
        assert bound == binding('one')
        assert checkpoint == {'decisions': 4}
        assert watermark == 'native:4'
        assert proof == evidence
        calls.append(m['package_id'])
    with closing(DeploymentStore(tmp_path / 'deploy.db')) as store:
        store.register_instance(binding('one'))
        with closing(store.for_instance('one')) as view:
            state = view.adopt_existing(manifest, {'decisions': 4}, 'native:4', evidence, verify=verify)
            assert state['active'] == manifest['package_id']
            assert state['switch'] is None
            assert calls == [manifest['package_id']]
            strategy = LoadedStrategy.from_strategy_package(release, trusted=True)
            runtime = InstanceDecisionRuntime(strategy, view, Execution(), allocation={'currency': 'USD'})
            runtime.decide(event('one'))
            assert view.checkpoint() == {'decisions': 5}
            assert view.adopt_existing(manifest, {'decisions': 4}, 'native:4', evidence,
                                       verify=verify)['checkpoint'] == {'decisions': 5}
            assert len(calls) == 1
            with pytest.raises(ValueError, match='identity conflict'):
                view.adopt_existing(manifest, {}, 'native:4', evidence, verify=verify)


def test_native_adoption_verification_failure_is_atomic(tmp_path):
    manifest, _ = package(tmp_path)
    evidence = dict(journal_id='native', source_code_sha256='c' * 64)
    def reject(*_):
        raise ValueError('host rejected code/journal evidence')
    with closing(DeploymentStore(tmp_path / 'deploy.db')) as store:
        store.register_instance(binding('one'))
        with closing(store.for_instance('one')) as view:
            with pytest.raises(ValueError, match='host rejected'):
                view.adopt_existing(manifest, {}, 'native:1', evidence, verify=reject)
            assert view.snapshot()['active'] is None
            assert view.pending() is None
            assert view.db.execute('SELECT count(*) FROM instance_adoptions').fetchone()[0] == 0
            view.request(manifest, candidate(manifest, 'one'))
            with pytest.raises(ValueError, match='pristine'):
                view.adopt_existing(manifest, {}, 'native:1', evidence, verify=lambda *_: None)


def test_causal_funding_forecast_and_settlement_are_distinct():
    declaration = spec(data=dict(data_types=['closed_bar', 'funding_forecast', 'funding_settlement'],
                                 history_limit=2, historical_observations=False))
    payload = event('one')
    future = dict(observed_at=NOW, effective_at=NOW + timedelta(hours=1), source='offline', payload={'rate': D('.001')})
    payload['symbols']['AAPL']['observations']['funding_forecast'] = future
    validate_event(payload, declaration)
    payload['symbols']['AAPL']['observations']['funding_settlement'] = future
    with pytest.raises(ValueError, match='noncausal'):
        validate_event(payload, declaration)


def test_unmodified_legacy_database_is_preserved(tmp_path):
    path = tmp_path / 'legacy.db'
    # The old implementation creates the historical layout; keep it as a fixture,
    # rather than deriving the migration fixture from the new schema.
    db = sqlite3.connect(path)
    db.executescript('''
      CREATE TABLE deployment(id INTEGER PRIMARY KEY CHECK(id=1), active TEXT, stable TEXT,
        previous TEXT, checkpoint TEXT NOT NULL DEFAULT '{}', watermark TEXT);
      INSERT INTO deployment VALUES(1,'legacy-grid','legacy-grid',NULL,'{"counter":7}','bar:old');
      CREATE TABLE switches(id INTEGER PRIMARY KEY, selection TEXT UNIQUE, old TEXT, new TEXT,
        phase TEXT, mode TEXT, policy TEXT, watermark TEXT, boot TEXT, reason TEXT, created REAL, updated REAL);
      INSERT INTO switches VALUES(19,'old-selection','old-package','legacy-grid','SUCCEEDED','hot','wait_flat',
        'bar:old','boot',NULL,1,2);
      CREATE TABLE strategy_orders(client_id TEXT PRIMARY KEY, version TEXT, watermark TEXT, reduce_only INTEGER NOT NULL);
      INSERT INTO strategy_orders VALUES('historical-order','old-package','bar:old',0);
      CREATE TABLE decisions(version TEXT,watermark TEXT,result TEXT,PRIMARY KEY(version,watermark));
      INSERT INTO decisions VALUES('old-package','bar:old','{"state":{"counter":7}}');
      CREATE TABLE deployment_bindings(package_id TEXT PRIMARY KEY,deployment_id TEXT UNIQUE NOT NULL);
      INSERT INTO deployment_bindings VALUES('old-package','legacy-s12345');
    ''')
    tables = ['deployment', 'switches', 'strategy_orders', 'decisions', 'deployment_bindings']
    before = {table: db.execute('SELECT * FROM ' + table).fetchall() for table in tables}
    db.close()
    for _ in range(2):
        with closing(DeploymentStore(path)) as store:
            assert store.snapshot()['checkpoint'] == {'counter': 7}
            assert store.snapshot()['switch']['id'] == 19
            assert store.instances() == [dict(instance_id=LEGACY, binding=None, fallback_package_id=LEGACY)]
            after = {table: [tuple(row) for row in store.db.execute('SELECT * FROM ' + table)] for table in tables}
            assert after == before
