"""Pending switches must service only persisted old-version cancellations."""
from decimal import Decimal as D
from factorlab.integrations.binance.client import Response
from factorlab.integrations.binance_futures_trading.protocol import SafetyBlocked
from test_binance_demo_network import NetworkFixture
import pytest
from factorlab.strategies.protocol import Decision
from test_s04_validation_budget import install_cycle
from test_unified_cloud_strategy_s04 import harness, promote_candidate, build_decision_package, EXAMPLES
from test_binance_demo_grid_network import network_grid, writes
from test_binance_demo_network import wired


@pytest.mark.parametrize('rollback', [False, True])
@pytest.mark.parametrize('obligation', [False, True])
def test_switch_services_persisted_reduce_only_cancel(tmp_path, harness, rollback, obligation):
    manifest = install_cycle(tmp_path, harness)
    with harness.open() as router:
        router.store.request(manifest, promote_candidate(manifest, 'start'))
        router.before_poll()
        harness.set_time(1, 1)
        harness.quote(router, '150')
        entry = writes(harness.requests)[0]['newClientOrderId'][0]
        harness.fake.trade_update(entry, '.04', '148')
        harness.set_time(1, 2)
        router.before_poll()
        harness.quote(router, '148')
        exit_id = writes(harness.requests)[1]['newClientOrderId'][0]
        runner = router._load_decision_runner(manifest['package_id'])[2]
        if obligation:
            runner.controller._atomic(lambda: runner._pending(Decision(cancellations=(exit_id,)), harness.now()))
        if rollback:
            router.store.rollback('review')
        else:
            archive, target = build_decision_package(tmp_path, 'hold', EXAMPLES,
                strategy_id='limit_cycle', strategy_version='1.0.0', entrypoint='limit_cycle:LimitCycle',
                files=['limit_cycle.py'], configuration=dict(mode='hold'), execution_mode='orders')
            harness.install(archive)
            router.store.request(target, promote_candidate(target, 'next', expected_active=manifest['package_id']))
        harness.set_time(1, 3)
        router.before_poll()
        assert len(writes(harness.requests)) == 2
        deletes = writes(harness.requests, 'DELETE')
        assert len(deletes) == int(obligation)
        if obligation:
            assert deletes[0]['origClientOrderId'] == [exit_id]
            harness.set_time(1, 4)
            router.before_poll()
            assert not runner.has_unresolved_work()['pending_cancellations']
            assert router.store.pending()['reason'] == 'WAIT_OLD_POSITIONS'
        assert router.bridge.session.status()['positions'][0]['quantity'] == D('.04')


@pytest.mark.parametrize('outcome', ['lost_canceled', 'http200_active', 'http400_active', 'partial', 'stop', 'authority', 'restart', 'concurrent', 'unreconciled', 'unknown'])
def test_pending_switch_cancel_recovery(tmp_path, harness, monkeypatch, outcome):
    manifest = install_cycle(tmp_path, harness)
    with harness.open() as router:
        router.store.request(manifest, promote_candidate(manifest, 'start-recovery'))
        router.before_poll()
        harness.set_time(1, 1)
        harness.quote(router, '150')
        entry = writes(harness.requests)[0]['newClientOrderId'][0]
        harness.fake.trade_update(entry, '.04', '148')
        harness.set_time(1, 2)
        router.before_poll()
        harness.quote(router, '148')
        key = writes(harness.requests)[1]['newClientOrderId'][0]
        runner = router._load_decision_runner(manifest['package_id'])[2]
        runner.controller._atomic(lambda: runner._pending(Decision(cancellations=(key,)), harness.now()))
        router.store.rollback('cancel-recovery')
        if outcome in {'unreconciled', 'unknown'}:
            status = router.bridge.session.status()
            with monkeypatch.context() as blocked:
                blocked.setattr(router.bridge.session, 'status', lambda: dict(status,
                    reconciled=outcome != 'unreconciled', unknown=int(outcome == 'unknown')))
                router._maintain_cancellations(manifest['package_id'], runner)
            assert not writes(harness.requests, 'DELETE')
            assert runner.has_unresolved_work()['pending_cancellations']
        original = NetworkFixture.__call__
        injected = False
        def reply(self, request, timeout):
            nonlocal injected
            if request.method == 'DELETE' and not injected:
                injected = True
                if outcome == 'lost_canceled':
                    original(self, request, timeout)
                    raise TimeoutError('accepted DELETE reply lost')
                if outcome in {'http200_active', 'partial', 'http400_active'}:
                    self.calls.append(('DELETE', request.path, {}))
                    return Response(400 if outcome == 'http400_active' else 200, {}, b'{}')
            return original(self, request, timeout)
        monkeypatch.setattr(NetworkFixture, '__call__', reply)
        if outcome == 'stop':
            harness.control.state = 'STOPPED'
        if outcome == 'authority':
            harness.authority.allowed = False
        if outcome == 'partial':
            harness.fake.trade_update(key, '.02', '147.70')
        if outcome == 'concurrent':
            old_dispatch = runner.dispatch_cancellations
            def change_switch(at):
                router.store.rollback('concurrent-new-target')
                return old_dispatch(at)
            monkeypatch.setattr(runner, 'dispatch_cancellations', change_switch)
            # A superseded switch cannot grant dispatch authority.
            harness.set_time(1, 3)
            with pytest.raises(SafetyBlocked):
                router.before_poll()
            monkeypatch.setattr(runner, 'dispatch_cancellations', old_dispatch)
        else:
            harness.set_time(1, 3)
            router.before_poll()
        if outcome in {'stop', 'authority'}:
            assert not writes(harness.requests, 'DELETE')
            harness.control.state = 'RUNNING'
            harness.authority.allowed = True
        assert len(writes(harness.requests)) == 2
        if outcome == 'http400_active':
            harness.set_time(1, 4)
            with pytest.raises(SafetyBlocked, match='dispatch already consumed'):
                router.before_poll()
            assert len(writes(harness.requests, 'DELETE')) == 1
            assert runner.has_unresolved_work()['pending_cancellations'] == [key]
            assert router.store.pending() is not None
            return
        if outcome == 'restart':
            pass
        else:
            harness.set_time(1, 4)
            router.before_poll()
            harness.set_time(1, 5)
            router.before_poll()
            assert not runner.has_unresolved_work()['pending_cancellations']
            expected = 2 if outcome in {'http200_active', 'partial', 'http400_active'} else 1
            assert len(writes(harness.requests, 'DELETE')) == expected
            delete_indices = [i for i, request in enumerate(harness.requests)
                              if request.get_method() == 'DELETE']
            if expected == 2:
                assert any(request.get_method() == 'GET' and '/fapi/v1/order?' in request.full_url
                           for request in harness.requests[delete_indices[0] + 1:delete_indices[1]])
            quantity = '.02' if outcome == 'partial' else '.04'
            assert router.bridge.session.status()['positions'][0]['quantity'] == D(quantity)
            assert router.store.pending()['reason'] == 'WAIT_OLD_POSITIONS'
            return
    with harness.open() as router:
        harness.set_time(1, 4)
        router.before_poll()
        assert len(writes(harness.requests)) == 2
        assert len(writes(harness.requests, 'DELETE')) == 1
        runner = router._load_decision_runner(manifest['package_id'])[2]
        assert not runner.has_unresolved_work()['pending_cancellations']
