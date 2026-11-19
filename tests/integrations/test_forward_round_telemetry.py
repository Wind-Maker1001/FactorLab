"""Offline round boundaries, bounded samples, and durable commit timing."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from factorlab.integrations.binance_futures_trading import observability
from factorlab.integrations.binance_futures_trading.observability import RequestCounts, ObservedClient
from factorlab.integrations.binance_futures_trading.demo_supervisor import DemoSupervisor
from factorlab.integrations.binance_futures_trading.demo_market_feed import DemoMarketFeed
from factorlab.integrations.binance_futures_trading.protocol import Environment, ExchangeError
from factorlab.integrations.binance.client import Response
from factorlab.trading.storage import PaperStore
from test_demo_market_feed import Transport, Consumer
from datetime import datetime, timezone, timedelta


def test_rounds_opt_in_bounded_classification_and_percentiles():
    counts = RequestCounts()
    with counts.measure('outside'):
        pass
    assert counts.rounds is None
    emitted = []
    counts.enable_rounds(max_requests=3, sink=emitted.append)
    counts.begin_round()
    for duration in (1., 2., 3., 4.):
        counts.record('session', 'GET', '/fapi/v1/time', duration, None, {})
    counts.record('session', 'GET', '/fapi/v3/account', 5., 'RATE_LIMIT', {}, status=429)
    summary = counts.end_round()
    assert summary['request_count'] == 5
    assert summary['retained_requests'] == 3
    assert summary['operations']['market_get'] == dict(count=3, total_seconds=9.,
        p50_seconds=3., p95_seconds=4., max_seconds=4.)
    assert summary['operations']['account_get']['max_seconds'] == 5.
    assert all(row['classification'] == 'market_get' for row in summary['requests'])
    assert emitted == [summary]
    for _ in range(5):
        counts.begin_round()
        counts.end_round()
    assert len(counts.rounds) == 1


def test_quote_gap_and_sink_preserves_original_failure():
    def sink(_):
        raise OSError('evidence budget')
    counts = RequestCounts()
    counts.enable_rounds(max_quote_gap_seconds=30, sink=sink)
    at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    counts.begin_round()
    counts.book_received(at, at)
    counts.book_received(at + timedelta(seconds=31), at + timedelta(seconds=31))
    summary = counts.end_round(error='KeyboardInterrupt')
    assert summary['quote_gap_exceeded']
    assert summary['sink_error'] == 'OSError'
    assert counts.snapshot()['rounds'] == [summary]
    counts.begin_round()
    with pytest.raises(OSError):
        counts.end_round()


def test_http_throttle_and_transport_rtt_excludes_build_and_parse(monkeypatch):
    now = [0.]
    monkeypatch.setattr(observability, 'perf_counter', lambda: now[0])
    class TimedTransport(Transport):
        def __call__(self, request, timeout):
            now[0] += .25
            return Response(429, {}, b'{"code":-1003}')
    from factorlab.integrations.binance_futures_trading.protocol import FuturesClient
    build = FuturesClient.build
    parse = FuturesClient._parse_response
    def slow_build(self, *args, **kwargs):
        now[0] += 10
        return build(self, *args, **kwargs)
    def slow_parse(self, *args, **kwargs):
        now[0] += 20
        return parse(self, *args, **kwargs)
    monkeypatch.setattr(FuturesClient, 'build', slow_build)
    monkeypatch.setattr(FuturesClient, '_parse_response', slow_parse)
    counts = RequestCounts()
    counts.enable_rounds()
    counts.begin_round()
    client = ObservedClient(environment=Environment.DEMO, transport=TimedTransport(), request_counts=counts)
    with pytest.raises(ExchangeError):
        client.request('GET', '/fapi/v1/time', signed=False)
    row, = counts.end_round()['requests']
    assert row['rtt_seconds'] == .25
    assert row['http_failure'] and row['throttled'] and row['http_status'] == 429


@pytest.mark.parametrize('failure', [RuntimeError, KeyboardInterrupt])
def test_supervisor_finalizes_arbitrary_interruption(failure):
    counts = RequestCounts()
    counts.enable_rounds()
    def poll():
        with counts.measure('consumer'):
            raise failure('interrupted')
    feed = SimpleNamespace(poll=poll, pause=lambda _: None)
    supervisor = DemoSupervisor(feed, request_counts=counts)
    with pytest.raises(failure):
        supervisor.run()
    summary = counts.rounds[-1]
    assert summary['interrupted'] and summary['error'] == failure.__name__
    assert summary['wait_seconds'] == 0
    assert summary['operations']['consumer']['count'] == 1


def test_supervisor_actual_wait_and_cumulative_deltas(monkeypatch):
    from factorlab.integrations.binance_futures_trading import demo_supervisor
    now = [0.]
    monkeypatch.setattr(demo_supervisor, 'monotonic', lambda: now[0])
    metric = [5.]
    counts = RequestCounts()
    counts.enable_rounds()
    def poll():
        metric[0] += .5
        return dict(bars=0, quotes=0)
    feed = SimpleNamespace(poll=poll, pause=lambda _: None, status='RUNNING', reason=None,
        interval='1m', watermarks=lambda: (None, None))
    supervisor = DemoSupervisor(feed, request_counts=counts, report=lambda _: None,
        metric_snapshot=lambda: dict(commit=metric[0]))
    def wait(delay):
        now[0] += .125
        supervisor.stop()
    supervisor.stopped.wait = wait
    supervisor.run()
    summary = counts.rounds[-1]
    assert summary['wait_seconds'] == .125
    assert summary['scheduled_wait_seconds'] == 2
    assert summary['cumulative_seconds'] == dict(commit=.5)
    assert not summary['interrupted']


def test_paper_commit_excludes_transaction_body(tmp_path, monkeypatch):
    from factorlab.trading import storage
    now = [0.]
    monkeypatch.setattr(storage.time, 'perf_counter', lambda: now[0])
    store = PaperStore(tmp_path / 'paper.sqlite')
    db = store.db
    class TimedDB:
        def execute(self, sql):
            if sql == 'COMMIT':
                now[0] += .25
            return db.execute(sql)
    store.db = TimedDB()
    with store.transaction():
        now[0] += 10
    assert store.commit_seconds == .25
    assert store.commit_count == 1
    assert store.last_transaction == 10.25
    with pytest.raises(RuntimeError):
        with store.transaction():
            raise RuntimeError('rollback')
    assert store.commit_count == 1
    db.close()


def test_failed_cursor_commit_rolls_back_acknowledgement(tmp_path):
    import sqlite3
    transport = Transport()
    client = ObservedClient(environment=Environment.DEMO, transport=transport)
    feed = DemoMarketFeed(client, Consumer(), state_path=tmp_path / 'cursor.sqlite', consumer_id='test')
    db = feed.db
    class FailedCommit:
        def execute(self, *args):
            return db.execute(*args)
        def commit(self):
            raise sqlite3.OperationalError('database is locked')
        def rollback(self):
            db.rollback()
    feed.db = FailedCommit()
    try:
        with pytest.raises(sqlite3.OperationalError, match='database is locked'):
            feed._ack('quote', 123)
        assert feed.watermarks() == (None, None)
        assert not db.in_transaction
    finally:
        db.close()


@pytest.mark.parametrize('explicit', [False, True], ids=['request-counts', 'explicit-timings'])
def test_feed_measures_callbacks_cursor_commits_and_book_receipt(tmp_path, explicit):
    counts = RequestCounts()
    counts.enable_rounds()
    transport = Transport()
    client = ObservedClient(environment=Environment.DEMO, transport=transport, request_counts=counts,
        clock=transport.clock, monotonic=transport.monotonic)
    timings = Mock(spec_set=['sample', 'measure', 'book_received'], wraps=counts) if explicit else None
    feed = DemoMarketFeed(client, Consumer(), state_path=tmp_path / 'cursor.sqlite', consumer_id='test',
        clock=lambda: datetime.fromtimestamp(transport.time / 1000, timezone.utc), timings=timings)
    before = feed.commit_count
    counts.begin_round()
    assert feed.poll() == dict(bars=1, quotes=1)
    summary = counts.end_round()
    assert feed.commit_count - before == 3
    assert summary['operations']['cursor_commit']['count'] == 3
    assert summary['operations']['closed_bar_callback']['count'] == 1
    assert summary['operations']['book_market_to_received']['max_seconds'] == .101
    assert summary['operations']['book_host_wall_to_received']['max_seconds'] == .100
    assert {row['classification'] for row in summary['requests']} == {'market_get'}
    if timings is not None:
        timings.book_received.assert_called_once()
    feed.close()
