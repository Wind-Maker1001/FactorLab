"""Persistent cumulative budget migration over existing Demo account fixtures."""
from contextlib import closing
from decimal import Decimal as D
import json

import pytest

from test_binance_demo_grid_network import network_grid, writes
from test_binance_demo_network import wired
from test_demo_continuous_campaign import initialize, expire
from factorlab.integrations.binance_futures_trading.demo_cli import parse_args, run_command
from factorlab.integrations.binance_futures_trading.demo_runtime import DemoRuntime, open_grid_session
from factorlab.integrations.binance_futures_trading.protocol import SafetyBlocked


LIMITS = dict(total_orders=100000, total_notional=D('500000'), max_loss=D('250'))


def test_account_budget_includes_smoke_portfolio_and_configuration_posts(network_grid):
    root, store, _, _, _, _ = network_grid
    with closing(DemoRuntime(root)) as runtime:
        authorization = runtime.start_continuous(store.config, opening_equity=D('5000'),
            stop_epoch=runtime.stop_epoch())
        runtime.db.executemany('INSERT INTO dispatches VALUES (?, ?, ?, ?, ?)', [
            ('smoke-original', 'POST', 'smoke-order', json.dumps(dict(quantity='.04', price='150')), '{}'),
            ('portfolio-campaign:original', 'POST', 'portfolio-order',
             json.dumps(dict(quantity='.01', price='600')), '{}'),
            ('portfolio-campaign:original', 'POST', 'leverage', json.dumps(dict(symbol='QQQUSDT', leverage=1)), '{}'),
            ('smoke-original', 'DELETE', 'smoke-order', '{}', '{}')])
        runtime.db.commit()
        before = runtime.db.execute('SELECT * FROM dispatches').fetchall()
        assert runtime.consumption(authorization) == dict(posts=3, submitted_notional='12.00')
        migrated = runtime.update_continuous_budget(store.config, current_equity=D('5000'), **LIMITS)
        assert migrated['consumption'] == dict(posts=3, submitted_notional='12.00')
        assert runtime.db.execute('SELECT * FROM dispatches').fetchall() == before


@pytest.mark.parametrize('limit', [dict(total_orders=100), dict(total_notional=D('12'))])
@pytest.mark.parametrize('idempotent', [False, True])
def test_migration_preserves_reserved_exits_under_expanded_history(network_grid, limit, idempotent):
    root, store, _, _, _, _ = network_grid
    with closing(DemoRuntime(root)) as runtime:
        authorization = runtime.start_continuous(store.config, opening_equity=D('5000'),
            stop_epoch=runtime.stop_epoch())
        limits = dict(total_orders=101, total_notional=D('14'), max_loss=D('250')) | limit
        if idempotent:
            authorization = authorization | limits | dict(total_notional=str(limits['total_notional']), max_loss='250')
            runtime._set('continuous', json.dumps(authorization))
        runtime.db.executemany('INSERT INTO dispatches VALUES (?, ?, ?, ?, ?)', [
            ('smoke-original', 'POST', f'old-{i}', json.dumps(dict(quantity='.001', price='120')), '{}')
            for i in range(100)])
        runtime.db.execute('INSERT INTO exit_budget_reservations VALUES (?, ?, ?, ?)',
            (authorization['id'], 'entry', 'exit', '2'))
        runtime.db.commit()
        before = runtime.db.execute('SELECT * FROM exit_budget_reservations').fetchall()
        with pytest.raises(SafetyBlocked, match='history and reserved exits'):
            runtime.update_continuous_budget(store.config, current_equity=D('5000'), **limits)
        assert runtime.continuous_authorization() == authorization
        assert runtime.db.execute('SELECT * FROM exit_budget_reservations').fetchall() == before
        result = runtime.update_continuous_budget(store.config, current_equity=D('5000'), **LIMITS)
        assert result['consumption']['posts'] == 100
        assert runtime.db.execute('SELECT * FROM exit_budget_reservations').fetchall() == before


def test_window_activation_requires_budget_for_legacy_exit_commitments(network_grid):
    root, store, factory, _, _, _ = network_grid
    initialize(root, store)
    with open_grid_session(root, store, factory, continuous=True) as bridge:
        authorization = bridge.runtime.continuous_authorization()
        bridge.runtime.db.executemany('INSERT INTO dispatches VALUES (?, ?, ?, ?, ?)', [
            ('smoke-original', 'POST', f'old-{i}', json.dumps(dict(quantity='.001', price='120')), '{}')
            for i in range(100)])
        bridge.runtime.db.execute('INSERT INTO exit_budget_reservations VALUES (?, ?, ?, ?)',
            (authorization['id'], 'entry', 'exit', '2'))
        bridge.runtime.db.commit()
        bridge.recover()
        with pytest.raises(SafetyBlocked, match='update budget before activation'):
            bridge.ensure_campaign()
        bridge.runtime.update_continuous_budget(store.config, current_equity=D('5000'), **LIMITS)
        bridge.ensure_campaign()
        assert bridge.runtime.db.execute('SELECT exit_id FROM exit_budget_reservations').fetchall() == [('exit',)]


def exhausted(runtime, binding):
    authorization = runtime.start_continuous(binding, opening_equity=D('5000'), stop_epoch=runtime.stop_epoch())
    historical = dict(id='original', journal_id=authorization['journal_id'], opening_equity='5000', smoke=False)
    runtime.db.execute('INSERT INTO campaigns VALUES (?, ?)', ('original', json.dumps(historical)))
    runtime.db.executemany('INSERT INTO dispatches VALUES (?, ?, ?, ?, ?)', [
        ('original', 'POST', f'historical-{i}', json.dumps(dict(quantity='.04', price='158.9935')), '{}')
        for i in range(100)])
    runtime.db.commit()
    return authorization


def test_exact_exhausted_account_restart_and_original_equity_correction(network_grid):
    root, store, _, _, _, _ = network_grid
    with closing(DemoRuntime(root)) as runtime:
        authorization = exhausted(runtime, store.config)
        runtime._set('continuous', json.dumps(dict(authorization, opening_equity='4999.97114760')))
        runtime.db.commit()
        history = runtime.db.execute('SELECT * FROM dispatches').fetchall()
        result = runtime.update_continuous_budget(store.config, current_equity=D('5000'), **LIMITS)
        assert result['authorization']['opening_equity'] == '5000'
        assert result['consumption']['posts'] == 100
        assert D(result['consumption']['submitted_notional']) == D('635.974')
        assert result['authorization']['total_orders'] - result['consumption']['posts'] == 99900
        assert D(result['authorization']['total_notional']) - D(result['consumption']['submitted_notional']) == D('499364.026')
        assert D(result['authorization']['max_loss']) - (D('5000') - D('4999.32325687')) == D('249.32325687')
        event = json.loads(runtime.db.execute("SELECT value FROM runtime WHERE key LIKE 'continuous_budget_migration:%'").fetchone()[0])
        assert event['previous_authorization']['opening_equity'] == '4999.97114760'
        assert event['original_equity_campaign'] == 'original'
    with closing(DemoRuntime(root)) as runtime:
        assert runtime.continuous_authorization() == result['authorization']
        assert runtime.db.execute('SELECT * FROM dispatches').fetchall() == history
        assert runtime.update_continuous_budget(store.config, current_equity=D('5000'), **LIMITS)['idempotent']


@pytest.mark.parametrize('state', ['STOPPED', 'HALTED', 'PAUSED', 'UNKNOWN'])
def test_migration_does_not_clear_independent_controls(network_grid, state):
    root, store, _, _, _, _ = network_grid
    authorization, _, _ = initialize(root, store)
    with closing(DemoRuntime(root)) as runtime:
        runtime._set('continuous', json.dumps(dict(authorization, state=state)))
        runtime._set('continuous_diagnostic', 'independent halt')
        runtime._set('continuous_recovery', 'UNKNOWN')
        runtime.db.commit()
        before = dict(runtime.db.execute('SELECT key, value FROM runtime'))
        result = runtime.update_continuous_budget(store.config, current_equity=D('5000'), **LIMITS)
        assert result['authorization']['state'] == state
        for key, value in before.items():
            if key != 'continuous':
                assert runtime._get(key) == value


@pytest.mark.parametrize('limit', [dict(total_orders=99), dict(total_notional=D('635'))])
def test_limit_below_consumption_is_rejected_atomically(network_grid, limit):
    root, store, _, _, _, _ = network_grid
    with closing(DemoRuntime(root)) as runtime:
        before = exhausted(runtime, store.config)
        with pytest.raises(SafetyBlocked, match='below historical consumption'):
            runtime.update_continuous_budget(store.config, current_equity=D('5000'), **(LIMITS | limit))
        assert runtime.continuous_authorization() == before
        assert not runtime.db.execute("SELECT value FROM runtime WHERE key LIKE 'continuous_budget_migration:%'").fetchall()


@pytest.mark.parametrize('equity', [D('4750'), D('4749')])
def test_migration_rejects_exhausted_original_equity_loss(network_grid, equity):
    root, store, _, _, _, _ = network_grid
    with closing(DemoRuntime(root)) as runtime:
        before = exhausted(runtime, store.config)
        with pytest.raises(SafetyBlocked, match='loss budget is already exhausted'):
            runtime.update_continuous_budget(store.config, current_equity=equity, **LIMITS)
        assert runtime.continuous_authorization() == before


def test_interrupted_audit_write_rolls_back_budget(network_grid, monkeypatch):
    root, store, _, _, _, _ = network_grid
    with closing(DemoRuntime(root)) as runtime:
        before = exhausted(runtime, store.config)
        original = runtime._set
        def interrupt(key, value):
            if key.startswith('continuous_budget_migration:'):
                raise RuntimeError('injected interruption')
            original(key, value)
        monkeypatch.setattr(runtime, '_set', interrupt)
        with pytest.raises(RuntimeError, match='injected interruption'):
            runtime.update_continuous_budget(store.config, current_equity=D('5000'), **LIMITS)
    with closing(DemoRuntime(root)) as runtime:
        assert runtime.continuous_authorization() == before
        assert runtime.consumption(before)['posts'] == 100
        assert not runtime.db.execute("SELECT value FROM runtime WHERE key LIKE 'continuous_budget_migration:%'").fetchall()
        assert not runtime.update_continuous_budget(store.config, current_equity=D('5000'), **LIMITS)['idempotent']


def test_window_and_gateway_use_new_loss_cap_without_rewriting_history(network_grid, monkeypatch):
    root, store, factory, _, fake, _ = network_grid
    initialize(root, store)
    with open_grid_session(root, store, factory, continuous=True) as bridge:
        before = bridge.runtime.active_campaign()
        bridge.runtime.update_continuous_budget(store.config, current_equity=D('5000'), **LIMITS)
        fake.cash = D('751')
        bridge.recover()
        current = bridge.ensure_campaign()
        assert current['id'] != before['id']
        assert current['max_loss'] == '250'
        assert bridge.runtime.authority(store.config, current).plan.max_loss == D('250')
        assert bridge.runtime.campaign(before['id']) == before
        expire(monkeypatch, current)
        bridge.recover()
        bridge.ensure_campaign()
        assert bridge.runtime.continuous_authorization()['total_orders'] == 100000
        assert bridge.runtime.consumption(bridge.runtime.continuous_authorization())['posts'] == 0
        fake.cash = D('750')
        bridge.recover()
        with pytest.raises(SafetyBlocked, match='global Demo loss'):
            bridge.ensure_campaign()
        assert bridge.runtime.continuous_authorization()['state'] == 'HALTED'


def test_cli_update_is_get_only_and_idempotent(network_grid):
    root, store, _, _, _, requests = network_grid
    initialize(root, store)
    args = parse_args(['update-budget', '--total-orders', '100000', '--total-notional', '500000', '--max-loss', '250'])
    with closing(DemoRuntime(root)) as runtime:
        for repeated in (False, True):
            result = run_command(args, root, store, runtime, dict(GET=0, POST=0, DELETE=0), 'smoke-v1')
            assert result['status'] == 'DEMO_BUDGET_UPDATED'
            assert result['idempotent'] is repeated
    assert not writes(requests)
    assert not writes(requests, 'DELETE')
