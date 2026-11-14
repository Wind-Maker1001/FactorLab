"""Current-user Demo runtime and restart-safe write budgets."""

from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
import sqlite3
from uuid import NAMESPACE_URL, uuid4, uuid5

from factorlab.trading.control import RiskLimits

from .demo_session import DEMO, SYMBOL
from .network import DemoExecutionPlan, DemoNetworkAuthorization
from .protocol import SafetyBlocked


MAX_QUANTITY = Decimal('0.04')
MAX_NOTIONAL = Decimal('7')
SMOKE_CAMPAIGN = 'smoke-v1'
LIMIT_POLICY = 'nvda-long-inside-limit-v1'


def now():
    return datetime.now(timezone.utc)


def journal_id(account_id):
    return uuid5(NAMESPACE_URL, 'factorlab-binance-demo:' + account_id).hex


def state_root():
    root = Path(os.environ['LOCALAPPDATA']) / 'FactorLab' / 'BinanceDemo'
    root.mkdir(parents=True, exist_ok=True)
    return root


@contextmanager
def operator_lock(root):
    """Serialize sessions opening both ledgers, across terminals and worktrees."""
    import msvcrt

    with (root / 'session.lock').open('a+b') as lock:
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            raise SafetyBlocked('another Demo session is active') from None
        yield


def private_binding(store):
    config = store.read_config()
    if config is None:
        raise SafetyBlocked('Demo account binding missing; run setup')
    if (config['version'], config['environment'], config['host'], config['credential_reference'], config['symbols']) != (
        1, DEMO.name, DEMO.host, store.reference, [SYMBOL]
    ):
        raise SafetyBlocked('Demo binding version, host or credential reference mismatch')
    return config


def bind_account(store, credential, facts, configuration):
    existing = store.read_config()
    if existing is not None and existing['account_id'] != facts.account_id:
        raise SafetyBlocked('Demo credential does not match the persisted account binding')
    config = dict(version=1, environment=DEMO.name, host=DEMO.host, credential_reference=store.reference,
        account_id=facts.account_id, configuration=configuration, symbols=[SYMBOL],
        journal_id=journal_id(facts.account_id))
    store.write(credential)
    store.write_config(config)
    return config


class DemoRuntime:
    """SQLite journals consumed dispatches before network IO and keeps a stable smoke budget."""

    def __init__(self, root):
        self.db = sqlite3.connect(root / 'runtime.sqlite3', isolation_level='IMMEDIATE')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS runtime (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS campaigns (id TEXT PRIMARY KEY, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS dispatches (
                campaign TEXT NOT NULL, method TEXT NOT NULL, client_id TEXT NOT NULL,
                parameters TEXT NOT NULL, receipt TEXT,
                PRIMARY KEY (campaign, method, client_id)
            );
        ''')

    def close(self):
        self.db.close()

    def _get(self, key):
        row = self.db.execute('SELECT value FROM runtime WHERE key = ?', (key,)).fetchone()
        return row[0] if row else None

    def _set(self, key, value):
        self.db.execute('INSERT OR REPLACE INTO runtime VALUES (?, ?)', (key, value))

    def stop(self):
        with self.db:
            self._set('active', '')
            self._set('stop_epoch', uuid4().hex)

    def stop_epoch(self):
        return self._get('stop_epoch')

    def start(self, binding, *, seconds, orders, opening_equity, max_loss, stop_epoch, started_at, smoke_id=None):
        smoke = smoke_id is not None
        if seconds <= 0 or not 1 <= orders <= 1000 or not Decimal(0) < max_loss <= MAX_NOTIONAL:
            raise SafetyBlocked('Demo budgets require a positive duration, 1–1000 POSTs and a positive loss cap up to 7 USDT')
        if smoke and (seconds > 900 or orders not in (1, 2)):
            raise SafetyBlocked('the one-time Demo budget permits at most two POSTs within fifteen minutes')
        expires_at = started_at + timedelta(seconds=seconds)
        campaign = smoke_id if smoke else 'demo-' + uuid4().hex
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            if stop_epoch != self.stop_epoch():
                raise SafetyBlocked('Demo was stopped during account preflight')
            active = self.active_campaign()
            if active is not None:
                raise SafetyBlocked('an existing Demo campaign is active; stop it before enabling another')
            if self.db.execute('SELECT 1 FROM campaigns WHERE id = ?', (campaign,)).fetchone():
                raise SafetyBlocked('the one-time Demo smoke budget was already started; use recover')
            if now() >= expires_at:
                raise SafetyBlocked('Demo authorization expired during account preflight')
            config = dict(id=campaign, journal_id=journal_id(binding['account_id']), prefix='fl-' + uuid4().hex[:16],
                expires_at=expires_at.isoformat(), seconds=seconds, orders=orders,
                max_loss=str(max_loss), opening_equity=str(opening_equity), smoke=smoke, policy=LIMIT_POLICY,
                account_configuration=binding['configuration'])
            self.db.execute('INSERT INTO campaigns VALUES (?, ?)', (campaign, json.dumps(config)))
            self._set('active', campaign)
        return config

    def active_campaign(self):
        key = self._get('active')
        if key:
            campaign = self.campaign(key)
            if now() < datetime.fromisoformat(campaign['expires_at']):
                return campaign
        return None

    def campaign(self, key):
        row = self.db.execute('SELECT payload FROM campaigns WHERE id = ?', (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def tradfi_receipt(self, account_id):
        saved = self._get('tradfi-agreement:' + journal_id(account_id))
        return json.loads(saved) if saved is not None else None

    def reserve_tradfi(self, account_id, receipt):
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            if self.tradfi_receipt(account_id) is not None:
                raise SafetyBlocked('this Demo account already has a recorded agreement attempt')
            self._set('tradfi-agreement:' + journal_id(account_id), json.dumps(receipt))

    def save_tradfi(self, account_id, receipt):
        with self.db:
            self._set('tradfi-agreement:' + journal_id(account_id), json.dumps(receipt))

    def report(self, smoke_id):
        campaign = self.active_campaign()
        smoke = self.campaign(smoke_id)
        return dict(enabled=campaign is not None, expires_at=campaign['expires_at'] if campaign else None,
            authorization_kind=('SMOKE' if campaign['smoke'] else 'PERSISTENT') if campaign else None,
            policy=campaign['policy'] if campaign else None,
            post_budget=campaign['orders'] if campaign else None,
            posts_consumed=self.post_count(campaign['id']) if campaign else None,
            limits=dict(symbol=SYMBOL, max_quantity=str(MAX_QUANTITY), max_notional_usdt=str(MAX_NOTIONAL),
                max_open_orders=1, max_loss_usdt=campaign['max_loss']) if campaign else None,
            smoke_started=smoke is not None)

    def post_count(self, campaign):
        return self.db.execute("SELECT count(*) FROM dispatches WHERE campaign = ? AND method = 'POST'",
            (campaign,)).fetchone()[0]

    def receipts(self, campaign):
        rows = self.db.execute('SELECT method, client_id, receipt FROM dispatches WHERE campaign = ? ORDER BY rowid',
            (campaign,))
        return [json.loads(receipt) if receipt is not None else dict(method=method, client_order_id=client_id,
            http_status=None, dispatch='RESERVED_RESPONSE_UNCONFIRMED') for method, client_id, receipt in rows]

    def authority(self, binding, campaign):
        if campaign is None or campaign['journal_id'] != journal_id(binding['account_id']):
            raise SafetyBlocked('no active Demo authorization for this account')
        if campaign.get('policy') != LIMIT_POLICY:
            raise SafetyBlocked('persisted Demo execution policy is unsupported')
        if campaign['account_configuration'] != binding['configuration']:
            raise SafetyBlocked('Demo account configuration differs from the enabled campaign')
        plan = DemoExecutionPlan(account_id=binding['account_id'], order_types=frozenset({'LIMIT'}),
            max_quantity=MAX_QUANTITY, max_notional=MAX_NOTIONAL, max_orders=campaign['orders'], max_open_orders=1,
            max_seconds=campaign['seconds'], max_loss=Decimal(campaign['max_loss']), max_margin=MAX_NOTIONAL,
            expires_at=datetime.fromisoformat(campaign['expires_at']), client_id_prefix=campaign['prefix'],
            cleanup='CANCEL_KNOWN_THEN_STOP', recovery='GET_RECONCILE_NO_RESUBMIT')
        return DemoNetworkAuthorization(plan)

    def reserve(self, campaign, method, params, plan, *, expected, facts):
        """Consume each admitted POST/DELETE exactly once, including unknown responses."""
        client_id = params['newClientOrderId' if method == 'POST' else 'origClientOrderId']
        if params['symbol'] != SYMBOL or not client_id.startswith(campaign['prefix'] + '-'):
            raise SafetyBlocked('order is outside the active Demo campaign')
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            if self._get('active') != campaign['id'] or now() >= plan.expires_at:
                raise SafetyBlocked('Demo authorization stopped or expired')
            if method == 'POST':
                actual = (client_id, params['side'], Decimal(params['quantity']), params['reduceOnly'])
                if expected is not None and len(expected) == 5:
                    actual += (Decimal(params['price']),)
                if expected is None or actual != expected:
                    raise SafetyBlocked('Demo order differs from the admitted runtime intent')
                if (params['type'], params['timeInForce'], params['positionSide']) != ('LIMIT', 'GTC', 'BOTH'):
                    raise SafetyBlocked('Demo runtime accepts one-way LIMIT GTC only')
                count = self.post_count(campaign['id'])
                if count >= campaign['orders']:
                    raise SafetyBlocked('persistent Demo POST budget exhausted')
                if campaign['smoke'] and (count, params['side'], params['reduceOnly']) not in {
                    (0, 'BUY', 'false'), (1, 'SELL', 'true'),
                }:
                    raise SafetyBlocked('Demo smoke permits only one entry and one confirmed reduction')
                funds = facts.usdt()
                if Decimal(campaign['opening_equity']) - (funds['walletBalance'] + funds['unrealizedProfit']) >= plan.max_loss:
                    raise SafetyBlocked('persistent Demo loss budget exhausted')
                quantity = Decimal(params['quantity'])
                position = sum((p.quantity for p in facts.shared().positions), Decimal(0))
                if ((params['side'] == 'BUY' and not 0 <= position <= MAX_QUANTITY - quantity)
                    or (params['side'] == 'SELL' and not 0 < quantity <= position)):
                    raise SafetyBlocked('Demo position exceeds the authorized net quantity or reduction')
            elif not self.db.execute("SELECT 1 FROM dispatches WHERE campaign=? AND method='POST' AND client_id=?",
                    (campaign['id'], client_id)).fetchone():
                raise SafetyBlocked('Demo cancellation requires an order from this campaign')

            inserted = self.db.execute('INSERT OR IGNORE INTO dispatches VALUES (?, ?, ?, ?, NULL)',
                (campaign['id'], method, client_id, json.dumps(params))).rowcount
            if not inserted:
                raise SafetyBlocked('persistent Demo dispatch already consumed; use recover')

    def record_receipt(self, campaign, receipt):
        with self.db:
            self.db.execute('UPDATE dispatches SET receipt=? WHERE campaign=? AND method=? AND client_id=?',
                (json.dumps(receipt), campaign['id'], receipt['method'], receipt['client_order_id']))
            if receipt['http_status'] != 200:
                self._set('active', '')
                self._set('stop_epoch', uuid4().hex)


DEMO_LIMITS = RiskLimits(MAX_NOTIONAL, MAX_NOTIONAL, MAX_NOTIONAL, orders_per_minute=20)


@contextmanager
def open_grid_session(root, store, grid_factory=None, *, read_only=False, new_deployment=False,
                      preview=False, continuous=False, request_counts=None, dispatch_gate=None):
    """Open one campaign-bound Grid bridge over the shared Demo journals."""
    from factorlab.trading.storage import PaperStore, dumps, decode

    from .demo_grid_bridge import DemoGridBridge
    from .demo_session import DemoSession

    if preview:
        root = root / 'grid-preview'
        root.mkdir(parents=True, exist_ok=True)
    with operator_lock(root), closing(DemoRuntime(root)) as runtime:
        binding = private_binding(store)
        credential = store.read()
        if credential is None:
            raise SafetyBlocked('saved Demo credential missing; run setup')
        campaign = dict(id='grid-preview', prefix='preview') if preview else runtime.active_campaign()
        writes = not read_only and not preview
        if writes and (campaign is None or campaign['smoke']):
            raise SafetyBlocked('no persistent Demo authorization; run enable-demo')
        authorization = runtime.authority(binding, campaign) if writes else None
        expected = None

        def expect(intent):
            nonlocal expected
            expected = (intent.client_order_id, intent.side.value, intent.quantity,
                'true' if intent.reduce_only else 'false', intent.limit_price)

        def guard(method, params, plan):
            if dispatch_gate is not None:
                dispatch_gate()
            runtime.reserve(campaign, method, params, plan, expected=expected, facts=session.adapter.facts)

        def record(receipt):
            runtime.record_receipt(campaign, receipt)

        journal = root / journal_id(binding['account_id'])
        journal.mkdir(exist_ok=True)
        capacity = dict(continuous_capacity=True) if continuous else {}
        with (closing(PaperStore(journal / 'protocol.sqlite3', **capacity)) as protocol_store,
              closing(PaperStore(journal / 'controller.sqlite3', **capacity)) as controller_store):
            session = DemoSession(credential, protocol_store=protocol_store, controller_store=controller_store,
                account_id=binding['account_id'], configuration=binding['configuration'],
                authorization=authorization, limits=DEMO_LIMITS,
                write_guard=guard if writes else None, receipt_sink=record if writes else None,
                request_counts=request_counts, get_confirmed_writes=continuous)
            key = 'demo-grid:deployment'
            saved = saved_grid_deployment(controller_store)
            if new_deployment:
                if read_only:
                    raise SafetyBlocked('new Grid deployment requires explicit write mode')
                if saved is not None and decode(json.loads(saved))['campaign'] == campaign['id']:
                    raise SafetyBlocked('current campaign already has a Grid deployment; reuse it')
                session.recover()
                status = session.status()
                if not status['reconciled'] or status['positions'] or status['open_orders'] or status['unknown']:
                    raise SafetyBlocked('new Grid deployment requires a reconciled flat account without open or UNKNOWN orders')
                saved = None
            if saved is not None:
                deployment = decode(json.loads(saved))
                if (writes or preview) and deployment['campaign'] != campaign['id']:
                    raise SafetyBlocked('persisted Grid deployment or Demo campaign differs')
                if grid_factory is None or read_only:
                    grid = restore_grid(deployment['deployment'])
                else:
                    grid = grid_factory(session, campaign['prefix'] + '-g')
                    if dumps(grid.deployment()) != dumps(deployment['deployment']):
                        raise SafetyBlocked('persisted Grid deployment differs')
            else:
                if read_only or grid_factory is None:
                    raise SafetyBlocked('no persisted Grid deployment')
                deployment_id = campaign['prefix'] + '-g'
                grid = grid_factory(session, deployment_id)
                if grid.deployment_id != deployment_id:
                    raise SafetyBlocked('Grid deployment identity differs from the active Demo campaign')
                deployment = dict(campaign=campaign['id'], deployment=grid.deployment())
            if continuous:
                grid.enable_continuous_capacity()
            bridge = DemoGridBridge(grid, session, expect=expect if writes else None,
                read_only=read_only, preview=preview)
            with controller_store.transaction():
                controller_store.record_metadata(key + ':' + deployment['campaign'], deployment)
            bridge.runtime = runtime
            yield bridge


def saved_grid_deployment(store):
    """Read the latest immutable campaign binding, including the original journal key."""
    row = store.db.execute("SELECT payload FROM events WHERE id = 'demo-grid:deployment' "
        "OR id LIKE 'demo-grid:deployment:%' ORDER BY rowid DESC LIMIT 1").fetchone()
    return row[0] if row else None


def restore_grid(deployment):
    """Reconstruct the exact journaled economic deployment, before command replay."""
    from factorlab.backtest.fees import FeeRule, FeeSchedule
    from factorlab.strategies.grid import GridConfig
    from factorlab.strategies.grid_execution import GridExecution, GridRules

    values = dict(deployment)
    values['config'] = GridConfig(**values['config'])
    values['rules'] = GridRules(**values['rules'])
    values['fees'] = FeeSchedule([FeeRule(**rule) for rule in values['fees']])
    return GridExecution(**values)
