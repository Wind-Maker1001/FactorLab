from decimal import Decimal as D

import pandas as pd
import pytest

from factorlab.integrations.alpaca.native_v69 import NativeWeeklyExecution
from factorlab.integrations.alpaca.cloud_execution import WeeklyExecution
from factorlab.integrations.alpaca.market_data.sessions import AlpacaSessionCalendar
from factorlab.strategies.v69 import ASSETS, V69Strategy
from factorlab.strategies.v69_alpaca import create_alpaca_plan
from test_alpaca_instance_runtime import Account
from test_alpaca_portfolio import NOW


@pytest.mark.parametrize('managed', [False, True])
def test_native_instance_borrows_the_shared_runner_and_explicit_funding(tmp_path, managed):
    from factorlab.integrations.alpaca.strategy_instances import StrategyInstances
    binding = dict(market='ALPACA_US_EQUITY', account_id='paper', strategy_id='V69_CAUSAL',
                   instance_id='V69_CAUSAL', allocation_id='v69-original')
    configuration = dict(releases=str(tmp_path / 'releases'),
        trusted_python=True, instances=[dict(binding=binding, native_v69=True,
            funding={'capital': '50', 'initial_cash': '50'})])
    if managed:
        configuration['capital_policy'] = dict(currency='USD', budget='50', fee_rate='0')
    host = StrategyInstances(tmp_path, Account(), configuration, clock=lambda: NOW, revision='a' * 40)
    native = host.native['V69_CAUSAL']
    assert native.runner is host.runner
    assert native.store is host.store
    assert host.store.cash('paper', 'V69_CAUSAL') == 50
    assert set(host.symbols) == set(ASSETS)
    if managed:
        policy = host.store.capital_policy
        assert {symbol for symbol, in host.store.db.execute('SELECT symbol FROM capital_symbols')} == set(ASSETS)
        policy.pause('paper', 'V69_CAUSAL')
        with pytest.raises(ValueError, match='paused'):
            create_alpaca_plan(host.runner.executor, now=NOW, **dict(inputs(),
                decision=dict(weights=dict.fromkeys(ASSETS, 0), orders=[], state={})))
    host.close()


def test_native_release_switch_requires_promotion_and_recovers_activation(tmp_path, monkeypatch):
    from factorlab.strategies.package import install_package
    from test_alpaca_instance_switch import request

    account = Account()
    original = NativeWeeklyExecution(tmp_path, account, clock=lambda: NOW, revision='a' * 40)
    old = original.package_id
    original.close()
    execution = NativeWeeklyExecution(tmp_path, account, clock=lambda: NOW, revision='b' * 40)
    assert execution.package_id == old
    _, candidate = install_package(tmp_path / ('native-v69-' + 'b' * 40 + '.zip'),
                                   tmp_path / 'strategy-releases')
    request(execution.view, candidate, 'native-release-switch')
    dispatched = []
    monkeypatch.setattr(WeeklyExecution, 'tick', lambda *args: dispatched.append(True))

    def unavailable(version):
        raise OSError('activation interrupted')

    execution.controller.adapter.activated = unavailable
    execution.tick(None, {}, NOW, account.read_account_details())
    assert not dispatched
    assert execution.view.pending() is None
    assert execution.view.activation_callback()['reason'] == 'OSError'
    execution.close()
    execution = NativeWeeklyExecution(tmp_path, account, clock=lambda: NOW, revision='b' * 40)
    execution.tick(None, {}, NOW, account.read_account_details())
    assert execution.package_id == candidate['package_id']
    assert execution.view.activation_callback() is None
    assert dispatched == [True]
    assert not account.posts
    execution.close()


def inputs():
    return dict(account_id='paper', session='2026-11-23', raw_holdings={},
                factors=dict.fromkeys(ASSETS, D(1)), quantity_steps=dict.fromkeys(ASSETS, D('.000000001')),
                initial_inventory={}, signal_provenance={'source': 'ALPACA:iex'})


def test_native_adopts_checkpoint_and_keeps_original_plan_identity_on_frozen_replay(tmp_path):
    account = Account()
    execution = WeeklyExecution(tmp_path, account, clock=lambda: NOW)
    prepared = dict(inputs(), decision=dict(weights=dict.fromkeys(ASSETS, .04),
        orders=[dict(symbol='AAPL', side='buy', adjusted_quantity=2.25)],
        state={'last_week': [2026, 48]}))
    identity = create_alpaca_plan(execution.runner.executor, now=NOW, **prepared)
    execution.close()
    execution = NativeWeeklyExecution(tmp_path, account, clock=lambda: NOW, revision='a' * 40)
    assert execution.view.checkpoint() == {'checkpoint': prepared['decision']['state']}
    assert execution.resume_prepared(prepared) == identity
    assert execution.view.checkpoint()['checkpoint'] == prepared['decision']['state']
    execution.close()
    execution = NativeWeeklyExecution(tmp_path, account, clock=lambda: NOW, revision='a' * 40)
    assert execution.resume_prepared(prepared) == identity
    assert execution.store.db.execute('SELECT COUNT(*) FROM portfolio_plans').fetchone()[0] == 1
    assert not account.posts
    execution.close()


def test_native_runtime_upgrade_preserves_frozen_plan_and_rollback(tmp_path, monkeypatch):
    from contextlib import closing
    from factorlab.strategies import loader
    from factorlab.strategies.deployment import DeploymentStore

    account = Account()
    previous_sha = 'a' * 64
    replacement_sha = 'b' * 64
    package_revision = '1' * 40
    replacement_revision = '2' * 40
    monkeypatch.setattr(loader, 'runtime_identity', lambda: previous_sha)
    prepared = dict(inputs(), decision=dict(weights=dict.fromkeys(ASSETS, .04),
        orders=[dict(symbol='AAPL', side='buy', adjusted_quantity=1)],
        state={'last_week': [2026, 48]}))
    with closing(NativeWeeklyExecution(tmp_path, account, clock=lambda: NOW,
                                       revision=package_revision)) as original:
        plan_id = original.resume_prepared(prepared)
        package_id = original.package_id
        old_binding = original.strategy.binding
        old_checkpoint = original.view.checkpoint()

    with closing(DeploymentStore(tmp_path / 'deployments.sqlite3', instance_id='V69_CAUSAL')) as store:
        old_payload = store._runtime_binding(package_id)['binding']
        replacement = dict(old_binding, runtime_sha256=replacement_sha)
        with pytest.raises(ValueError, match='runtime identity changed'):
            store.bind_runtime(package_id, replacement)
        changed_model = dict(replacement, model={'artifact_sha256': '3' * 64})
        with pytest.raises(ValueError, match='strategy identity'):
            store.admit_runtime_upgrade(package_id, old_binding, changed_model)
        insert = store._insert

        def interrupt(table, values):
            insert(table, values)
            raise OSError('upgrade interrupted before commit')

        with monkeypatch.context() as crash:
            crash.setattr(store, '_insert', interrupt)
            with pytest.raises(OSError, match='upgrade interrupted'):
                store.admit_runtime_upgrade(package_id, old_binding, replacement)
        with pytest.raises(ValueError, match='runtime identity changed'):
            store.bind_runtime(package_id, replacement)
        assert store.admit_runtime_upgrade(package_id, old_binding, replacement)
        assert not store.admit_runtime_upgrade(package_id, old_binding, replacement)
        assert store._runtime_binding(package_id)['binding'] == old_payload

    for digest, revision in ((replacement_sha, replacement_revision), (previous_sha, package_revision)):
        monkeypatch.setattr(loader, 'runtime_identity', lambda: digest)
        with closing(NativeWeeklyExecution(tmp_path, account, clock=lambda: NOW, revision=revision)) as current:
            assert current.package_id == package_id
            assert current.resume_prepared(prepared) == plan_id
            assert current.view.checkpoint() == old_checkpoint
            assert current.store.db.execute('SELECT COUNT(*) FROM portfolio_plans').fetchone()[0] == 1
    assert not account.posts


@pytest.mark.parametrize('crash_before_plan', [False, True])
def test_native_package_decision_matches_legacy_plan_and_freezes_before_dispatch(tmp_path, crash_before_plan):
    account = Account()
    execution = NativeWeeklyExecution(tmp_path, account, clock=lambda: NOW, revision='a' * 40)
    days = pd.bdate_range(end='2026-11-20', periods=252)
    calendar = AlpacaSessionCalendar([
        dict(date=str(day.date()), open='09:30', close='16:00')
        for day in days.union(pd.DatetimeIndex(['2026-11-23']))],
        coverage_start=str((days[0] - pd.Timedelta(days=7)).date()), coverage_end='2026-11-23')
    close = pd.DataFrame({symbol: [10 + i * .01 for i in range(252)] for symbol in ASSETS}, index=days)
    volume = pd.DataFrame(100000., index=days, columns=ASSETS)
    context = dict(session='2026-11-23', calendar=calendar, close=close, volume=volume, holdings={}, cash=100.)
    expected = V69Strategy().decide(context, {})
    if crash_before_plan:
        def fail(receipt):
            raise OSError('crash before plan durability')
        execution.runtime.adapter.execute = fail
        with pytest.raises(OSError, match='crash before plan'):
            execution.decide(context, {}, bridge_inputs=inputs())
        assert execution.store.db.execute('SELECT COUNT(*) FROM portfolio_plans').fetchone()[0] == 0
        execution.close()
        execution = NativeWeeklyExecution(tmp_path, account, clock=lambda: NOW, revision='a' * 40)
        execution.prepare(None, {'execution_session': '2026-11-23'}, NOW, account.read_account_details())
        actual = execution.view.checkpoint()['original']
    else:
        actual = execution.decide(context, {}, bridge_inputs=inputs())
    assert actual == expected
    assert execution.event_path('2026-11-23').exists()
    original_id = create_alpaca_plan(execution.runner.executor, now=NOW,
                                   decision=expected, **inputs())
    assert execution.identity == original_id
    assert execution.store.plan(original_id)['request']['signal_inputs']['checkpoint'] == expected['state']
    assert execution.view.checkpoint()['original'] == expected
    execution.close()



def test_native_acknowledged_buy_plan_protects_donor_cash_across_restart(tmp_path):
    from contextlib import closing
    from dataclasses import replace
    from factorlab.integrations.alpaca.strategy_instances import StrategyInstances

    class OfficialAccount(Account):
        def read_account(self):
            return replace(super().read_account(), official_equity=D(100))

    binding = dict(market='ALPACA_US_EQUITY', account_id='paper', strategy_id='V69_CAUSAL',
                   instance_id='V69_CAUSAL', allocation_id='v69-original')
    configuration = dict(releases=tmp_path / 'releases', trusted_python=True,
        capital_policy=dict(currency='USD', budget='100', fee_rate='0'),
        instances=[dict(binding=binding, native_v69=True, funding=dict(capital='40', initial_cash='40'))])
    account = OfficialAccount()
    prepared = dict(inputs(), decision=dict(weights=dict.fromkeys(ASSETS, .04),
        orders=[dict(symbol='AAPL', side='buy', adjusted_quantity=1)],
        state={'last_week': [2026, 48]}))
    with closing(StrategyInstances(tmp_path, account, configuration, clock=lambda: NOW,
                                  revision='a' * 40)) as host:
        native = host.native['V69_CAUSAL']
        identity = native.resume_prepared(prepared)
        assert native.view.outstanding(native.package_id) == []
        assert host.store.orders(identity) == []
        deltas = host.store.plan(identity)['request']['quantity_deltas']
        assert {symbol: delta for symbol, delta in deltas.items() if delta} == {'AAPL': D(1)}

    with closing(StrategyInstances(tmp_path, account, configuration, clock=lambda: NOW,
                                  revision='a' * 40)) as host:
        blocked = host.capital.change(account_id='paper', event_id='withdraw-native-in-flight',
            source='V69_CAUSAL', destination=None, amount=D(40))
        assert blocked.status == 'INSUFFICIENT_CAPITAL' and blocked.applied == 0
        assert host.store.cash('paper', 'V69_CAUSAL') == D(40)
        result = host.runner.executor.expire(identity)
        assert result.phase == 'COMPLETE' and result.order_ids == ()
        complete = host.capital.change(account_id='paper', event_id='withdraw-native-after-expiry',
            source='V69_CAUSAL', destination=None, amount=D(40))
        assert complete.applied == D(40)
        assert host.store.cash('paper', 'V69_CAUSAL') == 0
        assert not account.posts
