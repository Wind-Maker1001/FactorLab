"""Artificial OHLCV, PIT and security inputs; never financial evidence.

This optional namespace package deliberately has no __init__.py. Its tests
and coverage run separately from the repository's frozen research CI suite.
"""

from dataclasses import dataclass

import numpy as np
import pandas as pd

from factorlab.dataset.builder import DatasetBuilder, DatasetBuildOptions
from factorlab.factors.registry import FACTOR_REGISTRY
from factorlab.universe.filters import UniverseFilterConfig
from factorlab.validation import ExpandingWalkForwardSplitter, WalkForwardRunner


FEATURES = tuple(FACTOR_REGISTRY)
MODELS = ("factor_score", "ridge", "lightgbm", "xgboost")
TARGET = "EXCESS_RET5"
WARNING = "SYNTHETIC SOFTWARE ACCEPTANCE ONLY / NO LIVE MONEY / 非五年中证500研究结果"


@dataclass
class Inputs:
    calendar: pd.DatetimeIndex
    bars: pd.DataFrame
    benchmark: pd.DataFrame
    membership: pd.DataFrame
    status: pd.DataFrame
    listings: pd.DataFrame

    @property
    def research_calendar(self):
        return pd.DatetimeIndex(self.membership.date.drop_duplicates().sort_values())


def generate_inputs(symbols=12, sessions=300, seed=42):
    if symbols < 8 or sessions < 260:
        raise ValueError("readiness inputs require >=8 symbols and >=260 sessions")
    # Preserve the existing 120-session filter/listing warmup. Only the
    # following `sessions` dates have research PIT membership.
    dates = pd.bdate_range("2017-07-18", periods=sessions + 120)
    total = len(dates)
    codes = ["%06d.SZ" % (i + 1) for i in range(symbols)]
    rng = np.random.default_rng(seed)
    innovations = rng.normal(0.0001, 0.008, (total, symbols))
    close = (10 + np.arange(symbols) / symbols * 20) * np.exp(np.cumsum(innovations, axis=0))
    open_ = close * np.exp(rng.normal(0, 0.002, close.shape))
    bars = pd.DataFrame({
        "date": np.repeat(dates.to_numpy(), symbols), "symbol": codes * total,
        "open": open_.ravel(), "close": close.ravel(),
        "high": (np.maximum(open_, close) * 1.01).ravel(),
        "low": (np.minimum(open_, close) * .99).ravel(),
        "volume": rng.uniform(1e5, 1e6, close.shape).ravel(),
        "turnover": rng.uniform(.001, .08, close.shape).ravel(),
    })
    bars["amount"] = bars.volume * bars.close
    for column in ("open", "close", "high", "low"):
        bars["adj_" + column] = bars[column]  # No corporate actions in artificial data.
        bars["raw_" + column] = bars[column]
    index_close = 5000 * np.exp(np.cumsum(rng.normal(.0001, .004, total)))
    benchmark = pd.DataFrame({
        "date": dates, "adj_open": index_close * np.exp(rng.normal(0, .001, total)),
        "adj_close": index_close,
    })
    keys = bars[["date", "symbol"]]
    return Inputs(
        dates, bars, benchmark, keys.loc[keys.date.isin(dates[120:])].assign(is_member=True),
        keys.assign(trade_status=1, is_st=False),
        pd.DataFrame({"symbol": codes, "listing_date": pd.Timestamp("2010-01-01")}),
    )


def build_dataset(inputs):
    return DatasetBuilder(DatasetBuildOptions(
        factor_names=FEATURES, expected_member_count=inputs.bars.symbol.nunique(),
        minimum_cross_section=8, filter_config=UniverseFilterConfig(),
    )).build(inputs.bars, inputs.benchmark, inputs.membership, inputs.status,
             inputs.listings, inputs.calendar)


def split_options(size="small"):
    return dict(initial_train_months=4 if size == "small" else 24,
                validation_months=2 if size == "small" else 3,
                test_months=3, step_months=3, purge_days=5, embargo_days=5)


def model_options(model):
    # Explicitly small diagnostic candidates, never edits to formal YAML/models.
    if model == "factor_score":
        return {"factor_weights": {name: 1.0 for name in FEATURES}}
    if model == "ridge":
        return {"ridge_alphas": (.1, 10.)}
    if model == "lightgbm":
        return {"lightgbm_candidates": (
            {"n_estimators": 8, "num_leaves": 7, "max_depth": 3, "min_child_samples": 8},
            {"n_estimators": 12, "num_leaves": 7, "max_depth": 3, "min_child_samples": 8},
        ), "lightgbm_n_jobs": 1}
    if model == "xgboost":
        return {"xgboost_candidates": (
            {"n_estimators": 8, "max_depth": 2},
            {"n_estimators": 12, "max_depth": 2},
        ), "xgboost_n_jobs": 1}
    raise ValueError(model)


def make_runner(model, size="small", seed=42):
    return WalkForwardRunner(
        ExpandingWalkForwardSplitter(**split_options(size)), model_name=model,
        feature_names=FEATURES, top_k=3 if size == "small" else 20,
        minimum_cross_section=8, regime_col="MARKET_REGIME", random_seed=seed, **model_options(model),
    )
