"""Confirmation-driven, long-only grid seam; no matching or economic posting.

The caller owns account valuation and execution. Quantity units explicitly map
legacy grid units to base-asset/contract units; spot cash is never inferred from
a futures wallet. Paper integration needs a pre-fill turnover admission hook.
"""
from copy import deepcopy
from functools import wraps
from datetime import datetime, timezone
import re

from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR

from factorlab.trading.contracts import (
    Market, OrderIntent, OrderStatus, OrderType, Side, TERMINAL, decimal_value,
)
from .grid import GridStrategy

def D(value):
    return Decimal(str(value))


def atomic(method):
    """Publish a staged decision only after every operation succeeds."""
    @wraps(method)
    def apply(self, *args, **kwargs):
        # TODO: replace history-wide staging with bounded command deltas.
        staged = deepcopy(self)
        result = method(staged, *args, **kwargs)
        self.__dict__ = staged.__dict__
        return result
    return apply


def validate_identity(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', value):
        raise ValueError('invalid identity')


@dataclass(frozen=True)
class GridRules:
    quantity_unit: Decimal
    step_size: Decimal
    tick_size: Decimal
    min_quantity: Decimal
    min_notional: Decimal

    def __post_init__(self):
        for name in ('quantity_unit', 'step_size', 'tick_size'):
            decimal_value(getattr(self, name), name, positive=True)
        for name in ('min_quantity', 'min_notional'):
            decimal_value(getattr(self, name), name)


@dataclass(frozen=True)
class GridAccount:
    """Authoritative risk projection, in quote currency and explicit quantity units.

    equity includes unrealized PnL; committed includes existing entry capital
    and all pending opening reservations. available excludes broker reservations.
    turnover is confirmed gross filled notional since the deployment baseline.
    """
    inventory: Decimal
    available: Decimal
    equity: Decimal
    committed: Decimal
    turnover: Decimal
    reconciled: bool = True
    account_id: str = 'external'
    instrument_id: str = 'external'
    market: Market | None = None

    def __post_init__(self):
        validate_identity(self.account_id)
        validate_identity(self.instrument_id)
        if type(self.reconciled) is not bool or (self.market is not None and not isinstance(self.market, Market)):
            raise ValueError('invalid account state')
        for name in ('inventory', 'available', 'equity', 'committed', 'turnover'):
            decimal_value(getattr(self, name), name)


def project_paper_account(controller, instrument_id, *, turnover):
    """Read a single-instrument paper account; turnover must come from its journal.

    Position entry notional is collateral for USD-M, not a spot cash purchase.
    The snapshot lacks historical fill prices, so it cannot infer gross turnover.
    """
    state = controller.broker.state
    snapshot = controller.account()
    if set(state.instruments) != {instrument_id}:
        raise ValueError('dedicated single-instrument account required')
    instrument = state.instruments[instrument_id]
    asset = next(a for a in snapshot.assets if a.currency == instrument.quote_currency)
    position = state.positions.get(instrument_id)
    inventory = position.quantity if position else Decimal(0)
    if inventory < 0:
        raise ValueError('long-only Grid cannot project short inventory')
    entry = inventory * position.entry_price if position else Decimal(0)
    mark = inventory * position.mark_price if position else Decimal(0)
    equity = asset.total + (mark-entry if state.market == Market.BINANCE_USDM else mark)
    committed = asset.reserved if state.market == Market.BINANCE_USDM else entry + asset.reserved
    return GridAccount(inventory, asset.available, equity, committed, turnover,
        snapshot.reconciled and not any(o.reconcile_required for o in snapshot.open_orders),
        snapshot.account_id, instrument_id, state.market)


class GridExecution:
    """Order proposals and cancellation requests, updated only by confirmations.

    Persist/replay the ordered observe/confirm command stream together with the
    caller's authoritative account journal. Reuse a deployment ID only on replay.
    Unknown outcomes block all decisions until explicit reconciliation feedback.
    """
    grid_prices = GridStrategy.grid_prices

    def __init__(self, config, rules, *, deployment_id, market, instrument_id,
                 initial_equity, fee_rate=None, fees=None):
        if not deployment_id or len(deployment_id) > 48:
            raise ValueError('bounded deployment identity required')
        decimal_value(initial_equity, 'initial_equity', positive=True)
        from factorlab.backtest.fees import FeeRule, FeeSchedule
        validate_identity(deployment_id)
        if fee_rate is not None:
            decimal_value(fee_rate, 'fee_rate')
        self.fees = fees or FeeSchedule([FeeRule('1900-01-01',
            commission_bps=config.fee_bps if fee_rate is None else float(fee_rate * 10000),
            minimum_commission_cny=0, slippage_bps=config.slippage_bps)])
        self.timestamp = None
        self.account_identity = None
        self.config, self.rules = config, rules
        self.deployment_id, self.market, self.instrument_id = deployment_id, market, instrument_id
        self.initial_equity, self.peak, self.fee_rate = initial_equity, initial_equity, fee_rate
        self.prices = self.grid_prices(config.lower, config.upper)
        self.center = config.initial_price
        self.sequence = self.last_rebuild = self.rebuilds = 0
        self.state = 'WARMUP' if config.mode == 'atr' else 'RUNNING'
        self.true_ranges, self.previous_close = [], None
        self.records = {}
        self.orders, self.targets, self.levels, self.created = {}, {}, {}, {}
        self.cancellations, self.expirations, self.seen = set(), set(), {}
        self.pending_rebuild = None
        self.journal = []
        self.commands, self.feedback = {}, {}
        self.account = None
        self.initialized = False

    @property
    def active(self):
        return [o for o in self.orders.values() if o.status not in TERMINAL]

    def estimated_cost(self, side, price, quantity):
        """Reserve one minimum-fee execution per quantity step, not one per order."""
        unit = self.rules.step_size
        count = quantity / unit
        return D(self.fees.calculate(self.timestamp, side.value.lower(), float(price * unit)).total) * count

    def propose(self, side, target, quantity, level=-1, market=False):
        from factorlab.trading.contracts import OrderSnapshot
        r, a, c = self.rules, self.account, self.config
        quantity = (quantity / r.step_size).to_integral_value(rounding=ROUND_FLOOR) * r.step_size
        target = D(target)
        price = (target / r.tick_size).to_integral_value(
            rounding=ROUND_FLOOR if side == Side.BUY else ROUND_CEILING) * r.tick_size
        valuation = self.price if market else price
        if quantity < max(r.min_quantity, D(c.minimum_quantity) * r.quantity_unit) or quantity <= 0:
            return
        if valuation < D(c.minimum_price) or quantity * valuation < r.min_notional:
            return
        buys = sum((o.intent.quantity - o.cumulative_filled_quantity for o in self.active
                    if o.intent.side == Side.BUY), Decimal(0))
        sells = sum((o.intent.quantity - o.cumulative_filled_quantity for o in self.active
                     if o.intent.side == Side.SELL), Decimal(0))
        # Outstanding proposals count before a controller has accepted them.
        pending = sum(((o.intent.quantity - o.cumulative_filled_quantity)
                       * max(self.price, o.intent.limit_price or self.price)
                       + self.estimated_cost(Side.BUY, max(self.price, o.intent.limit_price or self.price),
                           o.intent.quantity - o.cumulative_filled_quantity)
                       for o in self.active if o.intent.side == Side.BUY
                       and o.status == OrderStatus.NEW), Decimal(0))
        cost = quantity * valuation + self.estimated_cost(side, valuation, quantity)
        if side == Side.SELL:
            if quantity > a.inventory - sells:
                return
        elif (a.inventory + buys + quantity > D(c.max_inventory) * r.quantity_unit
              or pending + cost > a.available
              or a.committed + pending + cost > D(c.max_cash_committed)):
            return
        if not market and level >= 0:
            adjacent = level + (1 if side == Side.BUY else -1)
            if 0 <= adjacent < len(self.prices):
                other = D(self.prices[adjacent])
                opposite = Side.SELL if side == Side.BUY else Side.BUY
                pair_cost = self.estimated_cost(side, price, r.quantity_unit) + self.estimated_cost(opposite, other, r.quantity_unit)
                if abs(other - target) * r.quantity_unit <= pair_cost:
                    return
        identity = f'{self.deployment_id}-{len(self.orders) + 1:08d}'
        intent = OrderIntent(self.market, self.instrument_id, side,
            OrderType.MARKET if market else OrderType.LIMIT, quantity,
            None if market else price, identity, reduce_only=side == Side.SELL)
        self.orders[identity] = OrderSnapshot(intent)
        self.targets[identity], self.levels[identity], self.created[identity] = target, level, self.sequence
        return intent

    def _seed(self):
        for level, target in enumerate(self.prices):
            if D(target) != self.price:
                quantity = (D(self.config.quantity) * self.rules.quantity_unit
                            if self.config.budget is None else
                            D(int(self.config.budget // target)) * self.rules.quantity_unit)
                self.propose(Side.BUY if D(target) < self.price else Side.SELL, target, quantity, level)

    def _cancel_all(self):
        self.cancellations.update(o.intent.client_order_id for o in self.active)

    def _risk(self, reason, exit_position=False):
        self._cancel_all()
        self.state = 'EXITING' if exit_position and self.account.inventory else 'PAUSED'
        self.reason = reason

    def _rebuild(self, center, spacing=None):
        self._cancel_all()
        self.pending_rebuild = (center, spacing)
        self._settle()

    def _settle(self):
        if self.active or self.account is None or not self.account.reconciled:
            return
        if self.pending_rebuild is not None:
            center, spacing = self.pending_rebuild
            self.pending_rebuild = None
            # Reuse the legacy range construction and invalid-range decision.
            GridStrategy._rebuild(self, center, spacing)
        elif self.state == 'EXITING':
            if self.account.inventory:
                self.propose(Side.SELL, self.price, self.account.inventory, market=True)
            else:
                self.state = 'EXITED'

    def _log(self, kind, **values):
        if kind == 'RISK':
            self.reason = values['reason']

    def _validate_account(self, account):
        if not isinstance(account, GridAccount):
            raise ValueError('GridAccount required')
        account.__post_init__()
        key = (account.account_id, account.instrument_id, account.market)
        if account.instrument_id not in {'external', self.instrument_id} or account.market not in {None, self.market}:
            raise ValueError('account deployment mismatch')
        if self.account_identity is not None and key != self.account_identity:
            raise ValueError('account identity changed')
        if self.account is not None and account.turnover < self.account.turnover:
            raise ValueError('turnover regression')
        self.account_identity = key

    @atomic
    def observe(self, event, account):
        import pandas as pd
        identity_value = event['id']
        validate_identity(identity_value)
        if identity_value not in self.commands:
            timestamp = pd.Timestamp(event['time'])
            if pd.isna(timestamp) or timestamp.tzinfo is not None:
                raise ValueError('ordered timezone-free observations required')
            if self.timestamp is not None and timestamp < self.timestamp:
                raise ValueError('observation time regression')
            price = D(event['price'])
            decimal_value(price, 'price', positive=True)
            if type(event.get('volume')) is not int or event['volume'] < 0:
                raise ValueError('invalid volume')
            if type(event.get('completed_bar', False)) is not bool:
                raise ValueError('invalid completed_bar')
            if event.get('action', 'observe') not in {'observe', 'cancel'}:
                raise ValueError('invalid action')
            if event.get('action') == 'cancel' and event.get('order_id') not in self.orders:
                raise ValueError('unknown cancel order')
            if event.get('completed_bar'):
                high, low, close = (D(event[k]) for k in ('high', 'low', 'close'))
                for value in (high, low, close):
                    decimal_value(value, 'OHLC', positive=True)
                if not low <= close <= high or price != close:
                    raise ValueError('invalid OHLC')
            self._validate_account(account)
            self.timestamp = timestamp
        identity = identity_value
        command = (dict(event), account)
        if identity in self.commands:
            if self.commands[identity] != command:
                raise ValueError('observation identity conflict')
            return
        if not account.reconciled or any(o.reconcile_required for o in self.active):
            raise ValueError('RECONCILE_REQUIRED')
        self.journal.append(('observe', dict(event), account))
        self.commands[identity] = command
        self.account, self.price = account, D(event['price'])
        self.sequence += 1
        c = self.config
        self.peak = max(self.peak, account.equity)
        if not self.initialized:
            self.initialized = True
            if self.state == 'RUNNING':
                self._seed()
        if event.get('action') == 'cancel':
            self.cancellations.add(event['order_id'])
        if self.state in {'RUNNING', 'WARMUP'}:
            if 1 - account.equity / self.peak >= D(c.max_drawdown):
                self._risk('MAX_DRAWDOWN')
            elif account.turnover / self.initial_equity >= D(c.max_turnover):
                self._risk('MAX_TURNOVER')
            elif self.state == 'RUNNING' and not D(self.prices[0]) <= self.price <= D(self.prices[-1]):
                if c.breakout == 'rebuild' and self.sequence - self.last_rebuild >= c.min_rebuild_interval:
                    spacing = (sum(self.true_ranges[-c.atr_window:]) / c.atr_window * c.atr_multiplier
                               if c.mode == 'atr' else None)
                    self._rebuild(float(self.price), spacing)
                else:
                    self._risk('BREAKOUT_REBUILD_INTERVAL' if c.breakout == 'rebuild' else 'BREAKOUT',
                               c.breakout == 'exit')
        for o in self.active:
            key = o.intent.client_order_id
            if o.intent.order_type == OrderType.LIMIT and self.sequence - self.created[key] >= c.order_ttl:
                self.cancellations.add(key)
                self.expirations.add(key)
        if event.get('completed_bar'):
            high, low, close = (float(event[k]) for k in ('high', 'low', 'close'))
            previous = self.previous_close
            self.true_ranges.append(high - low if previous is None else max(high-low, abs(high-previous), abs(low-previous)))
            self.previous_close = close
            if c.mode == 'atr' and self.state in {'WARMUP', 'RUNNING'} and len(self.true_ranges) >= c.atr_window:
                spacing = sum(self.true_ranges[-c.atr_window:]) / c.atr_window * c.atr_multiplier
                if spacing > 0 and (self.state == 'WARMUP' or (abs(close-self.center) >= c.rebuild_threshold * spacing
                        and self.sequence-self.last_rebuild >= c.min_rebuild_interval)):
                    self._rebuild(close, spacing)
        self._settle()

    def deployment(self):
        from dataclasses import asdict
        return dict(config=asdict(self.config), rules=asdict(self.rules),
            deployment_id=self.deployment_id, market=self.market, instrument_id=self.instrument_id,
            initial_equity=self.initial_equity, fees=[asdict(rule) | {
                'effective_from': rule.effective_from.isoformat(),
                'effective_to': rule.effective_to.isoformat() if rule.effective_to else None}
                for rule in self.fees.rules])

    def checkpoint(self, store):
        """Store decision commands in the existing account transaction.

        Caller must hold PaperStore.transaction and have saved authority first.
        This does not submit or replay economic orders.
        """
        from dataclasses import asdict
        if not store.db.in_transaction:
            raise ValueError('account transaction required')
        state = store.load()
        for key, order in self.orders.items():
            if order.status != OrderStatus.NEW:
                authority = state.orders.get(key)
                if authority is None or authority.snapshot != order:
                    raise ValueError('strategy ahead of authority')
        commands = []
        for action, *args in self.journal:
            if action in {'observe', 'confirm'}:
                args[-1] = asdict(args[-1])
            commands.append([action, *args])
        state.runtime['grid:'+self.deployment_id] = dict(version=1, deployment=self.deployment(), commands=commands)
        store.save(state)

    @atomic
    def restore(self, store):
        if self.journal:
            raise ValueError('restore requires fresh deployment')
        state = store.load()
        payload = state.runtime.get('grid:'+self.deployment_id)
        if payload is None or payload['version'] != 1 or payload['deployment'] != self.deployment():
            raise ValueError('missing or incompatible Grid checkpoint')
        commands = []
        for action, *args in payload['commands']:
            if args[-1] is not None and action in {'observe', 'confirm'}:
                args[-1] = GridAccount(**args[-1])
            commands.append([action, *args])
        self.replay_commands(commands)
        for key, order in self.orders.items():
            authority = state.orders.get(key)
            if authority is not None and authority.snapshot.intent != order.intent:
                raise ValueError('authority intent mismatch')
            if order.status != OrderStatus.NEW:
                confirmed = state.confirmations.get(key)
                from dataclasses import replace
                expected = replace(authority.snapshot, status=confirmed.status, reconcile_required=False) if authority is not None and confirmed is not None else None
                if expected != order:
                    raise ValueError('checkpoint requires authority reconciliation')
        if not state.reconciled:
            for key in list(self.orders):
                if self.orders[key].status not in TERMINAL:
                    self.mark_unknown(key)

    @atomic
    def replay_commands(self, commands):
        """Replay into a fresh instance with identical deployment configuration."""
        for command in commands:
            action, *arguments = command
            if action not in {'observe', 'confirm', 'mark_unknown'}:
                raise ValueError('invalid replay command')
            getattr(self, action)(*arguments)

    @atomic
    def mark_unknown(self, order_id):
        from datetime import datetime, timezone
        from factorlab.trading.state import OrderRecord, unknown
        record = self.records.get(order_id, OrderRecord(
            self.orders[order_id], datetime.min.replace(tzinfo=timezone.utc), self.targets[order_id]))
        self.journal.append(('mark_unknown', order_id))
        self.records[order_id] = unknown(record)
        self.orders[order_id] = self.records[order_id].snapshot

    @atomic
    def confirm(self, event, account):
        from factorlab.trading.state import OrderRecord, advance
        validate_identity(event.event_id)
        if event.event_id in self.feedback:
            if self.feedback[event.event_id] != (event, account):
                raise ValueError('feedback identity conflict')
            return
        self._validate_account(account)
        key = event.client_order_id
        old = self.orders[key]
        cancel_requested = key in self.cancellations
        if not isinstance(event.event_time, datetime):
            raise ValueError('invalid confirmation time')
        record = self.records.get(key, OrderRecord(old, event.event_time, self.targets[key]))
        record = advance(record, event)
        updated = record.snapshot
        self.journal.append(('confirm', event, account))
        self.feedback[event.event_id] = (event, account)
        self.records[key] = record
        self.orders[key], self.account = updated, account
        delta = updated.cumulative_filled_quantity - old.cumulative_filled_quantity
        self.peak = max(self.peak, account.equity)
        if self.state in {'RUNNING', 'WARMUP'}:
            if 1 - account.equity / self.peak >= D(self.config.max_drawdown):
                self._risk('MAX_DRAWDOWN')
            elif account.turnover / self.initial_equity >= D(self.config.max_turnover):
                self._risk('MAX_TURNOVER')
        if updated.status in TERMINAL:
            self.cancellations.discard(key)
        if delta and self.state == 'RUNNING' and self.pending_rebuild is None and not cancel_requested and account.reconciled:
            level = self.levels[key] + (1 if old.intent.side == Side.BUY else -1)
            if 0 <= level < len(self.prices):
                self.propose(Side.SELL if old.intent.side == Side.BUY else Side.BUY,
                             self.prices[level], delta, level)
        self._settle()
