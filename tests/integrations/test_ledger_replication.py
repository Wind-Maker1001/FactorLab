import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from factorlab.integrations.ledger_replication import (
    MAX_BATCH_BYTES, BatchReceiver, EventAdmission, MissingBatch, ReplicationBatch,
    ReplicationError, make_batch,
)


def test_batches_are_contiguous_idempotent_and_durable(tmp_path):
    receiver = BatchReceiver(tmp_path, stream='binance:account', generation='g1')
    first = make_batch('binance:account', 'g1', 1, None,
                       [{'event_id': 'capital-1', 'kind': 'CAPITAL', 'amount': '10'}],
                       '2026-11-28T00:00:00+00:00')
    second = make_batch('binance:account', 'g1', 2, first['digest'],
                        [{'event_id': 'pause-1', 'kind': 'CONTROL', 'state': 'PAUSED'}],
                        '2026-11-28T00:00:01+00:00')
    assert receiver.accept(first) == 'accepted'
    assert receiver.accept(first) == 'duplicate'
    assert receiver.accept(second) == 'accepted'
    receiver.close()
    reopened = BatchReceiver(tmp_path, stream='binance:account', generation='g1')
    assert reopened.cursor().sequence == 2
    assert reopened.accept(first) == 'duplicate'
    reopened.close()


def test_missing_and_tampered_batches_are_rejected(tmp_path):
    receiver = BatchReceiver(tmp_path, stream='alpaca:account', generation='g1')
    first = make_batch('alpaca:account', 'g1', 1, None, [], '2026-11-28T00:00:00+00:00')
    third = make_batch('alpaca:account', 'g1', 3, first['digest'], [], '2026-11-28T00:00:02+00:00')
    assert receiver.accept(first) == 'accepted'
    with pytest.raises(MissingBatch):
        receiver.accept(third)
    tampered = dict(first, events=[{'event_id': 'changed'}])
    with pytest.raises(ReplicationError):
        receiver.accept(tampered)


def test_incomplete_batch_cannot_be_published():
    with pytest.raises(ReplicationError):
        ReplicationBatch('s', 'g', 1, None, (), 'now', complete=False).envelope()


def test_source_does_not_apply_before_offsite_persistence_and_resumes_lost_ack(tmp_path):
    receiver = BatchReceiver(tmp_path / 'offsite', stream='account', generation='g1')
    db = sqlite3.connect(tmp_path / 'source.sqlite3', isolation_level=None)
    db.execute('CREATE TABLE control (event_id TEXT PRIMARY KEY, desired TEXT NOT NULL)')

    def lose_ack(payload):
        receiver.accept(payload)
        raise TimeoutError('ACK lost after offsite commit')

    admission = EventAdmission(db, stream='account', generation='g1', send=lose_ack)
    event = dict(event_id='pause-1', desired='PAUSED')
    admission.propose(event, committed_at='2026-11-28T00:00:00+00:00')

    def apply(connection, original):
        connection.execute('INSERT INTO control VALUES (?,?)',
                           (original['event_id'], original['desired']))

    with pytest.raises(TimeoutError):
        admission.resume(apply)
    assert db.execute('SELECT * FROM control').fetchall() == []
    assert receiver.cursor().sequence == 1
    assert admission.pending()['events'] == [event]
    with pytest.raises(ReplicationError):
        admission.propose(dict(event_id='run-2', desired='RUNNING'),
                          committed_at='2026-11-28T00:00:01+00:00')
    db.close()

    def send(payload):
        assert receiver.accept(payload) == 'duplicate'
        return receiver.cursor()

    db = sqlite3.connect(tmp_path / 'source.sqlite3', isolation_level=None)
    admission = EventAdmission(db, stream='account', generation='g1', send=send)
    admission.resume(apply)
    assert admission.pending() is None
    assert db.execute('SELECT * FROM control').fetchall() == [('pause-1', 'PAUSED')]
    assert admission.resume(apply) is None
    db.close()
    receiver.close()


def test_local_reducer_failure_rolls_back_domain_effects_but_preserves_remote_event(tmp_path):
    receiver = BatchReceiver(tmp_path / 'offsite', stream='account', generation='g1')

    def send(payload):
        receiver.accept(payload)
        return receiver.cursor()

    db = sqlite3.connect(tmp_path / 'source.sqlite3', isolation_level=None)
    db.execute('CREATE TABLE capital (event_id TEXT PRIMARY KEY, amount TEXT NOT NULL)')
    admission = EventAdmission(db, stream='account', generation='g1', send=send)
    event = dict(event_id='transfer-1', amount='25')
    admission.propose(event, committed_at='2026-11-28T00:00:00+00:00')

    def crash(connection, original):
        connection.execute('INSERT INTO capital VALUES (?,?)',
                           (original['event_id'], original['amount']))
        raise RuntimeError('application failure before local commit')

    with pytest.raises(RuntimeError):
        admission.resume(crash)
    assert db.execute('SELECT * FROM capital').fetchall() == []
    assert next(receiver.batches_after(0))['events'] == [event]
    assert admission.pending() is not None
    db.close()
    receiver.close()


def test_conflicting_stream_event_and_generation_are_not_accepted(tmp_path):
    receiver = BatchReceiver(tmp_path, stream='account', generation='g1')
    first = make_batch('account', 'g1', 1, None, [{'event_id': 'e1'}],
                       '2026-11-28T00:00:00+00:00')
    receiver.accept(first)
    conflict = make_batch('account', 'g1', 2, first['digest'], [{'event_id': 'e1'}],
                          '2026-11-28T00:00:01+00:00')
    with pytest.raises(ReplicationError):
        receiver.accept(conflict)
    assert receiver.cursor().sequence == 1
    other = make_batch('other', 'g1', 2, first['digest'], [], '2026-11-28T00:00:01+00:00')
    with pytest.raises(ReplicationError):
        receiver.accept(other)
    receiver.close()
    with pytest.raises(ReplicationError):
        BatchReceiver(tmp_path, stream='account', generation='g2')


def test_oversized_proposal_does_not_block_next_valid_admission(tmp_path):
    db = sqlite3.connect(tmp_path / 'source.sqlite3', isolation_level=None)
    admission = EventAdmission(db, stream='account', generation='g1', send=None)
    with pytest.raises(ReplicationError, match='budget'):
        admission.propose(dict(event_id='oversized', data='x' * MAX_BATCH_BYTES),
                          committed_at='2026-11-28T00:00:00+00:00')
    assert admission.pending() is None
    valid = admission.propose(dict(event_id='pause-1', desired='PAUSED'),
                              committed_at='2026-11-28T00:00:01+00:00')
    assert valid['sequence'] == 1
    db.close()


def test_receiver_process_publishes_ack_only_after_complete_input(tmp_path):
    directory = tmp_path / 'offsite'
    command = [sys.executable, '-m', 'factorlab.integrations.ledger_replication', 'receive',
               '--directory', str(directory), '--stream', 'account', '--generation', 'g1']
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[2] / 'src'))
    payload = make_batch('account', 'g1', 1, None, [{'event_id': 'capital-1', 'amount': '25'}],
                         '2026-11-28T00:00:00+00:00')
    encoded = json.dumps(payload).encode()
    partial = subprocess.run(command, input=encoded[:len(encoded) // 2],
                             capture_output=True, env=env, cwd=tmp_path, timeout=20)
    assert partial.returncode != 0
    assert partial.stdout == b''
    assert not directory.exists()
    complete = subprocess.run(command, input=encoded, capture_output=True,
                              env=env, cwd=tmp_path, timeout=20, check=True)
    ack = json.loads(complete.stdout)
    assert ack == dict(stream='account', generation='g1', sequence=1, digest=payload['digest'])
    receiver = BatchReceiver(directory, stream='account', generation='g1')
    assert tuple(receiver.batches_after(0)) == (payload,)
    receiver.close()
