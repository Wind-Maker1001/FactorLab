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
        input_identity = identity({k: v for k, v in event.items()
                                   if k not in {'now', 'account', 'execution'}})
        with (nullcontext() if self.controller.store.db.in_transaction else self.controller.store.transaction()):
            saved = self._read(key)
            if saved is not None:
                record = saved
                # Receipt time may differ after restart; observations may not.
                if record['input_identity'] != input_identity:
                    raise PersistenceConflict('strategy event identity conflict')
                return Decision(**record['decision'])
            decision = self.strategy.decide(event, self._latest()).validate(self.spec, self.symbol)
            self._write(key, dict(
                input_identity=input_identity,
                event=event, decision=decision_payload(decision), state_version=self.spec.state_version))
            return decision


class InstanceDecisionRuntime:
    """V2 receipts and recovery over an instance view and the existing execution owner.

    Execution adapters persist stable per-intent identities in their controller
    journal before acknowledging a receipt. Dispatch retries reuse those IDs.
    """
    def __init__(self, strategy, store, adapter, *, allocation):
        from .protocol import StrategySpecV2
        from .package import manifest_spec
        if store.legacy or not isinstance(strategy.spec, StrategySpecV2):
            raise ValueError('V2 runtime requires a registered instance')
        self.strategy, self.store, self.adapter = strategy, store, adapter
        self.package_id = strategy.binding['package_id']
        if strategy.package_manifest != store.manifest(self.package_id) or strategy.spec != manifest_spec(strategy.package_manifest):
            raise ValueError('runtime/package identity mismatch')
        strategy.spec.require_binding(store.binding())
        adapter.qualify(strategy.spec, store.binding(), allocation)
        store.bind_runtime(self.package_id, strategy.binding)

    def recover(self):
        if (self.store.pending() is not None or self.store.activation_callback() is not None
                or self.store.snapshot()['active'] != self.package_id):
            raise ValueError('instance quiesced or runtime version is not active')
        for receipt in self.store.outstanding(self.package_id):
            self.adapter.execute(receipt)
            self.store.acknowledge(receipt)

    def decide(self, event):
        from .events import validate_event
        validate_event(event, self.strategy.spec)
        if event['binding'] != self.store.binding():
            raise ValueError('event routed to the wrong instance/allocation')
        self.recover()
        from .identity import content_identity
        input_identity = content_identity(event)
        saved = self.store.decision(self.package_id, event['event_id'], input_identity)
        if saved is not None:
            return saved
        model = self.strategy.binding.get('model')
        if model is not None:
            self.strategy.require_event_time(event['event_time'])
            self.store.require_new_model_event(event)
        snapshot = self.store.snapshot()
        checkpoint = snapshot['checkpoint']
        try:
            decision = self.strategy.decide(event, checkpoint)
        except Exception as error:
            self.store.rollback('STRATEGY_FAILED:' + type(error).__name__, expected_active=self.package_id)
            raise
        receipt = self.store.commit_decision(self.package_id, event['event_id'], input_identity,
            decision_payload(decision), event=event, expected_checkpoint=checkpoint,
            expected_watermark=snapshot['watermark'], model=model)
        self.adapter.execute(receipt)
        self.store.acknowledge(receipt)
        return receipt


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

    def cancellation_confirmed(self, key):
        record = self.controller.broker.state.orders.get(key)
        return (record is not None and record.snapshot.status in TERMINAL
                and not record.snapshot.reconcile_required)

    def dispatch(self, decision, *, deadlines=None, cancellation_attempts=None):
        c = self.controller
        results = []
        deferred = set()
        for key in decision.cancellations:
            self._owned(key)
            record = c.broker.state.orders.get(key)
            if record is None:
                raise ValueError('cannot cancel an unknown strategy order')
            if self.cancellation_confirmed(key):
                continue
            if not self.preview:
                if cancellation_attempts is not None:
                    attempt = cancellation_attempts[key]
                    if c.store.event_payload(attempt) is not None:
                        # This also accepts legacy pre-call "receipts". They
                        # prove only an attempt, never the exchange outcome.
                        if self.session:
                            self.session.recover()
                        if self.cancellation_confirmed(key):
                            continue
                    else:
                        with c.store.transaction():
                            c.store.record_metadata(attempt, dict(client_order_id=key,
                                status='ATTEMPTED', at=c.clock()))
                record = c.broker.state.orders[key]
                if not c.broker.state.reconciled or record.snapshot.reconcile_required:
                    continue
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
            if self.retired(intent.client_order_id, intent):
                continue
            if self.preview:
                continue
            if not c.broker.state.reconciled or (self.session and any(
                    r.snapshot.status not in TERMINAL for r in c.broker.state.orders.values())):
                deferred.add(intent.client_order_id)
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
                if deadlines is not None:
                    c._atomic(lambda: self.retire(intent, 'RISK_REJECTED', 'EXECUTION_BUDGET'))
                raise ValueError('strategy order exceeds execution budget')
            # Existing risk admission precedes the durable dispatch attempt.
            book = self.session.prepare_limit_book() if self.session else None
            if deadlines is not None and (c.clock() >= deadlines[intent.client_order_id]
                    or book is not None and book['time_utc'] >= deadlines[intent.client_order_id]):
                c._atomic(lambda: self.retire(intent, 'EXPIRED'))
                continue
            risk = c.assess(intent)
            if not risk.approved:
                if deadlines is not None:
                    c._atomic(lambda: self.retire(intent, 'RISK_REJECTED', risk.reason))
                raise RiskRejected(risk)
            with c.store.transaction():
                c.store.record_metadata(key, dict(intent=intent, at=c.clock()))
            if self.expect:
                self.expect(intent.client_order_id, intent.side, intent.quantity)
            try:
                result = self.session.submit_intent(intent, book=book) if self.session else c.submit(intent)
            except RiskRejected as error:
                if deadlines is not None:
                    c._atomic(lambda: self.retire(intent, 'RISK_REJECTED', error.decision.reason))
                raise
            results.append(result)
        return (results[-1] if results else None), deferred

    def attempted(self, key):
        return self.controller.store.event_payload(self.prefix + key) is not None

    def retired(self, key, intent=None):
        raw = self.controller.store.event_payload(self.prefix + key + ':outcome')
        if raw is None:
            return False
        if intent is not None and decode(json.loads(raw))['intent'] != intent:
            raise PersistenceConflict('strategy reused a retired order identity')
        return True

    def retire(self, intent, status, reason=None):
        self.controller.store.record_metadata(self.prefix + intent.client_order_id + ':outcome',
            dict(intent=intent, status=status, reason=reason, at=self.controller.clock()))

    def _owned(self, key):
        if not key.startswith(self.deployment_id + '-') or len(key) > 36:
            raise ValueError('order identity outside strategy deployment')


class OrderOutbox:
    """Maintain durable orders without loading or invoking strategy code."""
    def __init__(self, controller, *, deployment_id, runtime_namespace, session=None):
        self.controller = controller
        self.gateway = controller.broker
        self.runtime_namespace = runtime_namespace
        self.execution = ControllerExecution(controller, deployment_id=deployment_id, session=session)

    def _runtime(self):
        if self.runtime_namespace is None:
            return self.gateway.state.runtime
        slots = self.gateway.state.runtime.setdefault('strategy_deployments', {})
        return slots.setdefault(self.runtime_namespace, {})

    def dispatch_cancellations(self, observed_at):
        """Advance durable cancellation obligations without submitting new entries."""
        pending = self._runtime()['pending']
        if pending is None or not pending['cancellations']:
            return None
        self.controller._atomic(lambda: self._prune_pending(self.controller.clock()))
        pending = self._runtime()['pending']
        if pending is None or not pending['cancellations']:
            return None
        cancellations = {offer['key']: offer for offer in pending['cancellations']
                         if observed_at >= offer['eligible_at']}
        if not cancellations:
            return None
        result, _ = self.execution.dispatch(
            Decision(cancellations=tuple(cancellations)),
            cancellation_attempts={key: self.execution.prefix + 'cancel:' + identity((key, offer['eligible_at']))
                                   for key, offer in cancellations.items()},
        )
        self.controller._atomic(lambda: self._prune_pending(self.controller.clock()))
        return result

    def has_unresolved_work(self):
        """Report persisted unconfirmed dispatch attempts and pending cancellation obligations."""
        state = self.gateway.state
        pending = self._runtime().get('pending')
        if pending is None:
            return dict(unresolved_attempts=[], pending_cancellations=[])
        unresolved_attempts = [
            offer['intent'].client_order_id for offer in pending['orders']
            if offer['intent'].client_order_id not in state.orders
            and self.execution.attempted(offer['intent'].client_order_id)
        ]
        pending_cancellations = [
            offer['key'] for offer in pending['cancellations']
            if not self.execution.cancellation_confirmed(offer['key'])
        ]
        return dict(unresolved_attempts=unresolved_attempts,
                    pending_cancellations=pending_cancellations)

    def _save_pending(self, orders, cancellations):
        pending = dict(orders=orders, cancellations=cancellations) if orders or cancellations else None
        self._runtime()['pending'] = pending

    def _prune_pending(self, at, *, withdraw=False):
        state = self.gateway.state
        pending = self._runtime()['pending']
        if pending is None:
            return
        remaining = []
        for offer in pending['orders']:
            intent = offer['intent']
            key = intent.client_order_id
            if self.execution.retired(key, intent):
                continue
            expired = at >= offer['expires_at']
            if expired or withdraw:
                if key in state.orders:
                    continue
                if not self.execution.attempted(key):
                    self.execution.retire(intent, 'EXPIRED' if expired else 'WITHDRAWN')
                    continue
            remaining.append(offer)
        # Cancellation is a durable obligation for an existing order. Entry
        # deadlines and withdrawal on pause apply only to unsubmitted orders.
        cancellations = []
        for offer in pending['cancellations']:
            key = offer['key']
            if self.execution.cancellation_confirmed(key):
                order = state.orders[key].snapshot
                receipt = self.execution.prefix + 'cancel:' + identity((key, offer['eligible_at'])) + ':outcome'
                if self.controller.store.event_payload(receipt) is None:
                    self.controller.store.record_metadata(receipt, dict(client_order_id=key,
                        status=order.status, filled_quantity=order.cumulative_filled_quantity,
                        fee=order.cumulative_fee, at=self.controller.clock()))
            else:
                cancellations.append(offer)
        self._save_pending(remaining, cancellations)

    def _stop_pending(self, status):
        def operation():
            self._prune_pending(self.controller.clock(), withdraw=True)
            self._runtime()['status'] = status
        self.controller._atomic(operation)

    def pause(self):
        self._stop_pending('PAUSED')



class OrderStrategyRuntime(OrderOutbox):
    """Event-driven orders; confirmations, not proposals, change inventory."""
    def __init__(self, journal, *, session=None, expect=None, preview=False,
                 max_notional=None, max_quantity=None, client_id_prefix=None,
                 runtime_namespace=None):
        if journal.spec.decision_kind != 'orders':
            raise ValueError('order strategy required')
        self.journal, self.controller = journal, journal.controller
        self.gateway = self.controller.broker
        self.strategy = journal.strategy
        self.session = session
        self.runtime_namespace = runtime_namespace
        self.execution = ControllerExecution(self.controller, deployment_id=journal.deployment_id,
            session=session, expect=expect, preview=preview, max_notional=max_notional, max_quantity=max_quantity)
        self.symbol = journal.symbol
        def initialize():
            runtime = self._runtime()
            configuration = dict(strategy=journal.strategy.binding,
                client_id_prefix=client_id_prefix or journal.deployment_id + '-',
                max_notional=max_notional, max_quantity=max_quantity, preview=preview)
            saved = runtime.get('configuration')
            if saved is not None and saved != configuration:
                raise PersistenceConflict('strategy execution configuration changed')
            runtime['configuration'] = configuration
            runtime.setdefault('status', 'STARTING')
            runtime.setdefault('pending', None)
            runtime.setdefault('bars', [])
        self.controller._atomic(initialize)


    def _account(self):
        # The existing projection keeps base quantity separate from collateral.
        allow_shared = self.controller.store.event_payload('demo:static-shared-account:v1') is not None
        if self.controller.store.db.in_transaction:
            return asdict(_project_paper_account(self.gateway, self.symbol,
                _historical_turnover(self.controller.store), allow_shared=allow_shared))
        return asdict(project_paper_account(self.controller, self.symbol, allow_shared=allow_shared))

    def _event(self, kind, event_id, **value):
        state = self.gateway.state
        pending = self._runtime()['pending']
        pending_ids = [offer['intent'].client_order_id for offer in pending['orders']] if pending else []
        submitted = {key for key in state.orders if key.startswith(self.execution.deployment_id + '-')}
        submitted.update(key for key in pending_ids if self.execution.attempted(key))
        return self.journal.decide(dict(kind=kind, event_id=event_id, symbol=self.symbol,
            now=self.controller.clock(), deployment_id=self.journal.deployment_id,
            account=self._account(), execution=dict(submitted_order_ids=sorted(submitted),
                                                    pending_order_ids=pending_ids), **value))

    def historical_bar(self, bar):
        """Restore history; opted-in adapters observe without proposing entries."""
        return self.closed_bar(bar, historical=True)

    def closed_bar(self, bar, *, historical=False):
        if (not isinstance(bar, ClosedBar) or bar.symbol != self.symbol
                or bar.interval_seconds != self.journal.interval_seconds
                or bar.closed_at > self.controller.clock()):
            raise ValueError('confirmed deployment bar required')
        if not bar.complete:
            return False
        if 'closed_bar' not in self.journal.spec.data_types:
            return False
        def operation():
            runtime = self._runtime()
            bars = runtime['bars']
            if bars and bar.closed_at <= bars[-1]['closed_at']:
                if bar.closed_at == bars[-1]['closed_at'] and asdict(bar) != bars[-1]:
                    raise PersistenceConflict('closed bar identity conflict')
                return False
            if bars and bar.opened_at != bars[-1]['closed_at']:
                runtime.update(status='PAUSED', reason='BAR_SEQUENCE_GAP')
                self._prune_pending(self.controller.clock(), withdraw=True)
                return False
            bars.append(asdict(bar))
            del bars[:-self.journal.spec.history_limit]
            if runtime['status'] == 'STOPPED':
                return False
            runtime.pop('reason', None)
            if not historical or self.journal.spec.historical_observations:
                observation = dict(bar=asdict(bar), bars=bars)
                if historical:
                    observation['historical'] = True
                decision = self._event('closed_bar', 'bar:' + bar.closed_at.isoformat(),
                    **observation)
                if historical and decision.orders:
                    raise ValueError('historical observation cannot propose orders')
                self._pending(decision, bar.closed_at, historical=historical)
            return True
        return self.controller._atomic(operation)

    def quote(self, quote):
        if quote.instrument_id != self.symbol:
            raise ValueError('deployment quote required')
        state = self.gateway.state
        if self._runtime()['status'] != 'RUNNING' or not state.reconciled or state.kill_switch:
            return None
        if not timedelta(0) <= self.controller.clock() - quote.observed_at <= timedelta(seconds=5):
            raise ValueError('fresh quote required')
        self.controller.quote(quote)
        if 'quote' in self.journal.spec.data_types:
            def decide_quote():
                decision = self._event('quote', 'quote:' + quote.observed_at.isoformat(), quote=quote)
                self._pending(decision, quote.observed_at, True)
            # Decision state and its dispatchable outbox commit together.
            self.controller._atomic(decide_quote)
        return self._dispatch_pending(quote.observed_at)



    def _dispatch_pending(self, observed_at):
        if self._runtime()['pending'] is None:
            return None
        self.controller._atomic(lambda: self._prune_pending(self.controller.clock()))
        pending = self._runtime()['pending']
        if pending is None:
            return None
        state = self.gateway.state
        for offer in pending['orders']:
            key = offer['intent'].client_order_id
            if key not in state.orders and self.execution.attempted(key):
                raise PersistenceConflict('unresolved strategy dispatch attempt; reconcile before continuing')
        orders = {offer['intent'].client_order_id: offer for offer in pending['orders']
                  if observed_at >= offer['eligible_at']}
        cancellations = {offer['key']: offer for offer in pending['cancellations']
                         if observed_at >= offer['eligible_at']}
        decision = Decision(orders=tuple(offer['intent'] for offer in orders.values()),
                            cancellations=tuple(cancellations))
        result, deferred = self.execution.dispatch(decision,
            deadlines={key: offer['expires_at'] for key, offer in orders.items()},
            cancellation_attempts={key: self.execution.prefix + 'cancel:' + identity((key, offer['eligible_at']))
                                   for key, offer in cancellations.items()})

        def checkpoint():
            pending = self._runtime()['pending']
            remaining = []
            for offer in pending['orders']:
                key = offer['intent'].client_order_id
                if key in deferred:
                    remaining.append(dict(offer, status='DEFERRED'))
                elif key not in orders:
                    remaining.append(offer)
            self._save_pending(remaining, pending['cancellations'])
            self._prune_pending(self.controller.clock())
        self.controller._atomic(checkpoint)
        return result

    def sync_reconciled(self):
        """Sync confirmed order events and cancellation outcomes over an already reconciled account."""
        result = self.controller.account()
        if result.reconciled:
            self.controller._atomic(lambda: self._prune_pending(self.controller.clock()))
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
            self.controller._atomic(lambda: self._runtime().update(status='RUNNING')
                if self._runtime()['status'] == 'STARTING' else None)
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
            self.sync_reconciled()
        return result

    def _pending(self, decision, after, on_quote=False, *, historical=False):
        state = self.gateway.state
        runtime = self._runtime()
        pending = runtime['pending'] or dict(orders=[], cancellations=[])
        orders = {offer['intent'].client_order_id: offer for offer in pending['orders']}
        cancellations = {offer['key']: offer for offer in pending['cancellations']}
        expiry = after + timedelta(seconds=self.journal.interval_seconds)
        eligible = after if on_quote else after + timedelta(microseconds=1)
        paused = runtime['status'] in {'PAUSED', 'STOPPED'}
        for intent in decision.orders:
            key = intent.client_order_id
            self.execution._owned(key)
            if self.execution.retired(key, intent):
                continue
            existing = orders.get(key)
            if existing is not None:
                if existing['intent'] != intent:
                    raise PersistenceConflict('strategy reused a pending order identity')
                existing['eligible_at'] = min(existing['eligible_at'], eligible)
            elif paused and key not in state.orders and not self.execution.attempted(key):
                self.execution.retire(intent, 'WITHDRAWN', 'STRATEGY_PAUSED')
            elif not paused:
                orders[key] = dict(intent=intent, eligible_at=eligible, expires_at=expiry, status='PENDING')
        if historical or not paused:
            for key in decision.cancellations:
                self.execution._owned(key)
                if key not in state.orders and not self.execution.attempted(key):
                    if key in orders:
                        self.execution.retire(orders.pop(key)['intent'], 'WITHDRAWN')
                        continue
                    if self.execution.retired(key):
                        continue
                cancellations.setdefault(key, dict(key=key, eligible_at=eligible, expires_at=expiry))
        self._save_pending(list(orders.values()), list(cancellations.values()))





    def resume(self):
        def operation():
            runtime = self._runtime()
            if runtime['status'] == 'STOPPED':
                raise ValueError('stopped strategy cannot resume')
            runtime['status'] = 'STARTING'
        self.controller._atomic(operation)
        return self.recover()

    def stop(self):
        self._stop_pending('STOPPED')

    def disconnect(self):
        def operation():
            runtime = self._runtime()
            if runtime['status'] == 'RUNNING':
                runtime['status'] = 'STARTING'
        self.controller._atomic(operation)
        self.gateway.adapter.disconnect()
