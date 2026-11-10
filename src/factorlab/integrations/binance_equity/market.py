"""Native stock units and event-time semantics, independent of A-share schemas."""
from decimal import Decimal, InvalidOperation
import json

from factorlab.errors import DataQualityError
from . import INTERVALS, PHASES
from .client import symbol


def decimal_text(value):
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        raise DataQualityError('expected exact decimal text or integer')
    text = str(value)
    try:
        number = Decimal(text)
    except InvalidOperation:
        raise DataQualityError('invalid decimal') from None
    if not number.is_finite() or number < 0:
        raise DataQualityError('expected finite nonnegative decimal')
    return text


def epoch(value):
    if value is not None and (type(value) is not int or value < 0):
        raise DataQualityError('invalid millisecond timestamp')
    return value


def base(ticker, acquired, receipt, event_time=None, session='UNKNOWN'):
    return dict(symbol=symbol(ticker), asset_class='STOCK_SPOT', currency='USD',
                acquired_at=acquired, raw_receipt=receipt, event_time=epoch(event_time),
                session=session)


def discovery(value, acquired, receipt):
    if not isinstance(value, dict) or not isinstance(value.get('symbols'), list):
        raise DataQualityError('exchangeInfo must contain a symbols array')
    rows = []
    for item in value['symbols']:
        row = base(item['symbol'], acquired, receipt, item.get('tradabilityUpdateTime'))
        row.update(rule_observed_at=acquired, historical_rule_effective_at=None,
                   timezone=value.get('timezone'), rules_json=json.dumps(item, sort_keys=True))
        for key in ('tradability', 'overnightSupported', 'fractionable', 'fractionableEh',
                    'extendedSession', 'listingTime', 'delistingTime'):
            row[key] = item.get(key)
        for key in ('stepSize', 'minQty', 'maxQty', 'minNotional', 'maxNotional',
                    'multiplierUp', 'multiplierDown'):
            row[key] = decimal_text(item[key]) if item.get(key) is not None else None
        rows.append(row)
    return rows


def quote_fields(row, value, names):
    for target, source in zip(('bid_price', 'ask_price', 'bid_size', 'ask_size'), names):
        row[target] = decimal_text(value[source])
    return row


def rest_quote(value, ticker, acquired, receipt):
    if value is None:
        return [dict(base(ticker, acquired, receipt), availability='NO_QUOTE',
                     bid_price=None, ask_price=None, bid_size=None, ask_size=None,
                     server_event_time=None)]
    if value['symbol'] != ticker:
        raise DataQualityError('quote ticker differs from requested ticker')
    row = base(ticker, acquired, receipt)
    row.update(availability='AVAILABLE', server_event_time=None)
    return [quote_fields(row, value, ('bidPrice', 'askPrice', 'bidSize', 'askSize'))]


def stream_rows(value, acquired, receipt):
    event = value['e']
    if event == 'price':
        rows = []
        for rate in value['rates']:
            phase = rate.get('mp')
            row = base(rate['s'], acquired, receipt, rate.get('t'), PHASES.get(phase, 'UNKNOWN'))
            row.update(price=decimal_text(rate['p']), source_phase=phase,
                       previous_regular_close=(decimal_text(rate['pc']) if rate.get('pc') is not None else None),
                       today_regular_close=(decimal_text(rate['tc']) if rate.get('tc') is not None else None))
            rows.append(row)
        return 'prices', rows
    if event == 'quote':
        row = base(value['s'], acquired, receipt, value.get('T'))
        row.update(availability='AVAILABLE', server_event_time=epoch(value['E']))
        return 'quotes', [quote_fields(row, value, ('bp', 'ap', 'bs', 'as'))]
    if event == 'kline':
        candle = value['k']
        if candle['s'] != value['s'] or candle['i'] not in INTERVALS or type(candle['x']) is not bool:
            raise DataQualityError('invalid Equity candle identity or closure flag')
        row = base(value['s'], acquired, receipt, value['E'])
        row.update(open_time=epoch(candle['t']), close_time=epoch(candle['ct']),
                   interval=candle['i'], closed=candle['x'], trade_count=candle.get('n'))
        if row['open_time'] > row['close_time']:
            raise DataQualityError('candle closes before its open time')
        for name, key in (('open', 'o'), ('high', 'h'), ('low', 'l'), ('close', 'c'), ('volume', 'v')):
            row[name] = decimal_text(candle[key])
        if not Decimal(row['low']) <= min(Decimal(row['open']), Decimal(row['close'])) <= max(Decimal(row['open']), Decimal(row['close'])) <= Decimal(row['high']):
            raise DataQualityError('invalid candle OHLC range')
        row['vwap'] = decimal_text(candle['vw']) if candle.get('vw') is not None else None
        return ('bars' if candle['x'] else 'open_bars'), [row]
    if event == 'calendar':
        return 'sessions', [dict(event_time=epoch(value['ts']), acquired_at=acquired,
                                 raw_receipt=receipt, previous_phase=value['from'],
                                 source_phase=value['to'], session=PHASES.get(value['to'], 'UNKNOWN'))]
    raise DataQualityError('unsupported Equity market event')
