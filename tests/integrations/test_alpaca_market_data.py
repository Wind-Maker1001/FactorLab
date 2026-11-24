from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

import pandas as pd
import pytest

from factorlab.integrations.alpaca.market_data.client import (
    Credentials, MarketDataClient, MarketDataError, Response)
from factorlab.integrations.alpaca.market_data.cache import MarketCache, Series


NOW = datetime(2026, 11, 24, 15, tzinfo=timezone.utc)


def test_symbols_first_pagination_and_provenance():
    calls = []
    def request(url, headers, timeout):
        params = parse_qs(urlparse(url).query)
        calls.append(params)
        if len(calls) == 1:
            return Response(200, {}, {'bars': {'AAPL': [{'t': '2026-11-20T05:00:00Z', 'c': 1}]},
                                     'next_page_token': 'page2'})
        return Response(200, {}, {'bars': {'QQQ': [{'t': '2026-11-20T05:00:00Z', 'c': 2}]},
                                 'next_page_token': None})
    client = MarketDataClient(Credentials('key', 'secret'), feed='iex', request=request, now=lambda: NOW)
    rows = client.bars(['AAPL', 'QQQ'], '2026-11-20', '2026-11-23', asof='2026-11-24')
    assert [r['symbol'] for r in rows] == ['AAPL', 'QQQ']
    assert calls[1]['page_token'] == ['page2']
    assert calls[0]['feed'] == ['iex']
    assert all(r['received_at'] == NOW.isoformat() for r in rows)
    assert 'secret' not in repr(client.credentials)
    assert 'key' not in str(client.evidence)


def test_permission_does_not_retry_or_switch_feed():
    calls = []
    def request(url, headers, timeout):
        calls.append(url)
        return Response(403, {}, {})
    client = MarketDataClient(Credentials('a', 'b'), feed='sip', request=request)
    with pytest.raises(MarketDataError) as error:
        client.latest('quotes', ['AAPL'])
    assert error.value.status == 403
    assert len(calls) == 1 and 'feed=sip' in calls[0]
    with pytest.raises(ValueError):
        client.get('/v2/orders', paper=True)


def test_retry_rate_limit_and_repeated_token():
    replies = iter([Response(429, {'Retry-After': '1'}, {}),
                    Response(200, {}, {'bars': {}, 'next_page_token': 'same'}),
                    Response(200, {}, {'bars': {}, 'next_page_token': 'same'})])
    sleeps = []
    client = MarketDataClient(Credentials('a', 'b'), feed='iex',
                              request=lambda *_: next(replies), sleep=sleeps.append)
    with pytest.raises(MarketDataError, match='pagination'):
        client.bars(['AAPL'], '2026-11-20', '2026-11-23', asof='2026-11-24')
    assert sleeps == [1]


def frame(day, value):
    return pd.DataFrame({'date': pd.to_datetime([day]), 'instrument_id': ['asset-a'],
                         'event_at': [day + 'T05:00:00Z'], 'raw_close': [value]})


def test_cache_interval_replacement_and_adjusted_full_refresh(tmp_path):
    cache = MarketCache(tmp_path)
    raw = Series('asset-a', 'AAPL', '1Day', 'iex', 'raw', '2026-11-24')
    first = cache.publish(raw, frame('2026-11-20', 10), start='2026-11-20', end='2026-11-23',
                          calendar_version='test', captured_at=NOW)
    second = cache.publish(raw, frame('2026-11-23', 11), start='2026-11-23', end='2026-11-23',
                           calendar_version='test', captured_at=NOW)
    assert second['predecessor'] == first['generation']
    assert cache.load(raw)[0].raw_close.tolist() == [10, 11]
    assert len(cache.provider(raw).read('2026-11-20', '2026-11-23')) == 2
    adjusted = Series('asset-a', 'AAPL', '1Day', 'iex', 'all', '2026-11-24')
    cache.publish(adjusted, frame('2026-11-20', 5), start='2026-11-20', end='2026-11-23',
                  calendar_version='test', captured_at=NOW)
    with pytest.raises(ValueError, match='full-window'):
        cache.publish(adjusted, frame('2026-11-23', 6), start='2026-11-23', end='2026-11-23',
                      calendar_version='test', captured_at=NOW)
    other = Series('asset-a', 'AAPL', '1Day', 'sip', 'raw', '2026-11-24')
    assert cache.load(other)[1] is None
    (tmp_path / second['generation'] / 'bars.parquet').write_bytes(b'broken')
    with pytest.raises(ValueError, match='integrity'):
        cache.load(raw)


def test_cache_empty_response_replaces_interval_not_fabricates(tmp_path):
    cache = MarketCache(tmp_path)
    series = Series('asset-a', 'AAPL', '1Day', 'iex', 'raw', '2026-11-24')
    cache.publish(series, frame('2026-11-20', 10), start='2026-11-20', end='2026-11-20',
                  calendar_version='test', captured_at=NOW)
    cache.publish(series, frame('2026-11-20', 10).iloc[:0], start='2026-11-20', end='2026-11-20',
                  calendar_version='test', captured_at=NOW)
    rows, metadata = cache.load(series)
    assert rows.empty and metadata['requests']
