from io import BytesIO
from urllib.error import HTTPError

import pandas as pd
import pytest

from factorlab.integrations.alpaca.market_data import client as client_module
from factorlab.integrations.alpaca.market_data.client import Credentials, MarketDataClient, MarketDataError
from factorlab.integrations.alpaca.market_data.cache import MarketCache
from factorlab.integrations.alpaca.market_data.inputs import Instrument
from factorlab.integrations.alpaca.market_data.service import MarketDataService
from test_alpaca_service import Client, calendar


@pytest.mark.parametrize('status', [502, 503])
@pytest.mark.parametrize('body', [b'<html>Bad gateway</html>', b''])
def test_non_json_http_errors_preserve_headers_and_retry_evidence(monkeypatch, status, body):
    headers = {'Retry-After': '0.25', 'X-Request-ID': 'gateway-request'}
    requests = []
    def urlopen(request, timeout):
        requests.append(request)
        raise HTTPError(request.full_url, status, 'gateway error', headers, BytesIO(body))
    monkeypatch.setattr(client_module, 'urlopen', urlopen)
    response = client_module.transport('https://data.alpaca.markets/v2/stocks/bars', {}, 20)
    assert response.status == status
    assert response.headers == headers
    assert response.data is None
    requests.clear()
    delays = []
    client = MarketDataClient(Credentials('test-key', 'test-secret'), feed='iex',
                              attempts=3, sleep=delays.append)
    with pytest.raises(MarketDataError) as error:
        client.latest('quotes', ['AAPL'])
    assert error.value.status == status
    assert len(requests) == 3
    assert delays == [0.25, 0.25]
    assert [e['status'] for e in client.evidence] == [status] * 3
    assert [e['request_id'] for e in client.evidence] == ['gateway-request'] * 3


def test_prepare_history_generator_matches_list(tmp_path):
    service = MarketDataService(Client(), MarketCache(tmp_path), calendar())
    instruments = [Instrument(s, s, pd.Timestamp('2026-11-24T12:00Z')) for s in ['AAPL', 'QQQ']]
    kwargs = dict(start='2026-11-20', end='2026-11-23', now='2026-11-24T16:00Z',
                  adjustment_basis='yahoo_ratio')
    listed = service.prepare_history(instruments, **kwargs)
    generated = service.prepare_history((i for i in instruments), **kwargs)
    for field in ['bars', 'adjusted_close', 'adjusted_open', 'adjusted_volume']:
        pd.testing.assert_frame_equal(getattr(generated, field), getattr(listed, field))
    assert generated.adjusted_close.columns.tolist() == ['AAPL', 'QQQ']
