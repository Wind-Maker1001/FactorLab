"""Confirmation-driven, long-only grid seam; no matching or economic posting.

The caller owns account valuation and execution. Quantity units explicitly map
legacy grid units to base-asset/contract units; spot cash is never inferred from
a futures wallet. Paper integration needs a pre-fill turnover admission hook.
"""
from collections import deque
from contextlib import contextmanager
from copy import copy
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from hashlib import sha256
import re

import pandas as pd

from factorlab.backtest.fees import FeeRule, FeeSchedule
from factorlab.trading.contracts import (
    Market, OrderIntent, OrderSnapshot, OrderStatus, OrderType, Side, TERMINAL, ZERO, decimal_value,
)
from factorlab.trading.paper import Quote
from factorlab.trading.state import OrderRecord, advance, unknown
from factorlab.trading.storage import dumps
from .grid import GridStrategy


_CHECKPOINT = 'grid_execution'
_CHECKPOINT_VERSION = 2
_MAX_WORKING_ORDERS = 1000  # The PaperStore order budget.
_MAX_ATR_WINDOW = 4096
_DECISION_FIELDS = (
    'price', 'prices', 'center', 'state', 'reason', 'peak', 'sequence', 'last_rebuild',
    'rebuilds', 'previous_close', 'issued_orders', 'delivery', 'last_input', 'conflict',
)


def D(value):
    return Decimal(str(value))


def _fingerprint(value):
    return sha256(dumps(value).encode()).hexdigest()


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
    account_id: str
    instrument_id: str
    market: Market
    reconciled: bool = True

    def __post_init__(self):
        validate_identity(self.account_id)
        validate_identity(self.instrument_id)
        if type(self.reconciled) is not bool or not isinstance(self.market, Market):
            raise ValueError('invalid account state')
        for name in ('inventory', 'available', 'equity', 'committed', 'turnover'):
            decimal_value(getattr(self, name), name)


def _historical_turnover(store, *, after=0):
    filled = {}
    turnover = ZERO
    for cursor, event in store.order_events():
        delta = event.cumulative_filled_quantity - filled.get(event.client_order_id, ZERO)
        if cursor > after and delta:
            turnover += delta * event.fill_price
        filled[event.client_order_id] = event.cumulative_filled_quantity
    return turnover


def project_paper_account(controller, instrument_id):
    """Project the controller's account with gross turnover since account inception."""
    with _paper_snapshot(controller) as (broker, store):
        return _project_paper_account(broker, instrument_id, _historical_turnover(store))


@contextmanager
def _paper_snapshot(controller):
    store = controller.store
    with store.transaction():
        broker = copy(controller.broker)
        broker.state = store.load()
        yield broker, store


def _project_paper_account(broker, instrument_id, turnover):
    state = broker.state
    if set(state.instruments) != {instrument_id}:
        raise ValueError('dedicated single-instrument account required')
    snapshot = broker.read_account()
    instrument = state.instruments[instrument_id]
    asset = next(a for a in snapshot.assets if a.currency == instrument.quote_currency)
    position = state.positions.get(instrument_id)
    inventory = position.quantity if position else ZERO
    if inventory < 0:
        raise ValueError('long-only Grid cannot project short inventory')
    entry = inventory * position.entry_price if position else ZERO
    mark = inventory * position.mark_price if position else ZERO
    futures = state.market == Market.BINANCE_USDM
    equity = asset.total + (mark - entry if futures else mark)
    committed = asset.reserved if futures else entry + asset.reserved
    return GridAccount(inventory, asset.available, equity, committed, turnover,
        snapshot.account_id, instrument_id, state.market,
        reconciled=snapshot.reconciled and not any(o.reconcile_required for o in snapshot.open_orders))


def _observation(event):
    validate_identity(event['id'])
    timestamp = pd.Timestamp(event['time'])
    if pd.isna(timestamp) or timestamp.tzinfo is not None:
        raise ValueError('ordered timezone-free observations required')
    price = D(event['price'])
    decimal_value(price, 'price', positive=True)
    volume = event['volume']
    if type(volume) is not int or volume < 0:
        raise ValueError('invalid volume')
    completed_bar = event.get('completed_bar', False)
    if type(completed_bar) is not bool:
        raise ValueError('invalid completed_bar')
    action = event.get('action', 'observe')
    if action not in {'observe', 'cancel'}:
        raise ValueError('invalid action')
    observation = dict(id=event['id'], time=timestamp.isoformat(), price=price,
                       volume=volume, action=action, completed_bar=completed_bar)
    if action == 'cancel':
        observation['order_id'] = event['order_id']
        validate_identity(observation['order_id'])
    if completed_bar:
        high, low, close = (D(event[key]) for key in ('high', 'low', 'close'))
        for value in (high, low, close):
            decimal_value(value, 'OHLC', positive=True)
        if not low <= close <= high or price != close:
            raise ValueError('invalid OHLC')
        observation.update(high=high, low=low, close=close)
    return observation


class GridConfirmationConflict(ValueError):
    """The Grid deployment is blocked pending resolution by its authority."""


class GridExecution:
    """Order proposals and cancellation requests, updated only by confirmations.

    Delivery numbers order all commands within one deployment. Paper commands
    publish working state only after the authoritative account transaction commits.
    """
    grid_prices = GridStrategy.grid_prices

    def __init__(self, config, rules, *, deployment_id, account_id, market, instrument_id,
                 initial_equity, fee_rate=None, fees=None):
        validate_identity(deployment_id)
        validate_identity(account_id)
        validate_identity(instrument_id)
        if len(deployment_id) > 48 or not isinstance(market, Market):
            raise ValueError('invalid Grid deployment')
        if config.levels >= _MAX_WORKING_ORDERS or (config.mode == 'atr' and config.atr_window > _MAX_ATR_WINDOW):
            raise ValueError('Grid range or ATR window exceeds the checkpoint budget')
        decimal_value(initial_equity, 'initial_equity', positive=True)
        if fee_rate is not None:
            decimal_value(fee_rate, 'fee_rate')
        self.fees = fees or FeeSchedule([FeeRule('1900-01-01',
            commission_bps=config.fee_bps if fee_rate is None else float(fee_rate * 10000),
            minimum_commission_cny=0, slippage_bps=config.slippage_bps)])
        self.timestamp = None
        self.config, self.rules = config, rules
        self.deployment_id = deployment_id
        self.account_id, self.market, self.instrument_id = account_id, market, instrument_id
        self.initial_equity = self.peak = initial_equity
        self.prices = self.grid_prices(config.lower, config.upper)
        self.center, self.price = config.initial_price, D(config.initial_price)
        self.sequence = self.last_rebuild = self.rebuilds = 0
        self.state = 'WARMUP' if config.mode == 'atr' else 'RUNNING'
        self.reason = None
        self.true_ranges, self.previous_close = deque(maxlen=config.atr_window), None
        self.records = {}
        self.orders, self.targets, self.levels, self.created = {}, {}, {}, {}
        self.cancellations, self.expirations = set(), set()
        self.pending_rebuild = None
        self.pending_bar = None
        self.account = None
        self.issued_orders = self.delivery = 0
        self.last_input = self.conflict = None
        self._turnover = ZERO
        self._revision = self._baseline = None

    def _stage(self):
        staged = copy(self)
        for name in ('records', 'orders', 'targets', 'levels', 'created',
                     'cancellations', 'expirations', 'true_ranges'):
            setattr(staged, name, getattr(self, name).copy())
        return staged

    @property
    def active(self):
        return list(self.orders.values())

    def estimated_cost(self, side, price, quantity):
        """Reserve one minimum-fee execution per quantity step, not one per order."""
        unit = self.rules.step_size
        count = quantity / unit
        return D(self.fees.calculate(self.timestamp, side.value.lower(), float(price * unit)).total) * count

    def _can_propose(self):
        return (self.conflict is None and self.account is not None and self.account.reconciled
                and not any(o.reconcile_required or o.status == OrderStatus.UNKNOWN for o in self.active))

    def propose(self, side, target, quantity, level=-1, market=False):
        self._require_external()
        return self._propose(side, target, quantity, level, market)

    def _propose(self, side, target, quantity, level=-1, market=False):
        if not self._can_propose() or not (
                self.state == 'RUNNING' or
                (self.state == 'EXITING' and market and side == Side.SELL)):
            return
        r, a, c = self.rules, self.account, self.config
        quantity = (quantity / r.step_size).to_integral_value(rounding=ROUND_FLOOR) * r.step_size
        target = D(target)
        price = (target / r.tick_size).to_integral_value(
            rounding=ROUND_FLOOR if side == Side.BUY else ROUND_CEILING) * r.tick_size
        valuation = self.price if market else price
        if quantity < max(r.min_quantity, D(c.minimum_quantity) * r.quantity_unit) or quantity <= 0:
            return
        if (not market and price < D(c.minimum_price)) or quantity * valuation < r.min_notional:
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
        if len(self.orders) == _MAX_WORKING_ORDERS:
            raise ValueError('Grid working order budget exhausted')
        next_order = self.issued_orders + 1
        identity = f'{self.deployment_id}-{next_order:08d}'
        intent = OrderIntent(self.market, self.instrument_id, side,
            OrderType.MARKET if market else OrderType.LIMIT, quantity,
            None if market else price, identity, reduce_only=side == Side.SELL)
        self.issued_orders = next_order
        self.orders[identity] = OrderSnapshot(intent)
        self.targets[identity], self.levels[identity], self.created[identity] = target, level, self.sequence
        return intent

    def _seed(self):
        for level, target in enumerate(self.prices):
            if D(target) != self.price:
                quantity = (D(self.config.quantity) * self.rules.quantity_unit
                            if self.config.budget is None else
                            D(int(self.config.budget // target)) * self.rules.quantity_unit)
                self._propose(Side.BUY if D(target) < self.price else Side.SELL, target, quantity, level)

    def _cancel_all(self):
        self.cancellations.update(o.intent.client_order_id for o in self.active)

    def _risk(self, reason, exit_position=False):
        self.pending_rebuild = None
        self._cancel_all()
        self.state = 'EXITING' if exit_position and self.account.inventory else 'PAUSED'
        self.reason = reason

    def _rebuild(self, center, spacing=None):
        self._cancel_all()
        self.pending_rebuild = (center, spacing)
        self._settle()

    def _settle(self):
        if self.active or not self._can_propose():
            return
        if self.pending_rebuild is not None and self.state in {'RUNNING', 'WARMUP'}:
            center, spacing = self.pending_rebuild
            self.pending_rebuild = None
            # Reuse the legacy range construction and invalid-range decision.
            GridStrategy._rebuild(self, center, spacing)
        elif self.state == 'EXITING':
            if self.account.inventory:
                self._propose(Side.SELL, self.price, self.account.inventory, market=True)
            else:
                self.state = 'EXITED'

    def _log(self, kind, **values):
        if kind == 'RISK':
            self.reason = values['reason']

    def _validate_account(self, account):
        if (account.account_id, account.instrument_id, account.market) != (
                self.account_id, self.instrument_id, self.market):
            raise ValueError('account deployment mismatch')
        if self.account is not None and account.turnover < self.account.turnover:
            raise ValueError('turnover regression')

    def _require_external(self):
        if self._revision is not None:
            raise ValueError('Paper-bound Grid requires a Paper transaction')

    def _next_delivery(self, delivery, action, *values):
        if type(delivery) is not int or not 0 < delivery <= 2**63 - 1:
            raise ValueError('positive bounded Grid delivery required')
        fingerprint = _fingerprint([action, *values])
        if delivery == self.delivery:
            if fingerprint != self.last_input:
                raise ValueError('Grid delivery payload conflict')
            return False
        if delivery != self.delivery + 1:
            raise ValueError('Grid delivery must be consecutive')
        self.delivery, self.last_input = delivery, fingerprint
        return True

    def _apply_external(self, delivery, action, command, account):
        self._require_external()
        staged = self._stage()
        if staged._next_delivery(delivery, action, command, account):
            staged._require_no_conflict()
            if action == 'observe':
                staged._observe(command, account)
            elif action == 'confirm':
                if command.client_order_id in staged.orders:
                    staged._confirm(command, account)
                elif staged._owns(command.client_order_id):
                    staged._validate_account(account)
                    staged.conflict = command.event_id
                else:
                    raise ValueError('confirmation outside Grid deployment')
            elif action == 'unknown':
                staged._mark_unknown(command)
            else:
                staged._complete_bar()
        self.__dict__ = staged.__dict__
        self._require_no_conflict()

    def observe(self, event, account, *, delivery):
        self._apply_external(delivery, 'observe', _observation(event), account)

    def complete_bar(self, *, delivery):
        """Apply the ATR close after execution feedback for the observation."""
        self._apply_external(delivery, 'bar', None, None)

    def _check_observation(self, event):
        timestamp = pd.Timestamp(event['time'])
        if self.timestamp is not None and timestamp < self.timestamp:
            raise ValueError('observation time regression')
        if event['action'] == 'cancel' and event['order_id'] not in self.orders:
            raise ValueError('unknown cancel order')
        return timestamp

    def _observe(self, event, account):
        self._validate_account(account)
        if self.conflict or not account.reconciled or any(o.reconcile_required for o in self.active):
            raise ValueError('RECONCILE_REQUIRED')
        if self.pending_bar is not None:
            raise ValueError('complete the current ATR bar before observing another')
        self.timestamp = self._check_observation(event)
        self.account, self.price = account, event['price']
        self.sequence += 1
        c = self.config
        if self.sequence == 1 and self.state == 'RUNNING':
            self._seed()
        if event['action'] == 'cancel':
            self.cancellations.add(event['order_id'])
        self._account_risk()
        if self.state == 'RUNNING' and not D(self.prices[0]) <= self.price <= D(self.prices[-1]):
            if c.breakout == 'rebuild' and self.sequence - self.last_rebuild >= c.min_rebuild_interval:
                spacing = sum(self.true_ranges) / c.atr_window * c.atr_multiplier if c.mode == 'atr' else None
                self._rebuild(float(self.price), spacing)
            else:
                self._risk('BREAKOUT_REBUILD_INTERVAL' if c.breakout == 'rebuild' else 'BREAKOUT',
                           c.breakout == 'exit')
        for key, order in self.orders.items():
            if order.intent.order_type == OrderType.LIMIT and self.sequence - self.created[key] >= c.order_ttl:
                self.cancellations.add(key)
                self.expirations.add(key)
        if event['completed_bar'] and c.mode == 'atr':
            self.pending_bar = tuple(float(event[key]) for key in ('high', 'low', 'close'))
        self._settle()

    def _complete_bar(self):
        if self.pending_bar is None:
            raise ValueError('no pending ATR bar')
        if not self._can_propose():
            raise ValueError('RECONCILE_REQUIRED')
        high, low, close = self.pending_bar
        self.pending_bar = None
        previous = self.previous_close
        self.true_ranges.append(high - low if previous is None else max(high - low, abs(high - previous), abs(low - previous)))
        self.previous_close = close
        c = self.config
        if (self.pending_rebuild is None and self.state in {'WARMUP', 'RUNNING'}
                and len(self.true_ranges) == c.atr_window):
            spacing = sum(self.true_ranges) / c.atr_window * c.atr_multiplier
            moved = abs(close - self.center) >= c.rebuild_threshold * spacing
            interval_elapsed = self.sequence - self.last_rebuild >= c.min_rebuild_interval
            if spacing > 0 and (self.state == 'WARMUP' or (moved and interval_elapsed)):
                self._rebuild(close, spacing)

    def _account_risk(self):
        self.peak = max(self.peak, self.account.equity)
        if self.state in {'RUNNING', 'WARMUP'}:
            if 1 - self.account.equity / self.peak >= D(self.config.max_drawdown):
                self._risk('MAX_DRAWDOWN')
            elif self.account.turnover / self.initial_equity >= D(self.config.max_turnover):
                self._risk('MAX_TURNOVER')

    def deployment(self):
        return dict(config=asdict(self.config), rules=asdict(self.rules),
            deployment_id=self.deployment_id, account_id=self.account_id,
            market=self.market, instrument_id=self.instrument_id,
            initial_equity=self.initial_equity, fees=[asdict(rule) | {
                'effective_from': rule.effective_from.isoformat(),
                'effective_to': rule.effective_to.isoformat() if rule.effective_to else None}
                for rule in self.fees.rules])

    def _owns(self, order_id):
        deployment, _, number = order_id.rpartition('-')
        return deployment == self.deployment_id and re.fullmatch(r'[0-9]{8,}', number) is not None

    def _require_no_conflict(self):
        if self.conflict is not None:
            raise GridConfirmationConflict(f'RECONCILE_REQUIRED: conflicting Grid confirmation {self.conflict}')

    def mark_unknown(self, order_id, *, delivery):
        self._apply_external(delivery, 'unknown', order_id, None)

    def _mark_unknown(self, order_id):
        record = self.records.get(order_id, OrderRecord(
            self.orders[order_id], datetime.min.replace(tzinfo=timezone.utc), self.targets[order_id]))
        self.records[order_id] = unknown(record)
        self.orders[order_id] = self.records[order_id].snapshot

    def confirm(self, event, account, *, delivery):
        validate_identity(event.event_id)
        self._apply_external(delivery, 'confirm', event, account)

    def _confirm(self, event, account):
        key = event.client_order_id
        record = self.records.get(key) or OrderRecord(self.orders[key], event.event_time, self.targets[key])
        self._accept_confirmation(advance(record, event), account)

    def _forget(self, key):
        self.orders.pop(key)
        self.targets.pop(key)
        self.levels.pop(key)
        self.created.pop(key)
        self.records.pop(key, None)
        self.cancellations.discard(key)
        self.expirations.discard(key)

    def _accept_confirmation(self, record, account):
        self._validate_account(account)
        updated = record.snapshot
        key = updated.intent.client_order_id
        old = self.orders[key]
        cancel_requested = key in self.cancellations
        level = self.levels[key] + (1 if old.intent.side == Side.BUY else -1)
        delta = updated.cumulative_filled_quantity - old.cumulative_filled_quantity
        if updated.status in TERMINAL:
            self._forget(key)
        else:
            self.records[key], self.orders[key] = record, updated
        self.account = account
        self._account_risk()
        if delta and self.state == 'RUNNING' and self.pending_rebuild is None and not cancel_requested and self._can_propose():
            if 0 <= level < len(self.prices):
                self._propose(Side.SELL if old.intent.side == Side.BUY else Side.BUY,
                              self.prices[level], delta, level)
        self._settle()

    def _authority(self, state, cursor):
        if (state.account_id, state.market, set(state.instruments)) != (
                self.account_id, self.market, {self.instrument_id}):
            raise ValueError('Paper account deployment mismatch')
        digest = sha256(dumps([cursor, state.sequence, state.fee_rate, state.instruments,
                               state.cash, state.positions, state.quotes]).encode())
        for key, record in state.orders.items():
            # A restart changes presentation to UNKNOWN, preserving confirmed facts.
            snapshot = record.snapshot
            if snapshot.status == OrderStatus.UNKNOWN and snapshot.reconcile_required:
                record = replace(record, snapshot=replace(snapshot, status=record.confirmed_status,
                                                          reconcile_required=False))
            digest.update(dumps([key, record, state.confirmations.get(key)]).encode())
        return digest.hexdigest()

    def _paper_deployment(self, controller):
        return _fingerprint([self.deployment(), asdict(controller.limits)])

    def _read_checkpoint(self, state, cursor, deployment):
        saved = state.runtime.get(_CHECKPOINT)
        if not isinstance(saved, dict) or not isinstance(saved.get('checkpoint'), dict):
            raise ValueError('missing or corrupt Grid checkpoint')
        payload = saved['checkpoint']
        if saved.get('revision') != _fingerprint(payload):
            raise ValueError('corrupt Grid checkpoint')
        if payload['version'] != _CHECKPOINT_VERSION or payload['deployment'] != deployment:
            raise ValueError('incompatible Grid checkpoint deployment')
        if payload['authority'] != self._authority(state, cursor):
            raise ValueError('Grid checkpoint requires authority reconciliation')
        return saved['revision'], payload

    def _write_checkpoint(self, state, cursor, deployment):
        working = {}
        for key, order in self.orders.items():
            proposal = None if key in self.records else order.intent
            working[key] = [self.targets[key], self.levels[key], self.created[key], proposal]
        payload = dict(version=_CHECKPOINT_VERSION, deployment=deployment,
                       authority=self._authority(state, cursor), baseline=self._baseline, turnover=self.account.turnover,
                       decision={name: getattr(self, name) for name in _DECISION_FIELDS},
                       timestamp=self.timestamp.isoformat(), true_ranges=list(self.true_ranges),
                       pending_rebuild=self.pending_rebuild, pending_bar=self.pending_bar, working=working,
                       cancellations=sorted(self.cancellations), expirations=sorted(self.expirations))
        self._revision = _fingerprint(payload)
        state.runtime[_CHECKPOINT] = dict(revision=self._revision, checkpoint=payload)

    def _refresh_paper(self, broker):
        for key in self.records:
            record = broker.state.orders[key]
            self.records[key], self.orders[key] = record, record.snapshot

    def restore(self, controller):
        """Restore the decision snapshot against the current Paper authority."""
        if self.delivery:
            raise ValueError('restore requires fresh Grid execution')
        staged = self._stage()
        with _paper_snapshot(controller) as (broker, store):
            staged._revision, payload = staged._read_checkpoint(
                broker.state, store.event_cursor(), staged._paper_deployment(controller))
            staged._baseline = payload['baseline']
            for name in _DECISION_FIELDS:
                setattr(staged, name, payload['decision'][name])
            staged.timestamp = pd.Timestamp(payload['timestamp'])
            staged.true_ranges.extend(payload['true_ranges'])
            for name in ('pending_rebuild', 'pending_bar'):
                pending = payload[name]
                setattr(staged, name, tuple(pending) if pending is not None else None)
            staged.cancellations.update(payload['cancellations'])
            staged.expirations.update(payload['expirations'])
            for key, (target, level, created, proposal) in payload['working'].items():
                staged.targets[key], staged.levels[key], staged.created[key] = target, level, created
                if proposal is None:
                    record = broker.state.orders[key]
                    staged.records[key], staged.orders[key] = record, record.snapshot
                else:
                    staged.orders[key] = OrderSnapshot(proposal)
            turnover = _historical_turnover(store, after=staged._baseline)
            if turnover != payload['turnover']:
                raise ValueError('Grid checkpoint turnover disagrees with Paper journal')
            staged._turnover = turnover
            staged.account = _project_paper_account(broker, staged.instrument_id, turnover)
        self.__dict__ = staged.__dict__

    def observe_paper(self, controller, event, *, delivery, observed_at):
        """Commit the mark and resulting Grid decisions together."""
        observation = _observation(event)
        quote = Quote(self.instrument_id, observation['price'], observed_at)
        self._paper_command(controller, delivery, 'observe', (observation, quote))

    def complete_bar_paper(self, controller, *, delivery):
        self._paper_command(controller, delivery, 'bar', None)

    def submit_paper(self, controller, order_id, *, delivery):
        """Commit controller admission and the Grid transition together."""
        self._paper_command(controller, delivery, 'submit', order_id)

    def confirm_paper(self, controller, event, *, delivery):
        """Apply a report once or commit a reconciliation block before raising."""
        validate_identity(event.event_id)
        self._paper_command(controller, delivery, 'confirm', event)

    def _paper_command(self, controller, delivery, action, command):
        staged = self._stage()

        def apply():
            broker, store = controller.broker, controller.store
            state = broker.state
            cursor = store.event_cursor()
            deployment = staged._paper_deployment(controller)
            starting = staged._revision is None
            if starting:
                if (staged.delivery or action != 'observe' or _CHECKPOINT in state.runtime
                        or any(key.startswith('grid:') for key in state.runtime)):
                    raise ValueError('Grid checkpoint restore required')
                if any(staged._owns(key) or record.snapshot.status not in TERMINAL
                       for key, record in state.orders.items()):
                    raise ValueError('Grid requires an unused deployment identity and no preexisting working orders')
                staged._baseline = cursor
            else:
                revision, _ = staged._read_checkpoint(state, cursor, deployment)
                if revision != staged._revision:
                    raise ValueError('stale Grid checkpoint revision')
            staged._refresh_paper(broker)
            turnover = staged._turnover
            if not staged._next_delivery(delivery, action, command):
                staged.account = _project_paper_account(broker, staged.instrument_id, turnover)
                return
            staged._require_no_conflict()
            if action == 'observe':
                observation, quote = command
                broker.set_quote(quote)
                account = _project_paper_account(broker, staged.instrument_id, turnover)
                if starting and staged.initial_equity != account.equity:
                    raise ValueError('Grid initial equity differs from Paper authority')
                staged._observe(observation, account)
            elif action == 'submit':
                staged.account = _project_paper_account(broker, staged.instrument_id, turnover)
                order = staged.orders[command]
                if not staged._can_propose() or order.status != OrderStatus.NEW or command in staged.cancellations:
                    raise ValueError('Grid proposal is not submit-ready')
                controller._submit_in_transaction(order.intent)
                account = _project_paper_account(broker, staged.instrument_id, turnover)
                staged._accept_confirmation(state.orders[command], account)
            elif action == 'confirm':
                staged._confirm_paper(broker, store, command)
            else:
                staged.account = _project_paper_account(broker, staged.instrument_id, turnover)
                staged._complete_bar()
            # A proposal that never entered the Paper authority needs no cancel report.
            discarded = staged.cancellations - state.orders.keys()
            if discarded:
                for key in discarded:
                    staged._forget(key)
                staged._settle()
            staged._write_checkpoint(state, store.event_cursor(), deployment)

        controller._atomic(apply)
        self.__dict__ = staged.__dict__
        self._require_no_conflict()

    def _confirm_paper(self, broker, store, event):
        key = event.client_order_id
        if not self._owns(key):
            raise ValueError('confirmation outside Grid deployment')
        record = broker.state.orders[key]
        saved = store.event_payload(event.event_id)
        if saved is not None:
            if saved != dumps(event):
                self.conflict = event.event_id
        else:
            old = record.snapshot
            if record.confirmed_status in TERMINAL and (
                    event.status != record.confirmed_status
                    or event.cumulative_filled_quantity != old.cumulative_filled_quantity
                    or event.cumulative_fee != old.cumulative_fee
                    or event.exchange_order_id != old.exchange_order_id):
                self.conflict = event.event_id
            else:
                broker.apply(event)
                delta = event.cumulative_filled_quantity - old.cumulative_filled_quantity
                if delta:
                    self._turnover += delta * event.fill_price
        account = _project_paper_account(broker, self.instrument_id, self._turnover)
        if saved is None and self.conflict is None and record.confirmed_status not in TERMINAL:
            self._accept_confirmation(broker.state.orders[key], account)
        else:
            self.account = account
