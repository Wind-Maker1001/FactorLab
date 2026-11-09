"""Reproduce the deterministic synthetic pair experiment from a saved JSON config."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import platform
import subprocess
import sys

import numpy as np
import pandas as pd
import statsmodels

from factorlab.backtest.pairs import BorrowTerms
from factorlab.dataset.identity import file_hash, frame_hash
from factorlab.experiments.tracker import ExperimentTracker
from factorlab.strategies.pairs_statistics import SelectionConfig
from factorlab.strategies.pairs_trading import PairTradingConfig, run_pair_research
from factorlab.validation.splitter import ExpandingWalkForwardSplitter


def synthetic_panels(*, seed=810, periods=1000, relationship="cointegrated"):
    """AR(1) residual versus an independent integrated residual on a shared trend."""
    if relationship not in {"cointegrated", "noncointegrated"}:
        raise ValueError("unknown synthetic relationship")
    rng = np.random.default_rng(seed)
    index = pd.bdate_range("2020-01-01", periods=periods)
    if relationship == "cointegrated":
        x = 100 + np.cumsum(rng.normal(scale=.5, size=periods))
        spread = np.zeros(periods)
        shocks = rng.normal(scale=.6, size=periods)
        for i in range(1, periods):
            spread[i] = .65 * spread[i - 1] + shocks[i]
        y = 7 + 1.6 * x + spread
    else:
        x = 100 + np.arange(periods) + rng.normal(size=periods)
        spread = np.cumsum(rng.normal(size=periods))
        y = 100 + 2 * (x - 100) + spread
    closes = pd.DataFrame({"A": y, "B": x}, index=index)
    # Separate observable opens with overnight gaps, not today's close fills.
    opens = closes.shift(1).copy()
    opens.iloc[0] = closes.iloc[0]
    opens *= 1 + rng.normal(scale=.0005, size=opens.shape)
    return opens, closes


def execute(config, output):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    opens, closes = synthetic_panels(**config["synthetic"])
    strategy = PairTradingConfig(**config["strategy"])
    splitter = ExpandingWalkForwardSplitter(**config["splitter"])
    selection = SelectionConfig(train_end=closes.index[0].isoformat(), **config["selection"])
    borrow = {symbol: BorrowTerms(**terms) for symbol, terms in config["borrow"].items()}
    result = run_pair_research(opens, closes, selection=selection, strategy=strategy, splitter=splitter,
                              borrow=borrow, entry_grid=config["entry_grid"])
    (output / "config.json").write_text(json.dumps(config, indent=2, allow_nan=False), encoding="utf-8")
    for key in ("nav", "selections", "trades", "positions", "failures", "signals", "validation"):
        getattr(result, key).to_csv(output / (key + ".csv"), index=False)
    (output / "folds.json").write_text(result.folds.to_json(orient="records", date_format="iso", indent=2), encoding="utf-8")
    (output / "evaluation.json").write_text(json.dumps(result.evaluation.to_dict(), indent=2, allow_nan=False), encoding="utf-8")
    (output / "metadata.json").write_text(json.dumps(result.metadata, indent=2, allow_nan=False), encoding="utf-8")
    tracker = ExperimentTracker(output / "tracker")
    windows = {key: [str(result.folds[key + "_start"].iloc[0]), str(result.folds[key + "_end"].iloc[-1])]
               for key in ("train", "validation", "test")}
    tracker.create_experiment("pairs", dict(model="OLS-Engle-Granger-pairs", parameters=config,
        factors=[], dataset={"kind": "SYNTHETIC", "prices": frame_hash(closes.reset_index()),
                             "opens": frame_hash(opens.reset_index())}, label={"name": "marked-equity"},
        windows=windows, walk_forward=config["splitter"], costs=dict(commission_bps=strategy.commission_bps,
            minimum_commission=strategy.minimum_commission, borrow=config["borrow"], margin_rate=strategy.margin_rate)))
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    tree = subprocess.check_output(["git", "rev-parse", "HEAD^{tree}"], text=True).strip()
    dirty = bool(subprocess.check_output(["git", "status", "--porcelain"], text=True).strip())
    metrics = {key: metric.value for key, metric in result.evaluation.metrics.items()}
    tracker.append_run("pairs", "synthetic", code=dict(commit=commit, tree=tree, dirty=dirty),
        environment=dict(python=platform.python_version(), numpy=np.__version__, pandas=pd.__version__,
                         statsmodels=statsmodels.__version__),
        command=[sys.executable, "-m", "factorlab.strategies.pairs_cli", "--config", str((output / "config.json").resolve()),
                 "--output", "NEW_OUTPUT_DIRECTORY"], status="SUCCEEDED", qualification="SYNTHETIC",
        portfolio_metrics=metrics, validation=dict(sample_hash=frame_hash(result.nav[["date", "strategy_return"]]),
            evaluation="ResearchEvaluation.portfolio", result_kind="SOFTWARE_ONLY", actual_tradability="UNKNOWN"),
        artifacts={p.name: dict(path=str(p.resolve()), sha256=file_hash(p)) for p in output.iterdir() if p.is_file()})
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    result = execute(json.loads(args.config.read_text(encoding="utf-8")), args.output)
    print(json.dumps(dict(metadata=result.metadata, folds=len(result.folds), trades=len(result.trades),
        accepted_pairs=int(result.selections.accepted.sum()), terminal_nav=float(result.nav.strategy_nav.iloc[-1]),
        metrics={key: value.value for key, value in result.evaluation.metrics.items()}), indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
