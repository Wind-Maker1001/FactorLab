"""Synthetic, long-only grid state machine; no exchange connectivity."""
from dataclasses import asdict, dataclass
from datetime import date
import math

import pandas as pd

from factorlab.backtest.execution import PositionBook
from factorlab.backtest.fees import FeeRule, FeeSchedule
from factorlab.errors import ConfigurationError


@dataclass(frozen=True)
class GridConfig:
    mode: str = "arithmetic"
    lower: float = 80.0
    upper: float = 120.0
    levels: int = 8
    initial_cash: float = 10000.0
    initial_inventory: int = 20
    initial_price: float = 100.0
    quantity: int = 2
    budget: float | None = None
    max_inventory: int = 100
    max_cash_committed: float = 10000.0
    atr_window: int = 3
    atr_multiplier: float = 1.0
    rebuild_threshold: float = 2.0
    min_rebuild_interval: int = 3
    max_turnover: float = 10.0
    breakout: str = "exit"
    max_drawdown: float = 0.2
    minimum_price: float = 0.01
    minimum_quantity: int = 1
    fee_bps: float = 3.0
    slippage_bps: float = 2.0
    order_ttl: int = 100
    center_rule: str = "last_close"

    def __post_init__(self):
        if self.mode not in {"arithmetic", "geometric", "atr"}:
            raise ConfigurationError("unknown grid mode")
        if self.breakout not in {"pause", "exit", "rebuild"} or self.center_rule != "last_close":
            raise ConfigurationError("unsupported breakout or center rule")
        for name in ("levels", "quantity", "max_inventory", "atr_window", "min_rebuild_interval", "minimum_quantity", "order_ttl"):
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise ConfigurationError(name + " must be a positive integer")
        if type(self.initial_inventory) is not int or not 0 <= self.initial_inventory <= self.max_inventory:
            raise ConfigurationError("invalid initial inventory")
        for name in ("lower", "upper", "initial_cash", "initial_price", "max_cash_committed", "atr_multiplier", "rebuild_threshold", "max_turnover", "minimum_price", "max_drawdown"):
            value = getattr(self, name)
            if isinstance(value, bool) or not math.isfinite(value) or value <= 0:
                raise ConfigurationError(name + " must be finite and positive")
        if not self.minimum_price <= self.lower < self.initial_price < self.upper or self.max_drawdown > 1:
            raise ConfigurationError("invalid price range or drawdown")
        if self.initial_inventory * self.initial_price > self.max_cash_committed:
            raise ConfigurationError("initial inventory exceeds committed-capital limit")
        if self.budget is not None and (not math.isfinite(self.budget) or self.budget <= 0):
            raise ConfigurationError("budget must be positive")
        for value in (self.fee_bps, self.slippage_bps):
            if not math.isfinite(value) or value < 0:
                raise ConfigurationError("cost rates must be nonnegative")


@dataclass
class GridOrder:
    id: str
    side: str
    price: float
    quantity: int
    level: int
    created: int
    remaining: int
    status: str = "SUBMITTED"
    reason: str = ""
    cash_per_unit: float = 0.0
    market: bool = False


class GridStrategy:
    """Orders reserve cash/stock; PositionBook remains the inventory authority.

    Quantities are integer spot units, immediately sellable (not A-share T+1).
    A fill pays FeeSchedule costs, including cash-equivalent slippage, at the
    limit price without favorable gap improvement. Each partial fill is charged
    separately; reservations cover even one-unit partial-fill minimum fees.
    """

    symbol = "SYNTHETIC"
    active_states = {"OPEN", "PARTIALLY_FILLED"}

    def __init__(self, config: GridConfig, fees: FeeSchedule | None = None):
        self.config = config
        self.fees_schedule = fees or FeeSchedule([FeeRule(
            date(1900, 1, 1), commission_bps=config.fee_bps,
            minimum_commission_cny=0, slippage_bps=config.slippage_bps)])
        self.book = PositionBook()
        self.book.buy(self.symbol, "1900-01-01", config.initial_inventory)
        self.cash = config.initial_cash
        self.cost_basis = config.initial_inventory * config.initial_price
        self.initial_equity = self.cash + self.cost_basis
        self.peak = self.initial_equity
        self.realized_pnl = self.realized_grid_pnl = self.fees = self.slippage = self.notional = 0.0
        self.orders = {}
        self.events = []
        self.snapshots = []
        self.inputs = []
        self.seen = {}
        self.sequence = 0
        self.rebuilds = 0
        self.last_rebuild = 0
        self.state = "WARMUP" if config.mode == "atr" else "RUNNING"
        self.prices = self.grid_prices(config.lower, config.upper)
        self.center = config.initial_price
        self.true_ranges = []
        self.previous_close = None
        self.initialized = False
        self.time = None
        self.price = config.initial_price
        self.max_inventory_observed = self.inventory
        self.max_committed_observed = self.committed
        self.max_reserved_cash_observed = 0.0

    def grid_prices(self, lower, upper, spacing=None):
        c = self.config
        if spacing is not None:
            return [lower + i * spacing for i in range(c.levels + 1)]
        if c.mode == "geometric":
            ratio = (upper / lower) ** (1 / c.levels)
            return [lower * ratio ** i for i in range(c.levels + 1)]
        return [lower + (upper - lower) * i / c.levels for i in range(c.levels + 1)]

    @property
    def inventory(self):
        return self.book.total_shares(self.symbol)

    @property
    def active_orders(self):
        return [o for o in self.orders.values() if o.status in self.active_states]

    @property
    def reserved_cash(self):
        return sum(o.remaining * o.cash_per_unit for o in self.active_orders if o.side == "buy")

    @property
    def reserved_inventory(self):
        return sum(o.remaining for o in self.active_orders if o.side == "sell")

    @property
    def committed(self):
        return self.cost_basis + self.reserved_cash

    def _log(self, kind, **values):
        self.max_inventory_observed = max(self.max_inventory_observed, self.inventory)
        self.max_committed_observed = max(self.max_committed_observed, self.committed)
        self.max_reserved_cash_observed = max(self.max_reserved_cash_observed, self.reserved_cash)
        self.events.append(dict(sequence=len(self.events) + 1, event_id=self.current_id,
                                time=self.time, kind=kind, **values))

    def _transition(self, order, status, reason=""):
        allowed = {"SUBMITTED": {"ACCEPTED", "REJECTED"}, "ACCEPTED": {"OPEN"},
                   "OPEN": {"PARTIALLY_FILLED", "FILLED", "CANCELLED", "EXPIRED"},
                   "PARTIALLY_FILLED": {"PARTIALLY_FILLED", "FILLED", "CANCELLED", "EXPIRED"}}
        if status not in allowed.get(order.status, set()):
            raise ConfigurationError("illegal order transition")
        order.status, order.reason = status, reason
        self._log(status, order=order.id, side=order.side, price=order.price,
                  quantity=order.quantity, remaining=order.remaining, reason=reason,
                  cash=self.cash, inventory=self.inventory,
                  reserved_cash=self.reserved_cash, reserved_inventory=self.reserved_inventory)

    def submit(self, side, price, quantity, level=-1, market=False):
        """Submit during an event; rejection remains in the audit trail."""
        if side not in {"buy", "sell"} or type(quantity) is not int or quantity < 1 or not math.isfinite(price) or price <= 0:
            raise ConfigurationError("invalid order")
        order = GridOrder(f"grid-{len(self.orders) + 1:08d}", side, price, quantity,
                          level, self.sequence, quantity, market=market)
        self.orders[order.id] = order
        self._log("SUBMITTED", order=order.id, side=side, price=price, quantity=quantity)
        unit_cost = self.fees_schedule.calculate(self.time, side, price).total
        order.cash_per_unit = price + unit_cost
        pending_buys = sum(o.remaining for o in self.active_orders if o.side == "buy")
        reason = ""
        if price < self.config.minimum_price or quantity < self.config.minimum_quantity:
            reason = "MINIMUM_ORDER"
        elif side == "buy" and self.inventory + pending_buys + quantity > self.config.max_inventory:
            reason = "MAX_INVENTORY"
        elif side == "buy" and self.reserved_cash + quantity * order.cash_per_unit > self.cash + 1e-9:
            reason = "INSUFFICIENT_CASH"
        elif side == "buy" and self.committed + quantity * order.cash_per_unit > self.config.max_cash_committed + 1e-9:
            reason = "MAX_CASH_COMMITTED"
        elif side == "sell" and quantity > self.inventory - self.reserved_inventory:
            reason = "INSUFFICIENT_INVENTORY"
        elif not market and 0 <= level < len(self.prices):
            adjacent = level + (1 if side == "buy" else -1)
            if 0 <= adjacent < len(self.prices):
                spread = abs(self.prices[adjacent] - price)
                pair_cost = unit_cost + self.fees_schedule.calculate(
                    self.time, "sell" if side == "buy" else "buy", self.prices[adjacent]).total
                if spread <= pair_cost:
                    reason = "FEE_EROSION"
        self._transition(order, "REJECTED" if reason else "ACCEPTED", reason)
        if not reason:
            self._transition(order, "OPEN")
        return order

    def cancel(self, order_id, expired=False, reason=""):
        order = self.orders[order_id]
        if order.status in self.active_states:
            self._transition(order, "EXPIRED" if expired else "CANCELLED", reason)

    def _cancel_all(self):
        for order in list(self.active_orders):
            self.cancel(order.id)

    def _quantity(self, price):
        return self.config.quantity if self.config.budget is None else int(self.config.budget // price)

    def _seed(self):
        for i, price in enumerate(self.prices):
            if price != self.price:
                quantity = self._quantity(price)
                if quantity:
                    self.submit("buy" if price < self.price else "sell", price, quantity, i)

    def _rebuild(self, center, spacing=None):
        self._cancel_all()
        c = self.config
        if spacing is None:
            if c.mode == "geometric":
                ratio = c.upper / c.lower
                lower, upper = center / math.sqrt(ratio), center * math.sqrt(ratio)
            else:
                width = c.upper - c.lower
                lower, upper = center - width / 2, center + width / 2
        else:
            lower, upper = center - c.levels * spacing / 2, center + c.levels * spacing / 2
        if lower < c.minimum_price:
            self.state = "PAUSED"
            self._log("RISK", reason="INVALID_RANGE")
            return
        self.prices = self.grid_prices(lower, upper, spacing)
        self.center, self.last_rebuild = center, self.sequence
        self.rebuilds += 1
        self.state = "RUNNING"
        self._log("REBUILD", prices=self.prices.copy(), center=center)
        self._seed()

    def _risk(self, reason, exit_position=False):
        self._cancel_all()
        self.state = "EXITING" if exit_position and self.inventory else "PAUSED"
        self._log("RISK", reason=reason, state=self.state)
        if self.state == "EXITING":
            self.submit("sell", self.config.minimum_price, self.inventory, market=True)

    def _fill(self, order, quantity):
        price = self.price if order.market else order.price
        amount = price * quantity
        costs = self.fees_schedule.calculate(self.time, order.side, amount)
        if order.side == "buy":
            self.cash -= amount + costs.total
            self.cost_basis += amount + costs.total
            self.book.buy(self.symbol, self.time, quantity)
        else:
            basis = self.cost_basis * quantity / self.inventory
            self.book.sell(self.symbol, self.time, quantity, t_plus_one=False)
            self.cost_basis -= basis
            self.cash += amount - costs.total
            realized = amount - costs.total - basis
            self.realized_pnl += realized
            if not order.market:
                self.realized_grid_pnl += realized
        self.fees += costs.total - costs.slippage
        self.slippage += costs.slippage
        self.notional += amount
        order.remaining -= quantity
        self._log("TRADE", order=order.id, side=order.side, price=price, quantity=quantity,
                  **costs.to_dict(), cash=self.cash, inventory=self.inventory)
        self._transition(order, "FILLED" if not order.remaining else "PARTIALLY_FILLED")
        if not order.market and self.state == "RUNNING":
            level = order.level + (1 if order.side == "buy" else -1)
            if 0 <= level < len(self.prices):
                self.submit("sell" if order.side == "buy" else "buy", self.prices[level], quantity, level)
        if order.market and not self.inventory:
            self.state = "EXITED"

    def on_event(self, event):
        """Consume a uniquely identified, time-ordered observed-price event.

        OHLC-only events use only their explicitly observed close, never extrema
        for matching. ``completed_bar`` adds high/low/close to ATR *after* fills.
        Volume is a shared integer fill budget, not a queue-position estimate.
        """
        event = dict(event)
        identity = event["id"]
        if identity in self.seen:
            if event != self.seen[identity]:
                raise ConfigurationError("event identity reused with different payload")
            return
        if not isinstance(identity, str) or not identity:
            raise ConfigurationError("event id must be a nonempty string")
        action = event.get("action", "observe")
        if action not in {"observe", "cancel"}:
            raise ConfigurationError("unsupported event action")
        if action == "cancel" and event["order_id"] not in self.orders:
            raise ConfigurationError("unknown cancel order")
        timestamp = pd.Timestamp(event["time"])
        price, volume = event["price"], event["volume"]
        if pd.isna(timestamp) or timestamp.tzinfo is not None or (self.time is not None and timestamp < pd.Timestamp(self.time)):
            raise ConfigurationError("events must have ordered timezone-free times")
        if not math.isfinite(price) or price <= 0 or type(volume) is not int or volume < 0:
            raise ConfigurationError("invalid event price or volume")
        if event.get("completed_bar"):
            high, low, close = (event[k] for k in ("high", "low", "close"))
            if not all(math.isfinite(v) and v > 0 for v in (high, low, close)) or not low <= close <= high or price != close:
                raise ConfigurationError("invalid completed bar")
        self.seen[identity] = event
        self.inputs.append(event)
        self.sequence += 1
        self.current_id, self.time, self.price = identity, timestamp.isoformat(), float(price)
        self._log("OBSERVATION", input=event)
        if event.get("completed_bar"):
            self._log("INTRABAR_MATCHING", status="UNKNOWN", reason="OHLC_ORDER_UNKNOWN")
        if not self.initialized:
            self.initialized = True
            if self.state == "RUNNING":
                self._seed()
        if action == "cancel":
            self.cancel(event["order_id"], reason="USER_CANCEL")
        c = self.config
        equity = self.cash + self.inventory * price
        self.peak = max(self.peak, equity)
        if self.state in {"RUNNING", "WARMUP"}:
            if 1 - equity / self.peak >= c.max_drawdown:
                self._risk("MAX_DRAWDOWN")
            elif self.notional / self.initial_equity >= c.max_turnover:
                self._risk("MAX_TURNOVER")
            elif self.state == "RUNNING" and not self.prices[0] <= price <= self.prices[-1]:
                if c.breakout == "rebuild":
                    if self.sequence - self.last_rebuild >= c.min_rebuild_interval:
                        spacing = sum(self.true_ranges[-c.atr_window:]) / c.atr_window * c.atr_multiplier if c.mode == "atr" else None
                        self._rebuild(price, spacing)
                    else:
                        self._risk("BREAKOUT_REBUILD_INTERVAL")
                else:
                    self._risk("BREAKOUT", c.breakout == "exit")
        for order in list(self.active_orders):
            if not order.market and self.sequence - order.created >= c.order_ttl:
                self.cancel(order.id, expired=True)
                continue
            if order.created == self.sequence or not volume:
                continue
            touched = order.market or (price <= order.price if order.side == "buy" else price >= order.price)
            if not touched:
                continue
            quantity = min(order.remaining, volume)
            if not order.market:
                execution_price = order.price
                capacity = math.floor(max(0, c.max_turnover * self.initial_equity - self.notional) / execution_price + 1e-12)
                quantity = min(quantity, capacity)
                if not quantity:
                    self._risk("MAX_TURNOVER")
                    break
            fill_price = price if order.market else order.price
            amount = fill_price * quantity
            fee = self.fees_schedule.calculate(self.time, order.side, amount).total
            released = order.cash_per_unit * quantity if order.side == "buy" else 0
            cash_change = -amount - fee if order.side == "buy" else amount - fee
            if self.cash + cash_change < self.reserved_cash - released - 1e-9:
                self.cancel(order.id, reason="INSUFFICIENT_CASH_AT_FILL")
                continue
            if order.side == "buy" and self.committed + amount + fee - released > c.max_cash_committed + 1e-9:
                self.cancel(order.id, reason="MAX_CASH_COMMITTED_AT_FILL")
                continue
            self._fill(order, quantity)
            volume -= quantity
        if self.state == "RUNNING" and 1 - (self.cash + self.inventory * price) / self.peak >= c.max_drawdown:
            self._risk("MAX_DRAWDOWN")
        if event.get("completed_bar"):
            previous = self.previous_close
            tr = high - low if previous is None else max(high - low, abs(high - previous), abs(low - previous))
            self.true_ranges.append(tr)
            self.previous_close = close
            if c.mode == "atr" and self.state in {"WARMUP", "RUNNING"} and len(self.true_ranges) >= c.atr_window:
                spacing = sum(self.true_ranges[-c.atr_window:]) / c.atr_window * c.atr_multiplier
                if spacing > 0 and (self.state == "WARMUP" or (abs(close - self.center) >= c.rebuild_threshold * spacing and self.sequence - self.last_rebuild >= c.min_rebuild_interval)):
                    self._rebuild(close, spacing)
        equity = self.cash + self.inventory * price
        self.peak = max(self.peak, equity)
        snapshot = dict(time=self.time, equity=equity, cash=self.cash, inventory=self.inventory,
                        reserved_cash=self.reserved_cash, reserved_inventory=self.reserved_inventory,
                        realized_pnl=self.realized_pnl, realized_grid_pnl=self.realized_grid_pnl,
                        unrealized_pnl=self.inventory * price - self.cost_basis,
                        fees=self.fees, slippage=self.slippage, turnover=self.notional / self.initial_equity,
                        committed=self.committed, state=self.state)
        self.snapshots.append(snapshot)
        self._log("ACCOUNT", **{k: v for k, v in snapshot.items() if k != "time"})

    def replay_payload(self):
        """Serializable inputs include the effective fee rules, not just defaults."""
        rules = [dict(asdict(r), effective_from=r.effective_from.isoformat(),
                      effective_to=r.effective_to.isoformat() if r.effective_to else None)
                 for r in self.fees_schedule.rules]
        return dict(config=asdict(self.config), fee_rules=rules, inputs=self.inputs)

    @classmethod
    def replay(cls, payload):
        strategy = cls(GridConfig(**payload["config"]), FeeSchedule(FeeRule(**r) for r in payload["fee_rules"]))
        for event in payload["inputs"]:
            strategy.on_event(event)
        return strategy
