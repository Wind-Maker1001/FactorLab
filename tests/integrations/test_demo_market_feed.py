from datetime import datetime, timezone
from decimal import Decimal
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
    feed, _, _ = make(tmp_path, transport, consumer)
    feed.poll()
    assert len(consumer.bars) == 1
    feed.close()
