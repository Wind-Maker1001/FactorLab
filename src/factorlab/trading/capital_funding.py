"""Read-only ownership of confirmed funding against the execution journal."""
from dataclasses import dataclass
from decimal import Decimal
import json

from .capital_apportionment import apportion
from .contracts import Side, funding_identity_key
from .storage import decode

ZERO = Decimal(0)


@dataclass(frozen=True)
class FundingAttribution:
    amounts: tuple[tuple[str, Decimal], ...]
    unattributed_events: tuple[str, ...]
    complete: bool


def attribute_funding(store, account_id, events, coverage, observed_at):
    """Allocate official settlements against confirmed same-direction historical inventory.

    A fill and funding with the same timestamp need exchange sequence evidence;
    without it the payment remains unattributed rather than guessing their order.
    """
    owners = [owner for owner, in store.db.execute(
        'SELECT strategy FROM portfolio_allocations WHERE account=?', (account_id,))]
    initial, starts = {}, {}
    for owner in owners:
        initial[owner] = {symbol: Decimal(quantity) for symbol, quantity in store.db.execute(
            'SELECT symbol,initial_quantity FROM portfolio_claims WHERE account=? AND strategy=? '
            'UNION ALL SELECT symbol,initial_quantity FROM portfolio_claim_history WHERE account=? AND strategy=?',
            (account_id, owner, account_id, owner))}
        plans = [decode(json.loads(raw)) for raw, in store.db.execute(
            'SELECT payload FROM portfolio_plans WHERE account=? AND strategy=?', (account_id, owner))]
        starts[owner] = min((plan['created_at'] for plan in plans), default=None)
    order_owners = {identity: (owner, decode(json.loads(raw))['record'].snapshot.intent)
        for identity, owner, raw in store.db.execute(
            'SELECT o.id,p.strategy,o.payload FROM portfolio_orders o '
            'JOIN portfolio_plans p ON p.id=o.plan WHERE p.account=?', (account_id,))}
    confirmations = sorted((decode(json.loads(raw)) for raw, in store.db.execute(
        'SELECT payload FROM portfolio_confirmations WHERE account=?', (account_id,))), key=lambda event: event.event_time)
    changes, cumulative = [], {}
    for event in confirmations:
        if event.client_order_id not in order_owners:
            continue
        owner, intent = order_owners[event.client_order_id]
        previous = cumulative.get(event.client_order_id, ZERO)
        quantity = max(previous, event.cumulative_filled_quantity)
        cumulative[event.client_order_id] = quantity
        if quantity > previous:
            sign = 1 if intent.side == Side.BUY else -1
            changes.append((event.event_time, owner, intent.instrument_id, sign * (quantity - previous)))
    payments = {}
    for event in events:
        if event.account_id == account_id and event.event_time <= observed_at:
            if (event.event_id in payments
                    and funding_identity_key(payments[event.event_id]) != funding_identity_key(event)):
                raise ValueError('conflicting funding event identity')
            payments[event.event_id] = event
    amounts, fills, unattributed, index = dict.fromkeys(owners, ZERO), {}, [], 0
    for event in sorted(payments.values(), key=lambda event: (event.event_time, event.event_id)):
        while index < len(changes) and changes[index][0] < event.event_time:
            _, owner, symbol, quantity = changes[index]
            fills[owner, symbol] = fills.get((owner, symbol), ZERO) + quantity
            index += 1
        boundary = index
        ambiguous = False
        while boundary < len(changes) and changes[boundary][0] == event.event_time:
            if changes[boundary][2] == event.symbol:
                ambiguous = True
            boundary += 1
        quantities = {owner: (inventory.get(event.symbol, ZERO)
            if starts[owner] is not None and starts[owner] < event.event_time else ZERO)
            + fills.get((owner, event.symbol), ZERO) for owner, inventory in initial.items()}
        candidates = {owner: abs(quantity) for owner, quantity in quantities.items() if quantity}
        same_direction = (all(quantity >= ZERO for quantity in quantities.values())
                          or all(quantity <= ZERO for quantity in quantities.values()))
        if event.asset != 'USDT' or ambiguous or not candidates or not same_direction:
            unattributed.append(event.event_id)
        else:
            for owner, payment in apportion(event.amount, candidates):
                amounts[owner] += payment
    observed_ms = int(observed_at.timestamp() * 1000)
    earliest = min((int(start.timestamp() * 1000) for start in starts.values() if start is not None),
                   default=observed_ms)
    complete = bool(coverage and coverage['complete']
                    and coverage['covered_from_ms'] <= earliest
                    and coverage['covered_through_ms'] >= observed_ms and not unattributed)
    return FundingAttribution(tuple(sorted(amounts.items())), tuple(unattributed), complete)
