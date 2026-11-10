"""Read-only CLI; live access is an explicit option, replay is always offline."""
import argparse
import asyncio
import json
from pathlib import Path

from factorlab.errors import DataQualityError
from . import BOUNDARIES, INTERVALS
from .store import Reader, Sink, TABLES


def main(argv=None):
    parser = argparse.ArgumentParser(description='Binance stock-spot data. ' + ' / '.join(BOUNDARIES))
    commands = parser.add_subparsers(dest='command', required=True)
    for command in ('discover', 'quote', 'stream', 'smoke'):
        sub = commands.add_parser(command)
        sub.add_argument('--allow-network', action='store_true')
        sub.add_argument('--output', type=Path, required=True, help='new snapshot directory')
        if command != 'discover':
            sub.add_argument('--symbol', default='NVDA')
        else:
            sub.add_argument('--symbol')
        if command in ('stream', 'smoke'):
            sub.add_argument('--seconds', type=float, default=60)
            sub.add_argument('--interval', choices=INTERVALS, default='5m')
            sub.add_argument('--streams', nargs='+', choices=('price', 'quote', 'kline', 'calendar'),
                             default=['price', 'quote', 'kline', 'calendar'])
    replay = commands.add_parser('replay')
    replay.add_argument('--input', type=Path, required=True)
    replay.add_argument('--table', choices=TABLES, default='quotes')
    args = parser.parse_args(argv)
    try:
        if args.command == 'replay':
            reader = Reader(args.input)
            count = reader.verify_raw()
            frame = reader.read(args.table).data
            print(json.dumps(dict(boundaries=BOUNDARIES, raw_receipts=count, table=args.table,
                                  rows=json.loads(frame.to_json(orient='records')))))
            return 0
        if not args.allow_network:
            parser.error('live data requires --allow-network')
        from .client import Client, symbol
        if args.symbol is not None:
            symbol(args.symbol)
        client = Client(network=True) if args.command != 'stream' else None
        if args.command in ('stream', 'smoke'):
            from .stream import collect, subscriptions
            subscriptions(args.symbol, args.streams, args.interval)
            if args.seconds <= 0:
                parser.error('--seconds must be positive')
        sink = Sink(args.output)
        try:
            if args.command in ('discover', 'smoke'):
                rows = client.discover(sink, args.symbol if args.command == 'discover' else None)
                sink.capabilities['discovery'] = dict(status='OBSERVED', symbols=len(rows))
            if args.command in ('quote', 'smoke'):
                rows = client.quote(sink, args.symbol)
                sink.capabilities['rest_quote'] = dict(status=rows[0]['availability'])
            if args.command in ('stream', 'smoke'):
                asyncio.run(collect(sink, args.symbol, kinds=args.streams, interval=args.interval,
                                    seconds=args.seconds, network=True))
        except BaseException:
            sink.close(state='INTERRUPTED_OR_FAILED')
            raise
        reader = sink.close()
        print(json.dumps(dict(boundaries=BOUNDARIES, state=reader.manifest['state'],
                              data_version=reader.manifest['data_version'],
                              raw_receipts=reader.verify_raw(), capabilities=sink.capabilities,
                              rows={name: sum(part['rows'] for part in parts)
                                    for name, parts in sink.parts.items()})))
        return 0
    except (DataQualityError, ValueError, OSError) as error:
        # Do not print third-party transport exceptions or credential-bearing locals.
        if isinstance(error, (DataQualityError, ValueError)):
            parser.exit(2, str(error) + '\n')
        parser.exit(2, 'Equity snapshot I/O failed\n')


if __name__ == '__main__':
    main()
