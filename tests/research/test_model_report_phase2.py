from pathlib import Path

import matplotlib
import pandas as pd
import pytest

from qbot.research.backtest.fast import FastBacktestResult
from qbot.research.metrics.prediction import PredictionEvaluation
from qbot.research.reports.model import (
    ModelReportError,
    write_model_comparison_report,
    write_model_report,
)
from qbot.research.validation.walk_forward import WalkForwardResult


def _objects(with_coefficients=True):
    dates = pd.to_datetime(["2025-01-02", "2025-01-03"])
    predictions = pd.DataFrame(
        {
            "date": dates,
            "symbol": ["600000.SH", "000001.SZ"],
            "EXCESS_RET5": [0.01, -0.01],
            "score": [0.5, -0.3],
            "fold": [0, 0],
            "rank": [1, 1],
        }
    )
    coefficients = (
        pd.DataFrame(
            {
                "fold": [0, 0, 1, 1],
                "feature": ["REV1", "MOM20", "REV1", "MOM20"],
                "coefficient": [0.2, -0.1, 0.25, -0.08],
                "alpha": [1.0, 1.0, 10.0, 10.0],
            }
        )
        if with_coefficients
        else pd.DataFrame(columns=["fold", "feature", "coefficient", "alpha"])
    )
    walk_forward = WalkForwardResult(
        predictions=predictions,
        fold_summary=pd.DataFrame(
            {"fold": [0], "model_name": ["ridge"], "test_rows": [2]}
        ),
        validation_metrics=pd.DataFrame(
            {"fold": [0], "candidate": ["alpha=1"], "prediction_rank_ic_mean": [0.1]}
        ),
        coefficients=coefficients,
        model_paths=(Path("fold_000_ridge.pkl"),),
    )
    daily = pd.DataFrame(
        {
            "date": dates,
            "prediction_ic": [0.2, -0.1],
            "prediction_rank_ic": [0.3, 0.1],
        }
    )
    summary = pd.DataFrame(
        {
            "scope": ["OVERALL"],
            "period": ["ALL"],
            "prediction_ic_mean": [0.05],
            "prediction_icir": [0.2],
            "prediction_rank_ic_mean": [0.2],
            "prediction_rank_icir": [1.0],
            "top_k_future_return": [0.01],
            "bottom_k_future_return": [-0.01],
            "top_bottom_spread": [0.02],
            "score_coverage": [1.0],
            "coverage": [1.0],
            "top_label_coverage": [0.5],
            "bottom_label_coverage": [1.0],
            "valid_dates": [2],
            "rank_ic_valid_dates": [2],
        }
    )
    prediction = PredictionEvaluation(daily=daily, summary=summary)
    nav = pd.DataFrame(
        {
            "date": dates,
            "strategy_nav": [1.0, 1.02],
            "benchmark_nav": [1.0, 1.01],
            "excess_nav": [1.0, 1.00990099],
            "turnover": [1.0, 0.0],
            "transaction_cost": [5.0, 0.0],
        }
    )
    backtest = FastBacktestResult(
        nav=nav,
        trades=pd.DataFrame(
            {"execution_date": [dates[0]], "symbol": ["600000.SH"], "trade_notional": [1000.0]}
        ),
        positions=pd.DataFrame(
            {"date": dates, "symbol": ["600000.SH", "600000.SH"], "weight": [1.0, 1.0]}
        ),
        signals=pd.DataFrame(
            {"date": [dates[0]], "symbol": ["600000.SH"], "score": [0.5], "rank": [1]}
        ),
    )
    return walk_forward, prediction, backtest


def test_write_model_report_emits_complete_ridge_artifacts(tmp_path):
    walk_forward, prediction, backtest = _objects(with_coefficients=True)
    summary = write_model_report(
        model_name="ridge",
        walk_forward=walk_forward,
        prediction_evaluation=prediction,
        backtest=backtest,
        portfolio_metrics={"cumulative_return": 0.02, "number_of_trades": 1},
        output_dir=tmp_path,
    )

    table_names = {
        "predictions",
        "walk_forward_results",
        "validation_results",
        "ridge_coefficients",
        "prediction_daily",
        "prediction_summary",
        "nav",
        "trades",
        "positions",
        "signals",
        "portfolio_metrics",
    }
    for name in table_names:
        assert (tmp_path / (name + ".csv")).is_file()
        assert (tmp_path / (name + ".parquet")).is_file()
    for name in (
        "nav_curve.png",
        "drawdown.png",
        "prediction_ic.png",
        "turnover.png",
        "transaction_cost.png",
        "ridge_coefficients.png",
    ):
        assert (tmp_path / name).read_bytes().startswith(b"\x89PNG")

    assert summary == tmp_path / "summary.md"
    text = summary.read_text(encoding="utf-8")
    assert "Model: `ridge`" in text
    assert "prediction_rank_ic_mean" in text
    assert "top_label_coverage" in text
    assert "cumulative_return" in text
    assert "Stage diagnostics unavailable" in text
    assert not list(tmp_path.glob("stage_*.csv"))
    assert not (tmp_path / "stage_ic_comparison.png").exists()
    assert pd.read_parquet(tmp_path / "predictions.parquet").equals(
        walk_forward.predictions
    )
    assert matplotlib.get_backend().lower() == "agg"
    assert not list(tmp_path.glob(".*.tmp*"))


def test_empty_factor_score_report_is_explicit_and_omits_ridge_artifacts(tmp_path):
    empty = pd.DataFrame()
    walk_forward = WalkForwardResult(
        predictions=empty,
        fold_summary=empty,
        validation_metrics=empty,
        coefficients=empty,
        model_paths=(),
    )
    prediction = PredictionEvaluation(daily=empty, summary=empty)
    backtest = FastBacktestResult(nav=empty, trades=empty, positions=empty, signals=empty)

    summary = write_model_report(
        model_name="factor_score",
        walk_forward=walk_forward,
        prediction_evaluation=prediction,
        backtest=backtest,
        portfolio_metrics={},
        output_dir=tmp_path,
    )

    assert "No realised out-of-sample" in summary.read_text(encoding="utf-8")
    assert "Stage diagnostics unavailable" in summary.read_text(encoding="utf-8")
    assert not (tmp_path / "ridge_coefficients.csv").exists()
    assert not (tmp_path / "ridge_coefficients.png").exists()
    for name in (
        "nav_curve.png",
        "drawdown.png",
        "prediction_ic.png",
        "turnover.png",
        "transaction_cost.png",
    ):
        assert (tmp_path / name).is_file()
    assert pd.read_csv(tmp_path / "portfolio_metrics.csv").empty


def test_report_rejects_invalid_public_inputs(tmp_path):
    walk_forward, prediction, backtest = _objects()
    with pytest.raises(ModelReportError, match="model_name"):
        write_model_report(
            model_name="",
            walk_forward=walk_forward,
            prediction_evaluation=prediction,
            backtest=backtest,
            portfolio_metrics={},
            output_dir=tmp_path,
        )
    with pytest.raises(ModelReportError, match="portfolio_metrics"):
        write_model_report(
            model_name="ridge",
            walk_forward=walk_forward,
            prediction_evaluation=prediction,
            backtest=backtest,
            portfolio_metrics=[],
            output_dir=tmp_path,
        )


def test_comparison_report_merges_prediction_and_portfolio_metrics(tmp_path):
    _, ridge_prediction, _ = _objects()
    factor_summary = ridge_prediction.summary.copy()
    factor_summary["prediction_rank_ic_mean"] = 0.1

    summary = write_model_comparison_report(
        {
            "ridge": {
                "prediction": ridge_prediction,
                "portfolio_metrics": {"cumulative_return": 0.02, "sharpe_ratio": 0.4},
            },
            "factor_score": {
                "prediction_summary": factor_summary,
                "portfolio_metrics": pd.DataFrame(
                    {
                        "metric": ["cumulative_return", "sharpe_ratio"],
                        "value": [0.01, 0.2],
                    }
                ),
            },
        },
        tmp_path,
    )

    comparison = pd.read_csv(tmp_path / "model_comparison.csv")
    assert comparison["model_name"].tolist() == ["factor_score", "ridge"]
    assert comparison["prediction_rank_ic_mean"].tolist() == [0.1, 0.2]
    assert comparison["cumulative_return"].tolist() == [0.01, 0.02]
    assert (tmp_path / "model_ic_comparison.png").read_bytes().startswith(b"\x89PNG")
    text = summary.read_text(encoding="utf-8")
    assert "不自动评选最佳模型" in text
    assert "| factor_score |" in text


def test_comparison_report_accepts_empty_frame(tmp_path):
    summary = write_model_comparison_report(pd.DataFrame(), tmp_path)
    comparison = pd.read_csv(tmp_path / "model_comparison.csv")
    assert comparison.empty
    assert comparison.columns.tolist() == ["model_name"]
    assert "No completed model records" in summary.read_text(encoding="utf-8")
    assert (tmp_path / "model_ic_comparison.png").is_file()
