"""Shared quote-participation execution assumptions for local Paper runs."""
from decimal import ROUND_FLOOR

from .contracts import OrderType, Side, ZERO


def execution_candidate(order, *, ask, bid, budget, slippage, step=ZERO, price_override=None):
    """Return legal incremental quantity and adverse execution price, or no fill.

    Quote size is a synthetic participation budget, not historical depth evidence.
    Admission and economic posting remain with the authoritative Paper transaction.
    """
    intent = order.intent
    reference = ask if intent.side == Side.BUY else bid
    price = (reference * (1 + slippage * (1 if intent.side == Side.BUY else -1))
             if price_override is None else price_override)
    if intent.order_type == OrderType.LIMIT and (
            intent.side == Side.BUY and price > intent.limit_price
            or intent.side == Side.SELL and price < intent.limit_price):
        return None
    quantity = min(budget, intent.quantity - order.cumulative_filled_quantity)
    if step:
        quantity = (quantity / step).to_integral_value(rounding=ROUND_FLOOR) * step
    return (quantity, price) if quantity > ZERO else None
