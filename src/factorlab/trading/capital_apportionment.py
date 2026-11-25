"""Conserving USDT allocation at eight decimals or finer official precision."""
from decimal import Decimal


def exact_sum(values):
    """Sum ledger decimals without applying the caller's precision or rounding."""
    parts = [value.as_tuple() for value in values]
    exponent = min((part.exponent for part in parts), default=0)
    units = sum((-1 if part.sign else 1) * int(''.join(map(str, part.digits)))
                * 10 ** (part.exponent - exponent) for part in parts)
    return Decimal((int(units < 0), tuple(map(int, str(abs(units)))), exponent))


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
