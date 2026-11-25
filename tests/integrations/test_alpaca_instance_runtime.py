from contextlib import ExitStack, closing
from dataclasses import asdict, replace
from decimal import Decimal as D
from datetime import timedelta

import pytest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pandas as pd

from examples.alpaca_balanced.strategy import BalancedPortfolio
from factorlab.integrations.alpaca.market_data.inputs import InstrumentQuote, normalize_bars
from factorlab.integrations.alpaca.portfolio import AlpacaPortfolioRunner
from factorlab.integrations.alpaca.strategy_assets import StrategyAssets
from factorlab.integrations.alpaca.strategy_data import StrategyData
from factorlab.integrations.alpaca.strategy_execution import AlpacaDecisionExecution
from factorlab.integrations.alpaca.strategy_deployment import AlpacaDeployment
from factorlab.integrations.alpaca.strategy_runtime import AlpacaInstance
from factorlab.integrations.alpaca.strategy_instances import StrategyInstances
from factorlab.strategies.decision_runtime import InstanceDecisionRuntime
from factorlab.strategies.deployment import DeploymentStore, DeploymentController
from factorlab.strategies.loader import LoadedStrategy
from factorlab.strategies.package import build_package, install_package
from factorlab.trading.contracts import Market
from factorlab.trading.portfolio_execution import PortfolioStore
from test_alpaca_portfolio import Adapter, Missing, NOW
from test_alpaca_scheduling import calendar


class Account(Adapter):
    def read_account(self):
        return replace(super().read_account(), market=Market.ALPACA_US_EQUITY)

    def read_asset(self, symbol):
        return dict(id='official-' + symbol, symbol=symbol, **{'class': 'us_equity'},
                    status='active', tradable=True, fractionable=True)


@pytest.mark.parametrize('managed', [False, True])
@pytest.mark.parametrize('interval_seconds', [None, 60, 3600])
def test_real_package_two_asset_execution_and_restart_do_not_repeat_decision_or_post(tmp_path, interval_seconds, monkeypatch, managed):
    now = NOW + timedelta(hours=1)
    monkeypatch.setattr('test_alpaca_portfolio.NOW', now)
    parameters = dict(account_id='paper', symbols=['SPY', 'IWM'], capital='100',
                      interval_seconds=interval_seconds)
    declaration = BalancedPortfolio(**parameters).describe()
    data = asdict(declaration)
    requirements = {key: data[key] for key in ('data', 'schedule', 'state', 'capital', 'execution')}
    requirements['capital']['requested'] = '100'
    source = Path(__file__).parents[2] / 'examples' / 'alpaca_balanced'
    archive = tmp_path / 'balanced.zip'
    manifest = build_package(source, dict(schema_version=2, interface='FACTORLAB_DECISION_V2',
        strategy_id=declaration.strategy_id, strategy_version='1', market=declaration.market,
        account_id='paper', symbols=declaration.symbols, execution_mode='target_weights',
        requirements=requirements, code_commit='a' * 40, entrypoint='strategy:BalancedPortfolio',
        files={'strategy.py': ''}, configuration=parameters, dependencies=[],
        evidence={'offline': True}, migration='wait_flat'), archive)
    release, _ = install_package(archive, tmp_path / 'releases')
    binding = dict(market=declaration.market, account_id='paper', strategy_id=declaration.strategy_id,
                   instance_id='balanced', allocation_id='balanced-usd')
    account = Account()
    cal = calendar()
    def history(instrument, *args, **kwargs):
        if interval_seconds is None:
            rows = [dict(t='2026-11-23T05:00:00Z', o=10, h=10, l=10, c=10, v=100)]
        else:
            rows = [dict(t=stamp.isoformat(), o=10, h=10, l=10, c=10, v=100)
                    for stamp in pd.date_range('2026-11-24T14:30:00Z', periods=90, freq='min')]
        return normalize_bars(rows, instrument, calendar=cal, timeframe=kwargs['timeframe'],
                              received_at=now, feed='iex')
    def refresh(instruments, **kwargs):
        return {i.instrument_id: InstrumentQuote(i, D(10), D(10), pd.Timestamp(now),
            pd.Timestamp(now), 'alpaca', 'iex', pd.Timestamp('2026-11-24'),
            tuple(pd.Timestamp(t) for t in kwargs['decision_window']), ()) for i in instruments}
    service = SimpleNamespace(calendar=cal, history=history, refresh_quotes=refresh)
    for iteration in range(2):
        with ExitStack() as stack:
            store = stack.enter_context(closing(PortfolioStore(tmp_path / 'portfolio.sqlite3')))
            runner = AlpacaPortfolioRunner(account, store, order_not_found=Missing, clock=lambda: now)
            deployments = stack.enter_context(closing(DeploymentStore(tmp_path / 'deployments.sqlite3')))
            deployments.register_instance(binding)
            view = stack.enter_context(closing(deployments.for_instance('balanced')))
            if iteration == 0:
                runner.executor.allocate(account_id='paper', strategy_id='balanced',
                    allocation_id='balanced-usd', capital=D(100), initial_cash=D(100), official_equity=D(100))
                promotion = dict(schema='FACTORLAB_CHAMPION_PROMOTION_V2', binding=binding,
                    selection_id='offline', decision='PROMOTE', qualified=True, strictly_better=True,
                    expected_active=None, package_id=manifest['package_id'], evidence=manifest['evidence'])
                view.request(manifest, promotion)
            strategy = LoadedStrategy.from_strategy_package(release, trusted=True)
            assets = StrategyAssets(account, store, clock=lambda: now)
            instance = AlpacaInstance(tmp_path, binding, manifest['package_id'], strategy.spec,
                None, runner, StrategyData(assets, clock=lambda: now), clock=lambda: now)
            deployment = AlpacaDeployment(instance, view, boot_id='offline-' + str(iteration))
            assert DeploymentController(view, deployment, load_strategy=lambda _: strategy).step()['active'] == manifest['package_id']
            allocation = dict(currency='USD', capital=D(100))
            execution = AlpacaDecisionExecution(runner.executor, assets, strategy.spec, binding,
                                               allocation, clock=lambda: now)
            runtime = InstanceDecisionRuntime(strategy, view, execution, allocation=allocation)
            instance.runtime = runtime
            for _ in range(3):
                instance.tick(service, now, account.read_account())
            assert instance.state['state'] == 'RUNNING'
            assert store.holdings('paper', 'balanced') == {'SPY': D('4.5'), 'IWM': D('4.5')}
            assert len(account.posts) == 2
            assert view.checkpoint() == {'events': 1}
            assert len(list((tmp_path / 'events').glob('*.json'))) == 1
    account.http = SimpleNamespace(request=lambda *args, **kwargs: [])
    account.read_corporate_activities = lambda **kwargs: []
    configuration = dict(releases=str(tmp_path / 'releases'),
        trusted_python=True, instances=[dict(binding=binding,
            funding={'capital': '100', 'initial_cash': '100'})])
    if managed:
        configuration['capital_policy'] = dict(currency='USD', budget='100', fee_rate='0')
    host = StrategyInstances(tmp_path, account, configuration, clock=lambda: now)
    assert bool(host.store.db.execute('SELECT 1 FROM capital_accounts').fetchone()) == managed
    if managed:
        assert host.store.db.execute('SELECT symbol FROM capital_symbols ORDER BY symbol').fetchall() == [('IWM',), ('SPY',)]
    host.tick(service, {}, now, account.read_account_details())
    assert host.symbols == ('IWM', 'SPY')
    assert host.state['instances']['balanced']['state'] == 'RUNNING'
    assert len(account.posts) == 2
    host.close()


@pytest.mark.parametrize('phase', ['COMPLETE', 'ABANDONED'])
def test_terminal_original_orders_reconcile_before_receipt_recovery(tmp_path, phase):
    calls = []
    plan = {'id': 'old', 'phase': phase}
    runner = SimpleNamespace(report=lambda identity: calls.append('orders'), store=Mock())
    runtime = SimpleNamespace(recover=lambda: calls.append('receipts'))
    instance = AlpacaInstance(tmp_path, {'instance_id': 'owner'}, 'package', None, runtime, runner, None, clock=lambda: NOW)
    instance.plans = lambda: [plan]
    assert instance.recover(None, None)
    assert calls == ['orders', 'receipts']


@pytest.mark.parametrize('finished', [False, True])
def test_open_plan_advances_before_receipt_replay_and_gates_new_event(tmp_path, finished):
    calls = []
    plan = dict(id='old', phase='EXECUTING', request=dict(session=str(NOW.date()), owned=['SPY']))
    store = Mock()
    store.plan.return_value = plan
    def advance(*args, **kwargs):
        calls.append('advance')
        if finished:
            plan['phase'] = 'COMPLETE'
    runner = SimpleNamespace(store=store, report=lambda _: calls.append('orders'), run_once=advance)
    runtime = SimpleNamespace(recover=lambda: calls.append('receipts'))
    assets = Mock()
    data = SimpleNamespace(assets=assets, quotes=lambda *args, **kwargs:
        {'SPY': SimpleNamespace(ask=D('.1234'), valid=True)})
    spec = SimpleNamespace(execution={'fractional_quantities': False})
    instance = AlpacaInstance(tmp_path, {'instance_id': 'owner'}, 'package', spec, runtime, runner, data, clock=lambda: NOW)
    instance.plans = lambda: [plan]
    assert instance.recover(None, pd.Timestamp(NOW)) is finished
    assert calls == ['orders', 'advance'] + (['receipts'] if finished else [])
    assets.rules.assert_called_once_with(['SPY'], prices={'SPY': D('.1234')}, fractional_quantities=False)


def test_subdollar_rules_preserve_whole_share_package_restriction(tmp_path):
    with closing(PortfolioStore(tmp_path / 'rules.sqlite3')) as store:
        assets = StrategyAssets(Account(), store, clock=lambda: NOW)
        rule = assets.rules(['SPY'], prices={'SPY': D('.1234')}, fractional_quantities=False)['SPY']
        assert rule.price_tick == D('.0001')
        assert rule.quantity_step == D(1)
        fractional = assets.rules(['SPY'], prices={'SPY': D('.1234')}, fractional_quantities=True)['SPY']
        assert fractional.quantity_step == D('.000000001')
