from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
import hashlib
import hmac
import json
from urllib.parse import parse_qs

import pytest

from factorlab.errors import DataQualityError
from factorlab.trading import Market, OrderIntent, OrderType, Side
from factorlab.trading.observations import acquire_rules, futures_public_rules
from factorlab.integrations.binance_equity.client import Response
from factorlab.integrations.binance_equity_trading.protocol import Rules, Unavailable
from factorlab.integrations.binance_futures_trading.fake import synthetic_rules
from factorlab.integrations.binance_futures_trading.protocol import (
    Credentials, FuturesClient, ReadOnlyNetworkTransport, SafetyBlocked, OfflineTransport,
)

NOW = datetime(2026, 11, 12, tzinfo=timezone.utc)


def stock():
    return dict(symbol='NVDA', tradability='BUY_SELL', stepSize='0.000001', minQty='0.000001',
                maxQty='1000', minNotional='1.000001', maxNotional='99999.999999',
                fractionable=True, fractionableEh=False, extendedSession=True,
                overnightSupported=False, maxNumOrders=10, multiplierDown='0.9', multiplierUp='1.1')


def test_stock_reader_preserves_exact_fields_without_authorizing_account(monkeypatch):
    monkeypatch.setenv('BINANCE_EQUITY_API_KEY', 'testkey')
    calls = []
    row = stock()
    def transport(url, headers, timeout):
        calls.append((url, timeout))
        return Response(200, json.dumps(dict(timezone='America/New_York', symbols=[row])).encode())
    value = acquire_rules(Market.BINANCE_EQUITY, 'NVDA', network=True,
                          clock=lambda: NOW, stocks_transport=transport)
    assert calls == [('https://api.binance.com/sapi/v1/equity/market/exchangeInfo?symbol=NVDA', 10)]
    assert value['payload'] == row
    parsed = value['evidence']['parsed']
    assert parsed['step'] == D('0.000001') and parsed['min_notional'] == D('1.000001')
    assert parsed['account_permission'] == parsed['payment_asset'] == parsed['settlement'] == 'UNKNOWN'
    assert value['evidence']['expires_at'] == NOW + timedelta(seconds=300)
    assert value['reference'] is None


@pytest.mark.parametrize('field', list(stock()))
def test_missing_stock_rule_fields_fail_closed(field):
    row = stock()
    del row[field]
    with pytest.raises(Unavailable, match='INCOMPLETE_SYMBOL_RULES'):
        Rules.from_exchange_info(row)


def test_public_futures_precision_comes_from_filters_not_precision_integers():
    row = dict(status='TRADING', quoteAsset='USDT', marginAsset='USDT', orderTypes=['MARKET'],
               pricePrecision=2, quantityPrecision=0, filters=[
        dict(filterType='LOT_SIZE', minQty='0.000001', maxQty='1000', stepSize='0.000001'),
        dict(filterType='MARKET_LOT_SIZE', minQty='0.000001', maxQty='1000', stepSize='0.000001'),
        dict(filterType='PRICE_FILTER', minPrice='0.0001', maxPrice='10000', tickSize='0.0001'),
        dict(filterType='MIN_NOTIONAL', notional='5.000001')])
    parsed = futures_public_rules(row)
    assert parsed['LOT_SIZE']['stepSize'] == D('0.000001')
    assert parsed['PRICE_FILTER']['tickSize'] == D('0.0001')
    assert parsed['MIN_NOTIONAL']['notional'] == D('5.000001')
    assert parsed['validity'] == 'VALID' and parsed['account_limits'] == 'UNKNOWN'
    del row['filters'][0]['stepSize']
    assert futures_public_rules(row)['validity'] == 'UNKNOWN'


def test_missing_order_types_cannot_authorize_futures():
    from dataclasses import replace
    rules = synthetic_rules()
    intent = OrderIntent(Market.BINANCE_USDM, 'NVDAUSDT', Side.BUY, OrderType.MARKET, D('1'), None, 'one')
    decision = replace(rules, order_types=None).validate(intent, mark_price=D('100'), leverage=1,
        available_margin=D('1000'), current_notional=D('0'), now=rules.observed_at)
    assert decision.reason == 'UNKNOWN_ORDER_TYPES' and not decision.approved


@pytest.mark.parametrize('path', ['/fapi/v3/account', '/fapi/v3/positionRisk',
    '/fapi/v1/leverageBracket', '/fapi/v1/openOrders', '/fapi/v1/order'])
def test_signed_get_contract_offline(path):
    class Injected(OfflineTransport):
        def __call__(self, request, timeout):
            from factorlab.integrations.binance.client import Response as FuturesResponse
            assert request.method == 'GET' and request.path == path and timeout == 10
            unsigned, signature = request.query.rsplit('&signature=', 1)
            assert hmac.compare_digest(signature, hmac.new(b'offline-secret', unsigned.encode(), hashlib.sha256).hexdigest())
            assert parse_qs(unsigned)['recvWindow'] == ['5000']
            assert parse_qs(unsigned)['timestamp'] == [str(int(NOW.timestamp() * 1000))]
            assert request.headers == {'X-MBX-APIKEY': 'offline-key'}
            assert 'offline-secret' not in repr(request) and 'signature' not in repr(request)
            return FuturesResponse(200, {}, b'[]')
    client = FuturesClient(Credentials('offline-key', 'offline-secret'), transport=Injected(), clock=NOW.timestamp)
    assert client.request('GET', path, {'symbol': 'NVDAUSDT'}) == []


@pytest.mark.parametrize('method,path', [
    ('POST', '/fapi/v1/order'), ('DELETE', '/fapi/v1/order'),
    ('POST', '/fapi/v1/marginType'), ('POST', '/fapi/v1/positionSide/dual'),
    ('POST', '/sapi/v1/asset/transfer'), ('POST', '/sapi/v1/capital/withdraw/apply'),
    ('PUT', '/fapi/v1/leverage'), ('GET', '/sapi/v1/capital/withdraw/apply'),
])
def test_real_mutations_blocked_before_network(monkeypatch, method, path):
    def forbidden(*_):
        pytest.fail('real network opener must never be constructed')
    monkeypatch.setattr('factorlab.integrations.binance_futures_trading.protocol.build_opener', forbidden)
    client = FuturesClient(Credentials('offline-key', 'offline-secret'),
                           transport=ReadOnlyNetworkTransport(permit_account_read=True))
    with pytest.raises(SafetyBlocked):
        client.request(method, path)


@pytest.mark.parametrize('status,body', [(500, b'{}'), (200, b'{broken'), (200, b'{}')])
def test_stock_rule_endpoint_failures_remain_unavailable(monkeypatch, status, body):
    monkeypatch.setenv('BINANCE_EQUITY_API_KEY', 'testkey')
    with pytest.raises((DataQualityError, KeyError)):
        acquire_rules(Market.BINANCE_EQUITY, 'NVDA', network=True,
                      stocks_transport=lambda *_: Response(status, body))
