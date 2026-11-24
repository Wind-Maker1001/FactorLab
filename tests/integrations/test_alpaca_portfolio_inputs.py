import pandas as pd

from factorlab.integrations.alpaca.market_data.inputs import Instrument, normalize_bars
from factorlab.integrations.alpaca.market_data.service import MarketDataService
from test_alpaca_service import Client, calendar


def test_yahoo_projection_keeps_vendor_volume_and_late_adjustment_mask():
    client = Client()
    service = MarketDataService(client, None, calendar())
    instrument = Instrument('a', 'AAPL', pd.Timestamp('2026-11-24T12:00Z'))
    def history(instrument, start, end, *, adjustment='raw'):
        raw = adjustment == 'raw'
        return normalize_bars([
            {'t': '2026-11-20T05:00Z', 'symbol': 'AAPL',
             'o': 10 if raw else 5, 'h': 12 if raw else 6,
             'l': 8 if raw else 4, 'c': 10 if raw else 5,
             'v': 100 if raw else 150}], instrument, calendar=service.calendar,
            timeframe='1Day', received_at='2026-11-24T14:00Z' if raw else '2026-11-24T14:01Z',
            feed='sip', adjustment=adjustment, source='ALPACA:sip')
    service.history = history
    bundle = service.prepare_history([instrument], start='2026-11-20', end='2026-11-23',
                                     now='2026-11-24T15:00Z', adjustment_basis='yahoo_ratio')
    assert bundle.adjusted_open.loc['2026-11-20', 'a'] == 5
    assert bundle.adjusted_volume.loc['2026-11-20', 'a'] == 200
    assert bundle.bars.vendor_adjusted_volume.iloc[0] == 150
    assert bundle.bars.adjusted_bar_feed.iloc[0] == 'sip'
    late = service.prepare_history([instrument], start='2026-11-20', end='2026-11-23',
                                  now='2026-11-24T14:00:30Z', adjustment_basis='yahoo_ratio')
    assert late.adjusted_close.isna().all().all()
    assert late.adjusted_open.isna().all().all()
    assert late.adjusted_volume.isna().all().all()


def test_batch_quotes_share_cutoff_and_reject_predecision_event():
    class Batch(Client):
        def latest(self, kind, symbols):
            self.calls.append(symbols)
            return {'quotes': {
                'AAPL': {'t': '2026-11-24T15:00:04Z', 'bp': 10, 'ap': 11},
                'QQQ': {'t': '2026-11-24T15:00:02Z', 'bp': 20, 'ap': 21},
            }}, self.now()
    client = Batch()
    service = MarketDataService(client, None, calendar())
    instruments = [Instrument(s, s, pd.Timestamp('2026-11-24T12:00Z')) for s in ['AAPL', 'QQQ']]
    quotes = service.refresh_quotes(instruments, decision_at='2026-11-24T15:00:03Z',
                                    decision_window=('2026-11-24T14:30Z', '2026-11-24T21:00Z'))
    assert client.calls == [['AAPL', 'QQQ']]
    assert quotes['AAPL'].valid
    assert quotes['QQQ'].validity_reasons == ('not_after_decision',)


def test_history_trims_wider_cache_without_losing_source():
    client = Client()
    instrument = Instrument('a', 'AAPL', pd.Timestamp('2026-11-24T12:00Z'))
    frame = normalize_bars([
        {'t': d + 'T05:00Z', 'o': 10, 'h': 12, 'l': 8, 'c': 11, 'v': 100}
        for d in ['2026-11-20', '2026-11-23']], instrument, calendar=calendar(),
        timeframe='1Day', received_at=client.now(), feed='sip', source='ALPACA:sip')
    class Cache:
        def update(self, *args, **kwargs):
            return frame, {}
    service = MarketDataService(client, Cache(), calendar())
    result = service.history(instrument, '2026-11-23', '2026-11-23')
    assert result.session.tolist() == [pd.Timestamp('2026-11-23')]
    assert result.source.tolist() == ['ALPACA:sip']
