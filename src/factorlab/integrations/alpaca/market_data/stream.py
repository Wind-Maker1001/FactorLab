"""Read-only Alpaca stock WebSocket transport with explicit gap recovery."""
import asyncio
from collections import OrderedDict, deque
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math


FEED_URLS = {
    'iex': 'wss://stream.data.alpaca.markets/v2/iex',
    'sip': 'wss://stream.data.alpaca.markets/v2/sip',
    'delayed_sip': 'wss://stream.data.alpaca.markets/v2/delayed_sip',
    'boats': 'wss://stream.data.alpaca.markets/v1beta1/boats',
    'overnight': 'wss://stream.data.alpaca.markets/v1beta1/overnight',
}
CHANNELS = {'t': 'trades', 'q': 'quotes', 'b': 'bars', 'u': 'updatedBars',
            'd': 'dailyBars', 's': 'statuses', 'l': 'lulds',
            'c': 'corrections', 'x': 'cancelErrors'}


class StreamError(Exception):
    """Base error; transport and server messages never expose credentials."""


class StreamProtocolError(StreamError):
    """Malformed messages, rejected authentication, or subscription mismatch."""
    def __init__(self, message, code=None):
        self.code = code
        super().__init__(message)


class StreamDisconnected(StreamError):
    """The bounded transport reconnect budget was exhausted."""


class StreamRecoveryError(StreamError):
    """A disconnected stream cannot establish recovered coverage."""


@dataclass(frozen=True)
class StreamEvent:
    """Wire event without timestamp truncation or invented market values.

    ``event_at`` is the original wire ``t`` string, ``received_at`` is aware
    UTC, and ``source`` is the explicitly selected feed. Corrections and cancel
    errors retain their own channels and their complete original fields.
    """

    channel: str
    symbol: str
    event_at: str
    received_at: datetime
    source: str
    payload: dict


def connect(url):
    """Lazy optional dependency; injected connectors need no websockets package."""
    from websockets.asyncio.client import connect as websocket_connect
    return websocket_connect(url)


def utc_now():
    return datetime.now(timezone.utc)


class AlpacaStream:
    """Single-consumer asynchronous stock event stream.

    Supply ``api_key`` and ``secret_key`` separately, an explicit ``feed``, and
    ``subscriptions`` mapping wire channel names to symbol iterables. The
    connector takes only the selected URL and returns an async context manager
    whose socket implements async ``send`` and ``recv``. It receives no secrets
    in its URL. No trading actions or alternate-feed substitution are supported.

    ``events()`` authenticates and verifies subscription acknowledgments before
    yielding events. Corrections/cancel errors also arrive with trade subscriptions.
    Each receive has a timeout. Reconnect delays use injectable async ``sleep``;
    ``max_reconnects`` bounds the total retries for this instance, not each outage.
    After reconnection and subscription restoration, ``recover_gap`` is awaited
    with keyword arguments ``start``, ``end`` (aware UTC receipt boundaries),
    ``feed``, and ``subscriptions``. It must return exactly True after successful
    REST recovery; a missing callback or any other result raises StreamRecoveryError.
    This acknowledgment does not assert exchange sequence completeness. Recovery
    rows remain the callback's responsibility and are never fabricated here.

    Deduplication uses full wire payloads, partitioned by channel and symbol, so
    changed bar revisions, quotes, corrections, and cancels are preserved. Its
    global LRU is bounded by ``dedup_capacity``; older replays can be emitted.
    ``recovered`` is false throughout disconnect/recovery and on iterator close.
    """

    def __init__(self, *, api_key, secret_key, feed, subscriptions,
                 connector=connect, recover_gap=None, max_reconnects=3,
                 backoff=1.0, max_backoff=30.0, receive_timeout=30.0,
                 dedup_capacity=10000, sleep=asyncio.sleep, clock=utc_now):
        if feed not in FEED_URLS:
            raise ValueError('unsupported stock feed')
        if not isinstance(api_key, str) or not api_key or not isinstance(secret_key, str) or not secret_key:
            raise ValueError('separate nonempty API key and secret key required')
        if not isinstance(max_reconnects, int) or isinstance(max_reconnects, bool) or max_reconnects < 0:
            raise ValueError('max_reconnects must be a nonnegative integer')
        if not isinstance(dedup_capacity, int) or isinstance(dedup_capacity, bool) or dedup_capacity < 1:
            raise ValueError('dedup_capacity must be positive')
        if any(not math.isfinite(v) or v < 0 for v in (backoff, max_backoff)) or not math.isfinite(receive_timeout) or receive_timeout <= 0:
            raise ValueError('invalid timeout or backoff')
        if not isinstance(subscriptions, Mapping) or not subscriptions:
            raise ValueError('subscriptions must be a nonempty channel mapping')
        requested = {}
        for channel, symbols in subscriptions.items():
            if channel not in CHANNELS.values() or channel in {'corrections', 'cancelErrors'}:
                raise ValueError('unsupported subscription channel')
            if isinstance(symbols, str):
                raise ValueError('symbols must be an iterable, not a string')
            values = tuple(symbols)
            if not values or any(not isinstance(s, str) or not s for s in values) or len(set(values)) != len(values):
                raise ValueError('symbols must be distinct nonempty strings')
            requested[channel] = values
        self.feed, self.url = feed, FEED_URLS[feed]
        self._credentials = {'action': 'auth', 'key': api_key, 'secret': secret_key}
        self._subscriptions = requested
        self.connector, self.recover_gap = connector, recover_gap
        self.max_reconnects = max_reconnects
        self.backoff, self.max_backoff = backoff, max_backoff
        self.receive_timeout, self.dedup_capacity = receive_timeout, dedup_capacity
        self.sleep, self.clock = sleep, clock
        self.recovered = False
        self._seen = OrderedDict()
        self._started = False

    async def _receive(self, socket, pending):
        if not pending:
            raw = await asyncio.wait_for(socket.recv(), self.receive_timeout)
            try:
                batch = json.loads(raw)
            except (ValueError, TypeError, UnicodeError):
                raise StreamProtocolError('invalid JSON frame') from None
            if not isinstance(batch, list) or not batch or any(not isinstance(item, dict) for item in batch):
                raise StreamProtocolError('expected a nonempty message array')
            pending.extend((item, self.clock()) for item in batch)
        item, received = pending.popleft()
        if item.get('T') == 'error':
            raise StreamProtocolError('Alpaca rejected stream request', code=item.get('code'))
        return item, received

    async def _handshake(self, socket, pending):
        item, _ = await self._receive(socket, pending)
        if item != {'T': 'success', 'msg': 'connected'}:
            raise StreamProtocolError('missing connection acknowledgment')
        await socket.send(json.dumps(self._credentials))
        item, _ = await self._receive(socket, pending)
        if item != {'T': 'success', 'msg': 'authenticated'}:
            raise StreamProtocolError('missing authentication acknowledgment')
        await socket.send(json.dumps({'action': 'subscribe', **self._subscriptions}))
        item, _ = await self._receive(socket, pending)
        if item.get('T') != 'subscription':
            raise StreamProtocolError('missing subscription acknowledgment')
        for channel in set(CHANNELS.values()) - {'corrections', 'cancelErrors'}:
            actual = item.get(channel, [])
            if not isinstance(actual, list) or set(actual) != set(self._subscriptions.get(channel, ())):
                raise StreamProtocolError('subscription acknowledgment differs from request')

    def _event(self, item, received):
        channel = CHANNELS.get(item.get('T'))
        symbol, timestamp = item.get('S'), item.get('t')
        subscription = 'trades' if channel in {'corrections', 'cancelErrors'} else channel
        allowed = self._subscriptions.get(subscription, ())
        if channel is None or not isinstance(symbol, str) or (symbol not in allowed and '*' not in allowed):
            raise StreamProtocolError('unexpected channel or symbol')
        if not isinstance(timestamp, str) or not timestamp:
            raise StreamProtocolError('missing event timestamp')
        key = (channel, symbol, json.dumps(item, sort_keys=True, separators=(',', ':')))
        if key in self._seen:
            self._seen.move_to_end(key)
            return None
        self._seen[key] = None
        if len(self._seen) > self.dedup_capacity:
            self._seen.popitem(last=False)
        return StreamEvent(channel, symbol, timestamp, received, self.feed, dict(item))

    async def events(self):
        """Yield StreamEvent objects until cancelled, closed, or a terminal error."""
        if self._started:
            raise StreamError('stream instances support one consumer')
        self._started = True
        failures = 0
        gap_start = None
        last_received = self.clock()
        transport_errors = (OSError, TimeoutError, EOFError)
        try:
            from websockets.exceptions import ConnectionClosed, InvalidHandshake
        except ModuleNotFoundError:
            pass
        else:
            transport_errors += (ConnectionClosed, InvalidHandshake)
        try:
            while True:
                try:
                    async with self.connector(self.url) as socket:
                        pending = deque()
                        await self._handshake(socket, pending)
                        if gap_start is not None:
                            if self.recover_gap is None:
                                raise StreamRecoveryError('REST gap recovery callback required')
                            try:
                                recovered = await self.recover_gap(
                                    start=gap_start, end=self.clock(), feed=self.feed,
                                    subscriptions=dict(self._subscriptions))
                            except asyncio.CancelledError:
                                raise
                            except Exception:
                                raise StreamRecoveryError('REST gap recovery failed') from None
                            if recovered is not True:
                                raise StreamRecoveryError('REST gap recovery not acknowledged')
                            gap_start = None
                        self.recovered = True
                        while True:
                            item, received = await self._receive(socket, pending)
                            event = self._event(item, received)
                            last_received = received
                            if event is not None:
                                yield event
                except transport_errors:
                    self.recovered = False
                    if gap_start is None:
                        gap_start = last_received
                    if failures >= self.max_reconnects:
                        raise StreamDisconnected('stream reconnect budget exhausted') from None
                    delay = min(self.backoff, self.max_backoff)
                    for _ in range(failures):
                        delay = min(delay * 2, self.max_backoff)
                    failures += 1
                    await self.sleep(delay)
        finally:
            self.recovered = False
