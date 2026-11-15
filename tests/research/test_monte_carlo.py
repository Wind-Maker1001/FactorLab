"""Mathematical and integration contracts for offline risk simulation."""
from dataclasses import replace
import subprocess
import sys

import numpy as np
import pytest

from factorlab.compute import ComputeConfig, ComputeMemoryError, ComputeUnavailableError
from factorlab.research.monte_carlo import (
    MonteCarloConfig, MonteCarloInput, MonteCarloRunner,
)


def config(**kwargs):
    return MonteCarloConfig(n_paths=400, n_steps=12, seed=42, **kwargs)


def gbm(**kwargs):
    return MonteCarloInput(weights=[1.0], drift=[0.06], volatility=[0.2], **kwargs)


def test_zero_volatility():
    cfg = config()
    result = MonteCarloRunner(cfg).run(MonteCarloInput(
        weights=[0.75], drift=[0.12], volatility=[0.0]))
    expected = cfg.initial_value * (1 + .75 * np.expm1(.12 * cfg.dt)) ** cfg.n_steps
    np.testing.assert_allclose(result.terminal_values, expected)
    np.testing.assert_array_equal(result.maximum_drawdowns, 0)


def test_gbm_analytic_moments():
    cfg = MonteCarloConfig(n_paths=40000, n_steps=24, seed=91, dt=1/24)
    values = MonteCarloRunner(cfg).run(gbm()).terminal_values
    expected_mean = np.exp(.06)
    expected_var = np.exp(.12) * np.expm1(.04)
    assert abs(values.mean() - expected_mean) < 6 * np.sqrt(expected_var / len(values))
    assert abs(values.var() - expected_var) < .06 * expected_var


def test_fixed_seed():
    runner = MonteCarloRunner(config())
    first, second = runner.run(gbm()), runner.run(gbm())
    np.testing.assert_array_equal(first.terminal_values, second.terminal_values)
    np.testing.assert_array_equal(first.maximum_drawdowns, second.maximum_drawdowns)


@pytest.mark.parametrize('matrix', [
    [[1]], [[1, .3], [.1, 1]], [[1, 2], [2, 1]], [[1, 0], [0, .5]],
])
def test_invalid_correlation(matrix):
    with pytest.raises(ValueError):
        MonteCarloRunner(config()).run(MonteCarloInput(
            weights=[.5, .5], drift=[0, 0], volatility=[.1, .2], correlation=matrix))


def test_empirical_asset_correlation():
    cfg = MonteCarloConfig(n_paths=20000, n_steps=1, seed=4, save_full_paths=True)
    data = MonteCarloInput(weights=[.5, .5], drift=[0, 0], volatility=[.2, .3],
        initial_prices=[100, 50], correlation=[[1, .65], [.65, 1]])
    result = MonteCarloRunner(cfg).run(data)
    shocks = np.log(result.asset_paths[:, 1] / [100, 50])
    assert np.corrcoef(shocks.T)[0, 1] == pytest.approx(.65, abs=.025)


def test_recorded_correlation_roundoff_correction():
    result = MonteCarloRunner(config()).run(MonteCarloInput(
        weights=[.5, .5], drift=[0, 0], volatility=[.1, .1],
        correlation=[[1, 1 + 2e-13], [1 + 2e-13, 1]]))
    assert result.metadata['input']['correlation_correction']['minimum_original_eigenvalue'] < 0


def test_singular_correlation_is_valid():
    result = MonteCarloRunner(config()).run(MonteCarloInput(
        weights=[.5, .5], drift=[.02, .02], volatility=[.1, .1],
        correlation=[[1, 1], [1, 1]]))
    assert np.isfinite(result.terminal_values).all()


def bootstrap_input(returns, **kwargs):
    return MonteCarloInput(weights=[.5, .5], historical_returns=returns,
        timestamps=['2024-01-01', '2024-01-02', '2024-01-03'],
        available_at=['2024-01-01', '2024-01-02', '2024-01-03'],
        as_of='2024-01-03', **kwargs)


def test_bootstrap_full_block_sync_and_order():
    cfg = MonteCarloConfig(model='moving_block_bootstrap', n_paths=17, n_steps=3,
        seed=5, block_size=3)
    returns = np.array([[.1, -.05], [-.2, .1], [.3, -.15]])
    result = MonteCarloRunner(cfg).run(bootstrap_input(returns))
    nav = np.r_[1., np.cumprod(1 + returns @ [.5, .5])]
    expected_mdd = np.max(1 - nav / np.maximum.accumulate(nav))
    np.testing.assert_allclose(result.terminal_values, nav[-1])
    np.testing.assert_allclose(result.maximum_drawdowns, expected_mdd)


def test_bootstrap_truncates_last_block_without_wraparound():
    cfg = MonteCarloConfig(model='moving_block_bootstrap', n_paths=3, n_steps=5,
        block_size=3, save_full_paths=True)
    history = np.array([[.1, .02], [-.2, .01], [.3, -.1]])
    result = MonteCarloRunner(cfg).run(bootstrap_input(history))
    expected = np.r_[1., np.cumprod(1 + history[[0, 1, 2, 0, 1]] @ [.5, .5])]
    np.testing.assert_allclose(result.portfolio_paths, np.broadcast_to(expected, (3, 6)))


@pytest.mark.parametrize('change', [
    {'timestamps': ['2024-01-01', '2024-01-01', '2024-01-03']},
    {'available_at': None}, {'as_of': None},
    {'historical_returns': [[.1], [.2], [.3]]},
    {'historical_returns': [[.1, .1], [-1.1, .1], [.1, .1]]},
])
def test_invalid_bootstrap_input(change):
    data = replace(bootstrap_input([[.1, .1]] * 3), **change)
    with pytest.raises(ValueError):
        MonteCarloRunner(config(model='moving_block_bootstrap', block_size=2)).run(data)


def test_saved_nav_matches_existing_drawdown_metric():
    from factorlab.metrics.risk import maximum_drawdown
    cfg = replace(config(save_full_paths=True), initial_value=100.)
    result = MonteCarloRunner(cfg).run(gbm())
    for nav, mdd in zip(result.portfolio_paths[:8], result.maximum_drawdowns[:8]):
        assert mdd == pytest.approx(-maximum_drawdown(nav / cfg.initial_value))
    np.testing.assert_allclose(result.portfolio_paths[:, -1], result.terminal_values)
    assert result.to_dict()['financial_evidence'] is False


def test_default_does_not_retain_paths():
    result = MonteCarloRunner(config()).run(gbm())
    assert result.portfolio_paths is None
    assert result.asset_paths is None


def test_bootstrap_rejects_future_data():
    data = bootstrap_input([[.1, .1]] * 3)
    with pytest.raises(ValueError):
        MonteCarloRunner(config(model='moving_block_bootstrap', block_size=2)).run(
            replace(data, as_of='2024-01-02'))


def test_bootstrap_rejects_late_availability():
    data = bootstrap_input([[.1, .1]] * 3)
    with pytest.raises(ValueError):
        MonteCarloRunner(config(model='moving_block_bootstrap', block_size=2)).run(
            replace(data, available_at=['2024-01-01', '2024-01-02', '2024-01-04']))


def test_bootstrap_insufficient_history():
    with pytest.raises(ValueError):
        MonteCarloRunner(config(model='moving_block_bootstrap', block_size=4)).run(
            bootstrap_input([[.1, .1]] * 3))


def test_cpu_does_not_import_cupy(monkeypatch):
    monkeypatch.setitem(sys.modules, 'cupy', None)
    result = MonteCarloRunner(config()).run(gbm())
    assert result.metadata['compute']['device'] == 'cpu'


def test_gpu_missing_and_fallback(monkeypatch):
    monkeypatch.setitem(sys.modules, 'cupy', None)
    with pytest.raises(ComputeUnavailableError):
        MonteCarloRunner(config(compute=ComputeConfig(device='gpu'))).run(gbm())
    result = MonteCarloRunner(config(compute=ComputeConfig(
        device='gpu', allow_fallback=True))).run(gbm())
    assert result.metadata['compute']['device'] == 'cpu'
    assert result.metadata['compute']['fallback'] is True


@pytest.fixture
def cuda():
    pytest.importorskip('cupy')
    try:
        MonteCarloRunner(MonteCarloConfig(n_paths=2, n_steps=2,
            compute=ComputeConfig(device='gpu'))).run(gbm())
    except ComputeUnavailableError as exc:
        pytest.skip(str(exc))


def test_real_cuda_repeatability_and_pool_budget(cuda):
    cfg = replace(config(), batch_size=53,
        compute=ComputeConfig(device='gpu', gpu_memory_limit_mb=16))
    first = MonteCarloRunner(cfg).run(gbm())
    second = MonteCarloRunner(cfg).run(gbm())
    np.testing.assert_array_equal(first.terminal_values, second.terminal_values)
    execution = first.metadata['compute']['execution']
    assert execution['private_pool_limit_enforced'] is True
    assert execution['private_pool_peak_bytes'] <= 16 * 1024**2
    assert first.metadata['compute']['gpu_memory_limit_enforced'] is False


@pytest.mark.parametrize('batch_size', [None, 1000])
def test_real_cuda_budget_reduces_batch(cuda, batch_size):
    cfg = MonteCarloConfig(n_paths=1000, n_steps=12, batch_size=batch_size,
        compute=ComputeConfig(device='gpu', gpu_memory_limit_mb=1))
    result = MonteCarloRunner(cfg).run(MonteCarloInput(
        weights=[1.], drift=[0.], volatility=[0.]))
    execution = result.metadata['compute']['execution']
    assert execution['batch_size'] < 1000
    assert execution['private_pool_peak_bytes'] <= 1024**2
    np.testing.assert_array_equal(result.terminal_values, 1.)


def test_real_cuda_native_oom_never_reruns_cpu(cuda, monkeypatch):
    import cupy as cp
    import factorlab.research.monte_carlo as mc
    original = mc._cuda_reducer
    calls = []
    def make_reducer(runtime):
        kernel = original(runtime)
        def invoke(grid, block, args):
            calls.append(int(args[1]))
            if args[1] > 2:
                raise cp.cuda.memory.OutOfMemoryError(100, 200, 300)
            return kernel(grid, block, args)
        return invoke
    monkeypatch.setattr(mc, '_cuda_reducer', make_reducer)
    cfg = config(compute=ComputeConfig(device='gpu', allow_fallback=True))
    with pytest.raises(ComputeMemoryError):
        MonteCarloRunner(cfg).run(gbm())
    assert calls == [2, cfg.n_paths]


def test_real_cuda_saved_paths_and_degenerate_gbm(cuda):
    cfg = replace(config(save_full_paths=True),
        compute=ComputeConfig(device='gpu', gpu_memory_limit_mb=16))
    result = MonteCarloRunner(cfg).run(MonteCarloInput(
        weights=[.8], drift=[-.1], volatility=[0.], initial_prices=[100.]))
    expected = (1 + .8 * np.expm1(-.1 * cfg.dt)) ** np.arange(cfg.n_steps + 1)
    np.testing.assert_allclose(result.portfolio_paths, np.broadcast_to(expected, (cfg.n_paths, len(expected))))
    np.testing.assert_allclose(result.maximum_drawdowns, 1 - expected[-1])
    np.testing.assert_allclose(result.asset_paths[:, -1, 0], 100 * np.exp(-.1 * cfg.dt * cfg.n_steps))


def test_real_cuda_impossible_budget(cuda):
    dates = np.datetime64('1800-01-01') + np.arange(70000)
    data = MonteCarloInput(weights=[.5, .5], historical_returns=np.zeros((70000, 2)),
        timestamps=dates, available_at=dates, as_of=str(dates[-1]))
    cfg = MonteCarloConfig(model='moving_block_bootstrap', n_paths=2, n_steps=2,
        compute=ComputeConfig(device='gpu', gpu_memory_limit_mb=1))
    with pytest.raises(ComputeMemoryError):
        MonteCarloRunner(cfg).run(data)


def test_quantile_definition():
    cfg = config(confidence=.95)
    result = MonteCarloRunner(cfg).run(gbm())
    losses = 1 - result.terminal_values / cfg.initial_value
    assert result.var == pytest.approx(np.quantile(losses, .95, method='linear'))
    assert result.cvar >= result.var


@pytest.mark.parametrize('paths,confidence', [(1, .95), (7, .8), (10, .9), (4, 1e-20)])
def test_cvar_fractional_boundary(paths, confidence):
    cfg = MonteCarloConfig(n_paths=paths, n_steps=3, confidence=confidence, seed=18)
    result = MonteCarloRunner(cfg).run(gbm())
    ordered = np.sort(1 - result.terminal_values)[::-1]
    mass = (1 - confidence) * paths
    counts = np.clip(mass - np.arange(paths), 0, 1)
    assert result.cvar == pytest.approx(np.dot(counts, ordered) / mass)


def test_profitable_var_is_negative():
    result = MonteCarloRunner(config()).run(MonteCarloInput(
        weights=[1.], drift=[.1], volatility=[0.]))
    assert result.var < 0
    assert result.cvar == pytest.approx(result.var)
    assert result.loss_probability == 0
    assert result.drawdown_breach_probability == 0


def test_breach_is_strict_and_cash_is_flat():
    result = MonteCarloRunner(config(drawdown_threshold=0)).run(MonteCarloInput(
        weights=[0.], drift=[0.], volatility=[.2]))
    np.testing.assert_array_equal(result.terminal_values, 1.)
    assert result.drawdown_breach_probability == 0


def test_chunk_change_statistical_equivalence():
    cfg = MonteCarloConfig(n_paths=12000, n_steps=10, seed=7, batch_size=12000)
    whole = MonteCarloRunner(cfg).run(gbm())
    chunked = MonteCarloRunner(replace(cfg, batch_size=311)).run(gbm())
    a, b = whole.terminal_values, chunked.terminal_values
    se = np.sqrt((a.var(ddof=1) + b.var(ddof=1)) / len(a))
    assert abs(a.mean() - b.mean()) < 6 * se
    assert chunked.metadata['compute']['execution']['batch_size'] == 311


@pytest.mark.parametrize('kwargs', [{'n_paths': 0}, {'n_steps': 0}, {'seed': -1},
    {'confidence': 1.}, {'dt': 0}, {'block_size': 0}])
def test_invalid_config(kwargs):
    with pytest.raises(ValueError):
        MonteCarloConfig(**kwargs)


def test_tracker_record_is_compact_and_bound(tmp_path):
    from factorlab.experiments import ExperimentTracker
    runner = MonteCarloRunner(config())
    result = runner.run(gbm())
    tracker = ExperimentTracker(tmp_path / 'store')
    tracker.create_experiment('mc-test', {
        'model': 'monte_carlo/gbm',
        'parameters': {'monte_carlo': result.metadata['config'],
            'sample_hash': result.metadata['sample_hash']},
        'factors': [], 'dataset': {'source': 'synthetic'},
        'label': {'kind': 'risk_scenario'},
        'windows': {'train': None, 'validation': None, 'test': None},
        'walk_forward': {'mode': 'not_applicable'}, 'costs': {'rate': 0}})
    runner.record(result, tracker, 'mc-test', 'run-1',
        code={'commit': None, 'tree': None}, command=['python', 'example.py'])
    record = tracker.get_run('run-1')['payload']['run']
    assert record['status'] == 'SUCCEEDED'
    assert record['qualification'] != 'VERIFIED'
    assert record['environment']['compute']['device'] == 'cpu'
    assert record['portfolio_metrics']
    assert record['validation']['sample_hash']
    assert record['validation']['evaluation']
    definition = tracker.get_experiment('mc-test')['payload']
    assert definition['parameters']
    assert (tmp_path / 'store' / 'runs' / 'run-1.json').stat().st_size < 50000
    with pytest.raises(FileExistsError):
        runner.record(result, tracker, 'mc-test', 'run-1',
            code={'commit': None, 'tree': None}, command=['python', 'example.py'])


def test_cpu_only_clean_process():
    script = '''
import sys
sys.modules['cupy'] = None
from factorlab.research.monte_carlo import MonteCarloConfig, MonteCarloInput, MonteCarloRunner
r = MonteCarloRunner(MonteCarloConfig(n_paths=4, n_steps=2)).run(
    MonteCarloInput(weights=[1.], drift=[0.], volatility=[0.]))
assert r.metadata['compute']['device'] == 'cpu'
print('CPU_ONLY_OK')
'''
    completed = subprocess.run([sys.executable, '-c', script],
        text=True, capture_output=True, check=True)
    assert 'CPU_ONLY_OK' in completed.stdout


@pytest.mark.parametrize('weights', [[-0.1], [1.1], [float('nan')]])
def test_invalid_portfolio(weights):
    with pytest.raises(ValueError):
        MonteCarloRunner(config()).run(MonteCarloInput(
            weights=weights, drift=[0], volatility=[.1]))
