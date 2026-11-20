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
        if method != 'GET' or path not in {'/fapi/v1/ticker/bookTicker', '/fapi/v1/time'}:
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
            self.books.append(dict(sequence=receipt.sequence,
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
                 exchange_time=None):
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
                    {'symbol': SYMBOL}, signed=False)
                received = self.clock()
                if (not isinstance(payload, dict) or type(payload.get('time')) is not int
                        or payload.get('symbol') != SYMBOL):
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

    def receive(self, *, after):
        self._demand.set()
        with self._condition:
            ready = self._condition.wait_for(lambda: self._failure is not None or self._stop.is_set()
                or (self._receipt is not None and self._receipt.received_monotonic >= after),
                timeout=self.reader.timeout + self.poll_seconds)
            if self._failure is not None:
                error, self._failure = self._failure, None
                self._condition.notify_all()
                raise error
            if not ready or self._stop.is_set():
                raise FeedPaused('MARKET_COLLECTION_UNAVAILABLE')
            return self._receipt

    def close(self):
        self._stop.set()
        self._demand.set()
        with self._condition:
            self._condition.notify_all()
        if self._thread is not None:
            self._thread.join(timeout=self.reader.timeout + self.poll_seconds)
            if self._thread.is_alive():
                raise FeedPaused('MARKET_COLLECTION_SHUTDOWN_TIMEOUT')
