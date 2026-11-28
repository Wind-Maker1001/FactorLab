"""Immutable event envelopes and a transactionally durable receiving journal.

A received prefix is not proof that a disappeared source had no later commits.
The event admission and source application protocol must establish that boundary
before a recovery caller can publish an execution state.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
import argparse
import json
from pathlib import Path
import sqlite3
import subprocess
import sys


SCHEMA_VERSION = 1
MAX_BATCH_BYTES = 1024 * 1024


class ReplicationError(ValueError):
    """The received data conflicts with the replication contract."""


class MissingBatch(ReplicationError):
    """The next required predecessor has not been received."""


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def _digest(value):
    return sha256(_canonical(value)).hexdigest()


def _hash(value):
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


@dataclass(frozen=True)
class ReplicationBatch:
    stream: str
    generation: str
    sequence: int
    predecessor: str | None
    events: tuple[dict, ...]
    committed_at: str
    complete: bool = True

    def envelope(self):
        if (not isinstance(self.stream, str) or not self.stream
                or not isinstance(self.generation, str) or not self.generation
                or type(self.sequence) is not int or self.sequence < 1):
            raise ReplicationError("stream, generation and positive integer sequence are required")
        if self.complete is not True:
            raise ReplicationError("incomplete batches cannot be published")
        if ((self.sequence == 1 and self.predecessor is not None)
                or (self.sequence > 1 and not _hash(self.predecessor))):
            raise ReplicationError("batch predecessor does not match its sequence")
        try:
            at = datetime.fromisoformat(self.committed_at)
        except (ValueError, TypeError):
            raise ReplicationError("commit time must be an offset-aware ISO timestamp") from None
        if at.utcoffset() is None:
            raise ReplicationError("commit time must include a UTC offset")
        if any(not isinstance(event, dict) or not isinstance(event.get("event_id"), str)
               or not event["event_id"] for event in self.events):
            raise ReplicationError("every replicated event needs a stable event_id")
        identities = [event["event_id"] for event in self.events]
        if len(set(identities)) != len(identities):
            raise ReplicationError("an event identity occurs twice in a batch")
        payload = {
            "schema_version": SCHEMA_VERSION,
            "stream": self.stream,
            "generation": self.generation,
            "sequence": self.sequence,
            "predecessor": self.predecessor,
            "events": list(self.events),
            "committed_at": self.committed_at,
        }
        payload["digest"] = _digest(payload)
        return payload

    @classmethod
    def parse(cls, payload):
        keys = {"schema_version", "stream", "generation", "sequence", "predecessor",
                "events", "committed_at", "digest"}
        if (not isinstance(payload, dict) or set(payload) != keys
                or type(payload["schema_version"]) is not int
                or payload["schema_version"] != SCHEMA_VERSION
                or not isinstance(payload["events"], list)):
            raise ReplicationError("unsupported or incomplete replication envelope")
        batch = cls(payload["stream"], payload["generation"], payload["sequence"],
                    payload["predecessor"], tuple(payload["events"]), payload["committed_at"])
        if payload != batch.envelope():
            raise ReplicationError("replication batch digest mismatch")
        return batch


@dataclass(frozen=True)
class ReplicationCursor:
    stream: str
    generation: str
    sequence: int
    digest: str


class BatchReceiver:
    """One explicitly bound stream/generation per private receiving directory.

    The batch, event identity index and cursor commit in the same FULL-sync
    SQLite transaction. Returning from accept is the receiver's durable ACK.
    """
    def __init__(self, directory, *, stream, generation):
        if not stream or not generation:
            raise ReplicationError("explicit stream and generation are required")
        self.stream, self.generation = stream, generation
        directory = Path(directory)
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.db = sqlite3.connect(directory / "replication.sqlite3", isolation_level=None)
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS identity (
                id INTEGER PRIMARY KEY CHECK(id=1), stream TEXT NOT NULL, generation TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS batches (
                sequence INTEGER PRIMARY KEY, digest TEXT UNIQUE NOT NULL, payload BLOB NOT NULL);
            CREATE TABLE IF NOT EXISTS events (
                event_id TEXT PRIMARY KEY, sequence INTEGER NOT NULL, payload BLOB NOT NULL);
        """)
        self.db.execute("BEGIN IMMEDIATE")
        try:
            self.db.execute("INSERT OR IGNORE INTO identity VALUES(1,?,?)", (stream, generation))
            saved = self.db.execute("SELECT stream,generation FROM identity WHERE id=1").fetchone()
            if saved != (stream, generation):
                raise ReplicationError("receiving journal belongs to another stream or generation")
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            self.db.close()
            raise

    def close(self):
        self.db.close()

    def cursor(self):
        row = self.db.execute("SELECT sequence,digest FROM batches ORDER BY sequence DESC LIMIT 1").fetchone()
        return None if row is None else ReplicationCursor(self.stream, self.generation, *row)

    def accept(self, payload):
        batch = ReplicationBatch.parse(payload)
        if (batch.stream, batch.generation) != (self.stream, self.generation):
            raise ReplicationError("batch belongs to another stream or generation")
        encoded = _canonical(payload)
        if len(encoded) > MAX_BATCH_BYTES:
            raise ReplicationError("replication batch exceeds the receiving budget")
        self.db.execute("BEGIN IMMEDIATE")
        try:
            saved = self.db.execute("SELECT payload FROM batches WHERE sequence=?", (batch.sequence,)).fetchone()
            if saved is not None:
                if saved[0] != encoded:
                    raise ReplicationError("immutable batch identity changed")
                result = "duplicate"
            else:
                cursor = self.cursor()
                if (batch.sequence != (1 if cursor is None else cursor.sequence + 1)
                        or batch.predecessor != (None if cursor is None else cursor.digest)):
                    raise MissingBatch("replication batch is not the next contiguous predecessor")
                for event in batch.events:
                    if self.db.execute("SELECT 1 FROM events WHERE event_id=?", (event["event_id"],)).fetchone():
                        raise ReplicationError("event identity already belongs to a committed batch")
                    self.db.execute("INSERT INTO events VALUES(?,?,?)",
                                    (event["event_id"], batch.sequence, _canonical(event)))
                self.db.execute("INSERT INTO batches VALUES(?,?,?)", (batch.sequence, payload["digest"], encoded))
                result = "accepted"
            self.db.execute("COMMIT")
            return result
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def connection_digest(self, sequence):
        """Resolve a snapshot connection point in the retained receiving history."""
        if type(sequence) is not int or sequence < 0:
            raise ReplicationError("invalid replication connection sequence")
        if sequence == 0:
            return None
        row = self.db.execute('SELECT digest FROM batches WHERE sequence=?', (sequence,)).fetchone()
        if row is None:
            raise MissingBatch("snapshot connection point is absent from the receiving history")
        return row[0]

    def batches_after(self, sequence, *, through=None):
        """Stream a durable prefix, optionally ending at a pinned receiving cut."""
        query = 'SELECT payload FROM batches WHERE sequence>?'
        arguments = (sequence,)
        if through is not None:
            query += ' AND sequence<=?'
            arguments += (through,)
        for payload, in self.db.execute(query + ' ORDER BY sequence', arguments):
            yield json.loads(payload)


def make_batch(stream, generation, sequence, predecessor, events, committed_at):
    return ReplicationBatch(stream, generation, sequence, predecessor, tuple(events), committed_at).envelope()


class EventAdmission:
    """Write-before-apply admission on the owning domain SQLite connection.

    A pending proposal is durable before network IO. Once the receiver accepts
    it, the event is authoritative even if the source dies before local apply.
    apply_event must reduce the original event using this connection; its domain
    writes and the application marker commit together. No callback may perform
    HTTP, write another database, or recompute a strategy decision.
    """
    def __init__(self, db, *, stream, generation, send):
        self.db, self.stream, self.generation, self.send = db, stream, generation, send
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS dr_admission_identity (
                id INTEGER PRIMARY KEY CHECK(id=1), stream TEXT NOT NULL, generation TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS dr_admissions (
                sequence INTEGER PRIMARY KEY, event_id TEXT UNIQUE NOT NULL,
                payload BLOB NOT NULL, applied INTEGER NOT NULL CHECK(applied IN (0,1)));
            CREATE UNIQUE INDEX IF NOT EXISTS dr_one_pending_admission
                ON dr_admissions(applied) WHERE applied=0;
        """)
        self.db.execute("BEGIN IMMEDIATE")
        try:
            self.db.execute("INSERT OR IGNORE INTO dr_admission_identity VALUES(1,?,?)", (stream, generation))
            if tuple(self.db.execute("SELECT stream,generation FROM dr_admission_identity").fetchone()) != (stream, generation):
                raise ReplicationError("source journal belongs to another stream or generation")
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def pending(self):
        row = self.db.execute("SELECT payload FROM dr_admissions WHERE applied=0").fetchone()
        return None if row is None else json.loads(row[0])

    def applied_cursor(self):
        """Read the latest completed admission under the account snapshot lock."""
        row = self.db.execute('SELECT sequence,payload FROM dr_admissions WHERE applied=1 '
                              'ORDER BY sequence DESC LIMIT 1').fetchone()
        if row is None:
            return None
        payload = json.loads(row[1])
        return ReplicationCursor(self.stream, self.generation, row[0], payload['digest'])

    def propose(self, event, *, committed_at):
        """Persist an immutable proposal without applying its economic effects."""
        self.db.execute("BEGIN IMMEDIATE")
        try:
            saved = self.db.execute("SELECT payload FROM dr_admissions WHERE event_id=?", (event["event_id"],)).fetchone()
            if saved is not None:
                payload = json.loads(saved[0])
                if payload["events"] != [event]:
                    raise ReplicationError("event identity binds different admission content")
            else:
                if self.pending() is not None:
                    raise ReplicationError("resolve the pending admission before proposing another event")
                previous = self.db.execute("SELECT sequence,payload FROM dr_admissions ORDER BY sequence DESC LIMIT 1").fetchone()
                sequence = 1 if previous is None else previous[0] + 1
                predecessor = None if previous is None else json.loads(previous[1])["digest"]
                payload = make_batch(self.stream, self.generation, sequence, predecessor, [event], committed_at)
                encoded = _canonical(payload)
                if len(encoded) > MAX_BATCH_BYTES:
                    raise ReplicationError("replication admission exceeds the receiving budget")
                self.db.execute("INSERT INTO dr_admissions VALUES(?,?,?,0)",
                                (sequence, event["event_id"], encoded))
            self.db.execute("COMMIT")
            return payload
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def confirm_pending(self):
        """Return the original proposal only after its exact offsite durable ACK."""
        payload = self.pending()
        if payload is None:
            return None
        ack = self.send(payload)
        expected = ReplicationCursor(self.stream, self.generation, payload["sequence"], payload["digest"])
        if ack != expected:
            raise ReplicationError("receiver did not acknowledge this exact durable admission")
        return payload

    def resume(self, apply_event):
        """Retry the same proposal after lost ACK or source application failure."""
        payload = self.confirm_pending()
        if payload is None:
            return None
        return self.finish_confirmed(payload, apply_event)

    def finish_confirmed(self, payload, apply_event):
        """Commit reduction after confirm_pending; caller retains account ownership."""
        expected = ReplicationCursor(self.stream, self.generation, payload["sequence"], payload["digest"])
        self.db.execute("BEGIN IMMEDIATE")
        try:
            pending = self.pending()
            if pending is not None:
                if pending != payload:
                    raise ReplicationError("pending admission changed during transmission")
                apply_event(self.db, payload["events"][0])
                self.db.execute("UPDATE dr_admissions SET applied=1 WHERE sequence=?", (payload["sequence"],))
            self.db.execute("COMMIT")
            return expected
        except BaseException:
            self.db.execute("ROLLBACK")
            raise


class SSHBatchTransport:
    """One bounded stdin batch over an existing, host-key-pinned SSH alias.

    The alias must use a dedicated key with a forced receiver command; neither
    the sender nor event content chooses the offsite directory or receiver scope.
    """
    def __init__(self, host, *, stream, generation, timeout=20, ssh_config=None):
        if not host or host.startswith('-') or any(c.isspace() for c in host):
            raise ReplicationError("an SSH host alias is required")
        self.host, self.stream, self.generation = host, stream, generation
        self.timeout, self.ssh_config = timeout, ssh_config

    def __call__(self, payload):
        batch = ReplicationBatch.parse(payload)
        if (batch.stream, batch.generation) != (self.stream, self.generation):
            raise ReplicationError("transport scope does not match the batch")
        encoded = _canonical(payload)
        if len(encoded) > MAX_BATCH_BYTES:
            raise ReplicationError("replication batch exceeds the transport budget")
        command = ['ssh']
        if self.ssh_config is not None:
            command.extend(['-F', str(self.ssh_config)])
        command.extend(['-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
                        '-o', 'ConnectTimeout=10', '-T', self.host, 'receive-ledger-batch'])
        result = subprocess.run(command, input=encoded, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, timeout=self.timeout, check=True)
        if len(result.stdout) > 4096:
            raise ReplicationError("receiver ACK exceeds the transport budget")
        try:
            ack = ReplicationCursor(**json.loads(result.stdout))
        except (ValueError, TypeError):
            raise ReplicationError("receiver returned an invalid durable ACK") from None
        if ack != ReplicationCursor(self.stream, self.generation, batch.sequence, payload['digest']):
            raise ReplicationError("receiver ACK does not identify the transmitted batch")
        return ack


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    receive = commands.add_parser('receive', help='accept one bounded batch from stdin and return its durable ACK')
    receive.add_argument('--directory', type=Path, required=True)
    receive.add_argument('--stream', required=True)
    receive.add_argument('--generation', required=True)
    args = parser.parse_args(argv)
    raw = sys.stdin.buffer.read(MAX_BATCH_BYTES + 1)
    if len(raw) > MAX_BATCH_BYTES:
        raise ReplicationError("replication input exceeds the receiving budget")
    payload = json.loads(raw)
    receiver = BatchReceiver(args.directory, stream=args.stream, generation=args.generation)
    try:
        receiver.accept(payload)
        batch = ReplicationBatch.parse(payload)
        # Return this batch's ACK, even when an old batch is retransmitted after
        # newer batches were committed. Never advertise a later cursor as its ACK.
        ack = dict(stream=batch.stream, generation=batch.generation,
                   sequence=batch.sequence, digest=payload['digest'])
        print(json.dumps(ack, sort_keys=True, separators=(',', ':')), flush=True)
    finally:
        receiver.close()


if __name__ == '__main__':
    main()
