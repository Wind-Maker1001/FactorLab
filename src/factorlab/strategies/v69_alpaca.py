"""ALP-E03 portfolio bridge. No HTTP, credentials, fees or synthetic signals.

Adjustment convention: adjusted_price = raw_price * factor;
adjusted_quantity = raw_quantity / factor. Factors must describe the same
corporate-action basis as the history and execution-session inventory.
"""
from decimal import Decimal as D
from .v69 import ASSETS, validate_weights


def to_adjusted_holdings(raw_holdings, factors):
    if set(raw_holdings) - set(ASSETS):
        raise ValueError('holdings outside original universe')
    _factors(factors)
    return {s: float(D(str(raw_holdings.get(s, 0))) / D(str(factors[s]))) for s in ASSETS}


def _factors(factors):
    if set(factors) != set(ASSETS):
        raise ValueError('complete factors required')
    if any(not D(str(x)).is_finite() or D(str(x)) <= 0 for x in factors.values()):
        raise ValueError('finite positive adjustment factors required')


def create_alpaca_plan(executor, *, decision, account_id, session, now,
                       raw_holdings, factors, quantity_steps, initial_inventory=None):
    """Persist exact capped share intentions in the existing E03 target journal.

    E03 subsequently uses official equity, raw execution quotes, quantity steps,
    cash checks, sell-before-buy, durable order IDs and recovery. It may round
    down or reject an intent but must not resize it from the raw-price weights.
    Call only for a newly prepared first-session decision. A repeat with the
    same inputs returns the same plan; current inventory must be the planning
    snapshot, not a post-fill snapshot. No live submission occurs here.
    """
    if decision['weights'] is None:
        return None
    _factors(factors)
    weights = {s: D(str(decision['weights'][s])) for s in ASSETS}
    validate_weights(weights)
    if set(raw_holdings) - set(ASSETS):
        raise ValueError('holdings outside original universe')
    targets = {s: D(str(raw_holdings.get(s, 0))) for s in ASSETS}
    for order in decision['orders']:
        symbol = order['symbol']
        quantity = D(str(order['adjusted_quantity'])) * D(str(factors[symbol]))
        if not quantity.is_finite() or quantity <= 0 or order['side'] not in {'buy','sell'}:
            raise ValueError('invalid research intent')
        step = D(str(quantity_steps[symbol]))
        if not step.is_finite() or step <= 0:
            raise ValueError('positive broker quantity step required')
        quantity = (quantity // step) * step
        targets[symbol] += quantity if order['side'] == 'buy' else -quantity
    if any(not q.is_finite() or q < 0 for q in targets.values()):
        raise ValueError('invalid long-only inventory')
    # E03 create owns a transaction. Join it to the target-seeding transaction
    # through a private shallow facade, never mutating the shared executor.
    from contextlib import nullcontext
    from copy import copy
    store = executor.store
    class JoinedStore:
        def __getattr__(self, name):
            return getattr(store, name)
        def transaction(self):
            return nullcontext()
    joined = copy(executor)
    joined.store = JoinedStore()
    with store.transaction():
        identity = joined.create(account_id=account_id, strategy_id='V69_CAUSAL',
            strategy_version='69.1', trading_session=str(session), weights=weights,
            owned_symbols=list(ASSETS), now=now, initial_inventory=initial_inventory,
            economic_multipliers={'TQQQ': D(3)})
        plan = store.plan(identity)
        frozen = plan.get('v69_intent_targets')
        if frozen is not None:
            if frozen != targets:
                raise ValueError('rebalance already has different V69 intentions')
            return identity
        if store.orders(identity):
            raise ValueError('cannot change an execution plan after orders exist')
        plan['targets'] = targets
        plan['v69_intent_targets'] = targets
        store.save_plan(plan)
    return identity
