"""Deterministic public-GET failure and raw receipt fixtures; no external network."""
from http.client import IncompleteRead
import json

import pytest

from factorlab.errors import DataQualityError
from factorlab.integrations.binance.client import PublicClient, Response


def test_retry_after_backoff_disconnect_and_receipts(tmp_path):
    replies = iter([Response(429, {'Retry-After': '5'}, b'{"code":-1003}'),
                    ConnectionError('disconnected'), Response(503, {}, b'busy'),
                    Response(200, {}, b'{}')])
    delays, urls = [], []
    def transport(url, timeout):
        urls.append(url)
        value = next(replies)
        if isinstance(value, Exception):
            raise value
        return value
    client = PublicClient(transport=transport, retries=3, sleep=delays.append)
    assert client.get('ping', archive=tmp_path) == {}
    assert delays == [5.0, 2.0, 4.0]
    assert len(urls) == 4
    paths = list((tmp_path / 'requests').glob('*.json'))
    assert all(len(path.stem) == 32 for path in paths)
    receipts = [json.loads(path.read_text()) for path in paths]
    assert sorted(row['status'] for row in receipts) == [200, 429, 503]
    assert all(row['acquired_at'].endswith('+00:00') for row in receipts)
    assert all(row['url'] == 'https://fapi.binance.com/fapi/v1/ping' for row in receipts)


def test_retry_after_http_date():
    responses = iter([Response(429, {'retry-after': 'Thu, 01 Jan 1970 00:00:10 GMT'}, b'{}'),
                      Response(200, {}, b'{}')])
    delays = []
    client = PublicClient(transport=lambda *_: next(responses), clock=lambda: 3,
                          sleep=delays.append)
    assert client.get('ping') == {}
    assert delays == [7]


@pytest.mark.parametrize('status', [400, 401, 403, 418, 404])
def test_nonretryable_http_fails_immediately(status):
    delays = []
    client = PublicClient(transport=lambda *_: Response(status, {}, b'{}'), sleep=delays.append)
    with pytest.raises(DataQualityError, match=f'HTTP {status}'):
        client.get('ping')
    assert not delays


def test_timeout_retry_exhaustion():
    count = 0
    def transport(*_):
        nonlocal count
        count += 1
        raise TimeoutError('fixture timeout')
    delays = []
    client = PublicClient(transport=transport, retries=2, sleep=delays.append)
    with pytest.raises(DataQualityError, match='exhausted'):
        client.get('ping')
    assert count == 3
    assert delays == [1, 2]


@pytest.mark.parametrize('endpoint,params', [('order', {}), ('withdraw', {}),
    ('https://evil.example', {}), ('ping', {'apiKey': 'secret'}),
    ('klines', {'symbol': 'NVDAUSDT', 'method': 'POST'})])
def test_no_transaction_or_credential_surface(endpoint, params):
    def forbidden(*_):
        pytest.fail('transport invoked for disallowed request')
    with pytest.raises(ValueError, match='allowlisted'):
        PublicClient(transport=forbidden).get(endpoint, params)


def test_truncated_http_body_retries():
    replies = iter([IncompleteRead(b'{', 5), Response(200, {}, b'{}')])
    def transport(*_):
        value = next(replies)
        if isinstance(value, Exception):
            raise value
        return value
    delays = []
    assert PublicClient(transport=transport, sleep=delays.append).get('ping') == {}
    assert delays == [1]


def test_cache_rejects_dynamic_or_unclosed_klines():
    client = PublicClient(clock=lambda: 100)
    for endpoint, params in [('premiumIndex', {'symbol': 'NVDAUSDT'}),
        ('klines', {'symbol': 'NVDAUSDT', 'interval': '1d', 'endTime': 99_999}),
        ('fundingRate', {'symbol': 'NVDAUSDT', 'endTime': 100_001})]:
        with pytest.raises(ValueError):
            client.get(endpoint, params, cache=True)


def test_cached_raw_integrity(tmp_path):
    client = PublicClient(transport=lambda *_: Response(200, {}, b'{}'))
    client.get('ping', archive=tmp_path, cache=True)
    next((tmp_path / 'raw').glob('*.bin')).write_bytes(b'changed')
    with pytest.raises(DataQualityError, match='digest'):
        client.get('ping', archive=tmp_path, cache=True)
