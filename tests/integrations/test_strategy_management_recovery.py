"""Deterministic transaction interleavings and callback failure recovery."""
from contextlib import closing
from decimal import Decimal

import pytest

from factorlab.strategies.deployment import DeploymentStore, DeploymentController, OfflineAdapter
from factorlab.strategies.loader import LoadedStrategy
from factorlab.strategies.identity import content_identity
from factorlab.strategies.package import validate_manifest, digest
from factorlab.integrations.binance_futures_trading.strategy_deployment import DeploymentStore as BinanceStore
from factorlab.strategies.protocol import decision_payload, DecisionV2
from factorlab.strategies.decision_runtime import InstanceDecisionRuntime
from test_strategy_management_v2 import package, binding, candidate, event, activate, Execution


def test_event_identity_is_order_independent_and_type_preserving():
    assert content_identity({'a': {'x': 1, 'y': Decimal('2')}, 'b': True}) == content_identity(
        {'b': True, 'a': {'y': Decimal('2'), 'x': 1}})
    assert content_identity({'mode': 1}) != content_identity({'mode': True})


def test_receipt_sequence_rollback_and_checkpoint_revision(tmp_path):
    manifest, release = package(tmp_path)
    with closing(DeploymentStore(tmp_path / 'state.db')) as root:
        view, _, _, _ = activate(root, manifest, release, 'one')
        with closing(view):
            initial = view.snapshot()
            decision = decision_payload(DecisionV2('quantity_deltas', state={}))
            receipt = view.commit_decision(manifest['package_id'], 'a', 'input-a', decision,
                event=event('one', 'a'), expected_checkpoint={})
            with pytest.raises(ValueError, match='outstanding'):
                view.commit_decision(manifest['package_id'], 'b', 'input-b', decision,
                    event=event('one', 'b'), expected_checkpoint={})
            with pytest.raises(ValueError, match='outstanding'):
                view.rollback('operator rollback')
            assert view.pending() is None
            view.acknowledge(receipt)
            with pytest.raises(ValueError, match='concurrent'):
                view.commit_decision(manifest['package_id'], 'b', 'input-b', decision,
                    event=event('one', 'b'), expected_checkpoint={}, expected_watermark=initial['watermark'])
            changed = decision_payload(DecisionV2('quantity_deltas', state={'mode': True}))
            saved = view.commit_decision(manifest['package_id'], 'c', 'input-c', changed,
                event=event('one', 'c'), expected_checkpoint={})
            view.acknowledge(saved)
            with pytest.raises(ValueError, match='concurrent'):
                view.commit_decision(manifest['package_id'], 'd', 'input-d', decision,
                    event=event('one', 'd'), expected_checkpoint={'mode': 1})


@pytest.mark.parametrize('rollback', [False, True])
@pytest.mark.parametrize('error', [OSError, TimeoutError, ValueError])
def test_activation_callback_failure_survives_reopen(tmp_path, rollback, error):
    first, first_release = package(tmp_path)
    newer, new_release = package(tmp_path, '3', from_versions=('2',))
    with closing(DeploymentStore(tmp_path / 'state.db')) as root:
        view, old_strategy, adapter, _ = activate(root, first, first_release, 'one')
        current = LoadedStrategy.from_strategy_package(new_release, trusted=True)
        load = lambda version: old_strategy if version == first['package_id'] else current
        with closing(view):
            view.request(newer, candidate(newer, 'one', first['package_id'], 'upgrade'))
            control = DeploymentController(view, adapter, load_strategy=load)
            if rollback:
                control.step()
                view.rollback('test rollback')
            def fail(_):
                raise error('callback interrupted')
            adapter.activated = fail
            control.step()
            assert view.pending() is None
            assert view.activation_callback()['reason'] == error.__name__
            expected = first['package_id'] if rollback else newer['package_id']
            assert view.snapshot()['active'] == expected
            with closing(root.for_instance('one')) as recovered:
                runtime = InstanceDecisionRuntime(load(expected), recovered, Execution(), allocation={'currency': 'USD'})
                with pytest.raises(ValueError, match='quiesced'):
                    runtime.recover()
                repaired = OfflineAdapter()
                DeploymentController(recovered, repaired, load_strategy=load).step()
                assert repaired.actions == [('activate', expected)]
                assert recovered.activation_callback() is None
                runtime.recover()


def test_concurrent_migration_superseded_by_success(tmp_path):
    first, first_release = package(tmp_path)
    newer, new_release = package(tmp_path, '3', from_versions=('2',))
    with closing(DeploymentStore(tmp_path / 'state.db')) as root:
        owner, old, adapter, _ = activate(root, first, first_release, 'one')
        with closing(owner), closing(root.for_instance('one')) as other:
            owner.request(newer, candidate(newer, 'one', first['package_id'], 'upgrade'))
            strategy = LoadedStrategy.from_strategy_package(new_release, trusted=True)
            competing = LoadedStrategy.from_strategy_package(new_release, trusted=True)
            original = strategy.migrate_state
            def migrate(state, version):
                DeploymentController(other, OfflineAdapter(), load_strategy=lambda _: competing).step()
                return original(state, version)
            strategy.migrate_state = migrate
            # Supply an old checkpoint so that migration actually runs.
            with owner.atomic():
                owner._update('deployment', {'checkpoint': '{"count":1}'})
            result = DeploymentController(owner, adapter, load_strategy=lambda _: strategy).step()
            assert result['active'] == newer['package_id']
            assert result['switch']['phase'] == 'SUCCEEDED'
            assert owner.pending() is None


def test_same_account_cannot_claim_another_instances_client_id(tmp_path):
    with closing(DeploymentStore(tmp_path / 'state.db')) as root:
        root.register_instance(binding('one'))
        root.register_instance(binding('two'))
        with closing(root.for_instance('one')) as one, closing(root.for_instance('two')) as two:
            one.record_order('shared-id', 'package', 'event')
            with pytest.raises(ValueError, match='already owned'):
                two.record_order('shared-id', 'package', 'event')


@pytest.mark.parametrize('qualify', [None, lambda _: None])
def test_binance_store_qualification_cannot_be_replaced(tmp_path, qualify):
    manifest, _ = package(tmp_path)
    with closing(BinanceStore(tmp_path / 'binance.db', qualify=qualify)) as store:
        with pytest.raises(ValueError, match='Binance USD-M authority'):
            store.register_package(manifest)
        store.register_instance(binding('one'))
        with closing(store.for_instance('one')) as view:
            with pytest.raises(ValueError, match='Binance USD-M authority'):
                view.register_package(manifest)


@pytest.mark.parametrize('change', [dict(environment='LIVE'), dict(symbol='AAPL'), dict(interval_seconds=7)])
def test_public_v1_package_retains_legacy_authority(change):
    manifest = dict(schema_version=1, interface='FACTORLAB_DECISION_V1',
        environment='BINANCE_USDM_DEMO', symbol='NVDAUSDT', interval_seconds=60,
        state_version='1', strategy_id='legacy', strategy_version='1', execution_mode='target',
        code_commit='a' * 40, entrypoint='strategy:Strategy', files={'strategy.py': 'b' * 64},
        dependencies=[], configuration={}, migration='wait_flat', evidence={'offline': True})
    manifest.update(change)
    manifest['package_id'] = digest(manifest)
    with pytest.raises(ValueError, match='Demo'):
        validate_manifest(manifest)


def test_client_id_namespaces_are_separate_between_accounts(tmp_path):
    with closing(DeploymentStore(tmp_path / 'state.db')) as root:
        root.register_instance(binding('one'))
        root.register_instance(dict(binding('two'), account_id='another'))
        with closing(root.for_instance('one')) as one, closing(root.for_instance('two')) as two:
            one.record_order('shared-id', 'package', 'event')
            two.record_order('shared-id', 'package', 'event')
            assert two.order('shared-id')['version'] == 'package'


def test_reconstructed_event_returns_original_receipt(tmp_path):
    manifest, release = package(tmp_path)
    with closing(DeploymentStore(tmp_path / 'state.db')) as root:
        view, strategy, _, _ = activate(root, manifest, release, 'one')
        with closing(view):
            execution = Execution()
            runtime = InstanceDecisionRuntime(strategy, view, execution, allocation={'currency': 'USD'})
            original = event('one')
            first = runtime.decide(original)
            reordered = dict(reversed(list(original.items())))
            reordered['symbols'] = dict(reversed(list(original['symbols'].items())))
            assert runtime.decide(reordered) == first
            assert execution.calls == 1
