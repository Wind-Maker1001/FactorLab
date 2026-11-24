"""Continuous Demo windows over intercepted HTTP and existing account journals."""
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta
from decimal import Decimal as D
import json
from threading import Barrier

import pytest

from test_binance_demo_grid_network import network_grid, observe, writes
from test_binance_demo_network import wired
from factorlab.integrations.binance_futures_trading import demo_runtime as module
from factorlab.integrations.binance_futures_trading.demo_cli import open_session, parse_args
from factorlab.integrations.binance_futures_trading.demo_runtime import DemoRuntime, open_grid_session
from factorlab.integrations.binance_futures_trading.protocol import Credentials, Environment, SafetyBlocked
from factorlab.trading import OrderStatus as S, Side


def initialize(root, store, **limits):
    with closing(DemoRuntime(root)) as runtime:
        epoch = runtime.stop_epoch()
        with open_session(root, store, runtime, dict(GET=0, POST=0, DELETE=0)) as (session, _):
            status = session.recover()
            authorization = runtime.start_continuous(store.config, opening_equity=D('1000'),
                stop_epoch=epoch, **limits)
            return authorization, session.adapter.facts, status


def test_explicit_resume_retains_stop_audit_identity_and_consumption(network_grid):
    root, store, factory, _, fake, requests = network_grid
    authorization, _, _ = initialize(root, store)
    with open_grid_session(root, store, factory, continuous=True) as bridge:
        bridge.recover()
        observe(bridge)
        key = bridge.grid.active[0].intent.client_order_id
        consumed = bridge.runtime.consumption(authorization)
        bridge.runtime.stop()
        stopped = bridge.runtime.continuous_authorization()
        epoch = bridge.runtime.stop_epoch()
        bridge.recover()
        resumed = bridge.runtime.resume_continuous(store.config, facts=bridge.session.adapter.facts,
            reconciled=True, stop_epoch=epoch)
        assert resumed == dict(authorization, state='RUNNING', stop_epoch=epoch)
        assert bridge.runtime.consumption(resumed) == consumed
        audit, = bridge.runtime.db.execute("SELECT value FROM runtime WHERE key LIKE 'continuous_resume:%'")
        assert json.loads(audit[0])['previous_authorization'] == stopped
        assert bridge.runtime.stop_epoch() == epoch
    with open_grid_session(root, store, factory, continuous=True) as bridge:
        bridge.recover()
        assert bridge.gateway.adapter.order(key).status is S.SUBMITTED
        assert bridge.dispatch() is None
    assert len(writes(requests)) == 1
    assert parse_args(['resume-continuous']).command == 'resume-continuous'


@pytest.mark.parametrize('reason', ['unreconciled', 'stop_race', 'loss', 'halted', 'budget'])
def test_explicit_resume_rejects_unsafe_state_without_replacing_stop(network_grid, reason):
    root, store, factory, _, fake, requests = network_grid
    authorization, facts, _ = initialize(root, store)
    with closing(DemoRuntime(root)) as runtime:
        runtime.stop()
        epoch = runtime.stop_epoch()
        if reason == 'stop_race':
            runtime.stop()
        elif reason == 'loss':
            authorization['opening_equity'] = '1001'
            runtime._set('continuous', json.dumps(dict(authorization, state='STOPPED')))
            runtime.db.commit()
        elif reason == 'halted':
            runtime.halt('test risk halt')
            runtime._set('continuous', json.dumps(dict(authorization, state='HALTED')))
            runtime.db.commit()
        elif reason == 'budget':
            runtime._set('continuous', json.dumps(dict(authorization, state='STOPPED', total_orders=0)))
            runtime.db.commit()
        before = runtime.continuous_authorization()
        with pytest.raises(SafetyBlocked):
            runtime.resume_continuous(store.config, facts=facts,
                reconciled=reason != 'unreconciled', stop_epoch=epoch)
        assert runtime.continuous_authorization() == before
        assert runtime.db.execute("SELECT count(*) FROM runtime WHERE key LIKE 'continuous_resume:%'").fetchone()[0] == 0
    assert not writes(requests)


def resume_in_process(arguments):
    root, binding, facts, epoch = arguments
    with closing(DemoRuntime(root)) as runtime:
        try:
            runtime.resume_continuous(binding, facts=facts, reconciled=True, stop_epoch=epoch)
        except SafetyBlocked as error:
            return str(error)
    return None


@pytest.mark.parametrize('legacy_stop', [False, True])
@pytest.mark.parametrize('restart', [False, True])
def test_risk_halt_stop_cannot_resume_after_equity_recovers(network_grid, legacy_stop, restart):
    root, store, factory, _, fake, requests = network_grid
    authorization, _, _ = initialize(root, store)
    with open_grid_session(root, store, factory, continuous=True) as bridge:
        bridge.recover()
        fake.cash = D('999')
        bridge.recover()
        with pytest.raises(SafetyBlocked, match='global Demo loss'):
            bridge.ensure_campaign()
        runtime = bridge.runtime
        assert runtime.continuous_authorization()['state'] == 'HALTED'
        diagnostic = runtime._get('continuous_diagnostic')
        halt_epoch = runtime.stop_epoch()
        runtime.stop()
        epoch = runtime.stop_epoch()
        assert epoch != halt_epoch
        assert runtime._get('continuous_window') == runtime._get('active') == ''
        if legacy_stop:
            # Reproduce the persisted state written by STOP before the latch fix.
            runtime._set('continuous', json.dumps(dict(authorization, state='STOPPED')))
            runtime.db.commit()
        else:
            assert runtime.continuous_authorization()['state'] == 'HALTED'
        fake.cash = D('1000')
        bridge.recover()
        facts = bridge.session.adapter.facts
        assert facts.usdt()['walletBalance'] + facts.usdt()['unrealizedProfit'] == D('1000')
        before = runtime.continuous_authorization()
        campaigns = runtime.db.execute('SELECT * FROM campaigns').fetchall()
        consumed = runtime.consumption(authorization)
        if restart:
            with ProcessPoolExecutor(max_workers=1) as pool:
                error = pool.submit(resume_in_process, (root, store.config, facts, epoch)).result()
            assert error is not None
        else:
            with pytest.raises(SafetyBlocked):
                runtime.resume_continuous(store.config, facts=facts, reconciled=True, stop_epoch=epoch)
        assert runtime.continuous_authorization() == before
        assert runtime._get('continuous_diagnostic') == diagnostic
        assert runtime.consumption(authorization) == consumed
        assert runtime.db.execute('SELECT * FROM campaigns').fetchall() == campaigns
        assert runtime.db.execute("SELECT count(*) FROM runtime WHERE key LIKE 'continuous_resume:%'").fetchone()[0] == 0
        with pytest.raises(SafetyBlocked, match='not RUNNING'):
            runtime.ensure_window(store.config, facts=facts, reconciled=True, stop_epoch=epoch)
    assert not writes(requests)
    assert not writes(requests, 'DELETE')


def expire(monkeypatch, campaign):
    at = datetime.fromisoformat(campaign['expires_at']) + timedelta(seconds=1)
    monkeypatch.setattr(module, 'now', lambda: at)


def test_missing_window_restart_and_expiry_preserve_global_identity(network_grid, monkeypatch):
    root, store, factory, _, fake, requests = network_grid
    authorization, _, _ = initialize(root, store)
    with open_grid_session(root, store, factory, continuous=True) as bridge:
        first = bridge.runtime.active_campaign()
        assert first['authorization'] == authorization['id']
        bridge.recover()
        assert observe(bridge).status is S.SUBMITTED
        client_id = bridge.grid.active[0].intent.client_order_id
        consumed = bridge.runtime.consumption(authorization)
        deployment = bridge.grid.deployment_id
        capability = bridge.session.client.transport.authorization
        expire(monkeypatch, first)
        bridge.recover()
        second = bridge.ensure_campaign()
        assert capability.revoked
        assert bridge.session.client.transport.authorization is not capability
        assert first['id'] != second['id']
        assert bridge.runtime.campaign(first['id']) == first
        assert bridge.runtime.consumption(authorization) == consumed
        params = json.loads(bridge.runtime.db.execute(
            "SELECT parameters FROM dispatches WHERE method='POST' AND client_id=?", (client_id,)).fetchone()[0])
        with pytest.raises(SafetyBlocked, match='dispatch already consumed'):
            bridge.runtime.reserve(second, 'POST', params, bridge.session.client.transport.authorization.plan,
                expected=(client_id, 'BUY', D(params['quantity']), 'false', D(params['price'])),
                facts=bridge.session.adapter.facts)
        assert second['opening_equity'] == first['opening_equity'] == '1000'
        assert bridge.dispatch() is None
        assert bridge.session.adapter.order(client_id).status is S.SUBMITTED
    with open_grid_session(root, store, factory, continuous=True) as bridge:
        bridge.recover()
        assert bridge.grid.deployment_id == deployment
        assert bridge.dispatch() is None
        assert bridge.runtime.continuous_authorization() == authorization
        assert len(writes(requests)) == 1


def test_partial_fill_cross_window_recovery_and_owned_cancel(network_grid, monkeypatch):
    root, store, factory, _, fake, requests = network_grid
    initialize(root, store)
    with open_grid_session(root, store, factory, continuous=True) as bridge:
        bridge.recover()
        observe(bridge)
        key = bridge.grid.active[0].intent.client_order_id
        fake.trade_update(key, '.02', '146')
        expire(monkeypatch, bridge.runtime.active_campaign())
        bridge.recover()
        bridge.ensure_campaign()
        assert bridge.account().inventory == D('.02')
        assert bridge.dispatch() is None
        assert bridge.session.cancel(key).status is S.CANCELED
        bridge.recover()
        assert bridge.account().inventory == D('.02')
        assert len(writes(requests)) == len(writes(requests, 'DELETE')) == 1


def test_window_order_budget_rotates_without_another_approval(network_grid):
    root, store, factory, _, fake, requests = network_grid
    authorization, _, _ = initialize(root, store, orders=1)
    with open_grid_session(root, store, factory, continuous=True) as bridge:
        bridge.recover()
        observe(bridge)
        windows = set()
        for _ in range(5):
            windows.add(bridge.runtime.active_campaign()['id'])
            intent = bridge.grid.active[0].intent
            fake.trade_update(intent.client_order_id, str(intent.quantity), str(intent.limit_price))
            bridge.recover()
            bridge.ensure_campaign()
            assert bridge.dispatch().status is S.SUBMITTED
        assert len(windows) == 5
        assert len(writes(requests)) == 6
        assert bridge.runtime.consumption(authorization)['posts'] == 6
        report = bridge.runtime.report('smoke-v1')
        assert report['USER_DEMO_AUTHORIZATION']['id'] == authorization['id']
        assert report['EXCHANGE_WRITE_CAPABILITY']['historical_success']


def test_rotation_does_not_reset_global_loss(network_grid, monkeypatch):
    root, store, factory, _, fake, _ = network_grid
    authorization, _, _ = initialize(root, store)
    with open_grid_session(root, store, factory, continuous=True) as bridge:
        old = bridge.runtime.active_campaign()
        fake.cash = D('999')
        expire(monkeypatch, old)
        bridge.recover()
        with pytest.raises(SafetyBlocked, match='global Demo loss'):
            bridge.ensure_campaign()
        assert bridge.runtime.continuous_authorization()['opening_equity'] == authorization['opening_equity']
        assert bridge.runtime.db.execute('SELECT count(*) FROM campaigns').fetchone()[0] == 1


def test_historical_attempts_charged_and_policy_cannot_be_reset(network_grid):
    root, store, _, enable, _, _ = network_grid
    legacy = enable(orders=4)
    with closing(DemoRuntime(root)) as runtime:
        params = dict(quantity='.04', price='150')
        runtime.db.execute('INSERT INTO dispatches VALUES(?,?,?,?,NULL)',
            (legacy['id'], 'POST', legacy['prefix'] + '-historical', json.dumps(params)))
        runtime.db.commit()
    authorization, facts, _ = initialize(root, store)
    with closing(DemoRuntime(root)) as runtime:
        assert runtime.consumption(authorization) == dict(posts=1, submitted_notional='6.00')
        saved = runtime.start_continuous(store.config, opening_equity=D('900'), stop_epoch=runtime.stop_epoch())
        assert saved == authorization
        with pytest.raises(SafetyBlocked, match='policy is frozen'):
            runtime.start_continuous(store.config, opening_equity=D('1000'),
                stop_epoch=runtime.stop_epoch(), total_orders=101)
        assert runtime.ensure_window(store.config, facts=facts, reconciled=True,
            stop_epoch=runtime.stop_epoch())['opening_equity'] == legacy['opening_equity']
        assert runtime.campaign(legacy['id']) == legacy


@pytest.mark.parametrize('budget', [dict(total_orders=1), dict(total_notional=D('6'))])
def test_global_budget_not_replenished_by_rotation(network_grid, monkeypatch, budget):
    root, store, factory, _, fake, requests = network_grid
    initialize(root, store, **budget)
    with open_grid_session(root, store, factory, continuous=True) as bridge:
        bridge.recover()
        observe(bridge)
        key = bridge.grid.active[0].intent.client_order_id
        fake.trade_update(key, '.04', '146')
        expire(monkeypatch, bridge.runtime.active_campaign())
        bridge.recover()
        bridge.ensure_campaign()
        with pytest.raises(SafetyBlocked, match='global Demo trading budget|plan total order limit'):
            bridge.dispatch()
        assert len(writes(requests)) == 1


def test_uncertain_post_get_recovery_does_not_resubmit(network_grid, monkeypatch):
    root, store, factory, _, fake, requests = network_grid
    initialize(root, store)
    fake.timeout_next = True
    with open_grid_session(root, store, factory, continuous=True) as bridge:
        bridge.recover()
        observe(bridge)
        assert len(writes(requests)) == 1
        old = bridge.runtime.active_campaign()
        expire(monkeypatch, old)
        bridge.recover()
        bridge.ensure_campaign()
        assert bridge.dispatch() is None
        assert len(writes(requests)) == 1


def test_stop_latched_across_restart_start_and_inflight_capability(network_grid):
    root, store, factory, _, _, requests = network_grid
    initialize(root, store)
    with open_grid_session(root, store, factory, continuous=True) as bridge:
        bridge.recover()
        with closing(DemoRuntime(root)) as other:
            other.stop()
        with pytest.raises(SafetyBlocked, match='stopped or expired'):
            observe(bridge)
        assert not writes(requests)
    with closing(DemoRuntime(root)) as runtime:
        assert runtime.continuous_authorization()['state'] == 'STOPPED'
        with pytest.raises(SafetyBlocked, match='explicitly stopped'):
            runtime.start_continuous(store.config, opening_equity=D('1000'), stop_epoch=runtime.stop_epoch())
    with pytest.raises(SafetyBlocked, match='not RUNNING'):
        with open_grid_session(root, store, factory, continuous=True):
            pytest.fail('STOP was cleared by restart')


def test_two_connections_create_one_window(network_grid):
    root, store, _, _, _, _ = network_grid
    _, facts, _ = initialize(root, store)
    barrier = Barrier(2)
    def create(_):
        with closing(DemoRuntime(root)) as runtime:
            barrier.wait()
            return runtime.ensure_window(store.config, facts=facts, reconciled=True,
                stop_epoch=runtime.stop_epoch())['id']
    with ThreadPoolExecutor(max_workers=2) as pool:
        ids = list(pool.map(create, range(2)))
    assert ids[0] == ids[1]
    with closing(DemoRuntime(root)) as runtime:
        assert runtime.db.execute('SELECT count(*) FROM campaigns').fetchone()[0] == 1


def create_window_in_process(arguments):
    root, binding, facts = arguments
    with closing(DemoRuntime(root)) as runtime:
        return runtime.ensure_window(binding, facts=facts, reconciled=True,
            stop_epoch=runtime.stop_epoch())['id']


def test_separate_processes_share_one_window(network_grid):
    root, store, _, _, _, _ = network_grid
    _, facts, _ = initialize(root, store)
    with ProcessPoolExecutor(max_workers=2) as pool:
        ids = list(pool.map(create_window_in_process, [(root, store.config, facts)] * 2))
    assert ids[0] == ids[1]
    with closing(DemoRuntime(root)) as runtime:
        assert runtime.db.execute('SELECT count(*) FROM campaigns').fetchone()[0] == 1


def test_production_credential_refused_before_window(network_grid):
    root, store, factory, _, _, requests = network_grid
    initialize(root, store)
    store.credential = Credentials('demo-key', 'demo-secret', Environment.PRODUCTION)
    with pytest.raises(SafetyBlocked, match='Demo credentials required'):
        with open_grid_session(root, store, factory, continuous=True):
            pytest.fail('production credential admitted')
    assert not writes(requests)


def test_clock_skew_syncs_before_next_get_recovery(network_grid, monkeypatch):
    from test_demo_constructor_clock import inject, paths, response, signature_parameters, TIME
    root, store, factory, _, _, requests = network_grid
    initialize(root, store)
    with open_grid_session(root, store, factory, continuous=True) as bridge:
        endpoint = '/fapi/v3/positionRisk'
        inject(monkeypatch, [response()], endpoint=endpoint)
        before = len(requests)
        bridge.recover()
        recovered = requests[before:]
        observed = paths(recovered)
        assert observed.count(TIME) == 1
        assert observed.count('/fapi/v3/account') == 1
        assert observed.count(endpoint) == 2
        failed = observed.index(endpoint)
        assert observed[failed:failed + 3] == [endpoint, TIME, endpoint]
        attempts = [signature_parameters(request) for request in recovered
                    if endpoint in request.full_url]
        business = [{key: value for key, value in params.items()
                     if key not in {'timestamp', 'signature'}} for params in attempts]
        assert business[0] == business[1]
        assert business[0]['recvWindow'] == ['5000']
        assert bridge.session.client.clock_recoveries[-1]['outcome'] == 'RECOVERED'
        bridge.ensure_campaign()
        assert bridge.account().reconciled
        assert not writes(requests)


def test_non_protocol_clock_skew_does_not_replay_recovery(network_grid, monkeypatch):
    from factorlab.integrations.binance_futures_trading.protocol import ExchangeError
    from test_demo_constructor_clock import paths, TIME
    root, store, factory, _, _, requests = network_grid
    initialize(root, store)
    with open_grid_session(root, store, factory, continuous=True) as bridge:
        calls = []

        def reject(controller):
            calls.append(controller)
            raise ExchangeError(-1021, 400, 'CLOCK_SKEW')

        monkeypatch.setattr(bridge.session.gateway, 'recover', reject)
        before = len(requests)
        with pytest.raises(ExchangeError, match='CLOCK_SKEW'):
            bridge.recover()
        assert calls == [bridge.session.controller]
        assert TIME not in paths(requests[before:])
        assert not bridge.ready
        with pytest.raises(SafetyBlocked, match='RECONCILE_REQUIRED'):
            bridge.dispatch()
        assert not writes(requests)


def test_continuous_entrypoint_has_no_campaign_approval():
    args = parse_args(['start-continuous'])
    assert (args.total_orders, args.total_notional, args.window_seconds, args.window_orders) == (100, D('700'), 10800, 4)


def test_budget_migration_preserves_identity_consumption_and_is_idempotent(network_grid):
    root, store, _, _, _, _ = network_grid
    authorization, _, _ = initialize(root, store)
    campaign = dict(id='legacy-window', journal_id=authorization['journal_id'], smoke=False, opening_equity='1000')
    with closing(DemoRuntime(root)) as runtime:
        runtime.db.execute('INSERT INTO campaigns VALUES (?, ?)', (campaign['id'], json.dumps(campaign)))
        for index in range(100):
            runtime.db.execute('INSERT INTO dispatches VALUES (?, ?, ?, ?, NULL)',
                (campaign['id'], 'POST', f'legacy-{index}', json.dumps(dict(quantity='.04', price='158.9935'))))
        runtime.db.commit()
        before = runtime.continuous_authorization()
        consumed = runtime.consumption(before)
        assert consumed == dict(posts=100, submitted_notional='635.974000')
        migrated = runtime.update_continuous_budget(store.config, current_equity=D('1000'), total_orders=100000,
            total_notional=D('500000'), max_loss=D('250'))
        after = migrated['authorization']
        assert after['id'] == before['id']
        assert after['journal_id'] == before['journal_id']
        assert after['opening_equity'] == before['opening_equity'] == '1000'
        assert (after['total_orders'], D(after['total_notional']), D(after['max_loss'])) == (100000, D('500000'), D('250'))
        assert migrated['consumption'] == consumed
        repeated = runtime.update_continuous_budget(store.config, current_equity=D('1000'), total_orders=100000,
            total_notional=D('500000'), max_loss=D('250'))
        assert repeated['idempotent'] is True
        assert runtime.db.execute("SELECT count(*) FROM runtime WHERE key LIKE 'continuous_budget_migration:%'").fetchone()[0] == 1


def test_budget_migration_rejects_consumption_or_independent_state_changes(network_grid):
    root, store, _, _, _, _ = network_grid
    authorization, _, _ = initialize(root, store)
    with closing(DemoRuntime(root)) as runtime:
        with pytest.raises(SafetyBlocked, match='invalid continuous Demo risk budget'):
            runtime.update_continuous_budget(store.config, current_equity=D('1000'), total_orders=0, total_notional=D('500000'), max_loss=D('250'))
        runtime.stop()
        stopped = runtime.continuous_authorization()
        migrated = runtime.update_continuous_budget(store.config, current_equity=D('1000'), total_orders=100000,
            total_notional=D('500000'), max_loss=D('250'))
        assert migrated['authorization']['state'] == stopped['state'] == 'STOPPED'
        assert runtime.continuous_authorization()['stop_epoch'] == stopped['stop_epoch']
