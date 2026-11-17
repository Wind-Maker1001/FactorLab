"""Constructor clock recovery over intercepted official Demo HTTP."""
from concurrent.futures import ProcessPoolExecutor
from contextlib import closing
import hashlib
import hmac
import json
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from test_binance_demo_network import NetworkFixture, wired
from test_binance_demo_grid_network import network_grid, observe, writes
from test_demo_continuous_campaign import initialize
from factorlab.errors import DataQualityError
from factorlab.integrations.binance.client import Response
from factorlab.integrations.binance_futures_trading import protocol
from factorlab.integrations.binance_futures_trading.demo_runtime import DemoRuntime, open_grid_session
from factorlab.integrations.binance_futures_trading.demo_session import _read_account
from factorlab.integrations.binance_futures_trading.fake import MS
from factorlab.integrations.binance_futures_trading.network import DemoNetworkTransport
from factorlab.integrations.binance_futures_trading.observability import ObservedClient, RequestCounts
from factorlab.integrations.binance_futures_trading.protocol import Credentials, Environment, ExchangeError, SafetyBlocked


ACCOUNT = '/fapi/v3/account'
TIME = '/fapi/v1/time'


def response(status=400, code=-1021):
    return Response(status, {}, json.dumps(dict(code=code)).encode())


def inject(monkeypatch, actions, *, endpoint=ACCOUNT):
    original = NetworkFixture.__call__
    pending = iter(actions)

    def intercepted(self, request, timeout):
        if request.path == endpoint:
            action = next(pending, None)
            if isinstance(action, Exception):
                raise action
            if callable(action):
                return action(request)
            if action is not None:
                return action
        return original(self, request, timeout)

    monkeypatch.setattr(NetworkFixture, '__call__', intercepted)


def client():
    return ObservedClient(Credentials('demo-key', 'demo-secret', Environment.DEMO),
        environment=Environment.DEMO, transport=DemoNetworkTransport(),
        clock=lambda: MS / 1000, request_counts=RequestCounts())


def paths(requests):
    return [urlsplit(request.full_url).path for request in requests]


def assert_get_only(requests):
    assert requests and {request.get_method() for request in requests} == {'GET'}


def test_normal_constructor(wired):
    _, requests = wired
    subject = client()
    assert _read_account(subject).can_trade
    assert paths(requests)[:2] == [TIME, ACCOUNT]
    assert paths(requests).count(TIME) == paths(requests).count(ACCOUNT) == 1
    assert subject.clock_recoveries == []
    assert subject.recv_window == 5000
    assert not subject._recover_get_clock
    assert_get_only(requests)


@pytest.mark.parametrize('endpoint', [ACCOUNT, '/fapi/v3/positionRisk',
    '/fapi/v1/positionSide/dual', '/fapi/v1/openOrders', '/fapi/v1/symbolConfig',
    '/fapi/v1/apiTradingStatus'])
def test_constructor_resigns_only_failed_get(wired, monkeypatch, endpoint):
    _, requests = wired
    subject = client()
    inject(monkeypatch, [response()], endpoint=endpoint)
    inject(monkeypatch, [lambda _: Response(200, {}, json.dumps(dict(serverTime=MS)).encode()),
        lambda _: Response(200, {}, json.dumps(dict(serverTime=MS + 2000)).encode())], endpoint=TIME)
    facts = _read_account(subject)
    assert facts.can_trade
    assert paths(requests).count(TIME) == 2
    assert paths(requests).count(endpoint) == 2
    if endpoint != ACCOUNT:
        assert paths(requests).count(ACCOUNT) == 1
    attempts = [request for request in requests if urlsplit(request.full_url).path == endpoint]
    timestamps = []
    for request in attempts:
        query, signature = urlsplit(request.full_url).query.rsplit('&signature=', 1)
        assert hmac.new(b'demo-secret', query.encode(), hashlib.sha256).hexdigest() == signature
        params = parse_qs(query)
        assert params['recvWindow'] == ['5000']
        timestamps.append(int(params['timestamp'][0]))
    assert timestamps == [MS, MS + 2000]
    diagnostic, = subject.clock_recoveries
    assert diagnostic['endpoint'] == endpoint
    assert diagnostic['code'] == -1021 and diagnostic['http_status'] == 400
    assert diagnostic['outcome'] == 'RECOVERED'
    rows = subject.request_counts.snapshot()['endpoints']
    row, = [row for row in rows if row['endpoint'] == endpoint]
    assert row['count'] == 2 and row['errors'] == {'CLOCK_SKEW': 1}
    assert not subject._recover_get_clock
    assert_get_only(requests)


def test_second_skew_terminates_with_first_failure(wired, monkeypatch):
    _, requests = wired
    subject = client()
    inject(monkeypatch, [response(), response()])
    with pytest.raises(ExchangeError) as caught:
        _read_account(subject)
    assert caught.value.category == 'CLOCK_SKEW'
    assert isinstance(caught.value.__cause__, ExchangeError)
    assert caught.value.__cause__.code == -1021
    assert 'GET /fapi/v3/account: RETRY_FAILED' in caught.value.__notes__[0]
    assert paths(requests) == [TIME, ACCOUNT, TIME, ACCOUNT]
    assert subject.clock_recoveries[0]['outcome'] == 'RETRY_FAILED'
    assert not subject._recover_get_clock
    assert_get_only(requests)


@pytest.mark.parametrize('status,code,category', [
    (400, -1022, 'SIGNATURE'), (401, -2014, 'AUTH'), (401, -2015, 'AUTH'),
    (429, -1003, 'RATE_LIMIT'), (429, -1021, 'RATE_LIMIT')])
def test_other_exchange_errors_never_resync(wired, monkeypatch, status, code, category):
    _, requests = wired
    subject = client()
    inject(monkeypatch, [response(status, code)])
    with pytest.raises(ExchangeError) as caught:
        _read_account(subject)
    assert caught.value.category == category
    assert paths(requests) == [TIME, ACCOUNT]
    assert subject.clock_recoveries == []
    assert_get_only(requests)


@pytest.mark.parametrize('on_recovery', [False, True])
def test_invalid_sync_fails_without_retry(wired, monkeypatch, on_recovery):
    _, requests = wired
    subject = client()
    invalid = Response(200, {}, b'{}')
    if on_recovery:
        inject(monkeypatch, [response()])
    inject(monkeypatch, [None, invalid] if on_recovery else [invalid], endpoint=TIME)
    with pytest.raises(DataQualityError, match='invalid server time') as caught:
        _read_account(subject)
    assert paths(requests) == ([TIME, ACCOUNT, TIME] if on_recovery else [TIME])
    if on_recovery:
        assert caught.value.__cause__.category == 'CLOCK_SKEW'
        assert subject.clock_recoveries[0]['outcome'] == 'SYNC_FAILED'
    assert not subject._recover_get_clock
    assert_get_only(requests)


@pytest.mark.parametrize('after_skew', [False, True])
def test_get_timeout_is_not_retried(wired, monkeypatch, after_skew):
    _, requests = wired
    subject = client()
    inject(monkeypatch, [response(), TimeoutError()] if after_skew else [TimeoutError()])
    with pytest.raises(DataQualityError, match='account read failed'):
        _read_account(subject)
    assert paths(requests) == ([TIME, ACCOUNT, TIME, ACCOUNT] if after_skew else [TIME, ACCOUNT])
    assert_get_only(requests)


def test_high_rtt_records_uncertainty_without_widening_window(wired, monkeypatch):
    subject = client()
    local = [MS / 1000]
    subject.clock = lambda: local[0]
    def delayed(_):
        local[0] += 1.8
        return Response(200, {}, json.dumps(dict(serverTime=MS + 900)).encode())
    inject(monkeypatch, [delayed], endpoint=TIME)
    samples = iter([10., 11.8])
    monkeypatch.setattr(protocol, 'time', SimpleNamespace(monotonic=lambda: next(samples)))
    _read_account(subject)
    assert subject.time_sync['rtt_ms'] == pytest.approx(1800)
    assert subject.time_sync['midpoint_uncertainty_ms'] == pytest.approx(900)
    assert subject.time_sync['wall_elapsed_ms'] == pytest.approx(1800)
    assert subject.offset_ms == 0
    assert subject.recv_window == 5000
    assert paths(wired[1]).count(TIME) == 1


@pytest.mark.parametrize('jump', [-20., 20.])
@pytest.mark.parametrize('during_sync', [False, True])
def test_clock_step_recovered_by_fresh_sync(wired, monkeypatch, jump, during_sync):
    subject = client()
    local = [MS / 1000]
    subject.clock = lambda: local[0]
    if during_sync:
        def time_step(_):
            local[0] += jump
            return Response(200, {}, json.dumps(dict(serverTime=MS)).encode())
        inject(monkeypatch, [time_step], endpoint=TIME)
    def reject(_):
        if not during_sync:
            local[0] += jump
        return response()
    inject(monkeypatch, [reject])
    assert _read_account(subject).can_trade
    diagnostic, = subject.clock_recoveries
    initial = diagnostic['initial_sync']
    assert initial['wall_elapsed_ms'] == pytest.approx(jump * 1000 if during_sync else 0)
    assert initial['offset_ms'] == pytest.approx(-jump * 500 if during_sync else 0)
    assert diagnostic['recovery_sync']['offset_ms'] == pytest.approx(-jump * 1000)
    assert diagnostic['outcome'] == 'RECOVERED'
    assert_get_only(wired[1])


@pytest.mark.parametrize('method,signed', [('GET', True), ('GET', False), ('POST', True), ('DELETE', True)])
def test_scope_never_retries_unsigned_or_writes_and_default_is_off(method, signed):
    from factorlab.integrations.binance_futures_trading.protocol import OfflineTransport, FuturesClient
    class Reject(OfflineTransport):
        def __init__(self):
            self.calls = []
        def __call__(self, request, timeout):
            self.calls.append(request)
            return response()
    transport = Reject()
    subject = FuturesClient(Credentials('demo-key', 'demo-secret'), transport=transport)
    if method == 'GET' and signed:
        with pytest.raises(ExchangeError):
            subject.request(method, ACCOUNT)
    else:
        with subject.recover_get_clock(), pytest.raises(ExchangeError):
            subject.request(method, ACCOUNT if method == 'GET' else '/fapi/v1/order', signed=signed)
    assert len(transport.calls) == 1
    assert subject.clock_recoveries == []


@pytest.mark.parametrize('endpoint', [ACCOUNT, '/fapi/v1/leverageBracket'])
def test_full_grid_constructor_preserves_stop_budget_and_existing_deployment(network_grid, monkeypatch, endpoint):
    root, store, factory, _, fake, requests = network_grid
    authorization, _, _ = initialize(root, store)
    with open_grid_session(root, store, factory, continuous=True) as bridge:
        bridge.recover()
        observe(bridge)
        key = bridge.grid.active[0].intent.client_order_id
        fake.trade_update(key, '.04', '146')
        bridge.recover()
        deployment = bridge.grid.deployment_id
        orders = bridge.session.status()['orders']
        account = bridge.account()
        bridge.runtime.stop()
        consumption = bridge.runtime.consumption(authorization)
        campaigns = bridge.runtime.db.execute('SELECT * FROM campaigns ORDER BY id').fetchall()
        stopped = bridge.runtime.continuous_authorization()
    requests.clear()
    inject(monkeypatch, [response()], endpoint=endpoint)
    with open_grid_session(root, store, read_only=True, continuous=True) as bridge:
        assert bridge.session.client.clock_recoveries[0]['outcome'] == 'RECOVERED'
        assert bridge.grid.deployment_id == deployment
        bridge.recover()
        assert bridge.session.status()['orders'] == orders
        assert bridge.account() == account
        assert bridge.runtime.continuous_authorization() == stopped
        assert bridge.runtime.consumption(authorization) == consumption
        assert bridge.runtime.db.execute('SELECT * FROM campaigns ORDER BY id').fetchall() == campaigns
        assert not bridge.session.client.transport.permitted()
    assert_get_only(requests)


@pytest.mark.parametrize('recoverable', [False, True])
@pytest.mark.parametrize('endpoint', [ACCOUNT, '/fapi/v1/leverageBracket'])
def test_full_constructor_failure_preserves_runtime(network_grid, monkeypatch, recoverable, endpoint):
    root, store, factory, _, _, requests = network_grid
    initialize(root, store)
    with closing(DemoRuntime(root)) as runtime:
        before = runtime.db.iterdump()
        before = tuple(before)
    requests.clear()
    inject(monkeypatch, [response(), response()] if recoverable else [response(401, -2015)], endpoint=endpoint)
    with pytest.raises(ExchangeError), open_grid_session(root, store, factory, continuous=True):
        pytest.fail('failed account read constructed a session')
    with closing(DemoRuntime(root)) as runtime:
        assert tuple(runtime.db.iterdump()) == before
    assert_get_only(requests)


def test_unknown_post_reopening_recovers_without_resubmit(network_grid, monkeypatch):
    root, store, factory, _, fake, requests = network_grid
    authorization, _, _ = initialize(root, store)
    fake.timeout_next = True
    with open_grid_session(root, store, factory, continuous=True) as bridge:
        bridge.recover()
        observe(bridge)
        key = bridge.grid.active[0].intent.client_order_id
        assert bridge.session.status()['unknown']
        consumed = bridge.runtime.consumption(authorization)
        deployment = bridge.grid.deployment_id
    assert len(writes(requests)) == 1
    requests.clear()
    inject(monkeypatch, [response()])
    with open_grid_session(root, store, factory, continuous=True) as bridge:
        bridge.recover()
        assert not bridge.session.status()['unknown']
        assert bridge.session.adapter.order(key).status.value == 'SUBMITTED'
        assert bridge.grid.deployment_id == deployment
        assert bridge.runtime.consumption(authorization) == consumed
        assert bridge.dispatch() is None
    assert_get_only(requests)


def open_in_process(root):
    with pytest.MonkeyPatch.context() as patch:
        intercepted = wired.__wrapped__(patch)
        _, store, _, _, _, requests = network_grid.__wrapped__(root, patch, intercepted)
        requests.clear()
        inject(patch, [response()])
        try:
            with open_grid_session(root, store, read_only=True, continuous=True) as bridge:
                bridge.recover()
                assert bridge.session.client.clock_recoveries[0]['outcome'] == 'RECOVERED'
                assert_get_only(requests)
                return bridge.grid.deployment_id
        except SafetyBlocked as error:
            assert str(error) == 'another Demo session is active'
            assert not requests
            return None


def test_multiprocess_clock_recovery_keeps_one_window(network_grid):
    root, store, factory, _, _, _ = network_grid
    initialize(root, store)
    with open_grid_session(root, store, factory, continuous=True) as bridge:
        deployment = bridge.grid.deployment_id
    with closing(DemoRuntime(root)) as runtime:
        before = tuple(runtime.db.iterdump())
    with ProcessPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(open_in_process, [root, root]))
    assert deployment in results
    assert set(results) <= {deployment, None}
    with closing(DemoRuntime(root)) as runtime:
        assert tuple(runtime.db.iterdump()) == before
        assert runtime.db.execute('SELECT count(*) FROM campaigns').fetchone()[0] == 1
        assert runtime.db.execute('SELECT count(*) FROM dispatches').fetchone()[0] == 0
