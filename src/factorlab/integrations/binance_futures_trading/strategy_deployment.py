"""Restartable switch saga. Adapters share one account owner; never restore a ledger."""
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
from time import time
from .strategy_package import encoded, validate_manifest, candidate_package


LEGACY = 'legacy-grid'
TERMINAL = {'FILLED', 'CANCELED', 'REJECTED', 'EXPIRED'}


class DeploymentStore:
    def __init__(self, path):
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
        ''')

    @contextmanager
    def atomic(self):
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            yield

    def close(self):
        self.db.close()

    def snapshot(self):
        state = dict(self.db.execute('SELECT * FROM deployment').fetchone())
        state['checkpoint'] = json.loads(state['checkpoint'])
        row = self.db.execute('SELECT * FROM switches ORDER BY id DESC LIMIT 1').fetchone()
        state['switch'] = dict(row) if row else None
        return state

    def pending(self):
        row = self.db.execute("SELECT * FROM switches WHERE phase NOT IN ('SUCCEEDED','ROLLED_BACK','FAILED')").fetchone()
        return dict(row) if row else None

    def summary(self):
        state = self.snapshot()
        state.pop('checkpoint')
        events = []
        for row in self.db.execute('SELECT * FROM switch_events ORDER BY seq DESC LIMIT 30'):
            payload = json.loads(row['payload'])
            events.append(dict(seq=row['seq'], switch_id=row['switch_id'], at=row['at'], event=row['event'],
                **{k: payload[k] for k in ('old', 'new', 'watermark', 'reason') if k in payload}))
        state['events'] = list(reversed(events))
        return state

    def event(self, switch_id, event, **payload):
        self.db.execute('INSERT INTO switch_events(switch_id,at,event,payload) VALUES(?,?,?,?)',
                        (switch_id, time(), event, encoded(payload).decode()))

    def transition(self, switch_id, phase, **payload):
        with self.atomic():
            current = self.db.execute('SELECT phase,reason FROM switches WHERE id=?', (switch_id,)).fetchone()
            if current['phase'] == phase and current['reason'] == payload.get('reason'):
                return
            self.db.execute('UPDATE switches SET phase=?,reason=?,updated=? WHERE id=?',
                            (phase, payload.get('reason'), time(), switch_id))
            self.event(switch_id, phase, **payload)

    def request(self, manifest, candidate, *, mode='hot', boot='unknown'):
        validate_manifest(manifest)
        selection = candidate_package(candidate, manifest)
        if mode not in {'hot', 'restart'}:
            raise ValueError('invalid deployment mode')
        with self.atomic():
            previous = self.db.execute('SELECT id FROM switches WHERE selection=?', (selection,)).fetchone()
            if previous:
                return previous[0]
            state = self.snapshot()
            if candidate.get('expected_active') != state['active']:
                raise ValueError('selector baseline no longer active; stale promotion refused')
            if state['active'] == manifest['package_id']:
                raise ValueError('champion already active')
            self.db.execute('INSERT OR IGNORE INTO packages VALUES(?,?)',
                            (manifest['package_id'], encoded(manifest).decode()))
            now = time()
            cursor = self.db.execute('''INSERT INTO switches(selection,old,new,phase,mode,policy,
              watermark,boot,created,updated) VALUES(?,?,?,'REQUESTED',?,?,?,?,?,?)''',
              (selection, state['active'], manifest['package_id'], mode, manifest['migration'],
               state['watermark'], str(boot), now, now))
            self.event(cursor.lastrowid, 'REQUESTED', old=state['active'], new=manifest['package_id'],
                       watermark=state['watermark'], mode=mode)
            return cursor.lastrowid

    def rollback(self, reason):
        with self.atomic():
            state = self.snapshot()
            current = self.pending()
            target = state['previous'] or LEGACY
            if current:
                # A failure before activation keeps the stable version as the destination.
                target = current['old']
                self.db.execute("UPDATE switches SET new=?,phase='ROLLBACK',policy='wait_flat',reason=?,updated=? WHERE id=?",
                                (target, reason, time(), current['id']))
                switch_id = current['id']
            else:
                now = time()
                cursor = self.db.execute('''INSERT INTO switches(old,new,phase,mode,policy,watermark,
                  boot,reason,created,updated) VALUES(?,?,'ROLLBACK','hot','wait_flat',?,'',?,?,?)''',
                  (state['active'], target, state['watermark'], reason, now, now))
                switch_id = cursor.lastrowid
            self.event(switch_id, 'ROLLBACK_REQUESTED', old=state['active'], new=target, reason=reason,
                       failed_new=current['new'] if current else state['active'])


class DeploymentController:
    def __init__(self, store, adapter):
        self.store, self.adapter = store, adapter

    def step(self):
        """Every step starts from current GET facts. A crash can repeat any step."""
        switch = self.store.pending()
        if switch is None:
            return self.store.snapshot()
        a, s = self.adapter, self.store
        a.quiesce()
        try:
            facts = a.reconcile()
            if not facts['reconciled'] or facts['unknown'] or facts.get('untracked_orders'):
                s.transition(switch['id'], switch['phase'], reason='ORDER_STATE_UNCONFIRMED')
                return s.snapshot()
            if switch['phase'] == 'REQUESTED':
                with s.atomic():
                    s.db.execute("UPDATE switches SET phase='DRAINING',watermark=?,boot=?,updated=? WHERE id=?",
                                 (a.watermark(), str(a.boot_id), time(), switch['id']))
                    s.event(switch['id'], 'QUIESCED', old=switch['old'], new=switch['new'], watermark=a.watermark())
                switch = s.pending()
            orders = [o for o in facts['orders'] if o['status'] not in TERMINAL]
            for order in orders:
                if not order['reduce_only']:
                    a.cancel(order['client_id'])
            if orders:
                s.transition(switch['id'], switch['phase'], reason='WAIT_TERMINAL_ORDERS')
                return s.snapshot()
            if facts['positions']:
                if switch['policy'] == 'reduce_only' and switch['phase'] != 'ROLLBACK':
                    a.reduce(switch['id'], switch['new'])
                s.transition(switch['id'], switch['phase'], reason='WAIT_OLD_POSITIONS')
                return s.snapshot()
            if switch['mode'] == 'restart' and switch['phase'] != 'ROLLBACK' and str(a.boot_id) == switch['boot']:
                s.transition(switch['id'], 'READY_RESTART')
                return s.snapshot()
            a.prepare(switch['new'])
            # The runner is stateless outside this checkpoint. Ownership and activation are one commit.
            with s.atomic():
                previous = switch['old']
                if switch['phase'] == 'ROLLBACK':
                    prior = s.db.execute("SELECT old FROM switches WHERE new=? AND phase='SUCCEEDED' ORDER BY id DESC LIMIT 1",
                                         (switch['new'],)).fetchone()
                    previous = prior[0] if prior else None
                s.db.execute("UPDATE deployment SET active=?,previous=?,stable=?,checkpoint='{}',watermark=? WHERE id=1",
                             (switch['new'], previous, switch['new'], a.watermark()))
                phase = 'ROLLED_BACK' if switch['phase'] == 'ROLLBACK' else 'SUCCEEDED'
                s.db.execute('UPDATE switches SET phase=?,reason=NULL,updated=? WHERE id=?',
                             (phase, time(), switch['id']))
                s.event(switch['id'], phase, old=switch['old'], new=switch['new'],
                        watermark=a.watermark(), facts=facts, boot=str(a.boot_id))
            a.activated(switch['new'])
        except (OSError, TimeoutError) as error:
            s.transition(switch['id'], switch['phase'], reason=type(error).__name__)
        except Exception as error:
            if switch['phase'] == 'ROLLBACK':
                # Remain blocked with a pending rollback: no restart can accidentally resume the bad runner.
                s.transition(switch['id'], 'ROLLBACK', reason='ROLLBACK_FAILED:' + type(error).__name__)
            else:
                s.rollback('ACTIVATION_FAILED:' + type(error).__name__)
        return s.snapshot()


class OfflineAdapter:
    """Fault injection adapter, deliberately has no network or trading capability."""
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
