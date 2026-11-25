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


def test_native_instance_borrows_the_shared_runner_and_explicit_funding(tmp_path):
    from factorlab.integrations.alpaca.strategy_instances import StrategyInstances
    binding = dict(market='ALPACA_US_EQUITY', account_id='paper', strategy_id='V69_CAUSAL',
                   instance_id='V69_CAUSAL', allocation_id='v69-original')
    host = StrategyInstances(tmp_path, Account(), dict(releases=str(tmp_path / 'releases'),
        trusted_python=True, instances=[dict(binding=binding, native_v69=True,
            funding={'capital': '50', 'initial_cash': '50'})]), clock=lambda: NOW, revision='a' * 40)
    native = host.native['V69_CAUSAL']
    assert native.runner is host.runner
    assert native.store is host.store
    assert host.store.cash('paper', 'V69_CAUSAL') == 50
    assert set(host.symbols) == set(ASSETS)
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
