"""Offline linear USD-M risk overlay; never an exchange liquidation engine."""
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from factorlab.experiments.grid_funding import cashflow
from factorlab.trading.contracts import Position, decimal_value, utc

D = Decimal
ZERO = D(0)
ASSUMPTION = 'SCENARIO_ASSUMPTION_ONLY'
BLOCKED = 'BLOCKED_RULE_EVIDENCE'


def signed(value, name):
    if not isinstance(value, D) or not value.is_finite():
        raise ValueError(f'{name} must be a finite Decimal')


@dataclass(frozen=True)
class Tier:
    floor: Decimal
    cap: Decimal
    initial_rate: Decimal
    maintenance_rate: Decimal
    deduction: Decimal

    def __post_init__(self):
        for name in ('floor', 'deduction'):
            decimal_value(getattr(self, name), name)
        for name in ('cap', 'initial_rate', 'maintenance_rate'):
            decimal_value(getattr(self, name), name, positive=True)
        if not (self.floor < self.cap and self.maintenance_rate <= self.initial_rate <= 1):
            raise ValueError('invalid tier bounds or margin rates')
        if self.floor * self.maintenance_rate < self.deduction:
            raise ValueError('negative maintenance at tier floor')


@dataclass(frozen=True)
class Rules:
    """Scenario rates apply to current absolute mark notional, not order admission."""
    version: str
    symbol: str
    contract_unit_base: Decimal
    tiers: tuple[Tier, ...]
    valid_from: datetime
    valid_until: datetime
    source: str
    fee_rate: Decimal
    exit_cost_rate: Decimal

    def __post_init__(self):
        utc(self.valid_from)
        utc(self.valid_until)
        decimal_value(self.contract_unit_base, 'contract_unit_base', positive=True)
        decimal_value(self.fee_rate, 'fee_rate')
        decimal_value(self.exit_cost_rate, 'exit_cost_rate')
        if self.version != 'mc-perp-risk/v1' or not self.symbol or not self.source:
            raise ValueError('supported rule version, symbol and source required')
        if self.valid_until <= self.valid_from or not self.tiers or self.tiers[0].floor != 0:
            raise ValueError('valid rule interval and tiers starting at zero required')
        for before, after in zip(self.tiers, self.tiers[1:]):
            if before.cap != after.floor:
                raise ValueError('tiers must be ordered, contiguous and nonoverlapping')
            if before.cap * before.maintenance_rate - before.deduction != after.floor * after.maintenance_rate - after.deduction:
                raise ValueError('maintenance must be continuous at tier boundary')

    def margin(self, notional):
        tier = next((t for t in self.tiers if t.floor <= notional < t.cap), None)
        if tier is None:
            raise ValueError('notional outside supplied tier evidence')
        return notional * tier.initial_rate, notional * tier.maintenance_rate - tier.deduction

    def position(self, contracts, entry, mark):
        """Convert signed contract count once; shared Position.quantity is base units."""
        signed(contracts, 'contracts')
        return Position(self.symbol, contracts * self.contract_unit_base, entry, mark, 'USDT')


@dataclass(frozen=True)
class Funding:
    event_id: str
    time: datetime
    symbol: str
    rate: Decimal
    mark: Decimal
    kind: str
    source: str

    def __post_init__(self):
        utc(self.time)
        signed(self.rate, 'funding rate')
        decimal_value(self.mark, 'settlement mark', positive=True)
        if self.kind not in ('ASSUMED_SETTLEMENT', 'OBSERVED_PUBLIC_SETTLEMENT') or not self.event_id or not self.source:
            raise ValueError('explicit settlement kind, identity and source required')


@dataclass(frozen=True)
class Frame:
    """Positions are base quantities strictly before any same-time fill."""
    time: datetime
    positions: tuple[Position, ...]
    original_equity: Decimal
    valuation_adjustment: Decimal = ZERO


def evaluate(*, frames, rules, funding=(), mode, budget, order_margin,
             portfolio_complete, equity_basis, isolated_collateral=None):
    """Return a read-only, post-settlement trajectory without invented liquidation fills.

    SYNTHETIC_WALLET: original_equity is wallet plus this frame's unrealized PnL.
    PAPER_NAV: original_equity already includes trade costs and reference valuation;
    valuation_adjustment is only the mark-minus-reference valuation difference.
    Funding must not already be included in either input equity basis.
    """
    frames, funding = tuple(frames), tuple(funding)
    decimal_value(budget, 'budget', positive=True)
    decimal_value(order_margin, 'order_margin')
    if mode not in ('CROSS', 'ISOLATED') or equity_basis not in ('SYNTHETIC_WALLET', 'PAPER_NAV'):
        raise ValueError('explicit supported margin mode and equity basis required')
    if portfolio_complete is not True or not frames:
        raise ValueError('complete portfolio and nonempty timeline required')
    if mode == 'ISOLATED':
        decimal_value(isolated_collateral, 'isolated_collateral', positive=True)
        if isolated_collateral > budget:
            raise ValueError('isolated collateral exceeds account budget')
    elif isolated_collateral is not None:
        raise ValueError('cross mode has no isolated collateral')
    if len({e.event_id for e in funding}) != len(funding):
        raise ValueError('duplicate settlement identity')
    times = []
    for frame in frames:
        utc(frame.time)
        signed(frame.original_equity, 'original_equity')
        signed(frame.valuation_adjustment, 'valuation_adjustment')
        if times and frame.time <= times[-1]:
            raise ValueError('strictly increasing observation times required')
        times.append(frame.time)
        symbols = [p.instrument_id for p in frame.positions]
        if len(symbols) != len(set(symbols)) or (mode == 'ISOLATED' and len(symbols) != 1):
            raise ValueError('one-way unique positions; isolated scope is one position')
        if mode == 'ISOLATED' and frame.positions:
            first = frames[0].positions
            if not first or (frame.positions[0].instrument_id, frame.positions[0].quantity, frame.positions[0].entry_price) != (first[0].instrument_id, first[0].quantity, first[0].entry_price):
                raise ValueError('isolated v1 requires fixed endowed exposure')
        for p in frame.positions:
            signed(p.quantity, 'base quantity')
            decimal_value(p.entry_price, 'entry', positive=True)
            decimal_value(p.mark_price, 'mark', positive=True)
            rule = rules[p.instrument_id]
            if p.quote_currency != 'USDT' or rule.symbol != p.instrument_id or not rule.valid_from <= frame.time < rule.valid_until:
                raise ValueError('position/rule currency, identity or effective interval mismatch')
    if any(e.time not in times or e.symbol not in rules for e in funding):
        raise ValueError('each settlement needs an exact observation and matching rule')
    rows, settlements = [], []
    cumulative = ZERO
    for frame in frames:
        positions = {p.instrument_id: p for p in frame.positions}
        flow = ZERO
        for event in sorted((e for e in funding if e.time == frame.time), key=lambda e: e.event_id):
            rule = rules[event.symbol]
            if not rule.valid_from <= event.time < rule.valid_until:
                raise ValueError('settlement outside rule interval')
            quantity = positions[event.symbol].quantity if event.symbol in positions else ZERO
            amount = cashflow(quantity, event.mark, event.rate)
            flow += amount
            settlements.append(dict(event_id=event.event_id, time=event.time, symbol=event.symbol,
                quantity=quantity, rate=event.rate, mark=event.mark, cashflow=amount,
                kind=event.kind, source=event.source))
        cumulative += flow
        pnl = sum((p.quantity * (p.mark_price-p.entry_price) for p in frame.positions), ZERO)
        notionals = {s: abs(p.quantity) * p.mark_price for s, p in positions.items()}
        margins = [rules[s].margin(n) for s, n in notionals.items()]
        initial = sum((m[0] for m in margins), ZERO)
        maintenance = sum((m[1] for m in margins), ZERO)
        equity = frame.original_equity + frame.valuation_adjustment + cumulative
        if equity_basis == 'SYNTHETIC_WALLET' and frame.original_equity != budget + pnl:
            raise ValueError('synthetic equity must equal budget plus unrealized PnL')
        if mode == 'ISOLATED':
            # v1 isolates a single endowed fixed position; external cash stays untouched.
            if equity_basis != 'SYNTHETIC_WALLET':
                raise ValueError('isolated Paper overlay requires unavailable compartment history')
            risk_equity = isolated_collateral + pnl + cumulative
        else:
            risk_equity = equity
        available = risk_equity - initial - order_margin
        events = []
        if available < 0:
            events.append('INSUFFICIENT_MARGIN')
        if notionals and sum(notionals.values(), ZERO) > 0 and risk_equity <= maintenance:
            events.append('SCENARIO_LIQUIDATION_THRESHOLD')
        if risk_equity < 0:
            events.append('NEGATIVE_RISK_EQUITY')
        if equity < 0:
            events.append('NEGATIVE_ACCOUNT_EQUITY')
        exit_cost = sum((n * (rules[s].fee_rate + rules[s].exit_cost_rate) for s, n in notionals.items()), ZERO)
        rows.append(dict(time=frame.time, original_equity=frame.original_equity,
            valuation_adjustment=frame.valuation_adjustment, unrealized_pnl=pnl,
            funding=flow, cumulative_funding=cumulative, equity=equity, risk_equity=risk_equity,
            external_balance=budget-isolated_collateral if mode == 'ISOLATED' else ZERO,
            notional=sum(notionals.values(), ZERO), initial_margin=initial,
            maintenance_margin=maintenance, order_margin=order_margin, available_margin=available,
            margin_utilization=(initial+order_margin)/risk_equity if risk_equity > 0 else None,
            maintenance_utilization=maintenance/risk_equity if risk_equity > 0 else None,
            equity_loss=budget-equity, hypothetical_exit_cost=exit_cost, events=events))
    return dict(schema='mc-perp-risk/v1', result_type='INDEPENDENT_RISK_OVERLAY',
        safety='NO LIVE MONEY', mode=mode, budget=budget, equity_basis=equity_basis,
        rule_status=ASSUMPTION, authoritative_liquidation=BLOCKED,
        continuation='FIXED_INPUT_EXPOSURE_NO_LIQUIDATION_EXECUTION',
        funding_status='INPUT_SETTLEMENTS_NOT_EXCHANGE_ACCOUNT_CONFIRMATION',
        rows=rows, settlements=settlements)
