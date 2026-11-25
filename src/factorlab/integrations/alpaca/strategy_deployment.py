"""Instance-scoped deployment operations using the existing account owner."""
import json

from factorlab.strategies.package import manifest_spec
from factorlab.trading.portfolio_execution import TERMINAL
from factorlab.trading.storage import decode


class AlpacaDeployment:
    def __init__(self, instance, view, *, boot_id):
        self.instance, self.view, self.boot_id = instance, view, boot_id
        self.paused = False

    def quiesce(self):
        self.paused = True

    def reconcile(self):
        instance, store = self.instance, self.instance.runner.store
        owned = []
        for plan in instance.plans():
            for child in store.orders(plan['id']):
                snapshot = child['record'].snapshot
                if snapshot.status not in TERMINAL or child['uncertain']:
                    instance.runner.executor.cancel(plan['id'], snapshot.intent.client_order_id)
            children = store.orders(plan['id'])
            if plan['phase'] != 'COMPLETE' and all(
                    child['record'].snapshot.status in TERMINAL and not child['uncertain']
                    for child in children):
                instance.runner.executor.abandon(plan['id'])
            for child in children:
                snapshot = child['record'].snapshot
                owned.append(dict(client_id=snapshot.intent.client_order_id,
                    status=snapshot.status.value, reduce_only=snapshot.intent.reduce_only))
        account = instance.runner.adapter.recover()
        account_orders = [decode(json.loads(payload)) for payload, in store.db.execute(
            'SELECT o.payload FROM portfolio_orders o JOIN portfolio_plans p ON p.id=o.plan '
            'WHERE p.account=?', (instance.binding['account_id'],))]
        tracked = {child['record'].snapshot.intent.client_order_id for child in account_orders}
        unknown = [child['record'].snapshot.intent.client_order_id
                   for child in account_orders if child['uncertain']]
        untracked = [order.intent.client_order_id for order in account.open_orders
                     if order.intent.client_order_id not in tracked]
        holdings = store.holdings(instance.binding['account_id'], instance.binding['instance_id'])
        return dict(reconciled=account.reconciled, unknown=unknown, untracked_orders=untracked,
                    orders=owned, positions=[dict(symbol=symbol, quantity=quantity)
                        for symbol, quantity in holdings.items() if quantity])

    def watermark(self):
        return self.view.snapshot()['watermark'] or '0'

    def cancel(self, client_id):
        for plan in self.instance.plans():
            if any(child['record'].snapshot.intent.client_order_id == client_id
                   for child in self.instance.runner.store.orders(plan['id'])):
                self.instance.runner.executor.cancel(plan['id'], client_id)
                return
        raise ValueError('cannot cancel another instance order')

    def reduce(self, switch_id, version):
        raise ValueError('Alpaca deployment requires wait_flat migration policy')

    def prepare(self, version):
        spec = manifest_spec(self.view.manifest(version))
        allocation = self.instance.allocation(self.instance.runner.adapter.recover())
        self.instance.data.assets.qualify(spec, self.instance.binding, allocation)
        if self.instance.runner.store.db.execute(
                'SELECT 1 FROM capital_accounts WHERE account=?',
                (self.instance.binding['account_id'],)).fetchone() is not None:
            self.instance.runner.executor.capital_policy.qualify(spec, self.instance.binding, allocation)

    def activated(self, version):
        self.paused = False
