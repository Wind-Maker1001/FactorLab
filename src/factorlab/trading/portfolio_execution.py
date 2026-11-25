"""Durable, cash-funded portfolio execution over an authoritative trading adapter."""
from __future__ import annotations

from contextlib import nullcontext
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D, ROUND_CEILING, ROUND_FLOOR
from hashlib import sha256
import json
from typing import Mapping, Protocol

from .contracts import (AccountSnapshot, OrderEvent, OrderIntent, OrderSnapshot,
                        OrderStatus, OrderType, Side, TERMINAL, decimal_value, utc)
from .portfolio_target import plan_targets
from .state import OrderRecord, advance
from .storage import PaperStore, decode, dumps

ZERO = D(0)


def plan_identity(account_id, strategy_id, trading_session, rebalance_id):
    """Stable execution identity, independent of changing account projections."""
    return sha256(dumps([account_id, strategy_id, trading_session, rebalance_id]).encode()).hexdigest()[:32]


class OrderAbsent(LookupError):
    """An authoritative original-client-ID lookup confirmed no such order."""


class SubmissionNotAttempted(OrderAbsent):
    """Durable adapter evidence proves this original identity never reached POST.

    A broker 404 alone is not this evidence. An adapter may use a durable local
    admission journal that is always committed before every HTTP submission.
    """


class PortfolioAdapter(Protocol):
    """query_order must distinguish authoritative absence from uncertain transport."""

    def read_account(self) -> AccountSnapshot: ...
    def submit_order(self, intent: OrderIntent) -> OrderEvent: ...
    def query_order(self, client_order_id: str) -> OrderEvent: ...
    def cancel_order(self, client_order_id: str) -> OrderEvent: ...


@dataclass(frozen=True)
class ExecutionQuote:
    instrument_id: str
    bid: D
    ask: D
    event_at: datetime
    received_at: datetime
    session: str
    source: str
    valid: bool = True
    reason: str = ''
    decision_window: tuple[datetime, datetime] | None = None

    def __post_init__(self):
        utc(self.event_at)
        utc(self.received_at)
        if self.valid:
            decimal_value(self.bid, 'bid', positive=True)
            decimal_value(self.ask, 'ask', positive=True)
            if self.ask < self.bid:
                raise ValueError('uncrossed execution quote required')
        if not self.instrument_id or not self.source:
            raise ValueError('identified execution quote required')
        if self.decision_window is not None:
            start, end = self.decision_window
            utc(start)
            utc(end)
            if start >= end:
                raise ValueError('nonempty decision window required')


@dataclass(frozen=True)
class InstrumentRules:
    quantity_step: D
    minimum_notional: D
    tradable: bool = True
    time_in_force: str = 'GTC'
    price_tick: D | None = None
    limit_price_tolerance: D = ZERO

    def __post_init__(self):
        decimal_value(self.quantity_step, 'quantity_step', positive=True)
        decimal_value(self.minimum_notional, 'minimum_notional')
        decimal_value(self.limit_price_tolerance, 'limit_price_tolerance')
        if not ZERO <= self.limit_price_tolerance < D(1):
            raise ValueError('limit price tolerance must be in [0, 1)')
        if self.price_tick is not None:
            decimal_value(self.price_tick, 'price_tick', positive=True)


@dataclass(frozen=True)
class RebalanceResult:
    rebalance_id: str
    phase: str
    reasons: Mapping[str, str]
    targets: Mapping[str, D]
    order_ids: tuple[str, ...]


class PortfolioStore(PaperStore):
    """Account claims, immutable rebalance inputs and an intent-before-POST outbox.

    Use a dedicated database shared by every strategy controlling this account.
    SQLite write transactions serialize each account read/decision/side effect.
    A lost POST result leaves its committed original intent for identity recovery.
    """

    def __init__(self, path):
        super().__init__(path, continuous_capacity=True)
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS portfolio_claims (
                account TEXT NOT NULL, symbol TEXT NOT NULL, strategy TEXT NOT NULL,
                quantity TEXT NOT NULL, initial_quantity TEXT NOT NULL,
                entry_price TEXT NOT NULL,
                PRIMARY KEY(account, symbol, strategy));
            CREATE TABLE IF NOT EXISTS portfolio_plans (
                id TEXT PRIMARY KEY, account TEXT NOT NULL, strategy TEXT NOT NULL,
                session TEXT NOT NULL, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS portfolio_orders (
                id TEXT PRIMARY KEY, plan TEXT NOT NULL, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS portfolio_confirmations (
                account TEXT NOT NULL, event TEXT NOT NULL, payload TEXT NOT NULL,
                PRIMARY KEY(account, event));
            CREATE TABLE IF NOT EXISTS portfolio_corporate_actions (
                account TEXT NOT NULL, strategy TEXT NOT NULL, event TEXT NOT NULL,
                payload TEXT NOT NULL, PRIMARY KEY(account, strategy, event));
            CREATE TABLE IF NOT EXISTS portfolio_allocations (
                account TEXT NOT NULL, strategy TEXT NOT NULL, allocation TEXT NOT NULL,
                capital TEXT NOT NULL, initial_cash TEXT NOT NULL,
                PRIMARY KEY(account, strategy), UNIQUE(account, allocation));
        ''')
        with self.transaction():
            for table, key in (('portfolio_claims', ('account', 'symbol', 'strategy')),
                               ('portfolio_corporate_actions', ('account', 'strategy', 'event'))):
                columns = self.db.execute('PRAGMA table_info(' + table + ')').fetchall()
                previous = tuple(row[1] for row in sorted(columns, key=lambda row: row[5]) if row[5])
                if previous == key:
                    continue
                self.db.execute('ALTER TABLE ' + table + ' RENAME TO ' + table + '_legacy')
                declaration = ','.join(row[1] + ' TEXT NOT NULL' for row in columns)
                self.db.execute('CREATE TABLE ' + table + ' (' + declaration
                                + ', PRIMARY KEY(' + ','.join(key) + '))')
                self.db.execute('INSERT INTO ' + table + ' SELECT * FROM ' + table + '_legacy ORDER BY rowid')
                self.db.execute('DROP TABLE ' + table + '_legacy')

        from .capital import CapitalPolicy
        self.capital_policy = CapitalPolicy(self)

    def holdings(self, account_id, strategy_id=None):
        query = 'SELECT symbol,quantity FROM portfolio_claims WHERE account=?'
        params = (account_id,)
        if strategy_id is not None:
            query += ' AND strategy=?'
            params += (strategy_id,)
        result = {}
        for symbol, quantity in self.db.execute(query, params):
            result[symbol] = result.get(symbol, ZERO) + D(quantity)
        return result

    def allocation(self, account_id, strategy_id):
        row = self.db.execute('SELECT allocation,capital,initial_cash FROM portfolio_allocations '
                              'WHERE account=? AND strategy=?', (account_id, strategy_id)).fetchone()
        return None if row is None else dict(allocation_id=row[0], capital=D(row[1]), initial_cash=D(row[2]))

    def cash(self, account_id, strategy_id, *, exclude_id=None, reserve=True):
        """Allocated cash after confirmed fills, actions and all local buy reservations."""
        allocation = self.allocation(account_id, strategy_id)
        return allocation['initial_cash'] + self.cash_flow(account_id, strategy_id,
                                                          exclude_id=exclude_id, reserve=reserve)

    def cash_flow(self, account_id, strategy_id, *, exclude_id=None, reserve=False):
        """Net economic postings, excluding any initial capital contribution."""
        cash = ZERO
        fee_row = self.db.execute('SELECT fee_rate FROM capital_accounts WHERE account=?',
                                  (account_id,)).fetchone()
        fee_rate = ZERO if fee_row is None else D(fee_row[0])
        for payload, in self.db.execute(
                'SELECT o.payload FROM portfolio_orders o JOIN portfolio_plans p ON p.id=o.plan '
                'WHERE p.account=? AND p.strategy=?', (account_id, strategy_id)):
            order = decode(json.loads(payload))
            snapshot = order['record'].snapshot
            intent = snapshot.intent
            amount = order.get('filled_notional', ZERO)
            cash += (amount if intent.side == Side.SELL else -amount) - snapshot.cumulative_fee
            if (reserve and intent.side == Side.BUY and snapshot.status not in TERMINAL
                    and intent.client_order_id != exclude_id):
                cash -= ((intent.quantity - snapshot.cumulative_filled_quantity)
                         * order.get('reservation_price', intent.limit_price) * (1 + fee_rate))
        for payload, in self.db.execute(
                'SELECT payload FROM portfolio_corporate_actions WHERE account=? AND strategy=?',
                (account_id, strategy_id)):
            cash += decode(json.loads(payload))['cash_flow']
        return cash

    def plan(self, identity):
        row = self.db.execute('SELECT payload FROM portfolio_plans WHERE id=?', (identity,)).fetchone()
        if row is None:
            return None
        plan = decode(json.loads(row[0]))
        # Weight-only journals predate the explicit strategy-quantity fields.
        plan['request'].setdefault('quantity_deltas', None)
        plan['request'].setdefault('signal_inputs', None)
        return plan

    def save_plan(self, plan):
        self.db.execute('UPDATE portfolio_plans SET payload=? WHERE id=?', (dumps(plan), plan['id']))

    def orders(self, identity):
        return [decode(json.loads(row[0])) for row in self.db.execute(
            'SELECT payload FROM portfolio_orders WHERE plan=? ORDER BY id', (identity,))]

    def save_order(self, order):
        self.db.execute('UPDATE portfolio_orders SET payload=? WHERE id=?',
                        (dumps(order), order['record'].snapshot.intent.client_order_id))


class PortfolioExecutor:
    """One invocation reconciles and submits a phase; it never polls for fills.

    The caller invokes run_once on an account/order event or a scheduled decision.
    All strategy owners must use the same PortfolioStore. Initial inventory may
    only be adopted explicitly; merely observing a broker position is not a claim.
    """

    def __init__(self, adapter: PortfolioAdapter, store: PortfolioStore, *,
                 quote_currency='USD', max_quote_age=timedelta(seconds=5),
                 clock=lambda: datetime.now(timezone.utc)):
        if max_quote_age <= timedelta(0):
            raise ValueError('positive quote age required')
        self.adapter, self.store = adapter, store
        self.quote_currency, self.max_quote_age, self.clock = quote_currency, max_quote_age, clock
        self.capital_policy = store.capital_policy

    def _capital_admission(self, account_id, strategy_id, *, allow_paused=False):
        policy = self.store.db.execute(
            'SELECT currency FROM capital_accounts WHERE account=?', (account_id,)).fetchone()
        if policy is None:
            return
        if policy[0] != self.quote_currency:
            raise ValueError('capital policy currency differs from executor')
        binding = self.store.db.execute(
            'SELECT paused FROM capital_instances WHERE account=? AND strategy=?',
            (account_id, strategy_id)).fetchone()
        if binding is None:
            raise ValueError('explicit strategy capital binding required')
        if binding[0] and not allow_paused:
            raise ValueError('strategy capital allocation is paused')
        return bool(binding[0])

    def _funding_adjustment(self, account, strategy_id):
        from .contracts import Market
        if account.market != Market.BINANCE_USDM or self.store.db.execute(
                'SELECT 1 FROM capital_accounts WHERE account=?', (account.account_id,)).fetchone() is None:
            return ZERO
        from .capital_funding import attribute_funding
        facts = self.adapter.margin_facts()
        if facts.account_id != account.account_id:
            raise ValueError('margin facts must match the account')
        attribution = attribute_funding(self.store, account.account_id, self.adapter.funding_events(),
            self.adapter.funding_coverage(), facts.observed_at)
        if not facts.source_complete or not attribution.complete:
            raise ValueError('FUNDING_RECONCILIATION_REQUIRED')
        # Funding receipts are reported in NAV but cannot enlarge static capital.
        return min(ZERO, dict(attribution.amounts)[strategy_id])

    def adopt_allocation(self, *, account_id, strategy_id, allocation_id, capital,
                         current_cash, official_equity, receipt_id):
        """Bind an existing journal using confirmed current cash, without double posting."""
        from .capital_adoption import adopt_allocation
        return adopt_allocation(self, account_id=account_id, strategy_id=strategy_id,
            allocation_id=allocation_id, capital=capital, current_cash=current_cash,
            official_equity=official_equity, receipt_id=receipt_id)

    def adopt_existing(self, *, receipt_id, account_id, sleeves, official_equity):
        """Atomically adopt explicitly attributed original execution journals."""
        from .capital_migration import adopt_existing
        return adopt_existing(self, receipt_id=receipt_id, account_id=account_id,
                              sleeves=sleeves, official_equity=official_equity)

    def allocate(self, *, account_id, strategy_id, allocation_id, capital, initial_cash,
                 official_equity):
        """Bind existing owned inventory and an explicit cash sleeve without minting capital.

        Allocation is immutable. Replaying the same registration is idempotent;
        changing capital requires an explicit transfer, not another registration.
        """
        decimal_value(capital, 'allocated capital', positive=True)
        decimal_value(initial_cash, 'allocated cash')
        decimal_value(official_equity, 'official equity', positive=True)
        if not all((account_id, strategy_id, allocation_id)) or initial_cash > capital:
            raise ValueError('identified cash-funded allocation required')
        value = dict(allocation_id=allocation_id, capital=capital, initial_cash=initial_cash)
        with self.store.transaction():
            previous = self.store.allocation(account_id, strategy_id)
            if previous is not None:
                if previous != value:
                    raise ValueError('allocation identity or initial funding changed')
                return
            unallocated = self.store.db.execute(
                'SELECT DISTINCT strategy FROM portfolio_claims WHERE account=? AND strategy!=? '
                'AND strategy NOT IN (SELECT strategy FROM portfolio_allocations WHERE account=?)',
                (account_id, strategy_id, account_id)).fetchall()
            if unallocated:
                raise ValueError('existing strategy capital must be allocated before sharing the account')
            self._recover_account(account_id)
            account = self.adapter.read_account()
            if account.account_id != account_id or not account.reconciled or self._account_uncertain(account_id):
                raise ValueError('allocation requires a matching reconciled account')
            if self.store.cash_flow(account_id, strategy_id) != ZERO:
                raise ValueError('existing economic postings require adopt_allocation with confirmed current cash')
            holdings = self.store.holdings(account_id, strategy_id)
            value = sum((p.mark_price * holdings.get(p.instrument_id, ZERO) for p in account.positions), ZERO)
            if initial_cash + value != capital:
                raise ValueError('allocation funding must equal cash plus explicitly owned inventory')
            cash_used = sum((self.store.cash(account_id, owner, reserve=False) for owner, in self.store.db.execute(
                'SELECT strategy FROM portfolio_allocations WHERE account=?', (account_id,))), ZERO)
            official_cash = next(a.total for a in account.assets if a.currency == self.quote_currency)
            if cash_used + initial_cash > official_cash:
                raise ValueError('allocations exceed official account cash')
            committed = sum((D(amount) for amount, in self.store.db.execute(
                'SELECT capital FROM portfolio_allocations WHERE account=?', (account_id,))), ZERO)
            policy = self.store.db.execute(
                'SELECT budget FROM capital_accounts WHERE account=?', (account_id,)).fetchone()
            if policy is not None and committed + capital > D(policy[0]):
                raise ValueError('allocations exceed static account capital budget')
            if committed + capital > official_equity:
                raise ValueError('allocations exceed official account equity')
            self.store.db.execute('INSERT INTO portfolio_allocations VALUES (?,?,?,?,?)',
                (account_id, strategy_id, allocation_id, str(capital), str(initial_cash)))

    def projection(self, account, strategy_id):
        """Return a sleeve snapshot; only the aggregate snapshot authorizes execution."""
        holdings = self.store.holdings(account.account_id, strategy_id)
        cash = self.store.cash(account.account_id, strategy_id, reserve=False)
        positions = tuple(replace(p, quantity=holdings[p.instrument_id]) for p in account.positions
                          if holdings.get(p.instrument_id, ZERO))
        assets = tuple(replace(a, total=cash, reserved=ZERO) if a.currency == self.quote_currency else a
                       for a in account.assets)
        return replace(account, positions=positions, assets=assets, open_orders=())

    def create(self, *, account_id, strategy_id, strategy_version, trading_session,
               weights, owned_symbols, now, initial_inventory=None,
               economic_multipliers=None, rebalance_id=None, initial_capital=None,
               quantity_deltas=None, signal_inputs=None, direct_orders=None):
        """Persist a complete target vector before any external order side effect.

        initial_inventory maps explicitly adopted symbols to (quantity, basis).
        Omitting it claims only flat inventory; foreign holdings cannot be adopted
        implicitly. Same account/strategy/session with different inputs is an error.
        quantity_deltas optionally supplies signed broker-share order quantities;
        omitted symbols mean no order, not liquidation. Remaining quantities
        deduct this plan's fills only, without current-price weight sizing.
        signal_inputs is a serializable provenance/checkpoint snapshot included
        in immutable identity validation. Strategy preparation must retain its
        original holdings snapshot when replaying the same decision.
        """
        utc(now)
        owned_symbols = tuple(owned_symbols)
        if not all((account_id, strategy_id, strategy_version, trading_session)):
            raise ValueError('complete rebalance identity required')
        for weight in weights.values():
            decimal_value(weight, 'weight')
        if sum(weights.values(), ZERO) > 1 or set(weights) - set(owned_symbols):
            raise ValueError('long-only weights and explicit symbol ownership required')
        if initial_capital is not None:
            decimal_value(initial_capital, 'initial capital', positive=True)
        multipliers = dict(economic_multipliers or {})
        for multiplier in multipliers.values():
            decimal_value(multiplier, 'economic multiplier', positive=True)
        initial = dict(initial_inventory or {})
        if set(initial) - set(owned_symbols):
            raise ValueError('adoption requires explicit symbol ownership')
        for quantity, basis in initial.values():
            decimal_value(quantity, 'adopted quantity')
            decimal_value(basis, 'entry basis')
        deltas = None if quantity_deltas is None else dict(quantity_deltas)
        if deltas is not None:
            if set(deltas) - set(owned_symbols):
                raise ValueError('order quantities require explicit symbol ownership')
            for delta in deltas.values():
                if not isinstance(delta, D) or not delta.is_finite():
                    raise ValueError('finite Decimal order quantity required')
        signal_id = rebalance_id if rebalance_id is not None else trading_session
        if not signal_id:
            raise ValueError('nonempty rebalance identity required')
        identity = plan_identity(account_id, strategy_id, trading_session, signal_id)
        request = dict(account=account_id, strategy=strategy_id, version=strategy_version,
                       session=trading_session, rebalance_id=signal_id, weights=dict(weights),
                       owned=sorted(owned_symbols), initial=initial, multipliers=multipliers,
                       initial_capital=initial_capital, quantity_deltas=deltas,
                       signal_inputs=signal_inputs)
        if direct_orders is not None:
            if weights or deltas is not None:
                raise ValueError('direct orders cannot mix with portfolio targets')
            direct_orders = tuple(direct_orders)
            ids = set()
            for intent, reservation_price in direct_orders:
                if intent.instrument_id not in owned_symbols or intent.client_order_id in ids:
                    raise ValueError('direct order symbols and identities must belong to this plan')
                ids.add(intent.client_order_id)
                decimal_value(reservation_price, 'order reservation price', positive=True)
                if intent.order_type == OrderType.LIMIT and intent.limit_price != reservation_price:
                    raise ValueError('limit reservation must use the original limit price')
            request['direct_orders'] = direct_orders
        with self.store.transaction():
            previous = self.store.plan(identity)
            if previous:
                if previous['request'] != decode(json.loads(dumps(request))):
                    raise ValueError('rebalance identity already has different targets or ownership')
                return identity
            self._capital_admission(account_id, strategy_id)
            self._recover_account(account_id)
            exclusive = self.store.db.execute(
                'SELECT 1 FROM capital_accounts WHERE account=?', (account_id,)).fetchone() is not None
            active = self.store.db.execute(
                'SELECT id,payload FROM portfolio_plans WHERE account=? AND strategy=?',
                (account_id, strategy_id)).fetchall()
            if any(decode(json.loads(payload))['phase'] != 'COMPLETE' for _, payload in active):
                raise ValueError('previous rebalance must complete before new targets')
            for symbol in sorted(owned_symbols):
                if exclusive:
                    self.capital_policy.claim(account_id, strategy_id, symbol)
                claims = self.store.db.execute(
                    'SELECT strategy FROM portfolio_claims WHERE account=? AND symbol=?',
                    (account_id, symbol)).fetchall()
                if any(owner != strategy_id and (
                        exclusive or self.store.allocation(account_id, owner) is None
                        or self.store.allocation(account_id, strategy_id) is None) for owner, in claims):
                    raise ValueError(f'position ownership conflict: {symbol}')
                if not any(owner == strategy_id for owner, in claims):
                    quantity, basis = initial.get(symbol, (ZERO, ZERO))
                    self.store.db.execute('INSERT INTO portfolio_claims VALUES (?,?,?,?,?,?)',
                        (account_id, symbol, strategy_id, str(quantity), str(quantity), str(basis)))
            targets = {}
            if deltas is not None:
                for symbol in sorted(owned_symbols):
                    quantity = D(self.store.db.execute(
                        'SELECT quantity FROM portfolio_claims WHERE account=? AND symbol=? AND strategy=?',
                        (account_id, symbol, strategy_id)).fetchone()[0])
                    target = quantity + deltas.get(symbol, ZERO)
                    if target < 0:
                        raise ValueError('strategy order exceeds owned inventory')
                    targets[symbol] = target
            plan = dict(id=identity, request=request, created_at=now, phase='REDUCE',
                        targets=targets, reasons={}, equity=None)
            self.store.db.execute('INSERT INTO portfolio_plans VALUES (?,?,?,?,?)',
                (identity, account_id, strategy_id, trading_session, dumps(plan)))
            if direct_orders is not None:
                for intent, price in direct_orders:
                    if intent.market != self.adapter.read_account().market:
                        raise ValueError('direct order market differs from the account')
                    order = dict(record=OrderRecord(OrderSnapshot(intent), now, price),
                                 reference_price=price, reservation_price=price,
                                 attempted=False, uncertain=False)
                    self.store.db.execute('INSERT INTO portfolio_orders VALUES (?,?,?)',
                                          (intent.client_order_id, identity, dumps(order)))
                allocation = self.store.allocation(account_id, strategy_id)
                if allocation is not None and self.store.cash(account_id, strategy_id) < ZERO:
                    raise ValueError('direct orders exceed allocated cash')
                account = self.adapter.read_account()
                if account.account_id != account_id or not account.reconciled or self._account_uncertain(account_id):
                    raise ValueError('direct reservation requires a matching reconciled account')
                if (allocation is not None and any(intent.side == Side.BUY for intent, _ in direct_orders)
                        and (self.store.cash(account_id, strategy_id)
                             + self._funding_adjustment(account, strategy_id)) < ZERO):
                    raise ValueError('direct orders exceed capital after funding')
                balance = next(a for a in account.assets if a.currency == self.quote_currency)
                broker_ids = {o.intent.client_order_id for o in account.open_orders}
                pending = ZERO
                for payload, in self.store.db.execute(
                        'SELECT o.payload FROM portfolio_orders o JOIN portfolio_plans p ON p.id=o.plan '
                        'WHERE p.account=?', (account_id,)):
                    order = decode(json.loads(payload))
                    snapshot = order['record'].snapshot
                    if (snapshot.intent.side == Side.BUY and snapshot.status not in TERMINAL
                            and snapshot.intent.client_order_id not in broker_ids):
                        pending += (snapshot.intent.quantity - snapshot.cumulative_filled_quantity) * order['reservation_price']
                fee_row = self.store.db.execute('SELECT fee_rate FROM capital_accounts WHERE account=?',
                                                (account_id,)).fetchone()
                fee_factor = 1 + (ZERO if fee_row is None else D(fee_row[0]))
                if pending * fee_factor > balance.available:
                    raise ValueError('direct orders exceed available account cash')
                self.capital_policy.validate_orders(account, strategy_id)
        return identity

    def transfer_flat_ownership(self, *, account_id, from_strategy, to_strategy, symbols):
        """Transfer explicitly selected flat sleeves after predecessor tasks settle."""
        symbols = tuple(symbols)
        with self.store.transaction():
            self._recover_account(account_id)
            account = self.adapter.read_account()
            if not account.reconciled or self._account_uncertain(account_id):
                raise ValueError('ownership transfer requires a reconciled account')
            plans = self.store.db.execute(
                'SELECT payload FROM portfolio_plans WHERE account=? AND strategy=?',
                (account_id, from_strategy)).fetchall()
            if any(decode(json.loads(payload))['phase'] != 'COMPLETE' for payload, in plans):
                raise ValueError('ownership transfer requires completed predecessor tasks')
            for symbol in symbols:
                claim = self.store.db.execute(
                    'SELECT strategy,quantity FROM portfolio_claims WHERE account=? AND symbol=? AND strategy IN (?,?)',
                    (account_id, symbol, from_strategy, to_strategy)).fetchone()
                if claim is None or claim[0] not in {from_strategy, to_strategy} or D(claim[1]) != ZERO:
                    raise ValueError('ownership transfer requires an explicitly owned flat sleeve')
                if any(p.instrument_id == symbol and p.quantity != ZERO for p in account.positions):
                    raise ValueError('ownership transfer requires flat official inventory')
                if any(o.intent.instrument_id == symbol and o.status not in TERMINAL for o in account.open_orders):
                    raise ValueError('ownership transfer cannot include open broker orders')
            for symbol in symbols:
                predecessor = self.store.db.execute(
                    'SELECT initial_quantity,entry_price FROM portfolio_claims '
                    'WHERE account=? AND symbol=? AND strategy=?',
                    (account_id, symbol, from_strategy)).fetchone()
                if predecessor is None:
                    continue
                self.store.db.execute(
                    'INSERT OR IGNORE INTO portfolio_claim_history VALUES (?,?,?,?,?)',
                    (account_id, symbol, from_strategy, *predecessor))
                self.store.db.execute(
                    'DELETE FROM portfolio_claims WHERE account=? AND symbol=? AND strategy=?',
                    (account_id, symbol, from_strategy))
                original = self.store.db.execute(
                    'SELECT initial_quantity,entry_price FROM portfolio_claim_history '
                    'WHERE account=? AND symbol=? AND strategy=?',
                    (account_id, symbol, to_strategy)).fetchone()
                initial_quantity, entry_price = original if original is not None else ('0', '0')
                self.store.db.execute('INSERT INTO portfolio_claims VALUES (?,?,?,?,?,?)',
                    (account_id, symbol, to_strategy, '0', initial_quantity, entry_price))
                self.store.db.execute(
                    'DELETE FROM portfolio_claim_history WHERE account=? AND symbol=? AND strategy=?',
                    (account_id, symbol, to_strategy))
                self.store.db.execute(
                    'UPDATE capital_symbols SET strategy=? WHERE account=? AND symbol=? AND strategy=?',
                    (to_strategy, account_id, symbol, from_strategy))
            identity = 'flat-ownership:' + sha256(dumps(
                [account_id, from_strategy, to_strategy, sorted(symbols)]).encode()).hexdigest()
            self.store.record_metadata(identity, dict(symbols=sorted(symbols),
                previous=from_strategy, owner=to_strategy))

    def record_corporate_action(self, *, account_id, strategy_id, event_id, symbol,
                                quantity_ratio, basis_ratio, cash_flow, evidence, effective_at=None):
        """Post an evidenced broker transformation, never send it to the broker.

        Caller supplies verified action terms and account implementation evidence.
        The confirmation boundary preserves pre-action fills in their original
        units; subsequent fills are replayed in the resulting share basis.
        """
        terms = dict(id=event_id, symbol=symbol, quantity_ratio=quantity_ratio,
                     basis_ratio=basis_ratio, cash_flow=cash_flow, evidence=evidence)
        if effective_at is not None:
            terms['effective_at'] = effective_at
        self.record_corporate_actions(account_id=account_id, strategy_id=strategy_id,
                                      actions=[terms])

    def record_corporate_actions(self, *, account_id, strategy_id, actions):
        """Post one owner's actions, reconciling against all account claims."""
        with self.store.transaction():
            self._record_corporate_actions(account_id=account_id, strategy_id=strategy_id,
                                          actions=actions, verify=True)

    def record_account_corporate_actions(self, *, account_id, by_strategy):
        """Atomically post one official transformation across all entitled sleeves."""
        with self.store.transaction():
            self._recover_account(account_id)
            for strategy_id, actions in by_strategy.items():
                self._record_corporate_actions(account_id=account_id, strategy_id=strategy_id,
                                              actions=actions, verify=False)
            account = self.adapter.read_account()
            total = self.store.holdings(account_id)
            symbols = {a['symbol'] for actions in by_strategy.values() for a in actions}
            symbols.update(item['symbol'] for actions in by_strategy.values() for action in actions
                           for item in action.get('allocations', ()))
            positions = {p.instrument_id: p.quantity for p in account.positions}
            if any(total.get(symbol, ZERO) != positions.get(symbol, ZERO) for symbol in symbols):
                raise ValueError('corporate action does not reconcile to official shares')

    def _record_corporate_actions(self, *, account_id, strategy_id, actions, verify):
        """Atomically reconcile an ordered group of official transformations."""
        from .contracts import decimal_value, signed_decimal_value
        identities = set()
        for terms in actions:
            if not terms['id'] or not terms['evidence']:
                raise ValueError('official corporate action identity and evidence required')
            if terms['id'] in identities:
                raise ValueError('duplicate corporate action identity')
            identities.add(terms['id'])
            decimal_value(terms['quantity_ratio'], 'corporate share ratio', positive=True)
            decimal_value(terms['basis_ratio'], 'corporate basis ratio')
            signed_decimal_value(terms['cash_flow'], 'corporate cash flow')
            allocations = terms.get('allocations', ())
            if allocations and terms['basis_ratio'] + sum((item['basis_fraction'] for item in allocations), ZERO) != 1:
                raise ValueError('corporate cost allocation must preserve total basis')
            for item in allocations:
                if item['symbol'] == terms['symbol']:
                    raise ValueError('corporate allocation must name a different security')
                decimal_value(item['quantity'], 'allocated shares', positive=True)
                decimal_value(item['basis_fraction'], 'allocated cost fraction')
        pending = []
        for terms in actions:
            prior = self.store.db.execute(
                'SELECT strategy,payload FROM portfolio_corporate_actions WHERE account=? AND strategy=? AND event=?',
                (account_id, strategy_id, terms['id'])).fetchone()
            if prior is None:
                pending.append(dict(terms))
                continue
            saved = decode(json.loads(prior[1]))
            if prior[0] != strategy_id or any(saved[key] != value for key, value in terms.items()):
                raise ValueError('corporate action identity changed')
        if not pending:
            return
        self._recover_account(account_id)
        account = self.adapter.read_account()
        if account.account_id != account_id or not account.reconciled:
            raise ValueError('corporate action requires matching reconciled account')
        children = {item['symbol'] for terms in pending for item in terms.get('allocations', ())}
        symbols = {terms['symbol'] for terms in pending} | children
        saved = [decode(json.loads(payload)) for payload, in self.store.db.execute(
            'SELECT payload FROM portfolio_corporate_actions WHERE account=? AND strategy=? ORDER BY rowid',
            (account_id, strategy_id))]
        remaining = saved + pending
        while remaining:
            connected, unrelated = [], []
            for terms in remaining:
                related = {terms['symbol']} | {item['symbol'] for item in terms.get('allocations', ())}
                if related & symbols:
                    symbols.update(related)
                    connected.append(terms)
                else:
                    unrelated.append(terms)
            if not connected:
                break
            remaining = unrelated
        for symbol in children:
            claim = self.store.db.execute(
                'SELECT strategy FROM portfolio_claims WHERE account=? AND symbol=? AND strategy=?',
                (account_id, symbol, strategy_id)).fetchone()
            if claim is None:
                self.store.db.execute('INSERT INTO portfolio_claims VALUES (?,?,?,?,?,?)',
                    (account_id, symbol, strategy_id, '0', '0', '0'))
            elif claim[0] != strategy_id:
                raise ValueError('corporate allocation conflicts with inventory ownership')
        orders = [decode(json.loads(payload)) for payload, in self.store.db.execute(
            'SELECT o.payload FROM portfolio_orders o JOIN portfolio_plans p ON p.id=o.plan '
            'WHERE p.account=? AND p.strategy=?', (account_id, strategy_id))]
        orders = [order for order in orders
                  if order['record'].snapshot.intent.instrument_id in symbols]
        if any(order['uncertain'] or order['record'].snapshot.status not in TERMINAL for order in orders):
            raise ValueError('corporate action requires settled original orders')
        confirmations = [decode(json.loads(payload)) for payload, in self.store.db.execute(
            'SELECT payload FROM portfolio_confirmations WHERE account=? ORDER BY rowid', (account_id,))]
        quantities = {}
        initial = {}
        for symbol in symbols:
            claim = self.store.db.execute(
                'SELECT quantity,initial_quantity FROM portfolio_claims WHERE account=? AND strategy=? AND symbol=?',
                (account_id, strategy_id, symbol)).fetchone()
            if claim is None:
                raise ValueError('corporate action requires explicit inventory ownership')
            quantities[symbol] = D(claim[0])
            initial[symbol] = D(claim[1])
        for terms in pending:
            if 'effective_at' not in terms:
                ids = {order['record'].snapshot.intent.client_order_id for order in orders}
                terms['after_event_id'] = next((event.event_id for event in reversed(confirmations)
                                               if event.client_order_id in ids), None)
        if any('effective_at' in terms for terms in saved + pending):
            from .portfolio_accounting import corporate_action_timeline
            intents = {order['record'].snapshot.intent.client_order_id:
                       order['record'].snapshot.intent for order in orders}
            timeline = corporate_action_timeline(
                [event for event in confirmations if event.client_order_id in intents],
                [terms for terms in saved + pending if terms['symbol'] in symbols])
            quantities = initial
            cumulative, seen = {}, set()
            for item in timeline:
                if isinstance(item, Mapping):
                    quantities[item['symbol']] *= item['quantity_ratio']
                    for allocation in item.get('allocations', ()):
                        quantities[allocation['symbol']] += allocation['quantity']
                elif item.event_id not in seen:
                    seen.add(item.event_id)
                    intent = intents[item.client_order_id]
                    previous = cumulative.get(item.client_order_id, ZERO)
                    delta = item.cumulative_filled_quantity - previous
                    if delta < ZERO:
                        raise ValueError('regressing effective-time cumulative fill')
                    quantities[intent.instrument_id] += delta if intent.side == Side.BUY else -delta
                    cumulative[item.client_order_id] = item.cumulative_filled_quantity
        else:
            for terms in pending:
                quantities[terms['symbol']] *= terms['quantity_ratio']
                for allocation in terms.get('allocations', ()):
                    quantities[allocation['symbol']] += allocation['quantity']
        for symbol, quantity in quantities.items():
            other = self.store.holdings(account_id).get(symbol, ZERO) - self.store.holdings(account_id, strategy_id).get(symbol, ZERO)
            actual = next((p.quantity for p in account.positions if p.instrument_id == symbol), ZERO)
            if verify and actual != quantity + other:
                raise ValueError('corporate action does not reconcile to official shares')
        for terms in pending:
            self.store.db.execute('INSERT INTO portfolio_corporate_actions VALUES (?,?,?,?)',
                                  (account_id, strategy_id, terms['id'], dumps(terms)))
        for symbol, quantity in quantities.items():
            self.store.db.execute('UPDATE portfolio_claims SET quantity=? '
                'WHERE account=? AND strategy=? AND symbol=?',
                (str(quantity), account_id, strategy_id, symbol))
        share_changes = {terms['symbol'] for terms in pending
                         if terms['quantity_ratio'] != 1 or terms.get('allocations')}
        if share_changes:
            for identity, in self.store.db.execute(
                    'SELECT id FROM portfolio_plans WHERE account=? AND strategy=?',
                    (account_id, strategy_id)).fetchall():
                plan = self.store.plan(identity)
                if plan['phase'] != 'COMPLETE' and share_changes & set(plan['request']['owned']):
                    plan['corporate_action_blocks'] = sorted(set(
                        plan.get('corporate_action_blocks', ())) | {terms['id'] for terms in pending})
                    self.store.save_plan(plan)

    def _accept(self, plan, order, event):
        account = plan['request']['account']
        payload = dumps(event)
        seen = self.store.db.execute(
            'SELECT payload FROM portfolio_confirmations WHERE account=? AND event=?',
            (account, event.event_id)).fetchone()
        if seen:
            if seen[0] != payload:
                raise ValueError('confirmation identity changed')
            snapshot = order['record'].snapshot
            if (event.cumulative_filled_quantity == snapshot.cumulative_filled_quantity
                    and event.status == snapshot.status):
                order['uncertain'], order['attempted'] = False, True
                self.store.save_order(order)
            return
        record = order['record']
        # Adapters may return an immediate fill on the first POST/query.
        if record.confirmed_status == OrderStatus.NEW and event.status not in (
                OrderStatus.SUBMITTED, OrderStatus.REJECTED, OrderStatus.UNKNOWN):
            record = replace(record, confirmed_status=OrderStatus.SUBMITTED)
        if event.status == OrderStatus.UNKNOWN:
            order['uncertain'] = True
            self.store.save_order(order)
            return
        updated = advance(record, event)
        delta = event.cumulative_filled_quantity - record.snapshot.cumulative_filled_quantity
        intent = updated.snapshot.intent
        if delta:
            row = self.store.db.execute(
                'SELECT quantity FROM portfolio_claims WHERE account=? AND symbol=? AND strategy=?',
                (account, intent.instrument_id, plan['request']['strategy'])).fetchone()
            quantity = D(row[0]) + (delta if intent.side == Side.BUY else -delta)
            if quantity < 0:
                raise ValueError('broker fill exceeds strategy-owned inventory')
            self.store.db.execute(
                'UPDATE portfolio_claims SET quantity=? WHERE account=? AND symbol=? AND strategy=?',
                (str(quantity), account, intent.instrument_id, plan['request']['strategy']))
        order['filled_notional'] = (event.cumulative_filled_notional
            if event.cumulative_filled_notional is not None else
            order.get('filled_notional', ZERO) + delta * (event.fill_price or ZERO))
        order['record'], order['uncertain'], order['attempted'] = updated, False, True
        self.store.save_order(order)
        self.store.db.execute('INSERT INTO portfolio_confirmations VALUES (?,?,?)',
                              (account, event.event_id, payload))

    def _recover(self, plan):
        for order in self.store.orders(plan['id']):
            if (order['record'].snapshot.status in {OrderStatus.FILLED, OrderStatus.REJECTED}
                    and not order['uncertain']):
                continue
            identity = order['record'].snapshot.intent.client_order_id
            try:
                event = self.adapter.query_order(identity)
            except SubmissionNotAttempted:
                order['attempted'], order['uncertain'] = False, False
                self.store.save_order(order)
                continue
            except OrderAbsent:
                if order['attempted']:
                    order['uncertain'] = True
                    plan['reasons'][order['record'].snapshot.intent.instrument_id] = 'ORDER_ABSENCE_AFTER_SUBMIT'
                    self.store.save_order(order)
                else:
                    order['uncertain'] = False
                    self.store.save_order(order)
                continue
            except (TimeoutError, ConnectionError):
                order['uncertain'] = True
                self.store.save_order(order)
                continue
            self._accept(plan, order, event)

    def _recover_account(self, account_id):
        for identity, in self.store.db.execute(
                'SELECT id FROM portfolio_plans WHERE account=? ORDER BY rowid', (account_id,)).fetchall():
            plan = self.store.plan(identity)
            self._recover(plan)
            self.store.save_plan(plan)

    def _account_uncertain(self, account_id):
        return any(order['uncertain'] for identity, in self.store.db.execute(
            'SELECT id FROM portfolio_plans WHERE account=?', (account_id,)).fetchall()
            for order in self.store.orders(identity))

    def _available_cash(self, account, quotes, request, now, not_before, *, exclude_id=None):
        balances = [a for a in account.assets if a.currency == self.quote_currency]
        if len(balances) != 1:
            raise ValueError('one explicit settlement balance required')
        balance = balances[0]
        broker_ids, committed = set(), ZERO
        for order in account.open_orders:
            intent = order.intent
            if order.status in TERMINAL or intent.client_order_id == exclude_id:
                continue
            broker_ids.add(intent.client_order_id)
            if intent.side != Side.BUY:
                continue
            price = intent.limit_price
            if price is None:
                quote = quotes.get(intent.instrument_id)
                if self._quote_reason(intent.instrument_id, quote, request, now, not_before):
                    return ZERO, 'UNPRICED_FOREIGN_BUY_RESERVATION'
                price = quote.ask
            committed += (intent.quantity - order.cumulative_filled_quantity) * price
        for (payload,) in self.store.db.execute(
                'SELECT o.payload FROM portfolio_orders o JOIN portfolio_plans p ON o.plan=p.id WHERE p.account=?',
                (request['account'],)):
            snapshot = decode(json.loads(payload))['record'].snapshot
            intent = snapshot.intent
            if (snapshot.status not in TERMINAL and intent.side == Side.BUY
                    and intent.client_order_id not in broker_ids and intent.client_order_id != exclude_id):
                saved = decode(json.loads(payload))
                committed += ((intent.quantity - snapshot.cumulative_filled_quantity)
                              * saved.get('reservation_price', intent.limit_price))
        fee_row = self.store.db.execute('SELECT fee_rate FROM capital_accounts WHERE account=?',
                                        (request['account'],)).fetchone()
        fee_factor = 1 + (ZERO if fee_row is None else D(fee_row[0]))
        cash = max(ZERO, balance.total - max(balance.reserved, committed * fee_factor))
        if self.store.allocation(request['account'], request['strategy']) is not None:
            try:
                funding = self._funding_adjustment(account, request['strategy'])
            except ValueError as error:
                return ZERO, str(error)
            cash = min(cash, max(ZERO, self.store.cash(request['account'], request['strategy'],
                                                     exclude_id=exclude_id) + funding))
        cash /= fee_factor
        return cash, ''

    def _quote_reason(self, symbol, quote, request, now, not_before):
        if quote is None:
            return 'MISSING_QUOTE'
        if quote.instrument_id != symbol:
            raise ValueError('cross-instrument execution quote')
        if not quote.valid:
            return quote.reason or 'INVALID_QUOTE'
        if quote.session != request['session']:
            return 'WRONG_SESSION'
        if quote.decision_window is not None:
            start, end = quote.decision_window
            if not all(start <= value < end for value in (quote.event_at, quote.received_at, now)):
                return 'OUTSIDE_DECISION_WINDOW'
        if (not_before is not None and quote.event_at <= not_before):
            return 'QUOTE_PRECEDES_DECISION_INPUT'
        if (quote.event_at > quote.received_at or quote.received_at > now
                or now - quote.event_at > self.max_quote_age
                or now - quote.received_at > self.max_quote_age):
            return 'STALE_QUOTE'
        return ''

    def _prepare(self, plan, account, equity, quotes, rules, now, not_before):
        request = plan['request']
        if account.account_id != request['account'] or not account.reconciled:
            raise ValueError('matching reconciled official account required')
        decimal_value(equity, 'official equity', positive=True)
        allocation = self.store.allocation(request['account'], request['strategy'])
        if plan['equity'] is None:
            if allocation is not None:
                projected = self.projection(account, request['strategy'])
                plan['equity'] = (self.store.cash(request['account'], request['strategy'], reserve=False)
                                  + sum((p.quantity * p.mark_price for p in projected.positions), ZERO))
            else:
                plan['equity'] = equity
            capital_key = 'portfolio-initial-capital:' + request['account']
            if self.store.event_payload(capital_key) is None:
                self.store.record_metadata(capital_key, request['initial_capital'] or equity)
        owned = {symbol: D(quantity) for symbol, quantity in self.store.db.execute(
            'SELECT symbol,quantity FROM portfolio_claims WHERE account=? AND strategy=?',
            (request['account'], request['strategy']))}
        positions = {p.instrument_id: p for p in account.positions}
        total_owned = self.store.holdings(request['account'])
        orders = self.store.orders(plan['id'])
        if request.get('quantity_deltas') is not None:
            executed = {}
            for order in orders:
                snapshot = order['record'].snapshot
                sign = 1 if snapshot.intent.side == Side.BUY else -1
                symbol = snapshot.intent.instrument_id
                executed[symbol] = executed.get(symbol, ZERO) + sign * snapshot.cumulative_filled_quantity
            for symbol in request['owned']:
                remaining = request['quantity_deltas'].get(symbol, ZERO) - executed.get(symbol, ZERO)
                plan['targets'][symbol] = owned[symbol] + remaining
                if plan['targets'][symbol] < ZERO:
                    raise ValueError('strategy order exceeds recovered owned inventory')
        if any(o['record'].snapshot.status not in TERMINAL or o['uncertain'] for o in orders):
            return []
        foreign_orders = [o for o in account.open_orders if o.status not in TERMINAL
                          and o.intent.client_order_id not in {
                              row['record'].snapshot.intent.client_order_id for row in orders}]
        eligible, prices, steps = set(), {}, {}
        for symbol in request['owned']:
            position = positions.get(symbol)
            if position is not None and position.quote_currency != self.quote_currency:
                raise ValueError('position quote currency mismatch')
            actual = position.quantity if position else ZERO
            if actual != total_owned[symbol]:
                plan['reasons'][symbol] = 'POSITION_OWNERSHIP_MISMATCH'
                continue
            if any(o.intent.instrument_id == symbol for o in foreign_orders):
                plan['reasons'][symbol] = 'FOREIGN_ORDER_CONFLICT'
                continue
            explicit = request.get('quantity_deltas') is not None
            if explicit and owned[symbol] == plan['targets'][symbol]:
                continue
            if not explicit and owned[symbol] == 0 and request['weights'].get(symbol, ZERO) == 0:
                plan['targets'][symbol] = ZERO
                continue
            reason = self._quote_reason(symbol, quotes.get(symbol), request, now, not_before)
            rule = rules.get(symbol)
            if reason or rule is None or not rule.tradable:
                plan['reasons'][symbol] = reason or ('MISSING_RULES' if rule is None else 'NOT_TRADABLE')
                continue
            eligible.add(symbol)
            # Midpoint fixes the target valuation; ask prices fund actual buys.
            prices[symbol] = (quotes[symbol].bid + quotes[symbol].ask) / 2
            steps[symbol] = rule.quantity_step
        # Reuse the shared planner for NAV sizing, without cash clipping the ideal.
        planning = replace(account, open_orders=(), assets=tuple(
            replace(a, total=plan['equity'], reserved=ZERO)
            if a.currency == self.quote_currency else a for a in account.assets),
            positions=tuple(replace(p, quantity=owned[p.instrument_id])
                            for p in account.positions if p.instrument_id in eligible))
        legs = [] if request.get('quantity_deltas') is not None else plan_targets(account=planning, equity=plan['equity'],
            weights={s: request['weights'].get(s, ZERO) for s in eligible},
            prices=prices, steps=steps, owned_symbols=eligible,
            economic_multipliers=request['multipliers'], quote_currency=self.quote_currency)
        # The legacy planner returns reductions first. Populate targets in both
        # phases from the same primitive without inventing a separate sizing rule.
        for leg in legs:
            plan['targets'].setdefault(leg.symbol, leg.target)
        if any(s not in plan['targets'] for s in eligible):
            flat = replace(planning, positions=())
            for leg in plan_targets(account=flat, equity=plan['equity'],
                    weights={s: request['weights'].get(s, ZERO) for s in eligible},
                    prices=prices, steps=steps, owned_symbols=eligible,
                    quote_currency=self.quote_currency):
                plan['targets'].setdefault(leg.symbol, leg.target)
        attempted = {o['record'].snapshot.intent.instrument_id: o for o in orders}
        blocked_reductions = [s for s in request['owned']
            if s not in attempted and s not in eligible and owned[s] > ZERO
            and (s not in plan['targets'] or owned[s] > plan['targets'][s])]
        reductions = [s for s in sorted(eligible) if owned[s] > plan['targets'][s]
                      and s not in attempted]
        if blocked_reductions and not reductions:
            plan['phase'] = 'BLOCKED'
            return []
        phase = 'REDUCE' if reductions else 'INCREASE'
        plan['phase'] = phase
        cash, cash_reason = self._available_cash(account, quotes, request, now, not_before)
        if cash_reason and phase == 'INCREASE':
            for symbol in eligible:
                plan['reasons'][symbol] = cash_reason
            return []
        candidates = reductions if reductions else sorted(eligible)
        prepared = []
        for symbol in candidates:
            if symbol in attempted:
                order = attempted[symbol]
                status = order['record'].snapshot.status
                if owned[symbol] != plan['targets'][symbol]:
                    plan['reasons'][symbol] = f'ORDER_{status.value}_RESIDUAL'
                continue
            target, current = plan['targets'][symbol], owned[symbol]
            delta = target - current
            if not delta:
                continue
            side = Side.SELL if delta < 0 else Side.BUY
            if phase == 'INCREASE' and side == Side.SELL:
                continue
            reference = quotes[symbol].bid if side == Side.SELL else quotes[symbol].ask
            tolerance = rules[symbol].limit_price_tolerance
            price = reference * (D(1) - tolerance if side == Side.SELL else D(1) + tolerance)
            tick = rules[symbol].price_tick
            if tick is not None:
                rounding = ROUND_FLOOR if side == Side.SELL else ROUND_CEILING
                price = (price / tick).to_integral_value(rounding=rounding) * tick
            step = rules[symbol].quantity_step
            quantity = (abs(delta) // step) * step
            if side == Side.BUY:
                quantity = min(quantity, (cash / price // rules[symbol].quantity_step)
                               * rules[symbol].quantity_step)
                if quantity < delta:
                    plan['reasons'][symbol] = 'INSUFFICIENT_CASH'
            if quantity == 0 or quantity * price < rules[symbol].minimum_notional:
                plan['reasons'].setdefault(symbol, 'BELOW_MINIMUM_NOTIONAL')
                continue
            client_id = 'flp-' + sha256(dumps([plan['id'], symbol, side.value]).encode()).hexdigest()[:40]
            intent = OrderIntent(account.market, symbol, side, OrderType.LIMIT,
                quantity, price, client_id, time_in_force=rules[symbol].time_in_force)
            order = dict(record=OrderRecord(OrderSnapshot(intent), now, price),
                         reference_price=reference, reservation_price=price,
                         attempted=False, uncertain=False)
            self.store.db.execute('INSERT INTO portfolio_orders VALUES (?,?,?)',
                                  (client_id, plan['id'], dumps(order)))
            prepared.append(client_id)
            if side == Side.BUY:
                cash -= quantity * price
        if not prepared:
            resolved = all(
                symbol in attempted or (
                    symbol in plan['targets'] and owned[symbol] == plan['targets'][symbol])
                for symbol in request['owned'])
            plan['phase'] = 'COMPLETE' if resolved else 'BLOCKED'
        return prepared

    def run_once(self, identity, *, equity, quotes, rules, now, not_before=None,
                 refresh_quotes=None):
        """Reconcile original identities, prepare one phase, then submit its outbox.

        equity is the official NAV observed with read_account, never buying power.
        Missing quotes block only dependent symbols. There is no cancellation of
        foreign orders or automatic replacement of partially filled terminal orders.
        """
        utc(now)
        if not_before is not None:
            utc(not_before)
        with self.store.transaction():
            plan = self.store.plan(identity)
            if plan is None:
                raise KeyError(identity)
            complete = plan['phase'] == 'COMPLETE' or plan.get('abandoned', False)
            if not complete:
                plan['reasons'] = {}
                self.store.save_plan(plan)
            self._recover_account(plan['request']['account'])
            plan = self.store.plan(identity)
            if complete:
                return RebalanceResult(identity, 'COMPLETE', dict(plan['reasons']),
                    dict(plan['targets']), tuple(o['record'].snapshot.intent.client_order_id
                                                for o in self.store.orders(identity)))
            if plan.get('corporate_action_blocks'):
                plan['phase'] = 'WAITING'
                plan['reasons'] = {'corporate_action': 'FROZEN_INTENT_REQUIRES_ACTION_RECONCILIATION'}
                self.store.save_plan(plan)
                return RebalanceResult(identity, plan['phase'], dict(plan['reasons']),
                    dict(plan['targets']), tuple(o['record'].snapshot.intent.client_order_id
                                                for o in self.store.orders(identity)))
            if self._capital_admission(plan['request']['account'], plan['request']['strategy'],
                                       allow_paused=True):
                plan['phase'] = 'WAITING'
                plan['reasons']['capital'] = 'STRATEGY_PAUSED'
                self.store.save_plan(plan)
                return RebalanceResult(identity, plan['phase'], dict(plan['reasons']),
                    dict(plan['targets']), tuple(o['record'].snapshot.intent.client_order_id
                                                for o in self.store.orders(identity)))
            account = self.adapter.read_account()
            if self._account_uncertain(plan['request']['account']):
                plan['phase'] = 'WAITING'
                plan['reasons']['account'] = 'UNRESOLVED_ORDER_OUTCOME'
            elif 'direct_orders' in plan['request']:
                if account.account_id != plan['request']['account'] or not account.reconciled:
                    raise ValueError('matching reconciled official account required')
                plan['phase'] = 'COMPLETE' if all(
                    o['record'].snapshot.status in TERMINAL and not o['uncertain']
                    for o in self.store.orders(identity)) else 'WAITING'
            else:
                self._prepare(plan, account, equity, quotes, rules, now, not_before)
            self.capital_policy.validate_orders(account, plan['request']['strategy'])
            self.store.save_plan(plan)
        # Commit SUBMITTING before POST: a crash or an eventually-consistent 404
        # can never turn an attempted order back into an unattempted outbox entry.
        identities = [o['record'].snapshot.intent.client_order_id for o in self.store.orders(identity)]
        for client_id in identities:
            send = False
            with self.store.transaction():
                plan = self.store.plan(identity)
                self._capital_admission(plan['request']['account'], plan['request']['strategy'])
                if plan.get('corporate_action_blocks') or self._account_uncertain(plan['request']['account']):
                    break
                order = next(o for o in self.store.orders(identity)
                             if o['record'].snapshot.intent.client_order_id == client_id)
                if order['attempted'] or order['uncertain']:
                    continue
                intent = order['record'].snapshot.intent
                try:
                    event = self.adapter.query_order(client_id)
                except OrderAbsent:
                    account = self.adapter.read_account()
                    request = plan['request']
                    reason = self._quote_reason(intent.instrument_id,
                        quotes.get(intent.instrument_id), request, now, not_before)
                    if reason:
                        plan['reasons'][intent.instrument_id] = reason
                        self.store.save_plan(plan)
                        continue
                    actual = next((p.quantity for p in account.positions
                                   if p.instrument_id == intent.instrument_id), ZERO)
                    owned = D(self.store.db.execute(
                        'SELECT quantity FROM portfolio_claims WHERE account=? AND symbol=? AND strategy=?',
                        (request['account'], intent.instrument_id, request['strategy'])).fetchone()[0])
                    if (not account.reconciled or account.account_id != request['account']
                            or actual != self.store.holdings(request['account']).get(intent.instrument_id, ZERO)):
                        plan['reasons'][intent.instrument_id] = 'ACCOUNT_RECONCILIATION_REQUIRED'
                        self.store.save_plan(plan)
                        continue
                    if intent.side == Side.SELL and intent.quantity > owned:
                        raise ValueError('sell exceeds owned inventory')
                    if any(o.status not in TERMINAL and o.intent.instrument_id == intent.instrument_id
                           and o.intent.client_order_id != client_id
                           for o in account.open_orders):
                        plan['reasons'][intent.instrument_id] = 'FOREIGN_ORDER_CONFLICT'
                        self.store.save_plan(plan)
                        continue
                    if intent.side == Side.BUY:
                        balance = next(a for a in account.assets if a.currency == self.quote_currency)
                        # Every local outbox has already reserved its notional.
                        # Official available cash must still fund this child order.
                        if balance.available < intent.quantity * order.get('reservation_price', intent.limit_price):
                            plan['reasons'][intent.instrument_id] = 'INSUFFICIENT_CASH'
                            self.store.save_plan(plan)
                            continue
                    order['attempted'] = True
                    self.store.save_order(order)
                    send = True
                except (TimeoutError, ConnectionError):
                    order['uncertain'] = True
                    self.store.save_order(order)
                else:
                    self._accept(plan, order, event)
            if send:
                # Serialize the account side effect, but retain the committed
                # submission marker if the process dies inside the network call.
                with self.store.transaction():
                    plan = self.store.plan(identity)
                    order = next(o for o in self.store.orders(identity)
                                 if o['record'].snapshot.intent.client_order_id == client_id)
                    if self._account_uncertain(plan['request']['account']):
                        break
                    if order['record'].snapshot.status in TERMINAL or order['uncertain']:
                        continue
                    try:
                        recovered = self.adapter.query_order(client_id)
                    except OrderAbsent:
                        account = self.adapter.read_account()
                        request = plan['request']
                        try:
                            self._capital_admission(request['account'], request['strategy'])
                            self.capital_policy.validate_orders(account, request['strategy'], include_id=client_id)
                        except ValueError as error:
                            order['attempted'] = False
                            plan['reasons'][intent.instrument_id] = str(error)
                            self.store.save_order(order)
                            self.store.save_plan(plan)
                            continue
                        actual = next((p.quantity for p in account.positions
                                       if p.instrument_id == intent.instrument_id), ZERO)
                        owned = D(self.store.db.execute(
                            'SELECT quantity FROM portfolio_claims WHERE account=? AND symbol=? AND strategy=?',
                            (request['account'], intent.instrument_id, request['strategy'])).fetchone()[0])
                        if refresh_quotes is not None:
                            quotes = refresh_quotes()
                        submission_now = self.clock()
                        utc(submission_now)
                        reason = self._quote_reason(intent.instrument_id, quotes.get(intent.instrument_id),
                                                    request, submission_now, not_before)
                        rule = rules.get(intent.instrument_id)
                        if rule is None or not rule.tradable:
                            reason = 'MISSING_RULES' if rule is None else 'NOT_TRADABLE'
                        elif (intent.quantity % rule.quantity_step != ZERO
                              or intent.quantity * order.get('reservation_price', intent.limit_price) < rule.minimum_notional
                              or intent.time_in_force != rule.time_in_force
                              or (intent.limit_price is not None and rule.price_tick is not None
                                  and intent.limit_price % rule.price_tick != ZERO)):
                            reason = 'ORDER_RULES_CHANGED'
                        if (not account.reconciled or account.account_id != request['account']
                                or actual != self.store.holdings(request['account']).get(intent.instrument_id, ZERO)):
                            reason = 'ACCOUNT_RECONCILIATION_REQUIRED'
                        elif any(o.status not in TERMINAL and o.intent.instrument_id == intent.instrument_id
                                 for o in account.open_orders):
                            reason = 'FOREIGN_ORDER_CONFLICT'
                        elif intent.side == Side.BUY and not reason:
                            cash, reason = self._available_cash(account, quotes, request, submission_now,
                                                                not_before, exclude_id=client_id)
                            if cash < intent.quantity * order.get('reservation_price', intent.limit_price):
                                reason = reason or 'INSUFFICIENT_CASH'
                        if reason:
                            # Admission failed before POST, so this is still an
                            # unattempted intent, not an uncertain broker result.
                            order['attempted'] = False
                            plan['reasons'][intent.instrument_id] = reason
                            self.store.save_order(order)
                            self.store.save_plan(plan)
                            continue
                    except (TimeoutError, ConnectionError):
                        order['uncertain'] = True
                        self.store.save_order(order)
                        continue
                    else:
                        self._accept(plan, order, recovered)
                        continue
                    try:
                        event = self.adapter.submit_order(intent)
                    except (TimeoutError, ConnectionError):
                        order['uncertain'] = True
                        self.store.save_order(order)
                    else:
                        self._accept(plan, order, event)
        with self.store.transaction():
            plan = self.store.plan(identity)
            orders = self.store.orders(identity)
            pending = any(o['record'].snapshot.status not in TERMINAL or o['uncertain'] for o in orders)
            if pending:
                plan['phase'] = 'WAITING'
            self.store.save_plan(plan)
            return RebalanceResult(identity, plan['phase'], dict(plan['reasons']),
                dict(plan['targets']), tuple(o['record'].snapshot.intent.client_order_id for o in orders))

    def abandon(self, identity):
        """Stop a rebalance without releasing ownership or erasing its fills."""
        for order in self.store.orders(identity):
            if order['record'].snapshot.status not in TERMINAL or order['uncertain']:
                self.cancel(identity, order['record'].snapshot.intent.client_order_id)
        with self.store.transaction():
            plan = self.store.plan(identity)
            self._recover_account(plan['request']['account'])
            if any(o['record'].snapshot.status not in TERMINAL or o['uncertain']
                   for o in self.store.orders(identity)):
                raise ValueError('cannot abandon unresolved orders')
            plan = self.store.plan(identity)
            plan['abandoned'] = True
            plan['phase'] = 'COMPLETE'
            plan['reasons']['plan'] = 'ABANDONED_REBALANCE'
            self.store.save_plan(plan)

    def cancel(self, identity, client_order_id):
        """Cancel only this plan's child; query original identity before any retry.

        No replacement order is generated. A later CANCELED→FILLED confirmation
        is absorbed on recovery before any subsequent allocation decision.
        """
        with self.store.transaction():
            plan = self.store.plan(identity)
            order = next(o for o in self.store.orders(identity)
                         if o['record'].snapshot.intent.client_order_id == client_order_id)
            try:
                event = self.adapter.query_order(client_order_id)
            except SubmissionNotAttempted:
                event = OrderEvent(client_order_id, None, OrderStatus.CANCELED, ZERO, None,
                    self.clock(), 'local-unsubmitted-cancellation',
                    'local-unsubmitted-cancellation:' + client_order_id)
            self._accept(plan, order, event)
            if order['record'].snapshot.status in TERMINAL or order['uncertain']:
                return
            order['uncertain'] = True
            self.store.save_order(order)
        with self.store.transaction():
            plan = self.store.plan(identity)
            order = next(o for o in self.store.orders(identity)
                         if o['record'].snapshot.intent.client_order_id == client_order_id)
            try:
                event = self.adapter.cancel_order(client_order_id)
            except (TimeoutError, ConnectionError):
                return
            self._accept(plan, order, event)

    def report(self, identity, *, official_equity, adjustments=(), readonly=False, account=None):
        """Persist a strategy-lifetime report against current official holdings.

        Official mark prices value inventory; execution quotes are not substituted
        for missing broker holdings. Research adjustments never fund orders.
        """
        from .portfolio_accounting import OwnedPosition, build_portfolio_report

        context = nullcontext() if readonly and self.store.db.in_transaction else self.store.transaction()
        with context:
            plan = self.store.plan(identity)
            if not readonly:
                self._recover_account(plan['request']['account'])
                plan = self.store.plan(identity)
            if account is None:
                account = self.adapter.read_account()
            request = plan['request']
            if account.account_id != request['account'] or not account.reconciled:
                raise ValueError('matching reconciled official account required')
            starting = {symbol: OwnedPosition(D(quantity), D(quantity) * D(basis))
                for symbol, quantity, basis in self.store.db.execute(
                    'SELECT symbol,initial_quantity,entry_price FROM portfolio_claims WHERE account=? AND strategy=? '
                    'UNION ALL SELECT symbol,initial_quantity,entry_price FROM portfolio_claim_history '
                    'WHERE account=? AND strategy=?',
                    (request['account'], request['strategy'], request['account'], request['strategy']))}
            orders = [decode(json.loads(payload)) for (payload,) in self.store.db.execute(
                'SELECT o.payload FROM portfolio_orders o JOIN portfolio_plans p ON p.id=o.plan '
                'WHERE p.account=? AND p.strategy=? ORDER BY o.rowid',
                (request['account'], request['strategy']))]
            intents = [o['record'].snapshot.intent for o in orders]
            ids = {intent.client_order_id for intent in intents}
            events = [decode(json.loads(payload)) for (payload,) in self.store.db.execute(
                'SELECT payload FROM portfolio_confirmations WHERE account=? ORDER BY rowid',
                (request['account'],))]
            allocation = self.store.allocation(request['account'], request['strategy'])
            capital = allocation['capital'] if allocation is not None else decode(json.loads(
                self.store.event_payload('portfolio-initial-capital:' + request['account'])))
            if allocation is not None:
                total = self.store.holdings(request['account'])
                if any(p.quantity != total.get(p.instrument_id, ZERO) for p in account.positions):
                    raise ValueError('aggregate inventory does not reconcile to official shares')
                account = self.projection(account, request['strategy'])
                official_equity = (self.store.cash(request['account'], request['strategy'], reserve=False)
                                   + sum((p.quantity * p.mark_price for p in account.positions), ZERO))
                capital = allocation['capital']
            result = build_portfolio_report(starting_inventory=starting, intents=intents,
                events=[event for event in events if event.client_order_id in ids],
                reference_prices={o['record'].snapshot.intent.client_order_id: o['reference_price'] for o in orders},
                account=account, prices={p.instrument_id: p.mark_price for p in account.positions},
                official_equity=official_equity, target_weights=request['weights'],
                adjustments=adjustments, economic_multipliers=request['multipliers'], initial_capital=capital,
                quote_currency=self.quote_currency, corporate_actions=[decode(json.loads(payload)) for payload, in self.store.db.execute(
                    'SELECT payload FROM portfolio_corporate_actions WHERE account=? AND strategy=? ORDER BY rowid',
                    (request['account'], request['strategy']))])
            if not readonly:
                plan['report'] = asdict(result)
                self.store.save_plan(plan)
            return result
