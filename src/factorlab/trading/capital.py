"""Static capital policy over the shared portfolio execution journal."""
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from .contracts import Market, decimal_value

ZERO = Decimal(0)


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


class CapitalPolicy:
    """Policy metadata shares the execution transaction, never a second cash ledger."""

    def __init__(self, store):
        self.store = store
        store.db.executescript('''
            CREATE TABLE IF NOT EXISTS capital_accounts (
                account TEXT PRIMARY KEY, currency TEXT NOT NULL,
                budget TEXT NOT NULL, fee_rate TEXT NOT NULL);
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
                PRIMARY KEY(account,symbol));
            CREATE TABLE IF NOT EXISTS capital_instances (
                account TEXT NOT NULL, strategy TEXT NOT NULL, instance TEXT NOT NULL,
                allocation TEXT NOT NULL, logical_strategy TEXT NOT NULL,
                paused INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(account,strategy), UNIQUE(account,instance),
                UNIQUE(account,allocation));
        ''')

    def configure(self, *, account_id, currency, budget, fee_rate):
        decimal_value(budget, 'account capital budget', positive=True)
        decimal_value(fee_rate, 'fee reservation rate')
        if not account_id or not currency:
            raise ValueError('account and currency required')
        with self.store.transaction():
            previous = self.store.db.execute(
                'SELECT currency,budget,fee_rate FROM capital_accounts WHERE account=?', (account_id,)).fetchone()
            if previous is not None:
                if previous != (currency, str(budget), str(fee_rate)):
                    raise ValueError('static account capital policy changed')
                return
            claims = self.store.db.execute(
                'SELECT symbol FROM portfolio_claims WHERE account=? GROUP BY symbol '
                'HAVING count(*)>1', (account_id,)).fetchall()
            if claims:
                raise ValueError('exclusive capital policy cannot adopt shared symbols')
            total = sum((Decimal(row[0]) for row in self.store.db.execute(
                'SELECT capital FROM portfolio_allocations WHERE account=?', (account_id,))), ZERO)
            if total > budget:
                raise ValueError('existing allocations exceed account capital budget')
            self.store.db.execute('INSERT INTO capital_accounts VALUES (?,?,?,?)',
                                  (account_id, currency, str(budget), str(fee_rate)))

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
            if not all((strategy_id, instance_id, allocation_id)):
                raise ValueError('complete instance identity required')
            self.store.db.execute('INSERT INTO capital_instances VALUES (?,?,?,?,?,0)',
                                  (account_id, strategy_id, instance_id, allocation_id, logical_strategy))

    def limits(self, account_id, strategy_id):
        from .control import RiskLimits
        row = self.store.db.execute(
            'SELECT max_order,max_symbol,budget FROM capital_limits WHERE account=? AND strategy=?',
            (account_id, strategy_id)).fetchone()
        if row is None:
            cap = self.store.allocation(account_id, strategy_id)['capital']
            return RiskLimits(max_order_notional=cap, max_instrument_exposure=cap, account_budget=cap)
        return RiskLimits(max_order_notional=Decimal(row[0]),
                          max_instrument_exposure=Decimal(row[1]), account_budget=Decimal(row[2]))

    def set_limits(self, account_id, strategy_id, limits):
        """Persist an immutable tighter instance projection of existing RiskLimits."""
        with self.store.transaction():
            capital = self.store.allocation(account_id, strategy_id)['capital']
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

    def validate_orders(self, account, strategy_id, *, include_id=None):
        """Use the controller's gross exposure limits inside the outbox transaction."""
        from .control import exposure_limit_reason
        from .contracts import Side, TERMINAL
        from .storage import decode
        import json
        policy = self.store.db.execute('SELECT budget FROM capital_accounts WHERE account=?',
                                       (account.account_id,)).fetchone()
        if policy is None:
            return
        limits = self.limits(account.account_id, strategy_id)
        owned = self.store.holdings(account.account_id, strategy_id)
        exposures = {p.instrument_id: abs(owned.get(p.instrument_id, ZERO)) * p.mark_price
                     for p in account.positions}
        account_exposure = sum((abs(p.quantity) * p.mark_price for p in account.positions), ZERO)
        pending = []
        local_ids = set()
        for owner, payload in self.store.db.execute(
                'SELECT p.strategy,o.payload FROM portfolio_orders o JOIN portfolio_plans p ON p.id=o.plan '
                'WHERE p.account=?', (account.account_id,)):
            order = decode(json.loads(payload))
            snapshot = order['record'].snapshot
            intent = snapshot.intent
            local_ids.add(intent.client_order_id)
            if snapshot.status in TERMINAL or intent.side != Side.BUY:
                continue
            amount = (intent.quantity - snapshot.cumulative_filled_quantity) * order['reservation_price']
            account_exposure += amount
            if owner == strategy_id:
                exposures[intent.instrument_id] = exposures.get(intent.instrument_id, ZERO) + amount
                if not order['attempted'] or intent.client_order_id == include_id:
                    pending.append((intent.instrument_id, intent.quantity * order['reservation_price']))
        for order in account.open_orders:
            if order.intent.client_order_id not in local_ids and order.status not in TERMINAL:
                if order.intent.limit_price is None:
                    raise ValueError('unpriced foreign order risk')
                account_exposure += ((order.intent.quantity - order.cumulative_filled_quantity)
                                     * order.intent.limit_price)
        for symbol, amount in pending:
            reason = exposure_limit_reason(limits, order_notional=amount,
                instrument_exposure=exposures[symbol], account_exposure=sum(exposures.values(), ZERO))
            if reason != 'APPROVED':
                raise ValueError(reason)
            if account_exposure > Decimal(policy[0]):
                raise ValueError('ACCOUNT_BUDGET_LIMIT')

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
            configured = self.store.allocation(account_id, owner)
            currency = self.store.db.execute(
                'SELECT currency FROM capital_accounts WHERE account=?', (account_id,)).fetchone()[0]
            if spec.capital['currency'] != currency or spec.capital['requested'] > configured['capital']:
                raise ValueError('strategy capital demand exceeds static allocation')
            for symbol in spec.symbols:
                self.claim(account_id, owner, symbol)
            return configured

    def claim(self, account_id, strategy_id, symbol):
        """Caller holds the portfolio write transaction."""
        owners = self.store.db.execute(
            'SELECT strategy FROM portfolio_claims WHERE account=? AND symbol=? '
            'UNION SELECT strategy FROM capital_symbols WHERE account=? AND symbol=?',
            (account_id, symbol, account_id, symbol)).fetchall()
        if any(owner != strategy_id for owner, in owners):
            raise ValueError(f'position ownership conflict: {symbol}')
        self.store.db.execute('INSERT OR IGNORE INTO capital_symbols VALUES (?,?,?)',
                              (account_id, symbol, strategy_id))

    def pause(self, account_id, strategy_id, paused=True):
        if type(paused) is not bool:
            raise ValueError('boolean pause state required')
        with self.store.transaction():
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

    def risk_snapshot(self, executor, account_id, *, official_equity):
        """Read one official account observation and one coherent journal view."""
        with self.store.transaction():
            row = self.store.db.execute(
                'SELECT currency,budget FROM capital_accounts WHERE account=?', (account_id,)).fetchone()
            if row is None:
                raise KeyError(account_id)
            currency, budget = row[0], Decimal(row[1])
            account = executor.adapter.read_account()
            if account.account_id != account_id:
                raise ValueError('matching official account required')
            derivatives = account.market == Market.BINANCE_USDM
            funding = None
            margin = None
            if derivatives:
                from .capital_funding import attribute_funding
                margin = executor.adapter.margin_facts()
                if margin.account_id != account_id:
                    raise ValueError('margin facts must match the account')
                account = margin.shared(account.open_orders, reconciled=account.reconciled)
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
                    nav = None if payment is None else capital.cash + payment
                    strategies.append(StrategyRiskSnapshot(capital, ZERO, ZERO, ZERO, ZERO,
                        ZERO, payment, nav, None if nav is None else nav - capital.reserved, (),
                        not derivatives or funding.complete))
                    continue
                report = executor.report(latest[0], official_equity=official_equity,
                                         readonly=True, account=account)
                occupied = sum((p.market_value for p in report.positions), ZERO)
                basis = sum((p.owned_cost_basis for p in report.positions), ZERO)
                gross_room = max(ZERO, self.limits(account_id, owner).account_budget - occupied - capital.reserved)
                payment, nav, remaining, complete = ZERO, report.economic_nav, min(capital.cash - capital.reserved, gross_room), True
                if derivatives:
                    symbols = self.store.holdings(account_id, owner)
                    rows = [position for position in margin.position_details()
                            if symbols.get(position['symbol'], ZERO) != ZERO]
                    margin_complete = (margin.source_complete
                                and all(position['initial_margin'] is not None
                                        and position['quantity'] == symbols[position['symbol']] for position in rows)
                                and {position['symbol'] for position in rows} ==
                                    {symbol for symbol, quantity in symbols.items() if quantity})
                    complete = margin_complete and funding.complete
                    occupied = sum((position['initial_margin'] for position in rows), ZERO) if margin_complete else None
                    payment = dict(funding.amounts)[owner] if funding.complete else None
                    initial_basis = sum((Decimal(quantity) * Decimal(price) for quantity, price in self.store.db.execute(
                        'SELECT initial_quantity,entry_price FROM portfolio_claims WHERE account=? AND strategy=? '
                        'UNION ALL SELECT initial_quantity,entry_price FROM portfolio_claim_history WHERE account=? AND strategy=?',
                        (account_id, owner, account_id, owner))), ZERO)
                    allocation = self.store.allocation(account_id, owner)
                    initial_unrealized = allocation['capital'] - allocation['initial_cash'] - initial_basis
                    nav = (capital.capital + report.realized_pnl + report.unrealized_pnl
                           - initial_unrealized + payment) if complete else None
                    remaining = min(gross_room, max(ZERO, nav - occupied - capital.reserved)) if complete else None
                strategies.append(StrategyRiskSnapshot(capital, occupied, basis,
                    report.realized_pnl, report.unrealized_pnl, report.execution_fees,
                    payment, nav, remaining, tuple(report.positions), complete))
            balance = next((asset for asset in account.assets if asset.currency == currency), None)
            allocated = sum((Decimal(amount) for amount, in self.store.db.execute(
                'SELECT capital FROM portfolio_allocations WHERE account=?', (account_id,))), ZERO)
            return AccountRiskSnapshot(account_id, currency, account.observed_at,
                reconciled, None if balance is None else balance.available, budget, allocated, tuple(strategies))

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
