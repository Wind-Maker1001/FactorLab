from datetime import datetime, timezone

import pandas as pd
import pytest

from factorlab.integrations.alpaca.market_data.cache import MarketCache
from factorlab.integrations.alpaca.market_data.inputs import Instrument, normalize_bars, execution_quote
from factorlab.integrations.alpaca.market_data.service import MarketDataService
from factorlab.integrations.alpaca.market_data.sessions import AlpacaSessionCalendar
from factorlab.integrations.alpaca.market_data.quality import audit_bars, adjustment_relationship


def calendar():
    return AlpacaSessionCalendar([
        {'date': '2026-11-20', 'open': '09:30', 'close': '16:00'},
        {'date': '2026-11-23', 'open': '09:30', 'close': '16:00'},
        {'date': '2026-11-24', 'open': '09:30', 'close': '16:00'}],
        coverage_start='2026-11-20', coverage_end='2026-11-24')


class Client:
    feed = 'iex'
    def __init__(self):
        self.calls = []
    def now(self):
        return datetime(2026, 11, 24, 15, 0, 5, tzinfo=timezone.utc)
    def bars(self, symbols, start, end, *, timeframe, adjustment, asof):
        self.calls.append((start, end, adjustment, asof))
        return [{'t': '2026-11-20T05:00:00Z', 'symbol': symbols[0],
                 'o': 10, 'h': 12, 'l': 9, 'c': 11, 'v': 100}]


def test_service_queries_cache_and_projects_cross_market(tmp_path):
    client = Client()
    service = MarketDataService(client, MarketCache(tmp_path), calendar())
    instrument = Instrument('alpaca-asset-id', 'AAPL', pd.Timestamp('2026-11-24T00:00:00Z'))
    result = service.query(instrument, '2026-11-20', '2026-11-23')
    assert result.data.raw_open.tolist() == [10]
    assert result.metadata.source_id == 'ALPACA:iex'
    assert result.metadata.provider_available_at is None
    assert 'PIT_UNVERIFIED' in result.metadata.evidence_status
    assert result.metadata.field_semantics['raw_volume']['volume_scope'] == 'single_exchange'
    assert client.calls[0][3] == '2026-11-23'


def test_strategy_bundle_acquires_opening_before_executable_quote(tmp_path):
    class Live(Client):
        def __init__(self):
            super().__init__()
            self.current = pd.Timestamp('2026-11-24T15:00:05Z')
        def now(self):
            return self.current.to_pydatetime()
        def bars(self, symbols, start, end, *, timeframe, adjustment, asof):
            if timeframe == '1Min':
                return [{'t': '2026-11-24T14:30:00Z', 'symbol': symbols[0],
                         'o': 10, 'h': 12, 'l': 9, 'c': 11, 'v': 100}]
            return super().bars(symbols, start, end, timeframe=timeframe, adjustment=adjustment, asof=asof)
        def latest(self, kind, symbols):
            self.current += pd.Timedelta(seconds=2)
            return {'quotes': {symbols[0]: {'t': (self.current - pd.Timedelta(seconds=1)).isoformat(),
                                            'bp': 10, 'ap': 11}}}, self.now()
        def clock(self):
            return {'timestamp': self.current.isoformat(), 'is_open': True}, self.now()
    cal = AlpacaSessionCalendar([
        {'date': d, 'open': '09:30', 'close': '16:00'} for d in
        ['2026-11-20', '2026-11-23', '2026-11-24', '2026-11-25']],
        coverage_start='2026-11-20', coverage_end='2026-11-25')
    client = Live()
    service = MarketDataService(client, MarketCache(tmp_path), cal)
    instrument = Instrument('asset-a', 'AAPL', pd.Timestamp('2026-11-24T12:00-05:00'))
    bundle = service.inputs([instrument], start='2026-11-20', end='2026-11-24', now=client.now(),
                            decision_window=('2026-11-24T14:30Z', '2026-11-24T21:00Z'), use_opening=True)
    assert bundle.quotes['asset-a'].valid
    assert bundle.openings['asset-a'].kind == 'bar'
    assert bundle.history.index.max() == pd.Timestamp('2026-11-23')
    assert not bundle.validity.loc['2026-11-23', 'asset-a']
    assert bundle.quotes['asset-a'].event_at > bundle.openings['asset-a'].received_at
    assert bundle.next_session.session == pd.Timestamp('2026-11-25')


def test_cross_symbol_rows_and_invalid_ohlcv_are_rejected():
    instrument = Instrument('a', 'AAPL', pd.Timestamp('2026-11-24T00:00Z'))
    row = {'symbol': 'QQQ', 't': '2026-11-20T05:00Z', 'o': 1, 'h': 2, 'l': 1, 'c': 2, 'v': 4}
    with pytest.raises(ValueError, match='instrument'):
        normalize_bars([row], instrument, calendar=calendar(), timeframe='1Day',
                       received_at='2026-11-24T15:00Z', feed='iex')
    row.update(symbol='AAPL', h=0)
    with pytest.raises(ValueError, match='OHLCV'):
        normalize_bars([row], instrument, calendar=calendar(), timeframe='1Day',
                       received_at='2026-11-24T15:00Z', feed='iex')
    with pytest.raises(ValueError, match='instrument'):
        execution_quote({'S': 'QQQ', 't': '2026-11-24T15:00Z', 'bp': 1, 'ap': 2}, instrument,
                        calendar=calendar(), received_at='2026-11-24T15:00:01Z', now='2026-11-24T15:00:02Z',
                        feed='iex', required_feed='iex', decision_window=('2026-11-24T14:30Z', '2026-11-24T21:00Z'))


def test_quality_preserves_missingness_and_adjustment_volume_basis():
    rows = [{'symbol': 'AAPL', 't': '2026-11-20T05:00:00Z', 'o': 10, 'h': 12, 'l': 9, 'c': 11, 'v': 100}]
    report = audit_bars(rows, calendar().schedule, symbol='AAPL', feed='iex', adjustment='raw')
    assert report['missing_sessions'] == ['2026-11-23', '2026-11-24']
    assert report['volume_scope'] == 'IEX_SINGLE_EXCHANGE'
    split = [dict(rows[0], o=5, h=6, l=4.5, c=5.5, v=200)]
    relation = adjustment_relationship(rows, split)
    assert relation['ratios'][0]['c_ratio'] == .5
    assert relation['ratios'][0]['volume_ratio'] == 2
    assert relation['pit'] == 'UNVERIFIED'
