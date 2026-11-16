"""Causal regime budgets on the confirmation-driven Grid execution seam."""
from copy import deepcopy
import math

import numpy as np
import pandas as pd

from factorlab.strategies.grid_execution import (
    D, GridExecution, _CHECKPOINT, _fingerprint, _paper_snapshot,
)
from factorlab.trading.contracts import OrderStatus, Side, ZERO


class RegimeBudgetPolicy:
    required_fields = ('close',)
    warmup_sessions = 25
    table = {
        'low_up': (1., 1., False), 'low_down': (.75, .75, False),
        'high_up': (.75, .50, False), 'high_down': (.50, .25, True),
    }

    def __init__(self, volatility_threshold, policy='dynamic', static_scale=1.):
        if not math.isfinite(volatility_threshold) or volatility_threshold < 0:
            raise ValueError('nonnegative finite volatility threshold required')
        if policy not in {'dynamic', 'static', 'neutral'}:
            raise ValueError('unknown budget policy')
        if not math.isfinite(static_scale) or not 0 <= static_scale <= 1:
            raise ValueError('static scale must be between zero and one')
        self.volatility_threshold = volatility_threshold
        self.policy, self.static_scale = policy, static_scale

    def generate_targets(self, bars, calendar):
        returns = np.diff(np.log(bars.close.to_numpy(dtype=float)[-25:]))
        state = ('high' if returns.std(ddof=1) > self.volatility_threshold else 'low')
        state += '_up' if returns.sum() >= 0 else '_down'
        inventory, entry, pause = self.table[state]
        if self.policy != 'dynamic':
            inventory = entry = self.static_scale if self.policy == 'static' else 1.
            pause = False
        return pd.DataFrame([dict(date=calendar[-1], symbol=bars.symbol.iloc[-1],
                                  target_weight=inventory, entry_factor=entry,
                                  pause_buys=pause, state=state)])


class BudgetGrid(GridExecution):
    def __init__(self, *args, volatility_threshold, seed_prices, policy='dynamic',
                 static_scale=1., **kwargs):
        super().__init__(*args, **kwargs)
        prices = tuple(float(p) for p in seed_prices)
        if len(prices) < 25 or any(not math.isfinite(p) or p <= 0 for p in prices):
            raise ValueError('at least 25 positive finite seed closes required')
        self.budget_policy = RegimeBudgetPolicy(volatility_threshold, policy, static_scale)
        self.seed_prices = prices
        self.budget_prices = list(prices[-25:])
        self.budget_events = []
        self.inventory_multiplier = self.entry_factor = D(1)
        self.pause_buys = False
        self.budget_state = None

    def _stage(self):
        staged = super()._stage()
        staged.budget_prices = self.budget_prices.copy()
        staged.budget_events = deepcopy(self.budget_events)
        staged.budget_policy = deepcopy(self.budget_policy)
        return staged

    @property
    def inventory_cap(self):
        return D(self.config.max_inventory) * self.rules.quantity_unit * self.inventory_multiplier

    def _pending_buys(self):
        return [o for o in self.active if o.intent.side == Side.BUY]

    def _new_reservations(self):
        return sum(((o.intent.quantity - o.cumulative_filled_quantity)
                    * max(self.price, o.intent.limit_price or self.price)
                    + self.estimated_cost(Side.BUY, max(self.price, o.intent.limit_price or self.price),
                        o.intent.quantity - o.cumulative_filled_quantity)
                    for o in self._pending_buys() if o.status == OrderStatus.NEW), ZERO)

    def _order_cap(self, target):
        return D(self.config.quantity) * self.rules.quantity_unit * D(target) * self.entry_factor

    def _propose(self, side, target, quantity, level=-1, market=False):
        if side == Side.BUY and self.budget_policy.policy != 'neutral':
            if self.pause_buys:
                return
            pending = sum((o.intent.quantity - o.cumulative_filled_quantity
                           for o in self._pending_buys()), ZERO)
            quantity = min(quantity, self._order_cap(target) / D(target),
                           max(ZERO, self.inventory_cap - self.account.inventory - pending))
            valuation = self.price if market else D(target)
            capacity = (D(self.config.max_cash_committed) * self.inventory_multiplier
                        - self.account.committed - self._new_reservations())
            unit_cost = valuation + self.estimated_cost(Side.BUY, valuation, self.rules.step_size) / self.rules.step_size
            quantity = min(quantity, max(ZERO, capacity / unit_cost))
        return super()._propose(side, target, quantity, level, market)

    def _account_risk(self):
        super()._account_risk()
        if (self.budget_policy.policy != 'neutral' and self.state in {'RUNNING', 'PAUSED'}
                and self.account.inventory > self.inventory_cap):
            self._risk('REGIME_INVENTORY_BUDGET', exit_position=True)

    def _observe(self, event, account):
        self._validate_account(account)
        self._check_observation(event)
        calendar = pd.date_range(end=pd.Timestamp(event['time']), periods=len(self.budget_prices), freq='h')
        frame = pd.DataFrame(dict(date=calendar, symbol=self.instrument_id, close=self.budget_prices))
        target = self.budget_policy.generate_targets(frame, calendar).iloc[-1]
        self.inventory_multiplier, self.entry_factor = D(target.target_weight), D(target.entry_factor)
        self.pause_buys, self.budget_state = bool(target.pause_buys), str(target.state)
        # The selected budget must also govern the initial seed and confirmations.
        self.account, self.price, self.timestamp = account, event['price'], pd.Timestamp(event['time'])
        pending = self._pending_buys()
        total_quantity = sum((o.intent.quantity - o.cumulative_filled_quantity for o in pending), ZERO)
        over_cap = account.inventory + total_quantity > self.inventory_cap
        over_cash = account.committed + self._new_reservations() > D(self.config.max_cash_committed) * self.inventory_multiplier
        for order in pending if self.budget_policy.policy != 'neutral' else ():
            remaining = order.intent.quantity - order.cumulative_filled_quantity
            if (self.pause_buys or over_cap or over_cash
                    or remaining * (order.intent.limit_price or self.price) > self._order_cap(order.intent.limit_price or self.price)):
                self.cancellations.add(order.intent.client_order_id)
        self._account_risk()
        super()._observe(event, account)
        self.budget_events.append(dict(time=event['time'], state=self.budget_state,
            inventory_cap=str(self.inventory_cap), order_cap=str(self._order_cap(self.price)),
            inventory=str(account.inventory), pending_quantity=str(sum((
                o.intent.quantity - o.cumulative_filled_quantity for o in self._pending_buys()), ZERO)),
            pause_buys=self.pause_buys, trigger=self.reason,
            cancellations=sorted(self.cancellations)))
        if event['completed_bar']:
            self.budget_prices = (self.budget_prices + [float(event.get('close', event['price']))])[-25:]

    def deployment(self):
        return super().deployment() | dict(regime_budget=dict(
            volatility_threshold=self.budget_policy.volatility_threshold,
            policy=self.budget_policy.policy, static_scale=self.budget_policy.static_scale,
            seed_prices=list(self.seed_prices)))

    def _write_checkpoint(self, state, cursor, deployment):
        super()._write_checkpoint(state, cursor, deployment)
        saved = state.runtime[_CHECKPOINT]
        saved['checkpoint']['regime_budget'] = dict(prices=self.budget_prices,
            events=self.budget_events, inventory_multiplier=self.inventory_multiplier,
            entry_factor=self.entry_factor, pause_buys=self.pause_buys, state=self.budget_state)
        self._revision = saved['revision'] = _fingerprint(saved['checkpoint'])

    def restore(self, controller):
        super().restore(controller)
        with _paper_snapshot(controller) as (broker, store):
            _, payload = self._read_checkpoint(broker.state, store.event_cursor(), self._paper_deployment(controller))
            budget = payload['regime_budget']
            self.budget_prices, self.budget_events = budget['prices'], budget['events']
            self.inventory_multiplier, self.entry_factor = budget['inventory_multiplier'], budget['entry_factor']
            self.pause_buys, self.budget_state = budget['pause_buys'], budget['state']


def make_budget_factory(*, volatility_threshold: float, seed_prices: list[float],
                        policy='dynamic', static_scale: float = 1.0):
    prices = tuple(seed_prices)

    def factory(config, rules, **kwargs):
        return BudgetGrid(config, rules, volatility_threshold=volatility_threshold,
                          seed_prices=prices, policy=policy, static_scale=static_scale, **kwargs)

    return factory
