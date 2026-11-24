"""Explicit, read-only online acceptance: python -m ...acceptance --help."""
import argparse
import asyncio
from datetime import datetime, timedelta, timezone, time
from zoneinfo import ZoneInfo
import json
from pathlib import Path

import pandas as pd

from factorlab.cross_market.calendar import us_calendar
from .sessions import AlpacaSessionCalendar, diagnose_clock
from .client import Credentials, MarketDataClient, MarketDataError
from .quality import audit_bars, adjustment_relationship, regular_session_aggregates


def calendar_from_response(rows, start, end):
    return AlpacaSessionCalendar(rows, coverage_start=start, coverage_end=end, version='ALPACA:v2/calendar')


async def probe_stream(credentials, feed, symbols, seconds):
    """Disconnect a real subscription once; recover raw trades/quotes via REST."""
    from .stream import AlpacaStream, connect
    observations, gaps, connections = [], [], []

    class InterruptSocket:
        def __init__(self, socket, interrupt):
            self.socket, self.interrupt = socket, interrupt
            self.saw_event = False
        async def send(self, value):
            return await self.socket.send(value)
        async def recv(self):
            if self.interrupt and self.saw_event:
                self.interrupt = False
                await self.socket.close()
                raise ConnectionError('acceptance intentional disconnect')
            value = await self.socket.recv()
            self.saw_event = any(x.get('T') in {'q', 't'} for x in json.loads(value))
            return value

    class Connection:
        def __init__(self, url):
            self.context = connect(url)
        async def __aenter__(self):
            socket = await self.context.__aenter__()
            connections.append(datetime.now(timezone.utc).isoformat())
            return InterruptSocket(socket, len(connections) == 1)
        async def __aexit__(self, *args):
            return await self.context.__aexit__(*args)

    async def recover_gap(*, start, end, feed, subscriptions):
        if feed not in MarketDataClient.feeds:
            gaps.append({'status': 'UNSUPPORTED_REST_FEED', 'feed': feed})
            return False
        client = MarketDataClient(credentials, feed=feed)
        counts = {}
        for kind in ('quotes', 'trades'):
            rows = await asyncio.to_thread(client.history, kind, symbols,
                                          (start - timedelta(seconds=30)).isoformat(), end.isoformat())
            counts[kind] = len(rows)
        gaps.append({'start': start.isoformat(), 'end': end.isoformat(), 'counts': counts,
                     'status': 'REST_BACKFILLED_SEQUENCE_COMPLETENESS_UNVERIFIED'})
        return True

    stream = AlpacaStream(api_key=credentials.key, secret_key=credentials.secret, feed=feed,
                          subscriptions={'quotes': symbols, 'trades': symbols}, connector=Connection,
                          recover_gap=recover_gap, max_reconnects=1, receive_timeout=seconds)
    status, error_code = 'TIME_WINDOW_EXPIRED', None
    try:
        async with asyncio.timeout(seconds):
            async for event in stream.events():
                observations.append({'symbol': event.symbol, 'channel': event.channel,
                                     'event_at': event.event_at, 'received_at': event.received_at.isoformat(),
                                     'source': event.source})
                if len(connections) >= 2 and gaps and set(symbols) <= {x['symbol'] for x in observations}:
                    status = 'OBSERVED_ALL_SYMBOLS_AND_RECONNECTED'
                    break
    except TimeoutError:
        pass
    except Exception as exc:
        status = type(exc).__name__
        error_code = getattr(exc, 'code', None)
    return {'status': status, 'error_code': error_code, 'connections': connections, 'events': observations, 'gaps': gaps,
            'missing_symbols': sorted(set(symbols) - {x['symbol'] for x in observations})}


def run(args):
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    credentials = Credentials.from_dpapi(args.credentials_file) if args.credentials_file else Credentials.from_env()
    now = datetime.now(timezone.utc)
    end = now.astimezone(ZoneInfo('America/New_York')).date()
    begin = end - timedelta(days=max(450, args.sessions * 2))
    client = MarketDataClient(credentials, feed=args.feed)
    calendar_rows, received = client.calendar(begin, end + timedelta(days=14))
    (output / 'calendar.json').write_text(json.dumps(calendar_rows))
    calendar = calendar_from_response(calendar_rows, begin, end + timedelta(days=14))
    completed = calendar.schedule.loc[calendar.schedule.close < pd.Timestamp(now)]
    target = completed.iloc[-args.sessions:]
    start, last = target.index[0].date(), target.index[-1].date()
    end_at = datetime.combine(last, time(23, 59, 59, 999999), ZoneInfo('America/New_York')).isoformat()
    quality, coverage, raw_by_feed, sources = {}, [], {}, []
    local = us_calendar(begin, end + timedelta(days=14))
    shared = local.schedule.index.intersection(calendar.schedule.index)
    quality['calendar'] = {'received_at': received.isoformat(),
                           'missing_local': [str(x.date()) for x in calendar.schedule.index.difference(local.schedule.index)],
                           'missing_vendor': [str(x.date()) for x in local.schedule.index.difference(calendar.schedule.index)],
                           'time_mismatches': [str(x.date()) for x in shared if any(calendar.at(x, c) != local.at(x, c) for c in ('open', 'close'))]}
    quality['clock'] = client.clock()[0]
    for feed in args.feeds:
        source = MarketDataClient(credentials, feed=feed)
        item = {'feed': feed, 'endpoints': {}}
        variants = {}
        for adjustment in ('raw', 'split', 'dividend', 'spin-off', 'all'):
            try:
                rows = source.bars(args.symbols, start, end_at, adjustment=adjustment, asof=str(end))
                variants[adjustment] = rows
                (output / f'{feed}-{adjustment}-daily.json').write_text(json.dumps(rows))
                for symbol in args.symbols:
                    coverage.append(audit_bars(rows, target, symbol=symbol, feed=feed, adjustment=adjustment))
                item['endpoints']['daily:' + adjustment] = 'HTTP_200'
            except MarketDataError as exc:
                item['endpoints']['daily:' + adjustment] = {'status': exc.status}
        raw_by_feed[feed] = variants.get('raw', [])
        if 'raw' in variants:
            quality[feed + ':adjustments'] = {key: adjustment_relationship(variants['raw'], value)
                                               for key, value in variants.items() if key != 'raw'}
        minute_start = target.index[-2].date()
        try:
            minute_start_at = datetime.combine(minute_start, time.min, ZoneInfo('America/New_York')).isoformat()
            minutes = source.bars(args.symbols, minute_start_at, end_at, timeframe='1Min', asof=str(end))
            (output / f'{feed}-raw-minute.json').write_text(json.dumps(minutes))
            minute_sessions = target.loc[str(minute_start):]
            quality[feed + ':minute'] = [audit_bars(minutes, minute_sessions, symbol=s, feed=feed,
                                                  adjustment='raw', timeframe='1Min') for s in args.symbols]
            quality[feed + ':rth_aggregates'] = regular_session_aggregates(minutes, minute_sessions)
            item['endpoints']['minutes'] = 'HTTP_200'
        except MarketDataError as exc:
            item['endpoints']['minutes'] = {'status': exc.status}
        for name, call in [('latest_quotes', lambda: source.latest('quotes', args.symbols)),
                           ('latest_trades', lambda: source.latest('trades', args.symbols)),
                           ('snapshots', lambda: source.snapshots(args.symbols))]:
            try:
                payload, received = call()
                (output / f'{feed}-{name}.json').write_text(json.dumps(payload))
                item['endpoints'][name] = {'status': 200, 'received_at': received.isoformat()}
                if name in {'latest_quotes', 'latest_trades'}:
                    channel = 'quotes' if name == 'latest_quotes' else 'trades'
                    item['endpoints'][name]['symbols'] = {
                        symbol: {'event_at': row['t'],
                                 'age_seconds_at_receipt': (pd.Timestamp(received) - pd.Timestamp(row['t'])).total_seconds()}
                        for symbol, row in payload.get(channel, {}).items() if row}
                    item['endpoints'][name]['missing_symbols'] = sorted(set(args.symbols) - set(payload.get(channel, {})))
            except MarketDataError as exc:
                item['endpoints'][name] = {'status': exc.status}
        item['requests'] = source.evidence
        sources.append(item)
    quality['streams'] = {feed: asyncio.run(probe_stream(credentials, feed, args.symbols, args.stream_seconds))
                          for feed in args.stream_feeds}
    try:
        actions = client.corporate_actions(args.symbols, begin, end)
        (output / 'corporate-actions.json').write_text(json.dumps(actions))
        quality['corporate_actions'] = 'RETRIEVED_PROCESS_DATE_FILTER_NOT_PIT_EVIDENCE'
    except MarketDataError as exc:
        quality['corporate_actions'] = {'status': exc.status}
    pd.DataFrame(coverage).to_csv(output / 'coverage.csv', index=False)
    (output / 'data_quality.json').write_text(json.dumps(quality, indent=2, default=str))
    (output / 'sources.json').write_text(json.dumps(sources, indent=2))
    (output / 'adjustment-report.md').write_text(
        '# Alpaca adjustment basis\n\nraw is retained. split adjusts prices and volume; dividend adjusts prices; '
        'spin-off adjusts spin-off prices; all includes all three. Separate response vintages are not historical '
        'knowledge evidence. PIT and HFQ remain UNVERIFIED. Original v69 equivalence is UNCONFIRMED pending '
        'the original source and adjustment convention. Corporate action process_date is not ex-date or '
        'provider publication time. SNDK and GE identity continuity requires dated security evidence; '
        'asof maps vendor symbols and does not certify continuity. No pre-listing bars are fabricated.\n')
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--credentials-file', help='Explicit Windows DPAPI SecureString XML path')
    parser.add_argument('--symbols', nargs='+', required=True)
    parser.add_argument('--feed', choices=sorted(MarketDataClient.feeds), required=True)
    parser.add_argument('--feeds', nargs='+', choices=sorted(MarketDataClient.feeds), default=['iex', 'sip'])
    parser.add_argument('--stream-feeds', nargs='+', default=['iex', 'sip', 'delayed_sip'])
    parser.add_argument('--stream-seconds', type=float, default=60)
    parser.add_argument('--sessions', type=int, default=252)
    parser.add_argument('--output', required=True, help='Ignored local evidence directory; never commit full payloads')
    args = parser.parse_args()
    if args.sessions < 1 or args.stream_seconds <= 0:
        parser.error('positive sessions and stream window required')
    print(run(args))


if __name__ == '__main__':
    main()
