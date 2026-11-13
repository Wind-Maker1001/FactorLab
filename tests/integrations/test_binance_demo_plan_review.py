from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D

import pytest

from factorlab.errors import DataQualityError
from factorlab.integrations.binance_futures_trading.account import AccountFacts
from factorlab.integrations.binance_futures_trading.fake import NOW, synthetic_rules
from factorlab.integrations.binance_futures_trading.network import DemoExecutionPlan
from factorlab.integrations.binance_futures_trading.plan_review import minimum_opening_order, review_opening_plan
from factorlab.integrations.binance_futures_trading.quantitative import observe_quantitative
from factorlab.trading.contracts import OrderType


@pytest.fixture
def inputs():
    account_id = 'DEMO:FactorLab-Demo:' + 'a' * 64
    plan = DemoExecutionPlan(account_id=account_id, order_types=frozenset({'MARKET', 'LIMIT'}),
        max_quantity=D('1'), max_notional=D('105'), max_orders=2, max_open_orders=1, max_seconds=60,
        max_loss=D('1'), max_margin=D('105'), expires_at=NOW + timedelta(seconds=60),
        client_id_prefix='m02b-', cleanup='CANCEL_KNOWN_THEN_STOP', recovery='GET_RECONCILE_NO_RESUBMIT')
    facts = AccountFacts(account_id, NOW,
        assets=({'asset': 'USDT', 'walletBalance': '100', 'unrealizedProfit': '0',
                 'initialMargin': '0', 'availableBalance': '100'},),
        positions=(), risk=(), open_orders=(), trades=(), income=(), hedge_mode=False,
        symbol_config=({'symbol': 'NVDAUSDT', 'leverage': 1, 'marginType': 'CROSSED'},),
        source_complete=True, can_trade=True, quantitative=observe_quantitative({'indicators': {}}))
    rules = synthetic_rules()
    filters = {**rules.filters, 'PERCENT_PRICE': {'multiplierDown': '0.97', 'multiplierUp': '1.03'},
               'MAX_NUM_ORDERS': {'limit': 200}}
    return plan, facts, replace(rules, filters=filters)


def test_limit_uses_price_grid_and_higher_quote_for_plan_reservation(inputs):
    _, _, rules = inputs
    order = minimum_opening_order(rules.filters, OrderType.LIMIT, mark_price=D('100'),
                                  limit_price=D('99.99'), quote_price=D('101'))
    assert order.quantity == D('0.06')
    assert order.exchange_notional == D('5.9994')
    assert order.plan_notional == D('6.06')
    assert not order.hypothetical
    estimate = minimum_opening_order(rules.filters, OrderType.LIMIT, mark_price=D('100.005'))
    assert estimate.price == D('100.00') and estimate.hypothetical
    with pytest.raises(DataQualityError, match='LIMIT_PRICE_FILTER'):
        minimum_opening_order(rules.filters, OrderType.LIMIT, mark_price=D('100'), limit_price=D('99.999'))


def test_market_uses_common_quantity_grid_and_upper_band_for_plan_reservation(inputs):
    _, _, rules = inputs
    filters = dict(rules.filters)
    filters['LOT_SIZE'] = {**filters['LOT_SIZE'], 'stepSize': '0.02'}
    filters['MARKET_LOT_SIZE'] = {**filters['MARKET_LOT_SIZE'], 'stepSize': '0.03'}
    filters['MIN_NOTIONAL'] = {'notional': '6'}
    order = minimum_opening_order(filters, OrderType.MARKET, mark_price=D('100'), quote_price=D('99'))
    assert order.quantity == D('0.12')
    assert order.exchange_notional == D('12')
    assert order.plan_notional == D('12.36')
    filters['MIN_NOTIONAL'] = {'notional': 'NaN'}
    with pytest.raises(DataQualityError, match='UNKNOWN_MIN_NOTIONAL'):
        minimum_opening_order(filters, OrderType.MARKET, mark_price=D('100'))


def test_opening_review_reports_incompatible_caps_per_order_type(inputs):
    plan, facts, rules = inputs
    args = dict(facts=facts, rules=rules, mark_price=D('100'), quote_price=D('100'), limit_price=D('99'), now=NOW)
    assert review_opening_plan(plan, **args) == {'LIMIT': (), 'MARKET': ()}
    bounded = replace(plan, max_quantity=D('0.05'), max_notional=D('5.10'), max_margin=D('5.10'))
    assert review_opening_plan(bounded, **args) == {
        'LIMIT': ('PLAN_QUANTITY_LIMIT', 'PLAN_NOTIONAL_LIMIT', 'PLAN_MARGIN_LIMIT'),
        'MARKET': ('PLAN_NOTIONAL_LIMIT', 'PLAN_MARGIN_LIMIT'),
    }
    assert review_opening_plan(replace(plan, max_orders=1), **args) == {
        'LIMIT': (), 'MARKET': ('TOTAL_ORDER_LIMIT_FOR_LIMIT_THEN_MARKET',),
    }
    config = {**facts.symbol_config[0], 'leverage': 200}
    mismatched = replace(facts, symbol_config=(config,))
    assert review_opening_plan(plan, **dict(args, facts=mismatched)) == {
        'LIMIT': ('RISK_BRACKET_LIMIT',), 'MARKET': ('RISK_BRACKET_LIMIT',),
    }
    with pytest.raises(ValueError, match='open-order bound exceeds total-order bound'):
        replace(plan, max_open_orders=plan.max_orders + 1)


def test_opening_review_blocks_unmanaged_orders_and_unbound_or_expired_plans(inputs):
    plan, facts, rules = inputs
    facts = replace(facts, open_orders=({'symbol': 'NVDAUSDT'},),
                    positions=({'symbol': 'NVDAUSDT', 'positionAmt': '1'},))
    plan = replace(plan, account_id=plan.account_id[:-1] + 'b', expires_at=NOW)
    reasons = review_opening_plan(plan, facts=facts, rules=rules, mark_price=D('100'),
                                  quote_price=D('100'), limit_price=D('99'), now=NOW)
    assert reasons['LIMIT'] == reasons['MARKET'] == (
        'EXISTING_POSITIONS_REQUIRE_REVIEW', 'EXISTING_OPEN_ORDERS_REQUIRE_REVIEW',
        'PLAN_ACCOUNT_MISMATCH', 'PLAN_EXPIRED', 'OPEN_ORDER_LIMIT',
    )
