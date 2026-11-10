"""Independent read-only CLI. Networking requires --allow-network."""
import argparse
import json
from pathlib import Path

from factorlab.data.evidence import json_bytes
from .client import PublicClient, powershell_transport, urllib_transport
from .collector import Collector
from .market import BOUNDARIES, INTERVALS
from .store import Reader


def main(argv=None):
    parser = argparse.ArgumentParser(description='Binance TradFi public market data — NO LIVE MONEY')
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ['discover', 'collect', 'update']:
        command = commands.add_parser(name)
        command.add_argument('--allow-network', action='store_true', required=True)
        command.add_argument('--transport', choices=['urllib', 'powershell'], default='urllib')
        command.add_argument('--timeout', type=int, default=30)
        command.add_argument('--output', type=Path, required=True)
        if name != 'discover':
            command.add_argument('--symbol', required=True)
            command.add_argument('--start', required=True, help='inclusive UTC ISO date or zoned instant')
            command.add_argument('--end', required=True, help='exclusive UTC ISO date or zoned instant')
            command.add_argument('--interval', choices=INTERVALS, default='1d')
            command.add_argument('--underlying-mapping', type=Path)
            command.add_argument('--underlying-evidence', type=Path)
    replay = commands.add_parser('replay')
    replay.add_argument('--output', type=Path, required=True)
    replay.add_argument('--table', choices=['bars', 'funding', 'mark'], default='bars')
    args = parser.parse_args(argv)
    if args.command == 'replay':
        reader = Reader(args.output)
        result = reader.read(args.table)
        print(result.data.to_json(orient='records', date_format='iso'))
        print(json.dumps({'data_version': result.metadata.data_version,
                          'raw_receipts_verified': reader.verify_raw(), 'boundaries': BOUNDARIES}))
        return 0
    client = PublicClient(transport=powershell_transport if args.transport == 'powershell'
                          else urllib_transport, timeout=args.timeout)
    collector = Collector(client)
    if args.command == 'discover':
        contracts = collector.discover(archive=args.output)
        print(json_bytes({'contracts': contracts, 'boundaries': BOUNDARIES}).decode(), end='')
    else:
        mapping = json.loads(args.underlying_mapping.read_text()) if args.underlying_mapping else None
        reader = collector.collect(symbol=args.symbol, start=args.start, end=args.end,
                                   interval=args.interval, output=args.output,
                                   update=args.command == 'update', underlying_mapping=mapping,
                                   underlying_evidence=args.underlying_evidence.read_bytes()
                                   if args.underlying_evidence else None)
        print(json_bytes({'data_version': reader.manifest['data_version'],
                          'generation': str(reader.root), 'integrity': reader.manifest['integrity'],
                          'boundaries': BOUNDARIES}).decode(), end='')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
