"""Account facts must describe a bounded read set, not its completion time."""
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timezone
import json
from threading import Barrier, Event, current_thread

import pytest

from factorlab.errors import DataQualityError
from factorlab.integrations.binance.client import Response
from factorlab.integrations.binance_futures_trading.adapter import ACCOUNT_PATHS, ACCOUNT_PERMISSION_PATH, FuturesAdapter
from factorlab.trading.storage import PaperStore
from test_demo_constructor_clock import NetworkClock, client, inject, paths, response, TIME
from test_binance_demo_network import NetworkFixture, wired


def test_unsigned_collector_refreshes_the_same_session_anchor(wired, monkeypatch):
    from factorlab.integrations.binance_futures_trading.network import DemoNetworkTransport
    from factorlab.integrations.binance_futures_trading.observability import ObservedClient
    from factorlab.integrations.binance_futures_trading.protocol import Environment
    clock = NetworkClock()
    owner = client(clock)
    inject(monkeypatch, [clock.time_response(.1, .1)] * 2, endpoint=TIME)
    owner.sync_time()
    reader = ObservedClient(environment=Environment.DEMO, transport=DemoNetworkTransport())
    reader.share_demo_clock(owner)
    clock.elapsed += 46
    reading = reader.demo_time(min_valid_seconds=5)
    assert owner.demo_clock is reader.demo_clock
    assert owner.demo_time(allow_refresh=False) == reading
    assert paths(wired[1]) == [TIME, TIME]
    assert all('X-MBX-APIKEY' not in request.headers for request in wired[1])
    assert reader.credentials is None
    assert owner.recv_window == reader.recv_window == 5000


def test_account_transport_does_not_hold_shared_calibration_lock(wired, monkeypatch):
    from threading import Event, Thread
    from factorlab.integrations.binance.client import Response
    from factorlab.integrations.binance_futures_trading.network import DemoNetworkTransport
    from factorlab.integrations.binance_futures_trading.observability import ObservedClient
    from factorlab.integrations.binance_futures_trading.protocol import Environment
    clock = NetworkClock()
    owner = client(clock)
    inject(monkeypatch, [clock.time_response()] * 2, endpoint=TIME)
    owner.sync_time()
    entered, release = Event(), Event()
    results = []

    def account(_):
        entered.set()
        assert release.wait(2)
        return Response(200, {}, b'{}')

    inject(monkeypatch, [account])
    reader = ObservedClient(environment=Environment.DEMO, transport=DemoNetworkTransport())
    reader.share_demo_clock(owner)
    thread = Thread(target=lambda: results.append(owner.request('GET', '/fapi/v3/account')))
    thread.start()
    try:
        assert entered.wait(2)
        clock.elapsed += 46
        assert reader.demo_time().estimated_ms == clock.server_ms()
        assert not results
    finally:
        release.set()
        thread.join(2)
    assert not thread.is_alive()
    assert results == [{}]
    assert paths(wired[1]).count(TIME) == 2


@pytest.mark.parametrize('endpoint', ['/fapi/v3/account', '/fapi/v3/positionRisk',
    '/fapi/v1/openOrders', '/fapi/v1/apiTradingStatus'])
@pytest.mark.parametrize('delay,expired', [(4.999, False), (5.001, True)])
def test_account_read_set_uses_five_second_window(wired, monkeypatch, endpoint, delay, expired):
    clock = NetworkClock()
    observed_at = datetime.fromtimestamp(clock.wall(), timezone.utc)
    subject = client(clock)
    original = NetworkFixture.__call__

    def slow(self, request, timeout):
        result = original(self, request, timeout)
        if request.path == endpoint:
            clock.elapsed += delay
        return result

    monkeypatch.setattr(NetworkFixture, '__call__', slow)
    inject(monkeypatch, [clock.time_response()], endpoint=TIME)
    subject.sync_time()
    with closing(PaperStore(':memory:')) as store:
        adapter = FuturesAdapter(subject, store, account_id=subject.environment.account_id,
            clock=lambda: datetime.fromtimestamp(clock.wall(), timezone.utc))
        if expired:
            with pytest.raises(DataQualityError, match='STALE_ACCOUNT'):
                adapter.reconcile()
            assert adapter.facts is None
            assert not adapter.reconciled
        else:
            snapshot = adapter.reconcile()
            assert snapshot.reconciled and adapter.reconciled
            assert snapshot.observed_at == observed_at
        assert all(request.get_method() == 'GET' for request in wired[1])


@pytest.mark.parametrize('funding_delay', [3.0, 5.001])
def test_funding_overlaps_account_reads_with_the_same_cutoff(wired, monkeypatch, funding_delay):
    clock = NetworkClock()
    subject = client(clock)
    subject.sync_time()
    observed_at = datetime.fromtimestamp(clock.wall(), timezone.utc)
    together = Barrier(2)
    account_path, *_ = ACCOUNT_PATHS
    income_path = '/fapi/v1/income'
    original = NetworkFixture.__call__

    def routes(fixture, request, timeout):
        if request.path in (account_path, income_path):
            started = clock.elapsed
            delay = funding_delay if request.path == income_path else 3.0
            together.wait(timeout=2)
            clock.elapsed = max(clock.elapsed, started + delay)
        if request.path == income_path:
            return Response(200, {}, b'[]')
        return original(fixture, request, timeout)

    monkeypatch.setattr(NetworkFixture, '__call__', routes)
    with closing(PaperStore(':memory:')) as store:
        adapter = FuturesAdapter(subject, store, account_id=subject.environment.account_id,
            clock=lambda: datetime.fromtimestamp(clock.wall(), timezone.utc))
        cutoff_ms = round(observed_at.timestamp() * 1000)
        monkeypatch.setattr(adapter, '_earliest_activity_ms', lambda: cutoff_ms - 60_000)
        if funding_delay > 5:
            with pytest.raises(DataQualityError, match='STALE_ACCOUNT'):
                adapter.read_facts(sync_funding=True)
            assert adapter.facts is None
        else:
            facts = adapter.read_facts(sync_funding=True)
            assert facts.observed_at == observed_at
            assert adapter.funding_coverage()['covered_through_ms'] == cutoff_ms
        assert paths(wired[1]).count(income_path) == 1


@pytest.mark.parametrize('endpoint', ['/fapi/v3/account', '/fapi/v1/openOrders'])
def test_consecutive_get_timeouts_leave_gate_closed_and_do_not_retry(wired, monkeypatch, endpoint):
    subject = client()
    subject.sync_time()
    inject(monkeypatch, [TimeoutError(), TimeoutError()], endpoint=endpoint)
    with closing(PaperStore(':memory:')) as store:
        adapter = FuturesAdapter(subject, store, account_id=subject.environment.account_id)
        for _ in range(2):
            with subject.recover_get_clock(), pytest.raises(DataQualityError) as caught:
                adapter.reconcile()
            assert caught.value.category == 'TRANSPORT_TIMEOUT'
            assert not adapter.reconciled
            assert adapter.facts is None
        assert paths(wired[1]).count(endpoint) == 2
        assert paths(wired[1]).count(TIME) == 1
        assert subject.clock_recoveries == []
        assert all(request.get_method() == 'GET' for request in wired[1])


def test_parallel_account_matches_serial_and_starts_known_v2_read_with_v3(wired, monkeypatch):
    original = NetworkFixture.__call__
    permission_read = Event()
    hold_account = False
    report_permission_in_v3 = False
    permission = True
    account, *_ = ACCOUNT_PATHS

    def account_routes(fixture, request, timeout):
        if request.path == ACCOUNT_PERMISSION_PATH:
            permission_read.set()
            return Response(200, {}, json.dumps(dict(canTrade=permission)).encode())
        if request.path == account and hold_account:
            assert permission_read.wait(2)
        reply = original(fixture, request, timeout)
        if request.path == account and not report_permission_in_v3:
            payload = json.loads(reply.body)
            del payload['canTrade']
            return Response(reply.status, reply.headers, json.dumps(payload).encode())
        return reply

    monkeypatch.setattr(NetworkFixture, '__call__', account_routes)
    clock = NetworkClock()
    subject = client(clock)
    subject.sync_time()
    with closing(PaperStore(':memory:')) as store:
        adapter = FuturesAdapter(subject, store, account_id=subject.environment.account_id)
        read_set = adapter._account_reads

        def serial_read_set(observed_at, started, read_windows, *, parallel, sync_funding):
            return read_set(observed_at, started, read_windows, parallel=False, sync_funding=sync_funding)

        with monkeypatch.context() as patch:
            patch.setattr(adapter, '_account_reads', serial_read_set)
            serial = adapter.reconcile()
            serial_facts = adapter.facts
        permission_read.clear()
        hold_account = True
        assert adapter.reconcile().assets == serial.assets
        parallel_facts = adapter.facts
        assert parallel_facts == replace(serial_facts, observed_at=parallel_facts.observed_at,
                                         read_windows=parallel_facts.read_windows)
        assert serial_facts.read_windows == {}
        assert set(parallel_facts.read_windows) == {'account', 'position_risk', 'open_orders'}
        for lower_ms, upper_ms in parallel_facts.read_windows.values():
            assert type(lower_ms) is int and type(upper_ms) is int
            assert lower_ms <= clock.server_ms() <= upper_ms
        assert parallel_facts.can_trade
        assert paths(wired[1]).count(ACCOUNT_PERMISSION_PATH) == 2
        report_permission_in_v3 = True
        permission = False
        permission_read.clear()
        with pytest.raises(DataQualityError, match='conflicting Demo account trade permissions'):
            adapter.read_facts()
        assert adapter.facts is None and not adapter.reconciled


def test_two_simultaneous_skews_share_one_recalibration(wired, monkeypatch):
    subject = client()
    subject.sync_time()
    first_attempts = Barrier(2)

    def reject(_):
        first_attempts.wait(2)
        return response()

    account, risk, *_ = ACCOUNT_PATHS
    inject(monkeypatch, [reject], endpoint=account)
    inject(monkeypatch, [reject], endpoint=risk)
    with closing(PaperStore(':memory:')) as store:
        adapter = FuturesAdapter(subject, store, account_id=subject.environment.account_id)
        with subject.recover_get_clock():
            assert adapter.reconcile().reconciled
    counts = paths(wired[1])
    assert counts.count(TIME) == counts.count(account) == counts.count(risk) == 2
    assert {row['endpoint']: row['outcome'] for row in subject.clock_recoveries} == {
        account: 'RECOVERED', risk: 'RECOVERED'}


def test_out_of_order_conflicting_position_risk_never_publishes_a_snapshot(wired, monkeypatch):
    subject = client()
    subject.sync_time()
    original = NetworkFixture.__call__
    account, risk, *_ = ACCOUNT_PATHS
    conflicting_risk = Event()

    def changed_position(fixture, request, timeout):
        if request.path == risk:
            conflicting_risk.set()
            return Response(200, {}, b'[{"symbol":"NVDAUSDT","positionSide":"BOTH","positionAmt":"1"}]')
        if request.path == account:
            assert conflicting_risk.wait(2)
        return original(fixture, request, timeout)

    with closing(PaperStore(':memory:')) as store:
        adapter = FuturesAdapter(subject, store, account_id=subject.environment.account_id)
        assert adapter.reconcile().reconciled
        monkeypatch.setattr(NetworkFixture, '__call__', changed_position)
        with pytest.raises(DataQualityError, match='conflicting positions'):
            adapter.read_facts()
        assert adapter.facts is None and not adapter.reconciled


def test_read_set_timeout_drains_its_workers_before_recovery(wired, monkeypatch):
    from factorlab.integrations.binance_futures_trading import adapter as adapter_module

    original = NetworkFixture.__call__
    workers = []
    release = Event()

    def pending(fixture, request, timeout):
        if request.path == ACCOUNT_PATHS[0]:
            workers.append(current_thread())
            release.wait(timeout + .05)
        return original(fixture, request, timeout)

    subject = client()
    subject.sync_time()
    with closing(PaperStore(':memory:')) as store:
        adapter = FuturesAdapter(subject, store, account_id=subject.environment.account_id)
        with monkeypatch.context() as patch:
            patch.setattr(adapter_module, 'ACCOUNT_READ_SECONDS', .1)
            patch.setattr(NetworkFixture, '__call__', pending)
            with pytest.raises(DataQualityError, match='STALE_ACCOUNT'):
                adapter.reconcile()
        worker, = workers
        assert not worker.is_alive()
        assert adapter.facts is None and not adapter.reconciled
        assert adapter.reconcile().reconciled
