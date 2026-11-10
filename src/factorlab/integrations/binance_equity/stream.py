"""Public stock market streams, bounded reconnection and explicit coverage gaps."""
import asyncio
from collections import OrderedDict
import json
import time

from factorlab.data.evidence import json_bytes, sha256, utc_now
from factorlab.errors import DataQualityError
from . import INTERVALS, WS_HOST
from .client import symbol
from .market import stream_rows


def subscriptions(ticker, kinds, interval):
    symbol(ticker)
    if not kinds or len(set(kinds)) != len(kinds) or not set(kinds) <= {'price', 'quote', 'kline', 'calendar'}:
        raise ValueError('choose distinct market streams: price, quote, kline, calendar')
    if interval not in INTERVALS:
        raise ValueError('unsupported Equity kline interval')
    names = {'price': 'price', 'quote': ticker + '@quote',
             'kline': ticker + '@kline_' + interval, 'calendar': 'calendar'}
    return [names[kind] for kind in kinds]


def connect(url, **options):
    from websockets.asyncio.client import connect as websocket_connect
    return websocket_connect(url, **options)


class Processor:
    def __init__(self, sink, ticker, kinds):
        self.sink, self.ticker, self.kinds = sink, ticker, set(kinds)
        self.seen = OrderedDict()
        self.messages = {kind: 0 for kind in kinds}
        self.duplicates = 0

    def feed(self, body, acquired, receipt):
        try:
            value = json.loads(body)
            if 'stream' in value:
                value = value['data']
            if 'id' in value:
                if value.get('result') is not None or 'code' in value:
                    raise DataQualityError('Equity subscription rejected')
                return
            event = value['e']
            if event not in self.kinds:
                raise DataQualityError('received unsubscribed event type')
            table, rows = stream_rows(value, acquired, receipt)
        except (ValueError, KeyError, TypeError, UnicodeError):
            raise DataQualityError('invalid Equity market stream payload') from None
        # Untimestamped snapshots are observations, not identifiable duplicates.
        identifiable = (event == 'price' and all(rate.get('t') is not None for rate in value['rates'])
                        or event != 'price' and (value.get('E') is not None or value.get('ts') is not None))
        if identifiable:
            identity = value
            if event == 'kline' or event == 'quote' and value.get('T') is not None:
                identity = {name: field for name, field in value.items() if name != 'E'}
            key = sha256(json_bytes(identity))
            if key in self.seen:
                self.seen.move_to_end(key)
                self.duplicates += 1
                return
            self.seen[key] = None
            if len(self.seen) > 10000:
                self.seen.popitem(last=False)
        rows = [row for row in rows if row.get('symbol', self.ticker) == self.ticker]
        if event != 'price' and event != 'calendar' and not rows:
            raise DataQualityError('stream ticker differs from subscription')
        self.messages[event] += 1
        self.sink.add(table, rows)
        observed = rows or self.sink.capabilities.get(event, {}).get('status') == 'OBSERVED'
        self.sink.capabilities[event] = dict(status='OBSERVED' if observed else 'MESSAGE_WITHOUT_TARGET',
                                             messages=self.messages[event])


async def collect(sink, ticker='NVDA', *, kinds=('price', 'quote', 'kline', 'calendar'),
                  interval='5m', seconds=60, reconnects=3, network=False,
                  connector=connect, proxy=True):
    if not network:
        raise ValueError('network access requires explicit opt-in')
    if seconds <= 0 or reconnects < 0:
        raise ValueError('invalid collection duration or reconnect limit')
    connection_errors = (OSError, TimeoutError)
    try:
        from websockets.exceptions import ConnectionClosed, InvalidHandshake
    except ModuleNotFoundError as error:
        if error.name != 'websockets' or connector is connect:
            raise
    else:
        connection_errors += (ConnectionClosed, InvalidHandshake)

    streams = subscriptions(ticker, kinds, interval)
    processor = Processor(sink, ticker, kinds)
    for kind in kinds:
        sink.capabilities[kind] = dict(status='DOCUMENTED_NOT_OBSERVED', messages=0)
    start = utc_now()
    deadline = time.monotonic() + seconds
    gap_start = last_message_at = start
    failures = 0
    sink.add('gaps', [dict(start=start, end=start, reason='OBSERVATION_START',
                           coverage='NO_HISTORICAL_BACKFILL')])
    while time.monotonic() < deadline:
        try:
            async with connector(WS_HOST + '/stream', proxy=proxy,
                                 open_timeout=min(20, max(.01, deadline - time.monotonic())),
                                 close_timeout=2, max_size=4 * 1024 * 1024) as socket:
                await socket.send(json.dumps({'method': 'SUBSCRIBE', 'params': streams, 'id': 1}))
                first_message = True
                while (remaining := deadline - time.monotonic()) > 0:
                    try:
                        message = await asyncio.wait_for(socket.recv(), timeout=remaining)
                    except TimeoutError:
                        break
                    acquired = last_message_at = utc_now()
                    if first_message:
                        sink.add('gaps', [dict(start=gap_start, end=acquired,
                                               reason='CONNECT_OR_RECONNECT_WINDOW',
                                               coverage='UNVERIFIED; NO_BACKFILL')])
                        first_message = False
                    body = message.encode('utf-8') if isinstance(message, str) else message
                    receipt = sink.raw(body, transport='WEBSOCKET', endpoint='/equity/stream',
                                       acquired_at=acquired, streams=streams)
                    processor.feed(body, acquired, receipt)
        except connection_errors as error:
            gap_start = last_message_at
            sink.add('gaps', [dict(start=gap_start, end=utc_now(), reason='CONNECTION_FAILURE',
                                   coverage='UNVERIFIED; NO_BACKFILL', error_type=type(error).__name__)])
            failures += 1
            if failures > reconnects:
                raise DataQualityError('Equity stream reconnect limit exhausted') from None
            await asyncio.sleep(min(2 ** (failures - 1), max(0, deadline - time.monotonic())))
        else:
            break
    end = utc_now()
    sink.add('gaps', [dict(start=last_message_at, end=end, reason='OBSERVATION_END',
                           coverage='NO_SEQUENCE_GUARANTEE; SEE_CAPABILITIES')])
    return dict(messages=processor.messages, duplicates=processor.duplicates, failures=failures)
