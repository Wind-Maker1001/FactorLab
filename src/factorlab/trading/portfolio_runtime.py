"""Durable target batches over an existing controller and recovery gateway.

No adapter, order state machine, cash ledger or network authority is created here.
"""
from decimal import Decimal as D
import json

from .contracts import OrderIntent, OrderType, Side, TERMINAL
from .portfolio_target import plan_targets
from .realtime import identity
from .storage import decode


class PortfolioRuntime:
    def __init__(self, controller, *, allocation_id, owned_symbols, recover):
        if not allocation_id or not owned_symbols:
            raise ValueError('explicit allocation and owned symbols required')
        self.controller = controller
        self.gateway = controller.broker
        self.allocation_id = allocation_id
        self.owned_symbols = frozenset(owned_symbols)
        self.recover_account = recover

    def _key(self, task):
        if not task:
            raise ValueError('stable scheduler task identity required')
        return 'portfolio:' + identity((self.allocation_id, task))

    def begin(self, task, *, weights, equity):
        # Validate before storing; no inferred ownership and no synthetic wallets.
        for value in weights.values():
            if not isinstance(value, D) or not value.is_finite() or value < 0:
                raise ValueError('finite nonnegative Decimal weights required')
        if (sum(weights.values(), D(0)) > 1 or set(weights) - self.owned_symbols
                or not isinstance(equity, D) or not equity.is_finite() or equity < 0):
            raise ValueError('invalid account target')
        payload = dict(allocation=self.allocation_id, owned=sorted(self.owned_symbols),
                       weights=weights, equity=equity)
        return self.controller.store.record_metadata(self._key(task), payload)

    def advance(self, task, *, prices, steps, submit=None):
        """Recover first, persist each decision before IO, never retry uncertain IO.

        submit may be the existing DemoSession.submit_intent wrapper so campaign
        admission remains authoritative. An existing batch always requires GET
        recovery; only terminal batches may advance to another phase.
        """
        key = self._key(task)
        raw = self.controller.store.event_payload(key)
        if raw is None:
            raise ValueError('begin task first')
        target = decode(json.loads(raw))
        if target['owned'] != sorted(self.owned_symbols):
            raise ValueError('allocation ownership changed')
        account = self.recover_account()
        if not account.reconciled:
            return 'RECONCILE_REQUIRED'
        index = 0
        while self.controller.store.event_payload(key + ':batch:' + str(index)) is not None:
            batch = decode(json.loads(self.controller.store.event_payload(key + ':batch:' + str(index))))
            for intent in batch:
                order = self.gateway.state.orders.get(intent.client_order_id)
                if order is None or order.snapshot.status not in TERMINAL:
                    return 'RECONCILE_REQUIRED'
            index += 1
        legs = plan_targets(account=account, equity=target['equity'], weights=target['weights'],
            prices=prices, steps=steps, owned_symbols=self.owned_symbols,
            fee_rate=self.gateway.state.fee_rate)
        intents = [OrderIntent(account.market, leg.symbol,
            Side.BUY if leg.delta > 0 else Side.SELL, OrderType.LIMIT,
            abs(leg.delta), prices[leg.symbol],
            'pf' + identity((key, index, leg.symbol))[:30],
            position_side='BOTH', reduce_only=leg.delta < 0)
            for leg in legs if leg.delta]
        if not intents:
            return 'NO_EXECUTABLE_DELTA'
        # One atomic reservation wins across processes. A loser must not dispatch.
        with self.controller.store.transaction():
            inserted = self.controller.store.record_metadata(key + ':batch:' + str(index), intents)
        if not inserted:
            return 'RECONCILE_REQUIRED'
        for intent in intents:
            (submit or self.controller.submit)(intent)
        return 'SUBMITTED'
