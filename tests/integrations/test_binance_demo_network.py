from dataclasses import replace, FrozenInstanceError
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
import json
import pickle
from types import SimpleNamespace
from urllib.parse import urlsplit, parse_qs

import pytest

from test_binance_demo_execution import DemoFixture, intent
from factorlab.integrations.binance.client import Response
from factorlab.integrations.binance_futures_trading.fake import NOW, MS
from factorlab.integrations.binance_futures_trading.network import (
    DemoExecutionPlan, DemoNetworkAuthorization, DemoNetworkTransport,
)
from factorlab.integrations.binance_futures_trading.execution import (
    DemoFuturesAdapter, DemoExecutionGateway, DemoExecutionAuthorization, observe_demo_rules,
)
from factorlab.integrations.binance_futures_trading.protocol import (
    Credentials, Environment as E, FuturesClient, ReadOnlyNetworkTransport, SignedRequest, SafetyBlocked,
)
from factorlab.integrations.binance_futures_trading.stream import UserDataEvents
from factorlab.trading.storage import PaperStore
from factorlab.trading.control import TradingController, RiskLimits
from factorlab.trading.paper import Instrument, Quote
from factorlab.trading import Side, OrderStatus as S


class NetworkFixture(DemoFixture):
    def __call__(self, request, timeout):
        if request.path in ('/fapi/v1/premiumIndex', '/fapi/v1/apiTradingStatus'):
            self.calls.append((request.method, request.path, parse_qs(request.query)))
            value = ({'symbol': 'NVDAUSDT', 'markPrice': '100', 'time': MS}
                     if request.path.endswith('premiumIndex') else
                     {'indicators': {'NVDAUSDT': [{'isLocked': False}]}})
            return Response(200, {}, json.dumps(value).encode())
        pending = self.response_next
        if request.method == 'GET':
            self.response_next = None
        result = super().__call__(request, timeout)
        if request.method == 'GET':
            self.response_next = pending
        if request.path.endswith('exchangeInfo'):
            value = json.loads(result.body)
            value['symbols'][0]['filters'].extend([
                {'filterType': 'PERCENT_PRICE', 'multiplierDown': '0.97', 'multiplierUp': '1.03'},
                {'filterType': 'MAX_NUM_ORDERS', 'limit': 200},
                {'filterType': 'POSITION_RISK_CONTROL', 'positionControlSide': 'NONE'}])
            return Response(200, {}, json.dumps(value).encode())
        if request.path == '/fapi/v3/account':
            value = json.loads(result.body)
            value['canTrade'] = True
            return Response(200, {}, json.dumps(value).encode())
        return result


def plan(account_id, **changes):
    values = dict(account_id=account_id, order_types=frozenset({'MARKET', 'LIMIT'}),
        max_quantity=D('2'), max_notional=D('250'), max_orders=3, max_open_orders=2,
        max_seconds=60, max_loss=D('5'), max_margin=D('500'),
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5), client_id_prefix='d03-',
        cleanup='CANCEL_KNOWN_THEN_STOP', recovery='GET_RECONCILE_NO_RESUBMIT')
    values.update(changes)
    return DemoExecutionPlan(**values)


@pytest.fixture
def wired(monkeypatch):
    fake, requests = NetworkFixture(), []
    class Result:
        def __init__(self, response):
            self.status, self.headers, self.body = response.status, response.headers, response.body
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def read(self, size):
            return self.body
    def open_stub(request, timeout):
        parsed = urlsplit(request.full_url)
        assert parsed.scheme == 'https' and parsed.netloc == 'demo-fapi.binance.com'
        requests.append(request)
        signed = SignedRequest(request.get_method(), parsed.path, parsed.query,
                               dict(request.header_items()), E.DEMO)
        return Result(fake(signed, timeout))
    def opener(*handlers):
        assert len(handlers) == 1
        assert handlers[0].redirect_request(None, None, 302, '', {}, 'https://fapi.binance.com') is None
        return SimpleNamespace(open=open_stub)
    monkeypatch.setattr('factorlab.integrations.binance_futures_trading.network.build_opener', opener)
    monkeypatch.setattr('factorlab.integrations.binance_futures_trading.protocol.build_opener',
                        lambda *args: pytest.fail('real connector forbidden in offline suite'))
    return fake, requests


def setup(wired, protocol=':memory:', paper=':memory:', authorized=True):
    import hashlib
    credentials = Credentials('demo-key', 'demo-secret', E.DEMO)
    account = 'DEMO:FactorLab-Demo:' + hashlib.sha256(b'demo-key').hexdigest()
    authorization = DemoNetworkAuthorization(plan(account)) if authorized else None
    transport = DemoNetworkTransport(authorization=authorization)
    client = FuturesClient(credentials, environment=E.DEMO, transport=transport, clock=lambda: NOW.timestamp())
    adapter = DemoFuturesAdapter(client, PaperStore(protocol), clock=lambda: NOW)
    observation = observe_demo_rules(adapter, source_version='OFFLINE_HTTP_STUB')
    if not authorized:
        return adapter, None, None
    gateway = DemoExecutionGateway(adapter, observation, (Instrument('NVDAUSDT', 'USDT'),), {'USDT': D('1000')})
    controller = TradingController(gateway, PaperStore(paper), clock=lambda: NOW,
                                  limits=RiskLimits(D('500'), D('500'), D('800'), 10))
    gateway.bind_controller(controller)
    controller.quote(Quote('NVDAUSDT', D('100'), NOW))
    gateway.recover(controller)
    return adapter, gateway, controller


@pytest.mark.parametrize('side', [Side.BUY, Side.SELL])
@pytest.mark.parametrize('limit', [None, '100.01'])
def test_http_request_contract(wired, side, limit):
    a, g, c = setup(wired)
    assert c.submit(intent('d03-one', side=side, limit=limit)).status == S.SUBMITTED
    assert c.cancel('d03-one').status == S.CANCELED
    writes = [r for r in wired[1] if r.get_method() != 'GET']
    assert [r.get_method() for r in writes] == ['POST', 'DELETE']
    params = parse_qs(urlsplit(writes[0].full_url).query)
    assert params['newClientOrderId'] == ['d03-one'] and params['quantity'] == ['2']
    assert params['side'] == [side.value] and 'signature' in params
    assert a.order('d03-one').status == S.CANCELED


def test_default_and_invalid_capabilities(wired, monkeypatch):
    a, _, _ = setup(wired, authorized=False)
    with pytest.raises(SafetyBlocked):
        a.client.request('POST', '/fapi/v1/order', {})
    for invalid in (True, {}, DemoExecutionAuthorization()):
        with pytest.raises(SafetyBlocked):
            DemoNetworkTransport(authorization=invalid)
    for name in ('BINANCE_TRADE', 'BINANCE_LIVE', 'DEMO_NETWORK_WRITE'):
        monkeypatch.setenv(name, 'true')
    for env in E:
        client = FuturesClient(Credentials('k', 's', env), environment=env,
            transport=ReadOnlyNetworkTransport(environment=env, permit_account_read=True))
        for method in ('POST', 'DELETE', 'PUT'):
            with pytest.raises(SafetyBlocked):
                client.request(method, '/fapi/v1/order')
    assert all(r.get_method() == 'GET' for r in wired[1])


def reauthorize(adapter, **changes):
    transport = adapter.client.transport
    updated = replace(transport.authorization.plan, **changes)
    adapter.client.transport = replace(transport, authorization=DemoNetworkAuthorization(updated))


def test_wrong_environment_host_account_and_no_direct_dispatch(wired):
    a, g, c = setup(wired)
    transport = a.client.transport
    with pytest.raises(FrozenInstanceError):
        transport.environment = E.PRODUCTION
    with pytest.raises(FrozenInstanceError):
        transport.host = 'https://fapi.binance.com'
    with pytest.raises(SafetyBlocked):
        FuturesClient(Credentials('k', 's', E.PRODUCTION), environment=E.DEMO, transport=transport)
    with pytest.raises(SafetyBlocked):
        a.client.request('POST', '/fapi/v1/order', {})
    with pytest.raises(SafetyBlocked):
        a.submit_order(intent('d03-direct'))
    reauthorize(a, account_id='DEMO:FactorLab-Demo:other')
    with pytest.raises(SafetyBlocked, match='account mismatch'):
        c.submit(intent('d03-one'))
    assert all(r.get_method() == 'GET' for r in wired[1])


@pytest.mark.parametrize('change', [dict(max_quantity=D('0')), dict(max_loss=D('NaN')),
    dict(max_seconds=0), dict(max_orders=True), dict(order_types=frozenset()),
    dict(environment=E.PRODUCTION), dict(symbol='BTCUSDT'), dict(cleanup=''), dict(recovery='')])
def test_plan_rejects_missing_or_invalid_limits(change):
    with pytest.raises(ValueError):
        plan('DEMO:FactorLab-Demo:test', **change)


@pytest.mark.parametrize('condition', ['mode', 'margin', 'brackets', 'canTrade', 'locked', 'kill',
    'quantity', 'notional', 'count', 'open', 'loss', 'occupied', 'expired', 'symbol', 'prefix'])
def test_admission_rejects(wired, condition):
    a, g, c = setup(wired)
    transport = a.client.transport
    order = intent('d03-one')
    if condition == 'mode':
        a.facts = replace(a.facts, hedge_mode=None)
    elif condition == 'margin':
        a.facts = replace(a.facts, assets=({'asset': 'USDT', 'walletBalance': '1000'},))
    elif condition == 'brackets':
        g.rules = replace(g.rules, brackets=())
    elif condition == 'canTrade':
        a.facts = replace(a.facts, can_trade=None)
    elif condition == 'locked':
        a.facts = replace(a.facts, trading_unlocked=None)
    elif condition == 'kill':
        c.set_kill_switch()
    elif condition == 'quantity':
        order = intent('d03-one', quantity='3')
    elif condition == 'notional':
        reauthorize(a, max_notional=D('10'))
    elif condition in ('count', 'open'):
        reauthorize(a, max_orders=1, max_open_orders=1)
        c.submit(intent('d03-first'))
    elif condition == 'loss':
        g._initial_equity = D('1010')
    elif condition == 'occupied':
        reauthorize(a, max_margin=D('10'))
    elif condition == 'expired':
        transport.authorization.revoke()
    elif condition == 'symbol':
        order = replace(order, instrument_id='BTCUSDT')
    else:
        order = intent('wrong-prefix')
    before = len([r for r in wired[1] if r.get_method() != 'GET'])
    with pytest.raises((SafetyBlocked, ValueError)):
        c.submit(order)
    assert len([r for r in wired[1] if r.get_method() != 'GET']) == before


def test_unknown_submit_cancel_and_duplicate_recovery(wired):
    a, g, c = setup(wired)
    fake, requests = wired
    fake.timeout_next = True
    assert c.submit(intent('d03-one')) is None
    assert a.order('d03-one').reconcile_required
    with pytest.raises(ValueError):
        c.submit(intent('d03-one'))
    g.recover(c)
    message = fake.trade_update('d03-one', '0.5')
    c.replay(UserDataEvents(a).parse(message))
    c.replay(UserDataEvents(a).parse(message))
    fake.response_next = Response(503, {}, b'{"code":-1007}')
    assert c.cancel('d03-one') is None
    g.recover(c)
    assert a.order('d03-one').cumulative_filled_quantity == D('0.5')
    assert len([r for r in requests if r.get_method() == 'POST']) == 1
    assert len([r for r in requests if r.get_method() == 'DELETE']) == 1


@pytest.mark.parametrize('response', [Response(429, {'Retry-After': '60'}, b'{"code":-1003}'),
                                      Response(500, {}, b'{"code":-1007}')])
def test_http_uncertainty_does_not_retry(wired, response):
    a, g, c = setup(wired)
    wired[0].response_next = response
    assert c.submit(intent('d03-one')) is None
    assert a.order('d03-one').reconcile_required
    before = len(wired[1])
    with pytest.raises(ValueError):
        c.submit(intent('d03-one'))
    assert len(wired[1]) == before


def test_capability_cannot_restore(wired, tmp_path):
    import subprocess
    import sys
    a, g, c = setup(wired)
    with pytest.raises(TypeError):
        pickle.dumps(a.client.transport.authorization)
    with pytest.raises(TypeError):
        pickle.dumps(a.client.transport)
    result = subprocess.run([sys.executable, '-c', '''
import os
from factorlab.integrations.binance_futures_trading.network import DemoNetworkTransport
os.environ['DEMO_NETWORK_WRITE']='true'
assert not DemoNetworkTransport().permitted()
print('NO_RESTORED_AUTHORIZATION')
'''], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'NO_RESTORED_AUTHORIZATION'


def test_redirect_does_not_follow_or_fallback(wired):
    a, g, c = setup(wired)
    wired[0].response_next = Response(302, {'Location': 'https://fapi.binance.com/fapi/v1/order'}, b'{}')
    assert c.submit(intent('d03-one')).status == S.REJECTED
    assert len([r for r in wired[1] if r.get_method() == 'POST']) == 1


def test_recovery_without_network_capability_and_changed_key_rejection(wired, tmp_path):
    a, g, c = setup(wired, tmp_path / 'protocol.sqlite', tmp_path / 'paper.sqlite')
    c.submit(intent('d03-one'))
    a.store.close()
    client = FuturesClient(Credentials('demo-key', 'demo-secret', E.DEMO), environment=E.DEMO,
        transport=DemoNetworkTransport(), clock=lambda: NOW.timestamp())
    restored = DemoFuturesAdapter(client, PaperStore(tmp_path / 'protocol.sqlite'), clock=lambda: NOW)
    assert restored.order('d03-one').reconcile_required
    before = len([r for r in wired[1] if r.get_method() != 'GET'])
    assert restored.query_order('d03-one').status == S.SUBMITTED
    with pytest.raises(SafetyBlocked):
        restored.cancel_order('d03-one')
    client.credentials = Credentials('another-key', 'secret', E.DEMO)
    with pytest.raises(SafetyBlocked):
        restored.query_order('d03-one')
    assert len([r for r in wired[1] if r.get_method() != 'GET']) == before


def test_shared_controller_cannot_be_bypassed_and_margin_tracks_fills(wired):
    a, g, c = setup(wired)
    with pytest.raises(SafetyBlocked, match='shared controller'):
        g.submit_order(intent('d03-direct'))
    reauthorize(a, max_margin=D('300'))
    c.submit(intent('d03-one'))
    c.replay(UserDataEvents(a).parse(wired[0].trade_update('d03-one', '2')))
    with pytest.raises(SafetyBlocked, match='margin limit'):
        c.submit(intent('d03-two'))
    assert len([r for r in wired[1] if r.get_method() == 'POST']) == 1
    assert c._admitted_intent is None


@pytest.mark.parametrize('quantity,limit', [('0.001', None), ('0.01', None), ('2', '100.001'), ('2', '104')])
def test_network_path_exchange_filters(wired, quantity, limit):
    a, g, c = setup(wired)
    with pytest.raises((SafetyBlocked, ValueError)):
        c.submit(intent('d03-one', quantity=quantity, limit=limit))
    assert all(r.get_method() == 'GET' for r in wired[1])


def test_cancel_http_timeout_query_only_recovery(wired, monkeypatch):
    a, g, c = setup(wired)
    c.submit(intent('d03-one'))
    from factorlab.integrations.binance_futures_trading import network
    original = network.build_opener
    def opener(*args):
        wrapped = original(*args)
        def dispatch(request, timeout):
            response = wrapped.open(request, timeout)
            if request.get_method() == 'DELETE':
                raise TimeoutError('offline cancellation reply lost')
            return response
        return SimpleNamespace(open=dispatch)
    monkeypatch.setattr(network, 'build_opener', opener)
    assert c.cancel('d03-one') is None
    assert a.order('d03-one').reconcile_required
    g.recover(c)
    assert a.order('d03-one').status == S.CANCELED
    assert len([r for r in wired[1] if r.get_method() == 'DELETE']) == 1


def test_preflight_retains_only_public_rules_and_conclusions(monkeypatch):
    from factorlab.integrations.binance_futures_trading.preflight import inspect
    client = FuturesClient(Credentials('k', 's', E.DEMO), environment=E.DEMO,
        transport=ReadOnlyNetworkTransport(environment=E.DEMO, permit_account_read=True),
        clock=lambda: NOW.timestamp())
    fixture = NetworkFixture()
    def read(method, path, params=None, *, signed=True):
        assert method == 'GET'
        result = json.loads(fixture(client.build(method, path, params, signed=signed), 10).body)
        if path == '/fapi/v3/account':
            result.pop('canTrade')
            result['private_marker'] = 'NEVER_RETAIN_ACCOUNT_RESPONSE'
        if path == '/fapi/v1/apiTradingStatus':
            return {'indicators': {}}
        return result
    monkeypatch.setattr(client, 'request', read)
    report = inspect(client)
    assert report['network_writes'] == 0 and len(report['endpoints']) == 9
    assert report['execution_admission'] == 'BLOCKED'
    assert 'account_can_trade' in report['unknown']
    assert 'quantitative_trading_unlocked' in report['unknown']
    assert 'NEVER_RETAIN_ACCOUNT_RESPONSE' not in json.dumps(report)
