"""Synthetic contract tests, not real financial observations."""
import json
import socket
from pathlib import Path

import pandas as pd
import pytest

from factorlab.data.hub.contracts import UnsupportedQueryError
from factorlab.errors import DataQualityError
from factorlab.integrations.binance_research import BinanceResearch
from factorlab.integrations.binance_equity.market import rest_quote, stream_rows
from factorlab.integrations.binance_equity.store import Sink

SOURCE = 'BINANCE_EQUITY'
ID = 'BINANCE:EQUITY:NVDA'
T = 1794322800000
A = '2026-11-10T15:10:00+00:00'
B = '2026-11-10T15:11:00+00:00'


def candle(closed=True, close='102'):
    return dict(e='kline', E=T + 300000, s='NVDA',
                k=dict(t=T, ct=T+299999, s='NVDA', i='5m', x=closed,
                       o='101', h='103', l='100', c=close, v='0.000000001'))


def snapshot(tmp_path, events=None, mutate=None):
    sink = Sink(tmp_path / 'equity')
    for event, acquired in events or [(candle(), A)]:
        receipt = sink.raw(json.dumps(event).encode(), acquired_at=acquired)
        kind, rows = stream_rows(event, acquired, receipt)
        if mutate:
            mutate(rows[0])
        sink.add(kind, rows)
    reader = sink.close()
    return BinanceResearch(SOURCE, sink.root, expected_data_version=reader.manifest['data_version'])


def read(bridge, kind='bars', **kwargs):
    args = dict(instrument_id=ID, start='2026-11-10T00:00:00Z',
                end='2026-11-11T00:00:00Z', observation_cutoff=B)
    args.update(kwargs)
    return bridge.read(kind, **args)


def test_cutoff_versions_precision_and_offline(tmp_path, monkeypatch):
    bridge = snapshot(tmp_path, [(candle(), A), (candle(close='102.123456789'), B)])
    monkeypatch.setattr(socket.socket, 'connect', lambda *a: pytest.fail('network'))
    monkeypatch.setattr(socket, 'create_connection', lambda *a, **k: pytest.fail('network'))
    first = read(bridge, observation_cutoff=A)
    latest = read(bridge)
    assert first.data.close.tolist() == ['102']
    assert latest.data.close.tolist() == ['102.123456789']
    assert latest.data.volume.tolist() == ['0.000000001']
    assert str(latest.data.event_time.dt.tz) == 'UTC'
    assert latest.data.event_time.iloc[0] == pd.Timestamp(T, unit='ms', tz='UTC')
    assert latest.data.server_event_time.iloc[0] == pd.Timestamp(T + 300000, unit='ms', tz='UTC')
    assert latest.data.instrument_type.tolist() == ['STOCK_SPOT']
    assert latest.metadata.provenance['verified_raw_receipts'] == 2
    assert 'HISTORICAL_AVAILABILITY_UNVERIFIED' in latest.metadata.evidence_status
    assert 'FORMAL_ADMISSION_BLOCKED' in latest.metadata.evidence_status


def test_utc_half_open_and_naive_rejection(tmp_path):
    bridge = snapshot(tmp_path)
    stamp = pd.Timestamp(T, unit='ms', tz='UTC')
    assert read(bridge, start=stamp, end=stamp + pd.Timedelta(seconds=1)).data.shape[0] == 1
    assert read(bridge, end=stamp).data.empty
    assert read(bridge, start=stamp.tz_convert('Asia/Shanghai')).data.shape[0] == 1
    with pytest.raises(UnsupportedQueryError, match='zoned'):
        read(bridge, observation_cutoff='2026-11-10 16:00')
    with pytest.raises(UnsupportedQueryError, match='cutoff'):
        read(bridge, observation_cutoff='2026-11-10T14:00:00Z')


def test_rest_quote_null_time(tmp_path):
    sink = Sink(tmp_path / 'quote')
    value = dict(symbol='NVDA', bidPrice='101.00001', askPrice='102', bidSize=10, askSize=20)
    receipt = sink.raw(json.dumps(value).encode(), acquired_at=A)
    sink.add('quotes', rest_quote(value, 'NVDA', A, receipt))
    reader = sink.close()
    bridge = BinanceResearch(SOURCE, sink.root, expected_data_version=reader.manifest['data_version'])
    with pytest.raises(UnsupportedQueryError, match='event time unavailable'):
        read(bridge, 'quotes')
    result = read(bridge, 'quotes', time_basis='acquired_at')
    assert result.data.event_time.isna().all()
    assert result.data.server_event_time.isna().all()
    assert result.data.currency.tolist() == ['USD']


def test_open_and_missing_closed_bars(tmp_path):
    bridge = snapshot(tmp_path, [(candle(False), A)])
    with pytest.raises(UnsupportedQueryError, match='EQUITY_CLOSED_BAR_RESEARCH_NOT_AVAILABLE'):
        read(bridge)
    assert read(bridge, 'open_bars').data.closed.tolist() == [False]


@pytest.mark.parametrize('mutation,match', [
    (lambda r: r.update(closed=False), 'closed/open'),
    (lambda r: r.update(currency='USDT'), 'currency'),
    (lambda r: r.update(quantity_unit='contracts'), 'unit'),
    (lambda r: r.update(close='NaN'), 'nonfinite'),
    (lambda r: r.update(high='1'), 'OHLC'),
    (lambda r: r.pop('volume'), 'missing'),
    (lambda r: r.update(raw_receipt='unknown'), 'receipt'),
])
def test_bad_source_rows_rejected(tmp_path, mutation, match):
    with pytest.raises(DataQualityError, match=match):
        read(snapshot(tmp_path, mutate=mutation))


def test_duplicate_observations_rejected(tmp_path):
    bridge = snapshot(tmp_path, [(candle(), A), (candle(close='102.1'), A)])
    with pytest.raises(DataQualityError, match='duplicate'):
        read(bridge)


def test_unknown_identity_and_types(tmp_path):
    bridge = snapshot(tmp_path)
    with pytest.raises(UnsupportedQueryError, match='spot'):
        read(bridge, instrument_id='BINANCE:USDM:NVDAUSDT')
    with pytest.raises(UnsupportedQueryError, match='unsupported'):
        read(bridge, 'funding')
    with pytest.raises(UnsupportedQueryError, match='unknown'):
        BinanceResearch('UNKNOWN', bridge.root, expected_data_version='x')
    with pytest.raises(UnsupportedQueryError, match='NOT_AVAILABLE'):
        read(bridge, instrument_id='BINANCE:EQUITY:AAPL')


@pytest.mark.parametrize('target', ['raw', 'parquet', 'version', 'manifest'])
def test_tamper_rejected(tmp_path, target):
    bridge = snapshot(tmp_path)
    if target == 'version':
        bridge.expected_data_version = 'bad'
    elif target == 'manifest':
        path = bridge.root / 'manifest.json'
        body = json.loads(path.read_text())
        body['state'] = 'changed'
        path.write_text(json.dumps(body))
    else:
        path = next(bridge.root.glob('raw/*.bin' if target == 'raw' else '*.parquet'))
        path.write_bytes(b'corrupt')
    with pytest.raises(DataQualityError):
        read(bridge)


def test_source_level_gaps_and_symbols(tmp_path):
    from factorlab.integrations.binance_equity.market import discovery
    sink = Sink(tmp_path / 'context')
    payload = dict(timezone='UTC', symbols=[dict(symbol='NVDA')])
    receipt = sink.raw(json.dumps(payload).encode(), acquired_at=A)
    sink.add('symbols', discovery(payload, A, receipt))
    sink.add('gaps', [dict(start=A, end=B, reason='CONNECT_OR_RECONNECT_WINDOW')])
    reader = sink.close()
    bridge = BinanceResearch(SOURCE, sink.root, expected_data_version=reader.manifest['data_version'])
    assert len(read(bridge, 'symbols', time_basis='acquired_at').data) == 1
    gaps = read(bridge, 'gaps', instrument_id=None, time_basis='acquired_at',
                end='2030-01-01T00:00:00Z', observation_cutoff='2030-01-01T00:00:00Z')
    assert gaps.data.instrument_id.isna().all()
    assert gaps.data.event_time.isna().all()
    with pytest.raises(UnsupportedQueryError, match='identity'):
        read(bridge, 'gaps')


def test_funding_special_decimal_and_perpetual_corruption(tmp_path):
    from factorlab.integrations.binance.store import publish
    from factorlab.integrations.binance.market import funding, mark, bars
    root = tmp_path / 'perpetual'
    pending = root / 'pending'
    (pending / 'requests').mkdir(parents=True)
    real = Path(__file__).resolve().parents[2] / 'docs/validation/p6-b01/nvda/runs/real/manifest.json'
    metadata = json.loads(real.read_text())
    for key in ('schema_version', 'boundaries', 'tables', 'raw_receipts', 'data_version'):
        metadata.pop(key)
    start = metadata['range']['start_ms']
    end = metadata['range']['end_ms']
    events = funding([dict(symbol='NVDAUSDT', fundingTime=start,
                           fundingRate='-0.000000123456789', rateType=kind)
                      for kind in ('Regular', 'Special')], 'NVDAUSDT', start, end)
    tables = dict(funding=events,
                  bars=bars([], start, end, 86400000, end),
                  mark=mark(dict(symbol='NVDAUSDT', time=start, markPrice='100',
                                 indexPrice='100'), 'NVDAUSDT'))
    native = publish(root, pending, tables, metadata)
    bridge = BinanceResearch('BINANCE_USDM_PUBLIC_REST', native.root,
                             expected_data_version=native.manifest['data_version'])
    result = perp_read(bridge, 'funding')
    assert result.data.rate_type.tolist() == ['Regular', 'Special']
    assert result.data.funding_rate.tolist() == ['-0.000000123456789'] * 2
    with pytest.raises(UnsupportedQueryError, match='coverage'):
        perp_read(bridge, 'funding', end='2027-01-01T00:00:00Z')
    with pytest.raises(UnsupportedQueryError, match='pin'):
        BinanceResearch('BINANCE_USDM_PUBLIC_REST', root,
                        expected_data_version=native.manifest['data_version'])


@pytest.fixture
def perpetual():
    root = Path(__file__).resolve().parents[2] / 'docs/validation/p6-b01/nvda/runs/real'
    manifest = json.loads((root / 'manifest.json').read_text())
    return BinanceResearch('BINANCE_USDM_PUBLIC_REST', root,
                          expected_data_version=manifest['data_version'])


def perp_read(bridge, kind, **kwargs):
    args = dict(instrument_id='BINANCE:USDM:NVDAUSDT', start='2026-10-31T00:00:00Z',
                end='2026-11-10T00:00:00Z', observation_cutoff='2026-11-11T00:00:00Z')
    args.update(kwargs)
    return bridge.read(kind, **args)


def test_real_perpetual_filter_funding_and_mark(perpetual, monkeypatch):
    monkeypatch.setattr(socket.socket, 'connect', lambda *a: pytest.fail('network'))
    bars = perp_read(perpetual, 'bars', start='2026-11-01T00:00:00Z', end='2026-11-03T00:00:00Z')
    assert len(bars.data) == 2
    assert bars.data.quote_asset.unique().tolist() == ['USDT']
    assert bars.data.margin_asset.unique().tolist() == ['USDT']
    assert 'funding_rate' not in bars.data
    funding = perp_read(perpetual, 'funding')
    assert len(funding.data) == 30
    assert funding.data.rate_type.unique().tolist() == ['Regular']
    assert 'open' not in funding.data
    with pytest.raises(UnsupportedQueryError, match='snapshot'):
        perp_read(perpetual, 'mark')
    mark = perp_read(perpetual, 'mark', purpose='snapshot',
                     end='2026-11-11T00:00:00Z')
    assert len(mark.data) == 1
    with pytest.raises(UnsupportedQueryError, match='contract'):
        perp_read(perpetual, 'bars', instrument_id=ID)
    with pytest.raises(UnsupportedQueryError, match='cutoff'):
        perp_read(perpetual, 'bars', observation_cutoff='2026-11-01T00:00:00Z')
