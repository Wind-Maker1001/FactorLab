"""UTC market-data normalization; perpetual contracts are not stock sessions."""
from decimal import Decimal, InvalidOperation
import re

import pandas as pd

from factorlab.errors import DataQualityError

BOUNDARIES = ('NO LIVE MONEY', 'PIT_UNVERIFIED', 'HFQ_UNVERIFIED', 'FORMAL_ADMISSION_BLOCKED')
INTERVALS = {'1m': 60_000, '3m': 180_000, '5m': 300_000, '15m': 900_000,
             '30m': 1_800_000, '1h': 3_600_000, '2h': 7_200_000,
             '4h': 14_400_000, '6h': 21_600_000, '8h': 28_800_000,
             '12h': 43_200_000, '1d': 86_400_000}
BAR_COLUMNS = ['open_time', 'close_time', 'open', 'high', 'low', 'close', 'volume',
               'quote_volume', 'trade_count', 'taker_buy_volume', 'taker_buy_quote_volume']
FUNDING_COLUMNS = ['funding_time', 'funding_rate', 'mark_price', 'rate_type']
MARK_COLUMNS = ['event_time', 'mark_price', 'index_price', 'estimated_settle_price',
                'last_funding_rate', 'next_funding_time', 'interest_rate']


def utc(value):
    """ISO dates mean UTC midnight; non-date instants require an explicit zone."""
    date_only = isinstance(value, str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}', value)
    result = pd.Timestamp(value)
    if pd.isna(result) or (result.tzinfo is None and not date_only):
        raise ValueError('use an ISO UTC date or an explicitly zoned timestamp')
    return result.tz_localize('UTC') if date_only else result.tz_convert('UTC')


def milliseconds(value):
    return utc(value).value // 1_000_000


def window(start, end, interval):
    if interval not in INTERVALS:
        raise ValueError('unsupported fixed UTC interval: ' + interval)
    if utc(start).value % 1_000_000 or utc(end).value % 1_000_000:
        raise ValueError('Binance request boundaries require millisecond precision')
    start, end = milliseconds(start), milliseconds(end)
    if start >= end or start < 0:
        raise ValueError('require 0 <= start < end; end is exclusive')
    step = INTERVALS[interval]
    if start % step or end % step:
        raise ValueError('range boundaries must align with the requested UTC interval')
    return start, end, step


def symbol_name(symbol):
    if not isinstance(symbol, str) or not re.fullmatch(r'[A-Z0-9_]{1,40}', symbol):
        raise ValueError('invalid Binance symbol')
    return symbol


def discover(payload):
    """Contract class comes exclusively from exchangeInfo, not symbol spelling.

    TRADFI_PERPETUAL alone does not certify that the underlying is a stock.
    Explicit evidence-backed underlying mappings are a separate assertion.
    """
    contracts = [dict(row) for row in payload['symbols']
                 if row['contractType'] in {'TRADFI_PERPETUAL', 'TRADIFI_PERPETUAL'}
                 and row['status'] == 'TRADING' and row.get('underlyingType') == 'EQUITY']
    names = [row['symbol'] for row in contracts]
    if len(set(names)) != len(names):
        raise DataQualityError('duplicate exchangeInfo contract identity')
    for row in contracts:
        symbol_name(row['symbol'])
    return contracts


def identity(contract, mapping=None):
    symbol = symbol_name(contract['symbol'])
    result = {'instrument_id': 'BINANCE:USDM:' + symbol,
              'symbol': symbol, 'contract_type': contract['contractType'],
              'status': contract['status'], 'quote_asset': contract['quoteAsset'],
              'margin_asset': contract['marginAsset'], 'onboard_time': contract['onboardDate'],
              'underlying': None, 'underlying_status': 'UNVERIFIED', 'exchange_info': contract}
    if mapping is not None:
        if set(mapping) != {'symbol', 'security_id', 'name', 'evidence_url', 'evidence_sha256'}:
            raise ValueError('underlying mapping requires symbol, security_id, name and pinned evidence')
        if (mapping['symbol'] != symbol or not mapping['security_id'] or not mapping['name']
                or not mapping['evidence_url'].startswith('https://www.binance.com/')
                or not re.fullmatch('[0-9a-f]{64}', mapping['evidence_sha256'])):
            raise ValueError('underlying mapping must bind this contract to official pinned evidence')
        result['underlying'] = dict(mapping)
        result['underlying_status'] = 'EXPLICIT_MAPPING; NOT_PIT_VERIFIED'
    return result


def deduplicate(frame, keys):
    if frame.empty:
        return frame.reset_index(drop=True)
    unique = frame.drop_duplicates()
    if unique.duplicated(keys).any():
        raise DataQualityError('conflicting duplicate market records')
    return unique.sort_values(keys, kind='stable').reset_index(drop=True)


def _decimal(value, *, nonnegative=False):
    try:
        number = Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise DataQualityError('invalid exchange decimal') from exc
    if not number.is_finite() or (nonnegative and number < 0):
        raise DataQualityError('nonfinite or negative exchange value')
    return number


def bars(rows, start, end, step, cutoff):
    records = []
    for row in rows:
        if len(row) != 12:
            raise DataQualityError('expected 12 Binance kline fields')
        opened, closed = int(row[0]), int(row[6])
        if opened % step or closed != opened + step - 1:
            raise DataQualityError('kline is not aligned to its UTC interval')
        if not start <= opened < end:
            raise DataQualityError('kline outside requested range')
        if closed >= cutoff:
            continue
        opened_price, high, low, close = [_decimal(value, nonnegative=True) for value in row[1:5]]
        if not low <= min(opened_price, close) <= max(opened_price, close) <= high:
            raise DataQualityError('inconsistent OHLC prices')
        for value in [row[5], row[7], row[9], row[10]]:
            _decimal(value, nonnegative=True)
        if int(row[8]) < 0:
            raise DataQualityError('negative trade count')
        records.append([opened, closed, *row[1:6], row[7], int(row[8]), row[9], row[10]])
    frame = pd.DataFrame(records, columns=BAR_COLUMNS)
    for field in ['open_time', 'close_time']:
        frame[field] = pd.to_datetime(frame[field], unit='ms', utc=True)
    for field in ['open', 'high', 'low', 'close', 'volume', 'quote_volume',
                  'taker_buy_volume', 'taker_buy_quote_volume']:
        # Decimal strings preserve the exchange's precision in Parquet and replay.
        frame[field] = frame[field].astype('string')
    frame['trade_count'] = frame.trade_count.astype('int64')
    return deduplicate(frame, ['open_time'])


def price_bars(rows, start, end, step, cutoff):
    """Historical Mark/Index OHLC; native ignored fields are not trade volumes."""
    for row in rows:
        if len(row) != 12:
            raise DataQualityError('expected 12 historical price kline fields')
    prices = [[r[0], *r[1:5], '0', r[6], '0', 0, '0', '0', r[11]] for r in rows]
    return bars(prices, start, end, step, cutoff)[
        ['open_time', 'close_time', 'open', 'high', 'low', 'close']]


def funding(rows, symbol, start, end):
    records = []
    for row in rows:
        if row['symbol'] != symbol or not start <= int(row['fundingTime']) < end:
            raise DataQualityError('funding record outside requested contract/range')
        _decimal(row['fundingRate'])
        if row.get('markPrice') is not None:
            _decimal(row['markPrice'], nonnegative=True)
        kind = row.get('rateType')
        if kind is not None and kind not in {'Regular', 'Special'}:
            raise DataQualityError('unknown funding rateType; retain raw response for schema review')
        records.append([int(row['fundingTime']), row['fundingRate'], row.get('markPrice'), kind])
    frame = pd.DataFrame(records, columns=FUNDING_COLUMNS)
    frame['funding_time'] = pd.to_datetime(frame.funding_time, unit='ms', utc=True)
    for field in FUNDING_COLUMNS[1:]:
        frame[field] = frame[field].astype('string')
    return deduplicate(frame, ['funding_time', 'rate_type'])


def mark(row, symbol):
    if row['symbol'] != symbol:
        raise DataQualityError('premiumIndex returned a different contract')
    for field in ['markPrice', 'indexPrice', 'estimatedSettlePrice']:
        if row.get(field) is not None:
            _decimal(row[field], nonnegative=True)
    for field in ['lastFundingRate', 'interestRate']:
        if row.get(field) is not None:
            _decimal(row[field])
    frame = pd.DataFrame([[row['time'], row['markPrice'], row['indexPrice'],
                           row.get('estimatedSettlePrice'), row.get('lastFundingRate'),
                           row.get('nextFundingTime'), row.get('interestRate')]], columns=MARK_COLUMNS)
    for field in ['event_time', 'next_funding_time']:
        frame[field] = pd.to_datetime(frame[field], unit='ms', utc=True)
    for field in set(MARK_COLUMNS) - {'event_time', 'next_funding_time'}:
        frame[field] = frame[field].astype('string')
    return frame


def paginate(client, endpoint, *, symbol, start, end, archive, interval=None):
    """Inclusive Binance endTime is derived from our exclusive end boundary."""
    cursor, output = start, []
    key = 'fundingTime' if endpoint == 'fundingRate' else 0
    limit = 1500 if endpoint == 'klines' else 1000
    while cursor < end:
        identity_key = 'pair' if endpoint == 'indexPriceKlines' else 'symbol'
        params = {identity_key: symbol, 'startTime': cursor, 'endTime': end - 1, 'limit': limit}
        if interval is not None:
            params['interval'] = interval
        page = client.get(endpoint, params, archive=archive, cache=True)
        if not isinstance(page, list):
            raise DataQualityError('expected market-data page array')
        if not page:
            break
        times = [int(row[key]) for row in page]
        if times != sorted(times) or min(times) < start or max(times) >= end:
            raise DataQualityError('unordered or out-of-range market-data page')
        # A full funding page may split Regular/Special events at one timestamp.
        # Re-request that timestamp before advancing beyond it.
        next_cursor = max(times) if endpoint == 'fundingRate' and len(page) == limit else max(times) + 1
        if next_cursor <= cursor:
            raise DataQualityError('market-data pagination did not advance')
        output.extend(page)
        cursor = next_cursor
        # Do not stop on a short page: empty or reaching end proves termination.
    return output


def completeness(frame, start, end, step, cutoff, onboard):
    expected_start = max(start, ((onboard + step - 1) // step) * step)
    completed_end = min(end, (cutoff // step) * step)
    observed = set(frame.open_time.astype('int64') // 1_000_000)
    missing = []
    gap_start = None
    for value in range(expected_start, completed_end, step):
        if value not in observed and gap_start is None:
            gap_start = value
        if value in observed and gap_start is not None:
            missing.append([gap_start, value])
            gap_start = None
    if gap_start is not None:
        missing.append([gap_start, completed_end])
    iso = lambda value: pd.Timestamp(value, unit='ms', tz='UTC').isoformat()
    return dict(expected_closed_bars=max(0, (completed_end - expected_start) // step),
                observed_closed_bars=len(frame),
                missing_ranges=[{'start': iso(a), 'end_exclusive': iso(b), 'bars': (b-a)//step}
                                for a, b in missing],
                pre_onboard_excluded=end <= onboard or start < onboard,
                unclosed_or_future_excluded=end > completed_end,
                calendar='BINANCE_PERPETUAL_UTC_BUCKETS; NOT_SPOT_SESSIONS',
                complete=not missing,
                funding_coverage='OBSERVED_EVENTS_ONLY; NO_ASSUMED_SCHEDULE; NO_FILL')
