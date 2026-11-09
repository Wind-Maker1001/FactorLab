"""Formal R14 API integration with execution capacity and cost diagnostics."""
from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

from examples.research.execution_capacity_synthetic import fixture
from factorlab.application.research_pipeline import feedback_execution
from factorlab.backtest.capacity import ResearchExecutionScenario
from factorlab.backtest.execution import AShareExecutionBacktester, ExecutionBacktestConfig
from factorlab.evaluation.execution_risk import execution_diagnostics, paired_constraint_difference
from factorlab.portfolio import PortfolioOptimizer, OptimizerConfig


from factorlab.evaluation.return_attribution import ReturnAttribution, attribute_execution


def execute(scenario, bars=None, predictions=None, returns=None):
    b, benchmark, p, r, _, _ = fixture()
    b = b if bars is None else bars
    p = p if predictions is None else predictions
    r = r if returns is None else returns
    first = p.date.min()
    return feedback_execution(p, r, model_name='fixture',
        optimizer=PortfolioOptimizer(OptimizerConfig(objective='score', investment=.8, max_weight=.5)),
        lookback=5, every=3,
        engine=AShareExecutionBacktester(ExecutionBacktestConfig(initial_cash=100000), research_scenario=scenario),
        bars=b[b.date >= first], benchmark=benchmark[benchmark.date >= first])


def attribute(result, decision, **kwargs):
    return execution_diagnostics(result, initial_cash=100000, calendar=pd.DatetimeIndex(result.nav.date),
        decisions=decision[['date', 'estimated_gross_security_turnover']].rename(
            columns={'date': 'signal_date', 'estimated_gross_security_turnover': 'target_turnover'}), **kwargs)


def test_actual_r14_reconciliation_and_counterfactual_scope():
    *_, liquidity, evidence = fixture()
    scenario = ResearchExecutionScenario(participation_limit=.02, impact_coefficient_bps=20, liquidity=liquidity)
    _, decisions, result = execute(scenario)
    actual = attribute(result, decisions)
    assert isinstance(actual, ReturnAttribution)
    injected = attribute(result, decisions, attribution=attribute_execution)
    pd.testing.assert_frame_equal(actual.daily, injected.daily)
    pd.testing.assert_frame_equal(actual.execution, injected.execution)
    assert actual.summary == injected.summary
    assert actual.summary['gross_pnl_same_fills'] - actual.summary['costs'] == pytest.approx(actual.summary['net_pnl'])
    assert actual.summary['market_impact'] > 0
    assert actual.summary['fill_rate_shares'] < 1
    assert actual.summary['target_turnover'] > 0
    assert (actual.daily.base_slippage >= -1e-12).all()
    _, rd, rr = execute(None)
    reference = attribute(rr, rd)
    diff = paired_constraint_difference(actual, reference)
    assert set(diff.scope) == {'HYPOTHETICAL_REEXECUTION_DIFFERENCE_NOT_ATTRIBUTION'}
    assert (diff.net_return_difference != 0).any()


def test_formal_attribution_without_fills():
    _, decisions, result = execute(ResearchExecutionScenario(participation_limit=.02))
    assert result.trades.empty
    actual = attribute(result, decisions)
    assert isinstance(actual, ReturnAttribution)
    assert actual.summary['costs'] == 0
    assert actual.summary['net_pnl'] == 0
    assert actual.summary['gross_pnl_same_fills'] == 0
    assert actual.summary['fill_rate_shares'] == 0
    assert actual.daily.cash.eq(100000).all()
    assert actual.daily.strategy_nav.eq(1).all()


def test_future_liquidity_risk_and_signal_perturbation_preserves_prior_decisions():
    bars, _, predictions, returns, liquidity, _ = fixture()
    cutoff = predictions.date.unique()[10]
    scenario = ResearchExecutionScenario(participation_limit=.1, impact_coefficient_bps=20, liquidity=liquidity)
    original = execute(scenario)
    future = tuple(replace(x, average=x.average * 100) if pd.Timestamp(x.known_at).normalize() > cutoff else x for x in liquidity)
    bars.loc[bars.date > cutoff, ['raw_open', 'raw_close']] *= 1.02
    predictions.loc[predictions.date > cutoff, 'score'] *= -10
    returns.loc[returns.index > cutoff] *= -2
    changed = execute(replace(scenario, liquidity=future), bars, predictions, returns)
    for a, b in zip(original[:2], changed[:2]):
        pd.testing.assert_frame_equal(a[a.date <= cutoff], b[b.date <= cutoff])
    a, b = original[2].trades, changed[2].trades
    pd.testing.assert_frame_equal(a[a.execution_date <= cutoff], b[b.execution_date <= cutoff])


def test_fixed_fills_extra_slippage_reduces_net_contribution():
    # A small extra charge leaves these fills unchanged; hold cash feasibility fixed.
    low_t, low_d, low = execute(None)
    high_t, high_d, high = execute(ResearchExecutionScenario(fixed_slippage_bps=.1))
    pd.testing.assert_frame_equal(low_t, high_t)
    pd.testing.assert_frame_equal(low.trades[['execution_date', 'symbol', 'side', 'executed_shares']],
                                  high.trades[['execution_date', 'symbol', 'side', 'executed_shares']])
    low_a, high_a = attribute(low, low_d), attribute(high, high_d)
    assert high_a.summary['net_return'] < low_a.summary['net_return']
    np.testing.assert_allclose(high_a.summary['gross_pnl_same_fills'], low_a.summary['gross_pnl_same_fills'], rtol=0, atol=1e-7)
