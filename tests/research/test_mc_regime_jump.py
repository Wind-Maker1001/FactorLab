"""MC-03 orchestration separates probability groups and reuses Paper authority."""
from dataclasses import replace
import json

import numpy as np
import pandas as pd
import pytest

from factorlab.experiments.grid_monte_carlo import close_bars, evaluate_path
from factorlab.experiments.grid_paper import D
from factorlab.experiments.mc_regime_jump import run_study, scenario_models
from factorlab.research.monte_carlo import MonteCarloInput
from factorlab.research.regime_jump import JumpConfig, JumpStress, PathPlan, RegimeConfig
from factorlab.strategies.grid import GridConfig
from factorlab.strategies.grid_execution import GridRules


def study_input():
    rng = np.random.default_rng(71)
    returns = rng.normal(0, .01, (800, 1))
    times = pd.date_range('2025-01-01', periods=800, freq='h', tz='UTC')
    logs = np.log1p(returns)
    vol = logs.std(ddof=1)
    inputs = MonteCarloInput(weights=[1.], drift=[float(logs.mean() + .5 * vol**2)], volatility=[float(vol)],
        historical_returns=returns, timestamps=times.tz_localize(None).to_numpy(),
        available_at=times.tz_localize(None).to_numpy(), as_of=times[-1].tz_localize(None).to_datetime64(),
        assets=('BINANCE:USDM:NVDAUSDT',), initial_prices=[100.])
    receipt = dict(source='SYNTHETIC_TEST', data_version='seed71', as_of=times[-1].isoformat(),
        observations=[times[0].isoformat(), times[-1].isoformat()], time_unit='hour')
    cfg = RegimeConfig(block_size=5, lookback=6, min_blocks_per_state=2, min_transitions_per_state=2)
    return inputs, receipt, times[299].isoformat(), cfg


def grid():
    config = GridConfig(lower=80, upper=120, levels=4, initial_price=100,
        initial_inventory=0, initial_cash=1000, quantity=10, max_drawdown=.9,
        max_turnover=100, breakout='pause', fee_bps=4, slippage_bps=6)
    return config, GridRules(D('.01'), D('.01'), D('.01'), D('.01'), D('5'))


def test_generated_close_only_prices_use_existing_account(tmp_path):
    model = JumpStress(assets=('A',), initial_prices=[100], as_of='2025-01-01T00:00:00Z',
        source=dict(source='PARAMETERIZED_TEST', data_version='v1'),
        config=JumpConfig(log_drift=(0.,), volatility=(0.,), jump_probability=1,
            jump_log_sizes=(np.log(.96),)))
    _, paths = next(model.batches(PathPlan(n_paths=1, n_steps=5)))
    prices = paths[0, :, 0]
    bars = close_bars(prices, '2025-02-05T00:00:00Z')
    assert all(not {'high', 'low', 'volume'} & set(bar) for bar in bars)
    config, rules = grid()
    metrics = evaluate_path(prices, start='2025-02-05T00:00:00Z', config=config,
        rules=rules, capacity='0.10', output=tmp_path / 'account')
    assert metrics['terminal_inventory'] > 0
    assert metrics['residual_inventory_loss'] > 0
    assert abs(metrics['reconciliation_residual']) < 1e-8


def test_study_probability_separation_compact_artifacts_and_cleanup(tmp_path, monkeypatch):
    inputs, receipt, threshold, cfg = study_input()
    config, rules = grid()
    accounts = []

    def account(prices, *, output, **kwargs):
        output.mkdir()
        (output / 'paper.sqlite').write_bytes(b'test')
        accounts.append(output)
        return dict(terminal_nav=float(prices[-1] * 10), maximum_drawdown=.1,
            terminal_inventory=.1, max_inventory=.2, inventory_mean_age_hours=1.,
            residual_inventory_loss=1., completed_cycle_net=0., fees=.01, slippage=.01)

    monkeypatch.setattr('factorlab.experiments.mc_regime_jump.evaluate_path', account)
    result = run_study(tmp_path / 'study', inputs=inputs, receipt=receipt, config=config, rules=rules,
        start='2025-02-05T00:00:00Z', threshold_as_of=threshold, regime_config=cfg,
        plan=PathPlan(n_paths=3, n_steps=7, batch_size=2))
    assert len(result['distributions']) == 8
    assert len(result['deterministic']) == 6
    assert all(row['paths'] == 3 for row in result['distributions'].values())
    assert result['deterministic_in_probability_distribution'] is False
    assert result['historical_calibration_rejection'] is None
    assert all(not directory.exists() for directory in accounts)
    assert not list((tmp_path / 'study').rglob('*.sqlite'))
    assert len(json.loads((tmp_path / 'study' / 'representative-paths.json').read_text())) == 14
    assert len(pd.read_csv(tmp_path / 'study' / 'paths.csv')) == 30


def test_insufficient_support_keeps_independent_parameterized_jumps():
    inputs, receipt, threshold, cfg = study_input()
    models, rejection = scenario_models(inputs, receipt, threshold_as_of=threshold,
        regime_config=replace(cfg, min_blocks_per_state=10_000))
    assert set(models) == {'jump_background', 'jump_down', 'jump_up'}
    assert 'block support' in rejection
    models, rejection = scenario_models(inputs, receipt, threshold_as_of=threshold,
        regime_config=replace(cfg, min_transitions_per_state=10_000))
    assert 'regime_historical' not in models
    assert 'regime_high_persistence' in models
    assert 'transition support' in rejection


def test_invalid_history_does_not_become_a_calibration_rejection():
    inputs, receipt, threshold, cfg = study_input()
    inputs.historical_returns[10] = np.nan
    with pytest.raises(ValueError, match='finite simple returns'):
        scenario_models(inputs, receipt, threshold_as_of=threshold, regime_config=cfg)


@pytest.mark.parametrize('change', [dict(start='2025-01-01T00:00:00Z'),
    dict(plan=PathPlan(n_paths=65)), dict(plan=PathPlan(n_steps=337)),
    dict(plan=PathPlan(n_paths=2, batch_size=1, seed=2**32 - 1))])
def test_study_rejects_invalid_origin_and_unbounded_work(tmp_path, change):
    inputs, receipt, threshold, cfg = study_input()
    config, rules = grid()
    args = dict(output=tmp_path / 'study', inputs=inputs, receipt=receipt, config=config, rules=rules,
        start='2025-02-05T00:00:00Z', threshold_as_of=threshold, regime_config=cfg)
    args.update(change)
    with pytest.raises(ValueError):
        run_study(**args)
    assert not (tmp_path / 'study').exists()
