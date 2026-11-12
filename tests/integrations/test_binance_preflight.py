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
