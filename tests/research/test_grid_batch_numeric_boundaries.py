from dataclasses import replace
from decimal import Decimal as D

import numpy as np
import pytest

from factorlab.research.grid_simulation import BatchGridConfig, UnsupportedGridSimulation, simulate
from factorlab.strategies.grid import GridConfig
from factorlab.strategies.grid_execution import GridRules


def test_exact_touch_and_minimum_notional():
    cp = pytest.importorskip('cupy')
    if not cp.cuda.is_available():
        pytest.skip('CUDA device required')
    config = BatchGridConfig(GridConfig(lower=80, upper=120, initial_price=100,
        initial_inventory=0, initial_cash=1000, max_cash_committed=1000,
        quantity=5, levels=4, breakout='pause', slippage_bps=0, fee_bps=0),
        GridRules(D('.01'), D('.01'), D('.01'), D('.01'), D('4.5')),
        capacity=D('.05'), fee_rate=D(0), slippage=D(0))
    cpu = simulate([[100, 90, 100]], config, backend='paper', trace=True)
    gpu = simulate([[100, 90, 100]], config, backend='cuda', trace=True)
    np.testing.assert_allclose(gpu.trace, cpu.trace, rtol=1e-12, atol=5e-10)
    np.testing.assert_allclose(gpu.fills[0], cpu.fills[0], rtol=1e-12, atol=5e-10)
    assert len(gpu.fills[0]) == 2


def test_reverse_fragments_accumulate_before_minimum_order():
    cp = pytest.importorskip('cupy')
    if not cp.cuda.is_available():
        pytest.skip('CUDA device required')
    config = BatchGridConfig(GridConfig(lower=80, upper=120, initial_price=100,
        initial_inventory=10, initial_cash=1000, max_cash_committed=1000,
        quantity=10, levels=4, breakout='pause'),
        GridRules(D('.01'), D('.01'), D('.01'), D('.01'), D('5.2')),
        capacity=D('.05'))
    prices = [[100, 111, 111, 99, 111]]
    paper = simulate(prices, config, backend='paper', trace=True)
    gpu = simulate(prices, config, backend='cuda', trace=True)
    np.testing.assert_allclose(gpu.trace, paper.trace, rtol=1e-12, atol=5e-10)
    np.testing.assert_allclose(gpu.fills[0], paper.fills[0], rtol=1e-12, atol=5e-10)
    assert paper.fills[0].shape[0] == 2


def test_ambiguous_nav_boundary_is_explicitly_unsupported():
    cp = pytest.importorskip('cupy')
    if not cp.cuda.is_available():
        pytest.skip('CUDA device required')
    config = BatchGridConfig(GridConfig(lower=80, upper=120, initial_price=100,
        initial_inventory=10, initial_cash=1000, max_cash_committed=1000,
        quantity=1, max_drawdown=.01, levels=4, breakout='pause'),
        GridRules(D('.1'), D('.1'), D('.01'), D('.1'), D('5')),
        capacity=D('.01'))
    with pytest.raises(UnsupportedGridSimulation, match='condition 3'):
        simulate([[100, 90]], config, backend='cuda')
