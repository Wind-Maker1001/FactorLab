import json
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from factorlab.integrations.binance_futures_trading.preflight import (
    account_can_trade, inspect_eligibility,
)
from factorlab.integrations.binance_futures_trading.protocol import (
    Credentials, Environment, FuturesClient, ReadOnlyNetworkTransport, SafetyBlocked,
)

from factorlab.integrations.binance_futures_trading.quantitative import observe_quantitative
from factorlab.integrations.binance_futures_trading import demo_preflight


@pytest.mark.parametrize('flag, expected', [
    (True, True), (False, False), (None, None), (0, None), (1, None),
    ('true', None), ('false', None), ([], None), ({}, None),
])
def test_account_requires_boolean(flag, expected):
    assert account_can_trade({'canTrade': flag}) is expected


@pytest.mark.parametrize('accounts, expected', [
    (({},), None), ((None,), None), (([],), None),
    (({}, {'canTrade': True}), True),
    (({'canTrade': True}, {'canTrade': True}), True),
    (({'canTrade': False}, {'canTrade': False}), False),
    (({'canTrade': True}, {'canTrade': False}), None),
    (({'canTrade': None}, {'canTrade': True}), None),
    (({'availableBalance': '999999'},), None),
])
def test_account_missing_and_conflicting_evidence(accounts, expected):
    assert account_can_trade(*accounts) is expected


def status(symbol=False, account=False):
    return {'indicators': {
        'NVDAUSDT': [{'indicator': 'UFR', 'isLocked': symbol}],
        'ACCOUNT': [{'indicator': 'TMV', 'isLocked': account}],
    }}


@pytest.mark.parametrize('symbol, account', [(False, False), (True, False), (False, True)])
def test_observed_lock_events(symbol, account):
    observation = observe_quantitative(status(symbol, account))
    assert observation.error is None
    assert bool(observation.applicable_locks('NVDAUSDT')) is (symbol or account)
    assert observation.report('NVDAUSDT')['scopes']['ACCOUNT']['TMV'] == ('LOCKED' if account else 'NOT_LOCKED')


@pytest.mark.parametrize('payload', [None, [], {}, {'indicators': None}, {'indicators': []},
    {'indicators': {'ACCOUNT': [None]}}, {'indicators': {'ACCOUNT': [{'indicator': 'TMV', 'isLocked': 1}]}}])
def test_invalid_response_is_unknown(payload):
    observation = observe_quantitative(payload)
    assert observation.error == 'MALFORMED_RESPONSE'
    assert observation.report('NVDAUSDT')['status'] == 'UNKNOWN'


@pytest.mark.parametrize('indicators', [{}, {'NVDAUSDT': []}, {'ACCOUNT': []}])
def test_not_reported_is_not_a_lock(indicators):
    observation = observe_quantitative({'indicators': indicators})
    assert observation.error is None
    assert observation.applicable_locks('NVDAUSDT') == ()
    assert observation.report('NVDAUSDT')['scopes']['ACCOUNT'] == {'status': 'NOT_REPORTED'}


def test_conflict_retains_lock_and_reports_unknown():
    observation = observe_quantitative({'indicators': {'ACCOUNT': [
        {'indicator': 'NEW', 'isLocked': True}, {'indicator': 'NEW', 'isLocked': False}]}})
    assert observation.error == 'CONFLICTING_FIELDS'
    assert observation.applicable_locks('NVDAUSDT') == (('ACCOUNT', 'NEW'),)


def test_unknown_named_indicator_and_other_contract():
    observation = observe_quantitative({'indicators': {'BTCUSDT': [{'indicator': 'NEW', 'isLocked': True}]}})
    assert observation.error is None
    assert not observation.applicable_locks('NVDAUSDT')
    assert observation.applicable_locks('BTCUSDT')


@pytest.fixture
def readonly(monkeypatch):
    payloads = {
        '/fapi/v1/time': {'serverTime': 1000000},
        '/fapi/v2/account': {'canTrade': True, 'private': 'private-account-marker'},
        '/fapi/v1/apiTradingStatus': {'indicators': {}},
    }
    calls = []

    class Reply:
        status = 200
        headers = {}

        def __init__(self, payload):
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self, size):
            return json.dumps(self.payload).encode()

    def open_stub(request, timeout):
        parts = urlsplit(request.full_url)
        assert request.get_method() == 'GET'
        assert parts.scheme == 'https' and parts.netloc == 'demo-fapi.binance.com'
        assert timeout == 10
        query = parse_qs(parts.query)
        if parts.path != '/fapi/v1/time':
            assert {'timestamp', 'recvWindow', 'signature'} <= query.keys()
        assert 'symbol' not in query
        calls.append(parts.path)
        payload = payloads[parts.path]
        if isinstance(payload, Exception):
            raise payload
        return Reply(payload)

    def opener(*handlers):
        assert len(handlers) == 1
        assert handlers[0].redirect_request(None, None, 302, '', {}, 'https://fapi.binance.com') is None
        return SimpleNamespace(open=open_stub)

    monkeypatch.setattr('factorlab.integrations.binance_futures_trading.protocol.build_opener', opener)
    client = FuturesClient(Credentials('private-key-marker', 'private-secret-marker', Environment.DEMO),
        transport=ReadOnlyNetworkTransport(environment=Environment.DEMO, permit_account_read=True),
        environment=Environment.DEMO, clock=lambda: 1000)
    return client, payloads, calls


def test_bounded_signed_reads_and_sanitized_evidence(readonly):
    client, _, calls = readonly
    result = inspect_eligibility(client)
    assert calls == ['/fapi/v1/time', '/fapi/v2/account', '/fapi/v1/apiTradingStatus']
    assert result['verdicts'] == {'account_can_trade': 'VERIFIED_TRUE'}
    assert result['evidence']['quantitative']['scopes']['ACCOUNT'] == {'status': 'NOT_REPORTED'}
    assert result['execution_admission'] == result['formal_admission'] == result['production_writes'] == 'BLOCKED'
    assert result['network_writes'] == 0
    serialized = json.dumps(result)
    assert 'private-' not in serialized and 'signature' not in serialized


@pytest.mark.parametrize('flag, expected', [(False, 'VERIFIED_FALSE'), (None, 'UNKNOWN'), (1, 'UNKNOWN')])
def test_live_shape_account_conclusions(readonly, flag, expected):
    client, payloads, _ = readonly
    payloads['/fapi/v2/account'] = {'canTrade': flag}
    assert inspect_eligibility(client)['verdicts']['account_can_trade'] == expected


def test_account_failure_is_unknown_without_retry(readonly):
    client, payloads, calls = readonly
    payloads['/fapi/v2/account'] = OSError('private transport details')
    result = inspect_eligibility(client)
    assert calls.count('/fapi/v2/account') == 1
    assert result['verdicts']['account_can_trade'] == 'UNKNOWN'
    assert 'private' not in json.dumps(result)


def test_time_failure_stops_signed_reads(readonly):
    client, payloads, calls = readonly
    payloads['/fapi/v1/time'] = OSError('failure')
    result = inspect_eligibility(client)
    assert calls == ['/fapi/v1/time']
    assert set(result['verdicts'].values()) == {'UNKNOWN'}


def test_v2_allowlist_does_not_enable_network_writes(readonly):
    client, _, calls = readonly
    with pytest.raises(SafetyBlocked):
        client.request('POST', '/fapi/v2/account')
    with pytest.raises(SafetyBlocked):
        client.request('DELETE', '/fapi/v2/account')
    assert calls == []


def test_production_client_rejected_before_read():
    client = FuturesClient(environment=Environment.PRODUCTION,
        transport=ReadOnlyNetworkTransport(environment=Environment.PRODUCTION, permit_account_read=True))
    with pytest.raises(ValueError, match='Demo read-only'):
        inspect_eligibility(client)


def test_failed_indicator_read_is_unknown_not_not_reported(readonly):
    client, payloads, calls = readonly
    payloads['/fapi/v1/apiTradingStatus'] = TimeoutError('private details')
    observation = inspect_eligibility(client)['evidence']['quantitative']
    assert observation['status'] == 'UNKNOWN'
    assert observation['missing_indicator_state'] == 'UNKNOWN'
    assert observation['error'] == 'DataQualityError'
    assert calls.count('/fapi/v1/apiTradingStatus') == 1


@pytest.fixture
def demo_account_http(monkeypatch):
    from datetime import datetime
    from io import BytesIO

    class FrozenClock(datetime):
        @classmethod
        def now(cls, tz):
            return datetime.fromtimestamp(1794538555, tz)

    monkeypatch.setattr(demo_preflight, 'datetime', FrozenClock)
    symbol = 'NVDAUSDT'
    server_ms = 1794538555000
    payloads = {
        '/fapi/v1/time': {'serverTime': server_ms},
        '/fapi/v1/exchangeInfo': {'symbols': [{
            'symbol': symbol, 'status': 'TRADING', 'contractType': 'TRADIFI_PERPETUAL',
            'marginAsset': 'USDT', 'quoteAsset': 'USDT', 'orderTypes': ['LIMIT', 'MARKET'],
            'filters': [
                dict(filterType='LOT_SIZE', minQty='0.01', maxQty='6000', stepSize='0.01'),
                dict(filterType='MARKET_LOT_SIZE', minQty='0.01', maxQty='600', stepSize='0.01'),
                dict(filterType='MIN_NOTIONAL', notional='5'),
                dict(filterType='PRICE_FILTER', minPrice='0.01', maxPrice='20000', tickSize='0.01'),
                dict(filterType='PERCENT_PRICE', multiplierDown='0.97', multiplierUp='1.03'),
                dict(filterType='MAX_NUM_ORDERS', limit=200),
            ],
        }]},
        '/fapi/v1/leverageBracket': {'symbol': symbol, 'brackets': [
            dict(bracket=1, initialLeverage=10, notionalFloor=0, notionalCap=10000, maintMarginRatio='0.05'),
        ]},
        '/fapi/v3/account': {'assets': [dict(asset='USDT', walletBalance='1000',
            availableBalance='1000', unrealizedProfit='0', initialMargin='0')], 'positions': [],
            'private': 'PRIVATE_PAYLOAD'},
        '/fapi/v3/positionRisk': [],
        '/fapi/v1/positionSide/dual': {'dualSidePosition': False},
        '/fapi/v1/openOrders': [],
        '/fapi/v1/symbolConfig': [{'symbol': symbol, 'leverage': 20, 'marginType': 'CROSSED'}],
        '/fapi/v2/account': {'canTrade': True},
        '/fapi/v1/apiTradingStatus': {'indicators': {}},
        '/fapi/v1/premiumIndex': {'symbol': symbol, 'markPrice': '165.98', 'time': server_ms - 500},
        '/fapi/v1/ticker/bookTicker': {'symbol': symbol, 'bidPrice': '165.91', 'askPrice': '166.26', 'time': server_ms},
    }
    calls = []
    unsigned = {'/fapi/v1/time', '/fapi/v1/exchangeInfo', '/fapi/v1/premiumIndex', '/fapi/v1/ticker/bookTicker'}

    def open_stub(request, timeout):
        url = urlsplit(request.full_url)
        assert request.get_method() == 'GET'
        assert url.scheme == 'https' and url.netloc == 'demo-fapi.binance.com'
        query = parse_qs(url.query)
        assert ('signature' in query) is (url.path not in unsigned)
        calls.append(url.path)
        reply = BytesIO(json.dumps(payloads[url.path]).encode())
        reply.status, reply.headers = 200, {}
        return reply

    def client(*args, **kwargs):
        return FuturesClient(*args, **kwargs, clock=lambda: server_ms / 1000)

    monkeypatch.setattr(demo_preflight, 'FuturesClient', client)
    monkeypatch.setattr('factorlab.integrations.binance_futures_trading.protocol.build_opener',
                        lambda *handlers: SimpleNamespace(open=open_stub))
    return payloads, calls, unsigned


def test_demo_signed_contract_separates_observation_from_execution(demo_account_http):
    payloads, calls, _ = demo_account_http
    result = demo_preflight.inspect_demo(Credentials('PRIVATE_KEY', 'PRIVATE_SECRET', Environment.DEMO))
    assert calls == list(payloads)
    assert result['status'] == 'P7_M02B_DEMO_READ_ONLY_VERIFIED'
    assert result['execution_admission'] == 'BLOCKED' and result['network_writes'] == 0
    assert result['checks']['api_user_data']['value'] == 'VERIFIED'
    assert result['checks']['api_key_trade']['value'] == 'UNKNOWN'
    assert result['checks']['account_can_trade']['value'] is True
    assert result['checks']['quantitative']['value']['scopes']['NVDAUSDT'] == {'status': 'NOT_REPORTED'}
    assert result['checks']['leverage']['value'] == 20 and result['checks']['leverage']['eligible'] is False
    assert result['minimum_opening_orders']['LIMIT']['hypothetical']
    assert str(result['minimum_opening_orders']['LIMIT']['quantity']) == '0.04'
    serialized = json.dumps(result, default=str)
    assert 'PRIVATE_' not in serialized and 'signature' not in serialized


@pytest.mark.parametrize('outcome, state', [(-2013, 'ABSENT'), (-2015, 'UNKNOWN'), (None, 'PRESENT')])
def test_demo_exact_identity_is_get_only(demo_account_http, monkeypatch, outcome, state):
    from factorlab.integrations.binance_futures_trading.protocol import ExchangeError
    original = FuturesClient.request
    identities = []

    def request(self, method, path, params=None, **kwargs):
        if path == '/fapi/v1/order':
            identities.append((method, params))
            if outcome is not None:
                raise ExchangeError(outcome, 400, 'ORDER_NOT_FOUND' if outcome == -2013 else 'AUTH')
            return {'status': 'NEW'}
        return original(self, method, path, params, **kwargs)

    monkeypatch.setattr(FuturesClient, 'request', request)
    result = demo_preflight.inspect_demo(Credentials('k', 's', Environment.DEMO), client_order_id='m04-test-01')
    check = result['checks']['order_identity']
    assert check['value'] == {'clientOrderId': 'm04-test-01', 'state': state}
    assert check['eligible'] is (state == 'ABSENT')
    assert identities == [('GET', {'symbol': 'NVDAUSDT', 'origClientOrderId': 'm04-test-01'})]
    assert result['network_writes'] == 0


def test_demo_invalid_identity_rejected_before_requests(demo_account_http):
    _, calls, _ = demo_account_http
    with pytest.raises(ValueError, match='identity'):
        demo_preflight.inspect_demo(client_order_id='invalid identity')
    assert calls == []


def test_missing_demo_credentials_leave_signed_facts_unknown(demo_account_http):
    _, calls, unsigned = demo_account_http
    result = demo_preflight.inspect_demo()
    assert set(calls) == unsigned
    assert result['status'] == 'P7_M02B_CREDENTIALS_NOT_CONFIGURED'
    assert result['checks']['account_id']['value'] == 'UNKNOWN'
    assert result['checks']['leverage_brackets']['value'] == 'UNKNOWN'


def test_demo_contract_does_not_infer_flat_from_malformed_positions(demo_account_http):
    payloads, _, _ = demo_account_http
    payloads['/fapi/v3/account']['positions'] = None
    payloads['/fapi/v1/apiTradingStatus'] = {'indicators': {'NVDAUSDT': [
        {'indicator': 'UFR', 'isLocked': True}, {'indicator': 'UFR', 'isLocked': False},
    ]}}
    result = demo_preflight.inspect_demo(Credentials('k', 's', Environment.DEMO))
    assert result['status'] == 'DEMO_READ_ONLY_INCOMPLETE'
    assert result['checks']['positions']['value'] == 'UNKNOWN'
    assert result['checks']['quantitative']['value']['error'] == 'CONFLICTING_FIELDS'
    assert result['checks']['quantitative']['eligible'] is None
