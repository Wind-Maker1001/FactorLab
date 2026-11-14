"""Offline campaign budgets and preview decisions."""
from datetime import timedelta
from decimal import Decimal as D

import pytest

from test_binance_demo_execution import setup
from test_binance_demo_strategy_session import bars
from factorlab.integrations.binance_futures_trading.fake import NOW
from factorlab.integrations.binance_futures_trading.protocol import SafetyBlocked
from factorlab.integrations.binance_futures_trading.strategy_runtime import DemoStrategyRuntime
from factorlab.trading.paper import Quote
from factorlab.trading.realtime import PersistenceConflict
from factorlab.trading.strategy import DualMAStrategy


def prepared(**configuration):
    adapter, gateway, controller, fake = setup()
    runner = DemoStrategyRuntime(controller, DualMAStrategy(), **configuration)
    runner.recover()
    bars(runner)
    at = NOW + timedelta(milliseconds=1)
    controller.clock = gateway.clock = lambda: at
    return runner, gateway, controller, fake, Quote('NVDAUSDT', D(150), at)


@pytest.mark.parametrize('preview', [False, True])
def test_budget_caps_and_preview(preview):
    runner, gateway, controller, fake, quote = prepared(
        max_notional=D(7), max_quantity=D('.04'), preview=preview)
    result = runner.quote(quote)
    outcome = gateway.state.runtime['last_order']
    assert outcome['target_position'] == D('.04')
    assert outcome['budget']['max_notional'] == D(7)
    assert outcome['budget']['max_quantity'] == D('.04')
    assert len([c for c in fake.calls if c[0] == 'POST']) == (0 if preview else 1)
    assert (result is None) == preview
    assert outcome['submission'] == ('PREVIEW' if preview else 'DECIDED')


def test_notional_floor_and_available_fee_budget():
    runner, gateway, controller, fake, quote = prepared(max_notional=D('3.7'), preview=True)
    runner.quote(quote)
    outcome = gateway.state.runtime['last_order']
    assert outcome['target_position'] * outcome['intent']['limit_price'] <= D('3.7')
    assert outcome['target_position'] % D(gateway.rules.filters['LOT_SIZE']['stepSize']) == 0


def test_reduction_remains_reduce_only():
    runner, gateway, controller, fake, quote = prepared(max_quantity=D('.04'), preview=True)
    fake.quantity = D('.03')
    fake.entry = D(100)
    assert runner.recover().reconciled
    pending = dict(gateway.state.runtime['pending'], direction='SELL', target_weight=D(0))
    controller._atomic(lambda: gateway.state.runtime.update(pending=pending))
    runner.quote(quote)
    intent = gateway.state.runtime['last_order']['intent']
    assert intent['side'].value == 'SELL'
    assert intent['reduce_only']
    assert intent['quantity'] == D('.03')


@pytest.mark.parametrize('budget', [dict(max_quantity=D('.04')), dict(max_notional=D(7))])
def test_overbudget_buy_never_liquidates(budget):
    runner, gateway, controller, fake, quote = prepared(**budget)
    fake.quantity = D('.1')
    fake.entry = D(100)
    assert runner.recover().reconciled
    pending = dict(gateway.state.runtime['pending'])
    assert runner.quote(quote) is None
    assert gateway.state.runtime['last_rejection'] == 'HOLDINGS_EXCEED_BUDGET'
    assert gateway.state.runtime['pending'] == pending
    assert not [c for c in fake.calls if c[0] == 'POST']


def test_sell_order_notional_is_capped():
    runner, gateway, controller, fake, quote = prepared(max_notional=D(7))
    fake.quantity = D('.1')
    fake.entry = D(100)
    assert runner.recover().reconciled
    pending = dict(gateway.state.runtime['pending'], direction='SELL', target_weight=D(0))
    controller._atomic(lambda: gateway.state.runtime.update(pending=pending))
    assert runner.quote(quote) is None
    assert gateway.state.runtime['last_order']['reason'] == 'TARGET_ORDER_BUDGET'
    assert not [c for c in fake.calls if c[0] == 'POST']


def test_preview_rejects_below_exchange_minimum():
    runner, gateway, controller, fake, quote = prepared(max_notional=D(3), preview=True)
    runner.quote(quote)
    outcome = gateway.state.runtime['last_order']
    assert outcome['kind'] == 'TARGET_REJECTED'
    assert outcome['reason'] == 'MIN_NOTIONAL'
    assert 'submission' not in outcome
    assert not [c for c in fake.calls if c[0] == 'POST']


def test_preview_configuration_is_separate():
    runner, gateway, controller, fake, quote = prepared(preview=True)
    with pytest.raises(PersistenceConflict):
        DemoStrategyRuntime(controller, DualMAStrategy())


def test_activation_preserves_history_and_requires_fresh_bar():
    runner, gateway, controller, fake, quote = prepared(preview=True)
    history = list(gateway.state.runtime['bars'])
    signal = dict(gateway.state.runtime['last_signal'])
    strategy_id = runner.strategy_id
    with pytest.raises(SafetyBlocked):
        runner.activate(client_id_prefix='next')
    runner.stop()
    runner.activate(client_id_prefix='next', max_notional=D(7), max_quantity=D('.04'), preview=False)
    assert runner.strategy_id == strategy_id
    assert gateway.state.runtime['bars'] == history
    assert gateway.state.runtime['last_signal'] == signal
    assert gateway.state.runtime['pending'] is None
    assert gateway.state.runtime['freshness_boundary'] == quote.observed_at
    assert runner.recover().reconciled
    assert runner.quote(quote) is None
    assert not [c for c in fake.calls if c[0] == 'POST']
    DemoStrategyRuntime(controller, DualMAStrategy(), client_id_prefix='next',
                        max_notional=D(7), max_quantity=D('.04'))
    runner.stop()
    runner.activate(client_id_prefix='next')
    assert runner.configuration['max_notional'] == D(7)
    assert runner.configuration['max_quantity'] == D('.04')
