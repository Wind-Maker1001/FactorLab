"""Behavioral checks for parameter-study reductions and selection."""
import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from factorlab.experiments.grid_parameter_study import (
    METRICS, calibration_prefix, nondominated, paired, passive_paths, reduce_paths, stream_seed, summarize,
)
from factorlab.research.grid_simulation import COLUMNS


def test_trace_reduction_uses_executed_fill_units_and_initial_observation():
    trace = np.zeros((1, 3, len(COLUMNS)))
    for name, values in {'equity': [1000, 1001, 1002], 'inventory': [0, .1, .2],
                         'committed': [100, 200, 300], 'fills': [0, 1, 2],
                         'maximum_drawdown': [0, .01, .02]}.items():
        trace[0, :, COLUMNS.index(name)] = values
    fills = [np.array([[1, 1, 0, 2, .1, 100, .004, .006],
                       [2, 2, 0, 1, .1, 90, .0036, .0054]])]
    result = SimpleNamespace(trace=trace, fills=fills)
    values = reduce_paths(result, np.array([[110, 100, 90]]), SimpleNamespace(capital=1000))
    row = dict(zip(METRICS, values[0]))
    assert row['turnover'] == pytest.approx(19)
    assert row['mean_committed'] == 200
    assert row['mean_exposure'] == pytest.approx(28 / 3)
    assert row['maximum_exposure'] == 18
    assert row['net_return'] == pytest.approx(.002)


def test_fractional_cvar_and_unconditional_no_fill_population():
    values = np.zeros((10, len(METRICS)))
    values[:, 0] = -np.arange(10) / 100
    summary = summarize(values)
    assert summary['cvar95'] == pytest.approx(.09)
    assert summary['loss_probability'] == .9
    assert summary['fill_probability'] == 0
    assert summary['profit_per_mean_committed'] is None


def test_pareto_dominance_preserves_equal_and_tradeoff_rows():
    assert nondominated([[0, 2], [1, 1], [2, 2], [0, 2]]) == [0, 1, 3]
    with pytest.raises(ValueError):
        nondominated([[float('nan'), 1]])


def test_paired_results_are_not_unpaired_errors():
    baseline = np.ones((4, len(METRICS))) * np.arange(4)[:, None]
    comparison = paired(baseline + .25, baseline)
    assert comparison['net_return']['mean_difference'] == .25
    assert comparison['net_return']['path_standard_error'] == 0
    with pytest.raises(ValueError):
        paired(baseline, baseline[:2])


def test_calibration_prefix_does_not_fit_validation_returns():
    from factorlab.research.monte_carlo import MonteCarloInput
    timestamps = np.array(['2026-07-14T22', '2026-07-14T23', '2026-07-15T00',
                           '2026-07-15T01'], dtype='datetime64[h]')
    returns = np.array([[.01], [-.02], [.03], [.9]])
    inputs = MonteCarloInput(weights=[1.], historical_returns=returns,
        timestamps=timestamps, available_at=timestamps, assets=('asset',), initial_prices=[100.])
    prefix, receipt = calibration_prefix(inputs, {'source': 'test'}, '2026-07-15T00:00:00Z')
    np.testing.assert_array_equal(prefix.historical_returns, returns[:3])
    expected = np.log1p(returns[:3])
    assert prefix.volatility[0] == pytest.approx(expected.std(ddof=1))
    assert prefix.drift[0] == pytest.approx(expected.mean() + .5 * expected.var(ddof=1))
    assert receipt['returns'] == 3


def test_stage_model_replicate_seed_intervals_do_not_overlap():
    settings = json.loads((Path(__file__).parents[2] / 'configs/research/mc_grid_opt_01.json').read_text())
    seeds = [stream_seed(settings, stage, model, replicate)
             for stage in 'ABCD' for model in settings['models'] for replicate in range(5)]
    assert len(set(seeds)) == len(seeds)
    assert min(np.diff(sorted(seeds))) >= settings['replicate_stride']
    assert max(seeds) + 100000 < 2**32


def test_trace_without_fill_collection_preserves_economics():
    from factorlab.experiments.grid_parameter_space import load_base
    from factorlab.research.grid_simulation import simulate
    base = load_base(Path(__file__).parents[2] / 'configs/mc05_grid_tail.json')
    prices = np.array([[204.34, 180., 190., 180., 200.]])
    full = simulate(prices, base, backend='cpu', trace=True)
    compact = simulate(prices, base, backend='cpu', trace=True, collect_fills=False)
    assert compact.fills is None
    np.testing.assert_array_equal(compact.trace, full.trace)
    np.testing.assert_allclose(reduce_paths(compact, prices, base), reduce_paths(full, prices, base))


def test_fixed_limit_control_matches_existing_r06_execution():
    from factorlab.experiments.grid_parameter_space import load_base
    from factorlab.experiments.grid_exposure_benchmark import simulate, stats
    base = load_base(Path(__file__).parents[2] / 'configs/mc05_grid_tail.json')
    prices = np.array([[204.34, 210., 200., 220., 180.]])
    bars = [{'time': f'2026-11-15T0{i}:00:00Z', 'close': price, 'volume': 10}
            for i, price in enumerate(prices[0])]
    nav, fills = simulate(bars, quantity='.1')
    expected = stats(nav, fills)
    row = dict(zip(METRICS, passive_paths(prices, base)[0]))
    for key, old in [('net_return', 'net_return'), ('maximum_drawdown', 'max_drawdown'),
                     ('fills', 'fills'), ('turnover', 'turnover_usdt'), ('fees', 'fees'),
                     ('slippage', 'slippage'), ('maximum_committed', 'max_committed')]:
        assert row[key] == pytest.approx(expected[old])
    assert row['fills'] == 1
    assert row['terminal_inventory'] == .1
