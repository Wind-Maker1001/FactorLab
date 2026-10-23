"""Deterministic, offline Phase-3 end-to-end demonstration.

This program validates software integration only.  Every observation is
synthetic; none of its metrics may be used as an A-share research conclusion.

Run from the repository root::

    python -m examples.research.phase3_synthetic_demo \
        --output results/examples/phase3_synthetic
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
from typing import Dict, List, Tuple
import uuid

import numpy as np
import pandas as pd

from qbot.research.backtest import (
    ExecutionBacktestConfig,
    FastBacktestConfig,
    FeeSchedule,
    PriceLimitSchedule,
    run_execution_backtest,
    run_fast_backtest,
)
from qbot.research.market import build_market_regime_features
from qbot.research.metrics import (
    RobustnessOptions, calculate_portfolio_metrics, evaluate_model_robustness,
    evaluate_predictions,
)
from qbot.research.reports import (
    write_model_comparison_report,
    write_model_report,
)
from qbot.research.reproducibility import (
    git_commit, git_worktree_state, package_versions, set_random_seed,
)
from qbot.research.validation import (
    ExpandingWalkForwardSplitter, WalkForwardRunner, diagnostic_frame_sha256,
)


WARNING = "SYNTHETIC SOFTWARE DEMO ONLY - NOT A RESEARCH RESULT"
MODEL_NAMES = ("factor_score", "ridge", "lightgbm", "xgboost")


def build_synthetic_inputs(
    start: str = "2020-01-02",
    end: str = "2020-12-31",
    symbols: int = 10,
    seed: int = 42,
) -> Tuple[
    pd.DatetimeIndex,
    pd.DataFrame,
    pd.DataFrame,
    pd.DataFrame,
    pd.DataFrame,
]:
    """Create small deterministic inputs for every Phase-3 subsystem."""

    if symbols < 8:
        raise ValueError("synthetic demo requires at least 8 symbols")
    dates = pd.bdate_range(start, end)
    if len(dates) < 180:
        raise ValueError("synthetic demo requires at least 180 business dates")

    generator = np.random.default_rng(seed)
    date_numbers = np.arange(len(dates))
    # Three deliberately smooth phases make all frozen market regimes visible.
    benchmark_returns = np.where(
        date_numbers < 65,
        0.0025,
        np.where(date_numbers < 120, -0.0025, 0.0022),
    )
    benchmark_returns += 0.0004 * np.sin(date_numbers / 9.0)
    benchmark_close = 5000.0 * np.cumprod(1.0 + benchmark_returns)
    benchmark = pd.DataFrame(
        {
            "date": dates,
            "symbol": "000905.SH",
            "adj_open": benchmark_close
            * (1.0 - 0.0002 * np.cos(date_numbers / 7.0)),
            "adj_close": benchmark_close,
        }
    )
    market = build_market_regime_features(benchmark)

    codes = ["%06d.SZ" % number for number in range(1, symbols + 1)]
    innovations = generator.normal(0.0001, 0.002, size=(len(dates), symbols))
    paths = np.empty_like(innovations)
    paths[0] = 10.0 + np.arange(symbols) * 0.35
    for offset in range(1, len(dates)):
        paths[offset] = paths[offset - 1] * (1.0 + innovations[offset])

    rows = []
    bar_rows = []
    for date_number, trading_date in enumerate(dates):
        market_wave = np.sin(date_number / 13.0)
        for symbol_number, symbol in enumerate(codes):
            cross_section = (symbol_number - (symbols - 1) / 2.0) / symbols
            reversal = cross_section + 0.08 * market_wave
            momentum = np.cos(date_number / 11.0 + symbol_number / 2.5)
            volatility = abs(np.sin(date_number / 17.0 + symbol_number / 4.0))
            noise = generator.normal(0.0, 0.003)
            target = (
                0.025 * reversal
                + 0.012 * momentum
                - 0.010 * volatility
                + noise
            )
            entry = dates[date_number + 1] if date_number + 1 < len(dates) else pd.NaT
            exit_ = dates[date_number + 6] if date_number + 6 < len(dates) else pd.NaT
            rows.append(
                {
                    "date": trading_date,
                    "symbol": symbol,
                    "REV1": reversal,
                    "MOM20": momentum,
                    "VOL20": volatility,
                    "entry_date": entry,
                    "exit_date": exit_,
                    "EXCESS_RET5": target if pd.notna(exit_) else np.nan,
                }
            )
            raw_open = float(paths[date_number, symbol_number])
            raw_close = raw_open * (
                1.0 + 0.0008 * np.sin(date_number / 5.0 + symbol_number)
            )
            bar_rows.append(
                {
                    "date": trading_date,
                    "symbol": symbol,
                    "adj_open": raw_open,
                    "raw_open": raw_open,
                    "raw_close": raw_close,
                    "volume": 100_000.0 + 1000.0 * symbol_number,
                    "trade_status": 1,
                    "is_st": False,
                }
            )

    dataset = pd.DataFrame(rows).merge(
        market,
        on="date",
        how="left",
        validate="many_to_one",
    )
    return dates, dataset, pd.DataFrame(bar_rows), benchmark, market


def _atomic_text(text: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(".%s.%s.tmp" % (destination.name, uuid.uuid4().hex))
    temporary.write_text(text, encoding="utf-8")
    os.replace(str(temporary), str(destination))


def _atomic_json(payload: dict, destination: Path) -> None:
    _atomic_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        destination,
    )


def _tag(model_name: str, frame: pd.DataFrame) -> pd.DataFrame:
    result = frame.copy()
    result["model_name"] = model_name
    return result.loc[
        :, ["model_name"] + [name for name in result.columns if name != "model_name"]
    ]


def _artifact_inventory(directory: Path) -> Dict[str, str]:
    inventory = {}
    for path in sorted(directory.rglob("*")):
        if not path.is_file():
            continue
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        inventory[path.relative_to(directory).as_posix()] = digest.hexdigest()
    return inventory


def run_demo(output_dir: Path, seed: int = 42) -> Dict[str, object]:
    """Run all four models, both backtests, and the Phase-3 reports."""

    set_random_seed(seed)
    dates, dataset, bars, benchmark, market = build_synthetic_inputs(seed=seed)
    destination = Path(output_dir).resolve()
    repo_root = Path(__file__).resolve().parents[2]
    provenance = {"git_commit": git_commit(repo_root), **git_worktree_state(repo_root)}
    destination.mkdir(parents=True, exist_ok=True)
    _atomic_text(
        "# %s\n\n"
        "These artifacts exercise the offline Phase-3 software path with generated "
        "data. They include market-regime features, four small models, the fast "
        "research backtest, and rule-aware daily execution. They cannot support "
        "claims about CSI500, A-share factors, model superiority, or expected returns.\n"
        % WARNING,
        destination / "README.md",
    )

    splitter_options = dict(
        initial_train_months=4,
        validation_months=2,
        test_months=2,
        step_months=8,
        purge_days=5,
        embargo_days=5,
    )
    common = dict(
        feature_names=("REV1", "MOM20", "VOL20"),
        target_col="EXCESS_RET5",
        regime_col="MARKET_REGIME",
        top_k=3,
        minimum_cross_section=8,
        random_seed=seed,
    )
    model_options = {
        "factor_score": dict(
            factor_weights={"REV1": 1.0, "MOM20": 1.0, "VOL20": -1.0},
            normalize_by_absolute_weight=True,
        ),
        "ridge": dict(
            ridge_alphas=(0.1, 1.0),
            ridge_fit_intercept=True,
            ridge_max_iter=2000,
            ridge_tolerance=0.0001,
        ),
        "lightgbm": dict(
            lightgbm_candidates=(
                {
                    "n_estimators": 8,
                    "learning_rate": 0.1,
                    "num_leaves": 7,
                    "max_depth": 3,
                    "min_child_samples": 2,
                },
            ),
            lightgbm_n_jobs=1,
        ),
        "xgboost": dict(
            xgboost_candidates=(
                {"n_estimators": 8, "learning_rate": 0.1, "max_depth": 2},
            ),
            xgboost_n_jobs=1,
        ),
    }

    comparison_records = []
    detail_frames: Dict[str, List[pd.DataFrame]] = {
        "walk_forward_results": [],
        "prediction_summary": [],
        "nav": [],
        "execution_nav": [],
        "feature_importance": [],
        "ridge_coefficients": [],
        "stage_prediction_daily": [],
        "stage_prediction_summary": [],
        "stage_prediction_audit": [],
    }
    outputs: Dict[str, object] = {}
    fees = FeeSchedule.a_share_default(slippage_bps=0.0)
    for model_name in MODEL_NAMES:
        model_output = destination / model_name
        runner = WalkForwardRunner(
            ExpandingWalkForwardSplitter(**splitter_options),
            model_name=model_name,
            model_directory=model_output / "fold_models",
            **common,
            **model_options[model_name],
        )
        walk_forward = runner.run(dataset, calendar=dates)
        prediction = evaluate_predictions(
            walk_forward.predictions,
            target_col="EXCESS_RET5",
            top_k=3,
            minimum_cross_section=8,
            regime_col="MARKET_REGIME",
        )

        first = walk_forward.predictions["date"].min()
        last = pd.to_datetime(walk_forward.predictions["exit_date"]).max()
        window_bars = bars.loc[bars["date"].between(first, last)].copy()
        window_benchmark = benchmark.loc[
            benchmark["date"].between(first, last)
        ].copy()
        scores = walk_forward.predictions[["date", "symbol", "score"]]
        fast = run_fast_backtest(
            scores,
            window_bars,
            window_benchmark,
            config=FastBacktestConfig(
                top_k=3,
                rebalance_days=5,
                minimum_cross_section=8,
                initial_cash=1_000_000.0,
                price_column="adj_open",
                benchmark_price_column="adj_open",
            ),
            fee_schedule=fees,
        )
        fast_metrics = calculate_portfolio_metrics(
            fast.nav, fast.trades, fast.positions
        )
        execution = run_execution_backtest(
            scores,
            window_bars,
            window_benchmark,
            config=ExecutionBacktestConfig(
                top_k=3,
                rebalance_days=5,
                minimum_cross_section=8,
                initial_cash=1_000_000.0,
                board_lot=100,
                price_limit_tolerance_bps=5.0,
            ),
            fee_schedule=fees,
            price_limit_schedule=PriceLimitSchedule.a_share_default(),
        )
        execution_metrics = calculate_portfolio_metrics(
            execution.nav, execution.trades, execution.positions
        )
        summary = write_model_report(
            model_name,
            walk_forward,
            prediction,
            fast,
            fast_metrics,
            model_output,
            disclaimer=WARNING,
            execution_backtest=execution,
            execution_metrics=execution_metrics,
        )

        comparison_records.append(
            {
                "model_name": model_name,
                "prediction": prediction,
                "portfolio_metrics": fast_metrics,
                "stage_diagnostics_available": True,
            }
        )
        detail_frames["walk_forward_results"].append(
            _tag(model_name, walk_forward.fold_summary)
        )
        detail_frames["prediction_summary"].append(
            _tag(model_name, prediction.summary)
        )
        detail_frames["nav"].append(_tag(model_name, fast.nav))
        detail_frames["execution_nav"].append(_tag(model_name, execution.nav))
        for name, table in (
            ("stage_prediction_daily", walk_forward.diagnostic_daily),
            ("stage_prediction_summary", walk_forward.diagnostic_summary),
            ("stage_prediction_audit", walk_forward.diagnostic_audit),
        ):
            detail_frames[name].append(_tag(model_name, table))
        if not walk_forward.feature_importance.empty:
            detail_frames["feature_importance"].append(
                _tag(model_name, walk_forward.feature_importance)
            )
        if not walk_forward.coefficients.empty:
            detail_frames["ridge_coefficients"].append(
                _tag(model_name, walk_forward.coefficients)
            )
        outputs[model_name] = {
            "summary": str(summary),
            "prediction_rows": int(len(walk_forward.predictions)),
            "folds": int(len(walk_forward.fold_summary)),
            "fast_trades": int(len(fast.trades)),
            "execution_trades": int(len(execution.trades)),
            "execution_rejections": int(len(execution.rejections)),
            "stage_prediction_rows": int(len(walk_forward.diagnostic_predictions)),
            "stage_audit_rows": int(len(walk_forward.diagnostic_audit)),
            "artifact_sha256": _artifact_inventory(model_output),
        }

    details = {
        name: pd.concat(
            [frame.dropna(axis=1, how="all") for frame in frames],
            ignore_index=True,
        )
        for name, frames in detail_frames.items()
        if frames
    }
    inference_daily = details["stage_prediction_daily"].loc[
        details["stage_prediction_daily"]["stage"].eq("HISTORICAL_TEST")
    ]
    # The example intentionally has a short Test. Default minimum sample
    # requirements remain unchanged: report unavailable CIs rather than tune
    # the threshold/block size to manufacture synthetic significance.
    robustness = evaluate_model_robustness(
        inference_daily, dates, RobustnessOptions(random_seed=seed),
    )
    comparison_summary = write_model_comparison_report(
        comparison_records,
        destination / "comparison",
        disclaimer=WARNING,
        details=details,
        robustness=robustness,
    )
    regime_counts = market["MARKET_REGIME"].value_counts().sort_index()
    manifest = {
        "warning": WARNING,
        "synthetic": True,
        "network_used": False,
        "seed": seed,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "python_version": platform.python_version(),
        "package_versions": package_versions(),
        **provenance,
        "configuration": {
            "splitter": splitter_options, "runner": common,
            "models": model_options,
            "inference": robustness.protocol,
            "backtests": {
                "initial_cash": 1_000_000.0, "rebalance_days": 5,
                "board_lot": 100, "fee_schedule": "a_share_default",
                "slippage_bps": 0.0, "price_limit_schedule": "a_share_default",
            },
        },
        "synthetic_input_sha256": {
            "dataset": diagnostic_frame_sha256(dataset),
            "stock_bars": diagnostic_frame_sha256(bars),
            "benchmark": diagnostic_frame_sha256(benchmark),
        },
        "stage_diagnostic_scope": {
            "fit_scope": "train_only", "validation": "selection_and_diagnostic",
            "train": "in_sample_diagnostic_only", "test": "evaluation_only",
            "train_folds_pooled": False, "real_forward_available": False,
        },
        "inference_evidence": robustness.evidence,
        "inference_calendar_sha256": diagnostic_frame_sha256(pd.DataFrame({"date": dates})),
        "date_start": dates.min().date().isoformat(),
        "date_end": dates.max().date().isoformat(),
        "symbols": int(dataset["symbol"].nunique()),
        "market_regime_counts": {
            str(name): int(value) for name, value in regime_counts.items()
        },
        "models": outputs,
        "comparison_summary": str(comparison_summary),
        "comparison_artifact_sha256": _artifact_inventory(destination / "comparison"),
    }
    _atomic_json(manifest, destination / "manifest.json")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=WARNING)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("results/examples/phase3_synthetic"),
    )
    parser.add_argument("--seed", type=int, default=42)
    arguments = parser.parse_args()
    manifest = run_demo(arguments.output, seed=arguments.seed)
    print(WARNING)
    print(manifest["comparison_summary"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
