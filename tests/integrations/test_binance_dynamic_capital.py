"""Dynamic capital runs through the signed Demo adapter on offline HTTP."""
from contextlib import closing, contextmanager
from datetime import timedelta
from decimal import Decimal as D
import json
from multiprocessing import get_context
from os import _exit
from types import SimpleNamespace

import pytest

from factorlab.integrations.binance.client import Response
from factorlab.integrations.binance_futures_trading.demo_cli import journal_id, open_session
from factorlab.integrations.binance_futures_trading.demo_instance_capability import DemoInstanceCapability
from factorlab.integrations.binance_futures_trading.demo_instance_execution import DemoInstanceExecutionAdapter
from factorlab.integrations.binance_futures_trading.demo_instance_owner import DemoInstanceOwner
from factorlab.integrations.binance_futures_trading.demo_market_feed import FeedPaused
from factorlab.integrations.binance_futures_trading.demo_runtime import DemoRuntime
from factorlab.integrations.binance_futures_trading.fake import MS
from factorlab.trading import AccountSnapshot, AssetBalance, Market, OrderStatus
from factorlab.trading.capital_cli import command_result, enqueue
from factorlab.trading.portfolio_execution import ExecutionQuote, InstrumentRules, PortfolioExecutor, PortfolioStore
from factorlab.trading.storage import PaperStore, decode
from test_binance_demo_execution import NOW, intent
from test_binance_demo_network import NetworkFixture, wired
from test_binance_demo_strategy_session import deployment


@pytest.fixture
def position_margin(deployment, monkeypatch):
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


def test_demo_http_funding_margin_and_capital_survive_restart(deployment, monkeypatch, position_margin):
    root, credentials, campaign, fake = deployment
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


def _exit_before_portfolio_commit(controller_path, portfolio_path, account_id, decision):
    """Abort actual plan preparation after its SQLite inserts, before commit."""
    account = AccountSnapshot(Market.BINANCE_USDM, account_id,
                              (AssetBalance('USDT', D(1000)),), (), (), NOW, True)
    facts = SimpleNamespace(account_id=account_id, observed_at=NOW, source_complete=True)
    adapter = SimpleNamespace(read_account=lambda: account, margin_facts=lambda: facts,
        funding_events=lambda: (), funding_coverage=lambda: {
            'complete': True, 'covered_from_ms': 0, 'covered_through_ms': MS})
    with closing(PaperStore(controller_path)) as controller_store, closing(PortfolioStore(portfolio_path)) as store:
        session = SimpleNamespace(adapter=SimpleNamespace(account_id=account_id),
                                  controller=SimpleNamespace(store=controller_store, broker=None))
        executor = PortfolioExecutor(adapter, store, quote_currency='USDT')
        execution = DemoInstanceExecutionAdapter(session, executor=executor)
        store.capital_policy.validate_orders = lambda *args, **kwargs: _exit(29)
        execution.prepare_plan(decision, now=NOW)


def test_accepted_buy_protects_capital_until_prepared_and_reconciled(deployment, monkeypatch, position_margin):
    root, credentials, campaign, fake = deployment

    @contextmanager
    def open_owner(runtime):
        with (open_session(root, credentials, runtime, dict(GET=0, POST=0, DELETE=0),
                           campaign=campaign) as (session, _),
              closing(PortfolioStore(root / 'pending-capital.sqlite3')) as store):
            monkeypatch.setattr('factorlab.integrations.binance_futures_trading.network.datetime',
                                SimpleNamespace(now=lambda tz: session._now()))
            monkeypatch.setattr('factorlab.integrations.binance_futures_trading.demo_runtime.now', session._now)
            session.gateway.recover(session.controller)
            capability = DemoInstanceCapability(session, runtime, dispatch_gate=lambda binding: None)
            yield DemoInstanceOwner.open(session, store, capability=capability)

    def unavailable(*args):
        raise FeedPaused('symbol temporarily unavailable')

    def timed_out(*args):
        raise TimeoutError('order outcome unavailable')

    with closing(DemoRuntime(root)) as runtime:
        runtime.start_continuous(credentials.read_config(), opening_equity=D(1000), stop_epoch=runtime.stop_epoch())
        with open_owner(runtime) as owner:
            account = owner.session.adapter.account_id
            executor, manager = owner.execution.executor, owner.capital
            store = executor.store
            store.capital_policy.configure(account_id=account, currency='USDT', budget=D(27),
                                           fee_rate=D('.001'), mode='SHARED_SAME_DIRECTION')
            for name in ('one', 'two'):
                executor.allocate(account_id=account, strategy_id=name, allocation_id=name + '-funded',
                                  capital=D(7), initial_cash=D(7), official_equity=D(1000))
                store.capital_policy.bind(account_id=account, strategy_id=name, instance_id=name,
                                          allocation_id=name + '-funded')
            binding = dict(account_id=account, instance_id='one', allocation_id='one-funded')
            owner.execution.store.record_metadata('demo:instance:one', dict(binding=binding,
                symbols=['NVDAUSDT'], shorting=False, allocation=dict(budget_usdt=D(7))))
            receipt = dict(binding=binding, package_id='package', event_id='accepted',
                input_identity='original-input', event={}, decision=dict(kind='orders',
                    orders=[intent('native-buy', limit='100', quantity='.04')], cancellations=[]))
            owner.execution.execute(receipt)
            owner.execution.execute(receipt)
            decision, payload = owner.execution.store.db.execute('SELECT id,payload FROM demo_instance_outbox').fetchone()
            original_id = decode(json.loads(payload))['intents'][0].client_order_id
            view = manager.snapshot(account)
            assert view.strategies[0].reserved == D('4.004')
            assert view.strategies[0].withdrawable == D('2.996')

            owner.market_sources = SimpleNamespace(require=lambda symbols: None, book=unavailable)
            enqueue(store, account, 'withdraw-too-much', dict(action='change', event_id='too-much',
                source='one', destination=None, amount=D(3), mode='EXACT'))
            assert owner.poll() == {}
            rejected = command_result(store, account, 'withdraw-too-much')['data']
            assert rejected['status'] == 'INSUFFICIENT_CAPITAL' and rejected['executable'] == D('2.996')
            transfer = manager.change(account_id=account, event_id='transfer-too-much',
                source='one', destination='two', amount=D(3))
            assert transfer.status == 'INSUFFICIENT_CAPITAL' and transfer.applied == 0
            with pytest.raises(ValueError, match='prepared or rejected'):
                manager.release(account_id=account, event_id='retire', strategy_id='one')

            enqueue(store, account, 'withdraw-free', dict(action='change', event_id='withdraw',
                source='one', destination=None, amount=D(2), mode='EXACT'))
            assert owner.poll() == {}
            assert command_result(store, account, 'withdraw-free')['data']['applied'] == D(2)
            moved = manager.change(account_id=account, event_id='transfer', source='one',
                                   destination='two', amount=D('.5'))
            assert moved.applied == D('.5')
            assert not fake.orders

            controller_path = root / journal_id(account) / 'controller.sqlite3'
            process = get_context('spawn').Process(target=_exit_before_portfolio_commit,
                args=(controller_path, root / 'pending-capital.sqlite3', account, decision))
            process.start()
            process.join(30)
            assert process.exitcode == 29

        with open_owner(runtime) as owner:
            executor, manager = owner.execution.executor, owner.capital
            store = executor.store
            assert not store.db.execute('SELECT 1 FROM portfolio_orders').fetchone()
            assert manager.snapshot(account).strategies[0].reserved == D('4.004')
            plan = owner.execution.prepare_plan(decision, now=NOW)
            assert owner.execution.prepare_plan(decision, now=NOW) == plan
            view = manager.snapshot(account)
            one, two = view.strategies
            assert one.reserved == D('4.004') and not one.pending_decisions
            assert (one.allocated, two.allocated, view.unallocated) == (D('4.5'), D('7.5'), D(15))
            assert view.pool_cash + one.cash + two.cash == view.official_wallet == D(1000)
            at = owner.session._now()
            quotes = {'NVDAUSDT': ExecutionQuote('NVDAUSDT', D(100), D(100), at, at, 'accepted', 'OFFLINE_HTTP')}
            rules = {'NVDAUSDT': InstrumentRules(D('.01'), D(1), price_tick=D('.01'))}
            owner.execution.advance(decision, equity=D(1000), quotes=quotes, rules=rules, now=at)
            assert fake.orders[original_id]['status'] == 'NEW'
            fake.trade_update(original_id, quantity='.02')
            one = manager.snapshot(account).strategies[0]
            assert one.occupied == D(2) and one.reserved == D('2.002')

            with monkeypatch.context() as fault:
                fault.setattr(executor.adapter, 'cancel_order', timed_out)
                executor.cancel(plan, original_id)
                assert store.orders(plan)[0]['uncertain']
                fault.setattr(executor.adapter, 'query_order', timed_out)
                unknown = manager.snapshot(account).strategies[0]
                assert unknown.reserved == D('2.002') and unknown.withdrawable is None
                with pytest.raises(ValueError, match='reconciled'):
                    manager.change(account_id=account, event_id='cancel-unknown',
                                   source='one', destination='two', amount=D(1))

            assert fake.orders[original_id]['status'] == 'PARTIALLY_FILLED'
            executor.cancel(plan, original_id)
            assert store.orders(plan)[0]['record'].snapshot.status is OrderStatus.CANCELED
            settled = manager.snapshot(account)
            assert settled.strategies[0].reserved == 0
            assert settled.pool_cash + sum(strategy.cash for strategy in settled.strategies) == fake.cash
            assert runtime.consumption(None)['posts'] == 1
