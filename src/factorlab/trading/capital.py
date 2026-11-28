"""Static capital policy over the shared portfolio execution journal."""
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
import json

from .storage import decode

from .capital_margin import MarginAttribution
from .contracts import Market, decimal_value

ZERO = Decimal(0)


class CapitalPolicyMode(StrEnum):
    EXCLUSIVE = 'EXCLUSIVE'
    SHARED_SAME_DIRECTION = 'SHARED_SAME_DIRECTION'


@dataclass(frozen=True)
class SymbolOwnership:
    account_id: str
    symbol: str
    strategy_id: str
    instance_id: str
    allocation_id: str
    logical_strategy_id: str


@dataclass(frozen=True)
class CapitalSnapshot:
    account_id: str
    strategy_id: str
    allocation_id: str
    capital: Decimal
    cash: Decimal
    reserved: Decimal
    paused: bool


@dataclass(frozen=True)
class StrategyRiskSnapshot:
    capital: CapitalSnapshot
    occupied: Decimal | None
    cost_basis: Decimal | None
    realized_pnl: Decimal | None
    unrealized_pnl: Decimal | None
    fees: Decimal | None
    funding: Decimal | None
    nav: Decimal | None
    remaining_budget: Decimal | None
    positions: tuple | None
    complete: bool = True
    wallet: Decimal | None = None
    corporate_cash_flow: Decimal | None = None


@dataclass(frozen=True)
class AccountRiskSnapshot:
    account_id: str
    currency: str
    observed_at: datetime
    reconciled: bool
    available: Decimal | None
    budget: Decimal
    allocated: Decimal
    strategies: tuple[StrategyRiskSnapshot, ...]
    margin_attribution: MarginAttribution | None = None


class CapitalPolicy:
    """Policy metadata shares the execution transaction, never a second cash ledger."""

    def __init__(self, store):
        from .capital_management import SCHEMA
        self.store = store
        schema = '''
            CREATE TABLE IF NOT EXISTS capital_accounts (
                account TEXT PRIMARY KEY, currency TEXT NOT NULL,
                budget TEXT NOT NULL, fee_rate TEXT NOT NULL,
                mode TEXT NOT NULL DEFAULT 'EXCLUSIVE');
            CREATE TABLE IF NOT EXISTS capital_migrations (
                id TEXT PRIMARY KEY, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS capital_limits (
                account TEXT NOT NULL, strategy TEXT NOT NULL,
                max_order TEXT NOT NULL, max_symbol TEXT NOT NULL, budget TEXT NOT NULL,
                PRIMARY KEY(account,strategy));
            CREATE TABLE IF NOT EXISTS portfolio_claim_history (
                account TEXT NOT NULL, symbol TEXT NOT NULL, strategy TEXT NOT NULL,
                initial_quantity TEXT NOT NULL, entry_price TEXT NOT NULL,
                PRIMARY KEY(account,symbol,strategy));
            CREATE TABLE IF NOT EXISTS capital_symbols (
                account TEXT NOT NULL, symbol TEXT NOT NULL, strategy TEXT NOT NULL,
                PRIMARY KEY(account,symbol,strategy));
            CREATE TABLE IF NOT EXISTS capital_symbol_limits (
                account TEXT NOT NULL, symbol TEXT NOT NULL, max_exposure TEXT NOT NULL,
                PRIMARY KEY(account,symbol));
            CREATE TABLE IF NOT EXISTS capital_explicit_symbol_limits (
                account TEXT NOT NULL, symbol TEXT NOT NULL,
                PRIMARY KEY(account,symbol));
            CREATE TABLE IF NOT EXISTS capital_instances (
                account TEXT NOT NULL, strategy TEXT NOT NULL, instance TEXT NOT NULL,
                allocation TEXT NOT NULL, logical_strategy TEXT NOT NULL,
                paused INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(account,strategy), UNIQUE(account,instance),
                UNIQUE(account,allocation));
        '''
        with store.transaction():
            for statement in schema.split(';'):
                if statement.strip():
                    store.db.execute(statement)
            for statement in SCHEMA:
                store.db.execute(statement)
            if 'mode' not in {row[1] for row in store.db.execute('PRAGMA table_info(capital_accounts)')}:
                store.db.execute("ALTER TABLE capital_accounts ADD COLUMN mode TEXT NOT NULL DEFAULT 'EXCLUSIVE'")
            primary = [row[1] for row in sorted(store.db.execute('PRAGMA table_info(capital_symbols)'),
                                               key=lambda row: row[5]) if row[5]]
            if primary == ['account', 'symbol']:
                store.db.execute('ALTER TABLE capital_symbols RENAME TO capital_symbols_exclusive')
                store.db.execute('CREATE TABLE capital_symbols (account TEXT NOT NULL, symbol TEXT NOT NULL, '
                                 'strategy TEXT NOT NULL, PRIMARY KEY(account,symbol,strategy))')
                store.db.execute('INSERT INTO capital_symbols SELECT account,symbol,strategy '
                                 'FROM capital_symbols_exclusive')
                store.db.execute('DROP TABLE capital_symbols_exclusive')
            for account, symbol, owner in store.db.execute('SELECT account,symbol,strategy FROM capital_symbols').fetchall():
                self._apply_owner_symbol_limit(account, owner, symbol)

    def is_configured(self, account_id):
        return self.store.db.execute('SELECT 1 FROM capital_accounts WHERE account=?',
                                     (account_id,)).fetchone() is not None

    def mode(self, account_id):
        """Return the persisted mode; an unconfigured account raises KeyError."""
        row = self.store.db.execute('SELECT mode FROM capital_accounts WHERE account=?',
                                    (account_id,)).fetchone()
        if row is None:
            raise KeyError(account_id)
        return CapitalPolicyMode(row[0])

    def configure(self, *, account_id, currency, budget, fee_rate, mode=None, symbol_limits=None):
        """Configure immutable account funding and optional explicit aggregate symbol ceilings.

        Explicit symbols retain independent instance limits without deriving the
        aggregate ceiling from owners. Existing aggregate ceilings may only tighten;
        omitted symbols preserve their persisted explicit or owner-derived policy.
        """
        decimal_value(budget, 'account capital budget', positive=True)
        decimal_value(fee_rate, 'fee reservation rate')
        if not account_id or not currency:
            raise ValueError('account and currency required')
        with (nullcontext() if self.store.db.in_transaction else self.store.transaction()):
            previous = self.store.db.execute(
                'SELECT currency,budget,fee_rate FROM capital_accounts WHERE account=?', (account_id,)).fetchone()
            selected = (self.mode(account_id) if previous is not None else CapitalPolicyMode.EXCLUSIVE
                        ) if mode is None else CapitalPolicyMode(mode)
            if previous is not None and (previous[0] != currency or
                    tuple(map(Decimal, previous[1:])) != (budget, fee_rate)):
                raise ValueError('static account capital policy changed')
            total = self.store.allocated_capital(account_id)
            if total > budget:
                raise ValueError('existing allocations exceed account capital budget')
            if previous is None or selected != self.mode(account_id):
                self._validate_mode(account_id, selected, changing=previous is not None)
            if previous is None:
                self.store.db.execute('INSERT INTO capital_accounts (account,currency,budget,fee_rate,mode) '
                                      'VALUES (?,?,?,?,?)',
                                      (account_id, currency, str(budget), str(fee_rate), selected.value))
            elif selected != self.mode(account_id):
                self.store.db.execute('UPDATE capital_accounts SET mode=? WHERE account=?',
                                      (selected.value, account_id))
            for symbol, limit in ({} if symbol_limits is None else symbol_limits).items():
                decimal_value(limit, 'aggregate symbol exposure limit')
                if not symbol or limit > budget:
                    raise ValueError('aggregate symbol limit requires a symbol and cannot exceed account budget')
                existing = self.store.db.execute('SELECT max_exposure FROM capital_symbol_limits '
                    'WHERE account=? AND symbol=?', (account_id, symbol)).fetchone()
                if existing is not None and limit > Decimal(existing[0]):
                    raise ValueError('symbol limit cannot increase the existing risk boundary')
                self._tighten_symbol_limit(account_id, symbol, limit)
                self.store.db.execute('INSERT OR IGNORE INTO capital_explicit_symbol_limits VALUES (?,?)',
                                      (account_id, symbol))
            for symbol, owner in self._ownership(account_id, self._orders(account_id)):
                self._apply_owner_symbol_limit(account_id, owner, symbol)

    def _orders(self, account_id):
        for owner, payload in self.store.db.execute(
                'SELECT p.strategy,o.payload FROM portfolio_orders o JOIN portfolio_plans p ON p.id=o.plan '
                'WHERE p.account=?', (account_id,)):
            yield owner, decode(json.loads(payload))

    def _ownership(self, account_id, orders):
        from .contracts import TERMINAL
        claims = {(symbol, owner) for symbol, owner, quantity in self.store.db.execute(
            'SELECT symbol,strategy,quantity FROM portfolio_claims WHERE account=?', (account_id,))
                  if Decimal(quantity)}
        claims.update(self.store.db.execute('SELECT symbol,strategy FROM capital_symbols WHERE account=?',
                                           (account_id,)))
        claims.update((order['record'].snapshot.intent.instrument_id, owner) for owner, order in orders
                      if order['record'].snapshot.status not in TERMINAL or order['uncertain'])
        return claims

    def _validate_mode(self, account_id, mode, *, changing):
        from .contracts import TERMINAL
        orders = tuple(self._orders(account_id))
        claims = self._ownership(account_id, orders)
        owners = {}
        for symbol, owner in claims:
            owners.setdefault(symbol, set()).add(owner)
            if self.store.allocation(account_id, owner) is None:
                raise ValueError('existing ownership requires a matching capital allocation')
        if mode == CapitalPolicyMode.EXCLUSIVE:
            if any(len(group) > 1 for group in owners.values()):
                raise ValueError('exclusive capital policy cannot adopt shared symbols')
            if changing and any(order['record'].snapshot.status not in TERMINAL or order['uncertain']
                                for _, order in orders):
                raise ValueError('unfinished orders prevent exclusive downgrade')
        else:
            for symbol in owners:
                self._same_direction(account_id, symbol)
            if changing:
                for _, owner in claims:
                    self._binding(account_id, owner)

    def _binding(self, account_id, strategy_id):
        row = self.store.db.execute(
            'SELECT instance,allocation,logical_strategy FROM capital_instances WHERE account=? AND strategy=?',
            (account_id, strategy_id)).fetchone()
        if row is None:
            raise ValueError('explicit strategy capital binding required')
        allocation = self.store.allocation(account_id, strategy_id)
        if allocation is None or allocation['allocation_id'] != row[1]:
            raise ValueError('existing matching capital allocation required')
        return row

    def _same_direction(self, account_id, symbol):
        from .contracts import Side, TERMINAL
        holdings = {owner: Decimal(quantity) for owner, quantity in self.store.db.execute(
            'SELECT strategy,quantity FROM portfolio_claims WHERE account=? AND symbol=?', (account_id, symbol))}
        signs = {1 if quantity > ZERO else -1 for quantity in holdings.values() if quantity}
        for owner, order in self._orders(account_id):
            snapshot = order['record'].snapshot
            intent = snapshot.intent
            if intent.instrument_id != symbol or (snapshot.status in TERMINAL and not order['uncertain']):
                continue
            quantity = intent.quantity - snapshot.cumulative_filled_quantity
            if not quantity:
                continue
            direction = 1 if intent.side == Side.BUY else -1
            held = holdings.get(owner, ZERO)
            if held * direction >= ZERO or quantity > abs(held):
                signs.add(direction)
        if len(signs) > 1:
            raise ValueError(f'shared symbol direction conflict: {symbol}')

    def bind(self, *, account_id, strategy_id, instance_id, allocation_id, logical_strategy=None):
        context = nullcontext() if self.store.db.in_transaction else self.store.transaction()
        with context:
            allocation = self.store.allocation(account_id, strategy_id)
            if allocation is None or allocation['allocation_id'] != allocation_id:
                raise ValueError('existing matching capital allocation required')
            policy = self.store.db.execute('SELECT budget FROM capital_accounts WHERE account=?',
                                           (account_id,)).fetchone()
            if policy is None:
                raise ValueError('account capital policy required')
            logical_strategy = strategy_id if logical_strategy is None else logical_strategy
            value = (instance_id, allocation_id, logical_strategy)
            previous = self.store.db.execute(
                'SELECT instance,allocation,logical_strategy FROM capital_instances WHERE account=? AND strategy=?',
                (account_id, strategy_id)).fetchone()
            if previous is not None:
                if previous != value:
                    raise ValueError('stable instance allocation binding changed')
                return
            if not all((strategy_id, instance_id, allocation_id, logical_strategy)):
                raise ValueError('complete instance identity required')
            if self.store.db.execute('SELECT 1 FROM capital_instances WHERE account=? '
                    'AND (instance=? OR allocation=?)', (account_id, instance_id, allocation_id)).fetchone():
                raise ValueError('instance or allocation identity already bound')
            self.store.db.execute('INSERT INTO capital_instances VALUES (?,?,?,?,?,0)',
                                  (account_id, strategy_id, instance_id, allocation_id, logical_strategy))

    def limits(self, account_id, strategy_id, *, original=False):
        from .control import RiskLimits
        row = self.store.db.execute(
            'SELECT max_order,max_symbol,budget FROM capital_limits WHERE account=? AND strategy=?',
            (account_id, strategy_id)).fetchone()
        allocation = self.store.allocation_basis if original else self.store.allocation
        cap = allocation(account_id, strategy_id)['capital']
        order, symbol, budget = (cap, cap, cap) if row is None else (min(cap, Decimal(value)) for value in row)
        return RiskLimits(max_order_notional=order, max_instrument_exposure=symbol, account_budget=budget)

    def set_limits(self, account_id, strategy_id, limits):
        """Persist an immutable tighter instance projection of existing RiskLimits."""
        with self.store.transaction():
            capital = self.store.allocation_basis(account_id, strategy_id)['capital']
            value = (limits.max_order_notional, limits.max_instrument_exposure, limits.account_budget)
            if any(amount > capital for amount in value):
                raise ValueError('instance risk limits exceed allocated capital')
            previous = self.store.db.execute(
                'SELECT max_order,max_symbol,budget FROM capital_limits WHERE account=? AND strategy=?',
                (account_id, strategy_id)).fetchone()
            if previous is not None:
                if tuple(map(Decimal, previous)) != value:
                    raise ValueError('static instance risk limits changed')
                return
            self.store.db.execute('INSERT INTO capital_limits VALUES (?,?,?,?,?)',
                                  (account_id, strategy_id, *map(str, value)))
            for symbol, in self.store.db.execute('SELECT symbol FROM capital_symbols WHERE account=? AND strategy=?',
                                                 (account_id, strategy_id)).fetchall():
                self._apply_owner_symbol_limit(account_id, strategy_id, symbol)

    def validate_orders(self, account, strategy_id, *, include_id=None, risk_now=None,
                        risk_ids=None, official_equity=None, apply_portfolio=True):
        """Check instance, gross symbol and account projections in the outbox transaction."""
        from .control import RiskRejected, exposure_limit_reason, inventory_effect
        from .contracts import RiskDecision, TERMINAL
        account_id = account.account_id
        policy = self.store.db.execute('SELECT budget FROM capital_accounts WHERE account=?',
                                       (account_id,)).fetchone()
        if policy is None:
            return
        limits = self.limits(account_id, strategy_id)
        config = self.store.portfolio_risk.config(account_id) if risk_now is not None else None
        positions = (account.official_positions if config is not None and account.official_positions is not None
                     else account.positions)
        owned = self.store.holdings(account_id, strategy_id)
        marks = {p.instrument_id: p.mark_price for p in positions}
        exposures = {}
        for symbol, quantity in owned.items():
            if quantity:
                if symbol in marks:
                    exposures[symbol] = abs(quantity) * marks[symbol]
                elif config is None:
                    raise ValueError('owned inventory requires an official mark')
        symbols = {}
        for position in positions:
            symbol = position.instrument_id
            symbols[symbol] = symbols.get(symbol, ZERO) + abs(position.quantity) * position.mark_price
        account_exposure = sum(symbols.values(), ZERO)
        pending, local_ids, reductions = [], set(), {}
        holdings = {strategy_id: owned}
        orders = (tuple(self.store.active_orders(account_id)) if config is not None
                  else self._orders(account_id))
        for owner, order in orders:
            snapshot = order['record'].snapshot
            intent = snapshot.intent
            symbol = intent.instrument_id
            local_ids.add(intent.client_order_id)
            if snapshot.status in TERMINAL and not order['uncertain']:
                continue
            quantity = intent.quantity - snapshot.cumulative_filled_quantity
            if owner not in holdings:
                holdings[owner] = self.store.holdings(account_id, owner)
            held = holdings[owner].get(symbol, ZERO)
            effect = inventory_effect(intent.side, quantity, held)
            if effect == 'REVERSE':
                raise ValueError('orders exceed instance owned inventory')
            reducing = effect in ('REDUCE', 'CLOSE')
            if reducing:
                key = (owner, symbol)
                reductions[key] = reductions.get(key, ZERO) + quantity
                if reductions[key] > abs(held):
                    raise ValueError('orders exceed instance owned inventory')
            amount = ZERO if reducing else quantity * order['reservation_price']
            account_exposure += amount
            symbols[symbol] = symbols.get(symbol, ZERO) + amount
            if owner == strategy_id:
                exposures[symbol] = exposures.get(symbol, ZERO) + amount
                selected = intent.client_order_id == include_id or (
                    not order['attempted'] and (include_id is None or config is None))
                if selected:
                    pending.append((symbol, intent.quantity * order['reservation_price'], amount > ZERO))
        foreign = False
        for order in account.open_orders:
            if order.intent.client_order_id not in local_ids and order.status not in TERMINAL:
                foreign = True
                if order.intent.limit_price is None:
                    if config is None or any(increasing for _, _, increasing in pending):
                        raise ValueError('unpriced foreign order risk')
                    continue
                amount = ((order.intent.quantity - order.cumulative_filled_quantity)
                          * order.intent.limit_price)
                symbol = order.intent.instrument_id
                symbols[symbol] = symbols.get(symbol, ZERO) + amount
                account_exposure += amount
        shared = self.mode(account_id) == CapitalPolicyMode.SHARED_SAME_DIRECTION
        for symbol, amount, increasing in pending:
            if shared:
                self._same_direction(account_id, symbol)
            if not increasing:
                continue
            reason = exposure_limit_reason(limits, order_notional=amount,
                instrument_exposure=exposures[symbol], account_exposure=sum(exposures.values(), ZERO))
            if reason == 'APPROVED':
                if shared and foreign:
                    raise ValueError('unknown external orders prevent risk increase')
                if symbols[symbol] > self.symbol_limit(account_id, symbol):
                    reason = 'SYMBOL_EXPOSURE_LIMIT'
                elif account_exposure > Decimal(policy[0]):
                    reason = 'ACCOUNT_BUDGET_LIMIT'
            if reason != 'APPROVED':
                raise RiskRejected(RiskDecision(False, reason))
        if config is not None and apply_portfolio and any(increasing for _, _, increasing in pending):
            from .portfolio_risk import PortfolioRiskRejected
            currency = self.store.db.execute('SELECT currency FROM capital_accounts WHERE account=?',
                                             (account_id,)).fetchone()[0]
            snapshot, openings = self.store.portfolio_risk.observe(
                account, currency, risk_now, config, orders=orders, equity=official_equity)
            selected = {include_id} if include_id is not None else risk_ids
            for client_id in sorted(selected):
                opening = openings.get(client_id)
                if opening is not None:
                    reason = self.store.portfolio_risk.reason(config[0], snapshot, opening)
                    if reason:
                        raise PortfolioRiskRejected(reason)

    def qualify(self, spec, binding, allocation=None):
        """Atomically reserve exclusive symbols for an explicitly funded instance."""
        account_id, owner = binding['account_id'], binding['instance_id']
        if (spec.account_id != account_id or spec.market != binding['market']
                or spec.strategy_id != binding['strategy_id']):
            raise ValueError('strategy identity and account binding differ')
        with self.store.transaction():
            self.bind(account_id=account_id, strategy_id=owner,
                      instance_id=binding['instance_id'], allocation_id=binding['allocation_id'],
                      logical_strategy=binding['strategy_id'])
            configured = self.store.allocation_basis(account_id, owner)
            currency = self.store.db.execute(
                'SELECT currency FROM capital_accounts WHERE account=?', (account_id,)).fetchone()[0]
            if spec.capital['currency'] != currency or spec.capital['requested'] > configured['capital']:
                raise ValueError('strategy capital demand exceeds static allocation')
            if self.store.capital_balance(account_id, owner)[3]:
                raise ValueError('retired capital allocation')
            for symbol in spec.symbols:
                self.claim(account_id, owner, symbol)
            return configured

    def ownership(self, account_id, symbol=None):
        """Return registered owners with their durable allocation and logical identities."""
        query = ('SELECT s.symbol,s.strategy,i.instance,i.allocation,i.logical_strategy '
                 'FROM capital_symbols s JOIN capital_instances i ON i.account=s.account '
                 'AND i.strategy=s.strategy WHERE s.account=?')
        params = (account_id,)
        if symbol is not None:
            query += ' AND s.symbol=?'
            params += (symbol,)
        return tuple(SymbolOwnership(account_id, *row) for row in self.store.db.execute(
            query + ' ORDER BY s.symbol,s.strategy', params))

    def validate_shared_symbol(self, account_id, strategy_id, symbol):
        """Validate funded identity and same-direction journal eligibility without claiming."""
        self._binding(account_id, strategy_id)
        owners = {owner for owner, quantity in self.store.db.execute(
            'SELECT strategy,quantity FROM portfolio_claims WHERE account=? AND symbol=?',
            (account_id, symbol)) if Decimal(quantity)}
        owners.update(owner for owner, in self.store.db.execute(
            'SELECT strategy FROM capital_symbols WHERE account=? AND symbol=?', (account_id, symbol)))
        if self.mode(account_id) == CapitalPolicyMode.EXCLUSIVE:
            if any(owner != strategy_id for owner in owners):
                raise ValueError(f'position ownership conflict: {symbol}')
        else:
            for owner in owners:
                self._binding(account_id, owner)
            self._same_direction(account_id, symbol)

    def symbol_limit(self, account_id, symbol):
        row = self.store.db.execute('SELECT max_exposure FROM capital_symbol_limits '
                                    'WHERE account=? AND symbol=?', (account_id, symbol)).fetchone()
        if row is None:
            raise KeyError((account_id, symbol))
        return Decimal(row[0])

    def _apply_owner_symbol_limit(self, account_id, strategy_id, symbol):
        explicit = self.store.db.execute('SELECT 1 FROM capital_explicit_symbol_limits '
            'WHERE account=? AND symbol=?', (account_id, symbol)).fetchone()
        if explicit is None:
            self._tighten_symbol_limit(account_id, symbol,
                self.limits(account_id, strategy_id, original=True).max_instrument_exposure)

    def _tighten_symbol_limit(self, account_id, symbol, limit):
        row = self.store.db.execute('SELECT max_exposure FROM capital_symbol_limits '
                                    'WHERE account=? AND symbol=?', (account_id, symbol)).fetchone()
        if row is None:
            self.store.db.execute('INSERT INTO capital_symbol_limits VALUES (?,?,?)',
                                  (account_id, symbol, str(limit)))
        elif limit < Decimal(row[0]):
            self.store.db.execute('UPDATE capital_symbol_limits SET max_exposure=? WHERE account=? AND symbol=?',
                                  (str(limit), account_id, symbol))

    def set_symbol_limit(self, account_id, symbol, limit):
        """Explicitly tighten the persistent aggregate ceiling; never enlarge it."""
        decimal_value(limit, 'symbol exposure limit')
        with self.store.transaction():
            if limit > self.symbol_limit(account_id, symbol):
                raise ValueError('symbol limit cannot increase the existing risk boundary')
            self._tighten_symbol_limit(account_id, symbol, limit)

    def claim(self, account_id, strategy_id, symbol):
        """Register only this funded instance; caller holds the portfolio write transaction."""
        if not symbol:
            raise ValueError('symbol required')
        self.validate_shared_symbol(account_id, strategy_id, symbol)
        self._apply_owner_symbol_limit(account_id, strategy_id, symbol)
        self.store.db.execute('INSERT OR IGNORE INTO capital_symbols VALUES (?,?,?)',
                              (account_id, symbol, strategy_id))

    def _validate_release(self, account_id, strategy_id, symbol):
        from .contracts import TERMINAL
        if self.store.holdings(account_id, strategy_id).get(symbol, ZERO):
            raise ValueError('ownership release requires flat instance inventory')
        for owner, order in self._orders(account_id):
            snapshot = order['record'].snapshot
            if owner == strategy_id and snapshot.intent.instrument_id == symbol and (
                    snapshot.status not in TERMINAL or order['uncertain']):
                raise ValueError('unfinished orders prevent ownership release')

    def release(self, account_id, strategy_id, symbol):
        """Remove admission only; caller preserves inventory and historical attribution."""
        self._binding(account_id, strategy_id)
        self._validate_release(account_id, strategy_id, symbol)
        self.store.db.execute('DELETE FROM capital_symbols WHERE account=? AND symbol=? AND strategy=?',
                              (account_id, symbol, strategy_id))

    def transfer_flat_ownership(self, account_id, from_strategy, to_strategy, symbols):
        """Transfer policy claims in the caller's inventory/history transaction."""
        self._binding(account_id, from_strategy)
        self._binding(account_id, to_strategy)
        for symbol in symbols:
            self._validate_release(account_id, from_strategy, symbol)
            others = {owner for owner, quantity in self.store.db.execute(
                'SELECT strategy,quantity FROM portfolio_claims WHERE account=? AND symbol=?', (account_id, symbol)) if Decimal(quantity)}
            others.update(owner for owner, in self.store.db.execute(
                'SELECT strategy FROM capital_symbols WHERE account=? AND symbol=?', (account_id, symbol)))
            others.difference_update((from_strategy, to_strategy))
            if self.mode(account_id) == CapitalPolicyMode.EXCLUSIVE and others:
                raise ValueError(f'position ownership conflict: {symbol}')
            if self.mode(account_id) == CapitalPolicyMode.SHARED_SAME_DIRECTION:
                self._same_direction(account_id, symbol)
            self.store.db.execute('DELETE FROM capital_symbols WHERE account=? AND symbol=? AND strategy=?',
                                  (account_id, symbol, from_strategy))
            self._apply_owner_symbol_limit(account_id, to_strategy, symbol)
            self.store.db.execute('INSERT OR IGNORE INTO capital_symbols VALUES (?,?,?)',
                                  (account_id, symbol, to_strategy))

    def pause(self, account_id, strategy_id, paused=True):
        if type(paused) is not bool:
            raise ValueError('boolean pause state required')
        with self.store.transaction():
            if not paused and self.store.capital_balance(account_id, strategy_id)[3]:
                raise ValueError('retired capital allocation')
            cursor = self.store.db.execute(
                'UPDATE capital_instances SET paused=? WHERE account=? AND strategy=?',
                (int(paused), account_id, strategy_id))
            if cursor.rowcount != 1:
                raise KeyError((account_id, strategy_id))

    def propose(self, account_id, optimizer, data):
        """Return an optimizer recommendation; static allocations are never applied."""
        from dataclasses import replace
        import pandas as pd
        with self.store.transaction():
            owners = {owner: Decimal(capital) for owner, capital in self.store.db.execute(
                'SELECT strategy,capital FROM portfolio_allocations WHERE account=?', (account_id,))}
            if set(data.symbols) != set(owners):
                raise ValueError('optimizer labels must match the funded execution owners')
            budget = Decimal(self.store.db.execute(
                'SELECT budget FROM capital_accounts WHERE account=?', (account_id,)).fetchone()[0])
            previous = pd.Series({owner: float(capital / budget) for owner, capital in owners.items()})
        return optimizer.optimize(replace(data, previous_weights=previous))

    def risk_snapshot(self, executor, account_id, *, official_equity, account=None, margin=None):
        """Read one official account observation and one coherent journal view."""
        context = nullcontext() if self.store.db.in_transaction else self.store.transaction()
        with context:
            row = self.store.db.execute(
                'SELECT currency,budget FROM capital_accounts WHERE account=?', (account_id,)).fetchone()
            if row is None:
                raise KeyError(account_id)
            currency, budget = row[0], Decimal(row[1])
            if account is None:
                account = executor.adapter.read_account()
            if account.account_id != account_id:
                raise ValueError('matching official account required')
            derivatives = account.market == Market.BINANCE_USDM
            funding = None
            margin_attribution = None
            if derivatives:
                from .capital_funding import attribute_funding
                from .capital_margin import attribute_margin
                if margin is None:
                    margin = executor.adapter.margin_facts()
                if margin.account_id != account_id:
                    raise ValueError('margin facts must match the account')
                account = margin.shared(account.open_orders, reconciled=account.reconciled)
                margin_attribution = attribute_margin(self.store, account_id, margin)
                funding = attribute_funding(self.store, account_id, executor.adapter.funding_events(),
                    executor.adapter.funding_coverage(), account.observed_at)
            owned_total = self.store.holdings(account_id)
            actual = {position.instrument_id: position.quantity for position in account.positions}
            reconciled = (account.reconciled and not executor._account_uncertain(account_id)
                          and all(actual.get(symbol, ZERO) == quantity for symbol, quantity in owned_total.items())
                          and all(owned_total.get(symbol, ZERO) == quantity for symbol, quantity in actual.items()))
            strategies = []
            for owner, in self.store.db.execute(
                    'SELECT strategy FROM capital_instances WHERE account=? ORDER BY strategy', (account_id,)):
                capital = self.snapshot(account_id, owner)
                if not reconciled:
                    strategies.append(StrategyRiskSnapshot(capital, None, None, None, None,
                        None, None, None, None, None, False))
                    continue
                latest = self.store.db.execute(
                    'SELECT id FROM portfolio_plans WHERE account=? AND strategy=? ORDER BY rowid DESC LIMIT 1',
                    (account_id, owner)).fetchone()
                if latest is None:
                    payment = dict(funding.amounts)[owner] if derivatives and funding.complete else (None if derivatives else ZERO)
                    complete = not derivatives or (funding.complete and margin_attribution.complete)
                    nav = capital.cash + payment if complete else None
                    occupied = (dict(margin_attribution.amounts)[owner] if margin_attribution.complete
                                else None) if derivatives else ZERO
                    strategies.append(StrategyRiskSnapshot(capital, occupied, ZERO, ZERO, ZERO,
                        ZERO, payment, nav, None if nav is None else nav - occupied - capital.reserved, (),
                        complete, nav, ZERO))
                    continue
                report = executor.report(latest[0], official_equity=official_equity,
                                         readonly=True, account=account, derivatives=derivatives)
                occupied = sum((p.market_value for p in report.positions), ZERO)
                basis = sum((p.owned_cost_basis for p in report.positions), ZERO)
                gross_room = max(ZERO, self.limits(account_id, owner).account_budget - occupied - capital.reserved)
                payment, nav, remaining, complete = ZERO, report.economic_nav, min(capital.cash - capital.reserved, gross_room), True
                wallet = capital.cash
                if derivatives:
                    complete = margin_attribution.complete and funding.complete
                    occupied = dict(margin_attribution.amounts)[owner] if margin_attribution.complete else None
                    payment = dict(funding.amounts)[owner] if funding.complete else None
                    initial_basis = sum((Decimal(quantity) * Decimal(price) for quantity, price in self.store.db.execute(
                        'SELECT initial_quantity,entry_price FROM portfolio_claims WHERE account=? AND strategy=? '
                        'UNION ALL SELECT initial_quantity,entry_price FROM portfolio_claim_history WHERE account=? AND strategy=?',
                        (account_id, owner, account_id, owner))), ZERO)
                    allocation = self.store.allocation_basis(account_id, owner)
                    _, cash_in, cash_out, _ = self.store.capital_balance(account_id, owner)
                    wallet = (allocation['initial_cash'] + initial_basis + cash_in - cash_out
                              + report.realized_pnl + payment) if complete else None
                    nav = wallet + report.unrealized_pnl if complete else None
                    remaining = min(gross_room, max(ZERO, nav - occupied - capital.reserved)) if complete else None
                strategies.append(StrategyRiskSnapshot(capital, occupied, basis,
                    report.realized_pnl, report.unrealized_pnl, report.execution_fees,
                    payment, nav, remaining, tuple(report.positions), complete, wallet, report.corporate_cash_flow))
            balance = next((asset for asset in account.assets if asset.currency == currency), None)
            allocated = self.store.allocated_capital(account_id)
            return AccountRiskSnapshot(account_id, currency, account.observed_at,
                reconciled, None if balance is None else balance.available, budget, allocated,
                tuple(strategies), margin_attribution)

    def snapshot(self, account_id, strategy_id):
        context = nullcontext() if self.store.db.in_transaction else self.store.transaction()
        with context:
            return self._snapshot(account_id, strategy_id)

    def _snapshot(self, account_id, strategy_id):
        allocation = self.store.allocation(account_id, strategy_id)
        row = self.store.db.execute(
            'SELECT paused FROM capital_instances WHERE account=? AND strategy=?',
            (account_id, strategy_id)).fetchone()
        if row is None:
            raise KeyError((account_id, strategy_id))
        cash = self.store.cash(account_id, strategy_id, reserve=False)
        available = self.store.cash(account_id, strategy_id)
        return CapitalSnapshot(account_id, strategy_id, allocation['allocation_id'],
                               allocation['capital'], cash, cash - available, bool(row[0]))
