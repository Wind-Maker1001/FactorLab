"""Offline by default. Public-only bounded WebSocket sessions, no trading client."""
import argparse
import asyncio
from datetime import datetime, timezone
from decimal import DecimalException
import json
import sqlite3
from pathlib import Path
import time

from factorlab.errors import DataQualityError
from factorlab.integrations.binance_equity import WS_HOST
from factorlab.integrations.binance_equity.stream import connect, subscriptions
from factorlab.integrations.binance_futures_trading.market import URL, parse
from .contracts import Market
from .realtime import RealtimeAccount, equity_events, PersistenceConflict


async def observe(account, *, seconds, network=False, reconnects=3, connector=connect, proxy=True):
    if not network:
        raise PermissionError('public market data requires explicit opt-in')
    if not 0 < seconds <= 86400 or not 0 <= reconnects <= 10:
        raise ValueError('bounded duration and reconnect budget required')
    from websockets.exceptions import ConnectionClosed, InvalidHandshake
    futures = account.market == Market.BINANCE_USDM
    url = URL if futures else WS_HOST + '/stream'
    deadline, failures, messages, accepted = time.monotonic() + seconds, 0, 0, 0
    while time.monotonic() < deadline:
        try:
            async with connector(url, proxy=proxy, open_timeout=min(10, max(.01, deadline - time.monotonic())),
                                 close_timeout=1, ping_interval=5, ping_timeout=5,
                                 max_size=65536, max_queue=8) as socket:
                if not futures:
                    await socket.send(json.dumps(dict(method='SUBSCRIBE',
                        params=subscriptions('NVDA', ('quote', 'calendar'), '5m'), id=1)))
                while (remaining := deadline - time.monotonic()) > 0:
                    try:
                        message = await asyncio.wait_for(socket.recv(), min(5, remaining))
                    except TimeoutError:
                        account.tick()
                        continue
                    received = datetime.now(timezone.utc)
                    value = json.loads(message)
                    if not isinstance(value, dict):
                        raise ValueError('public market message must be an object')
                    value = value.get('data', value)
                    if not isinstance(value, dict):
                        raise ValueError('public market data must be an object')
                    if 'id' in value:
                        if value.get('result') is not None or 'code' in value:
                            raise ValueError('public subscription rejected')
                        continue
                    messages += 1
                    events = [parse(value, received)] if futures else equity_events(value, received)
                    for event in events:
                        accepted += int(account.event(event))
                break
        except (OSError, TimeoutError, ConnectionClosed, InvalidHandshake) as error:
            if account.conflict:
                return dict(messages=messages, accepted=accepted, failures=failures, error=type(error).__name__)
            failures += 1
            account.disconnect(type(error).__name__)
            if failures > reconnects:
                break
        except (ValueError, KeyError, TypeError, DecimalException, DataQualityError,
                PersistenceConflict, sqlite3.Error) as error:
            if not account.conflict:
                account.disconnect(type(error).__name__)
            return dict(messages=messages, accepted=accepted, failures=failures, error=type(error).__name__)
    if not account.conflict:
        account.disconnect('OBSERVATION_ENDED')
    return dict(messages=messages, accepted=accepted, failures=failures)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir', type=Path, required=True)
    source = parser.add_mutually_exclusive_group()
    source.add_argument('--replay', type=Path)
    source.add_argument('--public-market-data', action='store_true')
    parser.add_argument('--paper', action='store_true', help='enable strategy and local paper execution')
    parser.add_argument('--observations', type=Path, help='fresh rule/session observations for public mode')
    parser.add_argument('--seconds', type=float, default=30)
    parser.add_argument('--reconnects', type=int, default=3)
    args = parser.parse_args()
    if args.observations and not args.public_market_data:
        parser.error('--observations requires --public-market-data')
    args.state_dir.mkdir(parents=True, exist_ok=True)
    now = [datetime.now(timezone.utc)]
    clock = (lambda: datetime.now(timezone.utc)) if args.public_market_data else lambda: now[0]
    accounts = {}
    try:
        for market, name in ((Market.BINANCE_USDM, 'futures'), (Market.BINANCE_EQUITY, 'stocks')):
            accounts[name] = RealtimeAccount(args.state_dir / (name + '.sqlite'), market, clock=clock,
                data_mode='public' if args.public_market_data else 'offline')
            if args.paper:
                accounts[name].control('start')
        network_results = {}
        if args.public_market_data:
            if args.observations:
                with args.observations.open(encoding='utf-8') as source:
                    for line in iter(lambda: source.readline(65537), ''):
                        if len(line) > 65536:
                            raise ValueError('observation exceeds metadata budget')
                        value = json.loads(line)
                        if value['kind'] not in {'rules', 'session'} or 'SYNTHETIC' in value.get('source', '').upper():
                            raise ValueError('public bootstrap accepts only nonsynthetic rules/session observations')
                        account = accounts['futures' if value['symbol'] == 'NVDAUSDT' else 'stocks']
                        account.event(value)
            async def run():
                values = await asyncio.gather(*(observe(account, seconds=args.seconds, network=True,
                    reconnects=args.reconnects) for account in accounts.values()))
                return dict(zip(accounts, values))
            network_results = asyncio.run(run())
        elif args.replay:
            with args.replay.open(encoding='utf-8') as source:
                for line in iter(lambda: source.readline(65537), ''):
                    if len(line) > 65536:
                        raise ValueError('replay event exceeds metadata budget')
                    value = json.loads(line)
                    now[0] = datetime.fromisoformat(value['received'])
                    account = accounts['futures' if value['symbol'] == 'NVDAUSDT' else 'stocks']
                    if value['kind'] == 'control':
                        account.control(value['action'])
                    else:
                        account.event(value)
        print(json.dumps(dict(accounts={key: account.health() for key, account in accounts.items()},
                              public_observation=network_results), default=str, indent=2))
    finally:
        for account in accounts.values():
            account.close()


if __name__ == '__main__':
    main()
