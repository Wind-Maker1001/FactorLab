"""Conserving allocation in the official amount's decimal settlement units."""
from decimal import Decimal


def apportion(amount, weights):
    """Allocate signed settlement units by largest remainder, ties by owner ID."""
    weights = sorted(weights.items())
    exponent = min(weight.as_tuple().exponent for _, weight in weights)
    integers = [(owner, int(''.join(map(str, weight.as_tuple().digits)))
                 * 10 ** (weight.as_tuple().exponent - exponent)) for owner, weight in weights]
    total = sum(weight for _, weight in integers)
    parts = amount.as_tuple()
    settlement_exponent = min(parts.exponent, -8)
    units = int(''.join(map(str, parts.digits))) * 10 ** (parts.exponent - settlement_exponent)
    shares, remainders = {}, []
    for owner, weight in integers:
        shares[owner], remainder = divmod(units * weight, total)
        remainders.append((-remainder, owner))
    for _, owner in sorted(remainders)[:units - sum(shares.values())]:
        shares[owner] += 1
    return tuple((owner, Decimal((parts.sign, tuple(map(int, str(shares[owner]))), settlement_exponent)))
                 for owner, _ in integers)
