import json
from types import SimpleNamespace

import pytest

from factorlab.integrations.binance_equity.client import Client
from factorlab.integrations.binance_futures_trading.adapter import FuturesAdapter
from factorlab.integrations.binance_futures_trading.fake import FakeTransport
from factorlab.integrations.binance_futures_trading.protocol import (
    Credentials, Environment, FuturesClient, ReadOnlyNetworkTransport, SafetyBlocked,
)
from factorlab.trading.storage import PaperStore


@pytest.mark.parametrize('environment', list(Environment))
def test_environment_routes_key_and_disallows_writes(monkeypatch, environment):
    calls = []
    class Result:
        status, headers = 200, {}
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def read(self, size):
            return b'[]'
    def read(request, timeout):
        calls.append(request)
        return Result()
    monkeypatch.setattr('factorlab.integrations.binance_futures_trading.protocol.build_opener',
                        lambda *args: SimpleNamespace(open=read))
    credentials = Credentials(environment.name + 'key', 'secret', environment)
    client = FuturesClient(credentials, environment=environment,
        transport=ReadOnlyNetworkTransport(environment=environment, permit_account_read=True))
    for path in ('/fapi/v3/balance', '/fapi/v3/positionRisk', '/fapi/v1/openOrders'):
        client.request('GET', path)
    assert all(r.full_url.startswith(environment.host + '/') for r in calls)
    assert all(r.get_header('X-mbx-apikey') == credentials.key for r in calls)
    for method in ('POST', 'DELETE', 'PUT'):
        with pytest.raises(SafetyBlocked):
            client.request(method, '/fapi/v1/order')
    assert len(calls) == 3


def test_mismatched_credentials_rejected_before_network():
    demo = ReadOnlyNetworkTransport(environment=Environment.DEMO, permit_account_read=True)
    for credentials, environment in (
        (Credentials('k', 's', Environment.PRODUCTION), Environment.DEMO),
        (Credentials('k', 's'), Environment.DEMO),
        (Credentials('k', 's', Environment.DEMO), Environment.PRODUCTION),
    ):
        with pytest.raises(SafetyBlocked):
            FuturesClient(credentials, environment=environment, transport=demo)
    production = FuturesClient(Credentials('k', 's', Environment.PRODUCTION),
                              environment=Environment.PRODUCTION)
    with pytest.raises(SafetyBlocked):
        demo(production.build('GET', '/fapi/v3/account'), 1)


def test_missing_demo_credentials_never_use_legacy_or_production(monkeypatch):
    for name in ('BINANCE_DEMO_API_KEY', 'BINANCE_DEMO_API_SECRET'):
        monkeypatch.delenv(name, raising=False)
    for prefix in ('BINANCE', 'BINANCE_READONLY'):
        monkeypatch.setenv(prefix + '_API_KEY', 'production')
        monkeypatch.setenv(prefix + '_API_SECRET', 'secret')
    with pytest.raises(ValueError):
        Credentials.from_env(Environment.DEMO)
    assert Credentials.from_env(Environment.PRODUCTION).key == 'production'


def test_stocks_rejects_demo_credentials():
    with pytest.raises(ValueError, match='production'):
        Client(network=True, credentials=Credentials('demo', 'secret', Environment.DEMO))
    client = Client(network=True, credentials=Credentials('production', 'secret', Environment.PRODUCTION))
    assert client._key == 'production'


def test_persisted_environment_identity_cannot_be_reused():
    store = PaperStore(':memory:')
    for environment in (Environment.DEMO, Environment.PRODUCTION):
        client = FuturesClient(transport=FakeTransport(), environment=environment)
        if environment is Environment.DEMO:
            FuturesAdapter(client, store, account_id=environment.account_id)
        else:
            with pytest.raises(ValueError, match='account mismatch'):
                FuturesAdapter(client, store, account_id=environment.account_id)
    with pytest.raises(SafetyBlocked):
        FuturesAdapter(client, store)


def test_verification_report_excludes_private_payloads(monkeypatch):
    from factorlab.integrations.binance_futures_trading.readonly import verify
    calls = []
    def request(self, method, path, params=None, *, signed=True):
        calls.append((self.environment, method, path, signed))
        if path.endswith('time'):
            return {'serverTime': 1700000000000}
        if path.endswith('exchangeInfo'):
            return {'symbols': [{'symbol': 'BTCUSDT', 'status': 'TRADING', 'contractType': 'PERPETUAL'}]}
        return {'private_account_marker': 'must-never-persist'}
    monkeypatch.setattr(FuturesClient, 'request', request)
    monkeypatch.setattr(Client, '_get', lambda *args: ({'private': 'must-never-persist'}, None, None))
    report = verify({env: Credentials(env.name + 'key', 'secret', env) for env in Environment})
    serialized = json.dumps(report)
    assert 'must-never-persist' not in serialized and 'secret' not in serialized
    assert len(report['capabilities']) == 13
    assert report['NVDAUSDT'] is None
    assert report['demo_symbols'][0]['symbol'] == 'BTCUSDT'
    assert all(method == 'GET' for _, method, _, _ in calls)
    assert all(not signed for _, _, path, signed in calls if path.endswith(('time', 'exchangeInfo')))


def test_persisted_identity_rejects_another_key_in_same_environment():
    store = PaperStore(':memory:')
    env = Environment.DEMO
    first = FuturesClient(Credentials('first', 'secret', env), environment=env, transport=FakeTransport())
    FuturesAdapter(first, store, account_id=env.account_id)
    second = FuturesClient(Credentials('second', 'secret', env), environment=env, transport=FakeTransport())
    with pytest.raises(ValueError, match='account mismatch'):
        FuturesAdapter(second, store, account_id=env.account_id)


def test_network_host_configuration_is_immutable():
    from dataclasses import FrozenInstanceError
    transport = ReadOnlyNetworkTransport(environment=Environment.DEMO)
    with pytest.raises(FrozenInstanceError):
        transport.environment = Environment.PRODUCTION
