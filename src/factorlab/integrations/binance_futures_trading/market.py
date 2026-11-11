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


URL = 'wss://fstream.binance.com/stream?streams=nvdausdt@aggTrade/nvdausdt@markPrice@1s'
