"""Instance allocations of official one-way contract initial margin."""
from dataclasses import dataclass
from decimal import Decimal

from .capital_apportionment import apportion

ZERO = Decimal(0)


@dataclass(frozen=True)
class MarginAttribution:
    amounts: tuple[tuple[str, Decimal], ...]
    unattributed_symbols: tuple[str, ...]
    complete: bool
    basis: str = 'confirmed_quantity_proportion_of_official_initial_margin'
    estimated: bool = True


def attribute_margin(store, account_id, margin):
    """Reconcile aggregate inventory before allocating official symbol margin.

    Allocations are estimates, not exchange-reported instance balances or claims
    on cross-margin collateral. Account availability remains an official fact.
    """
    owners = [owner for owner, in store.db.execute(
        'SELECT strategy FROM portfolio_allocations WHERE account=?', (account_id,))]
    holdings = {owner: store.holdings(account_id, owner) for owner in owners}
    amounts = dict.fromkeys(owners, ZERO)
    rows = margin.position_details()
    symbols = {symbol for inventory in holdings.values() for symbol, quantity in inventory.items() if quantity}
    symbols.update(row['symbol'] for row in rows if row['quantity'])
    unattributed = []
    for symbol in sorted(symbols):
        matches = [row for row in rows if row['symbol'] == symbol]
        quantities = {owner: inventory.get(symbol, ZERO) for owner, inventory in holdings.items()}
        weights = {owner: abs(quantity) for owner, quantity in quantities.items() if quantity}
        same_direction = (all(quantity >= ZERO for quantity in quantities.values())
                          or all(quantity <= ZERO for quantity in quantities.values()))
        if (len(matches) != 1 or not weights or not same_direction
                or matches[0]['position_side'] != 'BOTH'
                or matches[0]['quantity'] != sum(quantities.values(), ZERO)
                or matches[0]['initial_margin'] is None):
            unattributed.append(symbol)
            continue
        for owner, amount in apportion(matches[0]['initial_margin'], weights):
            amounts[owner] += amount
    return MarginAttribution(tuple(sorted(amounts.items())), tuple(unattributed),
        margin.account_id == account_id and margin.source_complete
        and margin.hedge_mode is False and not unattributed)
