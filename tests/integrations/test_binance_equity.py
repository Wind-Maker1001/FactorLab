"""Offline fixtures follow the pinned official Stocks 1.0.0 wire schemas."""
import asyncio
import json
import socket

import pandas as pd
import pytest

from factorlab.errors import DataQualityError
from factorlab.integrations.binance_equity import BOUNDARIES
from factorlab.integrations.binance_equity.client import Client, Response
from factorlab.integrations.binance_equity.market import decimal_text
from factorlab.integrations.binance_equity.store import Reader, Sink
from factorlab.integrations.binance_equity.stream import Processor, collect, subscriptions

ACQUIRED = '2026-11-10T15:00:00+00:00'
T = 1794322800000
KEY = 'OfflineFixtureKeyNotACredential123'
QUOTE = dict(symbol='NVDA', bidPrice='182.530000001', askPrice='182.650000002',
             bidSize=200, askSize=201)
INFO = dict(timezone='UTC', symbols=[dict(symbol='NVDA', tradability='BUY_SELL',
            tradabilityUpdateTime=T, overnightSupported=True, fractionable=True,
            extendedSession=True, stepSize='0.000000001', minNotional='5.00000000')])


def wire_quote(**updates):
    return dict(dict(e='quote', E=T, s='NVDA', bp='182.530000001', ap='182.650000002',
                     bs=200, **{'as': 201}, T=T - 10), **updates)


def candle(closed, **updates):
    k = dict(t=T, ct=T + 299999, s='NVDA', i='5m', o='182.530000001',
             c='182.650000002', h='183.000000000', l='182.000000000',
             v='0.000000001', x=closed)
    k.update(updates)
    return dict(e='kline', E=T + 300000, s='NVDA', k=k)


def feed(processor, value):
    body = json.dumps(value).encode()
    receipt = processor.sink.raw(body, transport='WEBSOCKET', acquired_at=ACQUIRED)
    processor.feed(body, ACQUIRED, receipt)


@pytest.fixture
def credentials(monkeypatch):
    monkeypatch.setenv('BINANCE_EQUITY_API_KEY', KEY)


def test_rest_precision_provenance_and_offline_replay(tmp_path, credentials, monkeypatch):
    calls = []
    def request(url, headers, timeout):
        calls.append(url)
        assert headers == {'X-MBX-APIKEY': KEY}
        return Response(200, json.dumps(INFO if 'exchangeInfo' in url else QUOTE).encode())
    sink = Sink(tmp_path / 'run')
    client = Client(network=True, request=request)
    client.discover(sink)
    client.quote(sink, 'NVDA')
    reader = sink.close()
    assert calls[0] == 'https://api.binance.com/sapi/v1/equity/market/exchangeInfo'
    assert calls[1].endswith('/quote?symbol=NVDA')
    monkeypatch.setattr(socket.socket, 'connect', lambda *a: pytest.fail('replay used network'))
    reader = Reader(sink.root)
    assert reader.verify_raw() == 2
    result = reader.read('quotes')
    assert result.data.iloc[0].bid_price == QUOTE['bidPrice']
    assert result.data.iloc[0].bid_size == '200'
    assert pd.isna(result.data.iloc[0].event_time)
    assert result.data.iloc[0].session == 'UNKNOWN'
    assert result.metadata.evidence_status == BOUNDARIES
    rule = reader.read('symbols').data.iloc[0]
    assert rule.stepSize == '0.000000001'
    assert rule.rule_observed_at and rule.historical_rule_effective_at is None
    for path in sink.root.rglob('*'):
        if path.is_file():
            assert KEY.encode() not in path.read_bytes()


def test_discovery_preserves_exchange_numeric_tickers(tmp_path, credentials):
    value = dict(timezone='UTC', symbols=[dict(INFO['symbols'][0], symbol='037ESC019')])
    sink = Sink(tmp_path / 'run')
    client = Client(network=True, request=lambda *a: Response(200, json.dumps(value).encode()))
    assert client.discover(sink)[0]['symbol'] == '037ESC019'
    assert sink.close().read('symbols').data.iloc[0].symbol == '037ESC019'


def test_no_quote_is_not_zero_price(tmp_path, credentials):
    sink = Sink(tmp_path / 'run')
    client = Client(network=True, request=lambda *a: Response(200, b''))
    assert client.quote(sink, 'NVDA')[0]['availability'] == 'NO_QUOTE'
    reader = sink.close()
    assert reader.read('quotes').data.bid_price.isna().all()
    assert reader.verify_raw() == 1


def test_auth_echo_discarded_and_transport_error_redacted(tmp_path, credentials):
    sink = Sink(tmp_path / 'run')
    client = Client(network=True, request=lambda *a: Response(403, KEY.encode()))
    with pytest.raises(DataQualityError, match='discarded'):
        client.quote(sink, 'NVDA')
    assert sink.count == 0
    def failure(*args):
        raise OSError(KEY)
    client = Client(network=True, request=failure)
    with pytest.raises(DataQualityError) as error:
        client.quote(sink, 'NVDA')
    assert KEY not in str(error.value)
    assert error.value.__suppress_context__


def test_endpoint_and_network_boundaries(tmp_path, credentials):
    with pytest.raises(ValueError, match='opt-in'):
        Client()
    sink = Sink(tmp_path / 'run')
    client = Client(network=True, request=lambda *a: pytest.fail('unexpected request'))
    with pytest.raises(ValueError, match='allowed'):
        client._get('order', 'NVDA', sink)
    for ticker in ('NVDA&secret=x', '000001.SZ/../../account', 'nvda'):
        with pytest.raises(ValueError):
            client.quote(sink, ticker)
    with pytest.raises(ValueError):
        subscriptions('NVDA', ['orderReport'], '5m')
    with pytest.raises(ValueError):
        subscriptions('NVDA', ['kline'], '1m')
    assert subscriptions('NVDA', ['quote', 'kline'], '1M') == ['NVDA@quote', 'NVDA@kline_1M']


def test_http_errors_keep_raw_receipts(tmp_path, credentials):
    sink = Sink(tmp_path / 'run')
    client = Client(network=True, request=lambda *a: Response(429, b'{"code":-1003}'))
    with pytest.raises(DataQualityError, match='HTTP 429'):
        client.discover(sink)
    reader = sink.close(state='INTERRUPTED_OR_FAILED')
    assert reader.verify_raw() == 1
    assert reader.manifest['state'] == 'INTERRUPTED_OR_FAILED'


def test_events_dedup_closed_bar_isolation_and_sessions(tmp_path):
    sink = Sink(tmp_path / 'run')
    processor = Processor(sink, 'NVDA', ['quote', 'price', 'kline', 'calendar'])
    feed(processor, wire_quote())
    feed(processor, wire_quote())
    feed(processor, wire_quote(E=T + 1, ap='182.660000002'))
    feed(processor, candle(False))
    feed(processor, candle(True))
    for phase in ('C', 'ON', 'PRE', 'O', 'POST'):
        feed(processor, dict(e='price', rates=[dict(s='NVDA', p='182.530000001', t=T, mp=phase)]))
    feed(processor, dict(e='calendar', **{'from': 'PRE'}, to='O', ts=T))
    reader = sink.close()
    assert reader.verify_raw() == 11
    assert processor.duplicates == 1
    assert len(reader.read('quotes').data) == 2
    assert list(reader.read('prices').data.session) == ['CLOSED', 'OVERNIGHT', 'PRE_MARKET', 'REGULAR', 'POST_MARKET']
    assert reader.read('sessions').data.iloc[0].session == 'REGULAR'
    assert len(reader.read('bars').data) == len(reader.read('open_bars').data) == 1
    assert reader.read('bars').data.iloc[0].volume == '0.000000001'


def test_candle_retransmission_dedup_preserves_corrections(tmp_path):
    sink = Sink(tmp_path / 'run')
    processor = Processor(sink, 'NVDA', ['kline'])
    feed(processor, candle(True))
    repeated = candle(True)
    repeated['E'] += 1000
    feed(processor, repeated)
    corrected = candle(True, c='182.700000003')
    corrected['E'] += 2000
    feed(processor, corrected)
    assert processor.duplicates == 1
    bars = sink.close().read('bars').data
    assert list(bars.close) == ['182.650000002', '182.700000003']


def test_observed_target_capability_survives_snapshot_without_target(tmp_path):
    sink = Sink(tmp_path / 'run')
    processor = Processor(sink, 'NVDA', ['price'])
    feed(processor, dict(e='price', rates=[dict(s='NVDA', p='182.53', t=T, mp='PRE')]))
    feed(processor, dict(e='price', rates=[dict(s='AAPL', p='220.00', t=T, mp='PRE')]))
    assert sink.capabilities['price']['status'] == 'OBSERVED'
    assert len(sink.buffers['prices']) == 1


def test_untimestamped_prices_are_distinct_observations(tmp_path):
    sink = Sink(tmp_path / 'run')
    processor = Processor(sink, 'NVDA', ['price'])
    value = dict(e='price', rates=[dict(s='NVDA', p='182.53', t=None, mp='ON')])
    feed(processor, value)
    feed(processor, value)
    assert len(sink.buffers['prices']) == 2


@pytest.mark.parametrize('value', ['NaN', 'Infinity', '-1', 1.1, True])
def test_invalid_precision_values_rejected(value):
    with pytest.raises(DataQualityError):
        decimal_text(value)


def test_bad_candle_and_foreign_ticker_rejected(tmp_path):
    sink = Sink(tmp_path / 'run')
    processor = Processor(sink, 'NVDA', ['kline', 'quote'])
    with pytest.raises(DataQualityError):
        feed(processor, candle(True, h='1'))
    with pytest.raises(DataQualityError, match='ticker'):
        feed(processor, wire_quote(s='AAPL'))


def test_raw_and_parquet_tampering_fail_replay(tmp_path, credentials):
    sink = Sink(tmp_path / 'run')
    client = Client(network=True, request=lambda *a: Response(200, json.dumps(QUOTE).encode()))
    client.quote(sink, 'NVDA')
    reader = sink.close()
    raw = sink.root / 'raw/000000000.bin'
    raw.write_bytes(b'{}')
    with pytest.raises(DataQualityError, match='digest'):
        reader.verify_raw()
    part = sink.root / reader.manifest['tables']['quotes'][0]['path']
    part.write_bytes(b'corrupt')
    with pytest.raises(DataQualityError, match='digest'):
        reader.read('quotes')


def test_batches_preserve_nullable_integer_epochs(tmp_path):
    sink = Sink(tmp_path / 'run')
    processor = Processor(sink, 'NVDA', ['quote'])
    for i in range(1001):
        feed(processor, wire_quote(E=T + i, T=None if i == 0 else T + i))
    reader = sink.close()
    frame = reader.read('quotes').data
    assert len(reader.manifest['tables']['quotes']) == 2
    assert len(frame) == 1001
    assert pd.isna(frame.iloc[0].event_time)
    assert frame.iloc[-1].event_time == T + 1000


def test_reconnect_duplicate_and_gap_recording(tmp_path):
    pytest.importorskip('websockets', reason='requires binance-equity optional dependency')
    sink = Sink(tmp_path / 'run')
    calls = []
    class FakeSocket:
        def __init__(self, number):
            self.number, self.received = number, 0
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            return False
        async def send(self, value):
            assert json.loads(value) == dict(method='SUBSCRIBE', params=['NVDA@quote'], id=1)
        async def recv(self):
            self.received += 1
            if self.received == 1:
                return json.dumps(wire_quote())
            if self.number == 1:
                raise OSError('connection lost')
            await asyncio.Event().wait()
    def connector(url, **options):
        calls.append(url)
        return FakeSocket(len(calls))
    result = asyncio.run(collect(sink, kinds=['quote'], seconds=1.2, network=True, connector=connector))
    reader = sink.close()
    assert len(calls) == 2
    assert calls[0] == 'wss://nbstream.binance.com/equity/stream'
    assert result['duplicates'] == 1
    assert len(reader.read('quotes').data) == 1
    reasons = set(reader.read('gaps').data.reason)
    assert {'OBSERVATION_START', 'CONNECTION_FAILURE', 'CONNECT_OR_RECONNECT_WINDOW', 'OBSERVATION_END'} <= reasons


def test_cli_replay_does_not_need_key_or_network(tmp_path, credentials, monkeypatch, capsys):
    from factorlab.integrations.binance_equity.__main__ import main
    sink = Sink(tmp_path / 'run')
    client = Client(network=True, request=lambda *a: Response(200, json.dumps(QUOTE).encode()))
    client.quote(sink, 'NVDA')
    sink.close()
    monkeypatch.delenv('BINANCE_EQUITY_API_KEY')
    monkeypatch.setattr(socket.socket, 'connect', lambda *a: pytest.fail('replay used network'))
    assert main(['replay', '--input', str(sink.root)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report['rows'][0]['bid_price'] == QUOTE['bidPrice']


def test_equity_snapshot_cannot_grant_formal_admission(tmp_path):
    from factorlab.data.admission import assert_formal_market_admission
    from factorlab.errors import ProviderError
    sink = Sink(tmp_path / 'run')
    feed(Processor(sink, 'NVDA', ['quote']), wire_quote())
    reader = sink.close()
    with pytest.raises(ProviderError, match='no qualification package'):
        assert_formal_market_admission(reader, membership=None, calendar=None,
                                       bars=reader.read('quotes').data, benchmark=None,
                                       research_start='2026-11-10', research_end='2026-11-10')


def test_cli_requires_explicit_network_before_creating_output(tmp_path):
    from factorlab.integrations.binance_equity.__main__ import main
    output = tmp_path / 'run'
    with pytest.raises(SystemExit):
        main(['quote', '--output', str(output)])
    assert not output.exists()
