import pandas as pd
import pytest

from factorlab.cross_market.contracts import ContractError
from factorlab.integrations.alpaca.market_data.sessions import AlpacaSessionCalendar, diagnose_clock
from factorlab.integrations.alpaca.market_data.inputs import (
    Instrument, normalize_bars, completed_daily_matrix, closed_bars,
    opening_observation, execution_quote,
)


def calendar():
    return AlpacaSessionCalendar([
        {'date': day, 'open': '09:30', 'close': close}
        for day, close in [('2024-12-30', '16:00'), ('2024-12-31', '16:00'),
                           ('2025-01-02', '16:00'), ('2025-03-07', '16:00'),
                           ('2025-03-10', '16:00'), ('2025-11-28', '13:00')]
    ], coverage_start='2024-12-29', coverage_end='2025-12-31')


def instrument():
    return Instrument('asset-1', 'ABC', pd.Timestamp('2025-01-01T00:00Z'))


def bar(timestamp, **extra):
    return dict(t=timestamp, o=10, h=12, l=9, c=11, v=100, **extra)


def test_session_dst_early_close_and_coverage():
    cal = calendar()
    assert cal.at('2025-03-07', 'open') == pd.Timestamp('2025-03-07T14:30Z')
    assert cal.at('2025-03-10', 'open') == pd.Timestamp('2025-03-10T13:30Z')
    assert cal.at('2025-11-28', 'close') == pd.Timestamp('2025-11-28T18:00Z')
    assert cal.regular_session('2025-11-28T18:00Z') is None
    assert cal.first_session('2025-01-02') == pd.Timestamp('2024-12-30')
    assert cal.first_session('2025-01-02', period='year') == pd.Timestamp('2025-01-02')
    assert not cal.is_session('2025-01-01')
    assert cal.next_session('2024-12-31T21:00Z').open == pd.Timestamp('2025-01-02T14:30Z')
    with pytest.raises(ContractError):
        cal.sessions('2024-12-28', '2024-12-29')


def test_clock_uses_injected_now():
    diagnostic = diagnose_clock({'timestamp': '2025-03-10T13:35Z', 'is_open': False},
                                calendar(), now=pd.Timestamp('2025-03-10T13:31Z'))
    assert diagnostic.reasons == ('clock_skew', 'session_state_mismatch')


def test_daily_excludes_current_unfinished_and_future_received():
    cal = calendar()
    rows = [bar('2024-12-30T05:00:00Z'), bar('2024-12-31T05:00:00Z', complete=False),
            bar('2025-01-02T05:00:00Z')]
    bars = normalize_bars(rows, instrument(), calendar=cal, timeframe='1Day',
                          received_at='2025-01-02T15:00Z', feed='iex')
    matrix = completed_daily_matrix(bars, now='2025-01-02T16:00Z')
    assert list(matrix.index) == [pd.Timestamp('2024-12-30')]
    assert list(matrix.columns) == ['asset-1']
    assert completed_daily_matrix(bars, now='2025-01-02T14:00Z').empty
    assert bars.iloc[0].original_event_at == rows[0]['t']
    assert set(bars.feed) == {'iex'}
    assert len(closed_bars(bars, now='2025-01-02T16:00Z')) == 1


def test_adjusted_columns_and_stable_identity():
    renamed = Instrument('asset-1', 'NEW', pd.Timestamp('2025-03-10T00:00Z'))
    bars = normalize_bars([bar('2025-03-10T13:30Z')], renamed, calendar=calendar(),
                          timeframe='1Min', received_at='2025-03-10T13:31Z',
                          feed='sip', adjustment='split')
    assert 'adjusted_close' in bars and 'close' not in bars
    assert bars.iloc[0].instrument_id == 'asset-1'
    assert bars.iloc[0].symbol == 'NEW'
    with pytest.raises(ValueError, match='raw'):
        closed_bars(bars, now='2025-03-10T13:32Z')


def test_opening_first_rth_not_auction_and_causal_quote():
    cal, asset = calendar(), instrument()
    opening = opening_observation([
        {'t': '2025-03-10T13:29:59Z', 'p': 9},
        {'t': '2025-03-10T13:30:02Z', 'p': 11},
        {'t': '2025-03-10T13:30:01Z', 'p': 10},
    ], asset, calendar=cal, session='2025-03-10',
        received_at='2025-03-10T13:30:05Z', now='2025-03-10T13:30:10Z', feed='iex')
    assert opening.price == 10 and opening.kind == 'trade'
    kwargs = dict(calendar=cal, received_at='2025-03-10T13:30:09Z',
                  now='2025-03-10T13:30:10Z', feed='iex', required_feed='iex',
                  decision_window=('2025-03-10T13:30Z', '2025-03-10T14:00Z'), opening=opening)
    quote = execution_quote({'t': '2025-03-10T13:30:04Z', 'bp': 10, 'ap': 11}, asset, **kwargs)
    assert quote.validity_reasons == ('not_after_opening_observation',)
    quote = execution_quote({'t': '2025-03-10T13:30:06Z', 'bp': 10, 'ap': 11}, asset, **kwargs)
    assert quote.valid
    kwargs['received_at'] = '2025-03-10T13:30:04Z'
    assert not execution_quote({'t': '2025-03-10T13:30:06Z', 'bp': 10, 'ap': 11}, asset, **kwargs).valid
    kwargs.update(received_at='2025-03-10T13:30:09Z', required_feed='sip')
    quote = execution_quote({'t': '2025-03-10T13:30:06Z', 'bp': 10, 'ap': 11}, asset, **kwargs)
    assert quote.feed == 'iex' and 'feed_mismatch' in quote.validity_reasons


def test_minute_opening_requires_completed_received_bar():
    kwargs = dict(calendar=calendar(), session='2025-03-10', feed='sip', kind='bar',
                  received_at='2025-03-10T13:30:30Z', now='2025-03-10T13:30:40Z')
    assert opening_observation([bar('2025-03-10T13:30Z')], instrument(), **kwargs) is None
    kwargs.update(received_at='2025-03-10T13:31:01Z', now='2025-03-10T13:31:02Z')
    assert opening_observation([bar('2025-03-10T13:30Z')], instrument(), **kwargs).kind == 'bar'
