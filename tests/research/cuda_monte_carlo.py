"""CUDA hardware acceptance; run explicitly with pytest on a CUDA host."""
from dataclasses import replace

import numpy as np
import pytest

from factorlab.compute import ComputeConfig, ComputeMemoryError
from factorlab.research.monte_carlo import (
    MonteCarloConfig, MonteCarloInput, MonteCarloRunner,
)


def config(**kwargs):
    return MonteCarloConfig(n_paths=400, n_steps=12, seed=42, **kwargs)


def gbm(**kwargs):
    return MonteCarloInput(weights=[1.0], drift=[0.06], volatility=[0.2], **kwargs)


@pytest.fixture
def cuda():
    import cupy
    MonteCarloRunner(MonteCarloConfig(n_paths=2, n_steps=2,
        compute=ComputeConfig(device='gpu'))).run(gbm())


@pytest.mark.parametrize('model', ['gbm', 'moving_block_bootstrap'])
@pytest.mark.parametrize('weights', [[.05] * 20, [1 + 1e-12], [.3, .4],
    [1 - 5e-13]])
def test_real_cuda_effective_portfolio_weights(cuda, model, weights):
    from tests.research.test_monte_carlo import check_effective_portfolio_weights
    check_effective_portfolio_weights(model, weights,
        ComputeConfig(device='gpu', gpu_memory_limit_mb=16))


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
