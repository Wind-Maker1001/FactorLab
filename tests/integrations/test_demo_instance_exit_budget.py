from dataclasses import replace
from decimal import Decimal as D
from types import SimpleNamespace

import pytest

from factorlab.integrations.binance_futures_trading.demo_allocations import DemoAllocations
from factorlab.integrations.binance_futures_trading.demo_instance_capability import DemoInstanceCapability
from factorlab.integrations.binance_futures_trading.demo_runtime import DemoRuntime, now
from factorlab.integrations.binance_futures_trading.rules import order_parameters
from factorlab.trading import OrderType, OrderStatus, Side
from factorlab.trading.storage import PaperStore
from test_binance_demo_execution import intent


@pytest.mark.parametrize('quantities', [('.05',), ('.01', '.04')])
def test_default_capability_resolves_partial_and_final_exit_from_original_entries(tmp_path, quantities):
    runtime = DemoRuntime(tmp_path)
    store = PaperStore(tmp_path / 'controller.sqlite3')
    gateway = SimpleNamespace(state=SimpleNamespace(orders={}))
    controller = SimpleNamespace(store=store, broker=gateway)
    account = 'DEMO:FactorLab-Demo:fixture'
    binding = dict(account_id=account, instance_id='one', allocation_id='funded', configuration={})
    facts = SimpleNamespace(usdt=lambda: dict(walletBalance=D(5000), unrealizedProfit=D(0)))
    session = SimpleNamespace(controller=controller, gateway=gateway,
        adapter=SimpleNamespace(account_id=account, facts=facts), _now=now, _instance_receipt_sinks={})
    store.record_metadata('demo:instance:one', dict(binding=binding, symbols=['NVDAUSDT'],
        allocation=dict(budget_usdt=D(7))))
    owners = DemoAllocations(controller)
    owner = ('one', 'funded', 'NVDAUSDT')
    entry = replace(intent('di-entry', quantity='.05'), order_type=OrderType.LIMIT, limit_price=D(100))
    def confirmed(order):
        gateway.state.orders[order.client_order_id] = SimpleNamespace(snapshot=SimpleNamespace(
            intent=order, cumulative_filled_quantity=order.quantity, status=OrderStatus.FILLED,
            reconcile_required=False))
    def dispatch(order):
        owners.claim(order, instance_id='one', allocation_id='funded')
        capability, guard = DemoInstanceCapability(session, runtime, dispatch_gate=lambda binding: None)(order, owner)
        guard('POST', order_parameters(order), capability.plan)
    try:
        runtime.start_continuous(binding, opening_equity=D(5000), stop_epoch=runtime.stop_epoch(),
            total_orders=len(quantities)+2, total_notional=D(10))
        for index, quantity in enumerate(quantities):
            opening = replace(entry, client_order_id='di-entry-' + str(index), quantity=D(quantity))
            dispatch(opening)
            confirmed(opening)
        original_exits = {row[0] for row in runtime.db.execute('SELECT exit_id FROM exit_budget_reservations')}
        partial = replace(entry, client_order_id='di-partial', quantity=D('.02'), side=Side.SELL, reduce_only=True)
        dispatch(partial)
        confirmed(partial)
        held = runtime.db.execute('SELECT exit_id,notional FROM exit_budget_reservations').fetchall()
        assert len(held) == 1
        assert held[0][0] in original_exits
        assert D(held[0][1]) == D(3)
        runtime.close()
        runtime = DemoRuntime(tmp_path)
        final = replace(partial, client_order_id='di-final', quantity=D('.03'))
        dispatch(final)
        assert runtime.db.execute('SELECT count(*) FROM exit_budget_reservations').fetchone()[0] == 0
        assert runtime.consumption(None) == dict(posts=len(quantities)+2, submitted_notional='10.00')
    finally:
        store.close()
        runtime.close()
