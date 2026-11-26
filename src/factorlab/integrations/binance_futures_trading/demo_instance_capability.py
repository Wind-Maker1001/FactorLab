"""Symbol-bound network capabilities backed by the account's existing dispatch ledger."""
from datetime import timedelta
from decimal import Decimal
import json

from factorlab.trading.storage import decode
from .network import DemoExecutionPlan, DemoNetworkAuthorization
from .protocol import SafetyBlocked


class DemoInstanceCapability:
    """Mint process-local authority; retain original durable dispatch identities.

    The default exit-budget resolver derives stable entry identities and releases
    only this instance's original holds as reductions consume owned inventory.
    An explicit ``exit_reservation(intent, owner)`` can preserve an adopted entry
    convention; it must resolve identically after restart and allocate no capital.
    ``dispatch_gate(binding)`` checks host controls before the budget transaction.
    """
    def __init__(self, session, runtime, *, exit_reservation=None, dispatch_gate):
        self.session, self.runtime = session, runtime
        self.exit_reservation = exit_reservation
        self.dispatch_gate = dispatch_gate

    def __call__(self, intent, owner):
        session, runtime = self.session, self.runtime
        raw = session.controller.store.event_payload('demo:instance:' + owner[0])
        if raw is None:
            raise SafetyBlocked('qualified instance required for network capability')
        configured = decode(json.loads(raw))
        binding = configured['binding']
        if (binding['account_id'] != session.adapter.account_id
                or binding['allocation_id'] != owner[1]
                or binding['instance_id'] != owner[0]
                or owner[2] != intent.instrument_id
                or intent.instrument_id not in configured['symbols']):
            raise SafetyBlocked('instance capability ownership mismatch')
        authorization = runtime.continuous_authorization()
        if authorization is None or authorization['state'] != 'RUNNING':
            raise SafetyBlocked('running cumulative Demo authorization required')
        budget = configured['allocation']['budget_usdt']
        notional = intent.quantity * intent.limit_price
        if not intent.reduce_only and notional > budget:
            raise SafetyBlocked('instance intent exceeds its funded capital')
        seconds = authorization['seconds']
        plan = DemoExecutionPlan(account_id=binding['account_id'], symbol=intent.instrument_id,
            order_types=frozenset({'LIMIT'}), max_quantity=intent.quantity,
            max_notional=Decimal(authorization['total_notional']) if intent.reduce_only else budget,
            max_orders=authorization['total_orders'],
            max_open_orders=min(authorization['total_orders'],
                int(session.symbol_context(intent.instrument_id).rules.filters['MAX_NUM_ORDERS']['limit'])),
            max_seconds=seconds, max_loss=Decimal(authorization['max_loss']), max_margin=budget,
            expires_at=session._now() + timedelta(seconds=seconds),
            client_id_prefix=intent.client_order_id[:20], cleanup='CANCEL_KNOWN_THEN_STOP',
            recovery='GET_RECONCILE_NO_RESUBMIT')

        def guard(method, params, admitted_plan):
            self.dispatch_gate(binding)
            from .demo_instance_exit_budget import DemoInstanceExitBudget
            reservation, consumption = None, ()
            if method == 'POST':
                if intent.reduce_only:
                    consumption = DemoInstanceExitBudget(session, runtime).consumption(intent, owner)
                else:
                    reserve = self.exit_reservation or DemoInstanceExitBudget(session, runtime).opening
                    reservation = reserve(intent, owner)
            runtime.reserve_instance(binding, method, params, admitted_plan, intent=intent,
                facts=session.adapter.facts, exit_reservation=reservation, exit_consumption=consumption,
                cancel_recovered_at=session.last_reconciled_at if method == 'DELETE' else None)

        def receipt_sink(receipt):
            with runtime.db:
                runtime.db.execute(
                    'UPDATE dispatches SET receipt=? WHERE campaign=? AND method=? AND client_id=?',
                    (json.dumps(receipt), 'instance:' + binding['instance_id'],
                     receipt['method'], receipt['client_order_id']))

        session._instance_receipt_sinks[intent.client_order_id] = receipt_sink
        return DemoNetworkAuthorization(plan, cancellation_ids=(intent.client_order_id,)), guard
