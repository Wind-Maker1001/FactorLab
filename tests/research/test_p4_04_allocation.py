"""Attribution adapters preserve ties, union support and actual execution differences."""
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from tools.research.audit_p4_04_allocation import (
    compare_feedback, rank_distance, ranking, weight_distance,
)
from factorlab.application.research_pipeline import prediction_targets
from factorlab.portfolio import OptimizerConfig, PortfolioOptimizer


def test_ranking_ties_are_not_broken_by_symbol():
    frame = pd.DataFrame(dict(date=pd.to_datetime(['2023-07-03'] * 3),
        symbol=['C', 'A', 'B'], fold=[1] * 3, score=[1., 2., 2.]))
    ranks = ranking(frame)
    assert ranks.symbol.tolist() == ['A', 'B', 'C']
    assert ranks['rank'].tolist() == [1.5, 1.5, 3.]
    other = ranking(frame.assign(score=[2., 1., 1.]))
    distance = rank_distance(ranks, other).iloc[0]
    assert distance.changed_ranks == 3
    assert distance.spearman == pytest.approx(-1.)
    assert distance.mean_absolute_rank_distance == pytest.approx(4 / 3)


def test_union_support_distance_includes_exited_symbols():
    left = pd.DataFrame(dict(date=pd.to_datetime(['2023-07-03'] * 2), symbol=['A', 'B'], target_weight=[.4, .4]))
    right = pd.DataFrame(dict(date=pd.to_datetime(['2023-07-03'] * 2), symbol=['B', 'C'], target_weight=[.2, .6]))
    distance = weight_distance(left, right).iloc[0]
    assert distance.l1 == pytest.approx(1.2)
    assert distance.changed_support == 2


def test_real_allocator_contract_across_objectives():
    dates = pd.bdate_range('2023-06-01', periods=12)
    returns = pd.DataFrame({'A': np.linspace(-.01, .01, 12),
                           'B': np.tile([-.02, .02], 6),
                           'C': np.tile([.01, -.02, .01], 4)}, index=dates)
    frame = pd.DataFrame(dict(date=[dates[-1]] * 3, symbol=['A', 'B', 'C'],
                              fold=[1] * 3, score=[-10., 0., 20.]))
    targets = {}
    for objective in ('minimum_variance', 'equal', 'score'):
        optimizer = PortfolioOptimizer(OptimizerConfig(objective=objective, investment=.8, max_weight=.8))
        a, _ = prediction_targets(frame, returns, model_name='first', optimizer=optimizer, lookback=12, every=1)
        b, _ = prediction_targets(frame.assign(score=[20., 0., -10.]), returns,
            model_name='second', optimizer=optimizer, lookback=12, every=1)
        ranks = frame.assign(score=-frame.groupby('date').score.rank(method='average', ascending=False))
        replay, _ = prediction_targets(ranks, returns, model_name='replay', optimizer=optimizer, lookback=12, every=1)
        pd.testing.assert_frame_equal(a, replay)
        if objective == 'score':
            assert not np.allclose(a.target_weight, b.target_weight)
            np.testing.assert_allclose(a.target_weight, [.8/6, .8/3, .8/2])
        else:
            np.testing.assert_allclose(a.target_weight, b.target_weight)
        targets[objective] = a
    assert weight_distance(targets['score'], targets['equal']).l1.iloc[0] > 0


def test_feedback_comparison_ignores_only_extra_telemetry():
    target = pd.DataFrame(dict(target_weight=[.8]))
    fields = {name: pd.DataFrame(dict(value=[1.])) for name in
              ('trades', 'positions', 'nav', 'rejections', 'target_comparison')}
    batch = SimpleNamespace(**fields)
    fields['nav']['optimizer_turnover'] = np.nan
    incremental = SimpleNamespace(**{name: frame.assign(gross_security_turnover=.8) for name, frame in fields.items()})
    incremental.nav['optimizer_turnover'] = .8
    assert compare_feedback(target, batch, target, incremental)['nav']['feedback_only_columns'] == ['gross_security_turnover']
    incremental.positions.loc[0, 'value'] = .5
    with pytest.raises(AssertionError):
        compare_feedback(target, batch, target, incremental)
