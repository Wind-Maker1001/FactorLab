"""Instance-scoped extension of the restartable deployment switch saga."""
from contextlib import contextmanager
import json
import sqlite3
from time import time
from threading import Lock

from .controller_lock import controller_lock

from factorlab.trading.storage import dumps, decode
from .package import (
    can_artifact_refresh_manifests,
    candidate_package,
    digest,
    encoded,
    validate_manifest,
    verify_artifact_refresh_manifests,
    verify_artifact_refresh_packages,
)
from .protocol import V2, validate_binding, json_value
from .identity import content_identity
from .events import StrategyInputUnavailable


class SupersededSwitch(Exception):
    """Another controller already advanced this switch."""


_UNSET = object()

LEGACY = 'legacy-grid'
TERMINAL = {'FILLED', 'CANCELED', 'REJECTED', 'EXPIRED'}
PENDING = "phase NOT IN ('SUCCEEDED','ROLLED_BACK','FAILED')"


class DeploymentStore:
    def __init__(self, path, *, instance_id=LEGACY, qualify=None):
        self.path, self.instance_id, self.qualify = path, instance_id, qualify
        self._controller_lock = Lock()
        self.db = sqlite3.connect(path, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
          PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL;
          CREATE TABLE IF NOT EXISTS packages(id TEXT PRIMARY KEY, manifest TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS deployment(id INTEGER PRIMARY KEY CHECK(id=1), active TEXT,
            stable TEXT, previous TEXT, checkpoint TEXT NOT NULL DEFAULT '{}', watermark TEXT);
          INSERT OR IGNORE INTO deployment(id,active,stable) VALUES(1,'legacy-grid','legacy-grid');
          CREATE TABLE IF NOT EXISTS switches(id INTEGER PRIMARY KEY, selection TEXT UNIQUE,
            old TEXT, new TEXT, phase TEXT, mode TEXT, policy TEXT, watermark TEXT,
            boot TEXT, reason TEXT, created REAL, updated REAL);
          CREATE UNIQUE INDEX IF NOT EXISTS one_pending_switch ON switches((1))
            WHERE phase NOT IN ('SUCCEEDED','ROLLED_BACK','FAILED');
          CREATE TABLE IF NOT EXISTS switch_events(seq INTEGER PRIMARY KEY, switch_id INTEGER,
            at REAL NOT NULL, event TEXT NOT NULL, payload TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS strategy_orders(client_id TEXT PRIMARY KEY, version TEXT,
            watermark TEXT, reduce_only INTEGER NOT NULL);
          CREATE TABLE IF NOT EXISTS decisions(version TEXT, watermark TEXT, result TEXT,
            PRIMARY KEY(version,watermark));
          CREATE TABLE IF NOT EXISTS deployment_bindings(package_id TEXT PRIMARY KEY,
            deployment_id TEXT UNIQUE NOT NULL);
          BEGIN IMMEDIATE;
          CREATE TABLE IF NOT EXISTS strategy_registry(instance_id TEXT PRIMARY KEY,
            binding TEXT, fallback TEXT);
          INSERT OR IGNORE INTO strategy_registry VALUES('legacy-grid',NULL,'legacy-grid');
          CREATE TABLE IF NOT EXISTS instance_deployment(instance_id TEXT PRIMARY KEY, id INTEGER DEFAULT 1,
            active TEXT, stable TEXT, previous TEXT, checkpoint TEXT NOT NULL DEFAULT '{}', watermark TEXT);
          CREATE TABLE IF NOT EXISTS instance_switches(id INTEGER PRIMARY KEY, instance_id TEXT NOT NULL,
            selection TEXT, old TEXT, new TEXT, phase TEXT, mode TEXT, policy TEXT, watermark TEXT,
            boot TEXT, reason TEXT, created REAL, updated REAL, UNIQUE(instance_id,selection));
          CREATE UNIQUE INDEX IF NOT EXISTS instance_pending_switch ON instance_switches(instance_id)
            WHERE phase NOT IN ('SUCCEEDED','ROLLED_BACK','FAILED');
          CREATE TABLE IF NOT EXISTS instance_switch_events(seq INTEGER PRIMARY KEY, instance_id TEXT NOT NULL,
            switch_id INTEGER, at REAL NOT NULL, event TEXT NOT NULL, payload TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS instance_deployment_bindings(instance_id TEXT NOT NULL,
            package_id TEXT NOT NULL, deployment_id TEXT UNIQUE NOT NULL, PRIMARY KEY(instance_id,package_id));
          CREATE TABLE IF NOT EXISTS instance_strategy_orders(instance_id TEXT NOT NULL,
            client_id TEXT NOT NULL, version TEXT, watermark TEXT, reduce_only INTEGER NOT NULL,
            PRIMARY KEY(instance_id,client_id));
          CREATE TABLE IF NOT EXISTS instance_decisions(instance_id TEXT NOT NULL, version TEXT NOT NULL,
            watermark TEXT NOT NULL, input_identity TEXT NOT NULL, result TEXT NOT NULL,
            PRIMARY KEY(instance_id,version,watermark));
          CREATE INDEX IF NOT EXISTS instance_decision_sequence ON instance_decisions(instance_id);
          CREATE TABLE IF NOT EXISTS instance_dispatches(instance_id TEXT NOT NULL, version TEXT NOT NULL,
            watermark TEXT NOT NULL, PRIMARY KEY(instance_id,version,watermark));
          CREATE TABLE IF NOT EXISTS activation_callbacks(instance_id TEXT PRIMARY KEY,
            switch_id INTEGER NOT NULL, package_id TEXT NOT NULL, reason TEXT);
          CREATE TABLE IF NOT EXISTS instance_adoptions(instance_id TEXT PRIMARY KEY,
            identity TEXT NOT NULL, evidence TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS instance_runtime_bindings(instance_id TEXT NOT NULL, package_id TEXT NOT NULL,
            binding TEXT NOT NULL, PRIMARY KEY(instance_id,package_id));
          CREATE TABLE IF NOT EXISTS instance_checkpoints(instance_id TEXT NOT NULL, package_id TEXT NOT NULL,
            checkpoint TEXT NOT NULL, PRIMARY KEY(instance_id,package_id));
          CREATE TABLE IF NOT EXISTS instance_migrations(instance_id TEXT NOT NULL, switch_id INTEGER NOT NULL,
            identity TEXT NOT NULL, checkpoint TEXT NOT NULL, PRIMARY KEY(instance_id,switch_id));
          CREATE TABLE IF NOT EXISTS strategy_management_schema(version INTEGER PRIMARY KEY);
          INSERT OR IGNORE INTO strategy_management_schema VALUES(2);
          COMMIT;
        ''')
        with self.atomic():
            if not self.db.execute('SELECT 1 FROM strategy_management_schema WHERE version=3').fetchone():
                for row in self.db.execute('SELECT instance_id,version,watermark,result FROM instance_decisions').fetchall():
                    receipt = decode(json.loads(row['result']))
                    receipt['input_identity'] = content_identity(receipt['event'])
                    self.db.execute('UPDATE instance_decisions SET input_identity=?,result=? '
                        'WHERE instance_id=? AND version=? AND watermark=?',
                        (receipt['input_identity'], dumps(receipt), row['instance_id'], row['version'], row['watermark']))
                self.db.execute('INSERT INTO strategy_management_schema VALUES(3)')
        if not self.db.execute('SELECT 1 FROM strategy_registry WHERE instance_id=?', (instance_id,)).fetchone():
            self.db.close()
            raise ValueError('strategy instance is not registered')

    @property
    def legacy(self):
        return self.instance_id == LEGACY

    def _table(self, name):
        return name if self.legacy else 'instance_' + name

    def _scope(self, where='', params=()):
        if not self.legacy:
            where = (where + ' AND ' if where else '') + 'instance_id=?'
            params = (*params, self.instance_id)
        return (' WHERE ' + where if where else ''), params

    def _rows(self, table, where='', params=(), *, tail=''):
        where, params = self._scope(where, params)
        return self.db.execute(f'SELECT * FROM {self._table(table)}{where} {tail}', params)

    def _insert(self, table, values):
        if not self.legacy:
            values = dict(values, instance_id=self.instance_id)
        columns = ','.join(values)
        marks = ','.join('?' for _ in values)
        return self.db.execute(f'INSERT INTO {self._table(table)}({columns}) VALUES({marks})', tuple(values.values()))

    def _update(self, table, values, where='', params=()):
        where, params = self._scope(where, params)
        assignments = ','.join(k + '=?' for k in values)
        return self.db.execute(f'UPDATE {self._table(table)} SET {assignments}{where}', (*values.values(), *params))

    @contextmanager
    def atomic(self):
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            yield

    def close(self):
        self.db.close()

    def register_instance(self, binding, *, fallback_package_id=None):
        validate_binding(binding)
        if binding['instance_id'] == LEGACY:
            raise ValueError('legacy instance identity is reserved')
        payload = encoded(binding).decode()
        with self.atomic():
            saved = self.db.execute('SELECT * FROM strategy_registry WHERE instance_id=?', (binding['instance_id'],)).fetchone()
            if saved:
                if (saved['binding'], saved['fallback']) != (payload, fallback_package_id):
                    raise ValueError('instance binding/fallback is immutable')
                return
            if fallback_package_id is not None:
                manifest = self.manifest(fallback_package_id)
                if manifest['interface'] != V2:
                    raise ValueError('instance fallback must use V2')
                from .package import manifest_spec
                manifest_spec(manifest).require_binding(binding)
            self.db.execute('INSERT INTO strategy_registry VALUES(?,?,?)',
                            (binding['instance_id'], payload, fallback_package_id))
            self.db.execute('INSERT INTO instance_deployment(instance_id) VALUES(?)', (binding['instance_id'],))

    def adopt_existing(self, manifest, checkpoint, watermark, evidence, *, verify):
        """Adopt an unchanged native deployment after host journal verification.

        This is not promotion: it preserves the existing execution ledger and
        bypasses no subsequent switch policy. Exact redelivery never rewinds
        a checkpoint advanced after the original adoption.
        """
        from .package import manifest_spec
        import re
        if self.legacy or manifest['interface'] != V2:
            raise ValueError('adoption requires a registered V2 instance')
        validate_manifest(manifest, qualify=self.qualify)
        binding = self.binding()
        manifest_spec(manifest).require_binding(binding)
        if not isinstance(checkpoint, dict) or not isinstance(evidence, dict):
            raise ValueError('checkpoint/evidence must be JSON objects')
        checkpoint, evidence = json_value(checkpoint), json_value(evidence)
        if (not isinstance(watermark, str) or not watermark
                or not isinstance(evidence.get('journal_id'), str) or not evidence['journal_id']
                or not isinstance(evidence.get('source_code_sha256'), str)
                or not re.fullmatch('[0-9a-f]{64}', evidence['source_code_sha256'])):
            raise ValueError('native journal, code identity and watermark required')
        if not callable(verify):
            raise ValueError('host adoption verifier required')
        identity = digest(dict(manifest=manifest, binding=binding, checkpoint=checkpoint,
                               watermark=watermark, evidence=evidence))
        with self.atomic():
            prior = self.db.execute('SELECT identity FROM instance_adoptions WHERE instance_id=?',
                                    (self.instance_id,)).fetchone()
            if prior:
                if prior[0] != identity:
                    raise ValueError('adoption identity conflict')
                return self.snapshot()
            state = self.snapshot()
            if (any(state[k] is not None for k in ('active', 'stable', 'previous', 'watermark'))
                    or state['checkpoint'] or state['switch'] is not None):
                raise ValueError('only a pristine instance can adopt an existing deployment')
            verify(manifest, binding, checkpoint, watermark, evidence)
            self.db.execute('INSERT OR IGNORE INTO packages VALUES(?,?)',
                            (manifest['package_id'], encoded(manifest).decode()))
            self.db.execute('INSERT INTO instance_adoptions VALUES(?,?,?)',
                            (self.instance_id, identity, encoded(evidence).decode()))
            self._update('deployment', dict(active=manifest['package_id'], stable=manifest['package_id'],
                checkpoint=encoded(checkpoint).decode(), watermark=watermark))
            return self.snapshot()

    def for_instance(self, instance_id):
        if self.path == ':memory:':
            raise ValueError('instance views require a persistent database')
        return type(self)(self.path, instance_id=instance_id, qualify=self.qualify)

    def instances(self):
        return [dict(instance_id=r['instance_id'], binding=json.loads(r['binding']) if r['binding'] else None,
                     fallback_package_id=r['fallback']) for r in self.db.execute('SELECT * FROM strategy_registry ORDER BY instance_id')]

    def binding(self):
        row = self.db.execute('SELECT binding FROM strategy_registry WHERE instance_id=?', (self.instance_id,)).fetchone()
        return json.loads(row[0]) if row[0] else None

    def manifest(self, package_id):
        row = self.db.execute('SELECT manifest FROM packages WHERE id=?', (package_id,)).fetchone()
        if row is None:
            raise ValueError('unregistered package')
        return json.loads(row[0])

    def register_package(self, manifest):
        validate_manifest(manifest, qualify=self.qualify)
        with self.atomic():
            self.db.execute('INSERT OR IGNORE INTO packages VALUES(?,?)',
                            (manifest['package_id'], encoded(manifest).decode()))

    def snapshot(self):
        state = dict(self._rows('deployment').fetchone())
        state['checkpoint'] = json.loads(state['checkpoint'])
        row = self._rows('switches', tail='ORDER BY id DESC LIMIT 1').fetchone()
        state['switch'] = dict(row) if row else None
        return state

    def checkpoint(self):
        return self.snapshot()['checkpoint']

    def activation_callback(self):
        row = self.db.execute('SELECT * FROM activation_callbacks WHERE instance_id=?', (self.instance_id,)).fetchone()
        return dict(row) if row else None

    def complete_activation_callback(self, callback):
        with self.atomic():
            self.db.execute('DELETE FROM activation_callbacks WHERE instance_id=? AND switch_id=?',
                            (self.instance_id, callback['switch_id']))

    def pending(self):
        row = self._rows('switches', PENDING).fetchone()
        return dict(row) if row else None

    def summary(self):
        state = self.snapshot()
        state.pop('checkpoint')
        events = []
        for row in self._rows('switch_events', tail='ORDER BY seq DESC LIMIT 30'):
            payload = json.loads(row['payload'])
            events.append(dict(seq=row['seq'], switch_id=row['switch_id'], at=row['at'], event=row['event'],
                **{k: payload[k] for k in ('old', 'new', 'watermark', 'reason') if k in payload}))
        state['events'] = list(reversed(events))
        return state

    def event(self, switch_id, event, **payload):
        self._insert('switch_events', dict(switch_id=switch_id, at=time(), event=event, payload=encoded(payload).decode()))

    def is_current(self, switch):
        return self.pending() == switch and self.snapshot()['active'] == switch['old']

    def transition(self, switch, phase, **payload):
        with self.atomic():
            if not self.is_current(switch):
                return
            if switch['phase'] == phase and switch['reason'] == payload.get('reason'):
                return
            self._update('switches', dict(phase=phase, reason=payload.get('reason'), updated=time()), 'id=?', (switch['id'],))
            self.event(switch['id'], phase, **payload)

    def request(self, manifest, candidate, *, mode='hot', boot='unknown'):
        validate_manifest(manifest, qualify=self.qualify)
        selection = candidate_package(candidate, manifest)
        if self.legacy != (manifest['interface'] != V2):
            raise ValueError('V2 requires a registered nonlegacy instance')
        if not self.legacy and candidate['binding'] != self.binding():
            raise ValueError('promotion routed to the wrong instance/allocation')
        if mode not in {'hot', 'restart'}:
            raise ValueError('invalid deployment mode')
        with self.atomic():
            if self.activation_callback() is not None:
                raise ValueError('activation callback must complete before switching')
            previous = self._rows('switches', 'selection=?', (selection,)).fetchone()
            if previous:
                if not self.legacy:
                    requested = self._rows('switch_events', "switch_id=? AND event='REQUESTED'", (previous['id'],)).fetchone()
                    if json.loads(requested['payload'])['new'] != manifest['package_id']:
                        raise ValueError('selection identity reused for a different package')
                return previous['id']
            state = self.snapshot()
            if candidate.get('expected_active') != state['active']:
                raise ValueError('selector baseline no longer active; stale promotion refused')
            if state['active'] == manifest['package_id']:
                raise ValueError('champion already active')
            if not self.legacy and state['active'] and self.outstanding(state['active']):
                raise ValueError('recover outstanding decisions before switching')
            if not self.legacy and manifest['migration'] == 'artifact_refresh' and state['active'] is not None:
                verify_artifact_refresh_manifests(self.manifest(state['active']), manifest, direction='upgrade')
            self.db.execute('INSERT OR IGNORE INTO packages VALUES(?,?)',
                            (manifest['package_id'], encoded(manifest).decode()))
            now = time()
            cursor = self._insert('switches', dict(selection=selection, old=state['active'], new=manifest['package_id'],
                phase='REQUESTED', mode=mode, policy=manifest['migration'], watermark=state['watermark'],
                boot=str(boot), created=now, updated=now))
            self.event(cursor.lastrowid, 'REQUESTED', old=state['active'], new=manifest['package_id'],
                       watermark=state['watermark'], mode=mode)
            return cursor.lastrowid

    def rollback(self, reason, *, expected=None, expected_active=None):
        with self.atomic():
            if expected is not None and not self.is_current(expected):
                return
            state, current = self.snapshot(), self.pending()
            if expected_active is not None and (state['active'] != expected_active or current is not None):
                return
            if self.activation_callback() is not None:
                raise ValueError('activation callback must complete before rollback')
            if not self.legacy and state['active'] and self.outstanding(state['active']):
                raise ValueError('recover outstanding decisions before rollback')
            fallback = self.db.execute('SELECT fallback FROM strategy_registry WHERE instance_id=?', (self.instance_id,)).fetchone()[0]
            target = state['previous'] or fallback
            if current:
                target = current['old'] if current['old'] is not None else fallback
                rb_policy = 'wait_flat'
                if not self.legacy and target is not None and state['active'] is not None:
                    if target == state['active'] and current['policy'] == 'artifact_refresh':
                        rb_policy = 'artifact_refresh'
                    elif can_artifact_refresh_manifests(self.manifest(state['active']), self.manifest(target), direction='rollback'):
                        rb_policy = 'artifact_refresh'
                self._update('switches', dict(new=target, phase='ROLLBACK', policy=rb_policy, reason=reason, updated=time()),
                             'id=?', (current['id'],))
                switch_id = current['id']
            else:
                rb_policy = 'wait_flat'
                if (not self.legacy and target is not None and state['active'] is not None
                        and can_artifact_refresh_manifests(self.manifest(state['active']), self.manifest(target), direction='rollback')):
                    rb_policy = 'artifact_refresh'
                now = time()
                cursor = self._insert('switches', dict(old=state['active'], new=target, phase='ROLLBACK', mode='hot',
                    policy=rb_policy, watermark=state['watermark'], boot='', reason=reason, created=now, updated=now))
                switch_id = cursor.lastrowid
            self.event(switch_id, 'ROLLBACK_REQUESTED', old=state['active'], new=target, reason=reason,
                       failed_new=current['new'] if current else state['active'])

    def deployment_id_for(self, package_id, prefix):
        row = self._rows('deployment_bindings', 'package_id=?', (package_id,)).fetchone()
        if row is not None:
            return row['deployment_id']
        with self.atomic():
            row = self._rows('deployment_bindings', 'package_id=?', (package_id,)).fetchone()
            if row is not None:
                return row['deployment_id']
            identity = package_id if self.legacy else digest(dict(instance=self.instance_id, package=package_id))
            for offset in range(len(identity) - 4):
                candidate = f'{prefix}-s{identity[offset:offset + 5]}'
                taken = self.db.execute('''SELECT 1 FROM deployment_bindings WHERE deployment_id=?
                    UNION ALL SELECT 1 FROM instance_deployment_bindings WHERE deployment_id=?''', (candidate, candidate)).fetchone()
                if taken is None:
                    self._insert('deployment_bindings', dict(package_id=package_id, deployment_id=candidate))
                    return candidate
            raise ValueError('unable to allocate unique strategy deployment_id')

    def record_order(self, client_id, package_id, event_id, *, reduce_only=False):
        if not isinstance(client_id, str) or not client_id or type(reduce_only) is not bool:
            raise ValueError('order identity and reduce_only required')
        values = dict(client_id=client_id, version=package_id, watermark=event_id, reduce_only=int(reduce_only))
        with self.atomic():
            if not self.legacy:
                bound = self.binding()
                for owner in self.db.execute('''SELECT r.binding FROM instance_strategy_orders o
                    JOIN strategy_registry r ON r.instance_id=o.instance_id
                    WHERE o.client_id=? AND o.instance_id<>?''', (client_id, self.instance_id)):
                    other = json.loads(owner['binding'])
                    if (other['market'], other['account_id']) == (bound['market'], bound['account_id']):
                        raise ValueError('client order ID already owned by another account instance')
            row = self._rows('strategy_orders', 'client_id=?', (client_id,)).fetchone()
            if row is not None:
                if any(row[k] != v for k, v in values.items()):
                    raise ValueError('order ownership identity conflict')
                return
            self._insert('strategy_orders', values)

    def order(self, client_id):
        row = self._rows('strategy_orders', 'client_id=?', (client_id,)).fetchone()
        return dict(row) if row is not None else None

    def decision(self, package_id, event_id, input_identity):
        row = self.db.execute('SELECT * FROM instance_decisions WHERE instance_id=? AND version=? AND watermark=?',
                              (self.instance_id, package_id, event_id)).fetchone()
        if row is None:
            return None
        if row['input_identity'] != input_identity:
            raise ValueError('decision input identity conflict')
        return decode(json.loads(row['result']))

    def bind_runtime(self, package_id, binding):
        payload = dumps(binding)
        with self.atomic():
            row = self.db.execute('SELECT binding FROM instance_runtime_bindings WHERE instance_id=? AND package_id=?',
                                  (self.instance_id, package_id)).fetchone()
            if row is not None and row[0] != payload:
                raise ValueError('instance runtime identity changed')
            self.db.execute('INSERT OR IGNORE INTO instance_runtime_bindings VALUES(?,?,?)',
                            (self.instance_id, package_id, payload))

    def outstanding(self, package_id):
        return [decode(json.loads(row[0])) for row in self.db.execute('''SELECT d.result FROM instance_decisions d
            LEFT JOIN instance_dispatches a ON a.instance_id=d.instance_id AND a.version=d.version AND a.watermark=d.watermark
            WHERE d.instance_id=? AND d.version=? AND a.watermark IS NULL ORDER BY d.rowid''',
            (self.instance_id, package_id))]

    def acknowledge(self, receipt):
        if receipt['binding'] != self.binding():
            raise ValueError('receipt binding mismatch')
        with self.atomic():
            self.db.execute('INSERT OR IGNORE INTO instance_dispatches VALUES(?,?,?)',
                            (self.instance_id, receipt['package_id'], receipt['event_id']))

    def require_new_model_event(self, event):
        latest = self.db.execute('SELECT result FROM instance_decisions WHERE instance_id=? '
            'ORDER BY rowid DESC LIMIT 1', (self.instance_id,)).fetchone()
        if latest is not None:
            previous = decode(json.loads(latest[0]))['event']
            if event['event_time'] <= previous['event_time'] or event['event_id'] == previous['event_id']:
                raise StrategyInputUnavailable('model decision must advance the instance event time')

    def commit_decision(self, package_id, event_id, input_identity, decision, *, event, expected_checkpoint,
                        expected_watermark=_UNSET, model=None):
        if self.legacy:
            raise ValueError('V2 decision journal requires a registered instance')
        with self.atomic():
            saved = self.decision(package_id, event_id, input_identity)
            if saved is not None:
                return saved
            state = self.snapshot()
            if self.pending() is not None or state['active'] != package_id:
                raise ValueError('instance quiesced or decision version is not active')
            if self.activation_callback() is not None:
                raise ValueError('activation callback is incomplete')
            if self.outstanding(package_id):
                raise ValueError('recover outstanding decisions before committing another event')
            if (content_identity(state['checkpoint']) != content_identity(expected_checkpoint)
                    or expected_watermark is not _UNSET and state['watermark'] != expected_watermark):
                raise ValueError('concurrent strategy state update')
            receipt = dict(binding=self.binding(), package_id=package_id, event_id=event_id,
                           input_identity=input_identity, event=event, decision=decision)
            if model is not None:
                self.require_new_model_event(event)
                receipt['model'] = model
            self._insert('decisions', dict(version=package_id, watermark=event_id, input_identity=input_identity, result=dumps(receipt)))
            self._update('deployment', dict(checkpoint=encoded(decision['state']).decode(), watermark=event_id))
            return receipt

    def migrated_checkpoint(self, switch, strategy):
        if self.legacy:
            return {}
        state = self.snapshot()
        if not self.is_current(switch):
            raise SupersededSwitch()
        if switch['phase'] == 'ROLLBACK':
            if state['active'] == switch['new'] or switch['policy'] == 'artifact_refresh':
                return state['checkpoint']
            row = self.db.execute('SELECT checkpoint FROM instance_checkpoints WHERE instance_id=? AND package_id=?',
                                  (self.instance_id, switch['new'])).fetchone()
            return json.loads(row[0]) if row else {}
        old = self.manifest(switch['old']) if switch['old'] else None
        source_version = old['requirements']['state']['version'] if old else None
        identity = digest(dict(old=switch['old'], new=switch['new'], source_version=source_version,
                               target_version=strategy.spec.state_version, checkpoint=state['checkpoint']))
        row = self.db.execute('SELECT * FROM instance_migrations WHERE instance_id=? AND switch_id=?',
                              (self.instance_id, switch['id'])).fetchone()
        if row:
            if row['identity'] != identity:
                raise ValueError('state migration input identity conflict')
            return json.loads(row['checkpoint'])
        migrated = strategy.migrate_state(state['checkpoint'], source_version) if old else {}
        with self.atomic():
            if not self.is_current(switch):
                raise SupersededSwitch()
            row = self.db.execute('SELECT * FROM instance_migrations WHERE instance_id=? AND switch_id=?',
                                  (self.instance_id, switch['id'])).fetchone()
            if row:
                if row['identity'] != identity:
                    raise ValueError('state migration input identity conflict')
                return json.loads(row['checkpoint'])
            if content_identity(self.checkpoint()) != content_identity(state['checkpoint']):
                raise ValueError('state changed during migration')
            self.db.execute('INSERT INTO instance_migrations VALUES(?,?,?,?)',
                            (self.instance_id, switch['id'], identity, encoded(migrated).decode()))
        return migrated

    def activate(self, switch, checkpoint, watermark, facts, boot):
        with self.atomic():
            if not self.is_current(switch):
                return False
            state = self.snapshot()
            previous = switch['old']
            if switch['phase'] == 'ROLLBACK':
                prior = self._rows('switches', "new=? AND phase='SUCCEEDED'", (switch['new'],), tail='ORDER BY id DESC LIMIT 1').fetchone()
                previous = prior['old'] if prior else None
            if not self.legacy and state['active'] is not None:
                self.db.execute('INSERT OR REPLACE INTO instance_checkpoints VALUES(?,?,?)',
                                (self.instance_id, state['active'], encoded(state['checkpoint']).decode()))
            self._update('deployment', dict(active=switch['new'], previous=previous, stable=switch['new'],
                                            checkpoint=encoded(checkpoint).decode(), watermark=watermark))
            phase = 'ROLLED_BACK' if switch['phase'] == 'ROLLBACK' else 'SUCCEEDED'
            self._update('switches', dict(phase=phase, reason=None, updated=time()), 'id=?', (switch['id'],))
            self.event(switch['id'], phase, old=switch['old'], new=switch['new'], watermark=watermark, facts=facts, boot=str(boot))
            self.db.execute('INSERT INTO activation_callbacks VALUES(?,?,?,NULL)',
                            (self.instance_id, switch['id'], switch['new']))
            return True


class DeploymentController:
    def __init__(self, store, adapter, *, load_strategy=None, load_sources=None):
        if not store.legacy and load_strategy is None:
            raise ValueError('V2 deployment requires an isolated strategy loader')
        self.store, self.adapter, self.load_strategy, self.load_sources = store, adapter, load_strategy, load_sources

    def _complete_activation(self):
        callback = self.store.activation_callback()
        if callback is None:
            return
        try:
            self.adapter.activated(callback['package_id'])
        except Exception as error:
            with self.store.atomic():
                self.store.db.execute('UPDATE activation_callbacks SET reason=? WHERE instance_id=? AND switch_id=?',
                    (type(error).__name__, self.store.instance_id, callback['switch_id']))
        else:
            self.store.complete_activation_callback(callback)

    def step(self):
        store = self.store
        if store.path == ':memory:':
            if not store._controller_lock.acquire(blocking=False):
                return store.snapshot()
            try:
                return self._step()
            finally:
                store._controller_lock.release()
        with controller_lock(store.path, store.instance_id) as acquired:
            if not acquired:
                return store.snapshot()
            return self._step()

    def _step(self):
        if self.store.activation_callback() is not None:
            self._complete_activation()
            return self.store.snapshot()
        switch = self.store.pending()
        if switch is None:
            return self.store.snapshot()
        a, s = self.adapter, self.store
        try:
            a.quiesce()
            facts = a.reconcile()
            if not facts['reconciled'] or facts['unknown'] or facts.get('untracked_orders'):
                s.transition(switch, switch['phase'], reason='ORDER_STATE_UNCONFIRMED')
                return s.snapshot()
            if switch['phase'] == 'REQUESTED':
                with s.atomic():
                    if not s.is_current(switch):
                        return s.snapshot()
                    watermark = a.watermark()
                    s._update('switches', dict(phase='DRAINING', watermark=watermark, boot=str(a.boot_id), updated=time()), 'id=?', (switch['id'],))
                    s.event(switch['id'], 'QUIESCED', old=switch['old'], new=switch['new'], watermark=watermark)
                    switch = s.pending()
            orders = [o for o in facts['orders'] if o['status'] not in TERMINAL]
            for order in orders:
                if not order['reduce_only']:
                    a.cancel(order['client_id'])
            if orders or facts.get('pending_cancellations'):
                s.transition(switch, switch['phase'], reason='WAIT_TERMINAL_ORDERS')
                return s.snapshot()
            artifact_refresh = (
                switch['policy'] == 'artifact_refresh'
                and not s.legacy
                and switch['old'] is not None
                and switch['new'] is not None
            )
            prepared_strategy = None
            if artifact_refresh:
                old_manifest = s.manifest(switch['old'])
                new_manifest = s.manifest(switch['new'])
                direction = 'rollback' if switch['phase'] == 'ROLLBACK' else 'upgrade'
                verify_artifact_refresh_manifests(old_manifest, new_manifest, direction=direction)
                if self.load_sources is not None:
                    # Validate hash-checked sources via pure AST BEFORE importing/loading any candidate strategy.
                    old_sources = self.load_sources(switch['old'])
                    new_sources = old_sources if switch['new'] == switch['old'] else self.load_sources(switch['new'])
                    verify_artifact_refresh_packages(
                        old_manifest,
                        new_manifest,
                        old_sources=old_sources,
                        new_sources=new_sources,
                        direction=direction,
                    )
                else:
                    old_strategy = self.load_strategy(switch['old'])
                    new_strategy = old_strategy if switch['new'] == switch['old'] else self.load_strategy(switch['new'])
                    verify_artifact_refresh_packages(
                        old_manifest,
                        new_manifest,
                        old_sources=old_strategy.sources,
                        new_sources=new_strategy.sources,
                        direction=direction,
                    )
                    prepared_strategy = new_strategy
            if facts['positions'] and not artifact_refresh:
                if switch['policy'] == 'reduce_only' and switch['phase'] != 'ROLLBACK':
                    a.reduce(switch['id'], switch['new'])
                s.transition(switch, switch['phase'], reason='WAIT_OLD_POSITIONS')
                return s.snapshot()
            if switch['mode'] == 'restart' and switch['phase'] != 'ROLLBACK' and str(a.boot_id) == switch['boot']:
                s.transition(switch, 'READY_RESTART')
                return s.snapshot()
            if switch['new'] is None:
                s.transition(switch, 'ROLLBACK', reason='NO_FALLBACK_PACKAGE')
                return s.snapshot()
            a.prepare(switch['new'])
            strategy = prepared_strategy if prepared_strategy is not None else (
                self.load_strategy(switch['new']) if not s.legacy else None
            )
            if strategy is not None:
                from .package import manifest_spec
                if strategy.spec != manifest_spec(s.manifest(switch['new'])):
                    raise ValueError('prepared strategy differs from registered package')
                strategy.spec.require_binding(s.binding())
            checkpoint = s.migrated_checkpoint(switch, strategy)
            if s.activate(switch, checkpoint, a.watermark(), facts, a.boot_id):
                self._complete_activation()
        except SupersededSwitch:
            pass
        except (OSError, TimeoutError) as error:
            s.transition(switch, switch['phase'], reason=type(error).__name__)
        except Exception as error:
            if switch['phase'] == 'ROLLBACK':
                s.transition(switch, 'ROLLBACK', reason='ROLLBACK_FAILED:' + type(error).__name__)
            else:
                s.rollback('ACTIVATION_FAILED:' + type(error).__name__, expected=switch)
        return s.snapshot()


class OfflineAdapter:
    """Fault injection adapter with no network or trading capability."""
    def __init__(self):
        self.boot_id = 'offline-1'
        self.facts = dict(reconciled=True, unknown=[], positions=[], orders=[])
        self.actions = []
        self.failure = None
        self.mark = '0'

    def quiesce(self): self.actions.append(('quiesce',))
    def reconcile(self):
        if self.failure == 'network': raise OSError('offline injected')
        return self.facts
    def watermark(self): return self.mark
    def cancel(self, client_id): self.actions.append(('cancel', client_id))
    def reduce(self, switch_id, version): self.actions.append(('reduce', switch_id, version))
    def prepare(self, version):
        if self.failure == 'strategy': raise ValueError('offline strategy injected')
        self.actions.append(('prepare', version))
    def activated(self, version): self.actions.append(('activate', version))
