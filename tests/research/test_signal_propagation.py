"""Deterministic causal interventions, not claims about market alpha."""
import numpy as np
import pandas as pd
import pytest

from factorlab.application.signal_propagation import prediction_targets, pairwise_transmission
from factorlab.backtest.execution import AShareExecutionBacktester, ExecutionBacktestConfig
from factorlab.backtest.fees import FeeSchedule
from tests.research.test_execution_backtest_phase3 import _bars, _benchmark
from tests.research.test_portfolio_execution import reconcile

DAYS = pd.bdate_range('2024-01-02', periods=8)
SYMBOLS = ['000001.SZ', '000002.SZ']


def allocate(scores, objective='score'):
    predictions = pd.DataFrame(dict(date=DAYS[3], symbol=SYMBOLS, fold=1, score=scores))
    risk = pd.DataFrame({SYMBOLS[0]: [.01, -.02, .03], SYMBOLS[1]: [-.02, .01, .04]}, index=DAYS[:3])
    return prediction_targets(predictions, risk, objective=objective, max_weight=1.)


def execute(target, *, cash=100000, suspended=False, moving=False):
    overrides = {(DAYS[4], SYMBOLS[0]): {'trade_status': 0}} if suspended else {}
    bars = _bars(DAYS, SYMBOLS, overrides=overrides)
    if moving:
        bars.loc[(bars.date >= DAYS[5]) & bars.symbol.eq(SYMBOLS[0]), 'raw_close'] = 12.
    return AShareExecutionBacktester(ExecutionBacktestConfig(initial_cash=cash),
        FeeSchedule.a_share_default(slippage_bps=0)).run_target_weights(target, bars, _benchmark(DAYS))


def test_score_swap_changes_weights_orders_trades_holdings_nav():
    a, receipts, ranks = allocate([1., 2.])
    b, _, _ = allocate([2., 1.])
    assert a.target_weight.tolist() == pytest.approx([.8/3, 1.6/3])
    assert b.target_weight.tolist() == pytest.approx([1.6/3, .8/3])
    assert ranks['rank'].tolist() == [2., 1.]
    assert (receipts.risk_last_date <= receipts.date).all()
    ea, eb = execute(a, moving=True), execute(b, moving=True)
    assert not ea.target_comparison.target_shares.equals(eb.target_comparison.target_shares)
    assert not ea.trades.executed_shares.equals(eb.trades.executed_shares)
    assert not ea.positions.shares.equals(eb.positions.shares)
    assert ea.nav.iloc[-1].portfolio_value != eb.nav.iloc[-1].portfolio_value
    for result in (ea, eb):
        assert set(result.trades.execution_date) == {DAYS[4]}
        assert set(result.trades.signal_date) == {DAYS[3]}
        reconcile(result, cash=100000)
        assert result.trades.transaction_cost.sum() > 0
        final = result.positions.loc[result.positions.date.eq(result.nav.iloc[-1].date)]
        gain = final.loc[final.symbol.eq(SYMBOLS[0]), 'shares'].sum() * 2
        assert result.nav.iloc[-1].portfolio_value == pytest.approx(100000 + gain - result.trades.transaction_cost.sum())


@pytest.mark.parametrize('objective', ['equal', 'minimum_variance'])
def test_score_independent_controls(objective):
    a, _, _ = allocate([-100., 3.], objective)
    b, _, _ = allocate([3., -100.], objective)
    pd.testing.assert_frame_equal(a, b)


def test_constants_and_shuffled_scores():
    target, _, ranks = allocate([0., 0.])
    assert target.target_weight.tolist() == pytest.approx([.4, .4])
    assert ranks['rank'].tolist() == [1.5, 1.5]
    a, _, _ = allocate([-10., 2.])
    b, _, _ = allocate([2., -10.])
    assert not np.allclose(a.target_weight, b.target_weight)


def test_constraints_can_erase_signal_and_zero_denominator_is_null():
    a, _, _ = allocate([1., 2.])
    b, _, _ = allocate([2., 1.])
    ea, eb = execute(a, cash=100), execute(b, cash=100)
    assert ea.trades.empty and eb.trades.empty
    frame, rate = pairwise_transmission({'a':a, 'b':b}, {'a':ea, 'b':eb})
    assert rate['target_to_trades_rate'] == 0
    assert frame.target_changed.all()
    _, rate = pairwise_transmission({'a':a, 'b':a}, {'a':ea, 'b':ea})
    assert rate['target_to_trades_rate'] is None
    rejected = execute(a, suspended=True)
    assert 'suspended' in set(rejected.rejections.reason)
    assert (rejected.target_comparison.weight_deviation.abs() > 0).any()
    reconcile(rejected, cash=100000)


def test_reproducible_allocation_execution_and_future_risk():
    a, _, _ = allocate([1., 2.])
    b, _, _ = allocate([1., 2.])
    pd.testing.assert_frame_equal(a, b)
    ea, eb = execute(a), execute(b)
    for field in ('trades', 'positions', 'nav', 'target_comparison', 'rejections'):
        pd.testing.assert_frame_equal(getattr(ea, field), getattr(eb, field))
    predictions = pd.DataFrame(dict(date=DAYS[3], symbol=SYMBOLS, fold=1, score=[1., 2.]))
    risk = pd.DataFrame({s:np.arange(8)*.01 for s in SYMBOLS}, index=DAYS)
    original = prediction_targets(predictions, risk, objective='score', max_weight=1.)[0]
    risk.loc[risk.index > DAYS[3]] = 1000.
    changed = prediction_targets(predictions, risk, objective='score', max_weight=1.)[0]
    pd.testing.assert_frame_equal(original, changed)


def test_future_market_perturbation_does_not_change_prior_model_decisions():
    from examples.research.signal_propagation_synthetic import fixture, fit
    bars, benchmark, dates, membership, status, listings = fixture()
    _, original, _, _ = fit(bars, benchmark, dates, membership, status, listings)
    cutoff = dates[200]
    changed_bars = bars.copy()
    price_columns = [c for c in bars if c.startswith(('raw_', 'adj_')) and any(x in c for x in ('open', 'high', 'low', 'close'))]
    changed_bars.loc[changed_bars.date > cutoff, price_columns] *= 1.01
    _, changed, _, _ = fit(changed_bars, benchmark, dates, membership, status, listings)
    def targets(walks, prices, model):
        pred = walks[model].predictions
        risk = prices.pivot(index='date', columns='symbol', values='raw_close').pct_change(fill_method=None).iloc[1:]
        return prediction_targets(pred, risk, objective='score')[0]
    for model in original:
        a, b = original[model].predictions, changed[model].predictions
        # Future realized labels may change; prior decision scores must not.
        decision_columns = ['date', 'symbol', 'fold', 'score']
        pd.testing.assert_frame_equal(a.loc[a.date <= cutoff, decision_columns].reset_index(drop=True),
            b.loc[b.date <= cutoff, decision_columns].reset_index(drop=True))
        ta, tb = targets(original, bars, model), targets(changed, changed_bars, model)
        pd.testing.assert_frame_equal(ta.loc[ta.date <= cutoff].reset_index(drop=True),
            tb.loc[tb.date <= cutoff].reset_index(drop=True))
        execution_dates = dates[(dates >= ta.date.min()) & (dates <= cutoff)]
        engine = AShareExecutionBacktester(ExecutionBacktestConfig(initial_cash=100000, minimum_cross_section=5))
        results = []
        for target, prices in ((ta, bars), (tb, changed_bars)):
            results.append(engine.run_target_weights(target.loc[target.date < cutoff],
                prices.loc[prices.date.isin(execution_dates)].merge(status, on=['date', 'symbol']),
                benchmark.loc[benchmark.date.isin(execution_dates)]))
        pd.testing.assert_frame_equal(results[0].trades, results[1].trades)
