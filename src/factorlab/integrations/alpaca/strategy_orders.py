"""Translate provider-adjusted strategy orders without recomputing their sizes."""
from decimal import Decimal


def adjusted_order_deltas(orders, *, adjustment_ratios):
    """Return signed real shares for native research-unit orders.

    Ratios must match the basis of the strategy's adjusted-share inventory and
    order quantities: adjusted_price = raw_price * ratio, hence real_quantity
    = adjusted_quantity * ratio. Prices and weights are deliberately not inputs.
    Orders use the native strategy boundary's symbol/side/adjusted_quantity fields.
    """
    deltas = {}
    for order in orders:
        symbol = order['symbol']
        if symbol in deltas:
            raise ValueError('one original strategy order per instrument required')
        quantity = Decimal(str(order['adjusted_quantity']))
        ratio = adjustment_ratios[symbol]
        if not isinstance(ratio, Decimal) or not ratio.is_finite() or ratio <= 0:
            raise ValueError('positive Decimal adjustment ratio required')
        if not quantity.is_finite() or quantity <= 0:
            raise ValueError('positive strategy order quantity required')
        if order['side'] not in ('sell', 'buy'):
            raise ValueError('unknown native strategy order direction')
        deltas[symbol] = quantity * ratio * (1 if order['side'] == 'buy' else -1)
    return deltas


def create_strategy_plan(executor, *, decision, adjustment_ratios, signal_inputs,
                         account_id, strategy_id, strategy_version, trading_session,
                         owned_symbols, now, initial_inventory=None,
                         economic_multipliers=None, rebalance_id=None):
    """Persist native strategy orders and its checkpoint in one execution plan.

    A suppressed weekly decision (weights=None) produces no new execution plan.
    Ratios and signal_inputs must describe the same adjusted history and holdings
    basis used by the strategy, not a later post-fill inventory snapshot.
    """
    if decision['weights'] is None:
        return None
    inputs = dict(signal_inputs, adjustment_ratios=dict(adjustment_ratios),
                  strategy_state=decision['state'], original_orders=decision['orders'])
    return executor.create(account_id=account_id, strategy_id=strategy_id,
        strategy_version=strategy_version, trading_session=trading_session,
        weights={symbol: Decimal(str(weight)) for symbol, weight in decision['weights'].items()},
        owned_symbols=owned_symbols, now=now, initial_inventory=initial_inventory,
        economic_multipliers=economic_multipliers, rebalance_id=rebalance_id,
        quantity_deltas=adjusted_order_deltas(decision['orders'],
                                            adjustment_ratios=adjustment_ratios),
        signal_inputs=inputs)
