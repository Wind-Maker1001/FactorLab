"""Offline HTTP-stubbed measurements; no exchange connectivity."""
import pytest
from collections import Counter
from copy import deepcopy
import json
from time import perf_counter
import tracemalloc
from urllib.parse import urlsplit

from test_binance_demo_grid_network import network_grid, wired
from factorlab.integrations.binance_futures_trading.demo_runtime import open_grid_session
from factorlab.integrations.binance_futures_trading.observability import RequestCounts


def test_idle_recovery_measurement(network_grid):
    root, credentials, factory, enable, fake, requests = network_grid
    enable()
    counts = RequestCounts()
    tracemalloc.start()
    with open_grid_session(root, credentials, factory, continuous=True, request_counts=counts) as bridge:
        initialization = Counter(urlsplit(r.full_url).path for r in requests)
        bridge.recover()
        economic = deepcopy(bridge.account())
        start_requests = len(requests)
        store = bridge.controller.store
        start_events = store.db.execute('SELECT count(*) FROM events').fetchone()[0]
        start_transactions = store.transaction_count
        started = perf_counter()
        counts.phase = 'poll'
        for _ in range(20):
            bridge.recover()
        elapsed = perf_counter() - started
        assert bridge.account() == economic
        report = dict(initialization=dict(initialization), polls=20, seconds=elapsed,
            endpoints=dict(Counter(urlsplit(r.full_url).path for r in requests[start_requests:])),
            events_added=store.db.execute('SELECT count(*) FROM events').fetchone()[0] - start_events,
            transactions=store.transaction_count - start_transactions,
            database_bytes=sum(p.stat().st_size for p in root.rglob('*.sqlite3')),
            peak_python_bytes=tracemalloc.get_traced_memory()[1])
    with open_grid_session(root, credentials, factory, continuous=True) as bridge:
        started = perf_counter()
        bridge.recover()
        report['restart_recovery_seconds'] = perf_counter() - started
        assert bridge.account() == economic
    tracemalloc.stop()
    assert report['endpoints']['/fapi/v3/account'] == 40
    assert sum(report['endpoints'].values()) == 260
    assert sum(row['count'] for row in counts.snapshot()['endpoints'] if row['phase'] == 'poll') == 260
    assert all(r.get_method() == 'GET' for r in requests)
    print('OFFLINE_MEASUREMENT=' + json.dumps(report, sort_keys=True))


def test_status_metrics_survive_control_snapshot(network_grid, tmp_path):
    from types import SimpleNamespace
    from contextlib import closing
    from factorlab.integrations.binance_futures_trading.observability import worker_metrics
    from factorlab.integrations.binance_futures_trading.demo_strategy_supervisor import StrategyControl
    from factorlab.integrations.binance_futures_trading.fake import MS
    root, credentials, factory, enable, fake, requests = network_grid
    enable()
    counts = RequestCounts()
    with open_grid_session(root, credentials, factory, continuous=True, request_counts=counts) as bridge:
        counts.phase = 'recovery'
        bridge.recover()
        metrics = worker_metrics(bridge.session, SimpleNamespace(watermarks=lambda: (None, MS)),
                                 bridge.runtime, counts)
        assert metrics['last_reconciled_at'] is not None
        assert metrics['last_recovery_seconds'] > 0
        assert metrics['last_valid_market_at'].endswith('+00:00')
        assert metrics['unknown_count'] == 0
        assert metrics['campaign']['posts_consumed'] == 0
        assert metrics['journals']['controller']['files_bytes']['database'] > 0
        with closing(StrategyControl(tmp_path / 'control.sqlite3', dict(mode='test'))) as control:
            control.report(dict(requests=counts, metrics=metrics))
            saved = control.snapshot()['observed']
            assert saved['requests'] == dict(counts)
            assert saved['metrics']['api']['scope'] == 'PROCESS_SINCE_START'
            encoded = json.dumps(saved)
            assert 'demo-secret' not in encoded and 'signature=' not in encoded


def test_metrics_classify_errors_weights_and_sources(monkeypatch):
    from factorlab.integrations.binance.client import Response
    from factorlab.integrations.binance_futures_trading.protocol import ExchangeError
    from factorlab.integrations.binance_futures_trading.demo_strategy_supervisor import MarketClient
    counts = RequestCounts()
    client = MarketClient(counts)
    response = Response(429, {'X-MBX-USED-WEIGHT-1M': '123'}, b'{"code":-1003}')
    monkeypatch.setattr('factorlab.integrations.binance_futures_trading.protocol._request_http',
        lambda *args, **kwargs: response)
    counts.phase = 'poll'
    with pytest.raises(ExchangeError):
        client.request('GET', '/fapi/v1/time', signed=False)
    row, = counts.snapshot()['endpoints']
    assert (row['phase'], row['source'], row['count'], row['error_rate']) == ('poll', 'feed', 1, 1)
    assert row['errors'] == {'RATE_LIMIT': 1}
    assert counts['GET'] == 1
    assert counts.snapshot()['observed_weight_headers'] == {'feed:x-mbx-used-weight-1m': '123'}


def test_supervisor_separates_reconnect_from_normal_polls():
    from types import SimpleNamespace
    from factorlab.errors import DataQualityError
    from factorlab.integrations.binance_futures_trading.demo_supervisor import DemoSupervisor
    counts = RequestCounts()
    phases = []
    def poll():
        phases.append(counts.phase)
        if len(phases) == 2:
            raise DataQualityError('offline disconnect')
        if len(phases) == 4:
            supervisor.stop()
        return dict(bars=0, quotes=0)
    feed = SimpleNamespace(poll=poll, pause=lambda _: None, watermarks=lambda: (None, None),
        status='RUNNING', reason=None, interval='1m')
    supervisor = DemoSupervisor(feed, request_counts=counts, report=lambda _: None)
    supervisor.stopped.wait = lambda _: None
    supervisor.run()
    assert phases == ['recovery', 'poll', 'recovery', 'poll']


def test_reconcile_fetches_each_account_endpoint_once(network_grid):
    from datetime import timedelta
    from dataclasses import replace
    from factorlab.integrations.binance_futures_trading.protocol import SafetyBlocked
    root, credentials, factory, enable, fake, requests = network_grid
    enable()
    with open_grid_session(root, credentials, factory, continuous=True) as bridge:
        start = len(requests)
        result = bridge.session.adapter.reconcile()
        paths = Counter(urlsplit(r.full_url).path for r in requests[start:])
        assert result.reconciled
        assert paths['/fapi/v3/account'] == 1
        assert paths['/fapi/v3/positionRisk'] == 1
        assert sum(paths.values()) == 6
        bridge.session.adapter.facts = replace(bridge.session.adapter.facts,
            observed_at=bridge.session._now() - timedelta(seconds=6))
        with pytest.raises(SafetyBlocked, match='STALE_ACCOUNT'):
            bridge.session._admit_write('POST', {}, None)
        start = len(requests)
        bridge.session.adapter.reconcile()
        assert len(requests) - start == 6


@pytest.mark.parametrize('size', [100, 1000])
def test_historical_orders_measurement(network_grid, size):
    from dataclasses import replace
    from decimal import Decimal
    from test_binance_demo_execution import intent
    from factorlab.trading import OrderSnapshot, OrderStatus
    from factorlab.trading.state import OrderRecord
    from factorlab.integrations.binance_futures_trading.fake import NOW, MS
    root, credentials, factory, enable, fake, requests = network_grid
    enable()
    with open_grid_session(root, credentials, factory, continuous=True) as bridge:
        adapter = bridge.session.adapter
        with adapter.store.transaction():
            for index in range(size):
                key = 'historical-' + str(index)
                snapshot = OrderSnapshot(intent(key, limit='150', quantity='.04'))
                snapshot = replace(snapshot, status=OrderStatus.CANCELED, exchange_order_id=str(index + 1))
                adapter._save(OrderRecord(snapshot, NOW, Decimal(0), OrderStatus.CANCELED), Decimal(0))
                fake.orders[key] = dict(clientOrderId=key, orderId=index + 1, symbol='NVDAUSDT',
                    side='BUY', type='LIMIT', positionSide='BOTH', origQty='.04', executedQty='0',
                    cumQuote='0', status='CANCELED', updateTime=MS)
        start = len(requests)
        started = perf_counter()
        adapter.reconcile()
        elapsed = perf_counter() - started
        counts = Counter(urlsplit(r.full_url).path for r in requests[start:])
        assert counts['/fapi/v1/order'] == size
        assert not any(r.get_method() != 'GET' for r in requests)
        print('ORDER_HISTORY_MEASUREMENT=' + json.dumps(dict(orders=size, seconds=elapsed,
            requests=dict(counts), bytes=sum(p.stat().st_size for p in root.rglob('*.sqlite3')))))


@pytest.mark.parametrize('size', [100, 1000])
def test_history_measurement(network_grid, size):
    root, credentials, factory, enable, fake, requests = network_grid
    enable()
    with open_grid_session(root, credentials, factory, continuous=True) as bridge:
        store = bridge.controller.store
        with store.transaction():
            for index in range(size):
                store.record_metadata('historical-input:' + str(index), dict(price='150', volume=0, id=str(index)))
        started = perf_counter()
        bridge.recover()
        recovery = perf_counter() - started
        started = perf_counter()
        for _ in range(100):
            store.event_payload('historical-input:' + str(size - 1))
        lookup = perf_counter() - started
        print('HISTORY_MEASUREMENT=' + json.dumps(dict(events=size, recovery_seconds=recovery,
            indexed_lookup_100_seconds=lookup,
            bytes=sum(p.stat().st_size for p in root.rglob('*.sqlite3')))))

