"""Deterministic optional capacity, cash and fee contracts."""
from dataclasses import replace
import math

import pandas as pd
import pytest

from factorlab.backtest.capacity import LiquidityObservation, ResearchExecutionScenario, ResearchFeeQuote
from factorlab.backtest.execution import AShareExecutionBacktester, ExecutionBacktestConfig, PositionBook
from factorlab.backtest.fees import FeeSchedule
from factorlab.evaluation.execution_risk import order_outcomes
from tests.research.test_portfolio_execution import DAYS, SYMBOLS, targets, reconcile
from tests.research.test_execution_backtest_phase3 import _bars, _benchmark

A = SYMBOLS[0]


def observation(average=2000, basis='shares', **kwargs):
    return LiquidityObservation(A, str(DAYS[0].date()), str(DAYS[0] + pd.Timedelta(hours=15)),
                                average, basis, 'deterministic fixture', 'SYNTHETIC', **kwargs)


def run(scenario=None, cash=10000, bars=None):
    engine = AShareExecutionBacktester(ExecutionBacktestConfig(initial_cash=cash),
                                      research_scenario=scenario)
    return engine.run_target_weights(targets((0, {A: .8}), (2, {A: 0})),
        _bars(DAYS, [A]) if bars is None else bars, _benchmark(DAYS))


def test_zero_incremental_cost_preserves_every_receipt():
    old, new = run(), run(ResearchExecutionScenario())
    for name in ('nav', 'trades', 'positions', 'rejections', 'target_comparison'):
        pd.testing.assert_frame_equal(getattr(old, name), getattr(new, name))


@pytest.mark.parametrize('basis,average', [('shares', 2000), ('cny', 20000)])
def test_partial_capacity_and_exact_cash(basis, average):
    scenario = ResearchExecutionScenario(participation_limit=.1,
        impact_coefficient_bps=20, liquidity=(observation(average, basis),))
    result = run(scenario)
    assert list(result.trades.executed_shares) == [200, 200]
    first = result.trades.iloc[0]
    assert first.market_impact == pytest.approx(2000 * 20 * math.sqrt(.1) / 10000)
    assert result.rejections.rejected_shares.sum() == 600
    assert order_outcomes(result).status.tolist() == ['PARTIAL', 'FILLED']
    reconcile(result)


def test_reject_policy_and_missing_liquidity():
    scenario = ResearchExecutionScenario(participation_limit=.1, fill_policy='reject',
                                          liquidity=(observation(),))
    result = run(scenario)
    assert result.trades.empty
    assert set(result.rejections.reason) == {'capacity_constraint'}
    assert order_outcomes(result).status.tolist() == ['REJECTED']
    missing = run(replace(scenario, liquidity=()))
    assert missing.trades.empty
    assert set(missing.rejections.reason) == {'liquidity_NOT_COMPUTABLE'}


def test_cash_search_includes_nonlinear_cost_and_fixed_path_monotonicity():
    low = ResearchExecutionScenario(impact_coefficient_bps=1, liquidity=(observation(),))
    high = replace(low, fixed_slippage_bps=50, impact_coefficient_bps=100)
    low_quote = ResearchFeeQuote(FeeSchedule.a_share_default(), low, observation(), 10)
    high_quote = ResearchFeeQuote(FeeSchedule.a_share_default(), high, observation(), 10)
    for notional in (1000, 5000, 9000):
        assert high_quote.calculate(DAYS[1], 'buy', notional).total > low_quote.calculate(DAYS[1], 'buy', notional).total
    # 8,000 target demand cannot afford its extra charge, despite baseline affordability.
    result = run(replace(high, fixed_slippage_bps=2000), cash=8100)
    assert (result.trades.cash_after >= 0).all()
    reconcile(result, 8100)


def test_future_liquidity_and_prices_do_not_change_prior_fills():
    base = ResearchExecutionScenario(participation_limit=.1, liquidity=(observation(),))
    future = replace(observation(), effective_date=str(DAYS[-1].date()),
                     known_at=str(DAYS[-1] + pd.Timedelta(hours=15)), average=99999999)
    bars = _bars(DAYS, [A])
    bars.loc[bars.date == DAYS[-1], 'raw_close'] *= 2
    a, b = run(base), run(replace(base, liquidity=base.liquidity + (future,)), bars=bars)
    pd.testing.assert_frame_equal(a.trades, b.trades)
    late = replace(observation(), known_at=str(DAYS[1] + pd.Timedelta(hours=9)))
    assert run(replace(base, liquidity=(late,))).trades.empty


def test_same_day_aggregate_capacity_and_t_plus_one():
    from factorlab.backtest.execution import _prepare_bars
    scenario = ResearchExecutionScenario(participation_limit=.15, liquidity=(observation(),))
    engine = AShareExecutionBacktester(research_scenario=scenario)
    book, trades, rejects = PositionBook(), [], []
    bars = _prepare_bars(_bars(DAYS, [A]), engine.config)
    kwargs = dict(book=book, day_bars=bars[bars.date == DAYS[1]].set_index('symbol'),
        trade_rows=trades, rejection_rows=rejects, signal_date=DAYS[0], execution_date=DAYS[1],
        symbol=A, requested_shares=200, metadata=None)
    first = engine._execute_order(side='buy', cash=10000, **kwargs)
    second = engine._execute_order(side='buy', cash=first[-1], **kwargs)
    assert first[0] == 200 and second[0] == 100
    assert sum(t['executed_shares'] for t in trades) == 300
    assert engine._execute_order(side='sell', cash=second[-1], **kwargs) is None
    assert rejects[-1]['reason'] == 't_plus_one'


def test_historical_liquidity_revision_does_not_replace_newer_effective_observation():
    current = replace(observation(), effective_date=str(DAYS[1].date()),
                      known_at=str(DAYS[1] + pd.Timedelta(hours=15)), average=3000)
    revision = replace(observation(), known_at=str(DAYS[2] + pd.Timedelta(hours=15)), average=9000)
    scenario = ResearchExecutionScenario(liquidity=(revision, current, observation()))
    assert scenario.observation(A, DAYS[0]).average == 2000
    assert scenario.observation(A, DAYS[2]).average == 3000


def test_sale_cost_cannot_overdraw_cash():
    from factorlab.backtest.execution import _prepare_bars
    scenario = ResearchExecutionScenario(fixed_slippage_bps=20000)
    engine = AShareExecutionBacktester(research_scenario=scenario)
    book = PositionBook()
    book.buy(A, DAYS[0], 200)
    trades, rejected = [], []
    bars = _prepare_bars(_bars(DAYS, [A]), engine.config)
    result = engine._execute_order(book=book, day_bars=bars[bars.date == DAYS[1]].set_index('symbol'),
        trade_rows=trades, rejection_rows=rejected, signal_date=DAYS[0], execution_date=DAYS[1],
        symbol=A, side='sell', requested_shares=200, cash=0, metadata=None)
    assert result is None and not trades
    assert rejected[0]['reason'] == 'cash_constraint'
    assert book.total_shares(A) == 200


def test_batch_incremental_capacity_parity():
    scenario = ResearchExecutionScenario(participation_limit=.1, liquidity=(observation(),))
    engine = AShareExecutionBacktester(ExecutionBacktestConfig(initial_cash=10000), research_scenario=scenario)
    bars, benchmark = _bars(DAYS, [A]), _benchmark(DAYS)
    batch = run(scenario)
    session = engine.start_session(bars, benchmark, signal_date=DAYS[0])
    for i in range(len(DAYS) - 1):
        if i in (0, 2):
            session.submit_targets({A: .8 if i == 0 else 0})
        session.advance()
    for name in ('trades', 'positions', 'rejections', 'target_comparison'):
        pd.testing.assert_frame_equal(getattr(batch, name), getattr(session.result, name))
