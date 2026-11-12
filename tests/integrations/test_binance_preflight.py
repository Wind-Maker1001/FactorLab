import json
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from factorlab.integrations.binance_futures_trading.preflight import (
    account_can_trade, inspect_eligibility, quantitative_evidence,
)
from factorlab.integrations.binance_futures_trading.protocol import (
    Credentials, Environment, FuturesClient, ReadOnlyNetworkTransport, SafetyBlocked,
)


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


@pytest.mark.parametrize('symbol, account, expected', [
    (False, False, None), (True, False, False), (False, True, False),
    (True, True, False), (1, False, None), (False, 'false', None),
])
def test_checks_symbol_and_account_without_certifying_partial_all_clear(symbol, account, expected):
    value, evidence = quantitative_evidence(status(symbol, account))
    assert value is expected
    assert evidence['unlocked_coverage_established'] is False


@pytest.mark.parametrize('payload', [
    None, [], {}, {'indicators': None}, {'indicators': []}, {'indicators': {}},
    {'data': status()},
    {'indicators': {'NVDAUSDT': [{'indicator': 'UFR', 'isLocked': False}]}},
    {'indicators': {'ACCOUNT': [{'indicator': 'TMV', 'isLocked': False}]}},
    {'indicators': {'NVDAUSDT': [], 'ACCOUNT': []}},
])
def test_missing_or_unknown_indicator_structure_is_unknown(payload):
    assert quantitative_evidence(payload)[0] is None


@pytest.mark.parametrize('rows', [
    {}, [None], [{}], [{'indicator': 'UFR'}], [{'isLocked': False}],
    [{'indicator': 'UNKNOWN', 'isLocked': False}],
    [{'indicator': 'UNKNOWN', 'isLocked': True}],
    [{'indicator': '', 'isLocked': False}],
    [{'indicator': 'UFR', 'isLocked': False}, {'indicator': 'UFR', 'isLocked': True}],
    [{'indicator': 'UFR', 'isLocked': False}, {'indicator': 'UFR', 'isLocked': False}],
])
def test_incomplete_and_duplicate_indicators_are_unknown(rows):
    payload = status()
    payload['indicators']['NVDAUSDT'] = rows
    assert quantitative_evidence(payload)[0] is None


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
    assert result['verdicts'] == {'account_can_trade': 'VERIFIED_TRUE',
                                  'quantitative_trading_unlocked': 'UNKNOWN'}
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
