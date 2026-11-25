from dataclasses import replace
from decimal import Decimal as D
from types import SimpleNamespace
import json

import pytest

from factorlab.integrations.binance_futures_trading.demo_instance_capability import DemoInstanceCapability
from factorlab.integrations.binance_futures_trading.demo_runtime import DemoRuntime, now
from factorlab.integrations.binance_futures_trading.protocol import SafetyBlocked
from factorlab.integrations.binance_futures_trading.rules import order_parameters
from factorlab.trading import OrderType
from factorlab.trading.storage import PaperStore
from test_binance_demo_execution import intent
from test_binance_demo_strategy_session import deployment
from test_binance_demo_network import wired


def test_capability_uses_existing_account_budget_and_records_original_receipt(tmp_path):
    runtime = DemoRuntime(tmp_path)
    store = PaperStore(tmp_path / 'controller.sqlite3')
    account = 'DEMO:FactorLab-Demo:fixture'
    binding = dict(account_id=account, instance_id='one', allocation_id='funded', configuration={})
    entry = replace(intent('di-original', quantity='.05'), order_type=OrderType.LIMIT, limit_price=D(100))
    facts = SimpleNamespace(usdt=lambda: dict(walletBalance=D(5000), unrealizedProfit=D(0)))
    session = SimpleNamespace(controller=SimpleNamespace(store=store),
        adapter=SimpleNamespace(account_id=account, facts=facts), _now=now,
        _instance_receipt_sinks={})
    store.record_metadata('demo:instance:one', dict(binding=binding, symbols=['NVDAUSDT'],
        allocation=dict(budget_usdt=D(7))))
    gates, reservations = [], []
    def reserve(offered, owner):
        reservations.append((offered.client_order_id, owner))
        return 'di-exit', D(5)
    factory = DemoInstanceCapability(session, runtime, exit_reservation=reserve,
        dispatch_gate=lambda admitted_binding: gates.append(admitted_binding['instance_id']))
    owner = ('one', 'funded', 'NVDAUSDT')
    try:
        runtime.start_continuous(binding, opening_equity=D(5000), stop_epoch=runtime.stop_epoch(),
            total_orders=2, total_notional=D(10))
        capability, guard = factory(entry, owner)
        assert capability.plan.symbol == 'NVDAUSDT'
        guard('POST', order_parameters(entry), capability.plan)
        assert gates == ['one']
        assert reservations == [('di-original', owner)]
        assert runtime.consumption(None) == dict(posts=1, submitted_notional='5.00')
        receipt = dict(method='POST', client_order_id='di-original', http_status=200, outcome_unknown=False)
        session._instance_receipt_sinks['di-original'](receipt)
        persisted = runtime.db.execute('SELECT receipt FROM dispatches WHERE client_id=?',
            ('di-original',)).fetchone()[0]
        assert json.loads(persisted) == receipt
        with pytest.raises(SafetyBlocked, match='already attempted'):
            guard('POST', order_parameters(entry), capability.plan)
        assert runtime.consumption(None)['posts'] == 1
        with pytest.raises(SafetyBlocked, match='ownership mismatch'):
            factory(entry, ('one', 'foreign', 'NVDAUSDT'))
    finally:
        store.close()
        runtime.close()


@pytest.mark.parametrize('lost_acknowledgement', [False, True])
def test_original_session_submits_recovers_and_cancels_with_instance_capability(deployment, monkeypatch, lost_acknowledgement):
    from contextlib import closing
    from factorlab.integrations.binance_futures_trading.demo_cli import open_session
    from factorlab.integrations.binance_futures_trading.demo_allocations import DemoAllocations
    from factorlab.integrations.binance_futures_trading.demo_portfolio_adapter import DemoPortfolioAdapter
    from test_binance_demo_network import NetworkFixture
    from factorlab.integrations.binance.client import Response
    root, credentials, campaign, fake = deployment
    original = NetworkFixture.__call__
    def perpetual(self, request, timeout):
        response = original(self, request, timeout)
        if lost_acknowledgement and request.path == '/fapi/v1/order' and request.method == 'POST':
            raise TimeoutError('offline fixture lost acknowledgement after exchange acceptance')
        if request.path != '/fapi/v1/exchangeInfo':
            return response
        payload = json.loads(response.body)
        payload['symbols'][0]['contractType'] = 'TRADIFI_PERPETUAL'
        return Response(response.status, response.headers, json.dumps(payload).encode())
    monkeypatch.setattr(NetworkFixture, '__call__', perpetual)
    entry = replace(intent('di-http-original', quantity='.04'), order_type=OrderType.LIMIT, limit_price=D(100))
    with closing(DemoRuntime(root)) as runtime:
        runtime.start_continuous(credentials.read_config(), opening_equity=D(1000),
                                 stop_epoch=runtime.stop_epoch())
        for restart in (False, True):
            with open_session(root, credentials, runtime, dict(GET=0, POST=0, DELETE=0), campaign=campaign) as (session, _):
                monkeypatch.setattr('factorlab.integrations.binance_futures_trading.network.datetime',
                    SimpleNamespace(now=lambda tz: session._now()))
                monkeypatch.setattr('factorlab.integrations.binance_futures_trading.demo_runtime.now', session._now)
                binding = dict(account_id=session.adapter.account_id, instance_id='one', allocation_id='funded')
                session.controller.store.record_metadata('demo:instance:one', dict(binding=binding,
                    symbols=['NVDAUSDT'], allocation=dict(budget_usdt=D(7))))
                owners = DemoAllocations(session.controller)
                owners.claim(entry, instance_id='one', allocation_id='funded')
                factory = DemoInstanceCapability(session, runtime,
                    exit_reservation=lambda *args: ('di-http-exit', D(4)), dispatch_gate=lambda binding: None)
                adapter = DemoPortfolioAdapter(session, capability=factory)
                session.gateway.recover(session.controller)
                original_context = (session.symbol, session.configuration, session.gateway.rules,
                                    session.client.transport, session._write_guard)
                if not restart:
                    if lost_acknowledgement:
                        from factorlab.integrations.binance_futures_trading.protocol import OutcomeUnknown
                        with pytest.raises(OutcomeUnknown):
                            adapter.submit_order(entry)
                    else:
                        adapter.submit_order(entry)
                else:
                    adapter.query_order(entry.client_order_id)
                    adapter.submit_order(entry)
                    adapter.cancel_order(entry.client_order_id)
                    assert fake.orders[entry.client_order_id]['status'] == 'CANCELED'
                assert len(fake.orders) == 1
                assert runtime.consumption(None)['posts'] == 1
                assert (session.symbol, session.configuration, session.gateway.rules,
                        session.client.transport, session._write_guard) == original_context
                dispatch = runtime.db.execute("SELECT receipt FROM dispatches WHERE campaign='instance:one' AND method='POST'").fetchone()
                recorded = json.loads(dispatch[0])
                assert recorded['http_status'] == (None if lost_acknowledgement else 200)
                assert recorded['outcome_unknown'] is lost_acknowledgement
                assert runtime.db.execute('SELECT count(*) FROM exit_budget_reservations').fetchone()[0] == 1


def test_http_partial_exit_reuses_original_hold_after_session_restart(deployment, monkeypatch):
    from contextlib import closing
    from test_binance_demo_network import NetworkFixture
    from factorlab.integrations.binance.client import Response
    from factorlab.integrations.binance_futures_trading.demo_cli import open_session
    from factorlab.integrations.binance_futures_trading.demo_allocations import DemoAllocations
    from factorlab.integrations.binance_futures_trading.demo_portfolio_adapter import DemoPortfolioAdapter
    from factorlab.trading import Side
    root, credentials, campaign, fake = deployment
    original = NetworkFixture.__call__
    def perpetual(self, request, timeout):
        response = original(self, request, timeout)
        if request.path != '/fapi/v1/exchangeInfo':
            return response
        payload = json.loads(response.body)
        payload['symbols'][0]['contractType'] = 'TRADIFI_PERPETUAL'
        return Response(response.status, response.headers, json.dumps(payload).encode())
    monkeypatch.setattr(NetworkFixture, '__call__', perpetual)
    entry = replace(intent('di-entry-http', quantity='.04'), order_type=OrderType.LIMIT, limit_price=D(100))
    with closing(DemoRuntime(root)) as runtime:
        runtime.start_continuous(credentials.read_config(), opening_equity=D(1000),
                                 stop_epoch=runtime.stop_epoch())
        for restart in (False, True):
            with open_session(root, credentials, runtime, dict(GET=0, POST=0, DELETE=0), campaign=campaign) as (session, _):
                monkeypatch.setattr('factorlab.integrations.binance_futures_trading.network.datetime',
                    SimpleNamespace(now=lambda tz: session._now()))
                monkeypatch.setattr('factorlab.integrations.binance_futures_trading.demo_runtime.now', session._now)
                binding = dict(account_id=session.adapter.account_id, instance_id='one', allocation_id='funded')
                session.controller.store.record_metadata('demo:instance:one', dict(binding=binding,
                    symbols=['NVDAUSDT'], allocation=dict(budget_usdt=D(7))))
                owners = DemoAllocations(session.controller)
                adapter = DemoPortfolioAdapter(session, capability=DemoInstanceCapability(
                    session, runtime, dispatch_gate=lambda binding: None))
                session.gateway.recover(session.controller)
                if not restart:
                    owners.claim(entry, instance_id='one', allocation_id='funded')
                    adapter.submit_order(entry)
                    fake.trade_update(entry.client_order_id, quantity='.04')
                    adapter.query_order(entry.client_order_id)
                    session.gateway.recover(session.controller)
                exit_order = replace(entry, client_order_id='di-final-http' if restart else 'di-partial-http',
                    quantity=D('.02'), side=Side.SELL, reduce_only=True)
                owners.claim(exit_order, instance_id='one', allocation_id='funded')
                adapter.submit_order(exit_order)
                fake.trade_update(exit_order.client_order_id, quantity='.02')
                adapter.query_order(exit_order.client_order_id)
                assert runtime.db.execute('SELECT count(*) FROM exit_budget_reservations').fetchone()[0] == (0 if restart else 1)
                assert runtime.consumption(None)['posts'] == (3 if restart else 2)
        assert fake.quantity == 0
        assert runtime.consumption(None)['submitted_notional'] == '8.00'


def test_dispatch_gate_blocks_before_reservation_or_budget_consumption(tmp_path):
    runtime = DemoRuntime(tmp_path)
    store = PaperStore(tmp_path / 'controller.sqlite3')
    account = 'DEMO:FactorLab-Demo:fixture'
    binding = dict(account_id=account, instance_id='one', allocation_id='funded', configuration={})
    entry = replace(intent('di-stopped', quantity='.05'), order_type=OrderType.LIMIT, limit_price=D(100))
    session = SimpleNamespace(controller=SimpleNamespace(store=store),
        adapter=SimpleNamespace(account_id=account), _now=now, _instance_receipt_sinks={})
    store.record_metadata('demo:instance:one', dict(binding=binding, symbols=['NVDAUSDT'],
        allocation=dict(budget_usdt=D(7))))
    def stopped(admitted_binding):
        raise SafetyBlocked('instance activation incomplete')
    factory = DemoInstanceCapability(session, runtime,
        exit_reservation=lambda *args: pytest.fail('stopped dispatch cannot reserve'), dispatch_gate=stopped)
    try:
        runtime.start_continuous(binding, opening_equity=D(5000), stop_epoch=runtime.stop_epoch())
        capability, guard = factory(entry, ('one', 'funded', 'NVDAUSDT'))
        with pytest.raises(SafetyBlocked, match='activation incomplete'):
            guard('POST', order_parameters(entry), capability.plan)
        assert runtime.consumption(None)['posts'] == 0
    finally:
        store.close()
        runtime.close()
