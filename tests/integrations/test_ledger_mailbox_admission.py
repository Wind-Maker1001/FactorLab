from contextlib import closing
from dataclasses import replace
import sqlite3

import pytest

from factorlab.integrations.ledger_replication import BatchReceiver, EventAdmission, ReplicationCursor
from factorlab.integrations.ledger_replay import AccountEventWriter, AccountReplay, DomainReplay, SnapshotPosition
from factorlab.integrations.binance_futures_trading.tradfi_tasks import TaskMailbox, reduce_task_mailbox
from test_tradfi_tasks import task


BASE = SnapshotPosition('account', 'g1', 0, None)


def test_task_and_receipt_lost_ack_restore_original_sequence_without_source(tmp_path):
    receiver = BatchReceiver(tmp_path / 'offsite', stream='account', generation='g1')
    lose_ack = True

    def send(payload):
        receiver.accept(payload)
        if lose_ack:
            raise TimeoutError('offsite committed; ACK lost')
        return ReplicationCursor('account', 'g1', payload['sequence'], payload['digest'])

    path = tmp_path / 'source.sqlite3'
    with closing(sqlite3.connect(tmp_path / 'admission.sqlite3', isolation_level=None)) as journal:
        def bind(db):
            return AccountEventWriter(EventAdmission(journal, stream='account', generation='g1', send=send),
                AccountReplay({'mailbox': DomainReplay(db, reduce_task_mailbox, baseline=BASE)}, baseline=BASE))

        mailbox = TaskMailbox(path, event_writer_factory=bind)
        with closing(sqlite3.connect(tmp_path / 'snapshot.sqlite3')) as target:
            mailbox.db.backup(target)
        with pytest.raises(TimeoutError):
            mailbox.submit(task())
        assert mailbox.outstanding() == []
        mailbox.close()
        lose_ack = False
        mailbox = TaskMailbox(path, event_writer_factory=bind)
        assert mailbox.submit(task()) == 'QUEUED'
        assert receiver.cursor().sequence == 1
        with pytest.raises(ValueError, match='immutable'):
            mailbox.submit(replace(task(), session='different'))
        with pytest.raises(ValueError, match='unknown task'):
            mailbox.receipt('missing', recorded_at='2026-11-30T14:30:02Z', status='ACCEPTED', detail={})
        assert receiver.cursor().sequence == 1
        lose_ack = True
        with pytest.raises(TimeoutError):
            mailbox.receipt(task().task_id, recorded_at='2026-11-30T14:30:02Z', status='ACCEPTED',
                            detail={'decision': 'original'})
        assert mailbox.status(task().task_id) == 'QUEUED'
        mailbox.close()
        lose_ack = False
        mailbox = TaskMailbox(path, event_writer_factory=bind)
        assert mailbox.status(task().task_id) == 'ACCEPTED'
        mailbox.receipt(task().task_id, recorded_at='2026-11-30T14:30:02Z', status='ACCEPTED',
                        detail={'decision': 'original'})
        assert receiver.cursor().sequence == 2
        mailbox.receipt(task().task_id, recorded_at='2026-11-30T14:30:03Z', status='COMPLETED',
                        detail={'plan': 'original'})
        mailbox.receipt(task().task_id, recorded_at='2026-11-30T14:30:04Z', status='COMPLETED',
                        detail={'plan': 'original'})
        assert receiver.cursor().sequence == 3
        expected = mailbox.snapshot()
        mailbox.close()
    path.unlink()
    with closing(TaskMailbox(tmp_path / 'snapshot.sqlite3')) as restored:
        replay = AccountReplay({'mailbox': DomainReplay(restored.db, reduce_task_mailbox, baseline=BASE)},
                               baseline=BASE)
        assert replay.catch_up(receiver).sequence == 3
        assert restored.snapshot() == expected
        assert restored.outstanding() == []
        with pytest.raises(PermissionError, match='account event writer'):
            restored.submit(replace(task(), task_id='unadmitted'))
    receiver.close()
