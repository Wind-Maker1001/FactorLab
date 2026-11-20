"""Offline admission checks for the single GET-only measurement window."""
from pathlib import Path
import runpy

import pytest

from test_binance_demo_strategy_session import deployment, wired

window = runpy.run_path(str(Path(__file__).resolve().parents[2] / 'tools/measure_demo_get_recovery.py'))
WindowRequests, WindowEnded = window['WindowRequests'], window['WindowEnded']


def test_preview_window_uses_only_get_and_redacts_account_binding(deployment, monkeypatch):
    import json
    from factorlab.integrations.binance_futures_trading import network
    monkeypatch.setattr('factorlab.integrations.binance_futures_trading.protocol.build_opener',
                        network.build_opener)
    from urllib.parse import parse_qs
    from test_binance_demo_network import NetworkFixture
    from test_demo_market_feed import bar
    from factorlab.integrations.binance.client import Response
    from factorlab.integrations.binance_futures_trading.fake import MS
    calls, unsupported = [], []
    original = NetworkFixture.__call__

    def market(fixture, request, timeout):
        calls.append((request.method, request.path))
        if request.path == '/fapi/v1/klines':
            params = parse_qs(request.query)
            assert request.method == 'GET'
            assert params['symbol'] == ['NVDAUSDT'] and params['interval'] == ['1m']
            start, end = int(params['startTime'][0]), int(params['endTime'][0])
            assert start % 60000 == 0 and (end + 1) % 60000 == 0
            assert end < MS and int(params['limit'][0]) == (end + 1 - start) // 60000
            return Response(200, {}, json.dumps([bar(t) for t in range(start, end + 1, 60000)]).encode())
        try:
            return original(fixture, request, timeout)
        except ValueError as error:
            unsupported.append((request.method, request.path, type(error).__name__))
            raise AssertionError(f'offline fixture {request.method} {request.path}: {type(error).__name__}') from error

    monkeypatch.setattr(NetworkFixture, '__call__', market)
    supervisor_type = window['DemoSupervisor']
    supervisors = []

    class OnePoll(supervisor_type):
        def run(self, *, seconds=None):
            supervisors.append(self)
            report = self.report

            def completed(raw):
                report(raw)
                self.stop()

            self.report = completed
            super().run()

    monkeypatch.setitem(window['observe'].__globals__, 'DemoSupervisor', OnePoll)
    root, credentials, _, _ = deployment
    result = window['observe'](root, credentials=credentials)
    assert not unsupported, unsupported
    assert result['outcome'] is None, (result['failure'], calls)
    assert ('GET', '/fapi/v1/klines') in calls
    assert ('GET', '/fapi/v1/ticker/bookTicker') in calls
    polls = [row for row in result['events'] if row['kind'] == 'poll']
    assert len(polls) == 1
    assert polls[0]['bars'] == polls[0]['quotes'] == 1
    assert not polls[0]['live_bar']
    assert polls[0]['reconciled']
    supervisor, = supervisors
    assert not supervisor.collection._thread.is_alive()
    import sqlite3
    from factorlab.integrations.binance_futures_trading.demo_runtime import operator_lock
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


@pytest.mark.parametrize('exception,outcome', [(ValueError, 'ValueError'), (WindowEnded, 'WINDOW_ENDED')])
def test_failed_window_releases_collection_feed_and_session(deployment, monkeypatch, exception, outcome):
    import json
    import sqlite3
    from factorlab.integrations.binance_futures_trading import network
    from factorlab.integrations.binance_futures_trading.demo_runtime import operator_lock
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
