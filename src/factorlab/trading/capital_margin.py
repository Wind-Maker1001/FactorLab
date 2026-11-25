"""Instance allocations of official one-way contract initial margin."""
from dataclasses import dataclass
from decimal import Decimal

from .capital_apportionment import apportion, exact_sum

ZERO = Decimal(0)


@dataclass(frozen=True)
class MarginAllocation:
    symbol: str
    official_quantity: Decimal
    official_initial_margin: Decimal
    quantities: tuple[tuple[str, Decimal], ...]
    amounts: tuple[tuple[str, Decimal], ...]
    leverage: str | None
    margin_type: str | None


@dataclass(frozen=True)
class MarginAttribution:
    amounts: tuple[tuple[str, Decimal], ...]
    unattributed_symbols: tuple[str, ...]
    complete: bool
    basis: str = 'confirmed_quantity_proportion_of_official_initial_margin'
    estimated: bool = True
    allocations: tuple[MarginAllocation, ...] = ()


def attribute_margin(store, account_id, margin):
    """Reconcile aggregate inventory before allocating official symbol margin.

    Allocations are estimates, not exchange-reported instance balances or claims
    on cross-margin collateral. Account availability remains an official fact.
    """
    if margin.source_complete:
        margin.confirmed_position_amounts()
    owners = [owner for owner, in store.db.execute(
        'SELECT strategy FROM portfolio_allocations WHERE account=?', (account_id,))]
    holdings = {owner: store.holdings(account_id, owner) for owner in owners}
    amounts = dict.fromkeys(owners, ZERO)
    rows = []
    for detail in margin.position_details():
        row = dict(detail)
        position_margin = row['position_initial_margin']
        if (position_margin is None
                and row['open_order_initial_margin'] in (None, ZERO)
                and not any(order['symbol'] == row['symbol'] for order in margin.open_orders)):
            position_margin = row['initial_margin']
        row['initial_margin'] = position_margin
        rows.append(row)
    symbols = {symbol for inventory in holdings.values() for symbol, quantity in inventory.items() if quantity}
    symbols.update(row['symbol'] for row in rows
                   if row['quantity'] is None or row['quantity']
                   or row['initial_margin'] is None or row['initial_margin'])
    unattributed, allocations = [], []
    for symbol in sorted(symbols):
        matches = [row for row in rows if row['symbol'] == symbol]
        quantities = {owner: inventory.get(symbol, ZERO) for owner, inventory in holdings.items()}
        weights = {owner: quantity.copy_abs() for owner, quantity in quantities.items() if quantity}
        same_direction = (all(quantity >= ZERO for quantity in quantities.values())
                          or all(quantity <= ZERO for quantity in quantities.values()))
        if (len(matches) != 1 or not weights or not same_direction
                or matches[0]['position_side'] != 'BOTH'
                or matches[0]['quantity'] != exact_sum(quantities.values())
                or matches[0]['initial_margin'] is None):
            unattributed.append(symbol)
            continue
        row = matches[0]
        shares = apportion(row['initial_margin'], weights)
        allocations.append(MarginAllocation(symbol, row['quantity'], row['initial_margin'],
            tuple(sorted((owner, quantities[owner]) for owner in weights)), shares,
            row['leverage'], row['margin_type']))
        for owner, amount in shares:
            amounts[owner] = exact_sum((amounts[owner], amount))
    return MarginAttribution(tuple(sorted(amounts.items())), tuple(unattributed),
        margin.account_id == account_id and margin.source_complete
        and margin.hedge_mode is False and not unattributed, allocations=tuple(allocations))
