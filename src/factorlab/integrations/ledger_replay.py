"""Resume a confirmed account event across its existing domain databases.

Each reducer uses its own original SQLite store. A crash between stores leaves
an incomplete account cut, not an execution-ready state. Repeating the batch
finishes the remaining reducers without repeating already committed postings.
"""
from contextlib import contextmanager, ExitStack, closing
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
import sqlite3

from .ledger_replication import ReplicationBatch, ReplicationError, _hash


@dataclass(frozen=True)
class SnapshotPosition:
    stream: str
    generation: str
    sequence: int
    digest: str | None

    def __post_init__(self):
        if (not isinstance(self.stream, str) or not self.stream
                or not isinstance(self.generation, str) or not self.generation
                or type(self.sequence) is not int or self.sequence < 0
                or (self.sequence == 0 and self.digest is not None)
                or (self.sequence > 0 and not _hash(self.digest))):
            raise ReplicationError('invalid snapshot replication connection point')


class DomainReplay:
    """A domain reducer and its application position share one transaction.

    reduce_event(connection, event) must use this connection only, preserve the
    original event's economic identities and perform no external IO. Source
    execution must remain quiesced until every domain reaches the account cut.
    """
    def __init__(self, db, reduce_event, *, baseline, restore_snapshot=False):
        self.db, self.reduce_event, self.baseline = db, reduce_event, baseline
        self.stream, self.generation = baseline.stream, baseline.generation
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS dr_replay_identity (
                id INTEGER PRIMARY KEY CHECK(id=1), stream TEXT NOT NULL,
                generation TEXT NOT NULL, base_sequence INTEGER NOT NULL, base_digest TEXT);
            CREATE TABLE IF NOT EXISTS dr_replay_batches (
                sequence INTEGER PRIMARY KEY, digest TEXT);
        ''')
        self.db.execute('BEGIN IMMEDIATE')
        try:
            identity = (baseline.stream, baseline.generation, baseline.sequence, baseline.digest)
            saved = self.db.execute('SELECT stream,generation,base_sequence,base_digest '
                                    'FROM dr_replay_identity WHERE id=1').fetchone()
            saved = None if saved is None else tuple(saved)
            if restore_snapshot:
                if saved is None:
                    raise ReplicationError('snapshot domain has no recorded replication coverage')
                if saved[:2] != identity[:2] or self.position() != baseline:
                    raise ReplicationError('domain snapshot connection point does not match the restore manifest')
            if saved is None:
                self.db.execute('INSERT INTO dr_replay_identity VALUES(1,?,?,?,?)', identity)
                self.db.execute('INSERT INTO dr_replay_batches VALUES(?,?)',
                                (baseline.sequence, baseline.digest))
            elif saved != identity:
                if not restore_snapshot:
                    raise ReplicationError('domain snapshot connection point does not match the restore manifest')
                self.db.execute('UPDATE dr_replay_identity SET base_sequence=?,base_digest=? WHERE id=1',
                                (baseline.sequence, baseline.digest))
            self.db.execute('COMMIT')
        except BaseException:
            self.db.execute('ROLLBACK')
            raise

    def position(self):
        sequence, digest = self.db.execute('SELECT sequence,digest FROM dr_replay_batches '
                                          'ORDER BY sequence DESC LIMIT 1').fetchone()
        return SnapshotPosition(self.stream, self.generation, sequence, digest)

    def check(self, batch, digest):
        """Check every domain before any domain applies this batch."""
        if (batch.stream, batch.generation) != (self.stream, self.generation):
            raise ReplicationError('batch belongs to a different domain replication lineage')
        saved = self.db.execute('SELECT digest FROM dr_replay_batches WHERE sequence=?',
                               (batch.sequence,)).fetchone()
        if saved is not None:
            if saved[0] != digest:
                raise ReplicationError('replayed batch conflicts with the domain application history')
            return False
        position = self.position()
        if batch.sequence != position.sequence + 1 or batch.predecessor != position.digest:
            raise ReplicationError('domain replay has a missing or conflicting predecessor')
        return True

    def apply(self, batch, digest, events):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            if self.check(batch, digest):
                for event in events:
                    self.reduce_event(self.db, event)
                self.db.execute('INSERT INTO dr_replay_batches VALUES(?,?)', (batch.sequence, digest))
            self.db.execute('COMMIT')
        except BaseException:
            self.db.execute('ROLLBACK')
            raise


def _database_path(db):
    filename = next(row[2] for row in db.execute('PRAGMA database_list') if row[1] == 'main')
    return Path(filename).resolve() if filename else None


class AccountReplay:
    """Replay the same ordered cut into all declared critical domains.

    domains is a map of stable manifest names to DomainReplay. Events explicitly
    list their affected domains in targets, rather than letting received content
    select database paths or SQL statements.
    """
    def __init__(self, domains, *, baseline):
        if not domains:
            raise ReplicationError('the account recovery manifest must declare its critical domains')
        self.domains, self.baseline = dict(domains), baseline
        databases = set()
        for domain in self.domains.values():
            database = _database_path(domain.db) or id(domain.db)
            if database in databases:
                raise ReplicationError('each replay domain must own a distinct database')
            databases.add(database)
            if domain.baseline != baseline:
                raise ReplicationError('account domains have different snapshot connection points')

    def apply(self, payload):
        batch = ReplicationBatch.parse(payload)
        if (batch.stream, batch.generation) != (self.baseline.stream, self.baseline.generation):
            raise ReplicationError('batch belongs to a different account recovery lineage')
        routed = self.route(batch.events)
        for domain in self.domains.values():
            domain.check(batch, payload['digest'])
        for name, domain in self.domains.items():
            domain.apply(batch, payload['digest'], routed[name])
        return SnapshotPosition(batch.stream, batch.generation, batch.sequence, payload['digest'])

    def route(self, events):
        """Validate declared targets before admission as well as during recovery."""
        routed = {name: [] for name in self.domains}
        for event in events:
            targets = event.get('targets')
            if (not isinstance(targets, list) or not targets
                    or any(not isinstance(target, str) for target in targets)
                    or len(set(targets)) != len(targets)
                    or set(targets) - self.domains.keys()):
                raise ReplicationError('event targets must name the declared critical domains')
            for target in targets:
                routed[target].append(event)
        return routed

    def catch_up(self, receiver):
        """Apply one pinned durable prefix; this does not authorize execution."""
        baseline = self.baseline
        if (receiver.stream, receiver.generation) != (baseline.stream, baseline.generation):
            raise ReplicationError('receiving journal belongs to a different recovery lineage')
        if receiver.connection_digest(baseline.sequence) != baseline.digest:
            raise ReplicationError('snapshot connection digest differs from the receiving history')
        cursor = receiver.cursor()
        end = baseline if cursor is None else SnapshotPosition(
            cursor.stream, cursor.generation, cursor.sequence, cursor.digest)
        if any(position.sequence > end.sequence for position in self.positions().values()):
            raise ReplicationError('domain application is ahead of the durable receiving cut')
        for payload in receiver.batches_after(baseline.sequence, through=end.sequence):
            self.apply(payload)
        if not self.complete_at(end):
            raise ReplicationError('account domains have not reached the durable receiving cut')
        return end

    def positions(self):
        return {name: domain.position() for name, domain in self.domains.items()}

    def complete_at(self, position):
        return all(domain.position() == position for domain in self.domains.values())


@contextmanager
def restored_account(root, manifest, reducers):
    """Open verified snapshot domains in quarantined staging, never publish them.

    manifest is the already verified recovery manifest. reducers is the runtime's
    fixed complete domain registry, not a map supplied by received events.
    """
    metadata = manifest['replication']
    if (set(metadata) != {'version', 'position', 'domains', 'admission_database'}
            or type(metadata['version']) is not int or metadata['version'] != 1
            or set(metadata['domains']) != set(reducers)):
        raise ReplicationError('snapshot critical domains differ from the runtime reducer registry')
    baseline = SnapshotPosition(**metadata['position'])
    root = Path(root).resolve()
    paths = {}
    for name, relative in [*metadata['domains'].items(),
                           (None, metadata['admission_database'])]:
        path = PurePosixPath(relative)
        if (not relative or path.is_absolute() or '..' in path.parts or '\\' in relative
                or ':' in relative or path.as_posix() != relative
                or relative not in manifest['files']):
            raise ReplicationError('snapshot replication path is not in the verified inventory')
        local = root.joinpath(*path.parts)
        if local.resolve() != local or not local.is_file():
            raise ReplicationError('snapshot replication database is missing or redirected')
        paths[name] = local
    if len(set(paths.values())) != len(paths):
        raise ReplicationError('snapshot replication databases must be distinct')
    with ExitStack() as stack:
        domains = {}
        for name, reducer in reducers.items():
            db = stack.enter_context(closing(sqlite3.connect(
                paths[name].as_uri() + '?mode=rw', uri=True, isolation_level=None)))
            domains[name] = DomainReplay(db, reducer, baseline=baseline, restore_snapshot=True)
        yield AccountReplay(domains, baseline=baseline)


class AccountEventWriter:
    """One admission sequence for every critical domain of an owned account.

    The existing account ownership lock must span proposal, confirmation and
    application. No execution may observe a partially applied account event.
    The admission connection is separate from the domain connections. Its
    pending marker is cleared only after every domain commits the same cut.
    """
    def __init__(self, admission, replay):
        self.admission, self.replay = admission, replay
        if (admission.stream, admission.generation) != (
                replay.baseline.stream, replay.baseline.generation):
            raise ReplicationError('admission and domain replay have different account lineages')

    def snapshot_position(self):
        """Capture a common cut while the owner and every domain writer are frozen."""
        if self.admission.pending() is not None:
            raise ReplicationError('cannot snapshot a pending account admission')
        cursor = self.admission.applied_cursor()
        position = SnapshotPosition(self.admission.stream, self.admission.generation,
                                    0 if cursor is None else cursor.sequence,
                                    None if cursor is None else cursor.digest)
        if not self.replay.complete_at(position):
            raise ReplicationError('snapshot domains differ from completed account admissions')
        return position

    def snapshot_capture(self, root, *, task_mailbox=None):
        """Build a capture callback for recovery_snapshot's existing writer freeze."""
        root = Path(root).resolve()
        mailbox = None if task_mailbox is None else Path(task_mailbox).resolve()

        def capture(frozen_databases):
            frozen = {Path(path).resolve() for path in frozen_databases}

            def name(db):
                path = _database_path(db)
                if path not in frozen:
                    raise ReplicationError('replication database is absent from the snapshot freeze')
                if path == mailbox:
                    return 'external/task-mailbox/tasks.sqlite3'
                try:
                    return path.relative_to(root).as_posix()
                except ValueError:
                    raise ReplicationError('replication database is outside the declared snapshot root') from None

            domains = {domain: name(replay.db) for domain, replay in self.replay.domains.items()}
            admission = name(self.admission.db)
            if admission in domains.values():
                raise ReplicationError('account admission journal must be separate from domain databases')
            return dict(version=1, position=asdict(self.snapshot_position()),
                        domains=domains, admission_database=admission)

        return capture

    def submit(self, event, *, committed_at):
        self.replay.route([event])
        payload = self.admission.propose(event, committed_at=committed_at)
        # A stable identity retry may refer to an already completed event.
        if self.admission.pending() != payload:
            return SnapshotPosition(payload['stream'], payload['generation'],
                                    payload['sequence'], payload['digest'])
        return self.resume()

    def resume(self):
        payload = self.admission.confirm_pending()
        if payload is None:
            return None
        position = self.replay.apply(payload)
        self.admission.finish_confirmed(payload, lambda _db, _event: None)
        return position
