"""Current-user Demo runtime and restart-safe write budgets."""

from contextlib import ExitStack, closing, contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
import sqlite3
from uuid import NAMESPACE_URL, uuid4, uuid5

from factorlab.trading.control import RiskLimits

from .demo_session import DEMO, SYMBOL, receipt_confirmed
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
    configured = os.environ.get('FACTORLAB_DEMO_STATE_ROOT')
    if configured:
        root = Path(configured)
        if not root.is_absolute():
            raise SafetyBlocked('Demo state root must be absolute')
        if os.name != 'nt':
            from .demo_cloud_storage import storage_metrics
            storage_metrics(root)
            # SQLite and account subdirectories inherit this process-wide privacy policy.
            os.umask(0o077)
        else:
            root.mkdir(parents=True, exist_ok=True)
        return root.resolve()
    if os.name != 'nt':
        raise SafetyBlocked('explicit persistent Demo state root required')
    root = Path(os.environ['LOCALAPPDATA']) / 'FactorLab' / 'BinanceDemo'
    root.mkdir(parents=True, exist_ok=True)
    return root


@contextmanager
def operator_lock(root, *, allow_quarantined=False):
    """Serialize sessions opening both ledgers, across terminals and worktrees."""
    root = Path(root)

    def check_quarantine():
        if not allow_quarantined and (root / 'restore-quarantine.json').exists():
            raise SafetyBlocked('restored Demo state is restricted to cloud GET-only recovery')

    if os.name == 'nt':
        import msvcrt
        with (root / 'session.lock').open('a+b') as lock:
            lock.seek(0)
            try:
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                raise SafetyBlocked('another Demo session is active') from None
            check_quarantine()
            yield
    else:
        import fcntl
        flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC
        with os.fdopen(os.open(root / 'session.lock', flags, 0o600), 'a+b') as lock:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise SafetyBlocked('another Demo session is active') from None
            try:
                check_quarantine()
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


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
        self.root = root
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
            CREATE TABLE IF NOT EXISTS exit_budget_reservations (
                authorization TEXT NOT NULL, entry_id TEXT PRIMARY KEY,
                exit_id TEXT UNIQUE NOT NULL, notional TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS dispatch_retry_history (
                campaign TEXT NOT NULL, method TEXT NOT NULL, client_id TEXT NOT NULL,
                parameters TEXT NOT NULL, receipt TEXT
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
        from .demo_cloud_storage import assert_source_controls_available

        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            assert_source_controls_available(self.root)
            authorization = self.continuous_authorization()
            if authorization is not None and authorization['state'] != 'HALTED':
                authorization['state'] = 'STOPPED'
                self._set('continuous', json.dumps(authorization))
            self._set('continuous_window', '')
            self._set('active', '')
            self._set('stop_epoch', uuid4().hex)

    def continuous_authorization(self):
        value = self._get('continuous')
        return json.loads(value) if value is not None else None

    def global_trading_capacity(self):
        """Report remaining cumulative order/notional capacity without granting writes."""
        authorization = self.continuous_authorization()
        if authorization is None:
            return True
        consumed = self.consumption(authorization)
        return (consumed['posts'] < authorization['total_orders']
                and Decimal(consumed['submitted_notional']) < Decimal(authorization['total_notional']))

    def _continuous_campaigns(self, authorization):
        return [campaign for (payload,) in self.db.execute('SELECT payload FROM campaigns ORDER BY rowid')
                if (campaign := json.loads(payload))['journal_id'] == authorization['journal_id']
                and not campaign['smoke']]

    def consumption(self, authorization):
        # The bound account owns this journal, including smoke and portfolio dispatches.
        count, notional = 0, Decimal(0)
        for (payload,) in self.db.execute(
                "SELECT parameters FROM dispatches WHERE method='POST' UNION ALL "
                "SELECT parameters FROM dispatch_retry_history WHERE method='POST'"):
            params = json.loads(payload)
            count += 1
            if 'quantity' in params:
                notional += Decimal(params['quantity']) * Decimal(params['price'])
        return dict(posts=count, submitted_notional=str(notional))

    def check_account_budget(self, params, facts, *, reserve_posts=0, reserve_notional=Decimal(0),
                             consume_exit=None, released_exit_posts=0,
                             released_exit_notional=Decimal(0)):
        """Called inside the owner's dispatch transaction, including portfolio dispatches."""
        authorization = self.continuous_authorization()
        if authorization is None or authorization['state'] != 'RUNNING':
            raise SafetyBlocked('running cumulative Demo authorization required')
        funds = facts.usdt()
        if Decimal(authorization['opening_equity']) - (funds['walletBalance'] + funds['unrealizedProfit']) >= Decimal(authorization['max_loss']):
            raise SafetyBlocked('global Demo loss budget exhausted')
        consumed = self.consumption(authorization)
        held = self.db.execute('SELECT notional FROM exit_budget_reservations WHERE authorization=? '
                              'AND (? IS NULL OR exit_id<>?)',
                              (authorization['id'], consume_exit, consume_exit)).fetchall()
        notional = Decimal(0) if 'leverage' in params else Decimal(params['quantity']) * Decimal(params['price'])
        if (consumed['posts'] + len(held) - released_exit_posts + 1 + reserve_posts > min(100000, authorization['total_orders'])
                or Decimal(consumed['submitted_notional']) + sum((Decimal(row[0]) for row in held), Decimal(0))
                    - released_exit_notional + notional + reserve_notional
                    > min(Decimal('500000'), Decimal(authorization['total_notional']))):
            raise SafetyBlocked('global Demo trading budget exhausted or reserved for exit')

    def reserve_instance(self, binding, method, params, plan, *, intent, facts,
                         exit_reservation=None, exit_consumption=(), cancel_recovered_at=None):
        """The existing Execute owner consumes every instance's dispatch before IO."""
        client_id = params['newClientOrderId' if method == 'POST' else 'origClientOrderId']
        if (binding['account_id'] != plan.account_id or params['symbol'] != intent.instrument_id
                or client_id != intent.client_order_id or intent.instrument_id != plan.symbol):
            raise SafetyBlocked('instance dispatch identity mismatch')
        campaign = 'instance:' + binding['instance_id']
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            authorization = self.continuous_authorization()
            if (authorization is None or authorization['state'] != 'RUNNING'
                    or authorization['account_id'] != binding['account_id']
                    or authorization['stop_epoch'] != self.stop_epoch()
                    or now() >= plan.expires_at):
                raise SafetyBlocked('account execution stopped or expired')
            if method == 'POST':
                from .rules import order_parameters
                if params != order_parameters(intent):
                    raise SafetyBlocked('instance dispatch differs from persisted intent')
                if self.db.execute("SELECT 1 FROM dispatches WHERE method='POST' AND client_id=?", (client_id,)).fetchone():
                    raise SafetyBlocked('instance order already attempted; GET recovery required')
                reserved = self.db.execute(
                    'SELECT entry_id,notional FROM exit_budget_reservations WHERE authorization=? AND exit_id=?',
                    (authorization['id'], client_id)).fetchone()
                notional = Decimal(params['quantity']) * Decimal(params['price'])
                if reserved is not None:
                    if (not intent.reduce_only or notional > Decimal(reserved[1])
                            or not self.db.execute(
                                "SELECT 1 FROM dispatches WHERE campaign=? AND method='POST' AND client_id=?",
                                (campaign, reserved[0])).fetchone()):
                        raise SafetyBlocked('reserved instance exit must reduce its owned entry within budget')
                if not intent.reduce_only:
                    if exit_reservation is None:
                        raise SafetyBlocked('instance opening order requires a funded exit reservation')
                    exit_id, exit_notional = exit_reservation
                    if (not exit_id or exit_id == client_id or not exit_notional.is_finite()
                            or exit_notional < notional):
                        raise SafetyBlocked('invalid instance exit reservation')
                    if self.db.execute("SELECT 1 FROM dispatches WHERE method='POST' AND client_id=?",
                                       (exit_id,)).fetchone():
                        raise SafetyBlocked('reserved instance exit identity was already consumed')
                elif exit_reservation is not None:
                    raise SafetyBlocked('reduction cannot reserve another opening exit')
                consumed_reservations = []
                if exit_consumption:
                    if not intent.reduce_only or reserved is not None:
                        raise SafetyBlocked('exit consumption requires a distinct owned reduction')
                    for entry_id, amount in exit_consumption:
                        row = self.db.execute(
                            'SELECT notional FROM exit_budget_reservations WHERE authorization=? AND entry_id=?',
                            (authorization['id'], entry_id)).fetchone()
                        entry_dispatch = self.db.execute(
                            "SELECT parameters FROM dispatches WHERE campaign=? AND method='POST' AND client_id=?",
                            (campaign, entry_id)).fetchone()
                        if (row is None or not amount.is_finite() or amount <= 0 or amount > Decimal(row[0])
                                or entry_dispatch is None
                                or json.loads(entry_dispatch[0])['symbol'] != intent.instrument_id):
                            raise SafetyBlocked('exit consumption exceeds owned entry reservation')
                        consumed_reservations.append((entry_id, amount, Decimal(row[0])))
                    if len({entry_id for entry_id, _, _ in consumed_reservations}) != len(consumed_reservations):
                        raise SafetyBlocked('entry reservation cannot be consumed twice')
                self.check_account_budget(params, facts,
                    released_exit_posts=sum(amount == held for _, amount, held in consumed_reservations),
                    released_exit_notional=sum((amount for _, amount, _ in consumed_reservations), Decimal(0)),
                    reserve_posts=int(exit_reservation is not None),
                    reserve_notional=exit_reservation[1] if exit_reservation is not None else Decimal(0),
                    consume_exit=client_id if reserved is not None else None)
                for entry_id, amount, held in consumed_reservations:
                    if amount == held:
                        self.db.execute('DELETE FROM exit_budget_reservations WHERE entry_id=?', (entry_id,))
                    else:
                        self.db.execute('UPDATE exit_budget_reservations SET notional=? WHERE entry_id=?',
                                        (str(held - amount), entry_id))
                if exit_reservation is not None:
                    self.db.execute('INSERT INTO exit_budget_reservations VALUES(?,?,?,?)',
                        (authorization['id'], client_id, exit_reservation[0], str(exit_reservation[1])))
                if reserved is not None:
                    self.db.execute('DELETE FROM exit_budget_reservations WHERE authorization=? AND exit_id=?',
                        (authorization['id'], client_id))
            elif not self.db.execute("SELECT 1 FROM dispatches WHERE campaign=? AND method='POST' AND client_id=?",
                                     (campaign, client_id)).fetchone():
                raise SafetyBlocked('instance cancellation has no owned dispatch')
            if method == 'DELETE':
                self._prepare_cancel_retry(client_id, plan, facts, cancel_recovered_at)
            inserted = self.db.execute('INSERT OR IGNORE INTO dispatches VALUES(?,?,?,?,NULL)',
                (campaign, method, client_id, json.dumps(params))).rowcount
            if not inserted:
                raise SafetyBlocked('instance dispatch already attempted; GET recovery required')

    def _prepare_cancel_retry(self, client_id, plan, facts, cancel_recovered_at):
        previous = self.db.execute(
            "SELECT campaign, parameters, receipt FROM dispatches WHERE method='DELETE' AND client_id=?",
            (client_id,)).fetchone()
        if previous is not None:
            receipt = json.loads(previous[2]) if previous[2] is not None else None
            attempted_at = self._get('cancel-attempt:' + client_id)
            if attempted_at is None and receipt is not None:
                attempted_at = receipt.get('observed_at')
            active_order = any(row['symbol'] == plan.symbol and row['clientOrderId'] == client_id
                and row.get('status') in {'NEW', 'PARTIALLY_FILLED'} for row in facts.open_orders)
            if (plan.quote_policy is not None or cancel_recovered_at is None
                    or attempted_at is not None and cancel_recovered_at <= datetime.fromisoformat(attempted_at)
                    or not active_order
                    or receipt is not None and (not receipt.get('outcome_unknown')
                                                or receipt.get('http_status') == 400)):
                raise SafetyBlocked('persistent Demo dispatch already consumed; use recover')
            self.db.execute('INSERT INTO dispatch_retry_history VALUES (?, ?, ?, ?, ?)',
                (previous[0], 'DELETE', client_id, previous[1], previous[2]))
            self.db.execute("DELETE FROM dispatches WHERE method='DELETE' AND client_id=?", (client_id,))
        self._set('cancel-attempt:' + client_id, facts.observed_at.isoformat())

    def _validate_exit_budget(self, authorization, consumed):
        reservations = self.db.execute(
            'SELECT notional FROM exit_budget_reservations WHERE authorization=?',
            (authorization['id'],)).fetchall()
        if (consumed['posts'] + len(reservations) > authorization['total_orders']
                or Decimal(consumed['submitted_notional'])
                    + sum((Decimal(row[0]) for row in reservations), Decimal(0))
                    > Decimal(authorization['total_notional'])):
            raise SafetyBlocked('continuous Demo budget must cover account history and reserved exits; update budget before activation')

    def start_continuous(self, binding, *, opening_equity, stop_epoch, total_orders=100,
                         total_notional=Decimal('700'), seconds=10800, orders=4,
                         max_loss=Decimal('1'), prefix=None):
        """Record continuous permission once; subsequent starts never reset it."""
        if (type(total_orders) is not int or not 0 < total_orders <= 100000
                or not total_notional.is_finite() or not 0 < total_notional <= Decimal('500000')
                or seconds <= 0 or not 1 <= orders <= 1000 or not 0 < max_loss <= Decimal('250')):
            raise SafetyBlocked('invalid continuous Demo risk budget')
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            if self.stop_epoch() != stop_epoch:
                raise SafetyBlocked('Demo was stopped during account preflight')
            saved = self.continuous_authorization()
            if saved is not None:
                self._validate_continuous(binding, saved)
                if saved['state'] != 'RUNNING':
                    raise SafetyBlocked('continuous Demo explicitly stopped')
                if (saved['total_orders'], Decimal(saved['total_notional']), saved['seconds'], saved['orders'],
                        Decimal(saved['max_loss'])) != (total_orders, total_notional, seconds, orders, max_loss):
                    raise SafetyBlocked('continuous Demo policy is frozen')
                return saved
            history = self._continuous_campaigns(dict(journal_id=journal_id(binding['account_id'])))
            origin = next((campaign for (payload,) in self.db.execute(
                'SELECT payload FROM campaigns ORDER BY rowid')
                if (campaign := json.loads(payload))['journal_id'] == journal_id(binding['account_id'])), None)
            if origin is not None:
                opening_equity = Decimal(origin['opening_equity'])
            if history:
                prefix = prefix or history[-1]['prefix']
            authorization = dict(version=1, id='continuous-' + uuid4().hex, state='RUNNING',
                journal_id=journal_id(binding['account_id']), account_id=binding['account_id'],
                account_configuration=binding['configuration'], policy=LIMIT_POLICY,
                prefix=prefix or 'fl-' + uuid4().hex[:16], opening_equity=str(opening_equity),
                max_loss=str(max_loss), total_orders=total_orders, total_notional=str(total_notional),
                seconds=seconds, orders=orders, stop_epoch=stop_epoch, budget_version=1)
            consumed = self.consumption(authorization)
            if consumed['posts'] > total_orders or Decimal(consumed['submitted_notional']) > total_notional:
                raise SafetyBlocked('historical Demo consumption exceeds continuous budget')
            self._set('continuous', json.dumps(authorization))
            self._set('continuous_schema', '1')
            self._set('active', '')
            return authorization

    def update_continuous_budget(self, binding, *, total_orders, total_notional, max_loss, current_equity):
        """Atomically migrate the cumulative budget without resuming or resetting the account."""
        if (binding['environment'] != DEMO.name or binding['host'] != DEMO.host):
            raise SafetyBlocked('official Demo account binding required')
        if (type(total_orders) is not int or not 0 < total_orders <= 100000
                or not total_notional.is_finite() or not 0 < total_notional <= Decimal('500000')
                or not max_loss.is_finite() or not 0 < max_loss <= Decimal('250')):
            raise SafetyBlocked('invalid continuous Demo risk budget')
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            saved = self.continuous_authorization()
            if saved is None:
                raise SafetyBlocked('continuous Demo authorization is missing')
            self._validate_continuous(binding, saved)
            consumed = self.consumption(saved)
            if (consumed['posts'] > total_orders
                    or Decimal(consumed['submitted_notional']) > total_notional):
                raise SafetyBlocked('new continuous Demo budget is below historical consumption')
            self._validate_exit_budget(dict(saved, total_orders=total_orders,
                total_notional=str(total_notional)), consumed)
            origin = next((campaign for (payload,) in self.db.execute('SELECT payload FROM campaigns ORDER BY rowid')
                if (campaign := json.loads(payload))['journal_id'] == saved['journal_id']), None)
            opening_equity = saved['opening_equity'] if origin is None else origin['opening_equity']
            if Decimal(opening_equity) - current_equity >= max_loss:
                raise SafetyBlocked('new continuous Demo loss budget is already exhausted')
            requested = (total_orders, total_notional, max_loss, Decimal(opening_equity))
            current = (saved['total_orders'], Decimal(saved['total_notional']), Decimal(saved['max_loss']),
                Decimal(saved['opening_equity']))
            if current == requested:
                return dict(authorization=saved, consumption=consumed, idempotent=True)
            if total_orders < saved['total_orders'] or total_notional < Decimal(saved['total_notional']) \
                    or max_loss < Decimal(saved['max_loss']):
                raise SafetyBlocked('continuous Demo budget can only be enlarged')
            previous = dict(saved)
            migrated = dict(saved, total_orders=total_orders, total_notional=str(total_notional),
                max_loss=str(max_loss), opening_equity=opening_equity,
                budget_version=saved.get('budget_version', 1) + 1)
            event = dict(action='UPDATE_CONTINUOUS_BUDGET', observed_at=now().isoformat(),
                previous_authorization=previous, authorization=migrated, consumption=consumed,
                original_equity_campaign=origin['id'] if origin is not None else None,
                observed_equity=str(current_equity))
            self._set('continuous', json.dumps(migrated))
            self._set('continuous_budget_migration:' + uuid4().hex, json.dumps(event))
            return dict(authorization=migrated, consumption=consumed, idempotent=False)

    def resume_continuous(self, binding, *, facts, reconciled, stop_epoch):
        """Explicitly resume the same stopped authorization, preserving its STOP audit and budgets."""
        from .demo_session import demo_account_configuration

        if (not reconciled or facts.account_id != binding['account_id'] or facts.can_trade is not True
                or demo_account_configuration(facts) != binding['configuration']):
            raise SafetyBlocked('fresh bound Demo account reconciliation required')
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            saved = self.continuous_authorization()
            if saved is None or saved['state'] != 'STOPPED':
                raise SafetyBlocked('explicitly stopped continuous Demo authorization required')
            self._validate_continuous(binding, saved)
            if self._get('continuous_diagnostic') is not None:
                raise SafetyBlocked('continuous Demo risk halt is latched')
            if self.stop_epoch() != stop_epoch:
                raise SafetyBlocked('Demo was stopped during account preflight')
            funds = facts.usdt()
            loss = Decimal(saved['opening_equity']) - (funds['walletBalance'] + funds['unrealizedProfit'])
            consumed = self.consumption(saved)
            if loss >= Decimal(saved['max_loss']):
                raise SafetyBlocked('global Demo loss budget exhausted')
            if (consumed['posts'] >= saved['total_orders']
                    or Decimal(consumed['submitted_notional']) >= Decimal(saved['total_notional'])):
                raise SafetyBlocked('global Demo trading budget exhausted')
            event = dict(action='RESUME_CONTINUOUS', observed_at=now().isoformat(),
                previous_authorization=saved, stopped_epoch=stop_epoch, consumption=consumed)
            self._set('continuous_resume:' + uuid4().hex, json.dumps(event))
            resumed = dict(saved, state='RUNNING', stop_epoch=stop_epoch)
            self._set('continuous', json.dumps(resumed))
            return resumed

    def _validate_continuous(self, binding, authorization):
        if (authorization['account_id'] != binding['account_id']
                or authorization['journal_id'] != journal_id(binding['account_id'])
                or authorization['account_configuration'] != binding['configuration']
                or authorization['policy'] != LIMIT_POLICY):
            raise SafetyBlocked('continuous Demo account or frozen policy mismatch')

    def halt(self, reason):
        """Latch risk protection without presenting another approval workflow."""
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            authorization = self.continuous_authorization()
            if authorization is not None:
                if authorization['state'] == 'RUNNING':
                    authorization['state'] = 'HALTED'
                    self._set('continuous', json.dumps(authorization))
                self._set('continuous_diagnostic', json.dumps(dict(reason=reason, observed_at=now().isoformat())))
            self._set('active', '')
            self._set('continuous_window', '')
            self._set('stop_epoch', uuid4().hex)

    def ensure_window(self, binding, *, facts, reconciled, stop_epoch):
        """Select or create one window after GET reconciliation, never across STOP."""
        from .demo_session import demo_account_configuration
        if (not reconciled or facts.account_id != binding['account_id'] or facts.can_trade is not True
                or demo_account_configuration(facts) != binding['configuration']):
            raise SafetyBlocked('fresh bound Demo account reconciliation required')
        authorization = self.continuous_authorization()
        if authorization is not None and authorization['state'] == 'RUNNING':
            funds = facts.usdt()
            loss = Decimal(authorization['opening_equity']) - (funds['walletBalance'] + funds['unrealizedProfit'])
            if loss >= Decimal(authorization['max_loss']):
                self.halt('global Demo loss budget exhausted')
                raise SafetyBlocked('global Demo loss budget exhausted')
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            authorization = self.continuous_authorization()
            if authorization is None or authorization['state'] != 'RUNNING':
                raise SafetyBlocked('continuous Demo is not RUNNING')
            self._validate_continuous(binding, authorization)
            self._validate_exit_budget(authorization, self.consumption(authorization))
            if stop_epoch != self.stop_epoch() or authorization['stop_epoch'] != stop_epoch:
                raise SafetyBlocked('continuous Demo stopped during recovery')
            funds = facts.usdt()
            loss = Decimal(authorization['opening_equity']) - (funds['walletBalance'] + funds['unrealizedProfit'])
            self._set('continuous_account', json.dumps(dict(loss=str(loss),
                exposure=str(sum((abs(p.quantity) * p.mark_price for p in facts.shared().positions), Decimal(0))),
                observed_at=facts.observed_at.isoformat())))
            if loss >= Decimal(authorization['max_loss']):
                raise SafetyBlocked('global Demo loss budget exhausted')
            key = self._get('continuous_window')
            campaign = self.campaign(key) if key else None
            if (campaign is None or now() >= datetime.fromisoformat(campaign['expires_at'])
                    or campaign.get('budget_version', 1) != authorization.get('budget_version', 1)
                    or self.post_count(campaign['id']) >= campaign['orders']):
                campaign = dict(id='window-' + uuid4().hex, authorization=authorization['id'],
                    journal_id=authorization['journal_id'], prefix=authorization['prefix'],
                    expires_at=(now() + timedelta(seconds=authorization['seconds'])).isoformat(),
                    seconds=authorization['seconds'], orders=authorization['orders'],
                    opening_equity=authorization['opening_equity'], max_loss=authorization['max_loss'],
                    smoke=False, policy=LIMIT_POLICY, account_configuration=binding['configuration'],
                    budget_version=authorization.get('budget_version', 1))
                self.db.execute('INSERT INTO campaigns VALUES (?,?)', (campaign['id'], json.dumps(campaign)))
                self._set('continuous_window', campaign['id'])
            self._set('continuous_recovery', '')
            return campaign

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
            if self.continuous_authorization() is not None:
                raise SafetyBlocked('continuous Demo policy cannot be replaced by another campaign')
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
        authorization = self.continuous_authorization()
        key = (self._get('continuous_window') if authorization['state'] == 'RUNNING' else None) if authorization else self._get('active')
        if key:
            campaign = self.campaign(key)
            if now() < datetime.fromisoformat(campaign['expires_at']):
                return campaign
        return None

    def campaign(self, key):
        row = self.db.execute('SELECT payload FROM campaigns WHERE id = ?', (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def grid_deployment_matches(self, store, deployment, campaign, authorization=None):
        """Check the active Grid's identity against its journal and campaign lineage."""
        from factorlab.trading.storage import dumps

        identity = deployment['deployment']['deployment_id']
        generation = deployment.get('generation', 0)
        if type(generation) is not int or not 0 <= generation <= 0xfffff:
            return False
        if authorization is None:
            return (campaign is not None and deployment['campaign'] == campaign['id']
                    and generation == 0 and identity == grid_deployment_id(campaign['prefix']))
        origin = self.campaign(deployment['campaign'])
        prefix = authorization['prefix']
        if (origin is None or origin['smoke'] or origin['prefix'] != prefix
                or origin['journal_id'] != authorization['journal_id']
                or origin.get('authorization', authorization['id']) != authorization['id']
                or origin['policy'] != authorization['policy']
                or origin['account_configuration'] != authorization['account_configuration']
                or (campaign is not None and (campaign['prefix'] != prefix
                    or campaign.get('authorization') != authorization['id']))
                or identity != grid_deployment_id(prefix, generation)):
            return False
        if generation == 0:
            return deployment.get('feed_deployment_id', identity) == identity
        previous = grid_deployment_id(prefix, generation - 1)
        activated = deployment.get('activated_at')
        return (deployment.get('feed_deployment_id') == grid_deployment_id(prefix)
                and deployment.get('replaces') == previous
                and deployment.get('terminal_reason') in ('BREAKOUT', 'BREAKOUT_REBUILD_INTERVAL')
                and isinstance(activated, datetime) and activated.tzinfo is not None
                and store.event_payload('demo-grid:deployment:' + identity) == dumps(deployment)
                and store.event_payload(f'demo-grid:{identity}:deployment') == dumps(deployment['deployment'])
                and store.event_payload(f'demo-grid:{previous}:deployment') is not None)

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
        authorization = self.continuous_authorization()
        result = dict(enabled=campaign is not None, expires_at=campaign['expires_at'] if campaign else None,
            authorization_kind='CONTINUOUS' if authorization else (
                ('SMOKE' if campaign['smoke'] else 'PERSISTENT') if campaign else None),
            policy=campaign['policy'] if campaign else None,
            post_budget=campaign['orders'] if campaign else None,
            posts_consumed=self.post_count(campaign['id']) if campaign else None,
            limits=dict(symbol=SYMBOL, max_quantity=str(MAX_QUANTITY), max_notional_usdt=str(MAX_NOTIONAL),
                max_open_orders=1, max_loss_usdt=campaign['max_loss']) if campaign else None,
            smoke_started=smoke is not None)
        if authorization is not None:
            result['USER_DEMO_AUTHORIZATION'] = dict(state=authorization['state'], id=authorization['id'],
                max_loss=authorization['max_loss'], opening_equity=authorization['opening_equity'],
                total_orders=authorization['total_orders'], total_notional=authorization['total_notional'],
                consumed=self.consumption(authorization))
            result['CAMPAIGN_WINDOW'] = dict(id=campaign['id'], expires_at=campaign['expires_at'],
                orders=campaign['orders'], consumed=self.post_count(campaign['id'])) if campaign else None
            receipts = [json.loads(value) for (value,) in self.db.execute(
                'SELECT receipt FROM dispatches WHERE receipt IS NOT NULL')]
            result['EXCHANGE_WRITE_CAPABILITY'] = dict(historical_success=any(
                receipt_confirmed(receipt)
                if {'outcome_unknown', 'order_id', 'status', 'code'} & receipt.keys()
                else receipt.get('http_status') == 200
                for receipt in receipts), current='EXCHANGE_RESPONSE_AUTHORITATIVE')
            account = self._get('continuous_account')
            result['ACCOUNT_EXECUTION_STATE'] = dict(account=json.loads(account) if account else None,
                recovery_required=bool(self._get('continuous_recovery')),
                diagnostic=self._get('continuous_diagnostic'))
        return result

    def post_count(self, campaign):
        return self.db.execute("SELECT count(*) FROM dispatches WHERE campaign = ? AND method = 'POST'",
            (campaign,)).fetchone()[0]

    def receipts(self, campaign):
        rows = self.db.execute('SELECT method, client_id, receipt FROM dispatches WHERE campaign = ? ORDER BY rowid',
            (campaign,))
        receipts = []
        for method, client_id, raw in rows:
            receipt = json.loads(raw) if raw is not None else dict(method=method, client_order_id=client_id,
                http_status=None, dispatch='RESERVED_RESPONSE_UNCONFIRMED')
            history = list(self.db.execute(
                'SELECT receipt FROM dispatch_retry_history WHERE client_id=? AND method=? ORDER BY rowid',
                (client_id, method)))
            if history:
                receipt['previous_attempts'] = [json.loads(previous) if previous is not None else
                    dict(method=method, client_order_id=client_id, http_status=None,
                         dispatch='RESERVED_RESPONSE_UNCONFIRMED') for previous, in history]
            receipts.append(receipt)
        return receipts

    def authority(self, binding, campaign):
        if campaign is None or campaign['journal_id'] != journal_id(binding['account_id']):
            raise SafetyBlocked('no active Demo authorization for this account')
        if campaign.get('policy') != LIMIT_POLICY:
            raise SafetyBlocked('persisted Demo execution policy is unsupported')
        if campaign['account_configuration'] != binding['configuration']:
            raise SafetyBlocked('Demo account configuration differs from the enabled campaign')
        continuous = self.continuous_authorization() if campaign.get('authorization') else None
        plan = DemoExecutionPlan(account_id=binding['account_id'], order_types=frozenset({'LIMIT'}),
            max_quantity=MAX_QUANTITY, max_notional=MAX_NOTIONAL,
            max_orders=continuous['total_orders'] if continuous else campaign['orders'], max_open_orders=1,
            max_seconds=campaign['seconds'], max_loss=Decimal(campaign['max_loss']), max_margin=MAX_NOTIONAL,
            expires_at=datetime.fromisoformat(campaign['expires_at']), client_id_prefix=campaign['prefix'],
            cleanup='CANCEL_KNOWN_THEN_STOP', recovery='GET_RECONCILE_NO_RESUBMIT')
        cancellation_ids = ()
        if campaign.get('authorization'):
            authorization = self.continuous_authorization()
            owners = {item['id'] for item in self._continuous_campaigns(authorization)}
            cancellation_ids = [client_id for owner, client_id in self.db.execute(
                "SELECT campaign, client_id FROM dispatches WHERE method='POST'") if owner in owners]
        return DemoNetworkAuthorization(plan, cancellation_ids=cancellation_ids)

    def release_unfilled_exit_reservations(self, orders):
        """Release only a GET-confirmed terminal entry with no actual fills."""
        from factorlab.trading.contracts import TERMINAL
        with self.db:
            for entry_id, in self.db.execute('SELECT entry_id FROM exit_budget_reservations').fetchall():
                record = orders.get(entry_id)
                if (record is not None and record.snapshot.status in TERMINAL
                        and not record.snapshot.reconcile_required
                        and record.snapshot.cumulative_filled_quantity == 0):
                    self.db.execute('DELETE FROM exit_budget_reservations WHERE entry_id=?', (entry_id,))

    def reserve(self, campaign, method, params, plan, *, expected, facts, dispatch_gate=None,
                cancel_recovered_at=None, exit_reservation=None):
        """Consume POST once; retry DELETE only after a newer GET confirms it active."""
        client_id = params['newClientOrderId' if method == 'POST' else 'origClientOrderId']
        continuous = campaign.get('authorization') is not None
        if continuous and method == 'POST':
            funds = facts.usdt()
            if Decimal(campaign['opening_equity']) - (funds['walletBalance'] + funds['unrealizedProfit']) >= plan.max_loss:
                self.halt('global Demo loss budget exhausted')
                raise SafetyBlocked('global Demo loss budget exhausted')
        if params['symbol'] != plan.symbol or (method == 'POST' and not client_id.startswith(campaign['prefix'] + '-')):
            raise SafetyBlocked('order is outside the active Demo campaign')
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            authorization = self.continuous_authorization() if continuous else None
            active = self._get('continuous_window') if continuous else self._get('active')
            if (active != campaign['id'] or now() >= plan.expires_at
                    or continuous and (authorization is None or authorization['state'] != 'RUNNING'
                        or authorization['id'] != campaign['authorization']
                        or authorization['stop_epoch'] != self.stop_epoch())):
                raise SafetyBlocked('Demo authorization stopped or expired')
            if dispatch_gate is not None:
                dispatch_gate()
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
                if continuous:
                    if self.db.execute("SELECT 1 FROM dispatches WHERE method='POST' AND client_id=?", (client_id,)).fetchone():
                        raise SafetyBlocked('persistent Demo dispatch already consumed; use recover')
                    consumed = self.consumption(authorization)
                    notional = Decimal(params['quantity']) * Decimal(params['price'])
                    reservations = self.db.execute(
                        'SELECT exit_id, notional FROM exit_budget_reservations WHERE authorization=?',
                        (authorization['id'],)).fetchall()
                    own_exit = next((Decimal(amount) for key, amount in reservations if key == client_id), None)
                    if own_exit is not None and (params['reduceOnly'] != 'true' or notional > own_exit):
                        raise SafetyBlocked('reserved exit must reduce inventory within its notional budget')
                    held = [(key, Decimal(amount)) for key, amount in reservations if key != client_id]
                    extra_posts, extra_notional = 0, Decimal(0)
                    if exit_reservation is not None:
                        exit_id, extra_notional = exit_reservation
                        if (params['side'] != 'BUY' or params['reduceOnly'] != 'false'
                                or not exit_id.startswith(campaign['prefix'] + '-')
                                or exit_id == client_id or not extra_notional.is_finite() or extra_notional <= 0):
                            raise SafetyBlocked('invalid conformance exit reservation')
                        extra_posts = 1
                    if (consumed['posts'] + len(held) + 1 + extra_posts > min(100000, authorization['total_orders'])
                            or Decimal(consumed['submitted_notional']) + sum((amount for _, amount in held), Decimal(0))
                                + notional + extra_notional > min(Decimal('500000'), Decimal(authorization['total_notional']))
                            or count + 1 + extra_posts > campaign['orders']):
                        raise SafetyBlocked('global Demo trading budget exhausted or reserved for exit')
                    if exit_reservation is not None:
                        self.db.execute('INSERT INTO exit_budget_reservations VALUES (?, ?, ?, ?)',
                            (authorization['id'], client_id, exit_id, str(extra_notional)))
                    if own_exit is not None:
                        self.db.execute('DELETE FROM exit_budget_reservations WHERE exit_id=?', (client_id,))
                funds = facts.usdt()
                if Decimal(campaign['opening_equity']) - (funds['walletBalance'] + funds['unrealizedProfit']) >= plan.max_loss:
                    raise SafetyBlocked('persistent Demo loss budget exhausted')
                quantity = Decimal(params['quantity'])
                position = sum((p.quantity for p in facts.shared().positions
                                if p.instrument_id == plan.symbol), Decimal(0))
                if ((params['side'] == 'BUY' and not 0 <= position <= MAX_QUANTITY - quantity)
                    or (params['side'] == 'SELL' and not 0 < quantity <= position)):
                    raise SafetyBlocked('Demo position exceeds the authorized net quantity or reduction')
            else:
                owners = {c['id'] for c in self._continuous_campaigns(authorization)} if continuous else {campaign['id']}
                origins = [owner for (owner,) in self.db.execute(
                    "SELECT campaign FROM dispatches WHERE method='POST' AND client_id=?", (client_id,)) if owner in owners]
                if len(origins) != 1:
                    raise SafetyBlocked('Demo cancellation requires a known owned order')
                self._prepare_cancel_retry(client_id, plan, facts, cancel_recovered_at)

            inserted = self.db.execute('INSERT OR IGNORE INTO dispatches VALUES (?, ?, ?, ?, NULL)',
                (campaign['id'], method, client_id, json.dumps(params))).rowcount
            if not inserted:
                raise SafetyBlocked('persistent Demo dispatch already consumed; use recover')

    def record_receipt(self, campaign, receipt):
        with self.db:
            self.db.execute('UPDATE dispatches SET receipt=? WHERE campaign=? AND method=? AND client_id=?',
                (json.dumps(receipt), campaign['id'], receipt['method'], receipt['client_order_id']))
            if not receipt_confirmed(receipt):
                if campaign.get('authorization'):
                    self._set('continuous_recovery', json.dumps(receipt))
                elif not (receipt['method'] == 'DELETE' and receipt.get('outcome_unknown')
                          and receipt.get('http_status') != 400):
                    self._set('active', '')
                    self._set('stop_epoch', uuid4().hex)


DEMO_LIMITS = RiskLimits(MAX_NOTIONAL, MAX_NOTIONAL, MAX_NOTIONAL, orders_per_minute=20)


def durable_control_state(runtime):
    """Return persisted account-wide STOP/HALTED state, or None when not stopped/halted."""
    authorization = runtime.continuous_authorization()
    if authorization is not None:
        if authorization['state'] == 'HALTED' or runtime._get('continuous_diagnostic') is not None:
            return 'HALTED'
        if authorization['state'] == 'STOPPED':
            return 'STOPPED'
        return None
    if runtime._get('stop_epoch') and not runtime._get('active') and not runtime._get('continuous_window'):
        return 'STOPPED'
    return None


def grid_deployment_id(prefix, generation=0):
    return f'{prefix}-g-{generation:05x}' if generation else f'{prefix}-g'


@contextmanager
def open_grid_session(root, store, grid_factory=None, *, read_only=False, new_deployment=False,
                      preview=False, continuous=False, request_counts=None, dispatch_gate=None,
                      runtime=None, ensure_on_open=True):
    """Open one campaign-bound Grid bridge over the shared Demo journals."""
    from factorlab.trading.contracts import TERMINAL
    from factorlab.trading.storage import PaperStore, dumps, decode

    from .demo_grid_bridge import DemoGridBridge
    from .demo_session import DemoSession

    if preview:
        root = root / 'grid-preview'
        root.mkdir(parents=True, exist_ok=True)
    with ExitStack() as stack:
        if runtime is None:
            stack.enter_context(operator_lock(root))
            runtime = stack.enter_context(closing(DemoRuntime(root)))
        binding = private_binding(store)
        credential = store.read()
        if credential is None:
            raise SafetyBlocked('saved Demo credential missing; run setup')
        campaign = dict(id='grid-preview', prefix='preview') if preview else runtime.active_campaign()
        writes = not read_only and not preview
        continuous_auth = runtime.continuous_authorization()
        automatic = writes and continuous and continuous_auth is not None
        if automatic and new_deployment:
            raise SafetyBlocked('continuous Grid deployments rotate automatically after a reconciled breakout')
        if writes and not automatic and (campaign is None or campaign['smoke']):
            raise SafetyBlocked('no persistent Demo authorization; run start-continuous')
        authorization = runtime.authority(binding, campaign) if writes and not automatic else None
        expected = None

        def expect(intent):
            nonlocal expected
            expected = (intent.client_order_id, intent.side.value, intent.quantity,
                'true' if intent.reduce_only else 'false', intent.limit_price)

        def guard(method, params, plan):
            strategy_guard = getattr(session, 'strategy_write_guard', None)
            if strategy_guard is not None and strategy_guard(
                    method, params, plan, campaign, expected, runtime, session.adapter.facts):
                return
            deployment = decode(json.loads(saved_grid_deployment(controller_store)))
            client_id = params['newClientOrderId' if method == 'POST' else 'origClientOrderId']
            identity, _, sequence = client_id.rpartition('-')
            if (identity != deployment['deployment']['deployment_id']
                    or len(sequence) < 8 or not sequence.isascii() or not sequence.isdecimal()
                    or not runtime.grid_deployment_matches(
                        controller_store, deployment, campaign, continuous_auth if automatic else None)):
                raise SafetyBlocked('order is outside the active Grid deployment')
            runtime.reserve(campaign, method, params, plan, expected=expected,
                            facts=session.adapter.facts, dispatch_gate=dispatch_gate,
                            cancel_recovered_at=session.last_reconciled_at)

        def record(receipt):
            runtime.record_receipt(campaign, receipt)

        journal = root / journal_id(binding['account_id'])
        journal.mkdir(exist_ok=True)
        capacity = dict(continuous_capacity=True) if continuous else {}
        protocol_store = stack.enter_context(closing(PaperStore(journal / 'protocol.sqlite3', **capacity)))
        controller_store = stack.enter_context(closing(PaperStore(journal / 'controller.sqlite3', **capacity)))
        saved = saved_grid_deployment(controller_store)
        reconcile_on_open = not read_only and ((automatic and ensure_on_open) or new_deployment
            or (saved is None and grid_factory is not None))
        session = DemoSession(credential, protocol_store=protocol_store, controller_store=controller_store,
            account_id=binding['account_id'], configuration=binding['configuration'],
            authorization=authorization, limits=DEMO_LIMITS,
            write_guard=guard if writes and not automatic else None,
            receipt_sink=record if writes and not automatic else None,
            request_counts=request_counts, get_confirmed_writes=continuous, reconcile_on_open=reconcile_on_open,
            require_bound_configuration=automatic)

        def ensure_campaign():
            nonlocal campaign
            if not automatic:
                return campaign
            epoch = runtime.stop_epoch()
            status = session.status()
            recovery_required = bool(runtime._get('continuous_recovery'))
            selected = runtime.ensure_window(binding, facts=session.adapter.facts,
                reconciled=status['reconciled'] and not status['unknown'], stop_epoch=epoch)
            if (campaign is None or selected['id'] != campaign['id']
                    or not session.client.transport.permitted()
                    or session.write_uncertain or session.receipt_error
                    or recovery_required):
                session.refresh_authorization(runtime.authority(binding, selected),
                    write_guard=guard, receipt_sink=record)
            campaign = selected
            return campaign

        if automatic and (ensure_on_open or saved is None):
            ensure_campaign()
        expected_prefix = (campaign['prefix'] if campaign is not None
                           else continuous_auth['prefix'] if automatic else None)

        def build_bridge(grid, deployment, *, create=False):
            if continuous:
                grid.enable_continuous_capacity()
            deferred_admission = automatic and not ensure_on_open and session._write_guard is None
            bridge = DemoGridBridge(grid, session, expect=expect if writes else None,
                read_only=read_only, preview=preview, deferred_admission=deferred_admission,
                expected_prefix=expected_prefix, new_deployment=deployment if create else None)
            bridge.runtime = runtime
            bridge.ensure_campaign = ensure_campaign
            bridge.feed_deployment_id = deployment.get('feed_deployment_id', grid.deployment_id)
            bridge.activated_at = deployment.get('activated_at')
            bridge.generation = deployment.get('generation', 0)
            bridge.rotate_if_terminal = rotate_if_terminal
            return bridge

        def rotate_if_terminal(bridge, *, running):
            if (not automatic or grid_factory is None or campaign is None
                    or not bridge.grid.closed_by_breakout or not running()):
                return bridge
            status = session.status()
            terminal_values = {item.value for item in TERMINAL}
            if (not bridge.ready or not status['reconciled'] or status['positions']
                    or bridge.gateway.state.positions
                    or status['open_orders'] or status['unknown'] or session.write_uncertain
                    or session.receipt_error or runtime._get('continuous_recovery')
                    or bridge.gateway.read_account().open_orders
                    or any(order['status'] not in terminal_values for order in status['orders'].values())
                    or any(order.intent.client_order_id in bridge.gateway.state.orders
                           for order in bridge.grid.active)):
                return bridge
            # A lost HTTP receipt may remain absent after GET has resolved
            # its order. Only reservations without a terminal order block rotation.
            for (client_id,) in runtime.db.execute('SELECT client_id FROM dispatches WHERE receipt IS NULL'):
                order = bridge.gateway.state.orders.get(client_id)
                if order is None or order.snapshot.status not in TERMINAL:
                    return bridge

            epoch = runtime.stop_epoch()
            generation = bridge.generation + 1
            if generation > 0xfffff:
                raise SafetyBlocked('continuous Grid deployment identity exhausted')
            deployment_id = grid_deployment_id(campaign['prefix'], generation)
            grid = grid_factory(session, deployment_id)
            if grid.deployment_id != deployment_id:
                raise SafetyBlocked('Grid deployment identity differs from the active Demo campaign')
            facts = session.adapter.facts
            activated_at = session._now()
            session.adapter._require_fresh_account_read(facts.observed_at, facts.read_started_monotonic, activated_at)
            deployment = dict(campaign=campaign['id'], deployment=grid.deployment(),
                feed_deployment_id=bridge.feed_deployment_id, activated_at=activated_at, generation=generation,
                replaces=bridge.grid.deployment_id, terminal_reason=bridge.grid.reason)
            # The owner lock serializes Grid sessions. The runtime write
            # transaction also serializes promotion with account-wide STOP.
            with runtime.db:
                runtime.db.execute('BEGIN IMMEDIATE')
                policy = runtime.continuous_authorization()
                if (policy is None or policy['state'] != 'RUNNING' or runtime.stop_epoch() != epoch
                        or runtime.active_campaign() != campaign or not running()
                        or decode(json.loads(saved_grid_deployment(controller_store)))['deployment']['deployment_id']
                        != bridge.grid.deployment_id):
                    return bridge
                consumed = runtime.consumption(policy)
                entry_notional = grid.rules.quantity_unit * Decimal(str(grid.config.lower))
                if (consumed['posts'] >= policy['total_orders']
                        or Decimal(consumed['submitted_notional']) + entry_notional > Decimal(policy['total_notional'])):
                    raise SafetyBlocked('global Demo trading budget exhausted')
                replacement = build_bridge(grid, deployment, create=True)
            replacement.restore_reconciled()
            return replacement

        if new_deployment:
            if read_only:
                raise SafetyBlocked('new Grid deployment requires explicit write mode')
            if saved is not None and decode(json.loads(saved))['campaign'] == campaign['id']:
                raise SafetyBlocked('current campaign already has a Grid deployment; reuse it')
            status = session.status()
            if not status['reconciled'] or status['positions'] or status['open_orders'] or status['unknown']:
                raise SafetyBlocked('new Grid deployment requires a reconciled flat account without open or UNKNOWN orders')
            saved = None
        if saved is not None:
            deployment = decode(json.loads(saved))
            deployment_id = deployment['deployment']['deployment_id']
            if ((writes or preview) and not runtime.grid_deployment_matches(
                    controller_store, deployment, campaign, continuous_auth if automatic else None)):
                raise SafetyBlocked('persisted Grid deployment or Demo campaign differs')
            if grid_factory is None or read_only:
                grid = restore_grid(deployment['deployment'])
            else:
                grid = grid_factory(session, deployment_id)
                if dumps(grid.deployment()) != dumps(deployment['deployment']):
                    raise SafetyBlocked('persisted Grid deployment differs')
        else:
            if read_only or grid_factory is None:
                raise SafetyBlocked('no persisted Grid deployment')
            deployment_id = grid_deployment_id(campaign['prefix'])
            grid = grid_factory(session, deployment_id)
            if grid.deployment_id != deployment_id:
                raise SafetyBlocked('Grid deployment identity differs from the active Demo campaign')
            deployment = dict(campaign=campaign['id'], deployment=grid.deployment())
        yield build_bridge(grid, deployment, create=saved is None)


def saved_grid_deployment(store):
    """Read the latest immutable campaign binding, including the original journal key."""
    row = store.db.execute("SELECT payload FROM events WHERE id = 'demo-grid:deployment' "
        "OR id LIKE 'demo-grid:deployment:%' ORDER BY rowid DESC LIMIT 1").fetchone()
    return row[0] if row else None


def restore_grid(deployment):
    """Reconstruct the exact journaled economic deployment, before command replay."""
    from factorlab.strategies.grid_restore import restore_grid as restore
    if deployment.get('r10_strategy') not in {None, 'B_FUNDING_SENSITIVE_GRID', 'C_VOL_ADAPTIVE_GRID'}:
        raise SafetyBlocked('unknown persisted Grid strategy')
    return restore(deployment)
