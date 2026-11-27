"""An unknown performance strategy cannot block either market's capital queue."""
from contextlib import closing, contextmanager
from decimal import Decimal as D
from types import SimpleNamespace

import pytest

from factorlab.integrations.alpaca.strategy_instances import StrategyInstances
from factorlab.integrations.binance_futures_trading.demo_cli import open_session
from factorlab.integrations.binance_futures_trading.demo_instance_owner import DemoInstanceOwner
from factorlab.integrations.binance_futures_trading.demo_runtime import DemoRuntime
from factorlab.trading import Market
from factorlab.trading.capital_cli import command_result, enqueue
from factorlab.trading.portfolio_execution import PortfolioStore
from test_alpaca_dynamic_capital import CapitalOfficial
from test_alpaca_execution import adapter
from test_binance_demo_network import wired
from test_binance_demo_strategy_session import deployment
from test_shared_portfolio_execution import ACCOUNT, NOW


@pytest.fixture
def alpaca_owner(tmp_path):
    official = CapitalOfficial()

    def transport(method, path, params, body):
        if path == '/v2/corporate_actions/announcements':
            return 200, []
        return official(method, path, params, body)

    binding = dict(account_id=ACCOUNT, market=Market.ALPACA_US_EQUITY.value,
                   strategy_id='strategy', instance_id='instance', allocation_id='funded')
    configuration = dict(releases=tmp_path / 'releases', trusted_python=True,
        capital_policy=dict(currency='USD', budget='100', fee_rate='0'),
        instances=[dict(binding=binding, funding=dict(capital='7', initial_cash='7'))])

    @contextmanager
    def open_owner():
        actual, _ = adapter(tmp_path, transport)
        with closing(actual), closing(StrategyInstances(tmp_path, actual, configuration, clock=lambda: NOW)) as host:
            service = SimpleNamespace()
            yield host.store, ACCOUNT, lambda: host.tick(service, {}, NOW, actual.read_account_details())

    return open_owner


@pytest.fixture
def binance_owner(deployment):
    root, credentials, _, _ = deployment

    @contextmanager
    def open_owner():
        with (closing(DemoRuntime(root)) as runtime,
              open_session(root, credentials, runtime, dict(GET=0, POST=0, DELETE=0)) as (session, _),
              closing(PortfolioStore(root / 'portfolio.sqlite3')) as store):
            session.gateway.recover(session.controller)
            owner = DemoInstanceOwner.open(session, store, capability=None)
            account_id = session.adapter.account_id
            store.capital_policy.configure(account_id=account_id, currency='USDT', budget=D(100), fee_rate=D(0))
            owner.execution.executor.allocate(account_id=account_id, strategy_id='instance',
                allocation_id='funded', capital=D(7), initial_cash=D(7), official_equity=D(1000))
            store.capital_policy.bind(account_id=account_id, strategy_id='instance',
                                      instance_id='instance', allocation_id='funded')
            yield store, account_id, owner.poll

    return open_owner


@pytest.mark.parametrize('owner_fixture', ['binance_owner', 'alpaca_owner'])
def test_unknown_performance_command_is_rejected_and_owner_continues(request, owner_fixture):
    open_owner = request.getfixturevalue(owner_fixture)
    with open_owner() as (store, account_id, tick):
        enqueue(store, account_id, 'bad', dict(action='performance', strategy_id='missing'))
        enqueue(store, account_id, 'top-up', dict(action='change', event_id='add', source=None,
                                                 destination='instance', amount=D(3)))

        tick()
        rejection = command_result(store, account_id, 'bad')
        assert rejection == dict(status='REJECTED', reason="unknown strategy ID 'missing'")

        tick()
        top_up = command_result(store, account_id, 'top-up')
        assert top_up['status'] == 'COMPLETE'
        assert top_up['data']['applied'] == D(3)

    with open_owner() as (store, account_id, tick):
        assert command_result(store, account_id, 'bad') == rejection
        enqueue(store, account_id, 'report', dict(action='performance', strategy_id='instance'))
        tick()
        report = command_result(store, account_id, 'report')
        assert report['status'] == 'COMPLETE'
        assert report['data']['returns']['capital_in'] == D(3)
