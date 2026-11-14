from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
from urllib.parse import parse_qs

import pytest

from factorlab.integrations.binance.client import Response
from factorlab.integrations.binance_futures_trading.demo_market_feed import DemoMarketFeed, FeedPaused, SOURCE
from factorlab.integrations.binance_futures_trading.demo_supervisor import DemoSupervisor
from factorlab.integrations.binance_futures_trading.protocol import Environment, FuturesClient, OfflineTransport


class Transport(OfflineTransport):
    def __init__(self):
        self.time = 180001
        self.rows = None
        self.failed = False
        self.requests = []

    def __call__(self, request, timeout):
        self.requests.append(request)
        assert request.method == 'GET'
        assert request.environment is Environment.DEMO
        assert request.headers == {}
        if self.failed:
            raise OSError('offline')
        if request.path.endswith('/time'):
            data = {'serverTime': self.time}
        elif request.path.endswith('/klines'):
            start = int(parse_qs(request.query)['startTime'][0])
            data = self.rows if self.rows is not None else [bar(t) for t in range(start, self.time, 60000)]
        else:
            data = dict(symbol='NVDAUSDT', time=self.time, bidPrice='100.00000001', askPrice='100.00000003')
        return Response(200, {}, json.dumps(data).encode())


def bar(t):
    return [t, '100', '102', '99', '101.00000001', '4.2', t + 59999]


class Consumer:
    def __init__(self):
        self.bars, self.quotes = [], []
        self.fail = False

    def closed_bar(self, bar):
        if self.fail:
            raise RuntimeError('consumer failed')
        self.bars.append(bar)

    def quote(self, quote):
        self.quotes.append(quote)


def make(tmp_path, transport=None, consumer=None, **kwargs):
    transport, consumer = transport or Transport(), consumer or Consumer()
    client = FuturesClient(environment=Environment.DEMO, transport=transport)
    feed = DemoMarketFeed(client, consumer, state_path=tmp_path / 'cursor.sqlite', consumer_id='test',
        clock=lambda: datetime.fromtimestamp(transport.time / 1000, timezone.utc), **kwargs)
    return feed, transport, consumer


def test_complete_precision_duplicate_restart(tmp_path):
    feed, transport, consumer = make(tmp_path)
    assert feed.poll() == dict(bars=1, quotes=1)
    assert consumer.bars[0].source == SOURCE
    assert consumer.bars[0].close == Decimal('101.00000001')
    assert consumer.quotes[0].price == Decimal('100.00000002')
    assert consumer.bars[0].closed_at.tzinfo is timezone.utc
    assert feed.poll() == dict(bars=0, quotes=0)
    feed.close()
    feed, _, _ = make(tmp_path, transport, consumer)
    assert feed.poll() == dict(bars=0, quotes=0)
    transport.time += 120000
    assert feed.poll() == dict(bars=2, quotes=1)
    feed.close()


@pytest.mark.parametrize('rows,reason', [
    ([bar(120000), bar(120000)], 'UNORDERED'),
    ([bar(180000), bar(120000)], 'UNORDERED'),
    ([bar(240000)], 'FUTURE'),
    ([], 'GAP'),
    ([bar(60000), bar(180000)], 'GAP'),
])
def test_bad_batch_never_partially_consumed(tmp_path, rows, reason):
    feed, transport, consumer = make(tmp_path)
    transport.rows = rows
    with pytest.raises(FeedPaused, match=reason):
        feed.poll()
    assert not consumer.bars and not consumer.quotes
    assert feed.watermarks() == (None, None)
    feed.close()


def test_backfill_bound_and_callback_failure(tmp_path):
    feed, transport, consumer = make(tmp_path, max_backfill=2)
    consumer.fail = True
    with pytest.raises(RuntimeError):
        feed.poll()
    assert feed.watermarks() == (None, None)
    consumer.fail = False
    feed.poll()
    transport.time += 180000
    with pytest.raises(FeedPaused, match='BACKFILL_LIMIT'):
        feed.poll()
    assert len(consumer.bars) == 1
    feed.close()


@pytest.mark.parametrize('advance', [120000, 180000])
def test_first_failure_preserves_start_across_restart(tmp_path, advance):
    feed, transport, consumer = make(tmp_path, max_backfill=3)
    consumer.fail = True
    with pytest.raises(RuntimeError):
        feed.poll()
    feed.close()
    transport.time += advance
    consumer.fail = False
    feed, _, _ = make(tmp_path, transport, consumer, max_backfill=3)
    if advance == 180000:
        with pytest.raises(FeedPaused, match='BACKFILL_LIMIT'):
            feed.poll()
        assert feed.watermarks() == (None, None)
        assert not consumer.bars
    else:
        assert feed.poll() == dict(bars=3, quotes=1)
        assert [b.opened_at.timestamp() for b in consumer.bars] == [120, 180, 240]
    feed.close()


def test_slow_backfill_pauses_quote_then_resumes_without_bar_replay(tmp_path):
    feed, transport, consumer = make(tmp_path)
    original = consumer.closed_bar
    def slow(bar):
        original(bar)
        transport.time += 6000
    consumer.closed_bar = slow
    with pytest.raises(FeedPaused, match='STALE_OR_FUTURE_QUOTE'):
        feed.poll()
    assert feed.watermarks() == (180000, None)
    assert not consumer.quotes
    assert feed.poll() == dict(bars=0, quotes=1)
    assert len(consumer.bars) == 1
    assert feed.status == 'RUNNING'
    feed.close()


@pytest.mark.parametrize('column,value', [
    (1, 'NaN'), (2, 'Infinity'), (3, '-Infinity'), (4, 'sNaN'),
    (1, 'not-a-decimal'), (2, '100'), (3, '102'), (5, '-1'),
    (1, '0'), (5, None),
])
def test_invalid_ohlcv_pauses_and_recovers(tmp_path, column, value):
    feed, transport, consumer = make(tmp_path)
    row = bar(120000)
    row[column] = value
    transport.rows = [row]
    with pytest.raises(FeedPaused, match='INVALID_OHLCV|DECIMAL_STRINGS_REQUIRED'):
        feed.poll()
    assert feed.status == 'PAUSED'
    assert feed.watermarks() == (None, None)
    assert not consumer.bars and not consumer.quotes
    transport.rows = None
    assert feed.poll() == dict(bars=1, quotes=1)
    feed.close()


@pytest.mark.parametrize('callback', ['closed_bar', 'quote'])
@pytest.mark.parametrize('error_type', [ValueError, InvalidOperation])
def test_consumer_errors_are_not_market_parse_errors(tmp_path, callback, error_type):
    feed, _, consumer = make(tmp_path)
    def fail(_):
        raise error_type('consumer bug')
    setattr(consumer, callback, fail)
    with pytest.raises(error_type, match='consumer bug') as error:
        feed.poll()
    assert not isinstance(error.value, FeedPaused)
    assert feed.reason == 'POLL_OR_CONSUMER_FAILED'
    feed.close()


def test_supervisor_recovers_from_invalid_ohlcv(tmp_path):
    feed, transport, consumer = make(tmp_path)
    row = bar(120000)
    row[1] = 'invalid'
    transport.rows = [row]
    reports = []
    def report(message):
        reports.append(json.loads(message))
        transport.rows = None
        if len(reports) == 2:
            supervisor.stop()
    supervisor = DemoSupervisor(feed, poll_seconds=0.001, report=report)
    supervisor.run()
    assert [(r['status'], r['reason']) for r in reports] == [
        ('PAUSED', 'INVALID_OHLCV'), ('RUNNING', None)]
    assert len(consumer.bars) == len(consumer.quotes) == 1
    feed.close()


def test_existing_cursor_schema_is_migrated(tmp_path):
    import sqlite3
    with sqlite3.connect(tmp_path / 'cursor.sqlite') as db:
        db.execute('CREATE TABLE cursor (id TEXT PRIMARY KEY, bar INTEGER, quote INTEGER)')
        db.execute('INSERT INTO cursor VALUES (?, ?, ?)',
            (f'{SOURCE}:NVDAUSDT:1m:test', 180000, 180001))
    feed, _, consumer = make(tmp_path)
    assert feed.poll() == dict(bars=0, quotes=0)
    assert feed.watermarks() == (180000, 180001)
    assert not consumer.bars and not consumer.quotes
    feed.close()


def test_recovery_and_stop(tmp_path):
    feed, transport, consumer = make(tmp_path)
    transport.failed = True
    supervisor = DemoSupervisor(feed, report=lambda _: supervisor.stop())
    supervisor.run()
    assert feed.reason == 'STOPPED'
    assert not consumer.bars
    transport.failed = False
    assert feed.poll()['bars'] == 1
    feed.close()


@pytest.mark.parametrize('offset', [6, -1])
def test_stale_or_future_observation_pauses_before_delivery(tmp_path, offset):
    feed, transport, consumer = make(tmp_path)
    feed.clock = lambda: datetime.fromtimestamp(transport.time / 1000 + offset, timezone.utc)
    with pytest.raises(FeedPaused):
        feed.poll()
    assert not consumer.bars and not consumer.quotes
    feed.close()


def test_rate_limit_pauses_and_honors_cooldown(tmp_path):
    from factorlab.integrations.binance_futures_trading.protocol import ExchangeError
    class Limited(Transport):
        def __call__(self, request, timeout):
            return Response(429, {'Retry-After': '17'}, b'{"code":-1003}')
    feed, _, consumer = make(tmp_path, transport=Limited())
    with pytest.raises(ExchangeError) as error:
        feed.poll()
    assert error.value.retry_after == 17
    assert feed.status == 'PAUSED'
    assert not consumer.bars and not consumer.quotes
    feed.close()


def test_identity_isolates_intervals(tmp_path):
    feed, transport, consumer = make(tmp_path)
    feed.poll()
    feed.close()
    other, _, _ = make(tmp_path, interval='3m')
    assert other.watermarks() == (None, None)
    other.close()


def test_ack_crash_replays_to_idempotent_consumer(tmp_path):
    class PersistentConsumer(Consumer):
        def closed_bar(self, bar):
            if bar not in self.bars:
                self.bars.append(bar)
    consumer = PersistentConsumer()
    feed, transport, _ = make(tmp_path, consumer=consumer)
    def crash(*_):
        raise RuntimeError('crash before cursor commit')
    feed._ack = crash
    with pytest.raises(RuntimeError):
        feed.poll()
    feed.close()
    transport.time += 120000
    feed, _, _ = make(tmp_path, transport, consumer)
    feed.poll()
    assert [b.opened_at.timestamp() for b in consumer.bars] == [120, 180, 240]
    feed.close()
