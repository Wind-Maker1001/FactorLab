"""Constructor clock recovery over intercepted official Demo HTTP."""
from concurrent.futures import ProcessPoolExecutor
from contextlib import closing
import hashlib
import hmac
import json
from urllib.parse import parse_qs, urlsplit

import pytest

from test_binance_demo_network import NetworkFixture, wired
from test_binance_demo_grid_network import network_grid, observe, writes
from test_demo_continuous_campaign import initialize
from factorlab.errors import DataQualityError
from factorlab.integrations.binance.client import Response
from factorlab.integrations.binance_futures_trading.demo_runtime import DemoRuntime, open_grid_session
from factorlab.integrations.binance_futures_trading.demo_session import _read_account
from factorlab.integrations.binance_futures_trading.fake import MS
from factorlab.integrations.binance_futures_trading.network import DemoNetworkTransport
from factorlab.integrations.binance_futures_trading.observability import ObservedClient, RequestCounts
from factorlab.integrations.binance_futures_trading.protocol import Credentials, Environment, ExchangeError, SafetyBlocked


ACCOUNT = '/fapi/v3/account'
TIME = '/fapi/v1/time'


def response(status=400, code=-1021, *, message=None):
    return Response(status, {}, json.dumps(dict(code=code, msg=message)).encode())


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


class NetworkClock:
    def __init__(self):
        self.elapsed = 0.
        self.wall_offset = 0.
        self.server_offset = 0.

    def wall(self):
        return MS / 1000 + self.elapsed + self.wall_offset

    def monotonic(self):
        return self.elapsed

    def server_ms(self):
        return round(MS + (self.elapsed + self.server_offset) * 1000)

    def time_response(self, upstream=0., downstream=0., *, wall_step=0.):
        def exchange(_):
            self.elapsed += upstream
            server_ms = self.server_ms()
            self.wall_offset += wall_step
            self.elapsed += downstream
            return Response(200, {}, json.dumps(dict(serverTime=server_ms)).encode())
        return exchange


def client(clock=None, *, recv_window=5000):
    if clock is None:
        clock = NetworkClock()
    return ObservedClient(Credentials('demo-key', 'demo-secret', Environment.DEMO),
        environment=Environment.DEMO, transport=DemoNetworkTransport(), recv_window=recv_window,
        clock=clock.wall, monotonic=clock.monotonic, request_counts=RequestCounts())


def paths(requests):
    return [urlsplit(request.full_url).path for request in requests]


def account_clock_paths(requests):
    return [path for path in paths(requests) if path in (ACCOUNT, TIME)]


def assert_get_only(requests):
    assert requests and {request.get_method() for request in requests} == {'GET'}


def test_normal_constructor(wired):
    _, requests = wired
    subject = client()
    assert _read_account(subject).can_trade
    assert paths(requests)[0] == TIME
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
    message = 'Timestamp for this request is outside of the recvWindow.'
    inject(monkeypatch, [response(message=message)], endpoint=endpoint)
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
    assert diagnostic['message'] == message
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
    assert account_clock_paths(requests) == [TIME, ACCOUNT, TIME, ACCOUNT]
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
    assert account_clock_paths(requests) == [TIME, ACCOUNT]
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
    assert account_clock_paths(requests) == ([TIME, ACCOUNT, TIME] if on_recovery else [TIME])
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
    assert account_clock_paths(requests) == ([TIME, ACCOUNT, TIME, ACCOUNT] if after_skew else [TIME, ACCOUNT])
    assert_get_only(requests)


def test_high_rtt_records_uncertainty_without_widening_window(wired, monkeypatch):
    clock = NetworkClock()
    subject = client(clock)
    inject(monkeypatch, [clock.time_response(.9, .9)], endpoint=TIME)
    _read_account(subject)
    assert subject.time_sync['rtt_ms'] == pytest.approx(1800)
    assert subject.time_sync['midpoint_uncertainty_ms'] == pytest.approx(900)
    assert subject.time_sync['wall_elapsed_ms'] == pytest.approx(1800)
    assert subject.offset_ms == 0
    assert subject.recv_window == 5000
    assert paths(wired[1]).count(TIME) == 1


def test_slow_time_sample_followed_by_a_usable_sample(wired, monkeypatch):
    clock = NetworkClock()
    subject = client(clock)
    inject(monkeypatch, [clock.time_response(.2, 2.1),
        clock.time_response(.12, .12)], endpoint=TIME)
    assert _read_account(subject).can_trade
    assert account_clock_paths(wired[1]) == [TIME, TIME, ACCOUNT]
    assert paths(wired[1]).count(TIME) == 2
    assert subject.time_sync['rtt_ms'] == pytest.approx(240)
    assert subject.time_sync['midpoint_uncertainty_ms'] == pytest.approx(120)
    assert abs(subject.demo_time().estimated_ms - clock.server_ms()) <= 1


@pytest.mark.parametrize('recv_window,delay', [(5000, 2.1), (100, .08)])
def test_unusable_time_samples_fail_before_account_dispatch(wired, monkeypatch, recv_window, delay):
    clock = NetworkClock()
    subject = client(clock, recv_window=recv_window)
    inject(monkeypatch, [clock.time_response(delay / 2, delay / 2)] * 3, endpoint=TIME)
    with pytest.raises(DataQualityError, match='RTT exceeds signing uncertainty budget'):
        _read_account(subject)
    assert paths(wired[1]) == [TIME, TIME, TIME]
    assert subject.recv_window == recv_window


@pytest.mark.parametrize('upstream,downstream', [(.06, .34), (.34, .06)])
def test_asymmetric_delay_uncertainty_contains_actual_server_time(wired, monkeypatch, upstream, downstream):
    clock = NetworkClock()
    subject = client(clock)
    inject(monkeypatch, [clock.time_response(upstream, downstream)], endpoint=TIME)
    subject.sync_time()
    reading = subject.demo_time(allow_refresh=False)
    assert reading.estimated_ms - clock.server_ms() == pytest.approx((upstream - downstream) * 500)
    assert reading.lower_ms <= clock.server_ms() <= reading.upper_ms
    assert reading.uncertainty_ms == pytest.approx(201)


def signature_parameters(request):
    return parse_qs(urlsplit(request.full_url).query)


def test_expired_calibration_refreshes_before_the_next_signed_get(wired, monkeypatch):
    clock = NetworkClock()
    subject = client(clock)
    inject(monkeypatch, [clock.time_response(.1, .1)] * 2, endpoint=TIME)
    subject.sync_time()
    clock.elapsed += 46
    with pytest.raises(DataQualityError, match='missing or expired'):
        subject.demo_time(allow_refresh=False)
    assert paths(wired[1]) == [TIME]
    subject.request('GET', ACCOUNT)
    assert paths(wired[1]) == [TIME, TIME, ACCOUNT]
    params = signature_parameters(wired[1][-1])
    assert abs(int(params['timestamp'][0]) - clock.server_ms()) <= 1
    assert params['recvWindow'] == ['5000']


def test_refresh_detects_server_drift_beyond_uncertainty(wired, monkeypatch):
    clock = NetworkClock()
    subject = client(clock)
    inject(monkeypatch, [clock.time_response(.1, .1)] * 2, endpoint=TIME)
    subject.sync_time()
    clock.elapsed += 46
    clock.server_offset += 2
    with pytest.raises(DataQualityError, match='server time drift exceeds calibration bounds'):
        subject.request('GET', ACCOUNT)
    assert paths(wired[1]) == [TIME, TIME]


@pytest.mark.parametrize('retry_delay', [.1, 5.1])
def test_arrival_outside_recv_window_can_only_retry_once(wired, monkeypatch, retry_delay):
    clock = NetworkClock()
    subject = client(clock)
    arrival_ages = []

    def arrive_after(seconds):
        def exchange(request):
            clock.elapsed += seconds
            params = parse_qs(request.query)
            age = clock.server_ms() - int(params['timestamp'][0])
            arrival_ages.append(age)
            if age > int(params['recvWindow'][0]):
                return response(message='Timestamp for this request is outside of the recvWindow.')
            return Response(200, {}, b'{}')
        return exchange

    inject(monkeypatch, [clock.time_response(.1, .1)] * 2, endpoint=TIME)
    inject(monkeypatch, [arrive_after(5.1), arrive_after(retry_delay)])
    subject.sync_time()
    with subject.recover_get_clock():
        if retry_delay > 5:
            with pytest.raises(ExchangeError, match='CLOCK_SKEW'):
                subject.request('GET', ACCOUNT)
        else:
            assert subject.request('GET', ACCOUNT) == {}
    assert paths(wired[1]) == [TIME, ACCOUNT, TIME, ACCOUNT]
    assert arrival_ages == pytest.approx([5100, retry_delay * 1000], abs=1)


@pytest.mark.parametrize('jump', [-20., 20.])
@pytest.mark.parametrize('during_sync', [False, True])
def test_wall_clock_step_does_not_change_server_timestamp(wired, monkeypatch, jump, during_sync):
    clock = NetworkClock()
    subject = client(clock)
    inject(monkeypatch, [clock.time_response(.1, .1, wall_step=jump if during_sync else 0.)], endpoint=TIME)
    subject.sync_time()
    if not during_sync:
        clock.wall_offset += jump
    clock.elapsed += 2
    subject.request('GET', ACCOUNT)
    assert paths(wired[1]) == [TIME, ACCOUNT]
    params = signature_parameters(wired[1][-1])
    assert abs(int(params['timestamp'][0]) - clock.server_ms()) <= 1
    assert params['recvWindow'] == ['5000']
    assert subject.time_sync['wall_step_ms'] == pytest.approx(jump * 1000 if during_sync else 0., abs=.001)
    assert subject.clock_recoveries == []
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


@pytest.mark.parametrize('failure,category', [
    (TimeoutError('private-url'), 'TRANSPORT_TIMEOUT'),
    (OSError('private-url'), 'TRANSPORT_ERROR'),
])
def test_transport_failure_category_is_safe(wired, monkeypatch, failure, category):
    from urllib.error import URLError
    subject = client()
    inject(monkeypatch, [URLError(failure)])
    with pytest.raises(DataQualityError) as caught:
        _read_account(subject)
    assert caught.value.category == category
    assert 'private-url' not in str(caught.value)
    row, = [row for row in subject.request_counts.snapshot()['endpoints'] if row['endpoint'] == ACCOUNT]
    assert row['errors'] == {category: 1}
    assert account_clock_paths(wired[1]) == [TIME, ACCOUNT]


@pytest.mark.parametrize('arrival,accepted', [(.1, True), (5.1, False)])
def test_high_response_rtt_does_not_determine_arrival_acceptance(wired, monkeypatch, arrival, accepted):
    clock = NetworkClock()
    subject = client(clock)
    inject(monkeypatch, [clock.time_response(.9, .9)], endpoint=TIME)
    def exchange(request):
        clock.elapsed += arrival
        params = parse_qs(request.query)
        age = clock.server_ms() - int(params['timestamp'][0])
        result = Response(200, {}, b'{}') if age <= 5000 else response()
        clock.elapsed += 6 - arrival
        return result
    inject(monkeypatch, [exchange])
    subject.sync_time()
    if accepted:
        assert subject.request('GET', ACCOUNT) == {}
    else:
        with pytest.raises(ExchangeError, match='CLOCK_SKEW'):
            subject.request('GET', ACCOUNT)
    assert subject.recv_window == 5000
    assert paths(wired[1]) == [TIME, ACCOUNT]


@pytest.mark.parametrize('failure', ['recover', 'second_skew', 'calibration'])
def test_session_recover_has_one_clock_retry_without_extra_sync(network_grid, monkeypatch, failure):
    root, store, factory, _, _, requests = network_grid
    authorization, _, _ = initialize(root, store)
    with open_grid_session(root, store, factory, continuous=True) as bridge:
        consumed = bridge.runtime.consumption(authorization)
        requests.clear()
        inject(monkeypatch, [response(), response()] if failure == 'second_skew' else [response()])
        if failure == 'calibration':
            inject(monkeypatch, [Response(200, {}, b'{}')], endpoint=TIME)
        if failure == 'recover':
            bridge.session.recover()
        else:
            with pytest.raises(DataQualityError):
                bridge.session.recover()
        assert paths(requests).count(TIME) == 1
        assert paths(requests).count(ACCOUNT) == (1 if failure == 'calibration' else 2)
        assert not bridge.session.client._recover_get_clock
        assert bridge.runtime.consumption(authorization) == consumed
        assert_get_only(requests)


def test_signed_local_http_stages_use_injected_monotonic(wired, monkeypatch):
    class Counts(RequestCounts):
        def __init__(self):
            super().__init__()
            self.events = []
        def clock_event(self, source, kind, details):
            if kind == 'signed_request_timing':
                self.events.append(details)
    clock = NetworkClock()
    subject = client(clock)
    subject.request_counts = Counts()
    inject(monkeypatch, [clock.time_response(.1, .1)], endpoint=TIME)
    def exchange(_):
        clock.elapsed += 6
        return Response(200, {}, b'{}')
    inject(monkeypatch, [exchange])
    subject.sync_time()
    subject.request('GET', ACCOUNT)
    event, = subject.request_counts.events
    assert event['signed_monotonic'] == pytest.approx(.2)
    assert event['transport_enter_monotonic'] == pytest.approx(.2)
    assert event['http_opener_enter_monotonic'] == pytest.approx(.2)
    assert event['headers_available_monotonic'] == pytest.approx(6.2)
    assert event['body_complete_monotonic'] == pytest.approx(6.2)
    assert event['timing_boundary'] == 'LOCAL_OPENER_AND_BODY_NOT_WIRE_OR_SERVER_RECEIPT'


def test_expired_calibration_refreshes_before_signed_post_and_cleans_placeholder_on_failure(network_grid, monkeypatch):
    from factorlab.integrations.binance_futures_trading.protocol import TimeCalibrationError
    root, store, factory, _, fake, requests = network_grid
    initialize(root, store)
    with open_grid_session(root, store, factory, continuous=True) as bridge:
        bridge.recover()
        original_request = bridge.session.client.request

        def expire_at_post(method, path, params=None, *, signed=True):
            if method == 'POST' and path == '/fapi/v1/order':
                bridge.session.client._demo_time_valid = False
            return original_request(method, path, params, signed=signed)

        # Case 1: /fapi/v1/time fails right when POST tries to refresh expired calibration.
        with monkeypatch.context() as patch:
            patch.setattr(bridge.session.client, 'request', expire_at_post)
            inject(patch, [Response(200, {}, b'{}')], endpoint=TIME)
            requests.clear()
            with pytest.raises(TimeCalibrationError):
                observe(bridge)
        # No HTTP POST was sent, and no phantom UNKNOWN placeholder remains in protocol.sqlite3.
        assert not writes(requests)
        assert paths(requests).count(TIME) == 1
        assert not bridge.session.status()['unknown']
        assert bridge.session.adapter.db.execute('SELECT count(*) FROM futures_orders').fetchone()[0] == 0
        assert bridge.runtime.db.execute("SELECT count(*) FROM dispatches WHERE method='POST'").fetchone()[0] == 0

    # Case 2: Once /fapi/v1/time succeeds, an expired calibration refreshes before POST and submits cleanly.
    with open_grid_session(root, store, factory, continuous=True) as bridge:
        bridge.recover()
        original_request = bridge.session.client.request

        def expire_at_post_ok(method, path, params=None, *, signed=True):
            if method == 'POST' and path == '/fapi/v1/order':
                bridge.session.client._demo_time_valid = False
            return original_request(method, path, params, signed=signed)

        def consistent_time(_):
            now_ms = round(bridge.session.client.projected_demo_time().estimated_ms)
            return Response(200, {}, json.dumps(dict(serverTime=now_ms)).encode())

        with monkeypatch.context() as patch:
            patch.setattr(bridge.session.client, 'request', expire_at_post_ok)
            inject(patch, [consistent_time], endpoint=TIME)
            requests.clear()
            observe(bridge)
        assert len(writes(requests)) == 1
        assert paths(requests).count(TIME) == 1
        assert not bridge.session.status()['unknown']


def test_post_confirm_readback_time_error_preserves_submitted_order_as_unknown(network_grid, monkeypatch):
    root, store, factory, _, fake, requests = network_grid
    initialize(root, store)
    with open_grid_session(root, store, factory, continuous=True) as bridge:
        bridge.recover()
        original_query = bridge.session.adapter.query_order

        def fail_readback(client_order_id):
            from factorlab.integrations.binance_futures_trading.protocol import TimeCalibrationError
            raise TimeCalibrationError('readback clock expired after POST')

        monkeypatch.setattr(bridge.session.adapter, 'query_order', fail_readback)
        requests.clear()
        observe(bridge)
        # POST already reached the exchange, so the placeholder MUST NOT be deleted.
        assert len(writes(requests)) == 1
        assert len(bridge.session.status()['unknown']) == 1
        monkeypatch.setattr(bridge.session.adapter, 'query_order', original_query)
        bridge.recover()
        assert not bridge.session.status()['unknown']


@pytest.mark.parametrize('time_failure', [TimeoutError('time socket timeout'), Response(500, {}, b'{"code":-1000,"msg":"err"}'), Response(400, {}, b'{"code":-1100,"msg":"bad"}')])
def test_pre_write_time_refresh_transport_or_http_error_cleans_placeholder(network_grid, monkeypatch, time_failure):
    from factorlab.integrations.binance_futures_trading.protocol import TimeCalibrationError
    root, store, factory, _, _, requests = network_grid
    initialize(root, store)
    with open_grid_session(root, store, factory, continuous=True) as bridge:
        bridge.recover()
        original_request = bridge.session.client.request

        def expire_at_post(method, path, params=None, *, signed=True):
            if method == 'POST' and path == '/fapi/v1/order':
                bridge.session.client._demo_time_valid = False
            return original_request(method, path, params, signed=signed)

        with monkeypatch.context() as patch:
            patch.setattr(bridge.session.client, 'request', expire_at_post)
            inject(patch, [time_failure], endpoint=TIME)
            requests.clear()
            with pytest.raises(TimeCalibrationError):
                observe(bridge)
        assert not writes(requests)
        assert not bridge.session.status()['unknown']
        assert bridge.session.adapter.db.execute('SELECT count(*) FROM futures_orders').fetchone()[0] == 0
        assert bridge.session.adapter.db.execute('SELECT count(*) FROM futures_rejections').fetchone()[0] == 0
        assert bridge.runtime.db.execute("SELECT count(*) FROM dispatches WHERE method='POST'").fetchone()[0] == 0


def test_direct_unadmitted_write_never_triggers_time_refresh(network_grid):
    from factorlab.integrations.binance_futures_trading.protocol import TimeCalibrationError
    root, store, factory, _, _, requests = network_grid
    initialize(root, store)
    with open_grid_session(root, store, factory, continuous=True) as bridge:
        bridge.recover()
        bridge.session.client._demo_time_valid = False
        requests.clear()
        with pytest.raises(TimeCalibrationError):
            bridge.session.client.request('POST', '/fapi/v1/order', {'symbol': 'NVDAUSDT'})
        assert requests == []
