"""Unsigned public trade/mark stream; no account or execution endpoints."""
from datetime import datetime, timezone
from decimal import Decimal


def parse(value, received):
    value = value.get('data', value)
    if value['s'] != 'NVDAUSDT':
        raise ValueError('unexpected futures symbol')
    kind = {'aggTrade': 'trade', 'markPriceUpdate': 'mark'}[value['e']]
    source_ms = value['T'] if kind == 'trade' else value['E']
    if type(source_ms) is not int or source_ms < 0 or type(value['E']) is not int:
        raise ValueError('invalid public event timestamp')
    source = datetime.fromtimestamp(source_ms / 1000, timezone.utc)
    emitted = datetime.fromtimestamp(value['E'] / 1000, timezone.utc)
    if not source <= emitted <= received:
        raise ValueError('unverified future public timestamp')
    price = Decimal(value['p'])
    if not price.is_finite() or price <= 0:
        raise ValueError('invalid public price')
    result = dict(kind=kind, symbol='NVDAUSDT', price=str(price), time=source,
                  received=received, emitted=emitted)
    if kind == 'trade':
        quantity = Decimal(value['q'])
        if type(value['a']) is not int or value['a'] < 0 or not quantity.is_finite() or quantity <= 0:
            raise ValueError('invalid public trade identity/quantity')
        result.update(size=str(quantity), sequence=value['a'])
    return result


def closed_kline(value, received, *, interval_seconds):
    """Normalize only exchange-confirmed closed UTC buckets; no trade synthesis."""
    from factorlab.trading.strategy import ClosedBar
    value = value.get('data', value)
    kline = value['k']
    if value['e'] != 'kline' or value['s'] != 'NVDAUSDT' or kline['s'] != 'NVDAUSDT':
        raise ValueError('unexpected kline identity')
    if type(kline['x']) is not bool:
        raise ValueError('explicit close confirmation required')
    if not kline['x']:
        return None
    intervals = {60: '1m', 180: '3m', 300: '5m', 900: '15m', 1800: '30m',
                 3600: '1h', 7200: '2h', 14400: '4h', 21600: '6h',
                 28800: '8h', 43200: '12h', 86400: '1d'}
    if interval_seconds not in intervals or kline['i'] != intervals[interval_seconds]:
        raise ValueError('kline frequency mismatch')
    if any(type(v) is not int or v < 0 for v in (kline['t'], kline['T'], value['E'])):
        raise ValueError('invalid kline timestamp')
    if kline['t'] % (interval_seconds * 1000) or kline['T'] + 1 - kline['t'] != interval_seconds * 1000:
        raise ValueError('invalid kline boundary')
    end = datetime.fromtimestamp((kline['T'] + 1) / 1000, timezone.utc)
    emitted = datetime.fromtimestamp(value['E'] / 1000, timezone.utc)
    if not end <= emitted <= received or (received - emitted).total_seconds() > 5:
        raise ValueError('future or stale closed kline')
    return ClosedBar('NVDAUSDT', datetime.fromtimestamp(kline['t'] / 1000, timezone.utc), end,
        *(Decimal(kline[key]) for key in ('o', 'h', 'l', 'c', 'v')),
        interval_seconds, 'BINANCE_USDM_PUBLIC_REST')


def strategy_stream_url(interval_seconds):
    intervals = {60: '1m', 180: '3m', 300: '5m', 900: '15m', 1800: '30m',
                 3600: '1h', 7200: '2h', 14400: '4h', 21600: '6h',
                 28800: '8h', 43200: '12h', 86400: '1d'}
    return URL + '/nvdausdt@kline_' + intervals[interval_seconds]


URL = 'wss://fstream.binance.com/market/stream?streams=nvdausdt@aggTrade/nvdausdt@markPrice@1s'
