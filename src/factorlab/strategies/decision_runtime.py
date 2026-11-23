"""Durable strategy decisions and execution adapters over existing controllers."""
from dataclasses import asdict
from contextlib import nullcontext
from datetime import timedelta
from decimal import Decimal as D
import json

from factorlab.trading.contracts import Side, TERMINAL
from factorlab.trading.control import RiskRejected
from factorlab.trading.realtime import identity, PersistenceConflict
from factorlab.trading.storage import decode, dumps
from factorlab.trading.strategy import StrategySignal, ClosedBar
from factorlab.strategies.grid_execution import project_paper_account, _project_paper_account, _historical_turnover
from .protocol import Decision, decision_payload


class DecisionJournal:
    """Immutable input/decision receipts bind source/config/state and recovery.

    Each decision is durably recorded before dispatch. Replay of an acknowledged
    event returns the saved decision and does not invoke strategy code again.
    """
    def __init__(self, strategy, controller, *, deployment_id, symbol, interval_seconds, data_types):
        strategy.spec.require(symbol=symbol, interval_seconds=interval_seconds, data_types=data_types)
        self.strategy, self.controller = strategy, controller
        self.spec, self.symbol, self.interval_seconds = strategy.spec, symbol, interval_seconds
        self.deployment_id = deployment_id
        if not deployment_id or ':' in deployment_id:
            raise ValueError('deployment_id must be nonempty and cannot contain a colon')
        self.prefix = 'decision:' + deployment_id + ':'
        binding = dict(strategy=strategy.binding, deployment_id=deployment_id, symbol=symbol,
                       interval_seconds=interval_seconds)
        store = controller.store
        with store.transaction():
            saved = store.event_payload(self.prefix + 'binding')
            if saved is not None and saved != dumps(binding):
                raise PersistenceConflict('strategy deployment identity/configuration changed')
            store.record_metadata(self.prefix + 'binding', binding)
            previous = self._latest()
            store.record_metadata(self.prefix + 'restore:' + str(self._count('restore:')),
                dict(binding=identity(binding), state_version=self.spec.state_version,
                     restored_state=identity(previous), at=controller.clock()))

    def _count(self, suffix):
        return sum(key.startswith(self.prefix + suffix) for key, in
                   self.controller.store.db.execute('SELECT id FROM events'))

    def _read(self, key):
        raw = self.controller.store.event_payload(key)
        if raw is None:
            return None
        receipt = decode(json.loads(raw))
        payload = ''.join(decode(json.loads(self.controller.store.event_payload(part)))
                          for part in receipt['parts'])
        if identity(payload) != receipt['sha256']:
            raise PersistenceConflict('strategy receipt checksum mismatch')
        return decode(json.loads(payload))

    def _write(self, key, record):
        payload = dumps(record)
        parts = []
        # PaperStore keeps its existing per-event size bound.
        for n, offset in enumerate(range(0, len(payload), 1000)):
            part = key + ':part:' + str(n)
            self.controller.store.record_metadata(part, payload[offset:offset + 1000])
            parts.append(part)
        self.controller.store.record_metadata(key, dict(parts=parts, sha256=identity(payload)))

    def _latest(self):
        for key, in self.controller.store.db.execute('SELECT id FROM events ORDER BY rowid DESC'):
            if key.startswith(self.prefix + 'event:') and ':part:' not in key:
                return self._read(key)['decision']['state']
        return {}

    def decide(self, event):
        if (event['symbol'] != self.symbol or event['kind'] not in self.spec.data_types
                or event['now'] > self.controller.clock()):
            raise ValueError('event exceeds declared deployment data requirements')
        key = self.prefix + 'event:' + identity(event['event_id'])
        with (nullcontext() if self.controller.store.db.in_transaction else self.controller.store.transaction()):
            saved = self._read(key)
            if saved is not None:
                record = saved
                # Receipt time may differ after restart; observations may not.
                if record['input_identity'] != identity({k: v for k, v in event.items() if k not in {'now', 'account'}}):
                    raise PersistenceConflict('strategy event identity conflict')
                return Decision(**record['decision'])
            decision = self.strategy.decide(event, self._latest()).validate(self.spec, self.symbol)
            self._write(key, dict(
                input_identity=identity({k: v for k, v in event.items() if k not in {'now', 'account'}}),
                event=event, decision=decision_payload(decision), state_version=self.spec.state_version))
            return decision


class SignalAdapter:
    """Plug a protocol target strategy into existing Demo/Realtime runtimes."""
    def __init__(self, journal):
        if journal.spec.decision_kind != 'target':
            raise ValueError('target strategy required')
        self.journal = journal
        self.history_limit = journal.spec.history_limit

    def describe(self):
        j = self.journal
        return dict(strategy_id=j.spec.strategy_id, strategy_version=j.spec.version,
            symbol=j.symbol, interval_seconds=j.interval_seconds, binding=j.strategy.binding)

    def signal(self, bars, now):
        bar = bars[-1]
        if any(b.symbol != self.journal.symbol or b.interval_seconds != self.journal.interval_seconds
               or not b.complete or b.closed_at > now for b in bars):
            raise ValueError('strategy bar deployment mismatch')
        event = dict(kind='closed_bar', symbol=bar.symbol, now=now,
            event_id='bar:' + bar.closed_at.isoformat(), bars=[asdict(b) for b in bars])
        target = self.journal.decide(event).target_weight
        return StrategySignal(self.journal.spec.strategy_id, self.journal.spec.version, bar.symbol,
            now, bar.closed_at, 'HOLD' if target is None else 'SELL' if target == 0 else 'BUY',
            target, bar.source, identity((self.describe(), bar.closed_at)),
            status=('INSUFFICIENT_DATA' if len(bars) < self.history_limit else 'NO_SIGNAL')
                if target is None else 'SIGNAL')


class ControllerExecution:
    """Order outbox. Only the account controller/session can mutate orders.

    Persisted attempts without a controller order are uncertain and block replay;
    operators reconcile them instead of silently submitting a second time.
    """
    def __init__(self, controller, *, deployment_id, session=None, expect=None, preview=False,
                 max_notional=None, max_quantity=None):
        self.controller, self.session, self.expect, self.preview = controller, session, expect, preview
        self.deployment_id = deployment_id
        self.prefix = 'strategy-dispatch:' + deployment_id + ':'
        self.max_notional, self.max_quantity = max_notional, max_quantity

    def dispatch(self, decision, *, book=None):
        c = self.controller
        results = []
        for key in decision.cancellations:
            self._owned(key)
            record = c.broker.state.orders.get(key)
            if record is None:
                raise ValueError('cannot cancel an unknown strategy order')
            if record.snapshot.status in TERMINAL:
                continue
            if not self.preview:
                results.append(self.session.cancel(key) if self.session else c.cancel(key))
        for intent in decision.orders:
            self._owned(intent.client_order_id)
            if intent.market != c.broker.state.market:
                raise ValueError('strategy order market mismatch')
            prior = c.broker.state.orders.get(intent.client_order_id)
            if prior is not None:
                if prior.snapshot.intent != intent:
                    raise PersistenceConflict('strategy reused an order identity')
                if prior.snapshot.reconcile_required:
                    raise PersistenceConflict('strategy order requires reconciliation')
                continue
            key = self.prefix + intent.client_order_id
            saved = c.store.event_payload(key)
            if saved is not None:
                if decode(json.loads(saved))['intent'] != intent:
                    raise PersistenceConflict('strategy dispatch identity changed')
                raise PersistenceConflict('unresolved strategy dispatch attempt; reconcile before continuing')
            if self.preview:
                continue
            price = max(intent.limit_price or D(0), c.broker.state.quotes[intent.instrument_id].price)
            position = c.broker.state.positions.get(intent.instrument_id)
            inventory = position.quantity if position else D(0)
            reserved = sum((r.snapshot.intent.quantity - r.snapshot.cumulative_filled_quantity
                for r in c.broker.state.orders.values() if r.snapshot.status not in TERMINAL
                and r.snapshot.intent.instrument_id == intent.instrument_id
                and r.snapshot.intent.side is Side.BUY), D(0))
            if ((self.max_quantity is not None and (intent.quantity > self.max_quantity
                    or intent.side is Side.BUY and inventory + reserved + intent.quantity > self.max_quantity))
                    or self.max_notional is not None and (intent.quantity * price > self.max_notional
                    or intent.side is Side.BUY and (inventory + reserved + intent.quantity) * price > self.max_notional)):
                raise ValueError('strategy order exceeds execution budget')
            # Existing risk admission precedes the durable dispatch attempt.
            risk = c.assess(intent)
            if not risk.approved:
                raise RiskRejected(risk)
            with c.store.transaction():
                c.store.record_metadata(key, dict(intent=intent, at=c.clock()))
            if self.expect:
                self.expect(intent.client_order_id, intent.side, intent.quantity)
            results.append(self.session.submit_intent(intent, book=book) if self.session else c.submit(intent))
        return results[-1] if results else None

    def _owned(self, key):
        if not key.startswith(self.deployment_id + '-') or len(key) > 36:
            raise ValueError('order identity outside strategy deployment')


class OrderStrategyRuntime:
    """Event-driven orders; confirmations, not proposals, change inventory."""
    def __init__(self, journal, *, session=None, expect=None, preview=False,
                 max_notional=None, max_quantity=None, client_id_prefix=None):
        if journal.spec.decision_kind != 'orders':
            raise ValueError('order strategy required')
        self.journal, self.controller = journal, journal.controller
        self.gateway = self.controller.broker
        self.strategy = journal.strategy
        self.session = session
        self.execution = ControllerExecution(self.controller, deployment_id=journal.deployment_id,
            session=session, expect=expect, preview=preview, max_notional=max_notional, max_quantity=max_quantity)
        self.symbol = journal.symbol
        def initialize():
            configuration = dict(strategy=journal.strategy.binding,
                client_id_prefix=client_id_prefix or journal.deployment_id + '-',
                max_notional=max_notional, max_quantity=max_quantity, preview=preview)
            saved = self.gateway.state.runtime.get('configuration')
            if saved is not None and saved != configuration:
                raise PersistenceConflict('strategy execution configuration changed')
            self.gateway.state.runtime['configuration'] = configuration
            self.gateway.state.runtime.setdefault('status', 'PAUSED')
            self.gateway.state.runtime.setdefault('pending', None)
            self.gateway.state.runtime.setdefault('bars', [])
        self.controller._atomic(initialize)

    def _account(self):
        # The existing projection keeps base quantity separate from collateral.
        if self.controller.store.db.in_transaction:
            return asdict(_project_paper_account(self.gateway, self.symbol,
                _historical_turnover(self.controller.store)))
        return asdict(project_paper_account(self.controller, self.symbol))

    def _event(self, kind, event_id, **value):
        return self.journal.decide(dict(kind=kind, event_id=event_id, symbol=self.symbol,
            now=self.controller.clock(), deployment_id=self.journal.deployment_id,
            account=self._account(), **value))

    def closed_bar(self, bar):
        if (not isinstance(bar, ClosedBar) or bar.symbol != self.symbol
                or bar.interval_seconds != self.journal.interval_seconds
                or bar.closed_at > self.controller.clock()):
            raise ValueError('confirmed deployment bar required')
        if not bar.complete:
            return False
        if 'closed_bar' not in self.journal.spec.data_types:
            return False
        def operation():
            runtime = self.gateway.state.runtime
            bars = runtime['bars']
            if bars and bar.closed_at <= bars[-1]['closed_at']:
                if bar.closed_at == bars[-1]['closed_at'] and asdict(bar) != bars[-1]:
                    raise PersistenceConflict('closed bar identity conflict')
                return False
            if bars and bar.opened_at != bars[-1]['closed_at']:
                runtime.update(status='PAUSED', reason='BAR_SEQUENCE_GAP')
                return False
            bars.append(asdict(bar))
            del bars[:-self.journal.spec.history_limit]
            if runtime['status'] == 'STOPPED':
                return False
            if 'closed_bar' in self.journal.spec.data_types:
                decision = self._event('closed_bar', 'bar:' + bar.closed_at.isoformat(),
                    bar=asdict(bar), bars=bars)
                self._pending(decision, bar.closed_at)
            return True
        return self.controller._atomic(operation)

    def quote(self, quote):
        if quote.instrument_id != self.symbol:
            raise ValueError('deployment quote required')
        state = self.gateway.state
        if state.runtime['status'] != 'RUNNING' or not state.reconciled or state.kill_switch:
            return None
        if not timedelta(0) <= self.controller.clock() - quote.observed_at <= timedelta(seconds=5):
            raise ValueError('fresh quote required')
        book = self.session.prepare_limit_book() if self.session else None
        self.controller.quote(quote)
        pending = self.gateway.state.runtime['pending']
        result = None
        if pending and quote.observed_at > pending['after']:
            if quote.observed_at < pending['after'] + timedelta(seconds=self.journal.interval_seconds):
                result = self.execution.dispatch(Decision(**pending['decision']), book=book)
            self.controller._atomic(lambda: self.gateway.state.runtime.update(pending=None))
        if 'quote' in self.journal.spec.data_types:
            def decide_quote():
                decision = self._event('quote', 'quote:' + quote.observed_at.isoformat(), quote=quote)
                self._pending(decision, quote.observed_at)
            # Decision state and its dispatchable outbox commit together.
            self.controller._atomic(decide_quote)
            pending = self.gateway.state.runtime['pending']
            result = self.execution.dispatch(Decision(**pending['decision']), book=book) or result
            self.controller._atomic(lambda: self.gateway.state.runtime.update(pending=None))
        return result

    def recover(self):
        if self.session:
            self.session.recover()
            result = self.controller.account()
        elif not hasattr(self.gateway, 'recover'):
            result = self.controller.reconcile()
        else:
            result = self.gateway.recover(self.controller)
        if result.reconciled:
            if 'order_event' in self.journal.spec.data_types:
                # Deliver each immutable confirmation once, with the current
                # reconciled account. Replays use the original saved receipt.
                for _, event in list(self.controller.store.order_events()):
                    if event.client_order_id.startswith(self.execution.deployment_id + '-'):
                        key = self.journal.prefix + 'event:' + identity(event.event_id)
                        if self.controller.store.event_payload(key) is None:
                            def confirm():
                                decision = self._event('order_event', event.event_id, order_event=event)
                                self._pending(decision, self.controller.clock())
                            self.controller._atomic(confirm)
            self.controller._atomic(lambda: self.gateway.state.runtime.update(status='RUNNING')
                if self.gateway.state.runtime['status'] != 'STOPPED' else None)
        return result

    def _pending(self, decision, after):
        runtime = self.gateway.state.runtime
        previous = runtime.get('pending')
        old = Decision(**previous['decision']) if previous else Decision()
        orders = {o.client_order_id: o for o in (*old.orders, *decision.orders)}
        runtime['pending'] = dict(decision=decision_payload(Decision(orders=tuple(orders.values()),
            cancellations=tuple(sorted(set(old.cancellations) | set(decision.cancellations))))), after=after)

    def pause(self):
        self.controller._atomic(lambda: self.gateway.state.runtime.update(status='PAUSED', pending=None))

    def resume(self):
        if self.gateway.state.runtime['status'] == 'STOPPED':
            raise ValueError('stopped strategy cannot resume')
        return self.recover()

    def stop(self):
        self.controller._atomic(lambda: self.gateway.state.runtime.update(status='STOPPED', pending=None))

    def disconnect(self):
        self.gateway.adapter.disconnect()
        self.pause()
