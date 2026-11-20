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
    root, credentials, _, _ = deployment
    result = window['observe'](root, credentials=credentials, worker_seconds=.01)
    assert result['outcome'] is None, result['failure']
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
