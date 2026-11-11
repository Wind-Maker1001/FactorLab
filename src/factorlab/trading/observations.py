"""Bounded current-rule acquisition; receipts live in the existing paper snapshot."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from factorlab.errors import DataQualityError
from factorlab.integrations.binance.client import PublicClient, HOST, urllib_transport
from factorlab.integrations.binance_equity.client import Client, transport as equity_transport
from factorlab.integrations.binance_equity import REST_HOST, SOURCE_VERSION
from factorlab.integrations.binance_equity_trading.protocol import Rules, Unavailable
from .contracts import Market

FUTURES_VERSION = 'binance-usds-futures-openapi-1.0.0@f75b4a52c5aebf52b1c6ad671b0ce53b96f8e179'


def futures_public_rules(row):
    filters = {f['filterType']: f for f in row.get('filters', [])}
    fields = {'LOT_SIZE': ('minQty', 'maxQty', 'stepSize'),
              'MARKET_LOT_SIZE': ('minQty', 'maxQty', 'stepSize'),
              'PRICE_FILTER': ('minPrice', 'maxPrice', 'tickSize'),
              'MIN_NOTIONAL': ('notional',)}
    parsed = {}
    complete = row.get('status') == 'TRADING' and row.get('quoteAsset') == row.get('marginAsset') == 'USDT'
    for kind, keys in fields.items():
        rule = filters.get(kind, {})
        parsed[kind] = {k: None if rule.get(k) is None else Decimal(str(rule[k])) for k in keys}
        complete &= all(v is not None and v.is_finite() and v >= 0 for v in parsed[kind].values())
    parsed.update(status=row.get('status', 'UNKNOWN'), order_types=row.get('orderTypes'),
                  validity='VALID' if complete and row.get('orderTypes') else 'UNKNOWN',
                  account_limits='UNKNOWN', trading_schedule='UNKNOWN',
                  session='TRADING' if row.get('status') == 'TRADING' else 'UNKNOWN')
    return parsed


class MemoryReceipt:
    """Use the market reader's receipt contract without a second data store."""
    def raw(self, body, **metadata):
        self.body = body
        self.metadata = metadata
        return 'paper-runtime'


def acquire_rules(market, symbol, *, network=False, clock=lambda: datetime.now(timezone.utc),
                  futures_transport=urllib_transport, stocks_transport=equity_transport):
    if not network:
        raise PermissionError('public rule retrieval requires explicit opt-in')
    started = clock()
    if market == Market.BINANCE_USDM:
        client = PublicClient(transport=futures_transport, timeout=10, retries=0,
                              max_requests=1, max_seconds=30)
        raw = client.get('exchangeInfo')
        row = next((r for r in raw['symbols'] if r['symbol'] == symbol), None)
        if row is None:
            raise Unavailable('UNKNOWN_SYMBOL_RULES')
        # Public exchangeInfo does not authenticate an account's leverage brackets.
        payload = dict(symbol=row, bracket=None)
        endpoint, version = HOST + '/fapi/v1/exchangeInfo', FUTURES_VERSION
        official_transport = futures_transport is urllib_transport
        parsed = futures_public_rules(row)
    else:
        sink = MemoryReceipt()
        client = Client(network=True, request=stocks_transport, timeout=10)
        raw, _, _ = client._get('exchangeInfo', symbol, sink)
        row = next((r for r in raw['symbols'] if r['symbol'] == symbol), None)
        if row is None:
            raise Unavailable('UNKNOWN_SYMBOL_RULES')
        payload = row
        try:
            rules = Rules.from_exchange_info(row)
        except (KeyError, ValueError, DataQualityError):
            parsed = dict(validity='UNKNOWN')
        else:
            from dataclasses import asdict
            parsed = dict(asdict(rules), validity='VALID' if rules.tradability in {'BUY_SELL', 'BUY', 'SELL'} else 'UNKNOWN')
        parsed.update(account_permission='UNKNOWN', payment_asset='UNKNOWN', settlement='UNKNOWN',
                      order_types=['MARKET', 'LIMIT'], price_tick=Decimal('0.01'), session='UNKNOWN')
        endpoint, version = REST_HOST + '/sapi/v1/equity/market/exchangeInfo', SOURCE_VERSION
        official_transport = stocks_transport is equity_transport
    received = clock()
    return dict(kind='rules', symbol=symbol, time=received, received=received, source=endpoint,
                payload=payload, reference=None, evidence=dict(endpoint=endpoint, version=version,
                requested_at=started, fetched_at=received, expires_at=received + timedelta(seconds=300),
                source_time=raw.get('serverTime'),
                provenance='OFFICIAL_TRANSPORT' if official_transport else 'OFFLINE_INJECTED',
                timestamp_basis='LOCAL_FETCH', raw=raw, parsed=parsed))
