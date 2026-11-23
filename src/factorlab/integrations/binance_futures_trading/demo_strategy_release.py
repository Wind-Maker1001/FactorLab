"""Strategy router inside the existing Grid execution owner and DemoSupervisor."""
from dataclasses import asdict
from decimal import Decimal
import json
import os
from pathlib import Path
from time import time

from factorlab.trading import Side
from .protocol import SafetyBlocked
from .strategy_deployment import DeploymentStore, DeploymentController, LEGACY, TERMINAL
from .strategy_package import digest, encoded, load_package
from .strategy_worker import invoke


class DemoReleaseAdapter:
    def __init__(self, router):
        self.router = router
        self.boot_id = str(os.getpid())

    def quiesce(self):
        self.router.base.bridge.withdraw_unsubmitted()
        self.router.base.live_bar = False

    def reconcile(self):
        s = self.router.bridge.session.status()
        untracked = [o['client_order_id'] for o in s['open_orders'] or []
                     if o['client_order_id'] not in s['orders']]
        return dict(reconciled=s['reconciled'], unknown=s['unknown'], positions=s['positions'],
            orders=[dict(client_id=k, status=v['status'], reduce_only=v['reduce_only'])
                    for k, v in s['orders'].items()], account_observed_at=str(s['account_observed_at']),
                    untracked_orders=untracked)

    def watermark(self):
        r = self.router
        cursor = r.bridge.controller.store.db.execute('SELECT COALESCE(MAX(rowid),0) FROM events').fetchone()[0]
        return json.dumps(dict(controller_event=cursor, market=str(r.base.last_bar)), sort_keys=True)

    def cancel(self, client_id):
        r = self.router
        if r.bridge._delete_already_consumed(client_id):
            return  # GET recovery must establish its terminal state; never repeat DELETE.
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
        load_package(path)
        answer = invoke(path, dict(event='probe', environment='BINANCE_USDM_DEMO'), {})
        if answer['intents']:
            raise ValueError('probe must not generate intents')

    def activated(self, version):
        r = self.router
        r.base.require_fresh_bar()
        r.base.running = True
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
        if method == 'POST':
            state = self.store.snapshot()
            if not self.maintenance and (row['version'] != state['active'] or self.store.pending()):
                raise SafetyBlocked('inactive strategy order refused')
            if bool(row['reduce_only']) != (params['reduceOnly'] == 'true'):
                raise SafetyBlocked('strategy order ownership mismatch')
        runtime.reserve(campaign, method, params, plan, expected=expected, facts=facts,
                        dispatch_gate=self.gate)
        return True

    def before_poll(self):
        if self.store.pending() or self.store.snapshot()['active'] != LEGACY:
            self.bridge.recover(collection=self.base.supervisor.collection if self.base.supervisor else None)
            if self.base.control.desired() != 'RUNNING':
                self.base.running = False
                self.base.reason = self.base.control.desired()
                return
            if self.base.ensure_campaign:
                self.base.ensure_campaign()
            self.base.running = self.base.authority()
            if not self.base.running:
                self.base.reason = 'AUTHORIZATION_REQUIRED'
                return
            self.deployer.step()
            if self.store.pending():
                self.base.reason = 'STRATEGY_SWITCH_PENDING'
            else:
                self.base.reason = None if self.base.live_bar else 'FRESH_CLOSED_BAR_REQUIRED'
        else:
            self.base.before_poll()

    def historical_bar(self, bar):
        if self.store.snapshot()['active'] == LEGACY and not self.store.pending():
            return self.base.historical_bar(bar)
        return False

    def closed_bar(self, bar):
        state = self.store.snapshot()
        if self.store.pending():
            return False
        if state['active'] == LEGACY:
            return self.base.closed_bar(bar)
        if not self.running or bar.closed_at <= self.base.eligible_after or not bar.complete:
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
        self.bridge.controller.quote(quote)
        if not self.running or not self.base.live_bar or not state['watermark']:
            return None
        row = self.store.db.execute('SELECT result FROM decisions WHERE version=? AND watermark=?',
                                   (state['active'], state['watermark'])).fetchone()
        if row is None:
            return None
        # Decision must be causal and expire at the next bar; a recovered decision is never replayed.
        from datetime import datetime, timedelta
        mark = datetime.fromisoformat(state['watermark'])
        if not mark < quote.observed_at < mark + timedelta(seconds=self.base.supervisor.feed.interval_seconds
                if hasattr(self.base.supervisor.feed, 'interval_seconds') else 60):
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
        if state['active'] != LEGACY:
            result.update(effective='RUNNING' if self.running and not self.reason else 'PAUSED',
                          grid_state='RUNNING', grid_reason=self.reason, reason=self.reason)
        result['strategy_release'] = state
        return result
