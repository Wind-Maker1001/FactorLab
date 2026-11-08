"""Deterministic evaluation-contract and existing-implementation comparisons."""
from dataclasses import replace
from types import SimpleNamespace
import json

import numpy as np
import pandas as pd
import pytest

from factorlab.backtest.fees import FeeSchedule
from factorlab.evaluation import (
    EvaluationContext, ResearchEvaluation, compare_results, paired_inference, reprice_costs,
)
from factorlab.metrics.prediction import evaluate_predictions
from factorlab.metrics.portfolio import calculate_portfolio_metrics
from factorlab.metrics.robustness import RobustnessOptions, mean_uncertainty


@pytest.fixture
def context():
    return EvaluationContext("dataset-v1", "universe-v1", "label", "labels-v1", "benchmark-v1",
        tuple(pd.bdate_range("2024-01-02", periods=12).strftime("%Y-%m-%d")), 1)


def prediction_sample(context):
    rows = []
    for i, day in enumerate(context.calendar):
        for j in range(5):
            rows.append((pd.Timestamp(day), str(j), 0, float(j), float((j + i) % 5)))
    frame = pd.DataFrame(rows, columns=["date", "symbol", "fold", "score", "label"])
    folds = pd.DataFrame([dict(fold=0, train_end="2023-10-01", label_end="2023-10-01",
        validation_start="2023-10-01", validation_end="2024-01-01",
        test_start="2024-01-02", test_end="2024-02-01")])
    return frame, folds


def nav_sample(context):
    returns = np.array([.01, -.02, .03, -.01, .004, .003] * 2)
    benchmark = returns / 2
    return pd.DataFrame(dict(date=pd.to_datetime(context.calendar), strategy_return=returns,
        benchmark_return=benchmark, strategy_nav=np.cumprod(1 + returns),
        benchmark_nav=np.cumprod(1 + benchmark)))


def test_prediction_equivalence_and_json(context):
    frame, folds = prediction_sample(context)
    result = ResearchEvaluation(context).predictions("ridge", frame, folds=folds, top_k=1)
    expected = evaluate_predictions(frame, target_col="label", top_k=1)
    pd.testing.assert_frame_equal(result.tables["daily"], expected.daily)
    assert result.metrics["prediction_rank_ic_mean"].value == expected.overall.iloc[0].prediction_rank_ic_mean
    adapted = ResearchEvaluation(context).walk_forward("ridge", SimpleNamespace(predictions=frame), folds=folds, top_k=1)
    pd.testing.assert_frame_equal(adapted.tables["daily"], result.tables["daily"])
    encoded = json.loads(json.dumps(result.to_dict(), allow_nan=False))
    assert encoded["financial_evidence"] is False
    assert "FORMAL_ADMISSION_BLOCKED" in encoded["evidence"]
    real = ResearchEvaluation(replace(context, source_kind="REAL_CANDIDATE")).predictions("real", frame, folds=folds)
    assert real.to_dict()["financial_evidence"] is False


@pytest.mark.parametrize("change", [dict(label_id="other"), dict(frequency="weekly"),
    dict(holding_days=5), dict(benchmark_id="other"), dict(periods_per_year=52)])
def test_comparison_rejects_context_mismatch(context, change):
    frame, folds = prediction_sample(context)
    a = ResearchEvaluation(context).predictions("a", frame, folds=folds)
    b = ResearchEvaluation(replace(context, **change)).predictions("b", frame, folds=folds)
    with pytest.raises(ValueError, match="incompatible"):
        compare_results([a, b])


def test_identity_and_windows(context):
    frame, folds = prediction_sample(context)
    service = ResearchEvaluation(context)
    a = service.predictions("a", frame, folds=folds)
    changed = frame.copy()
    changed.loc[0, "label"] += .1
    b = service.predictions("b", changed, folds=folds)
    with pytest.raises(ValueError, match="sample"):
        compare_results([a, b])
    with pytest.raises(ValueError, match="calendar"):
        service.predictions("b", frame.iloc[5:], folds=folds)
    unsafe = folds.copy()
    unsafe.loc[0, "train_end"] = "2024-01-03"
    with pytest.raises(ValueError, match="unsafe"):
        service.predictions("b", frame, folds=unsafe)
    with pytest.raises(ValueError, match="HISTORICAL_TEST"):
        replace(context, stage="VALIDATION_SELECTION")


def test_portfolio_equivalence_missing_receipts(context):
    nav = nav_sample(context)
    result = ResearchEvaluation(context).portfolio("strategy", nav)
    expected = calculate_portfolio_metrics(nav)
    for key in ("annualized_return", "sharpe_ratio", "sortino_ratio", "maximum_drawdown",
                "tracking_error", "information_ratio", "excess_return"):
        assert result.metrics[key].value == pytest.approx(expected[key])
    for key in ("total_transaction_cost", "portfolio_turnover", "number_of_trades", "average_holdings"):
        assert result.metrics[key].status == "NOT_COMPUTABLE"
        assert result.metrics[key].value is None
    nav.loc[0, "strategy_nav"] += .01
    with pytest.raises(ValueError, match="reconcile"):
        ResearchEvaluation(context).portfolio("bad", nav)


def test_pairing_and_inference_recompute(context):
    frame, folds = prediction_sample(context)
    changed = frame.copy()
    changed.loc[changed.date.isin(pd.to_datetime(context.calendar)[::3]), "score"] *= -1
    service = ResearchEvaluation(context)
    a = service.predictions("factor_score", frame, folds=folds)
    b = service.predictions("ridge", changed, folds=folds)
    options = RobustnessOptions(holding_days=1, hac_lags=1, bootstrap_block_days=2,
        min_valid_days=4, bootstrap_repetitions=100)
    actual = paired_inference([a, b], baseline="factor_score", metric="prediction_rank_ic", options=options)
    delta = b.tables["daily"].prediction_rank_ic - a.tables["daily"].prediction_rank_ic
    expected = mean_uncertainty(delta, signal_dates=12, options=options,
        identity="factor_score:ridge:prediction_rank_ic")
    assert actual.iloc[0].hac_p_value == pytest.approx(expected["hac_p_value"])
    assert actual.iloc[0].bootstrap_ci_lower == pytest.approx(expected["bootstrap_ci_lower"])
    assert actual.iloc[0].holm_p_value == actual.iloc[0].hac_p_value
    assert actual.iloc[0].unit == "market_trading_day"


def test_missing_pairs_and_insufficient_statistics(context):
    frame, folds = prediction_sample(context)
    service = ResearchEvaluation(context)
    a = service.predictions("a", frame, folds=folds)
    frame.loc[0, "score"] = np.nan
    b = service.predictions("b", frame, folds=folds)
    with pytest.raises(ValueError, match="sample"):
        compare_results([a, b])
    frame.score = 1.
    result = service.predictions("constant", frame, folds=folds)
    assert result.metrics["prediction_rank_ic_mean"].status == "NOT_COMPUTABLE"
    default = RobustnessOptions()
    table = paired_inference([a, service.predictions("copy", prediction_sample(context)[0], folds=folds)],
        baseline="a", metric="prediction_rank_ic", options=replace(default, holding_days=1))
    assert table.iloc[0].status == "INSUFFICIENT_DATES"


def test_actual_fill_cost_scenarios(context):
    schedule = FeeSchedule.a_share_default()
    notional = 10000.
    day = pd.Timestamp(context.calendar[0])
    recorded = schedule.calculate(day, "buy", notional).total
    ledger = pd.DataFrame([dict(execution_date=day, side="BUY", trade_notional=notional,
        transaction_cost=recorded, executed_shares=100., execution_price=100.)])
    high = FeeSchedule.a_share_default(commission_bps=20.)
    slip = FeeSchedule.a_share_default(slippage_bps=25.)
    table = reprice_costs(ledger, {"base": schedule, "high": high, "slippage": slip})
    assert table.iloc[0].cost_delta == 0
    assert table.iloc[1].transaction_cost == high.calculate(day, "buy", notional).total
    assert table.iloc[2].slippage == 25.
    assert table.scope.eq("FIXED_ACTUAL_FILLS_COST_ONLY").all()
    assert "strategy_nav" not in table
    lower = ledger.assign(side="buy")
    pd.testing.assert_frame_equal(reprice_costs(lower, {"base": schedule}), table.iloc[:1].reset_index(drop=True))
    nav = nav_sample(context)
    nav["transaction_cost"] = [recorded] + [0.] * 11
    nav["turnover"] = [.01] + [0.] * 11
    result = ResearchEvaluation(context).portfolio("costed", nav, trades=ledger)
    assert result.metrics["total_transaction_cost"].value == recorded
    assert result.metrics["portfolio_turnover"].value == .01
    nav.loc[0, "transaction_cost"] += 1
    with pytest.raises(ValueError, match="disagree"):
        ResearchEvaluation(context).portfolio("bad", nav, trades=ledger)


def test_factor_settings_and_usable_samples(context):
    from factorlab.metrics.factor import evaluate_factors
    frame, _ = prediction_sample(context)
    frame = frame.rename(columns={"score": "alpha"}).drop(columns="fold")
    service = ResearchEvaluation(context)
    a = service.factors("a", frame, ["alpha"], minimum_cross_section=2, quantiles=2)
    expected = evaluate_factors(frame, ["alpha"], target="label", minimum_cross_section=2,
        quantiles=2, calendar=pd.to_datetime(context.calendar))
    pd.testing.assert_frame_equal(a.tables["summary"], expected.summary)
    b = service.factors("b", frame, ["alpha"], minimum_cross_section=3, quantiles=2)
    with pytest.raises(ValueError, match="settings"):
        compare_results([a, b])
    frame.loc[0, "alpha"] = np.nan
    c = service.factors("c", frame, ["alpha"], minimum_cross_section=2, quantiles=2)
    with pytest.raises(ValueError, match="sample"):
        compare_results([a, c])


def test_ablation_receipts(context):
    from factorlab.validation.ablation_statistics import daily_receipts, summarize_ablation
    frame, _ = prediction_sample(context)
    frame = frame.rename(columns={"label": "EXCESS_RET5"})
    receipts = pd.concat([daily_receipts(frame, group=group, model="ridge")
                          for group in "ABCD"], ignore_index=True)
    result = ResearchEvaluation(replace(context, label="EXCESS_RET5")).ablation("factorial", receipts)
    expected = summarize_ablation(receipts)
    for table, reference in zip(("summary", "contrasts", "volatility"), expected):
        pd.testing.assert_frame_equal(result.tables[table], reference)
    assert result.metadata["inference"] == "descriptive_only"


def test_benchmark_and_portfolio_pairing(context):
    service = ResearchEvaluation(context)
    nav = nav_sample(context)
    a = service.portfolio("strategy", nav)
    benchmark = nav.assign(strategy_return=nav.benchmark_return, strategy_nav=nav.benchmark_nav)
    b = service.benchmark("benchmark", benchmark)
    assert len(compare_results([a, b])) > 0
    changed = benchmark.copy()
    changed["benchmark_return"] *= .5
    changed["benchmark_nav"] = (1 + changed.benchmark_return).cumprod()
    c = service.portfolio("different_prices", changed)
    with pytest.raises(ValueError, match="benchmark"):
        compare_results([a, c])


def test_holm_family_recomputed(context):
    from factorlab.metrics.robustness import holm_adjust
    frame, folds = prediction_sample(context)
    service = ResearchEvaluation(context)
    arms = [service.predictions("base", frame, folds=folds)]
    for name, every in [("ridge", 2), ("xgboost", 3)]:
        altered = frame.copy()
        altered.loc[altered.date.isin(pd.to_datetime(context.calendar)[::every]), "score"] *= -1
        arms.append(service.predictions(name, altered, folds=folds))
    options = RobustnessOptions(holding_days=1, hac_lags=1, bootstrap_block_days=2,
        min_valid_days=4, bootstrap_repetitions=100)
    stats = paired_inference(arms, baseline="base", metric="prediction_rank_ic", options=options)
    np.testing.assert_allclose(stats.holm_p_value, holm_adjust(stats.hac_p_value, family_size=2))
    assert stats.family_size.eq(2).all()


def test_existing_execution_result_end_to_end(context):
    from factorlab.backtest import ExecutionBacktestConfig, run_execution_backtest
    dates = pd.to_datetime(context.calendar)
    symbols = ["000001.SZ", "000002.SZ"]
    bars = pd.DataFrame([dict(date=day, symbol=symbol, raw_open=10., raw_close=10.,
        volume=100000., trade_status=1, is_st=False) for day in dates for symbol in symbols])
    scores = pd.DataFrame([dict(date=dates[0], symbol=symbol, score=float(i))
                           for i, symbol in enumerate(symbols)])
    benchmark = pd.DataFrame(dict(date=dates, adj_open=100., adj_close=100.))
    schedule = FeeSchedule.a_share_default()
    executed = run_execution_backtest(scores, bars, benchmark,
        config=ExecutionBacktestConfig(top_k=1, minimum_cross_section=2, initial_cash=10000.),
        fee_schedule=schedule)
    actual_context = replace(context, calendar=tuple(executed.nav.date.dt.strftime("%Y-%m-%d")))
    result = ResearchEvaluation(actual_context).portfolio("executed", executed.nav,
        trades=executed.trades, positions=executed.positions)
    repriced = reprice_costs(executed.trades, {"baseline": schedule})
    np.testing.assert_allclose(repriced.cost_delta, 0., atol=1e-10)
    assert result.metrics["total_transaction_cost"].value == pytest.approx(repriced.transaction_cost.sum())
