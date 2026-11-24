from datetime import datetime, timezone
from io import BytesIO
from urllib.error import HTTPError

from factorlab.integrations.alpaca.market_data import client as module
from factorlab.integrations.alpaca.market_data.client import Credentials, MarketDataClient, Response


def test_non_json_gateway_response_preserves_http_retry_status(monkeypatch):
    def failed(*args, **kwargs):
        raise HTTPError('https://data.alpaca.markets', 502, 'gateway', {}, BytesIO(b'<html>gateway</html>'))
    monkeypatch.setattr(module, 'urlopen', failed)
    result = module.transport('https://data.alpaca.markets', {}, 1)
    assert result.status == 502
    assert result.data is None


def test_http_date_retry_after_uses_received_time():
    responses = iter([Response(503, {'Retry-After': 'Tue, 24 Nov 2026 15:00:03 GMT'}, None),
                      Response(200, {}, {'quotes': {}})])
    delays = []
    client = MarketDataClient(Credentials('test', 'test'), feed='iex',
        request=lambda *args: next(responses), sleep=delays.append,
        now=lambda: datetime(2026, 11, 24, 15, tzinfo=timezone.utc))
    assert client.get('/v2/stocks/quotes/latest')[0] == {'quotes': {}}
    assert delays == [3]
