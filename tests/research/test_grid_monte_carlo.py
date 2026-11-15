"""Close-only scenario accounting, timing and shared numerical contracts."""
from dataclasses import replace
import json

import numpy as np
import pandas as pd
import pytest

from factorlab.experiments.grid_monte_carlo import close_bars, evaluate_path, historical_input, stress_paths
from factorlab.experiments.grid_paper import D, _bars, run_experiment
from factorlab.research.monte_carlo import _loss_tail
from factorlab.strategies.grid import GridConfig
from factorlab.strategies.grid_execution import GridRules


@pytest.fixture
def setup():
    config = GridConfig(lower=80, upper=120, levels=4, initial_price=100,
        initial_inventory=0, initial_cash=1000, quantity=10, max_drawdown=.9,
        max_turnover=100, breakout='pause', fee_bps=4, slippage_bps=6)
    rules = GridRules(D('.01'), D('.01'), D('.01'), D('.01'), D('5'))
    return config, rules


def test_close_only_account_capacity_and_replay(tmp_path, setup):
    config, rules = setup
    prices = np.array([100., 89., 88., 95., 105., 110., 95.])
    outputs = []
    for name, capacity in [('base', '.10'), ('repeat', '.10'), ('low', '.01')]:
        directory = tmp_path/name
        metrics = evaluate_path(prices, start='2026-11-15T00:00:00Z', config=config,
            rules=rules, capacity=capacity, output=directory)
        outputs.append(metrics)
        nav = pd.read_csv(directory/'daily.csv')
        trades = pd.read_csv(directory/'trades.csv')
        assert abs(metrics['reconciliation_residual']) < 1e-8
        equities = np.r_[1000., nav.equity.to_numpy()]
        assert metrics['maximum_drawdown'] == pytest.approx(np.max(1-equities/np.maximum.accumulate(equities)))
        if name != 'low':
            assert not trades.empty
        if not trades.empty:
            assert trades.groupby('execution_date').executed_shares.sum().max() <= float(capacity) + 1e-12
            assert pd.to_datetime(trades.event_time, utc=True).min() > pd.Timestamp('2026-11-15T01:00:00Z')
        cash, quantity, average = 1000., 0., 0.
        for row in nav.itertuples():
            for fill in trades[pd.to_datetime(trades.execution_date, utc=True) == pd.Timestamp(row.date)].itertuples():
                if fill.side == 'BUY':
                    average = (average*quantity + fill.execution_price*fill.executed_shares)/(quantity+fill.executed_shares)
                    quantity += fill.executed_shares
                    cash -= fill.fee
                else:
                    cash += fill.executed_shares*(fill.execution_price-average)-fill.fee
                    quantity -= fill.executed_shares
            assert quantity == pytest.approx(row.inventory)
            assert cash == pytest.approx(row.cash)
            assert cash+quantity*(row.close-average) == pytest.approx(row.equity)
    assert outputs[0] == outputs[1]
    assert outputs[2]['max_inventory'] < outputs[0]['max_inventory']


def test_atr_rejected_without_ohlc(tmp_path, setup):
    config, rules = setup
    with pytest.raises(ValueError, match='ATR'):
        run_experiment(tmp_path/'atr', bars=close_bars([100, 90], '2026-11-15T00:00:00Z'),
            config=replace(config, mode='atr'), rules=rules, capital=1000, fee_rate=0,
            slippage=0, scenario='CLOSE_ONLY_SCENARIO', synthetic_capacity='.1', interval='1h')


def test_close_only_rejects_fabricated_bar_fields_and_inconsistent_price():
    bars = close_bars([100., 90.], '2026-11-15T00:00:00Z')
    bars[0]['high'] = 101.
    with pytest.raises(ValueError, match='must not supply'):
        _bars(bars, None, None, '1h', close_only=True)
    del bars[0]['high']
    bars[0]['close'] = 99.
    with pytest.raises(ValueError, match='equal close'):
        _bars(bars, None, None, '1h', close_only=True)


def test_tail_fractional_boundary_and_negative_losses():
    var, cvar = _loss_tail(np.array([-.2, -.1, .1, .4, .8]), .7)
    assert var == pytest.approx(.34)
    assert cvar == pytest.approx((.8+.5*.4)/1.5)
    assert _loss_tail(np.array([-.4, -.2]), .95)[1] == pytest.approx(-.2)


def test_risk_exit_can_remain_partial(tmp_path, setup):
    config, rules = setup
    metrics = evaluate_path(np.array([100., 89., 88., 70., 69.]),
        start='2026-11-15T00:00:00Z', config=replace(config, breakout='exit', quantity=20),
        rules=rules, capacity='.10', output=tmp_path/'exit')
    assert metrics['exit_triggers'] == 1
    assert abs(metrics['reconciliation_residual']) < 1e-8
    assert metrics['final_state'] == 'EXITING'
    assert metrics['exit_partial_or_unfilled'] >= 1
    assert metrics['terminal_inventory'] > 0


def test_gbm_calibration_rejects_unavailable_snapshot(monkeypatch):
    monkeypatch.setattr('factorlab.experiments.grid_monte_carlo.load_hourly',
        lambda _: ([], {'acquired_at': '2026-11-13T00:00:00Z'}))
    # Availability is checked before any observation can be used for calibration.
    with pytest.raises(ValueError, match='acquisition'):
        historical_input('unused', '2026-11-12T00:00:00Z', 100)


def test_deterministic_stresses_are_price_only():
    paths = stress_paths(100, 168)
    assert len(paths) == 6
    assert all(len(p) == 169 and p[0] == 100 and np.all(p > 0) for p in paths.values())
