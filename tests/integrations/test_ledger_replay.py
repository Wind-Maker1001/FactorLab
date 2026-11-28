from contextlib import ExitStack, closing
import shutil
import sqlite3

import pytest

from factorlab.integrations.ledger_replication import (
    BatchReceiver, EventAdmission, ReplicationBatch, ReplicationCursor, ReplicationError, make_batch,
)
from factorlab.integrations.ledger_replay import (
    AccountEventWriter, AccountReplay, DomainReplay, SnapshotPosition, restored_account,
)
from dataclasses import asdict


BASE = SnapshotPosition('account', 'generation-1', 0, None)
AT = '2026-11-28T00:00:00+00:00'


def database(path):
    db = sqlite3.connect(path, isolation_level=None)
    db.execute('CREATE TABLE IF NOT EXISTS original_events '
               '(event_id TEXT PRIMARY KEY, economic_owner TEXT, amount TEXT)')
    return db


def reduce_event(db, event):
    db.execute('INSERT INTO original_events VALUES(?,?,?)',
               (event['event_id'], event['economic_owner'], event['amount']))


def test_account_source_keeps_pending_until_all_domains_apply(tmp_path):
    receiver = BatchReceiver(tmp_path / 'offsite', stream=BASE.stream, generation=BASE.generation)

    def send(payload):
        receiver.accept(payload)
        return ReplicationCursor(BASE.stream, BASE.generation, payload['sequence'], payload['digest'])

    with closing(database(tmp_path / 'a.sqlite3')) as a, closing(database(tmp_path / 'b.sqlite3')) as b, closing(
            sqlite3.connect(tmp_path / 'admission.sqlite3', isolation_level=None)) as journal:
        def crash(db, event):
            reduce_event(db, event)
            raise RuntimeError('second domain interrupted')

        admission = EventAdmission(journal, stream=BASE.stream, generation=BASE.generation, send=send)
        replay = AccountReplay({'a': DomainReplay(a, reduce_event, baseline=BASE),
                                'b': DomainReplay(b, crash, baseline=BASE)}, baseline=BASE)
        writer = AccountEventWriter(admission, replay)
        event = dict(event_id='e1', targets=['a', 'b'], economic_owner='original', amount='25')
        with pytest.raises(ReplicationError, match='targets'):
            writer.submit(dict(event, targets=['unknown']), committed_at=AT)
        assert admission.pending() is None
        assert receiver.cursor() is None
        with pytest.raises(RuntimeError):
            writer.submit(event, committed_at=AT)
        assert admission.pending()['events'] == [event]
        with pytest.raises(ReplicationError, match='pending'):
            writer.snapshot_position()
        assert a.execute('SELECT count(*) FROM original_events').fetchone() == (1,)
        assert b.execute('SELECT count(*) FROM original_events').fetchone() == (0,)
        with pytest.raises(ReplicationError, match='pending'):
            writer.submit(dict(event, event_id='e2'), committed_at=AT)
        writer = AccountEventWriter(admission, AccountReplay(
            {'a': DomainReplay(a, reduce_event, baseline=BASE),
             'b': DomainReplay(b, reduce_event, baseline=BASE)}, baseline=BASE))
        assert writer.resume().sequence == 1
        assert writer.snapshot_position().sequence == 1
        capture = writer.snapshot_capture(tmp_path)
        metadata = capture([tmp_path / 'a.sqlite3', tmp_path / 'b.sqlite3',
                            tmp_path / 'admission.sqlite3'])
        assert metadata['position']['sequence'] == 1
        assert metadata['domains'] == {'a': 'a.sqlite3', 'b': 'b.sqlite3'}
        assert metadata['admission_database'] == 'admission.sqlite3'
        with pytest.raises(ReplicationError, match='absent'):
            capture([tmp_path / 'a.sqlite3', tmp_path / 'b.sqlite3'])
        with pytest.raises(ReplicationError, match='distinct'):
            AccountReplay({'a': writer.replay.domains['a'], 'alias': writer.replay.domains['a']}, baseline=BASE)
        assert admission.pending() is None
        assert b.execute('SELECT count(*) FROM original_events').fetchone() == (1,)
        admission.propose(dict(event, event_id='e2'), committed_at=AT)
        assert writer.submit(event, committed_at=AT).sequence == 1
        assert admission.pending()['sequence'] == 2
        assert receiver.cursor().sequence == 1
    receiver.close()


def test_restore_requires_exact_reducer_registry_and_verified_database_inventory(tmp_path):
    with closing(database(tmp_path / 'capital.sqlite3')) as db:
        DomainReplay(db, reduce_event, baseline=BASE)
    with closing(sqlite3.connect(tmp_path / 'admission.sqlite3')):
        pass
    metadata = dict(version=1, position=asdict(BASE), domains={'capital': 'capital.sqlite3'},
                    admission_database='admission.sqlite3')
    manifest = dict(replication=metadata, files={'capital.sqlite3': 'verified', 'admission.sqlite3': 'verified'})
    with restored_account(tmp_path, manifest, {'capital': reduce_event}) as replay:
        assert replay.complete_at(BASE)
    with pytest.raises(ReplicationError, match='registry'):
        with restored_account(tmp_path, manifest, {}):
            pass
    for relative in ('../outside.sqlite3', 'missing.sqlite3', 'capital.sqlite3'):
        changed = dict(manifest, replication=dict(metadata, admission_database=relative))
        with pytest.raises(ReplicationError):
            with restored_account(tmp_path, changed, {'capital': reduce_event}):
                pass
    assert not (tmp_path / 'missing.sqlite3').exists()


def test_source_loss_replays_only_offsite_events_into_base_snapshot(tmp_path):
    source = tmp_path / 'source'
    source.mkdir()
    snapshot = tmp_path / 'snapshot'
    snapshot.mkdir()
    domains = ('capital', 'controller', 'deployment', 'mailbox')
    for name in domains:
        with closing(database(source / (name + '.sqlite3'))) as db:
            DomainReplay(db, reduce_event, baseline=BASE)
            with closing(sqlite3.connect(snapshot / (name + '.sqlite3'))) as target:
                db.backup(target)
    receiver = BatchReceiver(tmp_path / 'offsite', stream=BASE.stream, generation=BASE.generation)
    event = dict(event_id='transfer-1', targets=list(domains), economic_owner='strategy-original', amount='25')
    batch = make_batch(BASE.stream, BASE.generation, 1, None, [event], AT)
    receiver.accept(batch)
    shutil.rmtree(source)
    restored = tmp_path / 'restored'
    shutil.copytree(snapshot, restored)
    with ExitStack() as stack:
        stores = {name: stack.enter_context(closing(sqlite3.connect(
            restored / (name + '.sqlite3'), isolation_level=None))) for name in domains}
        replay = AccountReplay({name: DomainReplay(db, reduce_event, baseline=BASE)
                                for name, db in stores.items()}, baseline=BASE)
        position = replay.catch_up(receiver)
        assert replay.complete_at(position)
        replay.apply(batch)
        for db in stores.values():
            assert db.execute('SELECT * FROM original_events').fetchall() == [
                ('transfer-1', 'strategy-original', '25')]
    receiver.close()


def test_cross_database_failure_resumes_without_repeating_first_posting(tmp_path):
    with closing(database(tmp_path / 'capital.sqlite3')) as capital, closing(
            database(tmp_path / 'deployment.sqlite3')) as deployment:
        def fail_after_mutation(db, event):
            reduce_event(db, event)
            raise RuntimeError('process interrupted before second domain commit')

        first = DomainReplay(capital, reduce_event, baseline=BASE)
        second = DomainReplay(deployment, fail_after_mutation, baseline=BASE)
        replay = AccountReplay({'capital': first, 'deployment': second}, baseline=BASE)
        batch = make_batch(BASE.stream, BASE.generation, 1, None,
                           [dict(event_id='pause-1', targets=['capital', 'deployment'],
                                 economic_owner='strategy-original', amount='0')], AT)
        end = SnapshotPosition(BASE.stream, BASE.generation, 1, batch['digest'])
        with pytest.raises(RuntimeError):
            replay.apply(batch)
        assert not replay.complete_at(end)
        assert capital.execute('SELECT count(*) FROM original_events').fetchone() == (1,)
        assert deployment.execute('SELECT count(*) FROM original_events').fetchone() == (0,)
        resumed = AccountReplay({'capital': DomainReplay(capital, reduce_event, baseline=BASE),
                                 'deployment': DomainReplay(deployment, reduce_event, baseline=BASE)},
                                baseline=BASE)
        resumed.apply(batch)
        assert resumed.complete_at(end)
        assert capital.execute('SELECT count(*) FROM original_events').fetchone() == (1,)
        assert deployment.execute('SELECT count(*) FROM original_events').fetchone() == (1,)


def test_unknown_domain_and_wrong_snapshot_cut_fail_before_domain_writes(tmp_path):
    with closing(database(tmp_path / 'capital.sqlite3')) as db:
        domain = DomainReplay(db, reduce_event, baseline=BASE)
        replay = AccountReplay({'capital': domain}, baseline=BASE)
        batch = make_batch(BASE.stream, BASE.generation, 1, None,
                           [dict(event_id='e1', targets=['omitted-store'],
                                 economic_owner='strategy-original', amount='25')], AT)
        with pytest.raises(ReplicationError, match='targets'):
            replay.apply(batch)
        assert db.execute('SELECT count(*) FROM original_events').fetchone() == (0,)
        with pytest.raises(ReplicationError, match='connection point'):
            DomainReplay(db, reduce_event, baseline=SnapshotPosition(
                BASE.stream, BASE.generation, 1, batch['digest']))


def test_later_snapshot_rebases_only_at_its_recorded_cut(tmp_path):
    receiver = BatchReceiver(tmp_path / 'offsite', stream=BASE.stream, generation=BASE.generation)
    one = make_batch(BASE.stream, BASE.generation, 1, None,
                     [dict(event_id='e1', targets=['capital'], economic_owner='original', amount='1')], AT)
    two = make_batch(BASE.stream, BASE.generation, 2, one['digest'],
                     [dict(event_id='e2', targets=['capital'], economic_owner='original', amount='2')], AT)
    receiver.accept(one)
    with closing(database(tmp_path / 'source.sqlite3')) as source:
        domain = DomainReplay(source, reduce_event, baseline=BASE)
        domain.apply(ReplicationBatch.parse(one), one['digest'], one['events'])
        with closing(sqlite3.connect(tmp_path / 'snapshot.sqlite3')) as target:
            source.backup(target)
    cut = SnapshotPosition(BASE.stream, BASE.generation, 1, one['digest'])
    receiver.accept(two)
    with closing(sqlite3.connect(tmp_path / 'snapshot.sqlite3', isolation_level=None)) as restored:
        with pytest.raises(ReplicationError, match='connection point'):
            DomainReplay(restored, reduce_event, baseline=BASE, restore_snapshot=True)
        with pytest.raises(ReplicationError, match='connection point'):
            DomainReplay(restored, reduce_event, baseline=SnapshotPosition(
                BASE.stream, BASE.generation, 2, two['digest']), restore_snapshot=True)
        domain = DomainReplay(restored, reduce_event, baseline=cut, restore_snapshot=True)
        replay = AccountReplay({'capital': domain}, baseline=cut)
        assert replay.catch_up(receiver).sequence == 2
        assert restored.execute('SELECT event_id FROM original_events ORDER BY event_id').fetchall() == [
            ('e1',), ('e2',)]
    with closing(database(tmp_path / 'uncovered.sqlite3')) as uncovered:
        with pytest.raises(ReplicationError, match='no recorded'):
            DomainReplay(uncovered, reduce_event, baseline=cut, restore_snapshot=True)
    receiver.close()


def test_snapshot_connection_is_checked_even_without_a_suffix(tmp_path):
    receiver = BatchReceiver(tmp_path / 'offsite', stream=BASE.stream, generation=BASE.generation)
    batch = make_batch(BASE.stream, BASE.generation, 1, None, [], AT)
    receiver.accept(batch)
    wrong = SnapshotPosition(BASE.stream, BASE.generation, 1, 'a' * 64)
    with closing(database(tmp_path / 'capital.sqlite3')) as db:
        replay = AccountReplay({'capital': DomainReplay(db, reduce_event, baseline=wrong)}, baseline=wrong)
        with pytest.raises(ReplicationError, match='connection digest'):
            replay.catch_up(receiver)
    receiver.close()


def test_replay_pins_cut_before_new_batch_arrives(tmp_path):
    receiver = BatchReceiver(tmp_path / 'offsite', stream=BASE.stream, generation=BASE.generation)
    one = make_batch(BASE.stream, BASE.generation, 1, None,
                     [dict(event_id='e1', targets=['capital'], economic_owner='original', amount='1')], AT)
    two = make_batch(BASE.stream, BASE.generation, 2, one['digest'],
                     [dict(event_id='e2', targets=['capital'], economic_owner='original', amount='2')], AT)
    receiver.accept(one)

    def accept_during_replay(db, event):
        reduce_event(db, event)
        receiver.accept(two)

    with closing(database(tmp_path / 'capital.sqlite3')) as db:
        replay = AccountReplay({'capital': DomainReplay(db, accept_during_replay, baseline=BASE)}, baseline=BASE)
        end = replay.catch_up(receiver)
        assert end.sequence == 1
        assert receiver.cursor().sequence == 2
        assert db.execute('SELECT event_id FROM original_events').fetchall() == [('e1',)]
        assert replay.catch_up(receiver).sequence == 2
        assert db.execute('SELECT count(*) FROM original_events').fetchone() == (2,)
    receiver.close()


def test_one_domain_missing_predecessor_cannot_advance_any_other_domain(tmp_path):
    with closing(database(tmp_path / 'a.sqlite3')) as a, closing(database(tmp_path / 'b.sqlite3')) as b:
        domains = {'a': DomainReplay(a, reduce_event, baseline=BASE),
                   'b': DomainReplay(b, reduce_event, baseline=BASE)}
        one = make_batch(BASE.stream, BASE.generation, 1, None,
                         [dict(event_id='e1', targets=['a'], economic_owner='original', amount='1')], AT)
        two = make_batch(BASE.stream, BASE.generation, 2, one['digest'],
                         [dict(event_id='e2', targets=['a'], economic_owner='original', amount='2')], AT)
        domains['a'].apply(ReplicationBatch.parse(one), one['digest'], one['events'])
        replay = AccountReplay(domains, baseline=BASE)
        with pytest.raises(ReplicationError, match='predecessor'):
            replay.apply(two)
        assert domains['a'].position().sequence == 1
        assert domains['b'].position().sequence == 0
