"""Bounded Demo REST events with consumer-acknowledged durable cursors."""
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
import sqlite3

from factorlab.trading.paper import Quote
from factorlab.trading.strategy import ClosedBar
from .protocol import Environment, ReadOnlyNetworkTransport

SOURCE = 'BINANCE_USDM_DEMO_REST'
INTERVALS = {'1m': 60, '3m': 180, '5m': 300, '15m': 900, '30m': 1800,
             '1h': 3600, '2h': 7200, '4h': 14400, '6h': 21600,
             '8h': 28800, '12h': 43200, '1d': 86400}
SYMBOL = 'NVDAUSDT'


class FeedPaused(ValueError):
    """Observed market data cannot safely advance the consumer."""


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
                 max_backfill=120, max_quote_age=5, clock=lambda: datetime.now(timezone.utc)):
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
        self.client, self.consumer, self.clock = client, consumer, clock
        self.interval, self.seconds = interval, INTERVALS[interval]
        self.max_backfill, self.max_quote_age = max_backfill, max_quote_age
        self.status, self.reason = 'PAUSED', 'STARTING'
        path = Path(state_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('CREATE TABLE IF NOT EXISTS cursor (id TEXT PRIMARY KEY, bar INTEGER, quote INTEGER)')
        self.identity = f'{SOURCE}:{SYMBOL}:{interval}:{consumer_id}'
        with self.db:
            if 'initial_start' not in {row[1] for row in self.db.execute('PRAGMA table_info(cursor)')}:
                self.db.execute('ALTER TABLE cursor ADD COLUMN initial_start INTEGER')
            self.db.execute('INSERT OR IGNORE INTO cursor (id, bar, quote) VALUES (?, NULL, NULL)', (self.identity,))

    def close(self):
        self.db.close()

    def watermarks(self):
        return self.db.execute('SELECT bar, quote FROM cursor WHERE id=?', (self.identity,)).fetchone()

    def pause(self, reason):
        self.status, self.reason = 'PAUSED', reason

    def _ack(self, column, value):
        with self.db:
            self.db.execute(f'UPDATE cursor SET {column}=? WHERE id=?', (value, self.identity))

    def poll(self):
        self.pause('CHECKING')
        try:
            return self._poll()
        except FeedPaused as error:
            self.pause(str(error))
            raise
        except Exception:
            self.pause('POLL_OR_CONSUMER_FAILED')
            raise

    def _poll(self):
        server = self.client.request('GET', '/fapi/v1/time', signed=False)['serverTime']
        server_at = timestamp(server)
        if abs((self.clock() - server_at).total_seconds()) > self.max_quote_age:
            raise FeedPaused('CLOCK_SKEW')
        width = self.seconds * 1000
        boundary = server // width * width
        last_bar, last_quote = self.watermarks()
        initial_start = self.db.execute('SELECT initial_start FROM cursor WHERE id=?',
            (self.identity,)).fetchone()[0]
        if last_bar is None and initial_start is None:
            initial_start = boundary - width
            with self.db:
                self.db.execute('UPDATE cursor SET initial_start=? WHERE id=?',
                    (initial_start, self.identity))
        start = last_bar if last_bar is not None else initial_start
        if start > boundary:
            raise FeedPaused('CLOCK_REGRESSION')
        if boundary - start > self.max_backfill * width:
            raise FeedPaused('BACKFILL_LIMIT')
        rows = self.client.request('GET', '/fapi/v1/klines', dict(symbol=SYMBOL,
            interval=self.interval, startTime=start, endTime=server, limit=self.max_backfill + 1), signed=False)
        if not isinstance(rows, list) or len(rows) > self.max_backfill + 1:
            raise FeedPaused('INVALID_KLINES')
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
                bars.append(ClosedBar(SYMBOL, timestamp(opened), timestamp(closed + 1),
                    *(Decimal(v) for v in row[1:6]), self.seconds, SOURCE))
            except (ValueError, InvalidOperation) as error:
                raise FeedPaused('INVALID_OHLCV') from error
        if start + len(bars) * width != boundary:
            raise FeedPaused('BAR_SEQUENCE_GAP')
        raw = self.client.request('GET', '/fapi/v1/ticker/bookTicker', dict(symbol=SYMBOL), signed=False)
        try:
            observed = timestamp(raw['time'])
            bid, ask = Decimal(raw['bidPrice']), Decimal(raw['askPrice'])
            symbol = raw['symbol']
        except (KeyError, TypeError, ValueError, InvalidOperation) as error:
            raise FeedPaused('INVALID_BOOK') from error
        if (symbol != SYMBOL or raw['time'] < boundary
                or not 0 <= (self.clock() - observed).total_seconds() <= self.max_quote_age):
            raise FeedPaused('STALE_OR_FUTURE_QUOTE')
        if last_quote is not None and raw['time'] < last_quote:
            raise FeedPaused('QUOTE_TIME_REGRESSION')
        if not bid.is_finite() or not ask.is_finite() or not 0 < bid <= ask:
            raise FeedPaused('INVALID_BOOK')
        # Validate the entire batch before advancing even one strategy input.
        if (self.clock() - server_at).total_seconds() > self.max_quote_age:
            raise FeedPaused('STALE_POLL')
        for bar in bars:
            self.consumer.closed_bar(bar)
            self._ack('bar', int(bar.closed_at.timestamp()) * 1000)
        delivered = 0
        if last_quote is None or raw['time'] > last_quote:
            quote = Quote(SYMBOL, (bid + ask) / 2, observed)
            if not 0 <= (self.clock() - observed).total_seconds() <= self.max_quote_age:
                raise FeedPaused('STALE_OR_FUTURE_QUOTE')
            self.consumer.quote(quote)
            self._ack('quote', raw['time'])
            delivered = 1
        self.status, self.reason = 'RUNNING', None
        return dict(bars=len(bars), quotes=delivered)
