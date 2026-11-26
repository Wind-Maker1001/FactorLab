from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D
from types import SimpleNamespace

import pytest

from test_binance_demo_execution import intent
from factorlab.integrations.binance_futures_trading.demo_runtime import DemoRuntime, now
from factorlab.integrations.binance_futures_trading.protocol import SafetyBlocked
from factorlab.integrations.binance_futures_trading.rules import order_parameters
from factorlab.trading import Side, OrderType


def test_first_campaign_equity_remains_the_continuous_loss_baseline(tmp_path):
    runtime = DemoRuntime(tmp_path)
    binding = dict(account_id='synthetic', configuration={})
    try:
        runtime.start(binding, seconds=900, orders=2, max_loss=D(1), opening_equity=D(5000),
            stop_epoch=runtime.stop_epoch(), started_at=now(), smoke_id='smoke-original')
        runtime.stop()
        authorization = runtime.start_continuous(binding, opening_equity=D(4900),
            stop_epoch=runtime.stop_epoch())
        assert authorization['opening_equity'] == '5000'
    finally:
        runtime.close()


def test_partial_instance_exits_retain_remaining_hold_across_restart(tmp_path):
    runtime = DemoRuntime(tmp_path)
    binding = dict(account_id='synthetic', configuration={}, instance_id='one')
    facts = SimpleNamespace(usdt=lambda: dict(walletBalance=D(5000), unrealizedProfit=D(0)))
    plan = SimpleNamespace(account_id='synthetic', symbol='NVDAUSDT', expires_at=now()+timedelta(minutes=5))
    entry = replace(intent('entry', quantity='.05'), order_type=OrderType.LIMIT, limit_price=D(100))
    try:
        runtime.start_continuous(binding, opening_equity=D(5000), stop_epoch=runtime.stop_epoch(),
            total_orders=3, total_notional=D(10))
        runtime.reserve_instance(binding, 'POST', order_parameters(entry), plan, intent=entry,
            facts=facts, exit_reservation=('future-exit', D(5)))
        partial = replace(entry, client_order_id='partial', quantity=D('.02'), side=Side.SELL, reduce_only=True)
        with pytest.raises(SafetyBlocked, match='owned entry'):
            runtime.reserve_instance(dict(binding, instance_id='foreign'), 'POST',
                order_parameters(partial), plan, intent=partial, facts=facts,
                exit_consumption=(('entry', D(2)),))
        runtime.reserve_instance(binding, 'POST', order_parameters(partial), plan,
            intent=partial, facts=facts, exit_consumption=(('entry', D(2)),))
        assert runtime.db.execute('SELECT notional FROM exit_budget_reservations').fetchall() == [('3',)]
        runtime.close()
        runtime = DemoRuntime(tmp_path)
        final = replace(partial, client_order_id='remaining', quantity=D('.03'))
        runtime.reserve_instance(binding, 'POST', order_parameters(final), plan,
            intent=final, facts=facts, exit_consumption=(('entry', D(3)),))
        assert runtime.db.execute('SELECT count(*) FROM exit_budget_reservations').fetchone()[0] == 0
        assert runtime.consumption(None) == dict(posts=3, submitted_notional='10.00')
    finally:
        runtime.close()


@pytest.mark.parametrize('price,allowed', [('90', True), ('110', False)])
def test_exit_price_changes_use_actual_notional_without_expanding_reservations(tmp_path, price, allowed):
    runtime = DemoRuntime(tmp_path)
    binding = dict(account_id='synthetic', configuration={}, instance_id='one')
    facts = SimpleNamespace(usdt=lambda: dict(walletBalance=D(5000), unrealizedProfit=D(0)))
    plan = SimpleNamespace(account_id='synthetic', symbol='NVDAUSDT', expires_at=now()+timedelta(minutes=5))
    entry = replace(intent('entry', quantity='.05'), order_type=OrderType.LIMIT, limit_price=D(100))
    try:
        runtime.start_continuous(binding, opening_equity=D(5000), stop_epoch=runtime.stop_epoch(),
            total_orders=3, total_notional=D(10))
        runtime.reserve_instance(binding, 'POST', order_parameters(entry), plan, intent=entry,
            facts=facts, exit_reservation=('future-exit', D(5)))
        partial = replace(entry, client_order_id='partial', quantity=D('.02'), limit_price=D(price),
                          side=Side.SELL, reduce_only=True)
        def submit():
            runtime.reserve_instance(binding, 'POST', order_parameters(partial), plan,
                intent=partial, facts=facts, exit_consumption=(('entry', D(2)),))
        if allowed:
            submit()
            assert D(runtime.consumption(None)['submitted_notional']) == D('6.80')
            assert runtime.db.execute('SELECT notional FROM exit_budget_reservations').fetchall() == [('3',)]
        else:
            with pytest.raises(SafetyBlocked, match='reserved for exit'):
                submit()
            assert runtime.consumption(None)['posts'] == 1
            assert runtime.db.execute('SELECT notional FROM exit_budget_reservations').fetchall() == [('5',)]
    finally:
        runtime.close()


def test_partial_exit_cannot_spend_the_remaining_exit_post_slot(tmp_path):
    runtime = DemoRuntime(tmp_path)
    binding = dict(account_id='synthetic', configuration={}, instance_id='one')
    facts = SimpleNamespace(usdt=lambda: dict(walletBalance=D(5000), unrealizedProfit=D(0)))
    plan = SimpleNamespace(account_id='synthetic', symbol='NVDAUSDT', expires_at=now()+timedelta(minutes=5))
    entry = replace(intent('entry', quantity='.05'), order_type=OrderType.LIMIT, limit_price=D(100))
    try:
        runtime.start_continuous(binding, opening_equity=D(5000), stop_epoch=runtime.stop_epoch(),
            total_orders=2, total_notional=D(10))
        runtime.reserve_instance(binding, 'POST', order_parameters(entry), plan, intent=entry,
            facts=facts, exit_reservation=('future-exit', D(5)))
        partial = replace(entry, client_order_id='partial', quantity=D('.02'), side=Side.SELL, reduce_only=True)
        with pytest.raises(SafetyBlocked, match='reserved for exit'):
            runtime.reserve_instance(binding, 'POST', order_parameters(partial), plan,
                intent=partial, facts=facts, exit_consumption=(('entry', D(2)),))
        assert runtime.db.execute('SELECT notional FROM exit_budget_reservations').fetchall() == [('5',)]
        assert runtime.consumption(None)['posts'] == 1
    finally:
        runtime.close()


def test_opening_reserves_exit_and_only_owned_exit_consumes_it(tmp_path):
    runtime = DemoRuntime(tmp_path)
    binding = dict(account_id='synthetic', configuration={}, instance_id='one')
    facts = SimpleNamespace(usdt=lambda: dict(walletBalance=D(5000), unrealizedProfit=D(0)))
    plan = SimpleNamespace(account_id='synthetic', symbol='NVDAUSDT', expires_at=now()+timedelta(minutes=5))
    entry = replace(intent('entry', quantity='.05'), order_type=OrderType.LIMIT, limit_price=D(100))
    amount = entry.quantity * entry.limit_price
    try:
        runtime.start_continuous(binding, opening_equity=D(5000), stop_epoch=runtime.stop_epoch(),
            total_orders=2, total_notional=amount*2)
        with pytest.raises(SafetyBlocked, match='funded exit'):
            runtime.reserve_instance(binding, 'POST', order_parameters(entry), plan, intent=entry, facts=facts)
        runtime.reserve_instance(binding, 'POST', order_parameters(entry), plan, intent=entry,
            facts=facts, exit_reservation=('exit', amount))
        assert runtime.consumption(None)['posts'] == 1
        runtime.close()
        runtime = DemoRuntime(tmp_path)
        assert runtime.db.execute('SELECT count(*) FROM exit_budget_reservations').fetchone()[0] == 1
        other = replace(entry, client_order_id='other')
        with pytest.raises(SafetyBlocked, match='global Demo trading budget'):
            runtime.reserve_instance(binding, 'POST', order_parameters(other), plan, intent=other,
                facts=facts, exit_reservation=('other-exit', amount))
        reduction = replace(entry, client_order_id='exit', side=Side.SELL, reduce_only=True)
        with pytest.raises(SafetyBlocked, match='owned entry'):
            runtime.reserve_instance(dict(binding, instance_id='foreign'), 'POST',
                order_parameters(reduction), plan, intent=reduction, facts=facts)
        runtime.reserve_instance(binding, 'POST', order_parameters(reduction), plan,
            intent=reduction, facts=facts)
        assert runtime.consumption(None) == dict(posts=2, submitted_notional=str(amount*2))
        assert runtime.db.execute('SELECT count(*) FROM exit_budget_reservations').fetchone()[0] == 0
    finally:
        runtime.close()
