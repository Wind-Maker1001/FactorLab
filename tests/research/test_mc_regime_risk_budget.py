"""Causal paired research, accounting and retained low-participation outcomes."""
from dataclasses import replace
import json

import numpy as np
import pandas as pd
import pytest

from factorlab.experiments.grid_monte_carlo import evaluate_path
from factorlab.experiments.grid_paper import D, run_experiment
from factorlab.experiments.grid_regime_budget import make_budget_factory
from factorlab.experiments.mc_regime_risk_budget import (
    METRICS, account_metrics, calibration_state, execute_account, run_study)
from factorlab.research.monte_carlo import MonteCarloInput
from factorlab.research.regime_jump import PathPlan, RegimeConfig
from factorlab.strategies.grid import GridConfig
from factorlab.strategies.grid_execution import GridRules


def setup():
    rng = np.random.default_rng(71)
    returns = rng.normal(0, .01, (800, 1))
    times = pd.date_range('2025-01-01', periods=800, freq='h', tz='UTC')
    inputs = MonteCarloInput(weights=[1.], drift=[0.], volatility=[.01], historical_returns=returns,
        timestamps=times.tz_localize(None).to_numpy(), available_at=times.tz_localize(None).to_numpy(),
        as_of=times[-1].tz_localize(None).to_datetime64(),
        assets=('BINANCE:USDM:NVDAUSDT',), initial_prices=[100.])
    receipt = dict(source='SYNTHETIC_TEST', data_version='seed71', as_of=times[-1].isoformat(),
        observations=[times[0].isoformat(), times[-1].isoformat()], time_unit='hour')
    config = GridConfig(lower=80, upper=120, levels=4, initial_price=100,
        initial_inventory=0, initial_cash=1000, quantity=10, max_drawdown=.9,
        max_turnover=100, breakout='pause', fee_bps=4, slippage_bps=6)
    rules = GridRules(D('.01'), D('.01'), D('.01'), D('.01'), D('5'))
    return dict(inputs=inputs, receipt=receipt, config=config, rules=rules,
        start='2025-02-05T00:00:00Z', threshold_as_of=times[299].isoformat(),
        regime_config=RegimeConfig(block_size=5, lookback=6, min_blocks_per_state=2,
                                   min_transitions_per_state=2))


def test_threshold_matches_observable_mc03_prefix_and_rejects_unavailable():
    args = setup()
    threshold, seeds, identity = calibration_state(args['inputs'], args['receipt'], args['threshold_as_of'], 100.)
    logs = np.log1p(args['inputs'].historical_returns[:, 0])
    expected = np.median(np.lib.stride_tricks.sliding_window_view(logs[:300], 24).std(axis=1, ddof=1))
    assert threshold == expected
    np.testing.assert_allclose(np.diff(np.log(seeds)), logs[-24:], rtol=1e-12, atol=1e-15)
    assert seeds[-1] == 100.
    assert identity['latent_generator_labels_used'] is False
    args['inputs'].available_at[-1] = np.datetime64('2026-01-01')
    with pytest.raises(ValueError, match='available'):
        calibration_state(args['inputs'], args['receipt'], args['threshold_as_of'], 100.)


def test_default_account_and_neutral_factory_have_identical_financial_exports(tmp_path):
    args = setup()
    prices = np.array([100., 89., 89., 101., 111., 100.])
    common = {k: args[k] for k in ('config', 'rules', 'start')}
    original = evaluate_path(prices, output=tmp_path / 'A', capacity='.10', **common)
    factory = make_budget_factory(volatility_threshold=.01, seed_prices=[100.] * 25, policy='neutral')
    neutral = evaluate_path(prices, output=tmp_path / 'neutral', capacity='.10', grid_factory=factory, **common)
    assert original == neutral
    for name in ('daily.csv', 'trades.csv', 'result.json', 'evaluation.json'):
        assert (tmp_path / 'A' / name).read_bytes() == (tmp_path / 'neutral' / name).read_bytes()
    row, audit, evaluation = account_metrics(tmp_path / 'A', original)
    assert row['traded_quantity'] > 0
    assert row['fees'] > 0
    assert audit['terminal_nav'] == pytest.approx(audit['exported_terminal_equity'])
    assert abs(audit['fifo_residual']) < 1e-8
    assert evaluation


def test_zero_budget_is_retained_with_no_fabricated_execution(tmp_path):
    args = setup()
    result, audit, _ = execute_account(np.array([100., 90., 90., 100.]), output=tmp_path / 'account',
        config=args['config'], rules=args['rules'], start=args['start'], capacity='.10',
        threshold=.01, seed_prices=[100.] * 25, arm='static', static_scale=0.)
    assert result['zero_fills'] == 1
    assert result['terminal_nav'] == 1000
    assert result['average_exposure'] == result['fees'] == result['risk_exit_quantity'] == 0
    assert audit['ledger_fills'] == 0


def test_factory_conflicts_rejected_before_creating_account(tmp_path):
    with pytest.raises(ValueError, match='dedicated strategy'):
        run_experiment(tmp_path / 'account', bars=[], config=None, rules=None, capital=1000,
            fee_rate=0, slippage=0, inventory_risk='age', grid_factory=object())
    assert not (tmp_path / 'account').exists()


def fake_account(calls):
    def execute(prices, *, output, arm, static_scale, **kwargs):
        output.mkdir()
        (output / 'paper.sqlite').write_bytes(b'temporary')
        calls.append((np.array(prices), arm, static_scale, output))
        scale = 1. if arm == 'A' else .5 if arm == 'B' else static_scale
        terminal = 1000. + (float(prices[-1]) - 100.) * scale
        metrics = {key: 0. for key in METRICS}
        metrics.update(terminal_nav=terminal, net_return=terminal / 1000 - 1,
            maximum_drawdown=.1 * scale, average_exposure=100. * scale,
            terminal_inventory=0., residual_inventory_loss=0., completed_cycle_net=0.,
            reconciliation_residual=0.)
        return metrics, dict(fifo_residual=0.), {}
    return execute


def test_paired_paths_calibration_isolation_retained_losses_and_compact_cleanup(tmp_path, monkeypatch):
    args = setup()
    calls = []
    monkeypatch.setattr('factorlab.experiments.mc_regime_risk_budget.execute_account', fake_account(calls))
    summary = run_study(tmp_path / 'study', **args, models=('jump_background', 'jump_down'),
        plan=PathPlan(n_paths=2, n_steps=5, batch_size=2, seed=714),
        calibration_seed=10714, calibration_paths=1)
    root = tmp_path / 'study'
    rows = pd.read_csv(root / 'paths.csv')
    assert len(rows) == 30  # Four stochastic and six deterministic paths, three arms.
    assert rows.groupby(['model', 'path_id']).path_hash.nunique().eq(1).all()
    assert rows.groupby(['model', 'path_id']).arm.nunique().eq(3).all()
    for i in range(0, 4, 2):
        np.testing.assert_array_equal(calls[i][0], calls[i + 1][0])
    for i in range(4, len(calls), 3):
        np.testing.assert_array_equal(calls[i][0], calls[i + 1][0])
        np.testing.assert_array_equal(calls[i][0], calls[i + 2][0])
    calibration = json.loads((root / 'calibration.json').read_text())
    assert set(calibration['scales'].values()) == {.5}
    assert {r['path_hash'] for r in calibration['rows']}.isdisjoint(set(rows.path_hash))
    assert (rows.terminal_nav < 1000).any()
    assert rows.zero_fills.eq(0).all()  # Stub outcomes retained without activity filtering.
    assert summary['status'] == 'SUCCEEDED'
    assert summary['qualification'] == 'FINANCIAL_NOT_ESTABLISHED'
    assert len(summary['distributions']) == 6
    assert all(g['paths'] == 2 for g in summary['distributions'].values())
    assert summary['deterministic_in_probability_distribution'] is False
    assert len(summary['deterministic']) == 18
    assert len(json.loads((root / 'representative-tails.json').read_text())) == 16
    assert all(not path.exists() for _, _, _, path in calls)
    assert not list(root.glob('**/paper.sqlite*'))
    # Frozen generator streams and static selection reproduce without re-fitting on evaluation.
    again = run_study(tmp_path / 'again', **args, models=('jump_background', 'jump_down'),
        plan=PathPlan(n_paths=2, n_steps=5, batch_size=2, seed=714),
        calibration_seed=10714, calibration_paths=1)
    pd.testing.assert_frame_equal(rows, pd.read_csv(tmp_path / 'again' / 'paths.csv'))
    assert again['definition_hash'] == summary['definition_hash']


def test_time_budget_marks_partial_without_starting_incomplete_pairs(tmp_path, monkeypatch):
    args = setup()
    ticks = iter([0., 2., 3.])
    monkeypatch.setattr('factorlab.experiments.mc_regime_risk_budget.perf_counter', lambda: next(ticks))
    summary = run_study(tmp_path / 'partial', **args, models=('jump_down',),
        plan=PathPlan(n_paths=1, n_steps=5), max_seconds=1.)
    assert summary['status'] == 'TIME_BUDGET_EXHAUSTED'
    assert summary['paths_completed'] == summary['account_evaluations'] == 0
    run = json.loads((tmp_path / 'partial/tracker/runs/study.json').read_text())
    assert run['payload']['run']['status'] == 'FAILED'


@pytest.mark.parametrize('changes', [dict(calibration_seed=714), dict(max_seconds=1801),
    dict(config=GridConfig(mode='atr')), dict(models=('not_a_model',)),
    dict(start='2025-01-01T00:00:00Z')])
def test_invalid_study_inputs_do_not_create_output(tmp_path, changes):
    args = setup()
    args.update(changes)
    with pytest.raises(ValueError):
        run_study(tmp_path / 'invalid', **args, plan=PathPlan(seed=714))
    assert not (tmp_path / 'invalid').exists()
