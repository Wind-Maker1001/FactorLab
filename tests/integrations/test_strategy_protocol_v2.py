"""Versioned decisions retain exact portfolio intent across the JSON boundary."""
from dataclasses import replace
from decimal import Decimal as D
import json

import pytest

from factorlab.strategies.protocol import (
    Decision, DecisionV2, StrategySpec, StrategySpecV2, V2,
    decision_payload, spec_payload, read_decision, read_spec, validate_binding,
)
from factorlab.trading.contracts import Market, OrderIntent, OrderType, Side
from factorlab.trading.storage import dumps, decode


def spec(kind='target_weights', **changes):
    values = dict(strategy_id='portfolio', version='2', market='ALPACA_US_EQUITY',
        account_id='paper', symbols=['AAPL', 'MSFT'], decision_kind=kind,
        data=dict(data_types=['closed_bar'], history_limit=10, historical_observations=False),
        schedule=dict(kind='interval', interval_seconds=60),
        state=dict(version='2', migration='migrate', from_versions=['1']),
        capital=dict(currency='USD', requested=D('1000.01')),
        execution=dict(order_types=['LIMIT'], time_in_force=['DAY'], shorting=False,
                       fractional_quantities=False, reduce_only=False))
    values.update(changes)
    return StrategySpecV2(**values)


def roundtrip(value):
    return decode(json.loads(dumps(value)))


@pytest.mark.parametrize('kind,targets', [
    ('target_weight', {'AAPL': D('0.1234567890123456789012345678')}),
    ('target_weights', {'AAPL': D('0.35'), 'MSFT': D('0.40')}),
    ('target_quantities', {'AAPL': D('12'), 'MSFT': D('0')}),
    ('quantity_deltas', {'AAPL': D('-5'), 'MSFT': D('2')}),
])
def test_targets_roundtrip(kind, targets):
    declaration = read_spec(roundtrip(spec_payload(spec(kind))))
    decision = DecisionV2(kind, targets=targets, state={'count': 1}).validate(declaration)
    restored = read_decision(roundtrip(decision_payload(decision))).validate(declaration)
    assert restored.targets == targets
    assert restored.state == {'count': 1}
    assert all(isinstance(v, D) for v in restored.targets.values())


def test_multi_symbol_orders_and_cancellations():
    orders = tuple(OrderIntent(Market.ALPACA_US_EQUITY, symbol, Side.BUY, OrderType.LIMIT,
        D('2'), D('100.25'), 'instance-' + symbol, time_in_force='DAY') for symbol in ['AAPL', 'MSFT'])
    decision = DecisionV2('orders', orders=orders,
        cancellations=({'symbol': 'AAPL', 'client_order_id': 'old-aapl'},))
    restored = read_decision(roundtrip(decision_payload(decision))).validate(spec('orders'))
    assert tuple(restored.orders) == orders
    assert restored.cancellations == [{'symbol': 'AAPL', 'client_order_id': 'old-aapl'}]


@pytest.mark.parametrize('targets', [{'UNKNOWN': D(1)}, {'AAPL': D('NaN')}, {'AAPL': 0.5}, {'AAPL': D('-1')}])
def test_invalid_targets_rejected(targets):
    with pytest.raises(ValueError):
        DecisionV2('target_weights', targets=targets).validate(spec())


def test_modes_and_capabilities_are_not_interchangeable():
    with pytest.raises(ValueError):
        DecisionV2('quantity_deltas', targets={'AAPL': D('0.5')}).validate(spec('quantity_deltas'))
    with pytest.raises(ValueError):
        DecisionV2('target_quantities', targets={'AAPL': D(1)}).validate(spec('quantity_deltas'))
    with pytest.raises(ValueError):
        DecisionV2('orders', targets={'AAPL': D(1)}).validate(spec('orders'))
    with pytest.raises(ValueError):
        DecisionV2('target_weight', targets={'AAPL': D('.5'), 'MSFT': D('.5')}).validate(spec('target_weight'))


def test_binding_contains_allocation_and_cannot_change_account():
    binding = dict(market='ALPACA_US_EQUITY', account_id='paper', instance_id='one',
                   allocation_id='budget-one', strategy_id='portfolio')
    spec().require_binding(binding)
    with pytest.raises(ValueError):
        spec().require_binding(dict(binding, account_id='another'))
    del binding['allocation_id']
    with pytest.raises(ValueError):
        validate_binding(binding)


def test_v1_payload_and_single_symbol_validation_unchanged():
    old = StrategySpec('old', '1', 'target', ['AAPL'], [60], ['closed_bar'])
    payload = decision_payload(Decision(target_weight=D('.25'), state={'v': 1}))
    assert set(payload) == {'target_weight', 'orders', 'cancellations', 'state'}
    assert 'protocol' not in spec_payload(old)
    assert isinstance(read_spec(spec_payload(old)), StrategySpec)
    assert read_decision(roundtrip(payload)).validate(old, 'AAPL').target_weight == D('.25')
    order = OrderIntent(Market.ALPACA_US_EQUITY, 'MSFT', Side.BUY, OrderType.LIMIT,
                        D(1), D(100), 'wrong-symbol', time_in_force='DAY')
    with pytest.raises(ValueError):
        Decision(orders=(order,)).validate(replace(old, decision_kind='orders'), 'AAPL')
    with pytest.raises(ValueError):
        DecisionV2('target_weights').validate(old)
    assert spec_payload(spec())['protocol'] == V2
