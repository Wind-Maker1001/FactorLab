"""Bounded cash-flow coverage checkpoints, without economic postings."""
from .capital_apportionment import exact_sum
from .contracts import ZERO


def cash_flow_checkpoint(previous, records, *, start, end, retain_from):
    """Replace the reread overlap and retain only its source identities.

    Records are signed external settlement-currency movements. The caller must
    establish complete source coverage for [start, end] before committing this
    checkpoint to its existing protocol store.
    """
    unique = {}
    for identity, at, amount in records:
        if not start <= at <= end:
            raise ValueError('external cash-flow record outside covered interval')
        entry = (at, amount)
        if identity in unique and unique[identity] != entry:
            raise ValueError('external cash-flow identity changed within an observation')
        unique[identity] = entry
    prior_total = ZERO if previous is None else previous['total']
    replaced = () if previous is None else (
        amount.copy_negate() for at, amount in previous['tail'].values() if at >= start)
    total = exact_sum((prior_total, *replaced, *(amount for _, amount in unique.values())))
    return dict(origin=start if previous is None else previous['origin'], through=end, total=total,
                tail={identity: entry for identity, entry in unique.items() if entry[0] >= retain_from})
