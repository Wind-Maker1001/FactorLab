"""Research-only FIFO inventory gates on the existing Paper Grid lifecycle."""
from dataclasses import dataclass, replace
from datetime import timedelta
from decimal import Decimal

from factorlab.strategies.grid_execution import GridExecution
from factorlab.trading.contracts import Side

ZERO = Decimal(0)


@dataclass
class Lot:
    time: object
    quantity: Decimal
    price: Decimal


class InventoryRiskGrid(GridExecution):
    def __init__(self, *args, inventory_risk, **kwargs):
        super().__init__(*args, **kwargs)
        if inventory_risk not in {'age', 'loss', 'cap'}:
            raise ValueError('unknown inventory risk rule')
        self.inventory_risk = inventory_risk
        self.lots = []
        self.realized_net = ZERO
        self.buy_blocked = False
        self.risk_events = []

    def _stage(self):
        staged = super()._stage()
        staged.lots = [replace(lot) for lot in self.lots]
        staged.risk_events = self.risk_events.copy()
        return staged

    def _propose(self, side, target, quantity, level=-1, market=False):
        if side == Side.BUY:
            if self.buy_blocked:
                return
            if self.inventory_risk == 'cap':
                pending = sum((o.intent.quantity - o.cumulative_filled_quantity
                               for o in self.active if o.intent.side == Side.BUY), ZERO)
                if self.account.inventory + pending + quantity > Decimal('.2'):
                    return
        return super()._propose(side, target, quantity, level, market)

    def _admit_paper_fill(self, broker, order, quantity, price):
        if order.intent.side == Side.BUY and self.buy_blocked:
            return ZERO
        return super()._admit_paper_fill(broker, order, quantity, price)

    def fill_paper(self, controller, order_id, quantity, price=None, *, delivery):
        self.execution_price = price
        return super().fill_paper(controller, order_id, quantity, price, delivery=delivery)

    def _accept_confirmation(self, record, account):
        updated = record.snapshot
        old = self.orders[updated.intent.client_order_id]
        delta = updated.cumulative_filled_quantity - old.cumulative_filled_quantity
        self.realized_net -= updated.cumulative_fee - old.cumulative_fee
        if delta:
            if updated.intent.side == Side.BUY:
                self.lots.append(Lot(record.observed_at, delta, self.execution_price))
            else:
                remaining = delta
                while remaining:
                    lot = self.lots[0]
                    consumed = min(lot.quantity, remaining)
                    self.realized_net += consumed * (self.execution_price - lot.price)
                    lot.quantity -= consumed
                    remaining -= consumed
                    if not lot.quantity:
                        self.lots.pop(0)
        super()._accept_confirmation(record, account)

    def _account_risk(self):
        super()._account_risk()
        if self.state != 'RUNNING':
            return
        # Grid timestamps label the completed hour; Paper executions use its end.
        now = self.timestamp.replace(tzinfo=None) + timedelta(hours=1, microseconds=-1)
        age = max(((now - lot.time.replace(tzinfo=None)).total_seconds() / 3600
                   for lot in self.lots), default=0)
        unrealized = sum((lot.quantity * (self.price - lot.price) for lot in self.lots), ZERO)
        remaining = Decimal('.005') * self.account.equity + self.realized_net + min(unrealized, ZERO)
        blocked = self.inventory_risk == 'age' and age >= 100
        exit_reason = ('INVENTORY_AGE' if self.inventory_risk == 'age' and age >= 200
                       else 'LOSS_BUDGET' if self.inventory_risk == 'loss'
                       and self.lots and remaining <= ZERO else None)
        if blocked != self.buy_blocked or exit_reason:
            self.risk_events.append(dict(time=now.isoformat(), age_hours=age,
                equity=str(self.account.equity), realized_net=str(self.realized_net),
                unrealized=str(unrealized), remaining_budget=str(remaining),
                buy_blocked=blocked or bool(exit_reason), exit_reason=exit_reason))
        self.buy_blocked = blocked or bool(exit_reason)
        if exit_reason:
            self._risk(exit_reason, exit_position=True)
        elif blocked:
            self.cancellations.update(o.intent.client_order_id for o in self.active
                                      if o.intent.side == Side.BUY)
