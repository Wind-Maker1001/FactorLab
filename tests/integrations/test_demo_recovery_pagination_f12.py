"""Offline regressions for PR #226's bounded trade and income pagination."""
from types import SimpleNamespace
from decimal import Decimal

import pytest

from factorlab.errors import DataQualityError
from factorlab.integrations.binance_futures_trading.recovery import (
    MAX_QUERY_WINDOW_MS, OrderRecoveryConfig, audit_due, fetch_income_paginated,
    fetch_user_trades_paginated, load_state, save_state,
)
from factorlab.integrations.binance_futures_trading.fake import FakeTransport, MS
from test_demo_incremental_recovery_f12 import (
    MutableClock, archive_seeded_orders, make_adapter, seed_historical_order,
)


def trade(tid, time, **changes):
    return dict(symbol='NVDAUSDT', id=tid, orderId=tid, qty='1', quoteQty='100',
                commission='0.1', commissionAsset='USDT', time=time) | changes


def income(tid, time=10, **changes):
    return dict(symbol='NVDAUSDT', incomeType='FUNDING_FEE', income='-0.02',
                asset='USDT', time=time, tranId=tid) | changes


class HistoryClient:
    def __init__(self, rows=(), *, scripted=None):
        self.rows = list(rows)
        self.scripted = iter(scripted) if scripted is not None else None
        self.calls = []

    def request(self, method, path, params):
        assert method == 'GET'
        assert not ('fromId' in params and {'startTime', 'endTime'} & params.keys())
        self.calls.append((path, dict(params)))
        if self.scripted is not None:
            result = next(self.scripted)
            if isinstance(result, Exception):
                raise result
            return result
        rows = self.rows
        if 'startTime' in params:
            rows = [r for r in rows if r['time'] >= params['startTime']]
        if 'endTime' in params:
            rows = [r for r in rows if r['time'] <= params['endTime']]
        if 'fromId' in params:
            rows = [r for r in rows if r['id'] >= params['fromId']]
        offset = (params.get('page', 1) - 1) * params['limit']
        return rows[offset:offset + params['limit']]


def fetch_trades(client, **kwargs):
    return fetch_user_trades_paginated(SimpleNamespace(client=client), 'NVDAUSDT', **kwargs)


def fetch_income(client, **kwargs):
    return fetch_income_paginated(SimpleNamespace(client=client), 'NVDAUSDT',
                                  income_type='FUNDING_FEE', **kwargs)


def test_trade_time_boundary_minimal_repro():
    client = HistoryClient([trade(1, 10), trade(2, 20), trade(3, 30)])
    rows, pages = fetch_trades(client, start_time=10, end_time=20, limit=2)
    assert [r['time'] for r in rows] == [10, 20]
    assert pages == len(client.calls) == 2


def test_income_same_millisecond_minimal_repro():
    client = HistoryClient([income(i) for i in range(1, 4)])
    rows, pages = fetch_income(client, start_time=10, end_time=10, limit=2)
    assert [r['tranId'] for r in rows] == [1, 2, 3]
    assert pages == len(client.calls) == 3


def test_trade_adjacent_chunks_share_no_rows_and_have_one_global_page_budget():
    boundary = 10 + MAX_QUERY_WINDOW_MS
    client = HistoryClient([trade(i, t) for i, t in enumerate(
        [10, boundary, boundary + 1, boundary + 2, boundary + 3], 1)]
        + [trade(i, boundary + i) for i in range(6, 1006)])
    rows, pages = fetch_trades(client, start_time=10, end_time=boundary + 2,
                              limit=2, max_pages=4)
    assert [r['id'] for r in rows] == [1, 2, 3, 4]
    assert pages == 4
    assert client.calls[2][1]['startTime'] == boundary + 1
    assert 'fromId' in client.calls[1][1]
    with pytest.raises(DataQualityError, match='page cap'):
        fetch_trades(HistoryClient(client.rows), start_time=10, end_time=boundary + 2,
                     limit=2, max_pages=3)


def test_trade_large_future_history_is_bounded_and_from_id_has_no_time_ceiling():
    client = HistoryClient([trade(i, i * 10) for i in range(1, 10001)])
    rows, pages = fetch_trades(client, start_time=10, end_time=20, limit=2, max_pages=2)
    assert [r['id'] for r in rows] == [1, 2]
    assert pages == 2
    rows, pages = fetch_trades(HistoryClient(client.rows[:7]), from_id=1, limit=2, max_pages=4)
    assert [r['id'] for r in rows] == list(range(1, 8))
    assert pages == 4


def test_trade_local_lower_bound_and_identical_cross_page_overlap():
    client = HistoryClient(scripted=[[trade(1, 5), trade(2, 10)],
                                    [trade(2, 10), trade(3, 20)], [trade(4, 30)]])
    rows, pages = fetch_trades(client, start_time=10, end_time=20, limit=2)
    assert [r['id'] for r in rows] == [2, 3]
    assert pages == 3


@pytest.mark.parametrize('bad_page,match', [
    ([trade(3, 30), trade(4, 20)], 'monotonic'),  # validate after first out-of-range row
    ([trade(3, 15)], 'monotonic'),  # time reverses across pages, including short pages
    ([trade(4, 30), trade(3, 40)], 'IDs are not ordered'),
    ([trade(3, 30), trade(3, 30)], 'duplicate actual trades'),
    ([trade(2, 20, commission='0.2'), trade(3, 30)], 'conflicting duplicate'),
    ([trade(2, 21), trade(3, 30)], 'conflicting duplicate'),
    ([trade(1, 10), trade(2, 20)], 'did not advance'),
    ([trade(3, None)], 'missing trade timestamp'),
])
def test_trade_invalid_history_fails_closed_before_boundary_stop(bad_page, match):
    client = HistoryClient(scripted=[[trade(1, 10), trade(2, 20)], bad_page])
    with pytest.raises(DataQualityError, match=match):
        fetch_trades(client, start_time=10, end_time=20, limit=2, max_pages=4)
    assert len(client.calls) == 2


def test_trade_full_valid_pages_still_require_proof_of_completion():
    with pytest.raises(DataQualityError, match='page cap'):
        fetch_trades(HistoryClient([trade(i, 10) for i in range(6)]),
                     start_time=10, end_time=10, limit=2, max_pages=2)


def test_trade_equal_end_time_does_not_truncate_same_millisecond_fills():
    rows, pages = fetch_trades(HistoryClient([trade(i, 10) for i in range(7)]),
                              start_time=10, end_time=10, limit=2, max_pages=4)
    assert [r['id'] for r in rows] == list(range(7))
    assert pages == 4


@pytest.mark.parametrize('bounds,expected', [(dict(start_time=20), [2, 3]),
                                           (dict(end_time=20), [1, 2])])
def test_trade_one_sided_requested_bounds(bounds, expected):
    rows, _ = fetch_trades(HistoryClient([trade(i, i * 10) for i in range(1, 4)]),
                          limit=2, max_pages=3, **bounds)
    assert [r['id'] for r in rows] == expected


@pytest.mark.parametrize('times', [[10] * 7, [10, 10, 11, 11, 11, 12, 12],
                                  [9, 10, 10, 10, 11, 12, 12, 13]])
def test_income_many_boundary_pages_and_time_cursor_resets(times):
    client = HistoryClient([income(i, t) for i, t in enumerate(times, 1)])
    rows, pages = fetch_income(client, start_time=10, end_time=12, limit=2, max_pages=12)
    assert [(r['tranId'], r['time']) for r in rows] == [
        (i, t) for i, t in enumerate(times, 1) if 10 <= t <= 12]
    assert pages <= 10
    for _, p in client.calls:
        assert p['endTime'] == 12
    for (_, prev), (_, curr) in zip(client.calls, client.calls[1:]):
        if curr['startTime'] > prev['startTime']:
            assert curr.get('page', 1) == 1


def test_income_identical_identity_overlap_is_idempotent():
    client = HistoryClient(scripted=[[income(1), income(2)], [income(1), income(2)],
                                    [income(2), income(3)], []])
    rows, pages = fetch_income(client, start_time=10, end_time=10, limit=2)
    assert [r['tranId'] for r in rows] == [1, 2, 3]
    assert [p.get('page', 1) for _, p in client.calls] == [1, 1, 2, 3]
    assert pages == 4


@pytest.mark.parametrize('changes', [dict(income='-9'), dict(asset='BNB'), dict(time=11)])
def test_income_conflicting_identity_fails_closed(changes):
    client = HistoryClient(scripted=[[income(1), income(2)], [income(2, **changes)]])
    with pytest.raises(DataQualityError, match='conflicting duplicate'):
        fetch_income(client, start_time=10, end_time=20, limit=2)


@pytest.mark.parametrize('page,match', [
    ([income(1), income(1)], 'within single page'),
    ([income(1, 9)], 'outside requested window'),
    ([income(1, 21)], 'outside requested window'),
    ([income(1, 12), income(2, 11)], 'monotonic'),
])
def test_income_rejects_invalid_boundary_pages(page, match):
    with pytest.raises(DataQualityError, match=match):
        fetch_income(HistoryClient(scripted=[page]), start_time=10, end_time=20, limit=2)


@pytest.mark.parametrize('max_pages,match,expected_calls', [(2, 'page cap', 2),
                                                          (10, 'did not advance', 3)])
def test_income_repeated_server_page_has_bounded_rejection(max_pages, match, expected_calls):
    client = HistoryClient(scripted=[[income(1), income(2)]] * 10)
    with pytest.raises(DataQualityError, match=match):
        fetch_income(client, start_time=10, limit=2, max_pages=max_pages)
    assert len(client.calls) == expected_calls


@pytest.mark.parametrize('failure', ['network', 'stall', 'cap'])
def test_income_partial_failure_preserves_authoritative_watermark_and_events(tmp_path, failure):
    clock = MutableClock()
    fake = FakeTransport()
    fake.income = [income(1, MS)]
    adapter = make_adapter(tmp_path / 'protocol.sqlite', fake, clock)
    try:
        adapter.sync_funding()
        old_state = adapter.funding_coverage()
        repeated = [income(1, MS), income(2, MS)]
        script = [repeated, OSError('offline interruption')] if failure == 'network' else [repeated] * 10
        client = HistoryClient(scripted=script)
        adapter.client.request = client.request
        with pytest.raises((OSError, DataQualityError)):
            adapter.sync_funding(page_limit=2, max_pages=2 if failure == 'cap' else 10)
        assert adapter.funding_coverage() == old_state
        assert [event.tran_id for event in adapter.funding_events()] == ['1']
    finally:
        adapter.store.close()
    reopened = make_adapter(tmp_path / 'protocol.sqlite', fake, clock)
    try:
        assert reopened.funding_coverage() == old_state
        fake.income = [income(i, MS) for i in range(1, 8)]
        result = reopened.sync_funding(page_limit=2)
        assert len(result.events) == 7
        assert result.covered_through_ms == MS
        assert [event.tran_id for event in result.newly_recorded] == [str(i) for i in range(2, 8)]
        assert not reopened.sync_funding(page_limit=2).newly_recorded
        assert sum(event.amount for event in result.events) == Decimal('-0.14')
        assert reopened.recovery_status()['watermarks']['income:*:FUNDING_FEE'] == reopened.funding_coverage()
        assert all(method == 'GET' for method, _, _ in fake.calls)
    finally:
        reopened.store.close()


@pytest.mark.parametrize('initial_audit', [False, True])
def test_default_history_audit_30_rounds_or_300_seconds(tmp_path, initial_audit):
    clock = MutableClock()
    fake = FakeTransport()
    config = OrderRecoveryConfig()
    assert config.history_audit_interval_rounds == 30
    assert config.history_audit_interval_seconds == 300
    adapter = make_adapter(tmp_path / 'protocol.sqlite', fake, clock)
    try:
        seed_historical_order(adapter, fake, key='archived', order_id=1)
        archive_seeded_orders(adapter, clock)
        state = load_state(adapter.db, 'global')
        state.update(rounds_since_audit=29,
                     last_history_audit_at=None if initial_audit else clock.utc().isoformat(),
                     first_archive_at=clock.utc().isoformat())
        with adapter.store.transaction():
            save_state(adapter.db, 'global', state, clock.utc().isoformat())
        clock.advance(299)
        assert audit_due(adapter.db, now=clock.utc(), config=config) == (False, None, False)
        state['rounds_since_audit'] = 30
        with adapter.store.transaction():
            save_state(adapter.db, 'global', state, clock.utc().isoformat())
        assert audit_due(adapter.db, now=clock.utc(), config=config) == (True, 'INTERVAL_ROUNDS', False)
        clock.advance(1)
        expected_reason = 'INITIAL_AUDIT_INTERVAL' if initial_audit else 'INTERVAL_SECONDS'
        assert audit_due(adapter.db, now=clock.utc(), config=config) == (True, expected_reason, False)
    finally:
        adapter.store.close()
