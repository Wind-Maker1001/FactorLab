from dataclasses import replace
from decimal import Decimal as D

import numpy as np
import pytest

from factorlab.research.grid_simulation import (
    BatchGridConfig, COLUMNS, UnsupportedGridSimulation, freeze_prices, simulate,
)
from factorlab.strategies.grid import GridConfig
from factorlab.strategies.grid_execution import GridRules


@pytest.fixture
def config():
    return BatchGridConfig(GridConfig(lower=80, upper=120, levels=4, initial_cash=1000,
        initial_inventory=0, initial_price=100, quantity=10, max_inventory=100,
        max_cash_committed=1000, breakout='pause', order_ttl=100,
        fee_bps=4, slippage_bps=6), GridRules(D('.1'), D('.1'), D('.01'), D('.1'), D('5')),
        capacity=D('1'))


CASES = [
    [100, 101, 105, 110, 115, 120, 125],
    [100, 95, 90, 85, 80, 75, 70],
    [100, 89, 101, 89, 111, 89, 101],
    [100, 81, 119, 81, 119, 79, 100],
    [100, 100, 100, 100, 100, 100, 100],
    [100, 90, 89.946032, 90, 110, 110.06604, 100],
    [100, .000001, 1000000, 1, 100, 100, 100],
]


@pytest.fixture
def backend():
    return 'cpu'


@pytest.mark.parametrize('mode', ['arithmetic', 'geometric'])
@pytest.mark.parametrize('breakout', ['pause', 'exit'])
def test_paper_stepwise_parity(config, mode, breakout, backend):
    config = replace(config, grid=replace(config.grid, mode=mode, breakout=breakout))
    prices = np.array(CASES)
    cpu = simulate(prices, config, backend='paper', trace=True)
    gpu = simulate(prices, config, backend=backend, trace=True, batch_size=3)
    np.testing.assert_allclose(gpu.trace, cpu.trace, rtol=1e-12, atol=5e-10,
                               err_msg=str(COLUMNS))
    for actual, expected in zip(gpu.fills, cpu.fills):
        np.testing.assert_allclose(actual, expected, rtol=1e-12, atol=5e-10)
    assert gpu.risk(config.capital) == pytest.approx(cpu.risk(config.capital), abs=1e-12)


@pytest.mark.parametrize('change', [
    {'capacity': D('.2')},
    {'capacity': D('1.5')},
    {'capacity': D('3')},
    {'capacity': D('.01')},
    {'fee_rate': D('0'), 'slippage': D('0')},
    {'capital': D('150')},
])
def test_partial_zero_cost_and_cash_limits(config, change, backend):
    config = replace(config, **change)
    prices = np.asarray([[100] + [89, 101] * 12], dtype=float)
    cpu = simulate(prices, config, backend='paper', trace=True)
    gpu = simulate(prices, config, backend=backend, trace=True)
    np.testing.assert_allclose(gpu.trace, cpu.trace, rtol=1e-12, atol=5e-10)


@pytest.mark.parametrize('ttl', [1, 2, 3])
def test_expiry_and_endowed_inventory(config, ttl, backend):
    config = replace(config, grid=replace(config.grid, initial_inventory=10, order_ttl=ttl))
    prices = np.asarray([[100, 111, 89, 111, 89, 130, 100]], dtype=float)
    cpu = simulate(prices, config, backend='paper', trace=True)
    gpu = simulate(prices, config, backend=backend, trace=True)
    np.testing.assert_allclose(gpu.trace, cpu.trace, rtol=1e-12, atol=5e-10)


@pytest.mark.parametrize('mode', ['arithmetic', 'geometric'])
def test_volatile_random_step_and_fill_parity(config, mode, backend):
    config = replace(config, grid=replace(config.grid, mode=mode, initial_inventory=5,
                                         max_drawdown=.03, max_turnover=.3), capacity=D('1.5'))
    rng = np.random.default_rng(9187)
    prices = freeze_prices(100*np.exp(np.column_stack((np.zeros(64),
                            rng.normal(0, .03, (64, 64)).cumsum(axis=1)))))
    paper = simulate(prices, config, backend='paper', trace=True)
    result = simulate(prices, config, backend=backend, trace=True, batch_size=13)
    np.testing.assert_allclose(result.trace, paper.trace, rtol=1e-12, atol=5e-10)
    for actual, expected in zip(result.fills, paper.fills):
        np.testing.assert_allclose(actual, expected, rtol=1e-12, atol=5e-10)


def test_existing_mc_runner_integration(config, backend):
    from factorlab.research.monte_carlo import MonteCarloConfig, MonteCarloInput, MonteCarloRunner
    result = MonteCarloRunner(MonteCarloConfig(n_paths=4, n_steps=8, seed=71,
        dt=1., save_full_paths=True)).run(MonteCarloInput(weights=[1.],
            drift=[0.], volatility=[.03], initial_prices=[100.]))
    prices = freeze_prices(result.asset_paths[:, :, 0])
    paper = simulate(prices, config, backend='paper', trace=True)
    cpu = simulate(prices, config, backend=backend, trace=True)
    np.testing.assert_allclose(cpu.trace, paper.trace, rtol=1e-12, atol=5e-10)


def test_reject_unrepresented_inputs(config):
    with pytest.raises(UnsupportedGridSimulation, match='pause/exit'):
        replace(config, grid=replace(config.grid, breakout='rebuild'))
    with pytest.raises(UnsupportedGridSimulation, match='budget'):
        replace(config, grid=replace(config.grid, budget=100))
    with pytest.raises(UnsupportedGridSimulation, match='collateral'):
        replace(config, capital=D('1'), grid=replace(config.grid, initial_inventory=1))
    with pytest.raises(UnsupportedGridSimulation, match='6 decimal'):
        simulate([[100, 90.123456789]], config)
    np.testing.assert_array_equal(freeze_prices([[100, 90.123456789]]), [[100, 90.123457]])
