"""Lifecycle diagnosis through the shared hourly Paper research entry point."""
from dataclasses import replace
from decimal import Decimal as D
import json

import pandas as pd
import pytest

from factorlab.experiments.grid_lifecycle import lifecycle_metrics
from factorlab.experiments.grid_paper import run_experiment
from factorlab.strategies.grid import GridConfig
from factorlab.strategies.grid_execution import GridRules
from factorlab.trading.contracts import Market


def inputs():
    prices = [100, 100, 100, 90, 100, 100, 105]
    bars = [dict(id=str(i), time=t.isoformat(), price=p, close=p,
                 high=p+1, low=p-1, volume=100, completed_bar=True)
            for i, (t, p) in enumerate(zip(pd.date_range('2026-01-01', periods=len(prices), freq='h', tz='UTC'), prices))]
    return dict(bars=bars, config=GridConfig(lower=80, upper=120, levels=4,
        initial_cash=1000, initial_inventory=0, quantity=1, order_ttl=2,
        expiry_reseed_limit=2, expiry_reseed_interval=2, fee_bps=0, slippage_bps=0),
        rules=GridRules(D(1), D(1), D('.01'), D(1), D(1)), capital=1000,
        fee_rate=0, slippage=0, participation=1, interval='1h', market=Market.BINANCE_USDM)


def test_reseed_hourly_round_trip_and_reproduction(tmp_path):
    settings = inputs()
    for name in ('first', 'repeat'):
        run_experiment(tmp_path/name, **settings)
    first, repeat = (lifecycle_metrics(tmp_path/name) for name in ('first', 'repeat'))
    assert first == repeat
    assert first['grid_cycles'] == 1
    assert first['buy_fills'] == first['sell_fills'] == 1
    assert first['end_inventory'] == 0 and first['final_equity'] == 1010
    assert first['position_hours'] == pytest.approx(1)
    assert first['inventory_mean_age_hours'] == 1
    assert first['expiry_reseeds'] > 0
    assert first['ttl_confirmed_cancellations'] > 0
    pd.testing.assert_frame_equal(pd.read_csv(tmp_path/'first/daily.csv'), pd.read_csv(tmp_path/'repeat/daily.csv'))
    trades = pd.read_csv(tmp_path/'first/trades.csv')
    assert pd.Timestamp(trades.event_time.iloc[0]) > pd.Timestamp(settings['bars'][2]['time']) + pd.Timedelta(minutes=59)
    run_experiment(tmp_path/'legacy', **(settings | dict(config=replace(settings['config'], expiry_reseed_limit=0))))
    assert lifecycle_metrics(tmp_path/'legacy')['fills'] == 0


def test_reseed_future_prices_do_not_change_past(tmp_path):
    settings = inputs()
    run_experiment(tmp_path/'original', **settings)
    settings['bars'][-1].update(price=110, close=110, high=111, low=109)
    run_experiment(tmp_path/'changed', **settings)
    pd.testing.assert_frame_equal(pd.read_csv(tmp_path/'original/daily.csv').iloc[:-1],
                                  pd.read_csv(tmp_path/'changed/daily.csv').iloc[:-1])


def test_exit_with_zero_liquidity_keeps_actual_inventory(tmp_path):
    settings = inputs()
    settings['config'] = replace(settings['config'], order_ttl=100, breakout='exit')
    for bar, price, volume in zip(settings['bars'], [100, 90, 70, 75, 80, 85, 90], [100, 100, 0, 0, 0, 0, 0]):
        bar.update(price=price, close=price, high=price+1, low=price-1, volume=volume)
    run_experiment(tmp_path/'blocked', **settings)
    metrics = lifecycle_metrics(tmp_path/'blocked')
    assert metrics['exit_triggers'] == 1 and metrics['exit_success_rate'] == 0
    assert metrics['final_state'] == 'EXITING' and metrics['end_inventory'] == 1
    assert metrics['sell_fills'] == 0
    result = json.loads((tmp_path/'blocked/result.json').read_text(encoding='utf-8'))
    assert any(order.get('purpose') == 'RISK_EXIT' for order in result['orders'].values())
