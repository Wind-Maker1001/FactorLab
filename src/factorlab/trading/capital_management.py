"""Capital postings and management over the portfolio execution database."""
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal as D
import json

from .capital_apportionment import exact_sum
from .contracts import Market, Side, TERMINAL, ZERO, decimal_value
from .storage import decode, dumps


SCHEMA = (
    """CREATE TABLE IF NOT EXISTS capital_balances (
        account TEXT NOT NULL, strategy TEXT NOT NULL, budget_delta TEXT NOT NULL,
        cash_in TEXT NOT NULL, cash_out TEXT NOT NULL, retired INTEGER NOT NULL,
        PRIMARY KEY(account, strategy))""",
    """CREATE TABLE IF NOT EXISTS capital_origins (
        account TEXT NOT NULL, strategy TEXT NOT NULL, started_at TEXT NOT NULL,
        PRIMARY KEY(account, strategy))""",
    """CREATE TABLE IF NOT EXISTS capital_events (
        account TEXT NOT NULL, event TEXT NOT NULL, request TEXT NOT NULL,
        payload TEXT NOT NULL, PRIMARY KEY(account, event))""",
    """CREATE INDEX IF NOT EXISTS capital_event_requests ON capital_events(account, request)""",
    """CREATE TABLE IF NOT EXISTS capital_commands (
        account TEXT NOT NULL, command TEXT NOT NULL, request TEXT NOT NULL,
        result TEXT, PRIMARY KEY(account, command))""",
    """CREATE INDEX IF NOT EXISTS capital_pending_commands
        ON capital_commands(account) WHERE result IS NULL""",
)


@dataclass(frozen=True)
class CapitalChange:
    event_id: str
    request_id: str
    source: str | None
    destination: str | None
    observed_at: datetime
    requested: D | None
    applied: D
    released_budget: D
    remaining: D
    executable: D
    status: str
    source_equity: D | None
    destination_equity: D | None


@dataclass(frozen=True)
class StrategyCapital:
    strategy_id: str
    instance_id: str
    allocation_id: str
    initial_capital: D
    allocated: D
    cash_in: D
    cash_out: D
    cash: D | None
    reserved: D
    occupied: D | None
    exit_fee_reserve: D | None
    withdrawable: D | None
    unavailable_cash: D | None
    equity: D | None
    realized_pnl: D | None
    unrealized_pnl: D | None
    execution_fees: D | None
    funding: D | None
    corporate_cash_flow: D | None
    retired: bool


@dataclass(frozen=True)
class AccountCapital:
    account_id: str
    currency: str
    observed_at: datetime
    official_equity: D
    official_wallet: D
    official_available: D
    budget: D
    allocated: D
    unallocated: D
    pool_cash: D | None
    wallet_room: D | None
    allocatable: D | None
    strategies: tuple[StrategyCapital, ...]


class CapitalManagement:
    def __init__(self, executor):
        self.executor = executor
        self.store = executor.store

    def snapshot(self, account_id):
        """Refresh original confirmations and read official facts under the order lock."""
        with self.store.transaction():
            return self._snapshot(account_id)

    def _snapshot(self, account_id):
        executor, store = self.executor, self.store
        executor._recover_account(account_id)
        account = executor.adapter.read_account()
        if account.account_id != account_id:
            raise ValueError('capital observation belongs to a different account')
        derivatives = account.market == Market.BINANCE_USDM
        margin = None
        if derivatives:
            account, margin = executor.adapter.capital_facts()
        unrealized = ZERO
        if derivatives:
            funds = margin.usdt()
            unrealized = funds['unrealizedProfit']
            if any(funds[key] is None for key in ('walletBalance', 'availableBalance', 'unrealizedProfit')):
                raise ValueError('official wallet, availability and unrealized profit are required')
            equity = funds['walletBalance'] + unrealized
            open_ids = {(o.intent.instrument_id, o.intent.client_order_id) for o in account.open_orders}
            account = margin.shared(account.open_orders,
                                    reconciled=account.reconciled and margin.open_order_identities() == open_ids)
        else:
            equity = account.official_equity
        if equity is None:
            raise ValueError('official account equity is required')
        risk = executor.capital_policy.risk_snapshot(executor, account_id,
            official_equity=equity, account=account, margin=margin)
        wallet = next((asset.total for asset in account.assets if asset.currency == risk.currency), None)
        if wallet is None:
            raise ValueError('official settlement balance is required')
        official_available = account.official_available
        if official_available is None:
            official_available = risk.available
        fee_rate = D(store.db.execute('SELECT fee_rate FROM capital_accounts WHERE account=?',
                                      (account_id,)).fetchone()[0])
        bindings = dict(store.db.execute('SELECT strategy,instance FROM capital_instances WHERE account=?',
                                         (account_id,)))
        marks = {p.instrument_id: p.mark_price for p in account.positions}
        owners = {s.capital.strategy_id for s in risk.strategies}
        orders = store.account_orders(account_id)
        known_ids = {order['record'].snapshot.intent.client_order_id for _, order in orders}
        foreign = any(order.intent.client_order_id not in known_ids and order.status not in TERMINAL
                      for order in account.open_orders)
        bound = (store.db.execute('SELECT COUNT(*) FROM portfolio_allocations WHERE account=?',
                                  (account_id,)).fetchone()[0] == len(owners))
        complete = risk.reconciled and bound and not foreign and all(s.complete for s in risk.strategies)
        wallet_room = None
        pool_cash = None
        if complete:
            attributed = exact_sum(s.wallet for s in risk.strategies)
            pool_cash = exact_sum((wallet, attributed.copy_negate()))
            complete = pool_cash >= ZERO
        if complete:
            obligations = exact_sum(s.capital.reserved + (s.occupied if derivatives else ZERO)
                                    for s in risk.strategies)
            wallet_room = max(ZERO, min(wallet - obligations, risk.available,
                                        official_available - max(ZERO, unrealized)))
        strategies = []
        for s in risk.strategies:
            owner = s.capital.strategy_id
            base = store.allocation_basis(account_id, owner)
            _, cash_in, cash_out, retired = store.capital_balance(account_id, owner)
            exit_fee = withdrawable = unavailable = None
            if complete:
                holdings = store.holdings(account_id, owner)
                gross = exact_sum(abs(q) * marks[symbol] for symbol, q in holdings.items() if q)
                exit_fee = gross * fee_rate
                collateral = s.occupied if derivatives else ZERO
                losses = min(ZERO, s.nav - s.wallet) if derivatives else ZERO
                free = s.wallet + losses - collateral - s.capital.reserved - exit_fee
                # The existing policy funds gross exposure at full notional.
                # Keep that headroom in addition to protecting wallet collateral.
                budget_room = s.capital.capital - gross - s.capital.reserved / (1 + fee_rate)
                withdrawal = min(free, wallet_room, budget_room)
                if derivatives:
                    withdrawal = min(withdrawal, s.capital.cash - s.capital.reserved + min(ZERO, s.funding))
                withdrawable = max(ZERO, withdrawal)
                unavailable = max(ZERO, s.wallet - withdrawable)
            strategies.append(StrategyCapital(owner, bindings[owner], s.capital.allocation_id,
                base['capital'], s.capital.capital, cash_in, cash_out, s.wallet, s.capital.reserved,
                s.occupied, exit_fee, withdrawable, unavailable, s.nav, s.realized_pnl,
                s.unrealized_pnl, s.fees, s.funding, s.corporate_cash_flow, retired))
        unallocated = risk.budget - risk.allocated
        allocatable = (max(ZERO, min(unallocated, pool_cash, wallet_room, equity - risk.allocated))
                       if complete else None)
        return AccountCapital(account_id, risk.currency, account.observed_at, equity, wallet,
            official_available, risk.budget, risk.allocated, unallocated, pool_cash, wallet_room, allocatable,
            tuple(strategies))

    def performance(self, account_id, strategy_id):
        """Link current marked equity to the valuations recorded at capital postings."""
        from .capital_performance import build_capital_performance

        with self.store.transaction():
            snapshot = self._snapshot(account_id)
            owner = next(s for s in snapshot.strategies if s.strategy_id == strategy_id)
            if owner.equity is None:
                raise ValueError('strategy performance requires reconciled equity')
            date = self.store.db.execute(
                'SELECT started_at FROM capital_origins WHERE account=? AND strategy=?',
                (account_id, strategy_id)).fetchone()
            started = None if date is None else datetime.fromisoformat(date[0])
            observations = []
            for entry in self.history(account_id):
                event = entry['result']
                if not event.applied:
                    continue
                if event.source == strategy_id:
                    observations.append((event.observed_at, event.source_equity, -event.applied))
                if event.destination == strategy_id:
                    observations.append((event.observed_at, event.destination_equity, event.applied))
            returns = build_capital_performance(initial_capital=owner.initial_capital, started_at=started,
                observations=observations, current=(snapshot.observed_at, owner.equity))
            utilization = ((owner.occupied + owner.reserved) / owner.equity
                           if owner.equity > ZERO else None)
            return dict(returns=returns, realized_pnl=owner.realized_pnl, unrealized_pnl=owner.unrealized_pnl,
                execution_fees=owner.execution_fees, funding=owner.funding,
                corporate_cash_flow=owner.corporate_cash_flow, capital_utilization=utilization)

    def _read(self, account_id, event_id):
        row = self.store.db.execute('SELECT payload FROM capital_events WHERE account=? AND event=?',
                                    (account_id, event_id)).fetchone()
        return None if row is None else decode(json.loads(row[0]))

    def history(self, account_id):
        """Capital requests and their execution steps, in durable posting order."""
        for payload, in self.store.db.execute(
                'SELECT payload FROM capital_events WHERE account=? ORDER BY rowid', (account_id,)):
            entry = decode(json.loads(payload))
            yield dict(operation=entry['operation'], result=CapitalChange(**entry['result']))

    def change(self, *, account_id, event_id, source, destination, amount, mode='EXACT'):
        """Allocate, withdraw or transfer. A None endpoint refers to the public pool.

        EXACT leaves an insufficient request untouched. STAGED posts available
        cash and records the remainder for a later advance after owner reductions.
        """
        decimal_value(amount, 'capital amount', positive=True)
        if source == destination or mode not in {'EXACT', 'STAGED'} or (source is None and mode == 'STAGED'):
            raise ValueError('distinct capital endpoints and a supported mode required')
        operation = dict(kind=mode, source=source, destination=destination, amount=amount)
        return self._request(account_id, event_id, operation)

    def release(self, *, account_id, event_id, strategy_id):
        """Retire a flat and settled strategy, releasing its budget and sweeping cash."""
        if not strategy_id:
            raise ValueError('strategy identity is required')
        return self._request(account_id, event_id,
                             dict(kind='RELEASE', source=strategy_id, destination=None, amount=None))

    def _request(self, account_id, event_id, operation):
        with self.store.transaction():
            return self._apply(account_id, event_id, event_id, operation)

    def advance(self, *, account_id, event_id, step_id, reduction_plan=None):
        """Resume a staged request after existing execution confirms sales or cancellations."""
        if step_id == event_id:
            raise ValueError('a staged advance needs its own event identity')
        with self.store.transaction():
            initial = self._read(account_id, event_id)
            if initial is None or initial['operation']['kind'] != 'STAGED':
                raise ValueError('original staged capital request required')
            operation = dict(initial['operation'], kind='ADVANCE', request_id=event_id,
                             reduction_plan=reduction_plan)
            return self._apply(account_id, step_id, event_id, operation)

    def _apply(self, account_id, event_id, request_id, operation):
        if not account_id or not event_id:
            raise ValueError('account and capital event identity are required')
        previous = self._read(account_id, event_id)
        if previous is not None:
            if previous['operation'] != operation:
                raise ValueError('capital event identity conflicts with original request')
            return CapitalChange(**previous['result'])
        source, destination, amount, kind = (operation[key] for key in
                                            ('source', 'destination', 'amount', 'kind'))
        remaining = amount
        if kind == 'ADVANCE':
            last, = self.store.db.execute(
                'SELECT payload FROM capital_events WHERE account=? AND request=? ORDER BY rowid DESC LIMIT 1',
                (account_id, request_id)).fetchone()
            remaining = decode(json.loads(last))['result']['remaining']
        snapshot = self._snapshot(account_id)
        if operation.get('reduction_plan') is not None:
            self._require_reduction(account_id, source, operation['reduction_plan'])
        if snapshot.pool_cash is not None and snapshot.pool_cash < ZERO:
            raise ValueError('attributed strategy cash exceeds the official wallet')
        if snapshot.allocatable is None:
            raise ValueError('capital changes require reconciled cash, orders, inventory and funding')
        owners = {s.strategy_id: s for s in snapshot.strategies}
        donor = owners[source] if source is not None else None
        recipient = owners[destination] if destination is not None else None
        if (recipient is not None and recipient.retired) or (donor is not None and donor.retired and kind != 'RELEASE'):
            raise ValueError('retired capital allocation')
        capacity = snapshot.allocatable if donor is None else donor.withdrawable
        if kind == 'RELEASE':
            self._require_flat(account_id, source)
            if donor.cash < ZERO:
                raise ValueError('strategy cash deficit must be funded before retirement')
            applied = donor.cash
            released_budget = donor.allocated
            if applied > snapshot.wallet_room:
                raise ValueError('official available cash cannot settle the release')
            left, status = ZERO, 'COMPLETE'
            self.store.db.execute('UPDATE capital_instances SET paused=1 WHERE account=? AND strategy=?',
                                  (account_id, source))
            for ownership in self.executor.capital_policy.ownership(account_id):
                if ownership.strategy_id == source:
                    self.executor.capital_policy.release(account_id, source, ownership.symbol)
        else:
            applied = min(remaining, capacity)
            if kind == 'EXACT' and applied != remaining:
                applied = ZERO
            released_budget = applied
            left = remaining - applied
            status = 'COMPLETE' if not left else ('INSUFFICIENT_CAPITAL' if kind == 'EXACT' else 'WAITING')
        if donor is not None and (applied or kind == 'RELEASE'):
            self._post(account_id, donor, -released_budget, ZERO, applied, kind == 'RELEASE')
        if recipient is not None and applied:
            self._post(account_id, recipient, released_budget, applied, ZERO, False)
        result = CapitalChange(event_id, request_id, source, destination, snapshot.observed_at,
            amount, applied, released_budget, left, applied if kind == 'RELEASE' else min(remaining, capacity), status,
            donor.equity if donor else None, recipient.equity if recipient else None)
        entry = dict(operation=operation, result=asdict(result))
        self.store.db.execute('INSERT INTO capital_events VALUES (?,?,?,?)',
                              (account_id, event_id, request_id, dumps(entry)))
        return result

    def _require_reduction(self, account_id, source, identity):
        plan = self.store.plan(identity)
        orders = [child['record'].snapshot for child in self.store.orders(identity)]
        if (plan is None or (plan['request']['account'], plan['request']['strategy']) != (account_id, source)
                or not orders or any(order.intent.side != Side.SELL for order in orders)):
            raise ValueError('reduction plan must sell inventory owned by the source strategy')
        if not any(order.cumulative_filled_quantity > ZERO for order in orders):
            raise ValueError('reduction plan has no confirmed fills')

    def _require_flat(self, account_id, source):
        if any(self.store.holdings(account_id, source).values()):
            raise ValueError('capital release requires flat strategy inventory')
        for owner, order in self.store.account_orders(account_id):
            if owner == source and (order['uncertain'] or order['record'].snapshot.status not in TERMINAL):
                raise ValueError('capital release requires confirmed terminal orders')

    def _post(self, account_id, owner, budget_delta, cash_in, cash_out, retired):
        strategy = owner.strategy_id
        previous, incoming, outgoing, _ = self.store.capital_balance(account_id, strategy)
        self.store.db.execute(
            'INSERT INTO capital_balances VALUES (?,?,?,?,?,?) ON CONFLICT(account,strategy) DO UPDATE SET '
            'budget_delta=excluded.budget_delta,cash_in=excluded.cash_in,cash_out=excluded.cash_out,retired=excluded.retired',
            (account_id, strategy, str(exact_sum((previous, budget_delta))),
             str(exact_sum((incoming, cash_in))), str(exact_sum((outgoing, cash_out))), int(retired)))
