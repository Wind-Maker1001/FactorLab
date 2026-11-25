from dataclasses import replace
from decimal import Decimal as D
from types import SimpleNamespace

import pytest

from test_binance_demo_execution import setup, intent
from factorlab.integrations.binance_futures_trading.demo_allocations import DemoAllocations
from factorlab.integrations.binance_futures_trading.protocol import SafetyBlocked
from factorlab.trading import Side, OrderSnapshot, OrderStatus


def test_legacy_adoption_uses_immutable_batch_not_order_prefix():
    from factorlab.trading.portfolio_runtime import PortfolioRuntime
    adapter, gateway, controller, _ = setup()
    owners = DemoAllocations(controller)
    portfolio = PortfolioRuntime(controller, allocation_id='old-allocation',
        owned_symbols=['NVDAUSDT'], recover=lambda: gateway.read_account())
    original = intent('original-without-prefix', quantity='2')
    foreign = intent('pf-foreign', quantity='3')
    try:
        portfolio.begin('old-task', weights={'NVDAUSDT': D('.5')}, equity=D(20))
        controller.store.record_metadata(portfolio._key('old-task') + ':batch:0', [original])
        for order in (original, foreign):
            gateway.state.orders[order.client_order_id] = SimpleNamespace(snapshot=OrderSnapshot(
                order, status=OrderStatus.FILLED, cumulative_filled_quantity=order.quantity))
        for _ in range(2):
            assert owners.adopt_portfolio_task('old-task', instance_id='legacy',
                allocation_id='old-allocation') == (original.client_order_id,)
        assert owners.inventory('legacy', 'NVDAUSDT') == D(2)
        assert owners.owner(foreign.client_order_id) is None
        with pytest.raises(SafetyBlocked, match='existing portfolio task'):
            owners.adopt_portfolio_task('missing', instance_id='legacy', allocation_id='old-allocation')
    finally:
        controller.store.close()
        adapter.store.close()


def test_history_adoption_is_allocation_bound_and_atomic():
    from factorlab.trading.portfolio_runtime import PortfolioRuntime
    adapter, gateway, controller, _ = setup()
    owners = DemoAllocations(controller)
    try:
        original = []
        for task, allocation in [('first', 'funded'), ('second', 'funded'), ('foreign', 'other')]:
            portfolio = PortfolioRuntime(controller, allocation_id=allocation,
                owned_symbols=['NVDAUSDT'], recover=lambda: gateway.read_account())
            portfolio.begin(task, weights={'NVDAUSDT': D('.5')}, equity=D(20))
            order = intent(task)
            controller.store.record_metadata(portfolio._key(task) + ':batch:0', [order])
            original.append(order)
        owners.claim(original[1], instance_id='other-owner', allocation_id='funded')
        with pytest.raises(SafetyBlocked, match='different strategy'):
            owners.adopt_portfolio_history(instance_id='legacy', allocation_id='funded')
        assert owners.owner('first') is None
        assert owners.owner('foreign') is None
        assert owners.adopt_portfolio_history(instance_id='other-owner', allocation_id='funded') == ('first', 'second')
        assert owners.adopt_portfolio_history(instance_id='other-owner', allocation_id='funded') == ('first', 'second')
        assert owners.owner('foreign') is None
    finally:
        controller.store.close()
        adapter.store.close()


@pytest.mark.parametrize('suffix', ['2', '01', 'invalid'])
def test_history_adoption_rejects_missing_or_ambiguous_batches_atomically(suffix):
    from factorlab.trading.portfolio_runtime import PortfolioRuntime
    adapter, gateway, controller, _ = setup()
    owners = DemoAllocations(controller)
    try:
        portfolio = PortfolioRuntime(controller, allocation_id='funded',
            owned_symbols=['NVDAUSDT'], recover=lambda: gateway.read_account())
        portfolio.begin('original', weights={'NVDAUSDT': D('.5')}, equity=D(20))
        key = portfolio._key('original')
        controller.store.record_metadata(key + ':batch:0', [intent('first')])
        controller.store.record_metadata(key + ':batch:' + suffix, [intent('later')])
        with pytest.raises(SafetyBlocked, match='batch'):
            owners.adopt_portfolio_history(instance_id='legacy', allocation_id='funded')
        assert owners.owner('first') is None
        assert owners.owner('later') is None
    finally:
        controller.store.close()
        adapter.store.close()


def test_shared_symbol_inventory_reduction_and_opposite_exposure():
    adapter, gateway, controller, _ = setup()
    owners = DemoAllocations(controller)
    try:
        a = intent('a', quantity='2')
        b = intent('b', quantity='3')
        for order, instance in ((a, 'A'), (b, 'B')):
            owners.claim(order, instance_id=instance, allocation_id=instance)
            gateway.state.orders[order.client_order_id] = SimpleNamespace(snapshot=OrderSnapshot(
                order, status=OrderStatus.FILLED, cumulative_filled_quantity=order.quantity))
        assert owners.inventory('A', 'NVDAUSDT') == D(2)
        assert owners.inventory('B', 'NVDAUSDT') == D(3)
        exit_a = replace(intent('exit-a', Side.SELL, quantity='3'), reduce_only=True)
        owners.claim(exit_a, instance_id='A', allocation_id='A')
        with pytest.raises(SafetyBlocked, match='strategy-owned inventory'):
            owners.admit(exit_a, instance_id='A', allocation_id='A')
        exit_a = replace(exit_a, client_order_id='exit-a-valid', quantity=D(2))
        owners.claim(exit_a, instance_id='A', allocation_id='A')
        owners.admit(exit_a, instance_id='A', allocation_id='A')
        short = intent('short-c', Side.SELL, quantity='1')
        owners.claim(short, instance_id='C', allocation_id='C')
        with pytest.raises(SafetyBlocked, match='opposite strategy exposure'):
            owners.admit(short, instance_id='C', allocation_id='C')
        with pytest.raises(SafetyBlocked, match='another strategy'):
            owners.require_cancel('a', instance_id='B', allocation_id='B')
        with pytest.raises(SafetyBlocked, match='different strategy'):
            owners.claim(a, instance_id='B', allocation_id='B')
    finally:
        controller.store.close()
        adapter.store.close()
