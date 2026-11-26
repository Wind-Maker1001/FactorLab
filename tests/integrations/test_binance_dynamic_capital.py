"""Dynamic capital runs through the signed Demo adapter on offline HTTP."""
from contextlib import closing
from datetime import timedelta
from decimal import Decimal as D
import json
from types import SimpleNamespace

from factorlab.integrations.binance.client import Response
from factorlab.integrations.binance_futures_trading.demo_cli import open_session
from factorlab.integrations.binance_futures_trading.demo_instance_capability import DemoInstanceCapability
from factorlab.integrations.binance_futures_trading.demo_instance_owner import DemoInstanceOwner
from factorlab.integrations.binance_futures_trading.demo_runtime import DemoRuntime
from factorlab.integrations.binance_futures_trading.fake import MS
from factorlab.trading import OrderStatus, Side
from factorlab.trading.portfolio_execution import ExecutionQuote, InstrumentRules, PortfolioStore
from test_binance_demo_execution import intent
from test_binance_demo_network import NetworkFixture, wired
from test_binance_demo_strategy_session import deployment


def test_demo_http_funding_margin_and_capital_survive_restart(deployment, monkeypatch):
    root, credentials, campaign, fake = deployment
    original = NetworkFixture.__call__

    def position_margin(self, request, timeout):
        result = original(self, request, timeout)
        if request.path == '/fapi/v3/account':
            body = json.loads(result.body)
            for row in body['positions']:
                row['positionInitialMargin'] = row['initialMargin']
            return Response(result.status, result.headers, json.dumps(body).encode())
        return result

    monkeypatch.setattr(NetworkFixture, '__call__', position_margin)
    db = root / 'dynamic-capital.sqlite3'
    with closing(DemoRuntime(root)) as runtime:
        runtime.start_continuous(credentials.read_config(), opening_equity=D(1000),
                                 stop_epoch=runtime.stop_epoch())
        for restart in (False, True):
            with open_session(root, credentials, runtime, dict(GET=0, POST=0, DELETE=0),
                              campaign=campaign) as (session, _), closing(PortfolioStore(db)) as store:
                monkeypatch.setattr('factorlab.integrations.binance_futures_trading.network.datetime',
                    SimpleNamespace(now=lambda tz: session._now()))
                monkeypatch.setattr('factorlab.integrations.binance_futures_trading.demo_runtime.now', session._now)
                capability = DemoInstanceCapability(session, runtime, dispatch_gate=lambda binding: None)
                owner = DemoInstanceOwner.open(session, store, capability=capability)
                executor, manager = owner.execution.executor, owner.capital
                account = session.adapter.account_id
                session.gateway.recover(session.controller)
                if not restart:
                    store.capital_policy.configure(account_id=account, currency='USDT', budget=D(27),
                        fee_rate=D('.001'), mode='SHARED_SAME_DIRECTION')
                    for strategy in ('one', 'two'):
                        allocation_id = strategy + '-funded'
                        executor.allocate(account_id=account, strategy_id=strategy, allocation_id=allocation_id,
                            capital=D(7), initial_cash=D(7), official_equity=D(1000))
                        store.capital_policy.bind(account_id=account, strategy_id=strategy,
                            instance_id=strategy, allocation_id=allocation_id)
                        session.controller.store.record_metadata('demo:instance:' + strategy,
                            dict(binding=dict(account_id=account, instance_id=strategy,
                                             allocation_id=allocation_id),
                                 symbols=['NVDAUSDT'], allocation=dict(budget_usdt=D(7))))
                    entry = intent('di-capital-entry', limit='100', quantity='.04')
                    owner.execution.owners.claim(entry, instance_id='one', allocation_id='one-funded')
                    at = session._now()
                    plan = executor.create(account_id=account, strategy_id='one', strategy_version='2',
                        trading_session='demo', weights={}, owned_symbols=['NVDAUSDT'],
                        now=at - timedelta(milliseconds=2),
                        direct_orders=[(entry, D(100))])
                    quote = ExecutionQuote('NVDAUSDT', D(100), D(100), at, at, 'demo', 'OFFLINE_HTTP')
                    rules = InstrumentRules(D('.01'), D(1), price_tick=D('.01'))
                    executor.run_once(plan, equity=D(1000), quotes={'NVDAUSDT': quote},
                                      rules={'NVDAUSDT': rules}, now=at)
                    assert fake.orders[entry.client_order_id]['status'] == 'NEW'
                    live = manager.snapshot(account).strategies[0]
                    assert live.reserved == D('4.004') and live.withdrawable == D('2.996')
                    fake.trade_update(entry.client_order_id, quantity='.02')
                    fake.funding_update('-.02', time_ms=MS + 2)
                    funded = manager.snapshot(account).strategies[0]
                    assert funded.occupied == D(2)
                    assert funded.cash == D('6.978') and funded.funding == D('-.02')
                    assert funded.unrealized_pnl == 0 and funded.execution_fees == D('.002')
                    assert funded.reserved == D('2.002')
                    executor.cancel(plan, entry.client_order_id)
                    assert store.orders(plan)[0]['record'].snapshot.status is OrderStatus.CANCELED
                    change = manager.change(account_id=account, event_id='resize',
                        source='one', destination='two', amount=D(4))
                    assert change.applied == D(4)
                    assert fake.quantity == D('.02')
                else:
                    replay = manager.change(account_id=account, event_id='resize',
                        source='one', destination='two', amount=D(4))
                    assert replay == change
                    view = manager.snapshot(account)
                    one, two = view.strategies
                    assert (one.allocated, two.allocated, view.unallocated) == (D(3), D(11), D(13))
                    assert (one.cash, two.cash, one.occupied, one.reserved) == (D('2.978'), D(11), D(2), D(0))
                    assert view.pool_cash + one.cash + two.cash == fake.cash == view.official_wallet
                    assert store.holdings(account, 'two') == {}
                    assert runtime.consumption(None)['posts'] == 1
