"""Bounded Demo REST events with consumer-acknowledged durable cursors."""
from contextlib import AbstractContextManager, contextmanager, nullcontext
from datetime import datetime, timezone
from math import ceil
from time import perf_counter
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Protocol
import sqlite3

from factorlab.trading.paper import Quote
from factorlab.trading.strategy import ClosedBar
from .observability import RequestCounts
from .protocol import Environment, ReadOnlyNetworkTransport

SOURCE = 'BINANCE_USDM_DEMO_REST'
INTERVALS = {'1m': 60, '3m': 180, '5m': 300, '15m': 900, '30m': 1800,
             '1h': 3600, '2h': 7200, '4h': 14400, '6h': 21600,
             '8h': 28800, '12h': 43200, '1d': 86400}
SYMBOL = 'NVDAUSDT'
CATCH_UP_PAGES = 4
MAX_QUOTE_GAP_SECONDS = 30


class FeedPaused(ValueError):
    """Observed market data cannot safely advance the consumer."""


class FeedTimings(Protocol):
    """Telemetry required when a caller supplies an explicit timings object."""

    def sample(self, name: str, seconds: float, /) -> None: ...
    def measure(self, name: str, /) -> AbstractContextManager[None]: ...
    def book_received(self, market_at: datetime, received_at: datetime, /) -> None: ...


def timestamp(milliseconds):
    if type(milliseconds) is not int or milliseconds < 0:
        raise FeedPaused('INVALID_TIMESTAMP')
    try:
        return datetime.fromtimestamp(milliseconds // 1000, timezone.utc).replace(
            microsecond=milliseconds % 1000 * 1000)
    except (ValueError, OverflowError, OSError) as error:
        raise FeedPaused('INVALID_TIMESTAMP') from error


class DemoMarketFeed:
    """Callbacks must persistently deduplicate replay after an interrupted ack.

    A consumer's closed_bar/quote methods return normally to acknowledge consumption.
    A False return from closed_bar means already consumed, not a retry request.
    Exceptions leave that event unacknowledged. One owner must use each cursor file.
    """
    def __init__(self, client, consumer, *, state_path, consumer_id, interval='1m',
                 max_backfill=120, max_quote_age=5, initial_bars=1, catch_up=False,
                 clock=lambda: datetime.now(timezone.utc), timings: FeedTimings | None = None,
                 exchange_time=None, receipt_clock=None, symbol=SYMBOL):
        if client.environment is not Environment.DEMO:
            raise ValueError('Demo client required')
        if not isinstance(client.transport, ReadOnlyNetworkTransport):
            from .protocol import OfflineTransport
            if not isinstance(client.transport, OfflineTransport):
                raise ValueError('read-only or offline transport required')
        if interval not in INTERVALS or not consumer_id or type(max_backfill) is not int or not 1 <= max_backfill <= 1000:
            raise ValueError('invalid feed identity or backfill bound')
        if not 0 < max_quote_age <= 5:
            raise ValueError('quote freshness must be within five seconds')
        if type(initial_bars) is not int or not 1 <= initial_bars <= max_backfill:
            raise ValueError('initial history must fit backfill bound')
        if timings is None:
            request_counts = getattr(client, 'request_counts', None)
            if isinstance(request_counts, RequestCounts):
                timings = request_counts
        from factorlab.integrations.binance.market import symbol_name
        self.symbol = symbol_name(symbol)
        self.timings = timings
        self.commit_seconds = 0.
        self.commit_count = 0
        self.catch_up = catch_up
        self.initial_bars = initial_bars
        self.client, self.consumer, self.clock = client, consumer, clock
        self.receipt_clock = clock if receipt_clock is None else receipt_clock
        self.exchange_time = client.demo_time if exchange_time is None else exchange_time
        self.interval, self.seconds = interval, INTERVALS[interval]
        self.max_backfill, self.max_quote_age = max_backfill, max_quote_age
        self._last_live_quote_received = None
        self.collection = None
        self.status, self.reason = 'PAUSED', 'STARTING'
        path = Path(state_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('CREATE TABLE IF NOT EXISTS cursor (id TEXT PRIMARY KEY, bar INTEGER, quote INTEGER)')
        self.identity = f'{SOURCE}:{self.symbol}:{interval}:{consumer_id}'
        with self._transaction():
            if 'initial_start' not in {row[1] for row in self.db.execute('PRAGMA table_info(cursor)')}:
                self.db.execute('ALTER TABLE cursor ADD COLUMN initial_start INTEGER')
            self.db.execute('INSERT OR IGNORE INTO cursor (id, bar, quote) VALUES (?, NULL, NULL)', (self.identity,))

    def _measure(self, name):
        return self.timings.measure(name) if self.timings is not None else nullcontext()

    @contextmanager
    def _transaction(self):
        try:
            yield
            started = perf_counter()
            try:
                self.db.commit()
            finally:
                elapsed = perf_counter() - started
                self.commit_seconds += elapsed
                self.commit_count += 1
                if self.timings is not None:
                    self.timings.sample('cursor_commit', elapsed)
        except BaseException:
            self.db.rollback()
            raise

    def _closed_bar(self, bar, *, historical=False):
        with self._measure('closed_bar_callback'):
            if historical:
                return self.consumer.historical_bar(bar)
            return self.consumer.closed_bar(bar)

    def close(self):
        self.db.close()

    def watermarks(self):
        return self.db.execute('SELECT bar, quote FROM cursor WHERE id=?', (self.identity,)).fetchone()

    def pause(self, reason):
        self.status, self.reason = 'PAUSED', reason

    def _ack(self, column, value):
        with self._transaction():
            self.db.execute(f'UPDATE cursor SET {column}=? WHERE id=?', (value, self.identity))

    def poll(self, *, before_delivery=None):
        self.pause('CHECKING')
        try:
            if self.collection is not None:
                self.collection.check()
            with self._measure('market_poll'):
                return self._poll(before_delivery)
        except FeedPaused as error:
            self.pause(str(error))
            if hasattr(self.consumer, 'history_paused'):
                self.consumer.history_paused(self.reason)
            raise
        except Exception:
            self.pause('POLL_OR_CONSUMER_FAILED')
            if hasattr(self.consumer, 'history_paused'):
                self.consumer.history_paused(self.reason)
            raise

    def _bars(self, start, page_boundary, server):
        width = self.seconds * 1000
        count = (page_boundary - start) // width
        if count == 0:
            return [], [], None
        rows = self.client.request('GET', '/fapi/v1/klines', dict(symbol=self.symbol,
            interval=self.interval, startTime=start,
            endTime=page_boundary - 1, limit=count), signed=False)
        if not isinstance(rows, list) or len(rows) > count + 1:
            raise FeedPaused('INVALID_KLINES')
        bars_received_at = self.receipt_clock()
        bars, previous = [], None
        for row in rows:
            if not isinstance(row, list) or len(row) < 7:
                raise FeedPaused('INVALID_KLINE')
            opened, closed = row[0], row[6]
            timestamp(opened)
            timestamp(closed)
            if opened % width or closed + 1 != opened + width:
                raise FeedPaused('INVALID_BOUNDARY')
            if previous is not None and opened <= previous:
                raise FeedPaused('UNORDERED_KLINES')
            previous = opened
            if opened > server:
                raise FeedPaused('FUTURE_KLINE')
            if closed + 1 > server:
                continue
            if opened < start:
                continue
            if opened != start + len(bars) * width:
                raise FeedPaused('BAR_SEQUENCE_GAP')
            if any(not isinstance(v, str) for v in row[1:6]):
                raise FeedPaused('DECIMAL_STRINGS_REQUIRED')
            try:
                bars.append(ClosedBar(self.symbol, timestamp(opened), timestamp(closed + 1),
                    *(Decimal(v) for v in row[1:6]), self.seconds, SOURCE))
            except (ValueError, InvalidOperation) as error:
                raise FeedPaused('INVALID_OHLCV') from error
        if len(bars) != count:
            raise FeedPaused('BAR_SEQUENCE_GAP')
        return bars, rows, bars_received_at

    def _backfill(self, start, boundary, server, width):
        # Hold the newest closed bar for a fresh market batch. Every earlier
        # page is contiguous, validated in full and acknowledged bar by bar.
        history_end = boundary - width
        delivered = 0
        with self._measure('bar_backfill'):
            for _ in range(CATCH_UP_PAGES):
                end = min(history_end, start + self.max_backfill * width)
                bars, _, _ = self._bars(start, end, server)
                for bar in bars:
                    self._closed_bar(bar, historical=True)
                    self._ack('bar', int(bar.closed_at.timestamp()) * 1000)
                delivered += len(bars)
                start = end
                if start == history_end:
                    break
        self.pause('CATCHING_UP')
        return dict(bars=delivered, quotes=0)

    def _recover_consumer(self, boundary, server):
        watermark = getattr(self.consumer, 'history_watermark', lambda: None)()
        if watermark is None:
            return True
        start = int(watermark.timestamp() * 1000)
        width = self.seconds * 1000
        end = boundary - width
        if start >= end:
            return True
        self.consumer.history_paused('CATCHING_UP')
        try:
            for _ in range(CATCH_UP_PAGES):
                page_end = min(end, start + self.max_backfill * width)
                bars, _, _ = self._bars(start, page_end, server)
                for bar in bars:
                    if not self._closed_bar(bar, historical=True):
                        raise FeedPaused('HISTORY_NOT_CONSUMED')
                start = page_end
                if start == end:
                    return True
        except Exception as error:
            self.consumer.history_paused(str(error) if isinstance(error, FeedPaused) else 'HISTORY_READ_FAILED')
            raise
        self.pause('CATCHING_UP')
        return False

    def _poll(self, before_delivery):
        anchor = self.exchange_time(min_valid_seconds=self.max_quote_age)
        server = int(anchor.lower_ms)
        width = self.seconds * 1000
        boundary = server // width * width
        last_bar, last_quote = self.watermarks()
        initial_start = self.db.execute('SELECT initial_start FROM cursor WHERE id=?',
            (self.identity,)).fetchone()[0]
        if last_bar is None and initial_start is None:
            initial_start = boundary - width * self.initial_bars
            with self._transaction():
                self.db.execute('UPDATE cursor SET initial_start=? WHERE id=?',
                    (initial_start, self.identity))
        start = last_bar if last_bar is not None else initial_start
        if start > boundary:
            raise FeedPaused('CLOCK_REGRESSION')
        if self.catch_up and boundary - start > width:
            if before_delivery is not None and not before_delivery():
                return None
            if not self._recover_consumer(boundary, server):
                return dict(bars=0, quotes=0)
            return self._backfill(start, boundary, server, width)
        if boundary - start > self.max_backfill * width:
            raise FeedPaused('BACKFILL_LIMIT')
        bars, rows, bars_received_at = self._bars(start, boundary, server)
        if before_delivery is not None:
            if self._poll_age(anchor, self.exchange_time(allow_refresh=False)) > self.max_quote_age:
                raise FeedPaused('STALE_POLL')
            if not before_delivery():
                return None
            # The closed range stays fixed. Take a new anchor for the current quote.
            anchor = self.exchange_time(allow_refresh=False)
        if not self._recover_consumer(boundary, server):
            return dict(bars=0, quotes=0)
        # Request the book after the closed range so history cannot age the book.
        if self.collection is None:
            raw = self.client.request('GET', '/fapi/v1/ticker/bookTicker', dict(symbol=self.symbol), signed=False)
        else:
            # Acquisition can overlap klines. Apply the exchange age bound
            # below at delivery, using the current calibration.
            raw = self.collection.receive_recent(anchor.observed_monotonic, market_after=boundary).payload
        book_wall_received_at = self.clock()
        received = self.exchange_time(allow_refresh=False)
        book_received_at = timestamp(ceil(received.upper_ms))
        try:
            observed = timestamp(raw['time'])
            if self.timings is not None:
                self.timings.book_received(observed, book_received_at)
                self.timings.sample('book_host_wall_to_received',
                    (book_wall_received_at - observed).total_seconds())
            bid, ask = Decimal(raw['bidPrice']), Decimal(raw['askPrice'])
            symbol = raw['symbol']
        except (KeyError, TypeError, ValueError, InvalidOperation) as error:
            raise FeedPaused('INVALID_BOOK') from error
        if symbol != self.symbol or raw['time'] < boundary:
            raise FeedPaused('STALE_OR_FUTURE_QUOTE')
        self._validate_quote_age(raw['time'], received)
        if last_quote is not None and raw['time'] < last_quote:
            raise FeedPaused('QUOTE_TIME_REGRESSION')
        if self.collection is None and self._last_live_quote_received is not None and (
                received.observed_monotonic - self._last_live_quote_received > MAX_QUOTE_GAP_SECONDS
                or raw['time'] - last_quote > MAX_QUOTE_GAP_SECONDS * 1000):
            self._last_live_quote_received = None
            raise FeedPaused('R13_QUOTE_GAP')
        if not bid.is_finite() or not ask.is_finite() or not 0 < bid <= ask:
            raise FeedPaused('INVALID_BOOK')
        # Validate the entire batch before advancing even one strategy input.
        batch_age = self._poll_age(anchor, received)
        if self.timings is not None:
            self.timings.sample('poll_clock_uncertainty', received.uncertainty_ms / 1000)
            self.timings.sample('poll_anchor_age', batch_age)
        if batch_age > self.max_quote_age:
            raise FeedPaused('STALE_POLL')
        raw_by_open = {row[0]: row for row in rows}
        for bar in bars:
            if hasattr(self.consumer, 'observe_bar_receipt'):
                self.consumer.observe_bar_receipt(bar,
                    raw_by_open[int(bar.opened_at.timestamp() * 1000)], bars_received_at, self.receipt_clock())
            historical = (hasattr(self.consumer, 'history_watermark')
                          and bar.closed_at.timestamp() * 1000 < boundary)
            self._closed_bar(bar, historical=historical)
            self._ack('bar', int(bar.closed_at.timestamp()) * 1000)
        delivered = 0
        if last_quote is None or raw['time'] > last_quote:
            quote = Quote(self.symbol, (bid + ask) / 2, observed)
            self._validate_quote_age(raw['time'], self.exchange_time(allow_refresh=False))
            if hasattr(self.consumer, 'set_latest_book'):
                self.consumer.set_latest_book(raw)
            self.consumer.quote(quote)
            if hasattr(self.consumer, 'observe_book'):
                self.consumer.observe_book(raw, book_received_at)
            self._ack('quote', raw['time'])
            self._last_live_quote_received = received.observed_monotonic
            delivered = 1
        elif hasattr(self.consumer, 'maintain'):
            self._validate_quote_age(raw['time'], self.exchange_time(allow_refresh=False))
            self.consumer.maintain(raw)
        self.status, self.reason = 'RUNNING', None
        return dict(bars=len(bars), quotes=delivered)

    def _poll_age(self, anchor, reading):
        return reading.observed_monotonic - anchor.observed_monotonic + reading.uncertainty_ms / 1000

    def _validate_quote_age(self, quote_ms, reading):
        if (quote_ms > reading.lower_ms
                or ceil(reading.upper_ms) - quote_ms > self.max_quote_age * 1000):
            raise FeedPaused('STALE_OR_FUTURE_QUOTE')
