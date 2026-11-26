"""Project owned reductions onto the original durable entry exit-budget holds."""
from decimal import Decimal

from factorlab.trading.contracts import TERMINAL
from factorlab.trading.realtime import identity
from .demo_allocations import DemoAllocations
from .protocol import SafetyBlocked


class DemoInstanceExitBudget:
    def __init__(self, session, runtime):
        self.session, self.runtime = session, runtime
        self.owners = DemoAllocations(session.controller)

    def opening(self, intent, owner):
        if self.owners.owner(intent.client_order_id) != owner:
            raise SafetyBlocked('exit reservation requires durable entry ownership')
        return 'dx' + identity((owner, intent.client_order_id))[:30], intent.quantity * intent.limit_price

    def consumption(self, intent, owner):
        """FIFO is only budget attribution; confirmed inventory remains instance-owned.

        A partial reduction retains the unconsumed hold and its POST slot. Its
        extra POST must fit the existing account limit. Cancelled/unfilled exit
        attempts never refund their already consumed submission budget.
        """
        if self.owners.owner(intent.client_order_id) != owner:
            raise SafetyBlocked('exit consumption requires durable reduction ownership')
        self.owners.admit(intent, instance_id=owner[0], allocation_id=owner[1])
        entries, reduced = [], Decimal(0)
        for client_id, in self.owners.store.db.execute(
                'SELECT client_id FROM demo_order_owners WHERE instance_id=? AND allocation_id=? '
                'AND symbol=? ORDER BY rowid', owner):
            if client_id == intent.client_order_id:
                continue
            record = self.session.gateway.state.orders.get(client_id)
            if record is None:
                continue
            snapshot = record.snapshot
            if snapshot.reconcile_required:
                raise SafetyBlocked('exit budget requires confirmed original order identities')
            if snapshot.intent.reduce_only:
                reduced += snapshot.cumulative_filled_quantity
                if snapshot.status not in TERMINAL:
                    reduced += snapshot.intent.quantity - snapshot.cumulative_filled_quantity
            elif snapshot.cumulative_filled_quantity:
                entries.append((client_id, snapshot.cumulative_filled_quantity))
        remaining, releases = intent.quantity, []
        for entry_id, filled in entries:
            consumed = min(reduced, filled)
            reduced -= consumed
            available = filled - consumed
            if not available:
                continue
            quantity = min(remaining, available)
            hold = self.runtime.db.execute(
                'SELECT notional FROM exit_budget_reservations WHERE entry_id=?', (entry_id,)).fetchone()
            if hold is not None:
                held = Decimal(hold[0])
                amount = held if quantity == available else held * quantity / available
                releases.append((entry_id, amount))
            remaining -= quantity
            if not remaining:
                break
        if remaining:
            raise SafetyBlocked('exit quantity exceeds confirmed owned entry history')
        return tuple(releases)
