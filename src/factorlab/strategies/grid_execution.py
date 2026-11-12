"""Confirmation-driven, long-only grid seam; no matching or economic posting.

The caller owns account valuation and execution. Quantity units explicitly map
legacy grid units to base-asset/contract units; spot cash is never inferred from
a futures wallet. Paper integration needs a pre-fill turnover admission hook.
"""
from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR

from factorlab.trading.contracts import (
    Market, OrderIntent, OrderStatus, OrderType, Side, TERMINAL, decimal_value,
)
from .grid import GridStrategy

def D(value):
    return Decimal(str(value))


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

    def __post_init__(self):
        for name in ('inventory', 'available', 'equity', 'committed', 'turnover'):
            decimal_value(getattr(self, name), name)


class GridExecution:
    """Order proposals and cancellation requests, updated only by confirmations.

    Persist/replay the ordered observe/confirm command stream together with the
    caller's authoritative account journal. Reuse a deployment ID only on replay.
    Unknown outcomes block all decisions until explicit reconciliation feedback.
    """
    grid_prices = GridStrategy.grid_prices

    def __init__(self, config, rules, *, deployment_id, market, instrument_id,
                 initial_equity, fee_rate=Decimal(0)):
        if not deployment_id or len(deployment_id) > 48:
            raise ValueError('bounded deployment identity required')
        decimal_value(initial_equity, 'initial_equity', positive=True)
        decimal_value(fee_rate, 'fee_rate')
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
                       * max(self.price, o.intent.limit_price or self.price) * (1 + self.fee_rate)
                       for o in self.active if o.intent.side == Side.BUY
                       and o.status == OrderStatus.NEW), Decimal(0))
        cost = quantity * max(self.price, valuation) * (1 + self.fee_rate)
        if side == Side.SELL:
            if quantity > a.inventory - sells:
                return
        elif (a.inventory + buys + quantity > D(c.max_inventory) * r.quantity_unit
              or pending + cost > a.available
              or a.committed + pending + cost > D(c.max_cash_committed)):
            return
        # TODO: preserve effective FeeSchedule minimum fees and slippage erosion.
        if not market and level >= 0:
            adjacent = level + (1 if side == Side.BUY else -1)
            if 0 <= adjacent < len(self.prices):
                other = D(self.prices[adjacent])
                if abs(other - target) <= (other + target) * self.fee_rate:
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
        if self.active:
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

    def observe(self, event, account):
        # TODO: share legacy input validation and ordered-time checks before acceptance.
        identity = event['id']
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

    def replay_commands(self, commands):
        """Replay into a fresh instance with identical deployment configuration."""
        for command in commands:
            action, *arguments = command
            getattr(self, action)(*arguments)

    def mark_unknown(self, order_id):
        from datetime import datetime, timezone
        from factorlab.trading.state import OrderRecord, unknown
        record = self.records.get(order_id, OrderRecord(
            self.orders[order_id], datetime.min.replace(tzinfo=timezone.utc), self.targets[order_id]))
        self.journal.append(('mark_unknown', order_id))
        self.records[order_id] = unknown(record)
        self.orders[order_id] = self.records[order_id].snapshot

    def confirm(self, event, account):
        from factorlab.trading.state import OrderRecord, advance
        if event.event_id in self.feedback:
            if self.feedback[event.event_id] != (event, account):
                raise ValueError('feedback identity conflict')
            return
        key = event.client_order_id
        old = self.orders[key]
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
        if delta and self.state == 'RUNNING' and self.pending_rebuild is None and key not in self.cancellations:
            level = self.levels[key] + (1 if old.intent.side == Side.BUY else -1)
            if 0 <= level < len(self.prices):
                self.propose(Side.SELL if old.intent.side == Side.BUY else Side.BUY,
                             self.prices[level], delta, level)
        self._settle()
