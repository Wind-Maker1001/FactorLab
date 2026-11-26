"""Shared portfolio execution through the existing Demo account owner."""
from dataclasses import replace

from factorlab.trading.portfolio_execution import SubmissionNotAttempted
from .demo_allocations import DemoAllocations
from .protocol import OutcomeUnknown, SafetyBlocked


class DemoPortfolioAdapter:
    """Reserve full order notional against explicitly funded instance capital.

    Exchange leverage does not enlarge instance buying power. Official margin
    facts remain separate from this capital constraint. The original 7 USDT grid
    and 20 USDT portfolio sleeves must be adopted before funding new instances;
    account-wide dispatch and exit budgets remain owned by DemoRuntime.
    """

    def __init__(self, session, *, capability):
        self.session = session
        self.capability = capability
        self.owners = DemoAllocations(session.controller)

    def read_account(self):
        return self.session.gateway.read_account()

    def funding_events(self):
        return self.session.adapter.funding_events()

    def funding_coverage(self):
        return self.session.adapter.funding_coverage()

    def margin_facts(self):
        return self.session.adapter.read_facts()

    def observed_margin_facts(self):
        return self.session.adapter.facts

    def _confirmed(self, client_id):
        record, notional = self.session.adapter._load(client_id)
        if record.snapshot.reconcile_required:
            raise OutcomeUnknown('Demo order requires original-identity reconciliation')
        event = self.session.gateway.state.confirmations[client_id]
        return replace(event, cumulative_filled_notional=notional)

    def submit_risk_checked(self, intent, admit):
        return self.submit_order(intent, risk_admission=admit)

    def submit_order(self, intent, *, risk_admission=None):
        owner = self.owners.owner(intent.client_order_id)
        if owner is None or owner[2] != intent.instrument_id:
            raise SafetyBlocked('durable instance order ownership required')
        authorization, guard = self.capability(intent, owner)
        options = {} if risk_admission is None else dict(risk_admission=risk_admission)
        self.session.submit_instance_intent(intent, instance_id=owner[0],
            allocation_id=owner[1], authorization=authorization, guard=guard, **options)
        return self._confirmed(intent.client_order_id)

    def query_order(self, client_order_id):
        owner = self.owners.owner(client_order_id)
        if owner is None:
            raise SafetyBlocked('durable instance order ownership required')
        if client_order_id not in self.session._protocol_ids():
            # The adapter commits its intent before every POST. Missing local
            # evidence plus official absence proves this identity was not sent.
            self.session._require_absent(client_order_id, symbol=owner[2])
            raise SubmissionNotAttempted(client_order_id)
        event = self.session.adapter.query_order(client_order_id)
        self.session.controller._atomic(lambda: self.session.gateway._confirm(event))
        return self._confirmed(client_order_id)

    def cancel_order(self, client_order_id):
        owner = self.owners.owner(client_order_id)
        if owner is None:
            raise SafetyBlocked('durable instance order ownership required')
        intent = self.session.adapter.order(client_order_id).intent
        authorization, guard = self.capability(intent, owner)
        self.session.client.transport.authorizations[client_order_id] = authorization
        self.session._instance_guards[client_order_id] = guard
        self.session.cancel(client_order_id)
        return self._confirmed(client_order_id)
