from types import SimpleNamespace

import pandas as pd
import pytest

from factorlab.integrations.alpaca.cloud_execution import PreparationIncomplete
from factorlab.integrations.alpaca.market_data.inputs import Instrument, normalize_bars
from factorlab.integrations.alpaca.scheduling import schedule_window
from factorlab.integrations.alpaca.strategy_data import StrategyData
from test_alpaca_scheduling import at, calendar


def setup(data_types):
    now = at(24, 9, 31)
    instrument = Instrument('official-spy', 'SPY', now)
    assets = SimpleNamespace(instruments=lambda symbols: (instrument,))
    spec = SimpleNamespace(symbols=['SPY'], data=dict(data_types=data_types, history_limit=1))
    binding = dict(market='ALPACA_US_EQUITY', account_id='paper', instance_id='alpha',
                   allocation_id='sleeve', strategy_id='example')
    cal = calendar()
    window = schedule_window(cal, dict(kind='calendar', calendar_id='alpaca',
        timezone='America/New_York', event='session_open'), now)
    return StrategyData(assets, clock=lambda: now), instrument, spec, binding, cal, window, now


def test_quote_only_event_does_not_acquire_bars_and_uses_execution_time():
    data, instrument, spec, binding, cal, window, now = setup(['quote'])
    calls = []
    quote = SimpleNamespace(valid=True, received_at=pd.Timestamp(now), event_at=pd.Timestamp(now),
                            source='ALPACA:iex', feed='iex', bid=10, ask=11)
    def refresh(instruments, **kwargs):
        calls.append(kwargs)
        return {instrument.instrument_id: quote}
    service = SimpleNamespace(calendar=cal, refresh_quotes=refresh)
    event = data.event(service, spec, binding, {'cash': 100}, window)
    assert calls[0]['decision_at'] == now
    assert calls[0]['decision_window'] == (at(24, 9, 30), at(24, 16, 0))
    assert event['data_type'] == 'quote'
    assert set(event['symbols']['SPY']['observations']) == {'quote'}
    assert event['symbols']['SPY']['history'] == {'quote': []}
    assert event['symbols']['SPY']['observations']['quote']['payload']['feed'] == 'iex'


@pytest.mark.parametrize('last_day', [20, 23])
def test_daily_history_requires_exact_prior_official_session(last_day):
    data, instrument, spec, binding, cal, window, now = setup(['closed_bar'])
    frame = normalize_bars([dict(t=at(last_day, 0, 0).isoformat(), o=10, h=11, l=9, c=10, v=5)],
        instrument, calendar=cal, timeframe='1Day', received_at=now, feed='iex')
    service = SimpleNamespace(calendar=cal, history=lambda *args, **kwargs: frame)
    if last_day == 20:
        with pytest.raises(PreparationIncomplete, match='INCOMPLETE_HISTORY:SPY'):
            data.event(service, spec, binding, {'cash': 100}, window)
    else:
        event = data.event(service, spec, binding, {'cash': 100}, window)
        history = event['symbols']['SPY']['history']['closed_bar']
        assert len(history) == 1
        assert history[0]['effective_at'] == at(23, 16, 0)
        assert history[0]['payload']['instrument_id'] == 'official-spy'
