"""Account risk projections over a single official observation and the order outbox."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import logging
from time import perf_counter_ns, thread_time_ns
from uuid import uuid4

from factorlab._process_memory import peak_memory_bytes

from .contracts import Market, OrderSnapshot, Side, TERMINAL, ZERO, decimal_value
from .control import inventory_effect
from .storage import decode, dumps


log = logging.getLogger(__name__)
ONE = Decimal(1)
EMERGENCY_LIMITS = {
    'PORTFOLIO_GROSS_EXPOSURE_LIMIT': ('max_gross_exposure', 'projected_gross_exposure'),
    'PORTFOLIO_MARGIN_LIMIT': ('max_margin', 'projected_initial_margin'),
    'PORTFOLIO_DRAWDOWN_LIMIT': ('max_drawdown', 'drawdown'),
    'PORTFOLIO_DAILY_LOSS_LIMIT': ('max_daily_loss', 'daily_loss'),
    'PORTFOLIO_ROLLING_LOSS_LIMIT': ('max_rolling_loss', 'rolling_loss'),
}


@dataclass(frozen=True)
class PortfolioRiskLimits:
    """Opt-in ceilings in settlement currency unless the field is a ratio."""

    max_gross_exposure: Decimal | None = None
    max_symbol_exposure: Decimal | None = None
    max_strategy_exposure: Decimal | None = None
    max_symbol_concentration: Decimal | None = None
    max_margin: Decimal | None = None
    min_available_cash: Decimal | None = None
    max_drawdown: Decimal | None = None
    max_daily_loss: Decimal | None = None
    max_rolling_loss: Decimal | None = None
    max_order_notional: Decimal | None = None
    max_account_age_seconds: int | None = None
    rolling_loss_hours: int = 168
    stop_new_risk: bool = False
    paused_strategies: tuple[str, ...] = ()
    paused_symbols: tuple[str, ...] = ()
    cancel_open_on_breach: bool = False
    emergency_reduction_reasons: tuple[str, ...] = ()

    def __post_init__(self):
        for name, value in vars(self).items():
            if value is not None and name.startswith(('min_', 'max_')) and name != 'max_account_age_seconds':
                decimal_value(value, name)
        if any(reason not in EMERGENCY_LIMITS for reason in self.emergency_reduction_reasons):
            raise ValueError('emergency reduction requires a known limit reason')
        for name in ('max_drawdown', 'max_symbol_concentration'):
            value = getattr(self, name)
            if value is not None and value > ONE:
                raise ValueError(f'{name} is a fraction between 0 and 1')
        if self.max_account_age_seconds is not None and (
                type(self.max_account_age_seconds) is not int or self.max_account_age_seconds <= 0):
            raise ValueError('max_account_age_seconds must be positive')
        if type(self.rolling_loss_hours) is not int or not 1 <= self.rolling_loss_hours <= 744:
            raise ValueError('rolling_loss_hours must be between 1 and 744')


@dataclass(frozen=True)
class RiskContribution:
    market_value: Decimal | None
    gross_exposure: Decimal | None
    net_exposure: Decimal | None
    pending_open_exposure: Decimal
    pending_cancel_exposure: Decimal

    @property
    def projected_gross_exposure(self):
        if self.gross_exposure is not None:
            return self.gross_exposure + self.pending_open_exposure
        return None


@dataclass(frozen=True)
class PortfolioRiskSnapshot:
    account_id: str
    currency: str
    observed_at: datetime
    reconciled: bool
    official_equity: Decimal | None
    cash: Decimal | None
    available_cash: Decimal | None
    projected_available_cash: Decimal | None
    initial_margin: Decimal | None
    projected_initial_margin: Decimal | None
    market_value: Decimal | None
    gross_exposure: Decimal | None
    net_exposure: Decimal | None
    pending_open_notional: Decimal
    pending_open_exposure: Decimal
    pending_cancel_exposure: Decimal
    projected_gross_exposure: Decimal | None
    symbol_exposure: dict[str, Decimal]
    symbol_concentration: dict[str, Decimal]
    strategies: dict[str, RiskContribution]
    logical_strategies: dict[str, RiskContribution]
    strategy_bindings: dict[str, str]
    orders: dict[str, OrderRisk]
    drawdown: Decimal | None
    daily_loss: Decimal | None
    rolling_loss: Decimal | None
    missing: tuple[str, ...]
    capital: object = None
    realized_pnl: Decimal | None = None
    unrealized_pnl: Decimal | None = None
    funding: Decimal | None = None

    @property
    def valid(self):
        return self.reconciled and not set(self.missing).intersection({
            'account_age', 'positions', 'ownership', 'orders', 'order_price',
            'order_ownership', 'economic_multiplier', 'pending_capital'})


@dataclass(frozen=True)
class OrderRisk:
    client_id: str
    owner: str | None
    symbol: str
    order_notional: Decimal | None
    remaining_notional: Decimal | None
    economic_exposure: Decimal | None
    effect: str
    reflected: bool
    cancel_pending: bool
    uncertain: bool


class PortfolioRiskRejected(ValueError):
    pass


def reduce_portfolio_risk_event(db, event):
    """Apply original account controls without observing prices or inferring risk."""
    if event['kind'] == 'PORTFOLIO_RISK_NAV_RECORDED':
        account = event['account_id']
        if event['reset_hours']:
            db.execute('DELETE FROM portfolio_risk_hour WHERE account=?', (account,))
        db.execute('INSERT INTO portfolio_risk_nav VALUES (?,?) '
                   'ON CONFLICT(account) DO UPDATE SET payload=excluded.payload',
                   (account, event['point']))
        if event['hour'] is not None:
            db.execute('INSERT INTO portfolio_risk_hour VALUES (?,?,?,?)',
                       (account, event['hour'], event['observed_at'], event['nav']))
            db.execute('DELETE FROM portfolio_risk_hour WHERE account=? AND hour<?',
                       (account, event['hour'] - 745))
        return
    if event['kind'] == 'PORTFOLIO_RISK_MULTIPLIERS_RECORDED':
        db.execute('INSERT INTO portfolio_risk_multipliers VALUES (?,?,?) '
                   'ON CONFLICT(account,strategy) DO UPDATE SET payload=excluded.payload',
                   (event['account_id'], event['owner'], event['payload']))
        return
    if event['kind'] != 'PORTFOLIO_RISK_CONFIGURED':
        raise ValueError('unsupported portfolio risk event')
    account = event['account_id']
    db.execute('INSERT INTO portfolio_risk_config VALUES (?,?) '
               'ON CONFLICT(account) DO UPDATE SET payload=excluded.payload',
               (account, event['configuration']))
    for owner, payload in event['owner_multipliers'].items():
        db.execute('INSERT INTO portfolio_risk_multipliers VALUES (?,?,?) '
                   'ON CONFLICT(account,strategy) DO UPDATE SET payload=excluded.payload',
                   (account, owner, payload))


class PortfolioRisk:
    """Persist controls and bounded NAV observations alongside shared reservations."""

    def __init__(self, store):
        self.store = store
        self.db = store.db
        self.last_calculation = None
        self.pending_orders = None
        self.pending_commitments = None
        self.account_observation = None

    def configure(self, account_id, limits, multipliers, *, event_writer=None, event_id=None):
        """Admit controls under the account lock, outside any caller write transaction."""
        if event_writer is not None:
            event_writer.resume()
        with self.store.transaction():
            self.store.capital_policy.mode(account_id)
            current = self.config(account_id)
            if multipliers is None:
                multipliers = current[1] if current is not None else {}
            for multiplier in multipliers.values():
                decimal_value(multiplier, 'economic multiplier', positive=True)
            owner_values = {}
            if current is None:
                for owner, payload in self.db.execute(
                        'SELECT strategy,payload FROM portfolio_plans WHERE account=? ORDER BY rowid',
                        (account_id,)):
                    values = decode(json.loads(payload))['request']['multipliers']
                    if values:
                        if owner not in owner_values:
                            saved = self.db.execute(
                                'SELECT payload FROM portfolio_risk_multipliers WHERE account=? AND strategy=?',
                                (account_id, owner)).fetchone()
                            owner_values[owner] = {} if saved is None else decode(json.loads(saved[0]))
                        owner_values[owner].update(values)
            configuration = dumps(dict(limits=asdict(limits), multipliers=multipliers))
            if current is not None and configuration == dumps(dict(limits=asdict(current[0]), multipliers=current[1])):
                return
            event = dict(event_id=event_id or str(uuid4()), targets=['portfolio'],
                         kind='PORTFOLIO_RISK_CONFIGURED', account_id=account_id,
                         configuration=configuration,
                         owner_multipliers={owner: dumps(values) for owner, values in owner_values.items()})
            if event_writer is None:
                self.store.capital_policy._require_unreplicated_policy_write()
                reduce_portfolio_risk_event(self.db, event)
                return
        event_writer.submit(event, committed_at=datetime.now(timezone.utc).isoformat())

    def config(self, account_id):
        row = self.db.execute('SELECT payload FROM portfolio_risk_config WHERE account=?',
                              (account_id,)).fetchone()
        if row is not None:
            config = decode(json.loads(row[0]))
            limits = config['limits']
            for name in ('paused_strategies', 'paused_symbols', 'emergency_reduction_reasons'):
                limits[name] = tuple(limits[name])
            return PortfolioRiskLimits(**limits), config['multipliers']
        return None

    def _nav_history(self, account, equity, observed_at, limits, healthy, *,
                     event_writer=None, event_id=None):
        if event_writer is not None:
            event_writer.resume()
        result, event = self._prepare_nav_history(account, equity, observed_at, limits, healthy,
                                                  event_id=event_id)
        if event is not None:
            if event_writer is None:
                self.store.capital_policy._require_unreplicated_policy_write()
                reduce_portfolio_risk_event(self.db, event)
            else:
                event_writer.submit(event, committed_at=datetime.now(timezone.utc).isoformat())
        return result

    def _prepare_nav_history(self, account, equity, observed_at, limits, healthy, *, event_id=None):
        if equity is None or not healthy:
            return (None, None, None), None
        flow = account.cumulative_external_cash_flow
        account_id = account.account_id
        hour = int(observed_at.timestamp()) // 3600
        day = hour // 24
        saved = self.db.execute(
            'SELECT payload FROM portfolio_risk_nav WHERE account=?', (account_id,)).fetchone()
        previous = decode(json.loads(saved[0])) if saved else None
        if previous and observed_at < previous['at']:
            return (None, None, None), None
        if flow is None:
            return (None, None, None), None
        reset_hours = bool(previous and previous.get('initial_flow') is None)
        if reset_hours:
            # Raw and legacy adjusted observations lack the starting external
            # flow. Start a new series with the first observed reference.
            previous = None
        initial_flow = flow if previous is None else previous.get('initial_flow')
        # Keep the first observed equity as the reference and remove
        # subsequent account external cash movements.
        nav = equity if initial_flow is None else equity - (flow - initial_flow)
        peak = nav if previous is None else max(nav, previous['peak'])
        start = nav if previous is None or day != previous['day'] else previous['start']
        point = dict(at=observed_at, peak=peak, start=start, day=day,
                     flow_adjusted=initial_flow is not None, initial_flow=initial_flow)
        event = None
        if point != previous:
            event = dict(event_id=event_id or str(uuid4()),
                         targets=['portfolio'], kind='PORTFOLIO_RISK_NAV_RECORDED',
                         account_id=account_id, point=dumps(point), reset_hours=reset_hours,
                         hour=hour if previous is None or hour != int(previous['at'].timestamp()) // 3600 else None,
                         observed_at=observed_at.isoformat(), nav=str(nav))
        cutoff = observed_at - timedelta(hours=limits.rolling_loss_hours)
        baseline = self.db.execute(
            'SELECT nav FROM portfolio_risk_hour WHERE account=? AND hour BETWEEN ? AND ? '
            'AND observed BETWEEN ? AND ? ORDER BY hour DESC LIMIT 1',
            (account_id, int(cutoff.timestamp()) // 3600 - 1, int(cutoff.timestamp()) // 3600,
             (cutoff - timedelta(hours=1)).isoformat(), cutoff.isoformat())).fetchone()
        drawdown = (peak - nav) / peak if peak > ZERO else None
        rolling = max(ZERO, Decimal(baseline[0]) - nav) if baseline and not reset_hours else None
        return (drawdown, max(ZERO, start - nav), rolling), event

    def record_multipliers(self, account_id, owner, values, *, event_writer=None, event_id=None,
                           _prepared_events=None):
        """Preserve original merged exposure metadata under account ownership."""
        for multiplier in values.values():
            decimal_value(multiplier, 'economic multiplier', positive=True)
        if event_writer is not None:
            event_writer.resume()
        saved = self.db.execute(
            'SELECT payload FROM portfolio_risk_multipliers WHERE account=? AND strategy=?',
            (account_id, owner)).fetchone()
        previous = {} if saved is None else decode(json.loads(saved[0]))
        payload = dumps(previous | values)
        if saved is not None and payload == saved[0]:
            return
        event = dict(event_id=event_id or str(uuid4()), targets=['portfolio'],
                     kind='PORTFOLIO_RISK_MULTIPLIERS_RECORDED', account_id=account_id,
                     owner=owner, payload=payload)
        if event_writer is None:
            if _prepared_events is None:
                self.store.capital_policy._require_unreplicated_policy_write()
            else:
                _prepared_events.append(event)
            reduce_portfolio_risk_event(self.db, event)
            return
        event_writer.submit(event, committed_at=datetime.now(timezone.utc).isoformat())

    @staticmethod
    def emergency_active(limits, snapshot):
        if not snapshot.valid:
            return False
        for reason in limits.emergency_reduction_reasons:
            limit_name, field = EMERGENCY_LIMITS[reason]
            limit, actual = getattr(limits, limit_name), getattr(snapshot, field)
            if actual is not None and limit is not None and actual > limit:
                return True
        return False

    def _multipliers(self, account_id, configured):
        # Use the existing plan's exposure metadata where the operator has not
        # bound the symbol explicitly. Conflicting values make valuation unknown.
        values = defaultdict(set)
        for payload, in self.db.execute(
                'SELECT payload FROM portfolio_risk_multipliers WHERE account=?', (account_id,)):
            for symbol, multiplier in decode(json.loads(payload)).items():
                if symbol not in configured:
                    values[symbol].add(multiplier)
        ambiguous = {symbol for symbol, amounts in values.items() if len(amounts) > 1}
        multipliers = {symbol: max(amounts) for symbol, amounts in values.items()}
        return multipliers | configured, ambiguous

    def observe(self, account, currency, now, config, *, orders=None, equity=None, include_pending=True,
                event_writer=None, event_id=None, _prepared_events=None):
        """Return risk only after original history admission under account ownership.

        Writer-backed observation must run outside a caller SQLite transaction.
        """
        if event_writer is not None:
            event_writer.resume()
        if self.account_observation is not None:
            account = self.account_observation(account)
        started, cpu = perf_counter_ns(), thread_time_ns()
        pending = []

        def prepare_history(*args):
            result, event = self._prepare_nav_history(*args, event_id=event_id)
            if event is not None:
                pending.append(event)
            return result

        try:
            result = self._observe(account, currency, now, config, orders, equity, include_pending,
                                   prepare_history if event_writer is not None or _prepared_events is not None
                                   else self._nav_history)
            for event in pending:
                if _prepared_events is not None:
                    _prepared_events.append(event)
                    reduce_portfolio_risk_event(self.db, event)
                else:
                    event_writer.submit(event, committed_at=datetime.now(timezone.utc).isoformat())
            return result
        finally:
            cpu_ns, decision_ns = thread_time_ns() - cpu, perf_counter_ns() - started
            rss = peak_memory_bytes()
            self.last_calculation = dict(cpu_ns=cpu_ns, decision_ns=decision_ns, process_peak_rss_bytes=rss)
            log.info('portfolio_risk_calculation account=%s cpu_ns=%d decision_ns=%d process_peak_rss_bytes=%s',
                     account.account_id, cpu_ns, decision_ns, rss)

    def _observe(self, account, currency, now, config, orders, equity, include_pending, record_history):
        limits, configured = config
        account_id = account.account_id
        multipliers, ambiguous = self._multipliers(account_id, configured)
        positions = {}
        missing = set()
        observed_at = account.official_observed_at or account.observed_at
        official_positions = account.positions if account.official_positions is None else account.official_positions
        for position in official_positions:
            symbol = position.instrument_id
            if symbol in positions and position != positions[symbol]:
                missing.add('positions')
            if position.quote_currency != currency or not position.mark_price.is_finite() or position.mark_price <= ZERO:
                missing.add('positions')
            positions[symbol] = position

        holdings = defaultdict(dict)
        totals = defaultdict(lambda: ZERO)
        for owner, symbol, quantity in self.db.execute(
                'SELECT strategy,symbol,quantity FROM portfolio_claims WHERE account=?', (account_id,)):
            quantity = Decimal(quantity)
            if quantity:
                holdings[owner][symbol] = quantity
                totals[symbol] += quantity
        confirmed = {symbol: p.quantity for symbol, p in positions.items() if p.quantity}
        unknown_owner = totals != confirmed or any(
            quantity * confirmed.get(symbol, ZERO) <= ZERO
            for inventory in holdings.values() for symbol, quantity in inventory.items())
        if unknown_owner:
            missing.add('ownership')
        if not account.reconciled:
            missing.add('reconciliation')
        if observed_at > now or (limits.max_account_age_seconds is not None
                and now - observed_at > timedelta(seconds=limits.max_account_age_seconds)):
            missing.add('account_age')
        risk_symbols = defaultdict(lambda: ZERO)
        gross = net = market_value = ZERO
        for symbol, position in positions.items():
            if 'positions' in missing:
                break
            nominal = position.quantity * position.mark_price
            economic = nominal * multipliers.get(symbol, ONE)
            market_value += abs(nominal)
            gross += abs(economic)
            net += economic
            risk_symbols[symbol] += abs(economic)

        confirmed_risk = dict(risk_symbols)
        broker = {}
        for snapshot in account.open_orders:
            client_id = snapshot.intent.client_order_id
            if client_id in broker and snapshot != broker[client_id]:
                missing.add('orders')
            broker[client_id] = snapshot
        reflected = frozenset(broker if account.official_order_ids is None else account.official_order_ids)
        if orders is None:
            orders = self.store.active_orders(account_id)
        order_risk, opening = {}, {}
        by_owner = defaultdict(lambda: ZERO)
        cancel_by_owner = defaultdict(lambda: ZERO)
        reserved_cash = local_cash = local_margin = pending = pending_cancel = notional = ZERO

        def add(snapshot, owner, price, order=None, remote=None):
            nonlocal reserved_cash, local_cash, local_margin, pending, pending_cancel, notional
            intent = snapshot.intent
            oid, symbol = intent.client_order_id, intent.instrument_id
            uncertain = snapshot.reconcile_required if order is None else order['uncertain']
            cancel = order is not None and order.get('cancel_pending', False)
            if remote is not None and (remote.intent != intent or
                    (remote.status in TERMINAL) != (snapshot.status in TERMINAL)):
                missing.add('orders')
            if snapshot.status in TERMINAL and not uncertain and not cancel:
                return
            filled = snapshot.cumulative_filled_quantity
            if remote is not None:
                filled = max(filled, remote.cumulative_filled_quantity)
            quantity = intent.quantity - filled
            if quantity <= ZERO:
                return
            held = holdings.get(owner, {}).get(symbol, ZERO)
            effect = 'UNKNOWN' if owner is None else inventory_effect(intent.side, quantity, held)
            if effect in ('REDUCE', 'CLOSE'):
                order_risk[oid] = OrderRisk(
                    oid, owner, symbol, intent.quantity * price, quantity * price, ZERO,
                    effect, oid in reflected, cancel, uncertain)
                return
            if price is None:
                missing.add('order_price')
                amount = original = None
                economic = ZERO
            else:
                amount = price * quantity
                original = price * intent.quantity
                economic = amount * multipliers.get(symbol, ONE)
                if symbol in ambiguous:
                    missing.add('economic_multiplier')
            if owner is None:
                missing.add('order_ownership')
            cash = (amount or ZERO) if intent.side is Side.BUY or account.market is Market.BINANCE_USDM else ZERO
            pending += economic
            notional += amount or ZERO
            pending_cancel += economic if cancel else ZERO
            risk_symbols[symbol] += economic
            by_owner[owner] += economic
            cancel_by_owner[owner] += economic if cancel else ZERO
            if oid in reflected:
                reserved_cash += cash
            else:
                local_cash += cash
                local_margin += amount or ZERO
            entry = OrderRisk(oid, owner, symbol, original, amount, None if price is None else economic,
                              effect, oid in reflected, cancel, uncertain)
            opening[oid] = order_risk[oid] = entry

        seen_local = set()
        for owner, order in orders:
            snapshot = order['record'].snapshot
            seen_local.add(snapshot.intent.client_order_id)
            remote = broker.pop(snapshot.intent.client_order_id, None)
            add(snapshot, owner, order['reservation_price'], order, remote)
        for snapshot in broker.values():
            add(snapshot, None, snapshot.intent.limit_price)
        if include_pending:
            priced_owners = set()
            if self.pending_orders is not None:
                for owner, intent, price in self.pending_orders(account_id):
                    priced_owners.add(owner)
                    if intent.client_order_id not in seen_local and intent.client_order_id not in broker:
                        add(OrderSnapshot(intent), owner, price)
            pending_owners = self.store.unprepared_buy_owners(account_id)
            if self.pending_commitments is not None:
                pending_owners.update(owner for owner, amount in self.pending_commitments(account_id).items()
                                      if amount is None or (amount > ZERO and owner not in priced_owners))
            if pending_owners:
                missing.add('pending_capital')
        if reflected - (opening.keys() | broker.keys() | seen_local):
            missing.add('orders')
        if ambiguous & positions.keys():
            missing.add('economic_multiplier')
        equity = account.official_equity if account.official_equity is not None else equity
        balance = next((item for item in account.assets if item.currency == currency), None)
        cash = account.official_cash if account.official_cash is not None else (
            None if balance is None else balance.total)
        available = account.official_available if account.official_available is not None else (
            None if balance is None else balance.available)
        margin = account.initial_margin
        fee = Decimal(self.db.execute('SELECT fee_rate FROM capital_accounts WHERE account=?',
                                     (account_id,)).fetchone()[0])
        projected_cash = None if cash is None or available is None or 'order_price' in missing else (
            min(available, cash - reserved_cash * (ONE + fee)) - local_cash * (ONE + fee))
        projected_margin = None if margin is None or 'order_price' in missing else margin + local_margin
        for name, value in (('equity', equity), ('cash', cash), ('available_cash', available),
                            ('margin', margin), ('capital_cash_flow', account.cumulative_external_cash_flow)):
            if value is None:
                missing.add(name)
        healthy = not missing.intersection({'reconciliation', 'account_age', 'positions'})
        drawdown, daily, rolling = record_history(account, equity, observed_at, limits, healthy)
        if drawdown is None:
            missing.add('drawdown')
        if daily is None:
            missing.add('daily_loss')
        if rolling is None:
            missing.add('rolling_loss')
        bindings = dict(self.db.execute('SELECT strategy,logical_strategy FROM capital_instances '
                                        'WHERE account=? ORDER BY strategy', (account_id,)))
        contributions = {}
        grouped = defaultdict(list)
        for owner, logical in bindings.items():
            if not missing.intersection({'ownership', 'positions'}):
                values = [(quantity * positions[symbol].mark_price, multipliers.get(symbol, ONE))
                          for symbol, quantity in holdings[owner].items()]
                contributions[owner] = RiskContribution(
                    sum((abs(nominal) for nominal, _ in values), ZERO),
                    sum((abs(nominal) * factor for nominal, factor in values), ZERO),
                    sum((nominal * factor for nominal, factor in values), ZERO),
                    by_owner[owner], cancel_by_owner[owner])
            else:
                contributions[owner] = RiskContribution(None, None, None, by_owner[owner], cancel_by_owner[owner])
            grouped[logical].append(contributions[owner])

        def sum_contributions(group, attribute):
            amounts = [getattr(item, attribute) for item in group]
            if all(amount is not None for amount in amounts):
                return sum(amounts, ZERO)
            return None

        logical_contributions = {logical: RiskContribution(
            sum_contributions(group, 'market_value'), sum_contributions(group, 'gross_exposure'),
            sum_contributions(group, 'net_exposure'), sum_contributions(group, 'pending_open_exposure'),
            sum_contributions(group, 'pending_cancel_exposure')) for logical, group in grouped.items()}
        projected_gross = gross + pending
        concentration = {}
        for symbol, exposure in risk_symbols.items():
            # Only assume fills in the tested symbol. Other pending buys cannot
            # make concentration look safer before they become inventory.
            denominator = gross - confirmed_risk.get(symbol, ZERO) + exposure
            if denominator > ZERO:
                concentration[symbol] = exposure / denominator
        if 'positions' in missing:
            market_value = gross = net = projected_gross = None
        if 'pending_capital' in missing:
            projected_gross = projected_cash = projected_margin = None
        snapshot = PortfolioRiskSnapshot(account_id, currency, observed_at, account.reconciled,
            equity, cash, available, projected_cash, margin, projected_margin, market_value,
            gross, net, notional, pending, pending_cancel, projected_gross, dict(risk_symbols),
            concentration, contributions, logical_contributions, bindings, order_risk,
            drawdown, daily, rolling, tuple(sorted(missing)),
            unrealized_pnl=account.unrealized_pnl)
        return snapshot, opening

    @staticmethod
    def reason(limits, snapshot, opening, *, current=False):
        if limits.stop_new_risk:
            return 'PORTFOLIO_ACCOUNT_STOP'
        logical = snapshot.strategy_bindings.get(opening.owner)
        if opening.owner in limits.paused_strategies or logical in limits.paused_strategies:
            return 'PORTFOLIO_STRATEGY_PAUSED'
        if opening.symbol in limits.paused_symbols:
            return 'PORTFOLIO_SYMBOL_PAUSED'
        for name in ('account_age', 'reconciliation', 'positions', 'ownership', 'orders',
                     'order_ownership', 'order_price', 'economic_multiplier', 'pending_capital'):
            if name in snapshot.missing:
                return 'PORTFOLIO_RISK_' + name.upper()
        owner = snapshot.logical_strategies.get(logical)
        limits_and_facts = (
            ('GROSS_EXPOSURE', limits.max_gross_exposure, snapshot.projected_gross_exposure),
            ('SYMBOL_EXPOSURE', limits.max_symbol_exposure, snapshot.symbol_exposure[opening.symbol]),
            ('STRATEGY_EXPOSURE', limits.max_strategy_exposure,
             owner.projected_gross_exposure if owner else None),
            ('SYMBOL_CONCENTRATION', limits.max_symbol_concentration,
             snapshot.symbol_concentration.get(opening.symbol, ZERO)),
            ('MARGIN', limits.max_margin, snapshot.projected_initial_margin),
            ('DRAWDOWN', limits.max_drawdown, snapshot.drawdown),
            ('DAILY_LOSS', limits.max_daily_loss, snapshot.daily_loss),
            ('ROLLING_LOSS', limits.max_rolling_loss, snapshot.rolling_loss),
            ('ORDER_NOTIONAL', None if current else limits.max_order_notional, opening.order_notional),
        )
        for name, limit, value in limits_and_facts:
            if limit is not None:
                if value is None:
                    return 'PORTFOLIO_UNKNOWN_' + name
                if value > limit:
                    return 'PORTFOLIO_' + name + '_LIMIT'
        if limits.min_available_cash is not None:
            if snapshot.projected_available_cash is None:
                return 'PORTFOLIO_UNKNOWN_CASH_RESERVE'
            if snapshot.projected_available_cash < limits.min_available_cash:
                return 'PORTFOLIO_CASH_RESERVE'
        return ''

