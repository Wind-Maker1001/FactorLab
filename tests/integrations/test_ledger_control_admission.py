from contextlib import closing
import sqlite3

import pytest

from factorlab.integrations.ledger_replication import (
    BatchReceiver, EventAdmission, ReplicationCursor, ReplicationError,
)
from factorlab.integrations.ledger_replay import (
    AccountEventWriter, AccountReplay, DomainReplay, SnapshotPosition,
)
from factorlab.integrations.binance_futures_trading.demo_strategy_supervisor import (
    StrategyControl, reduce_strategy_control,
)
from factorlab.integrations.binance_futures_trading.protocol import SafetyBlocked


BASE = SnapshotPosition('account', 'g1', 0, None)


def factory(admission_db, send, *, fail=None):
    def bind(control_db):
        def reduce(db, event):
            reduce_strategy_control(db, event)
            if fail is not None:
                fail(event)
        replay = AccountReplay({'strategy-control': DomainReplay(control_db, reduce, baseline=BASE)},
                               baseline=BASE)
        admission = EventAdmission(admission_db, stream=BASE.stream, generation=BASE.generation, send=send)
        return AccountEventWriter(admission, replay)
    return bind


def test_control_ack_loss_restarts_with_original_pause_and_replays_offsite(tmp_path):
    receiver = BatchReceiver(tmp_path / 'offsite', stream=BASE.stream, generation=BASE.generation)
    lose_ack = False

    def send(payload):
        receiver.accept(payload)
        if lose_ack:
            raise TimeoutError('offsite persisted; ACK lost')
        return ReplicationCursor(BASE.stream, BASE.generation, payload['sequence'], payload['digest'])

    with closing(sqlite3.connect(tmp_path / 'admission.sqlite3', isolation_level=None)) as admission_db:
        control = StrategyControl(tmp_path / 'source.sqlite3', {'strategy': 'original'},
                                  event_writer_factory=factory(admission_db, send))
        assert control.desired() == 'RUNNING'
        lose_ack = True
        with pytest.raises(TimeoutError):
            control.request('PAUSED', event_id='original-pause')
        assert control.desired() == 'RUNNING'
        assert receiver.cursor().sequence == 2
        with pytest.raises(ReplicationError, match='pending'):
            control.request('RUNNING', event_id='later-resume')
        control.close()
        lose_ack = False
        control = StrategyControl(tmp_path / 'source.sqlite3',
                                  event_writer_factory=factory(admission_db, send))
        assert control.desired() == 'PAUSED'
        assert control.event_writer.admission.pending() is None
        control.request('PAUSED', event_id='original-pause')
        assert receiver.cursor().sequence == 2
        control.close()

    restored = StrategyControl(tmp_path / 'restored.sqlite3')
    replay = AccountReplay({'strategy-control': DomainReplay(
        restored.db, reduce_strategy_control, baseline=BASE)}, baseline=BASE)
    assert replay.catch_up(receiver).sequence == 2
    assert restored.snapshot()['configuration'] == {'strategy': 'original'}
    assert restored.desired() == 'PAUSED'
    with pytest.raises(SafetyBlocked, match='account event writer'):
        restored.request('RUNNING')
    assert restored.desired() == 'PAUSED'
    restored.close()
    receiver.close()


def test_control_reduction_failure_keeps_account_pending_until_restart(tmp_path):
    receiver = BatchReceiver(tmp_path / 'offsite', stream=BASE.stream, generation=BASE.generation)

    def send(payload):
        receiver.accept(payload)
        return ReplicationCursor(BASE.stream, BASE.generation, payload['sequence'], payload['digest'])

    def crash(event):
        if event['kind'] == 'STRATEGY_CONTROL_REQUEST':
            raise RuntimeError('interrupted after control mutation')

    with closing(sqlite3.connect(tmp_path / 'admission.sqlite3', isolation_level=None)) as admission_db:
        control = StrategyControl(tmp_path / 'control.sqlite3', {'strategy': 'original'},
                                  event_writer_factory=factory(admission_db, send, fail=crash))
        with pytest.raises(RuntimeError):
            control.request('STOPPED', event_id='stop-1')
        assert control.desired() == 'RUNNING'
        assert control.event_writer.admission.pending()['events'][0]['event_id'] == 'stop-1'
        control.close()
        control = StrategyControl(tmp_path / 'control.sqlite3',
                                  event_writer_factory=factory(admission_db, send))
        assert control.desired() == 'STOPPED'
        assert control.event_writer.admission.pending() is None
        control.close()
    receiver.close()
