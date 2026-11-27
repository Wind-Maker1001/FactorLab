from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

import numpy as np
import pytest

from factorlab.experiments.grid_funding_adaptive import FundingPolicy, VolatilityPolicy
from factorlab.research.grid_simulation import BatchGridConfig, COLUMNS, simulate
from factorlab.research.grid_simulation.inputs import BatchMarketInputs
from factorlab.research.grid_simulation.paper import canonical_order_events
from factorlab.strategies.grid import GridConfig
from factorlab.strategies.grid_execution import GridRules


def dynamic_config(**changes):
    grid = GridConfig(lower=80, upper=120, levels=4, initial_cash=1000,
        initial_inventory=0, initial_price=100, quantity=10, max_inventory=100,
        max_cash_committed=1000, breakout='rebuild', order_ttl=100,
        min_rebuild_interval=1, fee_bps=4, slippage_bps=6)
    return BatchGridConfig(replace(grid, **changes),
        GridRules(D('.1'), D('.1'), D('.01'), D('.1'), D('5')), capacity=D('1'))


def market_inputs(prices, **kwargs):
    anchor = datetime(2026, 1, 1, 0, 59, 59, 999999, tzinfo=timezone.utc)
    times = [anchor + timedelta(hours=i) for i in range(prices.shape[1])]
    return BatchMarketInputs(times, **kwargs)


@pytest.fixture
def backend():
    return 'cpu'


def assert_parity(prices, config, market, backend):
    paper = simulate(prices, config, market=market, backend='paper', trace=True)
    native = simulate(prices, config, market=market, backend=backend, trace=True)
    np.testing.assert_allclose(native.trace, paper.trace, rtol=1e-12, atol=5e-10,
        err_msg=str(COLUMNS))
    for actual, expected in zip(native.fills, paper.fills):
        np.testing.assert_allclose(actual, expected, rtol=1e-12, atol=5e-10)
    for actual, expected in zip(native.events, paper.events):
        np.testing.assert_allclose(canonical_order_events(actual), canonical_order_events(expected),
            rtol=1e-12, atol=5e-10)
    return paper, native


@pytest.mark.parametrize('mode', ['arithmetic', 'geometric'])
def test_breakout_rebuild_and_old_order_fill(mode, backend):
    prices = np.asarray([[100, 89, 125, 115, 95, 135, 100]], dtype=float)
    paper, _ = assert_parity(prices, dynamic_config(mode=mode), market_inputs(prices), backend)
    assert paper.terminal[0, COLUMNS.index('rebuilds')] > 0
    assert len(paper.fills[0]) > 0


@pytest.mark.parametrize('window', [1, 2, 3])
def test_atr_real_highlow_and_deferred_cancellation(window, backend):
    prices = np.asarray([[100, 101, 99, 105, 100, 102, 98]], dtype=float)
    config = dynamic_config(mode='atr', atr_window=window, atr_multiplier=2.,
        rebuild_threshold=.25)
    market = market_inputs(prices, highs=prices + np.asarray([[1, 2, 8, 1, 3, 2, 1]]),
        lows=prices - np.asarray([[1, 1, 2, 2, 1, 4, 1]]))
    paper, _ = assert_parity(prices, config, market, backend)
    assert paper.terminal[0, COLUMNS.index('rebuilds')] > 0
    if window > 1:
        assert paper.trace[0, 0, COLUMNS.index('state')] == 4


def test_atr_same_point_complete_bar_cancels_remain_pending(backend):
    prices = np.asarray([[100, 100, 101, 101, 101]], dtype=float)
    config = dynamic_config(mode='atr', atr_window=1, atr_multiplier=2.,
        rebuild_threshold=.1)
    paper, _ = assert_parity(prices, config,
        market_inputs(prices, highs=prices + 2, lows=prices - 2), backend)
    pending = paper.trace[0, :, COLUMNS.index('pending_rebuild')]
    assert np.any(pending == 1)
    requests = paper.events[0][paper.events[0][:, 1] == 2]
    confirmations = paper.events[0][paper.events[0][:, 1] == 3]
    assert any(np.any((confirmations[:, 2] == row[2]) &
        (confirmations[:, 0] > row[0])) for row in requests)


@pytest.mark.parametrize('warmup', [[100.], [100., 100., 100.], [99., 101., 100.]])
def test_volatility_real_subclass_fallback_and_extreme(warmup, backend):
    prices = np.asarray([[100, 100, 101, 99, 100, 100]], dtype=float)
    config = replace(dynamic_config(levels=1, lower=98, upper=102),
        volatility_policy=VolatilityPolicy(vol_window_hours=3,
            reference_vol_1h=.004, min_rebuild_interval=1, extreme_vol_mult=2.))
    paper, _ = assert_parity(prices, config, market_inputs(prices, warmup_closes=warmup), backend)
    assert np.all(paper.trace[0, :, COLUMNS.index('volatility')] > 0)


@pytest.mark.parametrize('profile', ['C', 'ATR', 'B'])
def test_continuous_history_order_capacity(profile, backend):
    prices = np.full((1, 2004), 100.)
    config = dynamic_config(order_ttl=1)
    if profile == 'C':
        config = replace(config, grid=replace(config.grid, levels=1, lower=98, upper=102),
            volatility_policy=VolatilityPolicy(vol_window_hours=3,
                reference_vol_1h=.004, min_rebuild_interval=1))
        market = market_inputs(prices, warmup_closes=[100.])
    elif profile == 'ATR':
        prices[0] = 100. + np.arange(prices.shape[1]) % 2
        config = replace(config, grid=replace(config.grid, mode='atr', atr_window=1,
            atr_multiplier=2., rebuild_threshold=.01))
        market = market_inputs(prices, highs=prices + 2, lows=prices - 2)
    else:
        config = replace(config, funding_policy=FundingPolicy(
            entry_max_bps_h=1., min_net_margin_bps=0., exit_mode='none'))
        settlement = market_inputs(prices).observation_times[0] - timedelta(hours=1)
        market = market_inputs(prices, funding_times=[settlement], funding_rates=[D('0')],
            funding_marks=[D('100')], expected_funding_times=[settlement])
    paper, native = assert_parity(prices, config, market, backend)
    proposals = paper.events[0][paper.events[0][:, 1] == 1]
    assert len(proposals) > 1000
    np.testing.assert_allclose(native.terminal, paper.terminal, rtol=1e-12, atol=5e-10)
    if profile == 'ATR':
        assert paper.terminal[0, COLUMNS.index('rebuilds')] > 100


def test_future_ohlc_and_prices_do_not_change_past(backend):
    first = np.asarray([[100, 101, 99, 105, 100, 102]], dtype=float)
    second = first.copy()
    second[0, 4:] = [110, 90]
    config = dynamic_config(mode='atr', atr_window=2, atr_multiplier=2.)
    for engine in ('paper', backend):
        a = simulate(first, config, market=market_inputs(first, highs=first + 2, lows=first - 2),
            backend=engine, trace=True)
        b = simulate(second, config, market=market_inputs(second,
                highs=np.concatenate((first[:, :4] + 2, second[:, 4:] + 5), axis=1),
                lows=np.concatenate((first[:, :4] - 2, second[:, 4:] - 3), axis=1)), backend=engine, trace=True)
        np.testing.assert_array_equal(a.trace[:, :4], b.trace[:, :4])
