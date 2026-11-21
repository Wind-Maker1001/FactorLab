"""Offline admission checks for the single GET-only measurement window."""
from collections import Counter
import json
from pathlib import Path
import runpy
import sqlite3
from urllib.parse import parse_qs

import pytest

from factorlab.integrations.binance.client import Response
from factorlab.integrations.binance_futures_trading import network, protocol
from factorlab.integrations.binance_futures_trading.demo_runtime import operator_lock
from factorlab.integrations.binance_futures_trading.demo_session import DemoSession
from factorlab.integrations.binance_futures_trading.fake import MS
from factorlab.trading.control import TradingController
from test_binance_demo_network import NetworkFixture
from test_binance_demo_strategy_session import deployment, wired
from test_demo_constructor_clock import NetworkClock
from test_demo_market_feed import bar

window = runpy.run_path(str(Path(__file__).resolve().parents[2] / 'tools/measure_demo_get_recovery.py'))
WindowRequests, WindowEnded = window['WindowRequests'], window['WindowEnded']
SESSION_NOW = DemoSession._now


def test_preview_reads_account_once_at_startup_and_delivers_live_market(deployment, monkeypatch):
    clock = NetworkClock()
    clock.elapsed = 58.2
    monkeypatch.setattr(DemoSession, '_now', SESSION_NOW)
    monkeypatch.setattr(protocol, 'build_opener', network.build_opener)
    initialize = protocol.FuturesClient.__init__

    def timed_client(client, *args, **kwargs):
        kwargs.update(clock=clock.wall, monotonic=clock.monotonic)
        initialize(client, *args, **kwargs)

    monkeypatch.setattr(protocol.FuturesClient, '__init__', timed_client)
    calls = []
    original = NetworkFixture.__call__

    def market(fixture, request, timeout):
        calls.append((request.method, request.path))
        if request.path == '/fapi/v1/time':
            return Response(200, {}, json.dumps(dict(serverTime=clock.server_ms())).encode())
        if request.path == '/fapi/v1/ticker/bookTicker':
            payload = dict(symbol='NVDAUSDT', bidPrice='100', askPrice='100', time=clock.server_ms() - 100)
            return Response(200, {}, json.dumps(payload).encode())
        if request.path == '/fapi/v1/klines':
            params = parse_qs(request.query)
            assert request.method == 'GET'
            assert params['symbol'] == ['NVDAUSDT'] and params['interval'] == ['1m']
            start, end = int(params['startTime'][0]), int(params['endTime'][0])
            assert start % 60000 == 0 and (end + 1) % 60000 == 0
            assert end < clock.server_ms() and int(params['limit'][0]) == (end + 1 - start) // 60000
            return Response(200, {}, json.dumps([bar(t) for t in range(start, end + 1, 60000)]).encode())
        return original(fixture, request, timeout)

    monkeypatch.setattr(NetworkFixture, '__call__', market)
    supervisor_type = window['DemoSupervisor']
    supervisors, startup = [], []

    class TwoPolls(supervisor_type):
        def run(self, *, seconds=None):
            supervisors.append(self)
            startup.extend(path for _, path in calls)
            report = self.report

            def completed(raw):
                report(raw)
                if json.loads(raw)['status'] != 'RUNNING' or clock.elapsed >= 60:
                    self.stop()
                else:
                    clock.elapsed = 60.2

            self.report = completed
            self.poll_seconds = .001
            super().run(seconds=seconds)

    monkeypatch.setitem(window['observe'].__globals__, 'DemoSupervisor', TwoPolls)
    root, credentials, _, _ = deployment
    result = window['observe'](root, credentials=credentials, worker_seconds=10)
    assert result['outcome'] is None, (result['failure'], calls)
    account_paths = {'/fapi/v3/account', '/fapi/v3/positionRisk', '/fapi/v1/positionSide/dual',
                     '/fapi/v1/openOrders', '/fapi/v1/symbolConfig', '/fapi/v1/apiTradingStatus'}
    assert Counter(path for path in startup if path in account_paths) == dict.fromkeys(account_paths, 1)
    assert startup.index('/fapi/v1/exchangeInfo') < startup.index('/fapi/v1/leverageBracket')
    assert startup.index('/fapi/v1/leverageBracket') < min(startup.index(path) for path in account_paths)
    assert '/fapi/v1/order' not in startup and '/fapi/v1/userTrades' not in startup
    warmup, live = [row for row in result['events'] if row['kind'] == 'poll']
    assert warmup['reconciled'] and live['reconciled']
    assert warmup['bars'] == warmup['quotes'] == live['bars'] == live['quotes'] == 1
    assert not warmup['live_bar'] and live['live_bar']
    assert live['watermarks'] == [MS + 60000, MS + 60100]
    supervisor, = supervisors
    assert not supervisor.collection._thread.is_alive()
    with pytest.raises(sqlite3.ProgrammingError, match='closed'):
        supervisor.feed.db.execute('SELECT 1')
    with operator_lock(root / 'grid-preview'):
        pass
    for path in root.rglob('*.sqlite3'):
        with sqlite3.connect(path) as connection:
            connection.execute('BEGIN EXCLUSIVE')
            connection.rollback()
    assert result['admitted_get_requests'] > 0
    assert result['completed_requests']['POST'] == result['completed_requests']['DELETE'] == 0
    assert all(row['method'] == 'GET' for row in result['events'] if row['kind'] == 'request')
    text = json.dumps(result)
    assert 'demo-key' not in text and 'demo-secret' not in text
    assert 'account_id' not in text and 'signature=' not in text


def test_preview_rejects_seed_that_ages_during_controller_initialization(deployment, monkeypatch):
    clock = NetworkClock()
    initialize_client = protocol.FuturesClient.__init__
    initialize_controller = TradingController.__init__

    def timed_client(client, *args, **kwargs):
        kwargs.update(clock=clock.wall, monotonic=clock.monotonic)
        initialize_client(client, *args, **kwargs)

    def slow_controller(controller, *args, **kwargs):
        initialize_controller(controller, *args, **kwargs)
        clock.elapsed += 5.001

    monkeypatch.setattr(protocol.FuturesClient, '__init__', timed_client)
    monkeypatch.setattr(TradingController, '__init__', slow_controller)
    root, credentials, _, _ = deployment
    result = window['observe'](root, credentials=credentials, worker_seconds=10)
    assert result['outcome'] == 'STALE_ACCOUNT'
    assert result['failure']['stage'] == 'session_initialization'
    paths = [row['endpoint'] for row in result['events'] if row['kind'] == 'request']
    assert paths.count('/fapi/v3/account') == 1
    assert '/fapi/v1/ticker/bookTicker' not in paths
    with operator_lock(root / 'grid-preview'):
        pass


@pytest.mark.parametrize('exception,outcome', [(ValueError, 'ValueError'), (WindowEnded, 'WINDOW_ENDED')])
def test_failed_window_releases_collection_feed_and_session(deployment, monkeypatch, exception, outcome):
    monkeypatch.setattr('factorlab.integrations.binance_futures_trading.protocol.build_opener',
                        network.build_opener)
    collections, feeds = [], []
    collection_type = window['DemoBookCollection']
    feed_type = window['DemoMarketFeed']
    start, close = collection_type.start, feed_type.close

    def started(collection):
        collections.append(collection)
        start(collection)

    def closed(feed):
        feeds.append(feed)
        close(feed)

    def fail(consumer):
        raise exception('demo-key demo-secret account_id signature=private')

    monkeypatch.setattr(collection_type, 'start', started)
    monkeypatch.setattr(feed_type, 'close', closed)
    monkeypatch.setattr(window['GridConsumer'], 'before_poll', fail)
    root, credentials, _, _ = deployment
    result = window['observe'](root, credentials=credentials, worker_seconds=1)
    assert result['outcome'] == outcome
    assert result['failure']['stage'] == 'supervisor_run'
    assert result['failure']['frames'][-1]['function'] == 'fail'
    text = json.dumps(result)
    assert all(secret not in text for secret in ('demo-key', 'demo-secret', 'account_id', 'signature='))
    assert len(collections) == len(feeds) == 1
    assert not collections[0]._thread.is_alive()
    with pytest.raises(sqlite3.ProgrammingError, match='closed'):
        feeds[0].db.execute('SELECT 1')
    with operator_lock(root / 'grid-preview'):
        pass
    for path in root.rglob('*.sqlite3'):
        with sqlite3.connect(path) as connection:
            connection.execute('BEGIN EXCLUSIVE')
            connection.rollback()
    assert result['completed_requests']['POST'] == result['completed_requests']['DELETE'] == 0


def test_request_and_time_budgets_are_reserved_before_dispatch():
    elapsed = [0]
    requests = WindowRequests(clock=lambda: elapsed[0], maximum=2, seconds=10)
    for _ in range(2):
        with requests.admission_lock:
            requests.before_request('collection', 'GET', '/fapi/v1/ticker/bookTicker')
    with pytest.raises(WindowEnded, match='request budget'):
        requests.before_request('session', 'GET', '/fapi/v3/account')
    assert requests.used == 2
    elapsed[0] = 10
    with pytest.raises(WindowEnded, match='deadline'):
        requests.before_request('session', 'GET', '/fapi/v3/account')
    assert requests.used == 2


@pytest.mark.parametrize('method', ['POST', 'DELETE'])
def test_window_rejects_every_write_before_dispatch(method):
    requests = WindowRequests()
    with pytest.raises(WindowEnded, match='GET-only'):
        requests.before_request('session', method, '/fapi/v1/order')
    assert requests.used == 0
    assert not requests.rows


def test_network_failure_does_not_end_the_recovery_observation():
    requests = WindowRequests()
    requests.before_request('session', 'GET', '/fapi/v1/openOrders')
    requests.record('session', 'GET', '/fapi/v1/openOrders', 10,
                    'TRANSPORT_TIMEOUT', {}, status=None)
    requests.before_request('session', 'GET', '/fapi/v3/account')
    assert requests.used == 2
    assert requests.rows[1]['error'] == 'TRANSPORT_TIMEOUT'


def test_byte_budget_leaves_room_for_inflight_diagnostics():
    requests = WindowRequests(max_bytes=81920)
    requests.append(dict(kind='fixture', detail='x' * 100))
    with pytest.raises(WindowEnded, match='response evidence'):
        requests.before_request('collection', 'GET', '/fapi/v1/ticker/bookTicker')
    assert requests.used == 0
