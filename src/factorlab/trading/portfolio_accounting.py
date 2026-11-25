"""Decimal, long-only USD accounting independent of planning and execution."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal

from .contracts import (
    AccountSnapshot, OrderEvent, OrderIntent, Side, ZERO,
    decimal_value, signed_decimal_value, utc,
)


def corporate_action_timeline(events, corporate_actions):
    """Place official effective-time actions before fills in the new share basis."""
    anchors, timed, identities = {}, [], set()
    event_ids = {event.event_id for event in events}
    for action in corporate_actions:
        if action['id'] in identities:
            raise ValueError('duplicate corporate action identity')
        identities.add(action['id'])
        if 'effective_at' in action:
            utc(action['effective_at'])
            timed.append(action)
        else:
            anchor = action['after_event_id']
            if anchor is not None and anchor not in event_ids:
                raise ValueError('corporate action lacks its confirmation boundary')
            anchors.setdefault(anchor, []).append(action)
    timeline = list(anchors.get(None, ()))
    attached = set()
    for event in events:
        timeline.append(event)
        if event.event_id not in attached:
            timeline.extend(anchors.get(event.event_id, ()))
            attached.add(event.event_id)
    if not timed:
        return timeline
    if anchors:
        def symbols(action):
            return {action['symbol']} | {item['symbol'] for item in action.get('allocations', ())}
        timed_symbols = set().union(*(symbols(action) for action in timed))
        if any(symbols(action) & timed_symbols for actions in anchors.values() for action in actions):
            raise ValueError('cannot mix effective-time and legacy anchored corporate actions')
        boundaries = {event.event_id: event.event_time for event in events}
        def order(item):
            if not isinstance(item, Mapping):
                return (1, item.event_time, 1)
            if 'effective_at' in item:
                return (1, item['effective_at'], 0)
            anchor = item['after_event_id']
            return (0, None, 0) if anchor is None else (1, boundaries[anchor], 2)
        return sorted([*timeline, *timed], key=order)
    return sorted([*events, *timed], key=lambda item:
        (item['effective_at'], 0) if isinstance(item, Mapping) else (item.event_time, 1))


@dataclass(frozen=True)
class OwnedPosition:
    """Explicit strategy-owned inventory; total cost basis includes buy fees."""

    quantity: Decimal
    cost_basis: Decimal

    def __post_init__(self):
        decimal_value(self.quantity, "owned quantity")
        decimal_value(self.cost_basis, "cost basis")
        if self.quantity == ZERO and self.cost_basis != ZERO:
            raise ValueError("zero inventory requires zero basis")


@dataclass(frozen=True)
class ResearchAdjustment:
    """Signed USD asset adjustment absent broker equity, not spendable cash.

    An outstanding recognized dividend may be positive; an unbooked research
    cost may be negative. Payment already in broker cash must not be added again.
    Unknown-tax gross receivables must be labeled as such, not called net assets.
    """

    adjustment_id: str
    kind: str
    amount: Decimal

    def __post_init__(self):
        if not self.adjustment_id or not self.kind:
            raise ValueError("adjustment identity and kind required")
        signed_decimal_value(self.amount, "adjustment amount")


@dataclass(frozen=True)
class ConfirmedFill:
    event_id: str
    client_order_id: str
    instrument_id: str
    side: Side
    quantity: Decimal
    price: Decimal
    fee: Decimal
    reference_price: Decimal
    slippage_cost: Decimal
    notional: Decimal


@dataclass(frozen=True)
class PositionAccounting:
    instrument_id: str
    actual_quantity: Decimal
    owned_quantity: Decimal
    external_quantity: Decimal
    owned_cost_basis: Decimal
    market_value: Decimal
    target_weight: Decimal
    actual_weight: Decimal
    economic_exposure_weight: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal


@dataclass(frozen=True)
class PortfolioReport:
    official_equity: Decimal
    official_cash: Decimal
    available_cash: Decimal
    cash_weight: Decimal
    target_cash_weight: Decimal
    adjustments: tuple[ResearchAdjustment, ...]
    research_adjustment: Decimal
    economic_nav: Decimal
    positions: tuple[PositionAccounting, ...]
    fills: tuple[ConfirmedFill, ...]
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    execution_fees: Decimal
    slippage_cost: Decimal
    traded_notional: Decimal
    turnover: Decimal
    tracking_error: Decimal
    initial_capital: Decimal | None
    corporate_cash_flow: Decimal = ZERO


def build_portfolio_report(
    *,
    starting_inventory: Mapping[str, OwnedPosition],
    intents: Sequence[OrderIntent],
    events: Sequence[OrderEvent],
    reference_prices: Mapping[str, Decimal],
    account: AccountSnapshot,
    prices: Mapping[str, Decimal],
    official_equity: Decimal,
    target_weights: Mapping[str, Decimal],
    adjustments: Sequence[ResearchAdjustment] = (),
    economic_multipliers: Mapping[str, Decimal] | None = None,
    initial_capital: Decimal | None = None,
    corporate_actions: Sequence[Mapping] = (),
    quote_currency: str = 'USD',
) -> PortfolioReport:
    """Report one accounting interval from explicit owned basis and broker facts.

    Events are chronological cumulative observations starting from zero for each
    supplied intent; fill_price prices only its new quantity, per OrderEvent.
    Exact event replays and unchanged cumulative observations do not add fills.
    Conflicting identities, regressing cumulative values, fee-only observations,
    oversells, and overfills are rejected. Sell fees reduce realized PnL; buy
    fees enter weighted-average basis. Slippage is positive when adverse and is
    measured against each intent's supplied reference, not charged twice to PnL.

    Actual quantities are exclusively account.positions, including external
    inventory. Only starting_inventory plus confirmed fills is strategy-owned;
    external basis and PnL are never inferred. Prices are raw USD marks and must
    cover every reported symbol, including target-only and fully sold symbols.
    Official equity is supplied by the broker, not reconstructed from holdings.
    All weights and gross traded-notional turnover use final official equity.
    Targets are neither capped nor normalized. Optional economic multipliers
    affect exposure weights only (e.g. TQQQ=3), not market value or economic NAV.
    Adjustments are uniquely identified outstanding research amounts absent
    official equity; dividend payment is an asset transfer, not new income.
    """
    decimal_value(official_equity, "official equity", positive=True)
    if initial_capital is not None:
        decimal_value(initial_capital, "initial capital", positive=True)
    orders = {}
    for intent in intents:
        if intent.client_order_id in orders:
            raise ValueError("duplicate intent identity")
        if intent.market != account.market:
            raise ValueError("intent and account markets differ")
        orders[intent.client_order_id] = intent
    inventory = dict(starting_inventory)
    realized: dict[str, Decimal] = {}
    cumulative: dict[str, tuple[Decimal, Decimal]] = {}
    notionals = {}
    seen: dict[str, OrderEvent] = {}
    fills = []
    timeline = corporate_action_timeline(events, corporate_actions)
    corporate_cash = ZERO
    for event in timeline:
        if isinstance(event, Mapping):
            symbol = event['symbol']
            ratio, basis_ratio = event['quantity_ratio'], event['basis_ratio']
            decimal_value(ratio, 'corporate share ratio', positive=True)
            decimal_value(basis_ratio, 'corporate basis ratio')
            signed_decimal_value(event['cash_flow'], 'corporate cash flow')
            owned = inventory.get(symbol, OwnedPosition(ZERO, ZERO))
            allocations = event.get('allocations', ())
            if allocations and basis_ratio + sum((item['basis_fraction'] for item in allocations), ZERO) != 1:
                raise ValueError('corporate cost allocation must preserve total basis')
            for item in allocations:
                if item['symbol'] == symbol:
                    raise ValueError('corporate allocation must name a different security')
                decimal_value(item['quantity'], 'allocated shares', positive=True)
                decimal_value(item['basis_fraction'], 'allocated cost fraction')
                child = inventory.get(item['symbol'], OwnedPosition(ZERO, ZERO))
                inventory[item['symbol']] = OwnedPosition(child.quantity + item['quantity'],
                    child.cost_basis + owned.cost_basis * item['basis_fraction'])
            inventory[symbol] = OwnedPosition(owned.quantity * ratio, owned.cost_basis * basis_ratio)
            corporate_cash += event['cash_flow']
            continue
        if event.event_id in seen:
            if seen[event.event_id] != event:
                raise ValueError("conflicting event identity")
            continue
        seen[event.event_id] = event
        intent = orders[event.client_order_id]
        previous_quantity, previous_fee = cumulative.get(intent.client_order_id, (ZERO, ZERO))
        quantity = event.cumulative_filled_quantity - previous_quantity
        fee = event.cumulative_fee - previous_fee
        if quantity < ZERO or fee < ZERO or event.cumulative_filled_quantity > intent.quantity:
            raise ValueError("regressing cumulative event or overfill")
        if quantity == ZERO:
            if fee != ZERO:
                raise ValueError("fee change without incremental fill")
            continue
        if event.fill_price is None:
            raise ValueError("incremental fill requires price")
        reference = reference_prices[intent.client_order_id]
        decimal_value(reference, "reference price", positive=True)
        price = event.fill_price
        previous_notional = notionals.get(intent.client_order_id, ZERO)
        amount = (quantity * price if event.cumulative_filled_notional is None else
                  event.cumulative_filled_notional - previous_notional)
        if amount <= ZERO:
            raise ValueError('positive incremental fill notional required')
        notionals[intent.client_order_id] = previous_notional + amount
        owned = inventory.get(intent.instrument_id, OwnedPosition(ZERO, ZERO))
        pnl = ZERO
        if intent.side == Side.BUY:
            updated = OwnedPosition(owned.quantity + quantity, owned.cost_basis + amount + fee)
        else:
            if quantity > owned.quantity:
                raise ValueError("sell exceeds explicitly owned inventory")
            removed_basis = owned.cost_basis if quantity == owned.quantity else owned.cost_basis * quantity / owned.quantity
            pnl = amount - fee - removed_basis
            updated = OwnedPosition(owned.quantity - quantity, owned.cost_basis - removed_basis)
        inventory[intent.instrument_id] = updated
        realized[intent.instrument_id] = realized.get(intent.instrument_id, ZERO) + pnl
        slippage = (amount - quantity * reference) * (Decimal(1) if intent.side == Side.BUY else Decimal(-1))
        fills.append(ConfirmedFill(event.event_id, intent.client_order_id, intent.instrument_id,
                                   intent.side, quantity, price, fee, reference, slippage, amount))
        cumulative[intent.client_order_id] = (event.cumulative_filled_quantity, event.cumulative_fee)

    actual = {}
    for position in account.positions:
        if position.quote_currency != quote_currency:
            raise ValueError('positions must use the report settlement currency')
        decimal_value(position.quantity, "actual quantity")
        if position.instrument_id in actual:
            raise ValueError("duplicate official position")
        actual[position.instrument_id] = position.quantity
    for weight in target_weights.values():
        decimal_value(weight, "target weight")
    multipliers = economic_multipliers or {}
    rows = []
    for symbol in sorted(actual.keys() | inventory.keys() | target_weights.keys()):
        owned = inventory.get(symbol, OwnedPosition(ZERO, ZERO))
        quantity = actual.get(symbol, ZERO)
        mark = prices[symbol] if quantity or owned.quantity else ZERO
        if quantity or owned.quantity:
            decimal_value(mark, "raw mark", positive=True)
        if owned.quantity > quantity:
            raise ValueError("owned inventory exceeds official holdings")
        multiplier = multipliers.get(symbol, Decimal(1))
        decimal_value(multiplier, "economic multiplier", positive=True)
        value = quantity * mark
        weight = value / official_equity
        rows.append(PositionAccounting(symbol, quantity, owned.quantity, quantity - owned.quantity,
                    owned.cost_basis, value, target_weights.get(symbol, ZERO), weight,
                    weight * multiplier, realized.get(symbol, ZERO), owned.quantity * mark - owned.cost_basis))
    settlement = [asset for asset in account.assets if asset.currency == quote_currency]
    if len(settlement) > 1:
        raise ValueError('duplicate settlement cash balance')
    cash = settlement[0].total if settlement else ZERO
    available = settlement[0].available if settlement else ZERO
    decimal_value(cash, "official cash")
    decimal_value(available, "available cash")
    if len({item.adjustment_id for item in adjustments}) != len(adjustments):
        raise ValueError("duplicate research adjustment")
    adjustment = sum((item.amount for item in adjustments), ZERO)
    notional = sum((fill.notional for fill in fills), ZERO)
    return PortfolioReport(
        official_equity, cash, available, cash / official_equity,
        Decimal(1) - sum(target_weights.values(), ZERO), tuple(adjustments), adjustment,
        official_equity + adjustment, tuple(rows), tuple(fills),
        sum((row.realized_pnl for row in rows), ZERO),
        sum((row.unrealized_pnl for row in rows), ZERO),
        sum((fill.fee for fill in fills), ZERO), sum((fill.slippage_cost for fill in fills), ZERO),
        notional, notional / official_equity,
        (sum(((row.actual_weight - row.target_weight) ** 2 for row in rows), ZERO)
         + (cash / official_equity - (Decimal(1) - sum(target_weights.values(), ZERO))) ** 2).sqrt(),
        initial_capital, corporate_cash,
    )
