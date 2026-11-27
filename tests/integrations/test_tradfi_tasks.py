from dataclasses import replace
from decimal import Decimal as D

import pytest

from factorlab.integrations.binance_futures_trading.tradfi_tasks import PortfolioTask, TaskMailbox, TEST_ALLOCATION


def task():
    return PortfolioTask('test:2026-11-30:v1', TEST_ALLOCATION, '2026-11-30',
                         '2026-11-30T14:30:01Z', '2026-11-30T14:31:01Z',
                         D('20'), {'QQQUSDT': D('.4'), 'SPYUSDT': D('.4')})


def test_restart_and_two_producers_publish_one_immutable_task(tmp_path):
    path = tmp_path / 'tasks.sqlite3'
    first, second = TaskMailbox(path), TaskMailbox(path)
    assert first.submit(task()) == 'QUEUED'
    assert second.submit(task()) == 'QUEUED'
    first.close()
    assert len(second.outstanding()) == 1
    with pytest.raises(ValueError, match='immutable'):
        second.submit(replace(task(), equity_usdt=D('21')))
    assert second.outstanding()[0]['task']['equity_usdt'] == '20'
    second.close()


def test_receipt_recovery_does_not_hide_expired_inflight_task(tmp_path):
    path = tmp_path / 'tasks.sqlite3'
    mailbox = TaskMailbox(path)
    mailbox.submit(task())
    mailbox.receipt(task().task_id, recorded_at='2026-11-30T14:30:02Z', status='ACCEPTED', detail={})
    mailbox.receipt(task().task_id, recorded_at='2026-11-30T14:32:00Z', status='RECONCILING', detail={'journal': 'batch:1'})
    mailbox.close()
    recovered = TaskMailbox(path)
    assert recovered.outstanding()[0]['status'] == 'RECONCILING'
    recovered.receipt(task().task_id, recorded_at='2026-11-30T14:33:00Z', status='COMPLETED', detail={'journal': 'batch:1'})
    assert recovered.outstanding() == []
    assert recovered.submit(task()) == 'COMPLETED'
    recovered.receipt(task().task_id, recorded_at='2026-11-30T14:34:00Z', status='COMPLETED', detail={'journal': 'batch:1'})
    with pytest.raises(ValueError, match='immutable'):
        recovered.receipt(task().task_id, recorded_at='2026-11-30T14:35:00Z', status='RUNNING', detail={})
    assert recovered.snapshot()[0]['status'] == 'COMPLETED'
    recovered.close()


def test_dynamic_symbols_survive_mailbox_restart(tmp_path):
    path = tmp_path / 'dynamic.sqlite3'
    offered = replace(task(), allocation_id='configured-multi-symbol',
                      weights={'BTCUSDT': D('.3'), 'ETHUSDT': D('.4')})
    mailbox = TaskMailbox(path)
    assert mailbox.submit(offered) == 'QUEUED'
    mailbox.close()
    recovered = TaskMailbox(path)
    assert recovered.outstanding() == [dict(task=offered.payload(), status='QUEUED')]
    assert recovered.submit(offered) == 'QUEUED'
    recovered.close()


@pytest.mark.parametrize('changes', [
    {'allocation_id': ''},
    {'weights': {'NVDAUSDT': D('.1'), 'QQQUSDT': D('.2')}},
    {'weights': {'QQQUSDT': D('.6'), 'SPYUSDT': D('.6')}},
    {'equity_usdt': D('NaN')},
    {'valid_until': '2026-11-30T14:30:00Z'},
])
def test_invalid_allocation_never_enters_mailbox(tmp_path, changes):
    mailbox = TaskMailbox(tmp_path / 'tasks.sqlite3')
    with pytest.raises(ValueError):
        mailbox.submit(replace(task(), **changes))
    assert mailbox.outstanding() == []
    mailbox.close()

def test_mailbox_preserves_nanosecond_payload_and_receipt_identity(tmp_path):
    offered = replace(task(), observed_at='2026-11-30T09:30:01.123456900-05:00',
                      valid_until='2026-11-30T14:30:01.123456901Z')
    payload = offered.payload()
    assert payload['observed_at'] == '2026-11-30T14:30:01.123456900+00:00'
    assert payload['valid_until'] == '2026-11-30T14:30:01.123456901+00:00'
    with pytest.raises(ValueError, match='expire after'):
        replace(offered, valid_until='2026-11-30T14:30:01.123456899Z').payload()
    with pytest.raises(ValueError, match='timezone-aware'):
        replace(offered, observed_at='2026-11-30T14:30:01.123456900').payload()

    from contextlib import closing
    path = tmp_path / 'tasks.sqlite3'
    with closing(TaskMailbox(path)) as mailbox:
        mailbox.submit(offered)
        mailbox.receipt(offered.task_id, recorded_at='2026-11-30T14:30:02.000000001Z',
                        status='COMPLETED', detail={'journal': 'batch:1'})
        original = mailbox.snapshot()
        assert original[0]['recorded_at'] == '2026-11-30T14:30:02.000000001+00:00'
    with closing(TaskMailbox(path)) as mailbox:
        assert mailbox.submit(replace(offered, observed_at=payload['observed_at'])) == 'COMPLETED'
        mailbox.receipt(offered.task_id, recorded_at='2026-11-30T14:30:03Z',
                        status='COMPLETED', detail={'journal': 'batch:1'})
        assert mailbox.snapshot() == original
