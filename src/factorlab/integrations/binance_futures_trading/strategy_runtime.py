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
from .demo_session import DemoSession


class DemoStrategyRuntime:
    """The controller owns the strategy checkpoint and shared execution journal.

    Construction grants no write capability. Recovery is explicit and read-only;
    only a subsequent quote can consume a causal pending signal.
    """
    def __init__(self, controller, strategy, *, client_id_prefix='sg', expect=None):
        self.session = controller if isinstance(controller, DemoSession) else None
        self.expect = expect
        if self.session is not None:
            controller = self.session.controller
        if not isinstance(controller.broker, DemoExecutionGateway):
            raise ValueError('Demo execution gateway required')
        if not client_id_prefix or len(client_id_prefix) > 21:
            raise ValueError('client ID prefix must contain 1..21 characters')
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
                runtime['enabled'] = True
        controller._atomic(initialize)

    def recover(self):
        """Query existing identities before permitting any new strategy order."""
        result = self.gateway.recover(self.controller)
        if result.reconciled:
            def recovered():
                runtime = self.gateway.state.runtime
                if runtime.get('enabled', True) and runtime['status'] != 'STOPPED':
                    runtime['status'] = 'RUNNING'
            self.controller._atomic(recovered)
        return result

    def pause(self):
        def paused():
            runtime = self.gateway.state.runtime
            if runtime['status'] != 'STOPPED':
                runtime.update(status='PAUSED', enabled=False)
        self.controller._atomic(paused)

    def resume(self):
        if self.gateway.state.runtime['status'] == 'STOPPED':
            raise SafetyBlocked('stopped strategy deployment cannot resume')
        result = self.recover()
        if result.reconciled:
            self.controller._atomic(lambda: self.gateway.state.runtime.update(status='RUNNING', enabled=True))
        return result

    def stop(self):
        """Stop decisions without cancelling orders or liquidating positions."""
        self.controller._atomic(lambda: self.gateway.state.runtime.update(status='STOPPED', enabled=False, pending=None))

    def disconnect(self):
        self.gateway.adapter.disconnect()
        def pause():
            self.gateway.state.reconciled = False
            if self.gateway.state.runtime['status'] != 'STOPPED':
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
            if runtime['status'] == 'STOPPED':
                return False
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
        state = self.gateway.state
        runtime = state.runtime
        if (runtime['pending'] is None or runtime['status'] != 'RUNNING' or state.kill_switch
                or not self.gateway.read_account().reconciled
                or any(r.snapshot.status not in TERMINAL for r in state.orders.values())):
            book = None
        elif self.session is not None:
            book = self.session.prepare_limit_book()
        else:
            book = dict(askPrice=quote.price, bidPrice=quote.price, time_utc=quote.observed_at)
        def operation():
            state, runtime = self.gateway.state, self.gateway.state.runtime
            current = state.quotes.get(self.symbol)
            if current is not None:
                if quote.observed_at < current.observed_at:
                    return None
                if quote.observed_at == current.observed_at and quote != current:
                    raise PersistenceConflict('quote identity conflict')
            if (runtime['pending'] is not None and runtime['status'] == 'RUNNING'
                    and not state.kill_switch and state.reconciled
                    and not any(r.snapshot.status not in TERMINAL for r in state.orders.values())
                    and not timedelta(0) <= self.controller.clock() - quote.observed_at <= timedelta(seconds=5)):
                runtime['last_rejection'] = 'STALE_QUOTE'
                return None
            if current is None or quote.observed_at > current.observed_at:
                self.gateway.set_quote(quote)
            signal = runtime['pending']
            if signal is None:
                return None
            if self.controller.store.event_payload('target:' + signal['signal_id']) is not None:
                runtime['pending'] = None
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
            account = self.gateway.read_account()
            if not account.reconciled:
                runtime['last_rejection'] = 'RECONCILE_REQUIRED'
                return None
            if (book['time_utc'] <= signal['input_watermark']
                    or book['time_utc'] < signal['generated_at']):
                runtime['last_rejection'] = 'NONCAUSAL_BOOK'
                return None
            funds = facts.usdt()
            if funds['unrealizedProfit'] is None:
                raise SafetyBlocked('Demo equity UNKNOWN')
            balance = next(a for a in account.assets if a.currency == 'USDT')
            equity = balance.total + funds['unrealizedProfit']
            current = next((p.quantity for p in account.positions if p.instrument_id == self.symbol), D(0))
            if current < 0:
                runtime['last_rejection'] = 'LONG_ONLY_REQUIRED'
                return None
            lot = self.gateway.rules.filters['LOT_SIZE']
            step, minimum, maximum = (D(lot[name]) for name in ('stepSize', 'minQty', 'maxQty'))
            price = DemoSession.limit_price(self.gateway.rules, Side.BUY, book)
            target = target_quantity(equity=equity, weight=signal['target_weight'], price=price,
                budget_price=price, fee_rate=state.fee_rate, current=current,
                available=balance.available, step=step)
            delta = target - current
            runtime['target_position'] = dict(signal_id=signal['signal_id'], quantity=target, current=current)
            runtime['pending'] = None
            key = self.configuration['client_id_prefix'] + identity((self.configuration, signal['signal_id']))[:36 - len(self.configuration['client_id_prefix'])]
            outcome = dict(kind='TARGET_ORDER', signal_id=signal['signal_id'], client_order_id=key,
                           target_position=target, quantity=abs(delta))
            intent = None
            if not delta:
                outcome.update(kind='TARGET_ALREADY_MET')
            elif abs(delta) < minimum or abs(delta) > maximum:
                outcome.update(kind='TARGET_REJECTED', reason='TARGET_ORDER_FILTER')
            else:
                side = Side.BUY if delta > 0 else Side.SELL
                intent = OrderIntent(state.market, self.symbol, side, OrderType.LIMIT, abs(delta),
                    DemoSession.limit_price(self.gateway.rules, side, book), key,
                    position_side='BOTH', reduce_only=side is Side.SELL)
                outcome.update(intent=asdict(intent), submission='DECIDED')
            self.controller.store.record_metadata('target:' + signal['signal_id'], outcome)
            runtime['last_order'] = outcome
            return intent
        intent = self.controller._atomic(operation)
        if intent is None:
            return None
        try:
            if self.session is not None:
                if self.expect is not None:
                    self.expect(intent.client_order_id, intent.side, intent.quantity)
                self.session.submit_limit(intent.client_order_id, intent.side, intent.quantity,
                    reduce_only=intent.reduce_only, book=book)
                return self.gateway.state.confirmations.get(intent.client_order_id)
            return self.controller.submit(intent)
        except RiskRejected as error:
            def rejected():
                outcome = self.gateway.state.runtime['last_order']
                outcome.update(kind='TARGET_REJECTED', reason=error.decision.reason)
                self.controller.store.record_metadata('target-rejection:' + outcome['signal_id'], outcome)
            self.controller._atomic(rejected)
            return None
