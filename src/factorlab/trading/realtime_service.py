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
    connection_errors = (OSError, TimeoutError)
    try:
        from websockets.exceptions import ConnectionClosed, InvalidHandshake
    except ModuleNotFoundError as error:
        if error.name != 'websockets' or connector is connect:
            raise
    else:
        connection_errors += (ConnectionClosed, InvalidHandshake)
    futures = account.market == Market.BINANCE_USDM
    url = URL if futures else WS_HOST + '/stream'
    deadline, failures, messages, accepted = time.monotonic() + seconds, 0, 0, 0
    diagnostics = dict(connection_attempts=0, handshakes=0, frames=0, receive_timeouts=0,
        subscription='URL_ROUTED' if futures else 'NOT_SENT', parsed=0, rejected=0,
        strategy_enabled=account.state.runtime['status'] == 'RUNNING',
        event_kinds={}, rejection_reasons={}, errors={}, started=datetime.now(timezone.utc).isoformat(),
        first_source=None, last_source=None)
    stage = 'connection'

    def result(error=None):
        diagnostics['ended'] = datetime.now(timezone.utc).isoformat()
        diagnostics['status'] = ('CONNECTION_FAILED' if not diagnostics['handshakes'] else
            'NO_MARKET_MESSAGES' if not messages else 'VALID_MARKET_DATA' if accepted else 'NO_ACCEPTED_EVENTS')
        runtime = account.state.runtime
        diagnostics['strategy'] = dict(status=runtime['status'], signals=runtime['signals'],
            orders=runtime['orders'], rejected=runtime['rejected'],
            last_rejection=runtime.get('last_rejection'))
        value = dict(messages=messages, accepted=accepted, failures=failures, diagnostics=diagnostics)
        if error is not None:
            value['error'] = type(error).__name__
        return value
    while time.monotonic() < deadline:
        try:
            stage = 'connection'
            diagnostics['connection_attempts'] += 1
            async with connector(url, proxy=proxy, open_timeout=min(10, max(.01, deadline - time.monotonic())),
                                 close_timeout=1, ping_interval=5, ping_timeout=5,
                                 max_size=65536, max_queue=8) as socket:
                diagnostics['handshakes'] += 1
                stage = 'subscription'
                if not futures:
                    diagnostics['subscription'] = 'SENT'
                    await socket.send(json.dumps(dict(method='SUBSCRIBE',
                        params=subscriptions('NVDA', ('quote', 'calendar'), '5m'), id=1)))
                while (remaining := deadline - time.monotonic()) > 0:
                    stage = 'receive'
                    try:
                        message = await asyncio.wait_for(socket.recv(), min(5, remaining))
                    except TimeoutError:
                        diagnostics['receive_timeouts'] += 1
                        account.tick()
                        continue
                    diagnostics['frames'] += 1
                    stage = 'parse'
                    received = datetime.now(timezone.utc)
                    value = json.loads(message)
                    if not isinstance(value, dict):
                        raise ValueError('public market message must be an object')
                    value = value.get('data', value)
                    if not isinstance(value, dict):
                        raise ValueError('public market data must be an object')
                    if 'code' in value or ('id' in value and value.get('result') is not None):
                        diagnostics['subscription'] = 'REJECTED'
                        stage = 'subscription'
                        raise ValueError('public subscription rejected')
                    if 'id' in value:
                        diagnostics['subscription'] = 'ACKNOWLEDGED'
                        continue
                    messages += 1
                    events = [parse(value, received)] if futures else list(equity_events(value, received))
                    for event in events:
                        diagnostics['parsed'] += 1
                        kind = event['kind']
                        diagnostics['event_kinds'][kind] = diagnostics['event_kinds'].get(kind, 0) + 1
                        source = event['time'].isoformat()
                        diagnostics['first_source'] = min(diagnostics['first_source'] or source, source)
                        diagnostics['last_source'] = max(diagnostics['last_source'] or source, source)
                        stage = 'validation'
                        admitted = int(account._stream_event(event, value, official_transport=connector is connect))
                        accepted += admitted
                        diagnostics['rejected'] += 1 - admitted
                        if not admitted:
                            reason = account.state.runtime['reason']
                            reason = reason if reason in {'STALE_OR_FUTURE_OBSERVATION', 'QUOTE_IDENTITY_CAP'} else 'DUPLICATE_OR_OLDER_WATERMARK'
                            diagnostics['rejection_reasons'][reason] = diagnostics['rejection_reasons'].get(reason, 0) + 1
                break
        except connection_errors as error:
            key = stage + ':' + type(error).__name__
            diagnostics['errors'][key] = diagnostics['errors'].get(key, 0) + 1
            if account.conflict:
                return result(error)
            failures += 1
            account.disconnect(type(error).__name__)
            if failures > reconnects:
                break
        except (ValueError, KeyError, TypeError, DecimalException, DataQualityError,
                PersistenceConflict, sqlite3.Error) as error:
            key = stage + ':' + type(error).__name__
            diagnostics['errors'][key] = diagnostics['errors'].get(key, 0) + 1
            if not account.conflict:
                account.disconnect(type(error).__name__)
            return result(error)
    if not account.conflict:
        account.disconnect('OBSERVATION_ENDED')
    return result()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir', type=Path, required=True)
    source = parser.add_mutually_exclusive_group()
    source.add_argument('--replay', type=Path)
    source.add_argument('--public-market-data', action='store_true')
    parser.add_argument('--paper', action='store_true', help='enable strategy and local paper execution')
    parser.add_argument('--observations', type=Path, help='unverified rule/session inputs; cannot authorize public paper orders')
    parser.add_argument('--refresh-rules', action='store_true', help='fetch current official rules once per market')
    parser.add_argument('--seconds', type=float, default=30)
    parser.add_argument('--reconnects', type=int, default=3)
    args = parser.parse_args()
    if (args.observations or args.refresh_rules) and not args.public_market_data:
        parser.error('--observations and --refresh-rules require --public-market-data')
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
            if args.refresh_rules:
                for account in accounts.values():
                    try:
                        account.refresh_rules(network=True)
                    except (DataQualityError, ValueError, OSError, TimeoutError, KeyError):
                        account.disconnect('RULE_REFRESH_FAILED')
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
