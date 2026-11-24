"""Strategy router inside the existing Grid execution owner and DemoSupervisor."""
from dataclasses import asdict
from datetime import datetime, timedelta
from decimal import Decimal
import json
import os
from pathlib import Path

from factorlab.errors import DataQualityError
from factorlab.strategies.decision_runtime import DecisionJournal, OrderStrategyRuntime, SignalAdapter
from factorlab.strategies.loader import LoadedStrategy, StrategyWorkerError
from factorlab.trading import Side
from factorlab.trading.control import RiskRejected
from factorlab.trading.paper import Quote
from factorlab.trading.realtime import PersistenceConflict
from .demo_market_feed import FeedPaused
from .demo_runtime import MAX_NOTIONAL, MAX_QUANTITY
from .protocol import SafetyBlocked
from .strategy_deployment import DeploymentStore, DeploymentController, LEGACY, TERMINAL
from .strategy_package import (
    DECISION_INTERFACE, INTERFACE, digest, encoded, load_package, strategy_package_identity,
)
from .strategy_runtime import DemoStrategyRuntime
from .strategy_worker import invoke


class DemoReleaseAdapter:
    def __init__(self, router):
        self.router = router
        self.boot_id = str(os.getpid())

    def quiesce(self):
        self.router.base.bridge.withdraw_unsubmitted()
        self.router.base.live_bar = False
        self.router._quiesce_active_decision_runner()

    def reconcile(self):
        s = self.router.bridge.session.status()
        untracked = [o['client_order_id'] for o in s['open_orders'] or []
                     if o['client_order_id'] not in s['orders']]
        unknown = list(s['unknown'])
        pending_cancellations = []
        active = self.router.store.snapshot()['active']
        if active != LEGACY and self.router._is_decision_package(active):
            runner = self.router._try_get_decision_runner(active)
            if runner is not None:
                if s['reconciled'] and not unknown:
                    try:
                        runner.sync_reconciled()
                    except (StrategyWorkerError, ValueError, RuntimeError, TypeError):
                        pass
                if isinstance(runner, OrderStrategyRuntime):
                    work = runner.has_unresolved_work()
                    for key in work['unresolved_attempts']:
                        if key not in unknown:
                            unknown.append(key)
                    pending_cancellations.extend(work['pending_cancellations'])
        return dict(reconciled=s['reconciled'], unknown=unknown, positions=s['positions'],
            orders=[dict(client_id=k, status=v['status'], reduce_only=v['reduce_only'])
                    for k, v in s['orders'].items()], account_observed_at=str(s['account_observed_at']),
            untracked_orders=untracked, pending_cancellations=pending_cancellations)

    def watermark(self):
        r = self.router
        cursor = r.bridge.controller.store.db.execute('SELECT COALESCE(MAX(rowid),0) FROM events').fetchone()[0]
        return json.dumps(dict(controller_event=cursor, market=str(r.base.last_bar)), sort_keys=True)

    def _cancel_retry_permitted(self, client_id):
        r = self.router
        runtime = getattr(r.bridge, 'runtime', None)
        if runtime is None:
            return True
        previous = runtime.db.execute(
            "SELECT receipt FROM dispatches WHERE method='DELETE' AND client_id=?",
            (client_id,),
        ).fetchone()
        if previous is None:
            return True
        receipt = json.loads(previous[0]) if previous[0] is not None else None
        if receipt is not None and (not receipt.get('outcome_unknown') or receipt.get('http_status') == 400):
            return False
        recovered_at = r.bridge.session.last_reconciled_at
        if recovered_at is None:
            return False
        attempted_at = runtime._get('cancel-attempt:' + client_id)
        if attempted_at is None and receipt is not None:
            attempted_at = receipt.get('observed_at')
        if attempted_at is not None and recovered_at <= datetime.fromisoformat(attempted_at):
            return False
        return True

    def cancel(self, client_id):
        r = self.router
        if r.bridge.session.write_uncertain or r.bridge.session.receipt_error:
            return
        if not self._cancel_retry_permitted(client_id):
            return  # GET recovery must establish its terminal state; never repeat DELETE blindly.
        r.maintenance = True
        try:
            r.bridge.session.cancel(client_id)
        finally:
            r.maintenance = False

    def reduce(self, switch_id, version):
        r = self.router
        m = load_package(r.releases / version)
        rule = m['reduction']
        count = r.store.db.execute('SELECT count(*) FROM strategy_orders WHERE watermark=?',
                                   ('migration:' + str(switch_id),)).fetchone()[0]
        if count >= rule['max_attempts']:
            return
        s = r.bridge.session.status()
        quantity = sum((Decimal(p['quantity']) for p in s['positions']), Decimal(0))
        if quantity <= 0:
            raise SafetyBlocked('only confirmed long position reduction supported')
        r.send(version, 'migration:' + str(switch_id), dict(side='SELL', quantity=str(quantity)),
               index=count, maintenance=True)

    def prepare(self, version):
        if version == LEGACY:
            # Grid retains its own journal; it is restored against the present account.
            self.router.bridge.restore_reconciled()
            return
        path = self.router.releases / version
        manifest = load_package(path)
        if manifest['interface'] == DECISION_INTERFACE:
            self.router._load_decision_runner(version, manifest=manifest)
            return
        answer = invoke(path, dict(event='probe', environment='BINANCE_USDM_DEMO'), {})
        if answer['intents']:
            raise ValueError('probe must not generate intents')

    def activated(self, version):
        r = self.router
        r.base.require_fresh_bar()
        r.base.running = True
        if version != LEGACY and r._is_decision_package(version):
            _, loaded, runner = r._load_decision_runner(version)
            if isinstance(runner, DemoStrategyRuntime):
                runner.clear_pending(freshness_boundary=r.base.eligible_after)
                runner.controller._atomic(lambda: runner._runtime().update(enabled=True))
                runner.sync_reconciled()
                r.base.live_bar = False
                r.base.reason = 'FRESH_CLOSED_BAR_REQUIRED'
            else:
                runner.pause()
                runner.controller._atomic(lambda: runner._runtime().update(status='RUNNING'))
                if 'closed_bar' in loaded.spec.data_types:
                    r.base.live_bar = False
                    r.base.reason = 'FRESH_CLOSED_BAR_REQUIRED'
                else:
                    r.base.live_bar = True
                    r.base.reason = None
            return
        r.base.reason = 'FRESH_CLOSED_BAR_REQUIRED'


class ReleaseConsumer:
    """Only the owner invokes strategies; workers return data and never submit orders."""
    def __init__(self, base, directory, stack):
        self.base = base
        self.releases = Path(directory) / 'strategy-releases'
        self.store = DeploymentStore(Path(directory) / 'strategy-deployment.sqlite3')
        stack.callback(self.store.close)
        self.adapter = DemoReleaseAdapter(self)
        self.deployer = DeploymentController(self.store, self.adapter)
        self.maintenance = False
        self._runners = {}
        self._expected = None
        self.bridge.session.strategy_write_guard = self.write_guard

    def __getattr__(self, name):
        return getattr(self.base, name)

    @property
    def bridge(self): return self.base.bridge
    @property
    def running(self): return self.base.running
    @running.setter
    def running(self, value): self.base.running = value
    @property
    def reason(self): return self.base.reason
    @reason.setter
    def reason(self, value): self.base.reason = value
    @property
    def live_bar(self): return self.base.live_bar
    @live_bar.setter
    def live_bar(self, value): self.base.live_bar = value
    @property
    def supervisor(self): return self.base.supervisor
    @supervisor.setter
    def supervisor(self, value): self.base.supervisor = value

    def _interval_seconds(self):
        feed = getattr(self.base.supervisor, 'feed', None) if self.base.supervisor else None
        if feed is not None:
            if hasattr(feed, 'seconds'):
                return feed.seconds
            if hasattr(feed, 'interval_seconds'):
                return feed.interval_seconds
        return 60

    def _stable_prefix(self):
        runtime = getattr(self.bridge, 'runtime', None)
        if runtime is not None:
            continuous = runtime.continuous_authorization()
            if continuous is not None:
                return continuous['prefix']
            campaign = runtime.active_campaign()
            if campaign is not None:
                return campaign['prefix']
        deployment_id = self.bridge.grid.deployment_id
        return deployment_id.split('-g', 1)[0]

    def _package_manifest(self, version):
        if version == LEGACY:
            return None
        cached = self._runners.get(version)
        if cached is not None:
            return cached[0]
        path = self.releases / version
        if not (path / 'manifest.json').exists():
            return None
        return load_package(path)

    def _is_decision_package(self, version):
        if version == LEGACY:
            return False
        try:
            manifest = self._package_manifest(version)
        except Exception:
            return False
        return manifest is not None and manifest.get('interface') == DECISION_INTERFACE

    def _expect_s01(self, version, client_id, side, quantity):
        self.gate()
        with self.store.atomic():
            self.store.db.execute('INSERT OR IGNORE INTO strategy_orders VALUES(?,?,?,?)',
                (client_id, version, str(self.base.last_bar or ''), int(side is Side.SELL)))
        self._expected = (client_id, side.value, quantity, 'true' if side is Side.SELL else 'false')

    def _load_decision_runner(self, version, *, manifest=None):
        cached = self._runners.get(version)
        if cached is not None:
            return cached
        path = self.releases / version
        manifest = load_package(path) if manifest is None else manifest
        if manifest['interface'] != DECISION_INTERFACE:
            raise ValueError('FACTORLAB_DECISION_V1 package required')
        interval_seconds = self._interval_seconds()
        if manifest.get('interval_seconds', 60) != interval_seconds:
            raise ValueError('strategy package interval does not match active feed')
        loaded = LoadedStrategy.from_strategy_package(path, manifest, trusted=True)
        loaded.spec.require(symbol='NVDAUSDT', interval_seconds=interval_seconds,
                            data_types=['closed_bar', 'quote', 'order_event'])
        prefix = self._stable_prefix()
        deployment_id = self.store.deployment_id_for(version, prefix)
        data_types = (['closed_bar'] if loaded.spec.decision_kind == 'target'
                      else ['closed_bar', 'quote', 'order_event'])
        journal = DecisionJournal(loaded, self.bridge.controller, deployment_id=deployment_id,
            symbol='NVDAUSDT', interval_seconds=interval_seconds, data_types=data_types)
        expect = lambda client_id, side, quantity: self._expect_s01(version, client_id, side, quantity)
        if loaded.spec.decision_kind == 'target':
            runner = DemoStrategyRuntime(self.bridge.session, SignalAdapter(journal),
                client_id_prefix=prefix + '-', expect=expect,
                max_notional=MAX_NOTIONAL, max_quantity=MAX_QUANTITY, preview=False,
                runtime_namespace=deployment_id)
        else:
            runner = OrderStrategyRuntime(journal, session=self.bridge.session, expect=expect,
                preview=False, max_notional=MAX_NOTIONAL, max_quantity=MAX_QUANTITY,
                client_id_prefix=deployment_id + '-', runtime_namespace=deployment_id)
        entry = (manifest, loaded, runner)
        self._runners[version] = entry
        return entry

    def _try_get_decision_runner(self, version):
        cached = self._runners.get(version)
        if cached is not None:
            return cached[2]
        try:
            return self._load_decision_runner(version)[2]
        except Exception:
            return None

    def _quiesce_active_decision_runner(self):
        active = self.store.snapshot()['active']
        if active == LEGACY or not self._is_decision_package(active):
            return
        runner = self._try_get_decision_runner(active)
        if runner is None:
            return
        runner.pause()
        if isinstance(runner, DemoStrategyRuntime):
            runner.clear_pending(freshness_boundary=self.bridge.controller.clock())

    def _rollback_on_strategy_error(self, error, runner=None):
        if runner is not None:
            try:
                runner.pause()
                if isinstance(runner, DemoStrategyRuntime):
                    runner.clear_pending(freshness_boundary=self.bridge.controller.clock())
            except Exception:
                pass
        self.store.rollback('STRATEGY_EXCEPTION:' + type(error).__name__)
        self.base.live_bar = False

    def _recover_account(self):
        """GET-reconcile shared account for non-legacy strategies without injecting askPrice quote conflicts."""
        session = self.bridge.session
        session.recover()
        self.bridge.runtime.release_unfilled_exit_reservations(self.bridge.gateway.state.orders)
        collection = self.base.supervisor.collection if self.base.supervisor else None
        if collection is None:
            book = session._fresh_book()
        else:
            previous = self.bridge.gateway.state.quotes.get('NVDAUSDT')
            market_after = 0 if previous is None else round(previous.observed_at.timestamp() * 1000)
            receipt = collection.receive_recent(collection.clock(), market_after=market_after)
            book = session._fresh_book(dict(receipt.payload))
        self.base.latest_book = book
        self.bridge.last_book = book
        if self.bridge.gateway.state.quotes.get('NVDAUSDT') is None:
            mid = (book['bidPrice'] + book['askPrice']) / Decimal(2)
            self.bridge.controller.quote(Quote('NVDAUSDT', mid, book['time_utc']))

    def gate(self):
        if self.maintenance:
            if self.base.control.desired() != 'RUNNING' or not self.base.authority():
                raise SafetyBlocked('existing authorization required for switch maintenance')
            return
        if self.store.pending():
            raise SafetyBlocked('strategy switch prohibits new openings')
        self.base.gate()

    def write_guard(self, method, params, plan, campaign, expected, runtime, facts):
        key = params['newClientOrderId' if method == 'POST' else 'origClientOrderId']
        row = self.store.db.execute('SELECT * FROM strategy_orders WHERE client_id=?', (key,)).fetchone()
        if row is None:
            return False
        effective_expected = expected
        if method == 'POST':
            state = self.store.snapshot()
            if not self.maintenance and (row['version'] != state['active'] or self.store.pending()):
                raise SafetyBlocked('inactive strategy order refused')
            if bool(row['reduce_only']) != (params['reduceOnly'] == 'true'):
                raise SafetyBlocked('strategy order ownership mismatch')
            if self._expected is not None and self._expected[0] == key:
                effective_expected = self._expected
        reservation = None
        if method == 'POST' and params['side'] == 'BUY':
            manifest = self._package_manifest(row['version'])
            configuration = manifest['configuration'] if manifest is not None else {}
            if 'exit_budget_notional' in configuration:
                reservation = (key.removesuffix('-entry') + '-exit',
                               Decimal(configuration['exit_budget_notional']))
        runtime.reserve(campaign, method, params, plan, expected=effective_expected, facts=facts,
                        dispatch_gate=self.gate, exit_reservation=reservation,
                        cancel_recovered_at=self.bridge.session.last_reconciled_at)
        return True

    def _refresh_readiness(self):
        desired = self.base.control.desired()
        allowed = desired == 'RUNNING' and self.base.authority()
        if not allowed or not self.running:
            self.base.require_fresh_bar()
            active = self.store.snapshot()['active']
            if active != LEGACY and self._is_decision_package(active):
                runner = self._try_get_decision_runner(active)
                if runner is not None:
                    runner.pause()
                    if isinstance(runner, DemoStrategyRuntime):
                        runner.clear_pending(freshness_boundary=self.base.eligible_after)
        self.running = allowed
        if not allowed:
            self.reason = desired if desired != 'RUNNING' else 'AUTHORIZATION_REQUIRED'
        return allowed

    def before_poll(self):
        state = self.store.snapshot()
        if self.store.pending() or state['active'] != LEGACY:
            if state['active'] == LEGACY:
                self.bridge.recover(collection=self.base.supervisor.collection if self.base.supervisor else None)
            else:
                self._recover_account()
            if self.base.control.desired() == 'RUNNING' and self.base.ensure_campaign:
                self.base.ensure_campaign()
            if not self._refresh_readiness():
                return
            self.deployer.step()
            state = self.store.snapshot()
            if self.store.pending():
                self.base.reason = 'STRATEGY_SWITCH_PENDING'
                return
            if state['active'] != LEGACY and self._is_decision_package(state['active']):
                runner = None
                try:
                    _, loaded, runner = self._load_decision_runner(state['active'])
                    if isinstance(runner, DemoStrategyRuntime):
                        runner.controller._atomic(lambda: runner._runtime().update(enabled=True))
                        runner.sync_reconciled()
                    else:
                        if runner._runtime()['status'] == 'PAUSED':
                            runner.controller._atomic(lambda: runner._runtime().update(status='STARTING'))
                        runner.sync_reconciled()
                        self.maintenance = True
                        try:
                            runner.dispatch_cancellations(self.bridge.controller.clock())
                        finally:
                            self.maintenance = False
                    if 'closed_bar' not in loaded.spec.data_types:
                        self.base.live_bar = True
                        self.base.reason = None
                    else:
                        self.base.reason = None if self.base.live_bar else 'FRESH_CLOSED_BAR_REQUIRED'
                except (StrategyWorkerError, ValueError, RuntimeError, TypeError) as error:
                    self._rollback_on_strategy_error(error, runner)
                    self.deployer.step()
                    self.base.reason = 'STRATEGY_SWITCH_PENDING' if self.store.pending() else 'FRESH_CLOSED_BAR_REQUIRED'
            else:
                self.base.reason = None if self.base.live_bar else 'FRESH_CLOSED_BAR_REQUIRED'
        else:
            self.base.before_poll()

    def history_watermark(self):
        """Current version's durable input boundary, independent of the feed cursor."""
        state = self.store.snapshot()
        if self.store.pending() or not self.running or not self._is_decision_package(state['active']):
            return None
        _, loaded, runner = self._load_decision_runner(state['active'])
        if 'closed_bar' not in loaded.spec.data_types:
            return None
        bars = runner._runtime()['bars']
        return bars[-1]['closed_at'] if bars else None

    def history_paused(self, reason):
        self.base.live_bar = False
        self.base.reason = reason

    def historical_bar(self, bar):
        state = self.store.snapshot()
        if self.store.pending():
            return False
        if state['active'] == LEGACY:
            return self.base.historical_bar(bar)
        if self._is_decision_package(state['active']) and self.running and bar.complete:
            try:
                _, loaded, runner = self._load_decision_runner(state['active'])
                if 'closed_bar' in loaded.spec.data_types:
                    if isinstance(runner, DemoStrategyRuntime):
                        runner.clear_pending(freshness_boundary=max(self.base.eligible_after, bar.closed_at))
                    return runner.historical_bar(bar)
            except (StrategyWorkerError, ValueError, RuntimeError, TypeError) as error:
                self._rollback_on_strategy_error(error)
        return False

    def closed_bar(self, bar):
        state = self.store.snapshot()
        if self.store.pending():
            return False
        if state['active'] == LEGACY:
            return self.base.closed_bar(bar)
        if (not self.running or not self._refresh_readiness()
                or bar.closed_at <= self.base.eligible_after or not bar.complete):
            return False
        if self._is_decision_package(state['active']):
            runner = None
            try:
                _, loaded, runner = self._load_decision_runner(state['active'])
                if 'closed_bar' not in loaded.spec.data_types:
                    self.base.last_bar = bar.closed_at
                    return False
                accepted = runner.closed_bar(bar)
                if accepted:
                    self.base.live_bar = True
                    self.base.last_bar = bar.closed_at
                    self.base.reason = None
                return accepted
            except (PersistenceConflict, SafetyBlocked, DataQualityError):
                raise
            except (StrategyWorkerError, ValueError, RuntimeError, TypeError, KeyError) as error:
                self._rollback_on_strategy_error(error, runner)
                return False
        mark = bar.closed_at.isoformat()
        if self.store.db.execute('SELECT 1 FROM decisions WHERE version=? AND watermark=?',
                                 (state['active'], mark)).fetchone():
            return False
        try:
            answer = invoke(self.releases / state['active'], dict(event='closed_bar',
                bar=json.loads(json.dumps(asdict(bar), default=str)),
                account=json.loads(json.dumps(self.bridge.session.status(), default=str))), state['checkpoint'])
            self.validate_intents(answer['intents'])
            with self.store.atomic():
                self.store.db.execute('INSERT INTO decisions VALUES(?,?,?)',
                    (state['active'], mark, encoded(answer).decode()))
                self.store.db.execute('UPDATE deployment SET checkpoint=?,watermark=? WHERE id=1',
                    (encoded(answer['state']).decode(), mark))
            self.base.live_bar = True
            self.base.last_bar = bar.closed_at
            self.base.reason = None
            return True
        except Exception as error:
            self.store.rollback('STRATEGY_EXCEPTION:' + type(error).__name__)
            self.base.live_bar = False
            return False

    @staticmethod
    def validate_intents(intents):
        for intent in intents:
            if set(intent) != {'side', 'quantity'} or intent['side'] not in {'BUY', 'SELL'}:
                raise ValueError('only existing Demo long LIMIT intents supported')
            quantity = Decimal(intent['quantity'])
            if not quantity.is_finite() or quantity <= 0:
                raise ValueError('positive finite strategy quantity required')

    def send(self, version, mark, intent, *, index=0, maintenance=False):
        campaign = self.bridge.runtime.active_campaign()
        key = campaign['prefix'] + '-' + digest([version, mark, index])[:15]
        session = self.bridge.session
        # Any recorded identity, including an unknown result, is recovered instead of submitted again.
        if key in session._protocol_ids():
            return
        with self.store.atomic():
            self.store.db.execute('INSERT OR IGNORE INTO strategy_orders VALUES(?,?,?,?)',
                (key, version, mark, int(intent['side'] == 'SELL')))
        side, quantity = Side(intent['side']), Decimal(intent['quantity'])
        if maintenance:
            self.base.latest_book = session._fresh_book()
        # Exact price is supplied by DemoSession and the existing expected-intent guard still checks quantity.
        self._expected = None
        self.bridge.expect(type('Expected', (), dict(client_order_id=key, side=side, quantity=quantity,
                            reduce_only=side is Side.SELL, limit_price=session.limit_price(
                            session.gateway.rules, side, self.base.latest_book)))())
        self.maintenance = maintenance
        try:
            session.submit_limit(key, side, quantity, reduce_only=side is Side.SELL,
                                 book=self.base.latest_book)
        finally:
            self.maintenance = False

    def quote(self, quote):
        if self.store.pending():
            return None
        state = self.store.snapshot()
        if state['active'] == LEGACY:
            return self.base.quote(quote)
        if self._is_decision_package(state['active']):
            if not self.running or not self._refresh_readiness():
                return None
            runner = None
            try:
                _, loaded, runner = self._load_decision_runner(state['active'])
                if isinstance(runner, DemoStrategyRuntime):
                    if not self.live_bar:
                        previous = self.bridge.gateway.state.quotes.get(quote.instrument_id)
                        if previous is None or quote.observed_at > previous.observed_at:
                            self.bridge.controller.quote(quote)
                        return None
                    return runner.quote(quote)
                if quote.observed_at <= self.base.eligible_after:
                    return runner.dispatch_cancellations(quote.observed_at)
                if 'closed_bar' in loaded.spec.data_types and not self.live_bar:
                    previous = self.bridge.gateway.state.quotes.get(quote.instrument_id)
                    if previous is None or quote.observed_at > previous.observed_at:
                        self.bridge.controller.quote(quote)
                    return runner.dispatch_cancellations(quote.observed_at)
                return runner.quote(quote)
            except SafetyBlocked as error:
                self.reason = str(error)
                raise FeedPaused(self.reason) from error
            except (DataQualityError, PersistenceConflict, OSError):
                raise
            except RiskRejected:
                return None
            except (StrategyWorkerError, ValueError, RuntimeError, TypeError, KeyError) as error:
                if isinstance(error, ValueError) and str(error) in {
                    'fresh quote required', 'future quote', 'quote time regression',
                }:
                    self.reason = str(error)
                    raise FeedPaused(self.reason) from error
                self._rollback_on_strategy_error(error, runner)
                return None
        previous = self.bridge.gateway.state.quotes.get(quote.instrument_id)
        if previous is None or quote.observed_at >= previous.observed_at:
            self.bridge.controller.quote(quote)
        if not self.running or not self.live_bar or not self._refresh_readiness() or not state['watermark']:
            return None
        row = self.store.db.execute('SELECT result FROM decisions WHERE version=? AND watermark=?',
                                   (state['active'], state['watermark'])).fetchone()
        if row is None:
            return None
        # Decision must be causal and expire at the next bar; a recovered decision is never replayed.
        mark = datetime.fromisoformat(state['watermark'])
        if not mark < quote.observed_at < mark + timedelta(seconds=self._interval_seconds()):
            return None
        if any(o['status'] not in TERMINAL for o in self.bridge.session.status()['orders'].values()):
            return None
        for index, intent in enumerate(json.loads(row[0])['intents']):
            self.gate()
            self.send(state['active'], state['watermark'], intent, index=index)

    def maintain(self, raw):
        self.base.set_latest_book(raw)
        if not self.store.pending() and self.store.snapshot()['active'] == LEGACY:
            return self.base.maintain(raw)

    def status(self):
        result = self.base.status()
        state = self.store.summary()
        active = state['active']
        if active == LEGACY:
            state['active_protocol'] = LEGACY
            state['active_identity'] = None
        else:
            manifest = None
            try:
                manifest = self._package_manifest(active)
            except Exception:
                manifest = None
            state['active_protocol'] = manifest['interface'] if manifest else 'unknown'
            state['active_identity'] = strategy_package_identity(manifest) if manifest else None
            result.update(effective='RUNNING' if self.running and not self.reason else 'PAUSED',
                          grid_state='RUNNING', grid_reason=self.reason, reason=self.reason)
        result['strategy_release'] = state
        return result
