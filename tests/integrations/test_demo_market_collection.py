"""Deterministic ownership and continuity checks for unsigned acquisition."""
from threading import Event, get_ident

import pytest

from factorlab.integrations.binance_futures_trading.demo_market_collection import DemoBookCollection
from factorlab.integrations.binance_futures_trading.demo_market_feed import FeedPaused
from test_binance_demo_network import wired


class Reader:
    timeout = 1

    def __init__(self, books):
        self.books = iter(books)
        self.elapsed = 0
        self.threads = []
        self.blocked = Event()
        self.release = Event()

    def request(self, method, path, params, *, signed):
        assert method == 'GET' and path.endswith('/bookTicker') and not signed
        self.threads.append(get_ident())
        entry = next(self.books, None)
        if entry is None:
            self.blocked.set()
            assert self.release.wait(2)
            entry = (self.elapsed + 1, 1000)
        if isinstance(entry, Exception):
            raise entry
        self.elapsed, market = entry
        return dict(symbol='NVDAUSDT', time=market, bidPrice='100', askPrice='101')


def test_collection_has_no_owner_callbacks_and_joins():
    reader = Reader([(1, 1000), (2, 2000)])
    collection = DemoBookCollection(reader, poll_seconds=.01, clock=lambda: reader.elapsed)
    owner = get_ident()
    collection.start()
    try:
        receipt = collection.receive(after=1, market_after=2000)
        assert receipt.payload['time'] == 2000
        assert receipt.received_monotonic == 2
        assert reader.threads and all(thread != owner for thread in reader.threads)
    finally:
        reader.release.set()
        collection.close()
    assert not collection._thread.is_alive()


@pytest.mark.parametrize('books', [
    [(1, 1000), (33, 2000), (34, 3000)],
    [(1, 1000), (2, 33001), (3, 34000)],
])
def test_real_gap_survives_coalescing(books):
    reader = Reader(books)
    collection = DemoBookCollection(reader, poll_seconds=.001, clock=lambda: reader.elapsed)
    collection.start()
    try:
        assert reader.blocked.wait(2)
        with pytest.raises(FeedPaused, match='R13_QUOTE_GAP'):
            collection.check()
    finally:
        reader.release.set()
        collection.close()


def test_continuous_acquisition_spans_a_bar_without_owner_consumption():
    reader = Reader([(second, second * 1000) for second in range(1, 72, 10)])
    collection = DemoBookCollection(reader, poll_seconds=.001, clock=lambda: reader.elapsed)
    collection.start()
    try:
        # The owner is occupied for 70 modeled seconds; acquisition keeps running.
        assert reader.blocked.wait(2)
        collection.check()
        receipt = collection.receive(after=71)
        assert receipt.sequence == 8
        assert receipt.received_monotonic == 71
        with pytest.raises(TypeError):
            receipt.payload['time'] = 0
    finally:
        reader.release.set()
        collection.close()


def test_collection_diagnostics_are_merged_only_on_owner(wired, monkeypatch):
    from factorlab.integrations.binance_futures_trading.demo_market_collection import CollectionRequests
    from factorlab.integrations.binance_futures_trading.observability import ObservedClient, RequestCounts
    from factorlab.integrations.binance_futures_trading.protocol import Environment
    from factorlab.integrations.binance_futures_trading.network import DemoNetworkTransport
    owner_thread = get_ident()

    class Counts(RequestCounts):
        def record(self, *args, **kwargs):
            assert get_ident() == owner_thread
            return super().record(*args, **kwargs)

        def clock_event(self, *args):
            assert get_ident() == owner_thread

    import json
    from factorlab.integrations.binance.client import Response
    fixture_type = type(wired[0])
    original_request = fixture_type.__call__

    def respond(fixture, request, timeout):
        if request.path.endswith('/bookTicker'):
            return Response(200, {}, json.dumps(dict(
                symbol='NVDAUSDT', time=1000, bidPrice='100', askPrice='101')).encode())
        return original_request(fixture, request, timeout)

    monkeypatch.setattr(fixture_type, '__call__', respond)
    counts = Counts()
    proxy = CollectionRequests(counts)
    reader = ObservedClient(environment=Environment.DEMO, transport=DemoNetworkTransport(),
                            request_counts=proxy, request_source='collection')
    acquired = Event()

    def received(receipt, previous):
        proxy.acquired(receipt, previous)
        acquired.set()

    collection = DemoBookCollection(reader, poll_seconds=1, on_acquired=received)
    collection.start()
    try:
        receipt = collection.receive(after=0)
        assert acquired.wait(2)
        assert receipt.payload['time'] == 1000
        assert not counts.endpoints
    finally:
        collection.close()
    proxy.drain()
    assert counts['GET'] == len(wired[1])
    assert counts['POST'] == counts['DELETE'] == 0
    rows = counts.snapshot()['endpoints']
    assert rows and {row['phase'] for row in rows} == {'collection'}


def test_supervisor_collects_through_long_recovery_and_resets_bar_gate(monkeypatch):
    from datetime import timedelta
    from types import SimpleNamespace
    from factorlab.integrations.binance_futures_trading.demo_grid_supervisor import GridConsumer
    from factorlab.integrations.binance_futures_trading.demo_supervisor import DemoSupervisor
    from factorlab.integrations.binance_futures_trading.fake import NOW
    reader = Reader([(second, second * 1000) for second in range(1, 72, 10)])
    collection = DemoBookCollection(reader, poll_seconds=.001, clock=lambda: reader.elapsed)
    monkeypatch.setattr('factorlab.integrations.binance_futures_trading.demo_supervisor.monotonic',
                        lambda: reader.elapsed)
    begin = Event()
    original_request = reader.request

    def request(*args, **kwargs):
        assert begin.wait(2)
        return original_request(*args, **kwargs)

    reader.request = request
    owner = get_ident()
    withdrawn, consumed = [], []
    consumer = GridConsumer(SimpleNamespace(withdraw_unsubmitted=lambda: withdrawn.append(get_ident())),
        SimpleNamespace(desired=lambda: 'RUNNING'), clock=lambda: NOW + timedelta(seconds=reader.elapsed))
    consumer.running = consumer.live_bar = True

    class Feed:
        seconds = 60
        interval = '1m'
        status = 'RUNNING'
        reason = None

        def poll(self):
            assert get_ident() == owner
            assert not consumer.live_bar
            assert consumer.eligible_after == NOW + timedelta(seconds=71)
            receipt = self.collection.receive(after=71)
            consumed.append(receipt.sequence)
            supervisor.stop()
            reader.release.set()
            return dict(bars=0, quotes=1)

        def pause(self, reason):
            self.reason = reason

        def watermarks(self):
            return None, None

    def recovery():
        assert get_ident() == owner
        begin.set()
        assert reader.blocked.wait(2)
        assert not consumed and not withdrawn

    supervisor = DemoSupervisor(Feed(), before_poll=recovery, collection=collection,
        on_recovery_delay=consumer.require_fresh_bar, report=lambda _: None)
    supervisor.run()
    assert consumed == [8]
    assert withdrawn == [owner]
    assert not collection._thread.is_alive()


def test_recorded_collection_failure_blocks_dispatch_gate():
    from types import SimpleNamespace
    from factorlab.integrations.binance_futures_trading.demo_grid_supervisor import GridConsumer
    reader = Reader([TimeoutError('fixture')])
    collection = DemoBookCollection(reader, poll_seconds=.01, clock=lambda: reader.elapsed)
    consumer = GridConsumer(None, SimpleNamespace(desired=lambda: 'RUNNING'))
    consumer.running = consumer.live_bar = True
    consumer.supervisor = SimpleNamespace(collection=collection)
    failed = Event()
    original = reader.request

    def request(*args, **kwargs):
        try:
            return original(*args, **kwargs)
        finally:
            failed.set()

    reader.request = request
    collection.start()
    try:
        assert failed.wait(2)
        # Acquire the condition after the worker publishes its terminal result.
        with collection._condition:
            assert collection._condition.wait_for(lambda: collection._failure is not None, timeout=2)
        with pytest.raises(TimeoutError, match='fixture'):
            consumer.gate()
    finally:
        reader.release.set()
        collection.close()


def test_network_failure_is_handed_to_owner_without_retry():
    reader = Reader([TimeoutError('fixture')])
    collection = DemoBookCollection(reader, poll_seconds=.01, clock=lambda: reader.elapsed)
    collection.start()
    try:
        with pytest.raises(TimeoutError):
            collection.receive(after=0)
        assert len(reader.threads) == 1
    finally:
        reader.release.set()
        collection.close()
