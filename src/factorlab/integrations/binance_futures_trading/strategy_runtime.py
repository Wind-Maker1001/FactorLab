"""Closed-bar strategy composition over the admitted Demo execution path."""
from dataclasses import asdict
from datetime import timedelta
from decimal import Decimal as D

from factorlab.trading.contracts import OrderIntent, OrderType, Side, TERMINAL
from factorlab.trading.control import RiskRejected
from factorlab.trading.paper import Quote
from factorlab.trading.realtime import identity, PersistenceConflict
from factorlab.trading.strategy import ClosedBar
from factorlab.trading.target import target_quantity

from .execution import DemoExecutionGateway
from .protocol import SafetyBlocked
from .rules import market_quantity_constraints


class DemoStrategyRuntime:
    """The controller owns the strategy checkpoint and shared execution journal.

    Construction grants no write capability. Recovery is explicit and read-only;
    only a subsequent quote can consume a causal pending signal.
    """
    def __init__(self, controller, strategy, *, client_id_prefix='sg'):
        if not isinstance(controller.broker, DemoExecutionGateway):
            raise ValueError('Demo execution gateway required')
        if not client_id_prefix or len(client_id_prefix) > 6:
            raise ValueError('client ID prefix must contain 1..6 characters')
        self.controller, self.strategy = controller, strategy
        self.gateway = controller.broker
        self.symbol = strategy.describe()['symbol']
        if self.symbol != 'NVDAUSDT':
            raise ValueError('Demo strategy supports NVDAUSDT only')
        self.configuration = dict(strategy=strategy.describe(), client_id_prefix=client_id_prefix)
        self.gateway.bind_controller(controller)
        def initialize():
            runtime = self.gateway.state.runtime
            if runtime and runtime['configuration'] != self.configuration:
                raise PersistenceConflict('Demo strategy deployment changed')
            if not runtime:
                runtime.update(configuration=self.configuration, bars=[], pending=None, status='PAUSED')
            runtime['status'] = 'PAUSED'
        controller._atomic(initialize)

    def recover(self):
        """Query existing identities before permitting any new strategy order."""
        result = self.gateway.recover(self.controller)
        if result.reconciled:
            self.controller._atomic(lambda: self.gateway.state.runtime.update(status='RUNNING'))
        return result

    def disconnect(self):
        self.gateway.adapter.disconnect()
        def pause():
            self.gateway.state.reconciled = False
            self.gateway.state.runtime['status'] = 'PAUSED'
        self.controller._atomic(pause)

    def kill(self):
        self.controller.set_kill_switch(True)

    def closed_bar(self, bar):
        if not isinstance(bar, ClosedBar) or bar.symbol != self.symbol or bar.closed_at > self.controller.clock():
            raise ValueError('confirmed deployment bar required')
        if not bar.complete:
            return False
        def operation():
            runtime = self.gateway.state.runtime
            bars, value = runtime['bars'], asdict(bar)
            if bars:
                previous = bars[-1]
                if bar.closed_at < previous['closed_at']:
                    return False
                if bar.closed_at == previous['closed_at']:
                    if previous != value:
                        raise PersistenceConflict('closed bar identity conflict')
                    return False
                if bar.opened_at != previous['closed_at']:
                    runtime['status'] = 'PAUSED'
                    runtime['reason'] = 'BAR_SEQUENCE_GAP'
                    return False
            bars.append(value)
            del bars[:-self.strategy.history_limit]
            signal = self.strategy.signal([ClosedBar(**v) for v in bars], self.controller.clock())
            runtime['last_signal'] = asdict(signal)
            runtime['pending'] = asdict(signal) if signal.status == 'SIGNAL' and signal.direction != 'HOLD' else None
            self.controller.store.record_metadata('signal:' + signal.signal_id,
                dict(kind='STRATEGY_SIGNAL', **asdict(signal)))
            return True
        return self.controller._atomic(operation)

    def quote(self, quote):
        if not isinstance(quote, Quote) or quote.instrument_id != self.symbol:
            raise ValueError('deployment quote required')
        def operation():
            self.gateway.set_quote(quote)
            state, runtime = self.gateway.state, self.gateway.state.runtime
            signal = runtime['pending']
            if signal is None:
                return None
            if state.kill_switch:
                runtime['last_rejection'] = 'KILL_SWITCH'
                return None
            if runtime['status'] != 'RUNNING' or not state.reconciled:
                runtime['last_rejection'] = 'RECONCILE_REQUIRED'
                return None
            if quote.observed_at <= signal['input_watermark'] or quote.observed_at < signal['generated_at']:
                return None
            if quote.observed_at >= signal['input_watermark'] + timedelta(seconds=self.strategy.describe()['interval_seconds']):
                runtime.update(pending=None, last_rejection='SIGNAL_EXPIRED')
                return None
            if any(r.snapshot.status not in TERMINAL for r in state.orders.values()):
                runtime['last_rejection'] = 'OUTSTANDING_ORDER'
                return None
            facts = self.gateway.adapter.facts
            if facts is None or not self.gateway.adapter.reconciled:
                raise SafetyBlocked('Demo account reconciliation required')
            account = facts.shared(reconciled=True)
            if not account.reconciled:
                raise SafetyBlocked('complete Demo account required')
            funds = facts.usdt()
            if funds['unrealizedProfit'] is None:
                raise SafetyBlocked('Demo equity UNKNOWN')
            equity = funds['walletBalance'] + funds['unrealizedProfit']
            current = next((p.quantity for p in account.positions if p.instrument_id == self.symbol), D(0))
            step, minimum, maximum = market_quantity_constraints(
                dict(filters=list(self.gateway.rules.filters.values())))
            target = target_quantity(equity=equity, weight=signal['target_weight'], price=quote.price,
                budget_price=quote.price, fee_rate=state.fee_rate, current=current,
                available=funds['availableBalance'], step=step)
            delta = target - current
            runtime['target_position'] = dict(signal_id=signal['signal_id'], quantity=target, current=current)
            runtime['pending'] = None
            key = self.configuration['client_id_prefix'] + identity((self.configuration, signal['signal_id']))[:30]
            outcome = dict(kind='TARGET_ORDER', signal_id=signal['signal_id'], client_order_id=key,
                           target_position=target, quantity=abs(delta))
            event = None
            if not delta:
                outcome.update(kind='TARGET_ALREADY_MET')
            elif abs(delta) < minimum or abs(delta) > maximum:
                outcome.update(kind='TARGET_REJECTED', reason='TARGET_ORDER_FILTER')
            else:
                intent = OrderIntent(state.market, self.symbol, Side.BUY if delta > 0 else Side.SELL,
                    OrderType.MARKET, abs(delta), None, key, reduce_only=current > 0 and -current <= delta < 0)
                try:
                    event = self.controller._submit_in_transaction(intent)
                except RiskRejected as error:
                    outcome.update(kind='TARGET_REJECTED', reason=error.decision.reason)
            self.controller.store.record_metadata('target:' + signal['signal_id'], outcome)
            runtime['last_order'] = outcome
            return event
        return self.controller._atomic(operation)
