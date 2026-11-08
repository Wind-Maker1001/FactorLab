"""Deterministic orchestration contracts over the existing execution ledger."""
from dataclasses import replace
import json

import numpy as np
import pandas as pd
import pytest

from factorlab.application.research_pipeline import feedback_execution, prediction_targets
from factorlab.portfolio import OptimizerConfig, PortfolioOptimizer
from factorlab.portfolio.optimizer import OptimizationError
from tests.research.test_execution_feedback import A, B, DAYS, engine, assert_reconciled
from tests.research.test_execution_backtest_phase3 import _bars, _benchmark


def inputs():
    history = pd.bdate_range(end=DAYS[-1], periods=15)
    returns = pd.DataFrame({A: np.sin(np.arange(15))*.01,
                            B: np.cos(np.arange(15))*.01}, index=history)
    predictions = pd.DataFrame([dict(date=day, symbol=symbol, score=score, fold=fold)
        for day, fold in [(DAYS[0], 1), (DAYS[3], 2)]
        for symbol, score in [(A, 1.), (B, 2.)]])
    return predictions, returns


def execute(predictions=None, returns=None, bars=None, limit=None, investment=.5, fee=5,
            sale_restrictions=()):
    p, r = inputs()
    return feedback_execution(p if predictions is None else predictions,
        r if returns is None else returns, model_name='ridge',
        optimizer=PortfolioOptimizer(OptimizerConfig(objective='score', investment=investment,
            turnover_limit=limit)), lookback=5, every=1, engine=engine(fee=fee),
        bars=_bars(DAYS, [A, B]) if bars is None else bars, benchmark=_benchmark(DAYS),
        sale_restrictions=sale_restrictions)


def test_batch_parity_and_calendar_gap():
    p, r = inputs()
    target, decisions, live = execute()
    batch_target, _ = prediction_targets(p, r, model_name='ridge',
        optimizer=PortfolioOptimizer(OptimizerConfig(objective='score', investment=.5)),
        lookback=5, every=1)
    pd.testing.assert_frame_equal(target, batch_target)
    # Batch has no holdings-aware optimizer receipt; supply identical metadata
    # to compare every execution field without conflating telemetry and fills.
    batch_target['optimizer_turnover'] = batch_target.date.map(
        decisions.set_index('date').estimated_gross_security_turnover)
    batch = engine(fee=5).run_target_weights(batch_target, _bars(DAYS, [A, B]), _benchmark(DAYS))
    pd.testing.assert_frame_equal(live.nav.drop(columns='gross_security_turnover'), batch.nav)
    for name in ['trades', 'positions', 'rejections', 'target_comparison']:
        pd.testing.assert_frame_equal(getattr(live, name), getattr(batch, name))
    pd.testing.assert_frame_equal(live.signals, batch.signals[live.signals.columns])
    assert list(live.nav.date) == list(DAYS[1:])
    assert list(live.trades.execution_date.unique()) == [DAYS[1]]
    assert len(live.positions.query('date == @DAYS[2]')) == 2
    assert (decisions.decision_time.dt.hour == 15).all()
    assert (decisions.execution_time.dt.hour == 9).all()
    assert (decisions.execution_time.dt.minute == 30).all()
    assert_reconciled(live)


def test_turnover_is_actual_not_previous_target():
    _, decisions, result = execute(limit=.5)
    row = decisions.iloc[1]
    weights = json.loads(row.actual_weights)
    targets = json.loads(row.previous_targets)
    assert weights != targets
    prior = result.positions.query('date == @DAYS[3]').set_index('symbol')
    nav = result.nav.query('date == @DAYS[3]').iloc[0]
    for symbol, weight in weights.items():
        assert weight == pytest.approx(prior.loc[symbol].market_value/nav.portfolio_value)
    next_target = json.loads(row.target_weights)
    assert row.estimated_gross_security_turnover == pytest.approx(
        sum(abs(next_target.get(s, 0)-weights.get(s, 0)) for s in set(weights) | set(next_target)))
    assert decisions.estimated_gross_security_turnover.le(.5 + 1e-8).all()
    assert result.target_comparison.weight_deviation.abs().gt(0).any()
    assert_reconciled(result)


def test_cash_partial_fill_and_rejection_feed_next_decision():
    p, r = inputs()
    p = p[p.symbol.eq(A)]
    _, decisions, result = execute(predictions=p, limit=1, investment=1)
    assert result.trades.iloc[0].executed_shares == 900
    assert result.rejections.iloc[0].reason == 'cash_constraint'
    row = decisions.iloc[1]
    assert json.loads(row.previous_targets) == {A: 1.}
    assert json.loads(row.actual_weights)[A] == pytest.approx(9000/9995)
    assert row.estimated_gross_security_turnover == pytest.approx(995/9995)
    assert_reconciled(result)


def test_suspension_and_outside_target_old_position():
    p, r = inputs()
    p = p[((p.date == DAYS[0]) & (p.symbol == A)) |
          ((p.date == DAYS[3]) & (p.symbol == B))]
    bars = _bars(DAYS, [A, B], overrides={(DAYS[4], A): {'trade_status': 0}})
    _, decisions, result = execute(predictions=p, bars=bars, limit=1, investment=.5, fee=0)
    assert json.loads(decisions.iloc[1].actual_weights) == {A: pytest.approx(.5)}
    assert decisions.iloc[1].estimated_gross_security_turnover == pytest.approx(1)
    assert 'suspended' in set(result.rejections.reason)
    assert result.positions.query('date == @DAYS[4] and symbol == @A').iloc[0].shares == 500
    assert_reconciled(result)


def test_future_fill_and_risk_cannot_change_prior_instruction():
    p, returns = inputs()
    normal = execute(limit=.5)
    changed = _bars(DAYS, [A, B])
    changed.loc[changed.date >= DAYS[4], ['raw_open', 'raw_close']] *= 1.05
    returns.loc[returns.index >= DAYS[4]] *= 100
    altered = execute(bars=changed, returns=returns, limit=.5)
    for left, right in zip(normal[:2], altered[:2]):
        pd.testing.assert_frame_equal(left, right)
    pd.testing.assert_frame_equal(normal[2].signals, altered[2].signals)


def test_known_sale_prohibition_rejects_outside_target_exit():
    from factorlab.portfolio.feedback import SaleRestriction
    p, _ = inputs()
    p = p[((p.date == DAYS[0]) & (p.symbol == A)) |
          ((p.date == DAYS[3]) & (p.symbol == B))]
    lock = SaleRestriction(A, 500, DAYS[2] + pd.Timedelta(hours=15), DAYS[4], 'known prohibition')
    with pytest.raises(OptimizationError, match='outside target universe'):
        execute(predictions=p, limit=1, fee=0, sale_restrictions=(lock,))
    future = replace(lock, known_at=DAYS[4] + pd.Timedelta(hours=15))
    # Future evidence must not leak into the previous decision.
    _, decisions, _ = execute(predictions=p, limit=1, fee=0, sale_restrictions=(future,))
    assert decisions.iloc[-1].estimated_gross_security_turnover == pytest.approx(1)


def test_infeasible_initial_turnover_is_not_relaxed():
    with pytest.raises(OptimizationError, match='infeasible'):
        execute(limit=.499)
