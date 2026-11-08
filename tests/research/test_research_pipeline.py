"""Integration/time-boundary contracts for the thin research pipeline."""
from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

from examples.research.research_pipeline_synthetic import configured
from factorlab.application.research_pipeline import ResearchPipeline, prediction_targets
from factorlab.experiments import ExperimentTracker
from factorlab.portfolio import OptimizerConfig, PortfolioOptimizer


@pytest.fixture(scope='module')
def completed(tmp_path_factory):
    root = tmp_path_factory.mktemp('p307b')
    config, hub, provider = configured(root / 'output')
    result = ResearchPipeline(config).run(hub, tracker=ExperimentTracker(root / 'tracker'),
        repo_root=__import__('pathlib').Path(__file__).resolve().parents[2], command=['pytest', 'tests/research/test_research_pipeline.py'])
    return config, result, provider


def test_four_model_chain_and_actual_evaluation(completed):
    config, result, _ = completed
    assert set(result.walk_forward) == {'factor_score', 'ridge', 'lightgbm', 'xgboost'}
    for model, walk in result.walk_forward.items():
        assert 2 <= len(walk.fold_summary) <= 3
        assert len(walk.predictions) > 0
        assert set(walk.diagnostic_audit.fit_scope) == {'train_only'}
        assert not walk.factor_selection.empty
        assert (walk.fold_summary.train_max_exit_date < walk.fold_summary.audit_validation_first_date).all()
        assert (walk.fold_summary.validation_max_exit_date < walk.fold_summary.audit_test_first_date).all()
        decision = result.decisions[model]
        assert (decision.risk_last_date <= decision.prediction_time).all()
        assert decision.risk_rows.le(config.risk_lookback).all()
        execution = result.executions[model]
        assert (execution.signals.execution_date > execution.signals.date).all()
        pd.testing.assert_frame_equal(result.evaluations[model][1].tables['daily'], execution.nav)
        assert result.evaluations[model][0].metrics['prediction_rank_ic_mean'].status == 'OK'
    assert not result.comparison.empty


def test_cash_positions_nav_and_unfilled_targets(completed):
    config, result, _ = completed
    for execution in result.executions.values():
        nav = execution.nav.set_index('date')
        market = execution.positions.groupby('date').market_value.sum().reindex(nav.index, fill_value=0)
        np.testing.assert_allclose(nav.cash + market, nav.portfolio_value)
        np.testing.assert_allclose(nav.portfolio_value / config.execution.initial_cash, nav.strategy_nav)
        np.testing.assert_allclose(config.execution.initial_cash +
            (nav.sell_notional - nav.buy_notional - nav.transaction_cost).cumsum(), nav.cash)
        assert (nav.cash >= 0).all()
        assert (execution.positions.shares % 100 == 0).all()
        assert execution.target_comparison.weight_deviation.abs().gt(1e-8).any()
    assert any(not execution.rejections.empty for execution in result.executions.values())


def test_tracker_query_compare_reproduce(completed):
    _, result, _ = completed
    assert len(result.tracker.query(status='SUCCEEDED')) == 4
    assert len(result.tracker.compare(result.run_ids)) == 4
    receipt = result.tracker.reproduction(result.run_ids[0], verify_artifacts=True)
    assert receipt
    result.tracker.rebuild_index()
    assert len(result.tracker.query(status='SUCCEEDED')) == 4


@pytest.mark.parametrize('objective', ['equal', 'score', 'minimum_variance'])
def test_prediction_adapter_future_risk_isolation(objective):
    dates = pd.bdate_range('2024-01-01', periods=12)
    returns = pd.DataFrame({'000001.SZ': np.linspace(-.01, .02, 12),
                            '000005.SZ': np.sin(np.arange(12)) * .01}, index=dates)
    predictions = pd.DataFrame(dict(date=[dates[6]] * 2, symbol=list(returns), score=[-2., 3.], fold=1))
    optimizer = PortfolioOptimizer(OptimizerConfig(objective=objective, investment=.8, max_weight=.8))
    args = dict(model_name='ridge', optimizer=optimizer, lookback=5, every=1)
    before = prediction_targets(predictions, returns, **args)
    changed = returns.copy()
    changed.loc[changed.index > dates[6]] *= 1000
    after = prediction_targets(predictions, changed, **args)
    for left, right in zip(before, after):
        pd.testing.assert_frame_equal(left, right)


def test_real_data_cannot_use_synthetic_escape(tmp_path):
    config, hub, provider = configured(tmp_path / 'output', models=['ridge'])
    config = replace(config, source_kind='REAL_CANDIDATE')
    with pytest.raises(Exception, match='QUALIFICATION REQUIRED'):
        ResearchPipeline(config).run(hub, tracker=ExperimentTracker(tmp_path / 'tracker'),
            repo_root=tmp_path, command=['test'])


def test_future_information_does_not_change_earlier_decisions(completed, tmp_path):
    config, original, _ = completed
    changed_config, hub, provider = configured(tmp_path / 'changed', models=['ridge'])
    cutoff = original.walk_forward['ridge'].predictions.date.sort_values().unique()[15]
    bars = provider.tables['daily_bars']
    future = bars.date > cutoff
    for column in ['raw_open', 'raw_high', 'raw_low', 'raw_close',
                   'open', 'high', 'low', 'close', 'adj_open', 'adj_high', 'adj_low', 'adj_close']:
        bars.loc[future, column] *= 1.7
    changed = ResearchPipeline(changed_config).run(hub,
        tracker=ExperimentTracker(tmp_path / 'tracker'),
        repo_root=__import__('pathlib').Path(__file__).resolve().parents[2], command=['test-future'])
    for source in ('targets', 'decisions'):
        left, right = getattr(original, source)['ridge'], getattr(changed, source)['ridge']
        columns = list(left.columns)
        pd.testing.assert_frame_equal(left.loc[left.date <= cutoff, columns].reset_index(drop=True),
            right.loc[right.date <= cutoff, columns].reset_index(drop=True))
    left = original.walk_forward['ridge'].predictions
    right = changed.walk_forward['ridge'].predictions
    columns = ['date', 'symbol', 'score', 'fold']
    pd.testing.assert_frame_equal(left.loc[left.date <= cutoff, columns].reset_index(drop=True),
        right.loc[right.date <= cutoff, columns].reset_index(drop=True))
    left = original.executions['ridge'].signals
    right = changed.executions['ridge'].signals
    pd.testing.assert_frame_equal(left.loc[left.date <= cutoff].reset_index(drop=True),
        right.loc[right.date <= cutoff].reset_index(drop=True))
