"""Shared collateral planning; execution remains with the admitted controller."""
from dataclasses import dataclass
from decimal import Decimal as D

from .contracts import TERMINAL, decimal_value
from .target import target_quantity


@dataclass(frozen=True)
class TargetLeg:
    symbol: str
    weight: D
    target: D
    delta: D
    notional: D
    economic_notional: D


def plan_targets(*, account, equity, weights, prices, steps, owned_symbols,
                 fee_rate=D('0'), economic_multipliers=None, quote_currency='USDT'):
    """Plan one phase against one reconciled account, never anticipated proceeds.

    Missing instruments retain cash. Call again after confirmed reductions and
    account recovery before opening risk. Ownership must come from deployment
    allocation evidence, not from observing an exchange position.
    """
    decimal_value(equity, 'equity')
    decimal_value(fee_rate, 'fee_rate')
    for weight in weights.values():
        decimal_value(weight, 'weight')
    if sum(weights.values(), D(0)) > 1:
        raise ValueError('long-only account weights exceed one')
    if not account.reconciled:
        raise ValueError('account recovery required')
    if any(order.status not in TERMINAL for order in account.open_orders):
        raise ValueError('outstanding orders require reconciliation')
    positions = {p.instrument_id: p.quantity for p in account.positions}
    symbols = set(weights) | {s for s in owned_symbols if positions.get(s, D(0))}
    if symbols - set(owned_symbols):
        raise ValueError('explicit position ownership required')
    if any(positions.get(s, D(0)) < 0 for s in symbols):
        raise ValueError('long-only positions required')
    if any(p.quote_currency != quote_currency for p in account.positions
           if p.instrument_id in symbols):
        raise ValueError('position quote currency mismatch')
    available = next(a.available for a in account.assets if a.currency == quote_currency)
    available = max(D(0), available)
    desired = []
    for symbol in sorted(symbols):
        if symbol not in prices or symbol not in steps:
            continue
        price, step = prices[symbol], steps[symbol]
        decimal_value(price, 'price', positive=True)
        decimal_value(step, 'step', positive=True)
        current = positions.get(symbol, D(0))
        weight = weights.get(symbol, D(0))
        target = target_quantity(equity=equity, weight=weight, price=price,
            budget_price=price, fee_rate=fee_rate, current=current,
            available=equity, step=step)
        desired.append((symbol, weight, current, target, price, step))
    reductions = any(target < current for _, _, current, target, _, _ in desired)
    legs = []
    for symbol, weight, current, target, price, step in desired:
        if reductions and target >= current:
            continue
        if not reductions:
            target = target_quantity(equity=equity, weight=weight, price=price,
                budget_price=price, fee_rate=fee_rate, current=current,
                available=available, step=step)
            available -= max(D(0), target-current) * price * (1+fee_rate)
        multiplier = (economic_multipliers or {}).get(symbol, D(1))
        decimal_value(multiplier, 'economic multiplier', positive=True)
        legs.append(TargetLeg(symbol, weight, target, target-current,
                              target*price, target*price*multiplier))
    return tuple(legs)
