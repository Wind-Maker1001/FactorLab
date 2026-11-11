"""Order-only observations; account projections stay in the shared trading core."""
from .protocol import Unavailable
from .reports import incremental


class OrderRecovery:
    def __init__(self, *, max_orders=1000):
        if type(max_orders) is not int or not 1 <= max_orders <= 1000:
            raise ValueError("order recovery budget must be 1–1000")
        self.max_orders = max_orders
        self.reports = {}
        self.clients = {}
        self.blocked = set()

    def register(self, client_order_id, order_id):
        if client_order_id not in self.clients and len(self.clients) >= self.max_orders:
            raise Unavailable("ORDER_RECOVERY_BUDGET_EXHAUSTED")
        if (client_order_id in self.clients and self.clients[client_order_id] != order_id) or (
                order_id in self.clients.values() and self.clients.get(client_order_id) != order_id):
            raise Unavailable("ORDER_IDENTITY_CONFLICT")
        self.clients[client_order_id] = order_id

    def uncertain(self, client_order_id):
        self.blocked.add(client_order_id)

    def disconnect(self):
        self.blocked.update(self.clients)

    def accept(self, client_order_id, report, *, query=False):
        if client_order_id not in self.clients or self.clients[client_order_id] != report.order_id:
            self.uncertain(client_order_id)
            raise Unavailable("UNREGISTERED_ORDER_IDENTITY")
        if report.client_order_id is not None and report.client_order_id != client_order_id:
            self.uncertain(client_order_id)
            raise Unavailable("CLIENT_IDENTITY_CONFLICT")
        old = self.reports.get(client_order_id)
        try:
            delta = incremental(old, report)
            if old is not None and old.updated_at == report.updated_at and old != report:
                raise Unavailable("SAME_VERSION_QUERY_CONFLICT")
        except Unavailable:
            self.uncertain(client_order_id)
            raise
        if client_order_id in self.blocked and not query:
            raise Unavailable("QUERY_REQUIRED_AFTER_UNCERTAIN_OUTCOME")
        self.reports[client_order_id] = report
        if query:
            self.blocked.discard(client_order_id)
        return None if report == old else delta

    def resubmit(self, client_order_id):
        # Existing exchange identities are queries, never replacement purchases.
        if client_order_id in self.clients or client_order_id in self.blocked:
            raise Unavailable("EXISTING_OR_UNKNOWN_ORDER_QUERY_ONLY")
