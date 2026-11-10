"""Synthetic budget and history tests, independent of actual market coverage."""
import json
from urllib.parse import parse_qs, urlsplit

from factorlab.data.hub.contracts import UnsupportedQueryError
from factorlab.integrations.binance.market import INTERVALS, milliseconds, price_bars
from factorlab.integrations.binance.series import collect_series
from factorlab.integrations.binance_research import BinanceResearch

import pytest

from factorlab.errors import DataQualityError
from factorlab.integrations.binance.client import PublicClient, Response
from factorlab.integrations.binance.history import survey
from tests.research.test_binance_collection import FixtureExchange, collector, collect, kline


@pytest.mark.parametrize('endpoint,table,key', [
    ('markPriceKlines', 'mark_bars', 'symbol'),
    ('indexPriceKlines', 'index_bars', 'pair'),
])
def test_historical_prices_are_not_current_snapshots(tmp_path, endpoint, table, key):
    baseline = collect(collector(FixtureExchange()), tmp_path / 'base')
    opened = milliseconds('2026-11-02')
    row = kline(opened)
    row[5] = 'ignored-not-volume'
    calls = []
    def transport(url, timeout):
        params = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
        calls.append(params)
        assert params[key] == 'NVDAUSDT'
        return Response(200, {}, json.dumps([row] if int(params['startTime']) <= opened else []).encode())
    result = collect_series(PublicClient(transport=transport), baseline=baseline.root,
                            output=tmp_path / 'series', start='2026-11-02', end='2026-11-03',
                            interval='1d', endpoint=endpoint)
    frame = result.read(table).data
    assert len(frame) == 1 and 'volume' not in frame
    bridge = BinanceResearch('BINANCE_USDM_PUBLIC_REST', result.root,
                             expected_data_version=result.manifest['data_version'])
    assert len(bridge.read(table, instrument_id='BINANCE:USDM:NVDAUSDT',
                          start='2026-11-02T00:00:00Z', end='2026-11-03T00:00:00Z',
                          observation_cutoff='2100-01-01T00:00:00Z').data) == 1
    with pytest.raises(UnsupportedQueryError, match='snapshot'):
        bridge.read('mark', instrument_id='BINANCE:USDM:NVDAUSDT',
                    start='2026-11-02T00:00:00Z', end='2026-11-03T00:00:00Z',
                    observation_cutoff='2100-01-01T00:00:00Z')
    with pytest.raises(ValueError, match='not available'):
        baseline.read(table)
    offline = PublicClient(transport=lambda *_: pytest.fail('completed series must be reused'))
    reused = collect_series(offline, baseline=baseline.root, output=tmp_path / 'series',
                            start='2026-11-02', end='2026-11-03', interval='1d', endpoint=endpoint)
    assert reused.manifest['data_version'] == result.manifest['data_version']


def test_price_history_rejects_invalid_ohlc_and_excludes_unclosed():
    t = milliseconds('2026-11-02')
    row = kline(t)
    assert price_bars([row], t, t + INTERVALS['1d'], INTERVALS['1d'], t).empty
    row[2] = '1'
    with pytest.raises(DataQualityError, match='OHLC'):
        price_bars([row], t, t + INTERVALS['1d'], INTERVALS['1d'], t + INTERVALS['1d'])


def test_history_resume_reuses_completed_price_requests(tmp_path):
    exchange = FixtureExchange()
    baseline = collect(collector(exchange), tmp_path / 'base')
    exchange.fail_endpoint = 'fundingRate'
    with pytest.raises(DataQualityError, match='exhausted'):
        survey(PublicClient(transport=exchange, retries=0), baseline=baseline.root,
               output=tmp_path / 'history', end='2026-11-06', progress=lambda _: None)
    assert not (tmp_path / 'history' / 'latest.json').exists()
    state = json.loads((tmp_path / 'history' / 'pending-history' / 'survey.json').read_text())
    assert state['failure']['type'] == 'DataQualityError'
    exchange.fail_endpoint = None
    client = PublicClient(transport=exchange, retries=0)
    result = survey(client, baseline=baseline.root, output=tmp_path / 'history',
                    end='2026-11-06', progress=lambda _: None)
    assert client.cache_hits > 0
    assert 'failure' not in result.manifest['survey']
    assert result.verify_raw() > 0
    offline = PublicClient(transport=lambda *_: pytest.fail('completed survey must be reused'))
    reused = survey(offline, baseline=baseline.root, output=tmp_path / 'history',
                    end='2026-11-06', progress=lambda _: None)
    assert reused.manifest['data_version'] == result.manifest['data_version']


def test_budget_counts_retries_and_stops_before_sleep():
    delays = []
    client = PublicClient(transport=lambda *_: Response(429, {'Retry-After': '4'}, b'{}'),
                          max_requests=1, sleep=delays.append)
    with pytest.raises(DataQualityError, match='request budget'):
        client.get('ping')
    assert client.attempts == 1
    assert client.status_counts == {'429': 1}
    assert delays == []


def test_wall_budget_includes_retry_after_and_transport_allowance():
    client = PublicClient(transport=lambda *_: Response(429, {'Retry-After': '100'}, b'{}'),
                          max_seconds=60, timeout=1, monotonic=lambda: 0,
                          sleep=lambda _: pytest.fail('must not sleep beyond deadline'))
    with pytest.raises(DataQualityError, match='wall-clock'):
        client.get('ping')
    assert client.attempts == 1


def test_cached_page_does_not_spend_request_budget(tmp_path):
    client = PublicClient(transport=lambda *_: Response(200, {}, b'[]'), max_requests=1)
    params = dict(symbol='NVDAUSDT', startTime=0, endTime=86_399_999, interval='1d')
    client.get('klines', params, archive=tmp_path, cache=True)
    assert client.get('klines', params, archive=tmp_path, cache=True) == []
    assert client.attempts == 1 and client.cache_hits == 1


def test_history_reuses_baseline_and_replays_offline(tmp_path):
    exchange = FixtureExchange()
    baseline = collect(collector(exchange), tmp_path / 'base')
    client = PublicClient(transport=exchange, max_requests=40)
    result = survey(client, baseline=baseline.root, output=tmp_path / 'history',
                    end='2026-11-06', progress=lambda _: None)
    result.verify_raw()
    assert len(result.read('bars').data) >= len(baseline.read('bars').data)
    assert result.manifest['parent_version'] == baseline.manifest['data_version']
    start, end = baseline.manifest['range']['start_ms'], baseline.manifest['range']['end_ms']
    inherited = {r['url'] for r in baseline.manifest['raw_receipts']}
    for receipt in result.manifest['raw_receipts']:
        if receipt['url'] in inherited or receipt['endpoint'] not in ('klines', 'fundingRate'):
            continue
        params = receipt['params']
        assert int(params['endTime']) < start or int(params['startTime']) >= end


def test_history_future_boundary_is_rejected_without_network(tmp_path):
    exchange = FixtureExchange()
    baseline = collect(collector(exchange), tmp_path / 'base')
    client = PublicClient(transport=lambda *_: pytest.fail('future survey requested network'))
    with pytest.raises(ValueError, match='completed'):
        survey(client, baseline=baseline.root, output=tmp_path / 'history', end='2100-01-01')
