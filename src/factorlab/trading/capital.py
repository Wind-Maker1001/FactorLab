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
    occupied: Decimal
    cost_basis: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    fees: Decimal
    funding: Decimal
    nav: Decimal
    remaining_budget: Decimal
    positions: tuple


@dataclass(frozen=True)
class AccountRiskSnapshot:
    account_id: str
    currency: str
    observed_at: datetime
    reconciled: bool
    available: Decimal
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

    def qualify(self, spec, binding, allocation=None):
        """Atomically reserve exclusive symbols for an explicitly funded instance."""
        account_id, owner = binding['account_id'], binding['instance_id']
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
            strategies = []
            for owner, in self.store.db.execute(
                    'SELECT strategy FROM capital_instances WHERE account=? ORDER BY strategy', (account_id,)):
                capital = self.snapshot(account_id, owner)
                latest = self.store.db.execute(
                    'SELECT id FROM portfolio_plans WHERE account=? AND strategy=? ORDER BY rowid DESC LIMIT 1',
                    (account_id, owner)).fetchone()
                if latest is None:
                    strategies.append(StrategyRiskSnapshot(capital, ZERO, ZERO, ZERO, ZERO,
                        ZERO, ZERO, capital.cash, capital.cash - capital.reserved, ()))
                    continue
                report = executor.report(latest[0], official_equity=official_equity,
                                         readonly=True, account=account)
                occupied = sum((p.market_value for p in report.positions), ZERO)
                basis = sum((p.owned_cost_basis for p in report.positions), ZERO)
                strategies.append(StrategyRiskSnapshot(capital, occupied, basis,
                    report.realized_pnl, report.unrealized_pnl, report.execution_fees,
                    ZERO, report.economic_nav, capital.cash - capital.reserved, tuple(report.positions)))
            balance = next(asset for asset in account.assets if asset.currency == currency)
            allocated = sum((Decimal(amount) for amount, in self.store.db.execute(
                'SELECT capital FROM portfolio_allocations WHERE account=?', (account_id,))), ZERO)
            return AccountRiskSnapshot(account_id, currency, account.observed_at,
                account.reconciled, balance.available, budget, allocated, tuple(strategies))

    def snapshot(self, account_id, strategy_id):
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
