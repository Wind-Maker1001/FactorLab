from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D, ROUND_HALF_EVEN

import numpy as np
import pytest

from factorlab.experiments.grid_funding_adaptive import FundingPolicy
from factorlab.research.grid_simulation import COLUMNS, UnsupportedGridSimulation, simulate
from factorlab.research.grid_simulation.inputs import BatchMarketInputs
from tests.research.test_grid_batch_dynamic import assert_parity, backend, dynamic_config, market_inputs


def funding_market(prices, rates, marks, offsets):
    base = market_inputs(prices)
    times = base.observation_times
    settlements = [times[0] + timedelta(hours=offset) for offset in offsets]
    return BatchMarketInputs(times, funding_times=settlements,
        funding_rates=rates, funding_marks=marks, expected_funding_times=settlements)


@pytest.mark.parametrize('rate', [D('.001'), D('-.001'), D('0')])
def test_funding_own_mark_and_prefill_inventory(rate, backend):
    prices = np.asarray([[100, 89, 111, 100]], dtype=float)
    config = replace(dynamic_config(breakout='pause'), fee_rate=D('0'), slippage=D('0'))
    market = funding_market(prices, [rate, rate], [D('123'), D('77')], [1, 2])
    paper, _ = assert_parity(prices, config, market, backend)
    # The first buy occurs after the first settlement; the second settlement
    # sees that inventory before this point's sell can execute.
    assert paper.trace[0, 1, COLUMNS.index('funding_pnl')] == 0
    receipts = [event for event in paper.metadata['paper_events'][0] if event['kind'] == 6]
    assert D(receipts[0]['quantity']) == 0
    assert D(receipts[1]['quantity']) > 0
    expected = (-D(receipts[1]['quantity']) * D('77') * rate).quantize(
        D('.00000001'), rounding=ROUND_HALF_EVEN)
    assert D(receipts[1]['value']) == expected
    assert paper.terminal[0, COLUMNS.index('total_funding_pnl')] == float(expected)


@pytest.mark.parametrize('rate,expected', [
    (D('.000000005'), D('0')),
    (D('.000000015'), D('-.00000002')),
    (D('-.000000005'), D('0')),
    (D('-.000000015'), D('.00000002')),
    (D('.000000005000000000000001'), D('-.00000001')),
])
def test_decimal_half_even_boundaries(rate, expected, backend):
    prices = np.asarray([[100, 100]], dtype=float)
    config = replace(dynamic_config(initial_inventory=10, quantity=1, lower=99, upper=101,
        levels=1, breakout='pause'), fee_rate=D('0'), slippage=D('0'))
    paper, _ = assert_parity(prices, config, funding_market(prices, [rate], [D('1')], [1]), backend)
    assert paper.terminal[0, COLUMNS.index('total_funding_pnl')] == float(expected)
    assert paper.terminal[0, COLUMNS.index('realized_gross')] == 0


def test_multiple_settlements_in_one_interval_and_history_not_charged(backend):
    prices = np.asarray([[100, 100, 100]], dtype=float)
    config = dynamic_config(initial_inventory=10, quantity=1, levels=1, lower=99, upper=101)
    market = funding_market(prices, [D('.5'), D('.001'), D('-.002'), D('.003')],
        [D('100'), D('101'), D('102'), D('103')], [-1, .25, .5, 1])
    paper, _ = assert_parity(prices, config, market, backend)
    events = paper.events[0][paper.events[0][:, 1] == 6]
    np.testing.assert_array_equal(events[:, 2], [2, 3, 4])
    assert paper.trace[0, 0, COLUMNS.index('total_funding_pnl')] == 0
    assert paper.trace[0, 1, COLUMNS.index('funding_pnl')] == pytest.approx(-.206)


def test_funding_drawdown_precedes_current_fills(backend):
    prices = np.asarray([[100, 89, 111]], dtype=float)
    config = dynamic_config(initial_inventory=10, max_drawdown=.0001, breakout='exit')
    paper, _ = assert_parity(prices, config,
        funding_market(prices, [D('.01')], [D('100')], [1]), backend)
    assert paper.trace[0, 1, COLUMNS.index('reason')] == 2
    assert paper.trace[0, 1, COLUMNS.index('state')] == 1


def test_negative_available_is_explicitly_unsupported(backend):
    prices = np.asarray([[100, 100]], dtype=float)
    config = dynamic_config(initial_inventory=10)
    market = funding_market(prices, [D('100')], [D('100')], [1])
    for engine in ('paper', backend):
        with pytest.raises(UnsupportedGridSimulation):
            simulate(prices, config, market=market, backend=engine, trace=True)


def test_funding_sensitive_gate_causal_history_and_unblock(backend):
    prices = np.asarray([[100, 100, 100, 89, 100]], dtype=float)
    config = replace(dynamic_config(breakout='pause'), funding_policy=FundingPolicy(
        lookback_hours=1, conservative=False, entry_max_bps_h=.5,
        min_net_margin_bps=0., exit_mode='none'))
    market = funding_market(prices, [D('.001'), D('0')], [D('100'), D('100')], [-.5, 1])
    paper, _ = assert_parity(prices, config, market, backend)
    assert paper.trace[0, 0, COLUMNS.index('buy_blocked')] == 1
    assert paper.trace[0, 2, COLUMNS.index('buy_blocked')] == 0
    assert paper.trace[0, 0, COLUMNS.index('total_funding_pnl')] == 0


@pytest.mark.parametrize('policy,rate,reason', [
    (FundingPolicy(entry_max_bps_h=1000., min_net_margin_bps=0.,
                   exit_bps_h=1., max_hold_hours=None), D('.001'), 6),
    (FundingPolicy(entry_max_bps_h=1000., min_net_margin_bps=0.,
                   exit_bps_h=None, max_hold_hours=1.), D('.00001'), 8),
    (FundingPolicy(entry_max_bps_h=1000., min_net_margin_bps=0.,
                   exit_mode='net_margin', max_hold_hours=None), D('1'), 9),
])
def test_funding_sensitive_position_exits(policy, rate, reason, backend):
    prices = np.asarray([[100, 89, 90, 91, 100]], dtype=float)
    config = replace(dynamic_config(breakout='pause'), funding_policy=policy)
    paper, _ = assert_parity(prices, config,
        funding_market(prices, [rate], [D('100')], [2]), backend)
    assert reason in paper.trace[0, :, COLUMNS.index('reason')]


def test_future_funding_does_not_change_past_decisions(backend):
    prices = np.asarray([[100, 89, 100, 89, 100]], dtype=float)
    config = replace(dynamic_config(breakout='pause'), funding_policy=FundingPolicy(
        entry_max_bps_h=1., min_net_margin_bps=0., exit_mode='none'))
    first = funding_market(prices, [D('0'), D('0')], [D('100'), D('100')], [1, 3])
    second = funding_market(prices, [D('0'), D('.01')], [D('100'), D('100')], [1, 3])
    for engine in ('paper', backend):
        a = simulate(prices, config, market=first, backend=engine, trace=True)
        b = simulate(prices, config, market=second, backend=engine, trace=True)
        np.testing.assert_array_equal(a.trace[:, :3], b.trace[:, :3])
