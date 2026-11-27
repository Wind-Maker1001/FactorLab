"""Unsigned book acquisition without consumer or SQLite access."""
from collections import deque
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from threading import Condition, Event, Thread
from time import monotonic
from types import MappingProxyType

from .demo_market_feed import FeedPaused, MAX_QUOTE_GAP_SECONDS, SYMBOL
from .observability import RequestCounts


class CollectionRequests(RequestCounts):
    """Reserve shared budgets before IO; merge diagnostics only on the owner."""
    def __init__(self, owner):
        super().__init__()
        self.owner = owner
        self.admission_lock = owner.admission_lock
        self.pending = deque()
        self.books = deque()
        self.clocks = deque()

    def before_request(self, source, method, path):
        if method != 'GET' or path not in {'/fapi/v1/ticker/bookTicker', '/fapi/v1/time', '/fapi/v1/klines',
                                             '/fapi/v1/fundingRate', '/fapi/v1/premiumIndex'}:
            raise FeedPaused('INVALID_COLLECTION_REQUEST')
        if len(self.pending) >= 1500:
            raise FeedPaused('MARKET_TELEMETRY_OVERFLOW')
        self.owner.before_request(source, method, path)

    def record(self, source, method, path, seconds, error, weights, *, status=None):
        self.pending.append((source, method, path, seconds, error, dict(weights), status))

    def clock_event(self, source, kind, details):
        if len(self.clocks) >= 1500:
            raise FeedPaused('MARKET_TELEMETRY_OVERFLOW')
        self.clocks.append((source, kind, dict(details)))

    def acquired(self, receipt, previous):
        with self.admission_lock:
            if len(self.books) >= 1500:
                raise FeedPaused('MARKET_TELEMETRY_OVERFLOW')
            self.books.append(dict(symbol=receipt.payload['symbol'], sequence=receipt.sequence,
                market_ms=receipt.payload['time'], received_monotonic=receipt.received_monotonic,
                received_gap_seconds=(receipt.received_monotonic - previous.received_monotonic
                    if previous is not None else None),
                market_gap_seconds=((receipt.payload['time'] - previous.payload['time']) / 1000
                    if previous is not None else None)))

    def drain(self):
        with self.admission_lock:
            while self.clocks:
                self.owner.clock_event(*self.clocks.popleft())
            while self.books:
                self.owner.clock_event('collection', 'book_acquired', self.books.popleft())
            phase = self.owner.phase
            self.owner.phase = 'collection'
            try:
                while self.pending:
                    source, method, path, seconds, error, weights, status = self.pending.popleft()
                    self.owner[method] += 1
                    self.owner.record(source, method, path, seconds, error, weights, status=status)
            finally:
                self.owner.phase = phase


def collection_client(requests, factory):
    telemetry = CollectionRequests(requests)
    return factory(telemetry), telemetry


@dataclass(frozen=True)
class BookReceipt:
    payload: object
    received_monotonic: float
    sequence: int


class DemoBookCollection:
    """Coalesce books while retaining every observed continuity failure.

    Only the owner may consume receipts or clear an acknowledged interruption.
    The reader must be a dedicated unsigned client, never the session client.
    """
    def __init__(self, reader, *, poll_seconds, clock=monotonic, on_acquired=None,
                 exchange_time=None, symbol=SYMBOL):
        from factorlab.integrations.binance.market import symbol_name
        self.symbol = symbol_name(symbol)
        if poll_seconds <= 0:
            raise ValueError('positive collection interval required')
        self.reader, self.poll_seconds, self.clock = reader, poll_seconds, clock
        self.on_acquired = on_acquired
        self._condition = Condition()
        self._stop = Event()
        self._demand = Event()
        self._receipt = None
        self._failure = None
        self._sequence = 0
        self._thread = None
        self._previous = None
        self.exchange_time = exchange_time

    def _validate_time(self, market_ms):
        if self.exchange_time is not None:
            reading = self.exchange_time(allow_refresh=False)
            if market_ms > reading.lower_ms or reading.upper_ms - market_ms > 5000:
                raise FeedPaused('STALE_OR_FUTURE_QUOTE')

    def start(self):
        self._thread = Thread(target=self._run, name='demo-book-get')
        self._thread.start()

    def _run(self):
        while not self._stop.is_set():
            try:
                if self.exchange_time is not None:
                    self.exchange_time(min_valid_seconds=5)
                payload = self.reader.request('GET', '/fapi/v1/ticker/bookTicker',
                    {'symbol': self.symbol}, signed=False)
                received = self.clock()
                if (not isinstance(payload, dict) or type(payload.get('time')) is not int
                        or payload.get('symbol') != self.symbol):
                    raise FeedPaused('INVALID_BOOK')
                try:
                    bid, ask = Decimal(payload['bidPrice']), Decimal(payload['askPrice'])
                except (KeyError, TypeError, ValueError, InvalidOperation) as error:
                    raise FeedPaused('INVALID_BOOK') from error
                if not bid.is_finite() or not ask.is_finite() or not 0 < bid <= ask:
                    raise FeedPaused('INVALID_BOOK')
                self._validate_time(payload['time'])
                with self._condition:
                    previous = self._previous
                    if previous is not None and payload['time'] < previous.payload['time']:
                        raise FeedPaused('QUOTE_TIME_REGRESSION')
                    if previous is not None and (
                            received - previous.received_monotonic > MAX_QUOTE_GAP_SECONDS
                            or payload['time'] - previous.payload['time'] > MAX_QUOTE_GAP_SECONDS * 1000):
                        self._failure = self._failure or FeedPaused('R13_QUOTE_GAP')
                    self._sequence += 1
                    receipt = BookReceipt(MappingProxyType(dict(payload)), received, self._sequence)
                    self._receipt = self._previous = receipt
                    self._condition.notify_all()
                if self.on_acquired is not None:
                    self.on_acquired(receipt, previous)
            except Exception as error:
                with self._condition:
                    self._failure = self._failure or error
                    self._condition.notify_all()
                # The owner acknowledges the failure before acquisition resumes.
                with self._condition:
                    self._condition.wait_for(lambda: self._failure is None or self._stop.is_set())
                self._stop.wait(max(self.poll_seconds, getattr(error, 'retry_after', None) or 0))
                continue
            self._demand.wait(self.poll_seconds)
            self._demand.clear()

    def check(self):
        with self._condition:
            if self._failure is not None:
                error, self._failure = self._failure, None
                self._condition.notify_all()
                raise error

    def receive(self, *, after, market_after=0):
        self._demand.set()
        with self._condition:
            ready = self._condition.wait_for(lambda: self._failure is not None or self._stop.is_set()
                or (self._receipt is not None and self._receipt.received_monotonic >= after
                    and self._receipt.payload['time'] >= market_after),
                timeout=self.reader.timeout + self.poll_seconds)
            if self._failure is not None:
                error, self._failure = self._failure, None
                self._condition.notify_all()
                raise error
            if not ready or self._stop.is_set():
                raise FeedPaused('MARKET_COLLECTION_UNAVAILABLE')
            return self._receipt

    def peek_recent(self, at):
        """Return without waiting so another symbol's reader cannot stall the owner."""
        self._demand.set()
        with self._condition:
            if self._failure is not None:
                error, self._failure = self._failure, None
                self._condition.notify_all()
                raise error
            receipt = self._receipt
            if (self._stop.is_set() or receipt is None
                    or at - receipt.received_monotonic > min(self.poll_seconds, 2)):
                raise FeedPaused('MARKET_COLLECTION_UNAVAILABLE')
            self._validate_time(receipt.payload['time'])
            return receipt

    def receive_recent(self, at, *, market_after=0):
        return self.receive(after=at - min(self.poll_seconds, 2), market_after=market_after)

    def close(self):
        self._stop.set()
        self._demand.set()
        with self._condition:
            self._condition.notify_all()
        if self._thread is not None:
            self._thread.join(timeout=self.reader.timeout + self.poll_seconds)
            if self._thread.is_alive():
                raise FeedPaused('MARKET_COLLECTION_SHUTDOWN_TIMEOUT')


class DemoSymbolCollections:
    """Dedicated unsigned readers; only the Execute owner drains their receipts."""
    def __init__(self, session_client, requests, factory, *, poll_seconds):
        from contextlib import ExitStack
        self.session_client, self.requests, self.factory = session_client, requests, factory
        self.poll_seconds = poll_seconds
        self.collections = {}
        self.telemetry = {}
        self.bar_readers, self.bar_jobs = {}, {}
        self.stack = ExitStack()

    def require(self, symbols):
        from factorlab.integrations.binance.market import symbol_name
        for offered in sorted(symbols):
            symbol = symbol_name(offered)
            if symbol in self.collections:
                continue
            reader, telemetry = collection_client(self.requests, self.factory)
            reader.share_demo_clock(self.session_client)
            collection = DemoBookCollection(reader, poll_seconds=self.poll_seconds,
                clock=reader.monotonic, on_acquired=telemetry.acquired,
                exchange_time=reader.demo_time, symbol=symbol)
            self.stack.callback(collection.close)
            self.collections[symbol], self.telemetry[symbol] = collection, telemetry
            collection.start()

    def bars(self, symbol, interval, *, after, history_limit):
        """Schedule or collect one page without waiting on another symbol's GET."""
        return self._collect((symbol, interval), self._read_bars, symbol, interval, after, history_limit)

    def funding(self, symbol, *, after,
                kinds=frozenset({'funding_settlement', 'funding_forecast'}), limit=1000):
        return self._collect((symbol, 'funding'), self._read_funding, symbol, after, kinds, limit)

    def _collect(self, key, read, *args):
        from concurrent.futures import ThreadPoolExecutor
        if key not in self.bar_readers:
            reader, telemetry = collection_client(self.requests, self.factory)
            reader.share_demo_clock(self.session_client)
            executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='demo-market-' + key[0])
            self.stack.callback(executor.shutdown)
            self.bar_readers[key] = reader, telemetry, executor
        reader, telemetry, executor = self.bar_readers[key]
        telemetry.drain()
        job = self.bar_jobs.get(key)
        if job is None:
            self.bar_jobs[key] = executor.submit(read, reader, *args)
            return None
        if not job.done():
            return None
        del self.bar_jobs[key]
        return job.result()

    @staticmethod
    def _read_funding(reader, symbol, after, kinds, limit):
        from .demo_market_feed import SOURCE, timestamp

        rows, forecast = [], None
        if 'funding_settlement' in kinds:
            params = dict(symbol=symbol, limit=limit)
            if after is not None:
                params['startTime'] = after
            rows = reader.request('GET', '/fapi/v1/fundingRate', params, signed=False)
            if not isinstance(rows, list) or len(rows) > limit:
                raise FeedPaused('INVALID_FUNDING_SETTLEMENT')
        if 'funding_forecast' in kinds:
            forecast = reader.request('GET', '/fapi/v1/premiumIndex', dict(symbol=symbol), signed=False)
            if not isinstance(forecast, dict):
                raise FeedPaused('INVALID_FUNDING_FORECAST')
        reading = reader.demo_time(min_valid_seconds=5)
        observed_at = timestamp(reading.upper_ms)
        observations = []
        previous = None
        for row in rows:
            if row['symbol'] != symbol or type(row['fundingTime']) is not int:
                raise FeedPaused('INVALID_FUNDING_IDENTITY')
            effective_at = timestamp(row['fundingTime'])
            rate, mark = Decimal(row['fundingRate']), Decimal(row['markPrice'])
            if (not rate.is_finite() or not mark.is_finite() or mark <= 0
                    or effective_at > observed_at
                    or previous is not None and effective_at <= previous):
                raise FeedPaused('INVALID_FUNDING_SETTLEMENT')
            previous = effective_at
            event_id = symbol + ':' + str(row['fundingTime'])
            observations.append(dict(kind='funding_settlement', event_id=event_id,
                observed_at=observed_at, effective_at=effective_at, source=SOURCE,
                payload=dict(event_id=event_id, funding_rate=rate, mark_price=mark)))
        if forecast is not None:
            if forecast['symbol'] != symbol or type(forecast['time']) is not int:
                raise FeedPaused('INVALID_FUNDING_FORECAST')
            rate, mark = Decimal(forecast['lastFundingRate']), Decimal(forecast['markPrice'])
            if (not rate.is_finite() or not mark.is_finite() or mark <= 0
                    or timestamp(forecast['time']) > observed_at):
                raise FeedPaused('INVALID_FUNDING_FORECAST')
            observations.append(dict(kind='funding_forecast',
                event_id=symbol + ':' + str(forecast['time']), observed_at=observed_at,
                effective_at=timestamp(forecast['nextFundingTime']), source=SOURCE,
                payload=dict(funding_rate=rate, mark_price=mark)))
        return observations

    @staticmethod
    def _read_bars(reader, symbol, interval, after, history_limit):
        from .demo_market_feed import INTERVALS, read_closed_bars, timestamp
        width = INTERVALS[interval] * 1000
        reading = reader.demo_time(min_valid_seconds=5)
        server = reading.lower_ms
        boundary = server // width * width
        start = boundary - min(history_limit, 1000) * width if after is None else after
        page_boundary = min(boundary, start + 1000 * width)
        bars, _, _ = read_closed_bars(reader, symbol=symbol, interval=interval,
            start=start, page_boundary=page_boundary, server=server,
            receipt_clock=lambda: timestamp(server))
        return bars

    def book(self, symbol):
        collection = self.collections[symbol]
        self.telemetry[symbol].drain()
        return dict(collection.peek_recent(collection.clock()).payload)

    def close(self):
        self.stack.close()
        for telemetry in self.telemetry.values():
            telemetry.drain()
        for _, telemetry, _ in self.bar_readers.values():
            telemetry.drain()
