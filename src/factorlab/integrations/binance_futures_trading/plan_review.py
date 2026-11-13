"""Size and review a first Demo opening order from observed rules and account facts."""
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, DecimalException, ROUND_CEILING, ROUND_FLOOR

from factorlab.errors import DataQualityError
from factorlab.integrations.binance.market import _decimal
from factorlab.trading.contracts import Market, OrderIntent, OrderType, Side, utc

from .account import AccountFacts
from .network import DemoExecutionPlan
from .rules import RuleObservation, SUPPORTED_NETWORK_FILTERS, market_quantity_constraints


@dataclass(frozen=True)
class MinimumOpeningOrder:
    quantity: Decimal
    price: Decimal
    exchange_notional: Decimal
    plan_notional: Decimal
    hypothetical: bool


def _filter_number(filters, name, key):
    row = filters.get(name)
    value = row.get(key) if isinstance(row, dict) else None
    if type(value) not in (str, int, Decimal):
        raise DataQualityError('UNKNOWN_' + name)
    try:
        return _decimal(value, nonnegative=True)
    except DataQualityError:
        raise DataQualityError('UNKNOWN_' + name) from None


def _quantity_grid(filters, order_type):
    if order_type is OrderType.LIMIT:
        return tuple(_filter_number(filters, 'LOT_SIZE', field) for field in ('stepSize', 'minQty', 'maxQty'))
    lots = []
    for name in ('LOT_SIZE', 'MARKET_LOT_SIZE'):
        lot = {field: _filter_number(filters, name, field) for field in ('stepSize', 'minQty', 'maxQty')}
        lots.append(dict(filterType=name, **lot))
    return market_quantity_constraints({'filters': lots})


def _positive_price(value, reason):
    if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
        raise DataQualityError(reason)


def _limit_price(filters, price, mark, low, high):
    tick, minimum, maximum = (_filter_number(filters, 'PRICE_FILTER', field)
                              for field in ('tickSize', 'minPrice', 'maxPrice'))
    low = max(low, minimum)
    if maximum:
        high = min(high, maximum)
    if price is None:
        price = min(max(mark, low), high)
        if tick:
            price = (price / tick).to_integral_value(rounding=ROUND_FLOOR) * tick
            if price < low:
                price += tick
    _positive_price(price, 'UNKNOWN_LIMIT_PRICE')
    if not low <= price <= high or (tick and price % tick):
        raise DataQualityError('LIMIT_PRICE_FILTER')
    return price


def minimum_opening_order(filters, order_type: OrderType, *, mark_price: Decimal,
                          limit_price: Decimal | None = None, quote_price: Decimal | None = None):
    """Calculate the first opening quantity at the given prices, before any fills.

    A missing quote or an inferred LIMIT price yields an illustrative estimate.
    MARKET sizing meets the minimum at both the exchange mark and the quote used
    by the shared gateway. The plan reserves MARKET notional at the upper band.
    """
    _positive_price(mark_price, 'UNKNOWN_MARK_PRICE')
    down = _filter_number(filters, 'PERCENT_PRICE', 'multiplierDown')
    up = _filter_number(filters, 'PERCENT_PRICE', 'multiplierUp')
    if not 0 < down <= 1 <= up:
        raise DataQualityError('UNKNOWN_PERCENT_PRICE')
    low, high = mark_price * down, mark_price * up
    quote = mark_price if quote_price is None else quote_price
    _positive_price(quote, 'UNKNOWN_QUOTE_PRICE')
    if not low <= quote <= high:
        raise DataQualityError('QUOTE_OUTSIDE_MARK_BAND')

    if order_type is OrderType.LIMIT:
        price = _limit_price(filters, limit_price, mark_price, low, high)
        sizing_price, plan_price = price, max(price, quote)
    else:
        price, sizing_price, plan_price = mark_price, min(mark_price, quote), high
    step, minimum, maximum = _quantity_grid(filters, order_type)
    if not step:
        raise DataQualityError('UNKNOWN_QUANTITY_GRID')
    minimum_notional = _filter_number(filters, 'MIN_NOTIONAL', 'notional')
    steps = max(Decimal(1), minimum / step, minimum_notional / (sizing_price * step))
    quantity = steps.to_integral_value(rounding=ROUND_CEILING) * step
    if quantity > maximum:
        raise DataQualityError('QUANTITY_FILTER')
    hypothetical = quote_price is None or (order_type is OrderType.LIMIT and limit_price is None)
    return MinimumOpeningOrder(quantity, price, quantity * price, quantity * plan_price, hypothetical)


def _account_blockers(facts, symbol, now):
    blockers = []
    if not facts.source_complete or not timedelta(0) <= now - facts.observed_at <= timedelta(seconds=5):
        blockers.append('UNKNOWN_OR_STALE_ACCOUNT')
    if facts.can_trade is not True:
        blockers.append('CANNOT_TRADE' if facts.can_trade is False else 'UNKNOWN_CAN_TRADE')
    if facts.hedge_mode is not False:
        blockers.append('HEDGE_MODE_UNSUPPORTED' if facts.hedge_mode is True else 'UNKNOWN_POSITION_MODE')
    if facts.quantitative is None or facts.quantitative.error:
        blockers.append('UNKNOWN_QUANTITATIVE_STATUS')
    elif facts.quantitative.applicable_locks(symbol):
        blockers.append('QUANTITATIVE_LOCK')
    try:
        quantities = [_decimal(row['positionAmt']) for row in (*facts.positions, *facts.risk)]
    except (DataQualityError, KeyError, TypeError):
        blockers.append('UNKNOWN_POSITIONS')
    else:
        if any(quantities):
            blockers.append('EXISTING_POSITIONS_REQUIRE_REVIEW')
    if facts.open_orders:
        blockers.append('EXISTING_OPEN_ORDERS_REQUIRE_REVIEW')
    return blockers


def review_opening_plan(plan: DemoExecutionPlan, *, facts: AccountFacts, rules: RuleObservation,
                        mark_price: Decimal, quote_price: Decimal, limit_price: Decimal | None,
                        now: datetime) -> dict[str, tuple[str, ...]]:
    """Return blockers per allowed type for one opening with no orders or positions.

    The caller supplies fresh prices. Shared risk limits and fees still apply
    when the controller later evaluates an order.
    """
    utc(now)
    blockers = _account_blockers(facts, plan.symbol, now)
    if plan.account_id != facts.account_id:
        blockers.append('PLAN_ACCOUNT_MISMATCH')
    if plan.expires_at <= now:
        blockers.append('PLAN_EXPIRED')
    if set(rules.filters) - SUPPORTED_NETWORK_FILTERS:
        blockers.append('UNSUPPORTED_EXCHANGE_FILTER')
    if 'POSITION_RISK_CONTROL' in rules.filters:
        control = rules.filters['POSITION_RISK_CONTROL'].get('positionControlSide')
        if control != 'NONE':
            blockers.append('POSITION_RISK_RESTRICTION' if control else 'UNKNOWN_POSITION_RISK_CONTROL')
    order_cap = rules.filters.get('MAX_NUM_ORDERS', {}).get('limit')
    if type(order_cap) is not int or order_cap < 0:
        blockers.append('UNKNOWN_EXCHANGE_OPEN_ORDER_LIMIT')
    elif len(facts.open_orders) >= min(order_cap, plan.max_open_orders):
        blockers.append('OPEN_ORDER_LIMIT')
    configs = [row for row in facts.symbol_config if row.get('symbol') == plan.symbol]
    config = configs[0] if len(configs) == 1 else {}
    leverage = config.get('leverage')
    if type(leverage) is not int or leverage <= 0 or config.get('marginType') not in {'CROSSED', 'ISOLATED'}:
        blockers.append('UNKNOWN_LEVERAGE_OR_MARGIN_MODE')
    try:
        funds = facts.usdt()
    except (DataQualityError, AttributeError, TypeError):
        funds = {}
    if any(funds.get(key) is None for key in ('walletBalance', 'unrealizedProfit', 'initialMargin', 'availableBalance')):
        blockers.append('UNKNOWN_ACCOUNT_FUNDS')
    elif funds['initialMargin'] < 0 or funds['availableBalance'] < 0:
        blockers.append('INVALID_MARGIN_EVIDENCE')
    if blockers:
        return {kind: tuple(blockers) for kind in sorted(plan.order_types)}

    results = {}
    for kind in sorted(plan.order_types):
        order_type = OrderType(kind)
        if order_type is OrderType.MARKET and 'LIMIT' in plan.order_types and plan.max_orders == 1:
            results[kind] = ('TOTAL_ORDER_LIMIT_FOR_LIMIT_THEN_MARKET',)
            continue
        if order_type is OrderType.LIMIT and limit_price is None:
            results[kind] = ('LIMIT_PRICE_REQUIRED',)
            continue
        try:
            _positive_price(quote_price, 'UNKNOWN_QUOTE_PRICE')
            candidate = minimum_opening_order(rules.filters, order_type, mark_price=mark_price,
                                              limit_price=limit_price, quote_price=quote_price)
        except DataQualityError as error:
            results[kind] = (str(error),)
            continue
        except DecimalException:
            results[kind] = ('UNKNOWN_RULE_NUMERIC_RANGE',)
            continue
        intent = OrderIntent(Market.BINANCE_USDM, plan.symbol, Side.BUY, order_type,
                             candidate.quantity, limit_price if order_type is OrderType.LIMIT else None,
                             plan.client_id_prefix + 'review')
        blockers = []
        if candidate.quantity > plan.max_quantity:
            blockers.append('PLAN_QUANTITY_LIMIT')
        if candidate.plan_notional > plan.max_notional:
            blockers.append('PLAN_NOTIONAL_LIMIT')
        if funds['initialMargin'] + candidate.plan_notional > plan.max_margin:
            blockers.append('PLAN_MARGIN_LIMIT')
        shared_notional = candidate.quantity * max(quote_price, intent.limit_price or quote_price)
        if shared_notional > funds['availableBalance']:
            blockers.append('INSUFFICIENT_SHARED_COLLATERAL')
        try:
            decision = rules.validate(intent, mark_price=quote_price, leverage=leverage, max_leverage=leverage,
                                      available_margin=funds['availableBalance'], current_notional=Decimal(0), now=now)
        except (DataQualityError, DecimalException, KeyError, TypeError, ValueError):
            blockers.append('UNKNOWN_RULES')
        else:
            if not decision.approved:
                blockers.append(decision.reason)
        results[kind] = tuple(blockers)
    return results
