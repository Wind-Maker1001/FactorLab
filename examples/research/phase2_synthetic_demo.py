"""Deterministic, offline Phase-2 demonstration.

This program validates software integration only.  Every observation is
synthetic; none of its metrics may be used as an A-share research conclusion.

Run from the repository root::

    python -m examples.research.phase2_synthetic_demo \
        --output results/examples/phase2_synthetic
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
from typing import Dict, Tuple
import uuid

import numpy as np
import pandas as pd

from factorlab.backtest import FastBacktestConfig, FeeSchedule, run_fast_backtest
from factorlab.metrics import calculate_portfolio_metrics, evaluate_predictions
from factorlab.reports import (
    write_model_comparison_report,
    write_model_report,
)
from factorlab.reproducibility import set_random_seed
from factorlab.validation import ExpandingWalkForwardSplitter, WalkForwardRunner


WARNING = "SYNTHETIC SOFTWARE DEMO ONLY - NOT A RESEARCH RESULT"


def build_synthetic_inputs(
    start: str = "2020-01-02",
    end: str = "2021-12-31",
    symbols: int = 30,
    seed: int = 42,
) -> Tuple[pd.DatetimeIndex, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Create deterministic features, labels, and positive next-open prices."""

    if symbols < 10:
        raise ValueError("synthetic demo requires at least 10 symbols")
    dates = pd.bdate_range(start, end)
    if len(dates) < 260:
        raise ValueError("synthetic demo requires at least 260 business dates")
    generator = np.random.default_rng(seed)
    codes = ["%06d.SZ" % number for number in range(1, symbols + 1)]
    rows = []
    prices = []
    innovations = generator.normal(0.0, 0.002, size=(len(dates), symbols))
    paths = np.empty_like(innovations)
    paths[0] = 10.0 + np.arange(symbols) * 0.2
    for offset in range(1, len(dates)):
        paths[offset] = paths[offset - 1] * (1.0 + innovations[offset])

    for date_number, trading_date in enumerate(dates):
        market_wave = np.sin(date_number / 17.0)
        for symbol_number, symbol in enumerate(codes):
            cross_section = (symbol_number - (symbols - 1) / 2.0) / symbols
            reversal = cross_section + 0.08 * market_wave
            momentum = np.cos(date_number / 23.0 + symbol_number / 5.0)
            volatility = abs(np.sin(date_number / 29.0 + symbol_number / 7.0))
            noise = generator.normal(0.0, 0.004)
            target = 0.025 * reversal + 0.012 * momentum - 0.01 * volatility + noise
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
            prices.append(
                {
                    "date": trading_date,
                    "symbol": symbol,
                    "adj_open": float(paths[date_number, symbol_number]),
                }
            )
    benchmark = pd.DataFrame(
        {
            "date": dates,
            "adj_open": 5000.0
            * np.cumprod(1.0 + generator.normal(0.0001, 0.003, len(dates))),
        }
    )
    return dates, pd.DataFrame(rows), pd.DataFrame(prices), benchmark


def _atomic_json(payload: dict, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(".%s.%s.tmp" % (destination.name, uuid.uuid4().hex))
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(str(temporary), str(destination))


def run_demo(output_dir: Path, seed: int = 42) -> Dict[str, object]:
    """Run both Phase-2 models and write explicitly synthetic artifacts."""

    set_random_seed(seed)
    dates, dataset, bars, benchmark = build_synthetic_inputs(seed=seed)
    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    readme = destination / "README.md"
    readme.write_text(
        "# %s\n\nThese artifacts validate the offline Phase-2 software path. "
        "They contain generated data and cannot support any claim about CSI500, "
        "A-share factors, model superiority, or expected returns.\n" % WARNING,
        encoding="utf-8",
    )
    splitter_options = dict(
        initial_train_months=6,
        validation_months=3,
        test_months=3,
        step_months=3,
        purge_days=5,
        embargo_days=5,
    )
    common = dict(
        feature_names=("REV1", "MOM20", "VOL20"),
        target_col="EXCESS_RET5",
        top_k=5,
        minimum_cross_section=10,
        random_seed=seed,
    )
    model_options = {
        "factor_score": dict(
            factor_weights={"REV1": 1.0, "MOM20": 1.0, "VOL20": -1.0},
            normalize_by_absolute_weight=True,
        ),
        "ridge": dict(
            ridge_alphas=(0.1, 1.0, 10.0),
            ridge_fit_intercept=True,
            ridge_max_iter=10000,
            ridge_tolerance=0.0001,
        ),
    }
    comparison_records = []
    outputs = {}
    for model_name, options in model_options.items():
        model_output = destination / model_name
        runner = WalkForwardRunner(
            ExpandingWalkForwardSplitter(**splitter_options),
            model_name=model_name,
            model_directory=model_output / "fold_models",
            **common,
            **options,
        )
        walk_forward = runner.run(dataset, calendar=dates)
        prediction = evaluate_predictions(
            walk_forward.predictions,
            target_col="EXCESS_RET5",
            top_k=5,
            minimum_cross_section=10,
        )
        first = walk_forward.predictions["date"].min()
        last = pd.to_datetime(walk_forward.predictions["exit_date"]).max()
        window_bars = bars.loc[bars["date"].between(first, last)]
        window_benchmark = benchmark.loc[benchmark["date"].between(first, last)]
        backtest = run_fast_backtest(
            walk_forward.predictions[["date", "symbol", "score"]],
            window_bars,
            window_benchmark,
            config=FastBacktestConfig(
                top_k=5,
                rebalance_days=5,
                minimum_cross_section=10,
                initial_cash=1_000_000.0,
            ),
            fee_schedule=FeeSchedule.a_share_default(),
        )
        metrics = calculate_portfolio_metrics(
            backtest.nav, backtest.trades, backtest.positions
        )
        summary = write_model_report(
            model_name,
            walk_forward,
            prediction,
            backtest,
            metrics,
            model_output,
            disclaimer=WARNING,
        )
        comparison_records.append(
            {
                "model_name": model_name,
                "prediction": prediction,
                "portfolio_metrics": metrics,
            }
        )
        outputs[model_name] = {
            "summary": str(summary),
            "prediction_rows": int(len(walk_forward.predictions)),
            "folds": int(len(walk_forward.fold_summary)),
        }

    comparison_summary = write_model_comparison_report(
        comparison_records,
        destination / "comparison",
        disclaimer=WARNING,
    )
    manifest = {
        "warning": WARNING,
        "synthetic": True,
        "network_used": False,
        "seed": seed,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "python_version": platform.python_version(),
        "date_start": dates.min().date().isoformat(),
        "date_end": dates.max().date().isoformat(),
        "symbols": int(dataset["symbol"].nunique()),
        "models": outputs,
        "comparison_summary": str(comparison_summary),
    }
    _atomic_json(manifest, destination / "manifest.json")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=WARNING)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("results/examples/phase2_synthetic"),
    )
    parser.add_argument("--seed", type=int, default=42)
    arguments = parser.parse_args()
    manifest = run_demo(arguments.output, seed=arguments.seed)
    print(WARNING)
    print(manifest["comparison_summary"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
