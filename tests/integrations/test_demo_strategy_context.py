from datetime import timedelta
from decimal import Decimal as D

import pytest

from factorlab.experiments.grid_funding_adaptive import (FundingPolicy, VolatilityPolicy,
    causal_funding_rate_bps_h, causal_realized_vol_1h, adaptive_half_width)
from factorlab.integrations.binance_futures_trading.demo_strategy_context import DemoStrategyContext
from factorlab.integrations.binance_futures_trading.fake import NOW
from factorlab.integrations.binance_futures_trading.protocol import SafetyBlocked
from factorlab.trading.storage import PaperStore


def test_funding_context_matches_research_without_future_or_forecast_leakage(tmp_path):
    path = tmp_path / 'context.sqlite3'
    store = PaperStore(path)
    context = DemoStrategyContext(store)
    policy = FundingPolicy()
    events = []
    for index in range(4):
        effective = NOW + timedelta(hours=index * 8)
        event = (effective, D(100), D('.0001') * (index + 1), str(index))
        events.append(event)
        context.observe('BTCUSDT', 'funding_settlement', event_id=str(index),
            observed_at=effective, effective_at=effective, source='OFFICIAL_DEMO',
            payload=dict(event_id=str(index),funding_rate=event[2],mark_price=event[1]))
    context.observe('BTCUSDT', 'funding_forecast', event_id='forecast', observed_at=NOW,
        effective_at=NOW+timedelta(hours=8), source='OFFICIAL_DEMO',payload=dict(rate=D('.9')))
    at = NOW + timedelta(hours=16)
    expected = causal_funding_rate_bps_h(events, at, lookback_hours=policy.lookback_hours,
                                        conservative=policy.conservative)
    assert context.funding_estimate('BTCUSDT', at=at, policy=policy) == expected
    snapshot = context.symbols(['BTCUSDT','ETHUSDT'],at=at,history_limit=20)
    assert len(snapshot['BTCUSDT']['history']['funding_settlement']) == 3
    assert not snapshot['ETHUSDT']['history']
    store.close()
    store = PaperStore(path)
    context = DemoStrategyContext(store)
    assert context.funding_estimate('BTCUSDT',at=at,policy=policy) == expected
    store.close()


def test_public_event_filters_other_instance_data_requirements(tmp_path):
    from types import SimpleNamespace
    store = PaperStore(tmp_path / 'events.sqlite3')
    context = DemoStrategyContext(store)
    binding = dict(instance_id='one')
    spec = SimpleNamespace(symbols=['ETHUSDT'],
        data=dict(history_limit=2, data_types=['quote']),
        require_binding=lambda offered: None)
    try:
        context.observe('ETHUSDT', 'quote', event_id='quote', observed_at=NOW,
            effective_at=NOW, source='OFFICIAL_DEMO', payload=dict(bid=D(100)))
        event = context.event(spec, binding, event_id='tick', event_time=NOW,
            data_type='quote', account={}, allocation={})
        context.observe('ETHUSDT', 'funding_settlement', event_id='funding', observed_at=NOW,
            effective_at=NOW, source='OFFICIAL_DEMO', payload=dict(amount=D('-1')))
        assert context.event(spec, binding, event_id='tick', event_time=NOW,
            data_type='quote', account={}, allocation={}) == event
        assert set(event['symbols']['ETHUSDT']['history']) == {'quote'}
        assert set(event['symbols']) == {'ETHUSDT'}
    finally:
        store.close()


def test_volatility_context_reuses_research_estimator_and_rejects_gaps(tmp_path):
    store = PaperStore(tmp_path / 'context.sqlite3')
    context = DemoStrategyContext(store)
    policy = VolatilityPolicy(vol_window_hours=3, reference_vol_1h=.01)
    closes = [100., 101., 99., 102.]
    for index, price in enumerate(closes):
        at = NOW + timedelta(hours=index)
        context.observe('ETHUSDT','closed_bar',event_id=str(index),observed_at=at,
            effective_at=at,source='OFFICIAL_DEMO',payload=dict(close=D(str(price))))
    at = NOW + timedelta(hours=3)
    value = context.volatility('ETHUSDT',at=at,policy=policy)
    sigma = causal_realized_vol_1h(closes,3,.01)
    assert value['sigma'] == sigma
    assert value['half_width'] == adaptive_half_width(sigma,policy)
    with pytest.raises(SafetyBlocked,match='warmed up'):
        context.volatility('ETHUSDT',at=NOW+timedelta(hours=2),policy=policy)
    with pytest.raises(SafetyBlocked,match='effective time'):
        context.observe('ETHUSDT','funding_settlement',event_id='future',observed_at=NOW,
            effective_at=NOW+timedelta(hours=1),source='OFFICIAL_DEMO',payload={})
    store.close()
