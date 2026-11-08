from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

from factorlab.portfolio import (OptimizationError, OptimizationInput, OptimizerConfig,
                                 PortfolioOptimizer, equal_weights)


def inputs(matrix=None, **kwargs):
    return OptimizationInput(symbols=("A", "B"), decision_time="2026-01-05",
        covariance=pd.DataFrame(np.diag([1., 4.]) if matrix is None else matrix,
                                index=["A", "B"], columns=["A", "B"]),
        covariance_as_of="2026-01-04", **kwargs)


def solve(data=None, **kwargs):
    return PortfolioOptimizer(OptimizerConfig(**kwargs)).optimize(data or inputs())


def test_closed_form_variance_and_cash():
    r = solve()
    np.testing.assert_allclose(r.target_weights, [.8, .2], atol=1e-7)
    assert r.estimated_variance == pytest.approx(.8)
    assert r.estimated_volatility == pytest.approx(np.sqrt(.8))
    r = solve(investment=.5)
    np.testing.assert_allclose(r.target_weights, [.4, .1], atol=1e-7)
    assert r.cash_weight == .5
    assert all(r.constraint_checks.values())


@pytest.mark.parametrize("aversion, expected", [(1., .6), (2., .7), (10., .78)])
def test_mean_variance(aversion, expected):
    r = solve(inputs(expected_returns=pd.Series([0., 1.], index=["A", "B"]),
                     expected_return_definition="explicit one-day arithmetic return forecast"),
              objective="mean_variance", risk_aversion=aversion)
    assert r.target_weights["A"] == pytest.approx(expected, abs=1e-7)


def test_cap_baselines_and_infeasibility():
    np.testing.assert_allclose(solve(max_weight=.6).target_weights, [.6, .4], atol=1e-7)
    np.testing.assert_allclose(solve(objective="equal").target_weights, equal_weights(["A", "B"]))
    score = inputs(scores=pd.Series([1., 3.], index=["A", "B"]))
    np.testing.assert_allclose(solve(score, objective="score").target_weights, [.25, .75])
    for data, config in [(inputs(), {"max_weight": .4}), (score, {"objective": "score", "max_weight": .6})]:
        with pytest.raises(OptimizationError) as exc:
            solve(data, **config)
        assert exc.value.status == "infeasible"


def test_industry_and_turnover_boundary():
    data = inputs(industries={"A": "tech", "B": "other"},
        benchmark_industries={"tech": .5}, previous_weights=pd.Series({"A": .5, "B": .5}))
    r = solve(data, industry_bounds={"tech": (.4, .7)}, industry_deviation={"tech": .1}, turnover_limit=.2)
    np.testing.assert_allclose(r.target_weights, [.6, .4], atol=1e-7)
    assert r.turnover == pytest.approx(.2)
    with pytest.raises(OptimizationError, match="infeasible"):
        solve(data, industry_bounds={"tech": (.8, 1.)}, industry_deviation={"tech": .1})
    r = solve(inputs(previous_weights=pd.Series({"X": .2, "A": .4, "B": .4})), turnover_limit=.4)
    assert r.turnover == pytest.approx(.4, abs=1e-7)
    numeric_strings = inputs(previous_weights=pd.Series({"X": "0.1", "Y": "0.1", "A": "0.4", "B": "0.4"}))
    assert solve(numeric_strings, turnover_limit=.4).turnover == pytest.approx(.4, abs=1e-7)
    with pytest.raises(OptimizationError):
        solve(inputs(previous_weights=pd.Series({"X": 1.})), turnover_limit=1.)


@pytest.mark.parametrize("matrix", [[[1., 1.], [1., 1.]], [[0., 0.], [0., 0.]]])
def test_singular_and_order(matrix):
    data = inputs(matrix)
    first = solve(data)
    second = solve(replace(data, symbols=("B", "A"), covariance=data.covariance.loc[["B", "A"], ["B", "A"]]))
    np.testing.assert_array_equal(first.target_weights, second.target_weights)
    assert first.input_identity == second.input_identity
    assert first.estimated_variance >= 0


@pytest.mark.parametrize("matrix", [[[1., 2.], [0., 1.]], [[1., 2.], [2., 1.]], [[np.nan, 0.], [0., 1.]]])
def test_invalid_covariance(matrix):
    with pytest.raises(OptimizationError) as exc:
        solve(inputs(matrix))
    assert exc.value.status == "invalid_input"


def test_sample_shrinkage_and_future_contract():
    frame = pd.DataFrame({"A": [0., .02, .04], "B": [.01, .03, .05]},
                         index=pd.date_range("2026-01-01", periods=3))
    data = replace(inputs(), covariance=None, returns=frame)
    result = solve(data, shrinkage=1., objective="equal")
    assert result.estimated_variance == pytest.approx(.0002)
    for bad in [replace(data, decision_time="2026-01-02"), replace(data, returns=frame.iloc[:1]),
                replace(data, returns=frame.assign(A=np.nan)), replace(data, returns=frame.iloc[::-1]),
                replace(inputs(), covariance_as_of="2026-01-06"),
                replace(inputs(), covariance=inputs().covariance.drop(columns="B"))]:
        with pytest.raises(OptimizationError):
            solve(bad)


@pytest.mark.parametrize("scale", [1e-12, 1., 1e6])
def test_covariance_scale_does_not_change_optimum(scale):
    result = solve(inputs(np.diag([scale, 4*scale])))
    np.testing.assert_allclose(result.target_weights, [.8, .2], atol=1e-7)


def test_zero_budget_single_security_and_locked_holdings():
    assert solve(investment=0).cash_weight == 1
    single = OptimizationInput(symbols=("A",), decision_time="2026-01-05",
        returns=pd.DataFrame({"A": [.01, .03]}, index=pd.date_range("2026-01-01", periods=2)))
    assert solve(single).target_weights["A"] == pytest.approx(1.)
    data = inputs(previous_weights=pd.Series({"A": .3, "B": .7}),
                  industries={"A": "tech", "B": "other"})
    result = solve(data, turnover_limit=0, industry_bounds={"tech": (.3, .3)})
    np.testing.assert_allclose(result.target_weights, [.3, .7], atol=1e-8)


def test_score_is_not_expected_return_and_solver_failure():
    with pytest.raises(OptimizationError, match="explicit expected_returns"):
        solve(inputs(scores=pd.Series({"A": 1., "B": 2.})), objective="mean_variance")
    for scores in [pd.Series({"A": -1., "B": 2.}), pd.Series({"A": 0., "B": 0.}), pd.Series({"A": 1.})]:
        with pytest.raises(OptimizationError):
            solve(inputs(scores=scores), objective="score")
    with pytest.raises(OptimizationError) as exc:
        solve(max_iterations=1)
    assert exc.value.status == "solver_failed"
