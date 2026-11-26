"""Instance inventory projections over the existing confirmed account order journal."""
from decimal import Decimal as D
import json

from factorlab.trading.contracts import Side, TERMINAL
from factorlab.trading.storage import decode
from .protocol import SafetyBlocked


class DemoAllocations:
    def __init__(self, controller):
        self.controller = controller
        self.store = controller.store
        self.gateway = controller.broker
        self.store.db.execute('''CREATE TABLE IF NOT EXISTS demo_order_owners (
            client_id TEXT PRIMARY KEY, instance_id TEXT NOT NULL,
            allocation_id TEXT NOT NULL, symbol TEXT NOT NULL)''')

    def owner(self, client_id):
        return self.store.db.execute('SELECT instance_id,allocation_id,symbol FROM demo_order_owners WHERE client_id=?',
                                     (client_id,)).fetchone()

    def claim(self, intent, *, instance_id, allocation_id):
        """Persist ownership before the controller can attempt an order."""
        with self.store.transaction():
            self.claim_in_transaction(intent, instance_id=instance_id, allocation_id=allocation_id)

    def claim_in_transaction(self, intent, *, instance_id, allocation_id):
        if not instance_id or not allocation_id:
            raise ValueError('stable instance and allocation identities required')
        desired = (instance_id, allocation_id, intent.instrument_id)
        self.store.record_metadata('demo:owned-intent:' + intent.client_order_id, intent)
        self.store.db.execute('INSERT OR IGNORE INTO demo_order_owners VALUES(?,?,?,?)',
            (intent.client_order_id, *desired))
        if self.owner(intent.client_order_id) != desired:
            raise SafetyBlocked('order identity belongs to a different strategy instance')

    def adopt_portfolio_task(self, task_id, *, instance_id, allocation_id):
        """Attribute original batch identities without adopting observed foreign positions."""
        from factorlab.trading.realtime import identity
        key = 'portfolio:' + identity((allocation_id, task_id))
        raw = self.store.event_payload(key)
        if raw is None:
            raise SafetyBlocked('existing portfolio task journal required')
        return self._adopt_portfolio_keys([key], instance_id=instance_id, allocation_id=allocation_id)

    def adopt_portfolio_history(self, *, instance_id, allocation_id):
        """Recover all original task batches belonging to one declared allocation."""
        keys = [key for key, raw in self.store.db.execute(
            "SELECT id,payload FROM events WHERE id LIKE 'portfolio:%' ORDER BY rowid")
            if key.count(':') == 1 and decode(json.loads(raw))['allocation'] == allocation_id]
        return self._adopt_portfolio_keys(keys, instance_id=instance_id, allocation_id=allocation_id)

    def _adopt_portfolio_keys(self, keys, *, instance_id, allocation_id):
        intents = []
        for key in keys:
            intents.extend(self._portfolio_intents(key, allocation_id))
        with self.store.transaction():
            for intent in intents:
                self.claim_in_transaction(intent, instance_id=instance_id, allocation_id=allocation_id)
        return tuple(intent.client_order_id for intent in intents)

    def _portfolio_intents(self, key, allocation_id):
        target = decode(json.loads(self.store.event_payload(key)))
        if target['allocation'] != allocation_id:
            raise SafetyBlocked('legacy portfolio allocation mismatch')
        intents = []
        prefix = key + ':batch:'
        batches = []
        for event_id, raw in self.store.db.execute(
                'SELECT id,payload FROM events WHERE id LIKE ?', (prefix + '%',)):
            suffix = event_id[len(prefix):]
            if not suffix.isascii() or not suffix.isdecimal() or str(int(suffix)) != suffix:
                raise SafetyBlocked('invalid original portfolio batch identity')
            batches.append((int(suffix), raw))
        batches.sort()
        if [index for index, _ in batches] != list(range(len(batches))):
            raise SafetyBlocked('original portfolio batch history has a gap')
        for _, raw in batches:
            for intent in decode(json.loads(raw)):
                if intent.instrument_id not in target['owned']:
                    raise SafetyBlocked('legacy batch exceeds declared ownership')
                record = self.gateway.state.orders.get(intent.client_order_id)
                if record is not None and record.snapshot.intent != intent:
                    raise SafetyBlocked('legacy portfolio intent conflicts with account journal')
                intents.append(intent)
        return intents

    def inventory(self, instance_id, symbol):
        capital = getattr(self.controller, 'demo_portfolio_store', None)
        if capital is not None and capital.allocation(self.controller.demo_account_id, instance_id) is not None:
            return capital.holdings(self.controller.demo_account_id, instance_id).get(symbol, D(0))
        quantity = D(0)
        for key, in self.store.db.execute('SELECT client_id FROM demo_order_owners WHERE instance_id=? AND symbol=?',
                                         (instance_id, symbol)):
            record = self.gateway.state.orders.get(key)
            if record is not None:
                snapshot = record.snapshot
                quantity += snapshot.cumulative_filled_quantity * (1 if snapshot.intent.side is Side.BUY else -1)
        return quantity

    def admit(self, intent, *, instance_id, allocation_id):
        """No opposite instance exposure; reductions consume only this instance's fills."""
        owner = self.owner(intent.client_order_id)
        if owner != (instance_id, allocation_id, intent.instrument_id):
            raise SafetyBlocked('order requires persisted instance ownership')
        saved = decode(json.loads(self.store.event_payload('demo:owned-intent:' + intent.client_order_id)))
        if saved != intent:
            raise SafetyBlocked('order differs from its immutable strategy intent')
        own = self.inventory(instance_id, intent.instrument_id)
        sign = 1 if intent.side is Side.BUY else -1
        pending = D(0)
        for key, record in self.gateway.state.orders.items():
            snapshot = record.snapshot
            if (key == intent.client_order_id or snapshot.intent.instrument_id != intent.instrument_id
                    or snapshot.status in TERMINAL):
                continue
            order_owner = self.owner(key)
            if order_owner is None:
                raise SafetyBlocked('unattributed active order requires legacy recovery')
            if order_owner[0] == instance_id:
                if snapshot.intent.reduce_only:
                    pending += snapshot.intent.quantity - snapshot.cumulative_filled_quantity
            elif not snapshot.intent.reduce_only and (1 if snapshot.intent.side is Side.BUY else -1) != sign and not intent.reduce_only:
                raise SafetyBlocked('opposite strategy pending exposure is not supported')
        if intent.reduce_only:
            if own * sign >= 0 or intent.quantity + pending > abs(own):
                raise SafetyBlocked('reduction exceeds strategy-owned inventory')
        else:
            for other, in self.store.db.execute('SELECT DISTINCT instance_id FROM demo_order_owners WHERE symbol=? AND instance_id<>?',
                                               (intent.instrument_id, instance_id)):
                if self.inventory(other, intent.instrument_id) * sign < 0:
                    raise SafetyBlocked('opposite strategy exposure is not supported')
            if own * sign < 0:
                raise SafetyBlocked('closing strategy inventory requires reduce-only')

    def require_cancel(self, client_id, *, instance_id, allocation_id):
        owner = self.owner(client_id)
        if owner is None or owner[:2] != (instance_id, allocation_id):
            raise SafetyBlocked('cancellation belongs to another strategy instance')
