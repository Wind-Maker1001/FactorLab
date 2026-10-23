"""Leakage-neutral evaluation of cross-sectional model predictions.

This module only evaluates scores that have already been produced out of
sample.  It never fits a model, fills a future label, or pools observations
across signal dates when computing IC.  Missing labels are retained in the
coverage denominator and excluded from realised-return statistics.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd
from pandas.api.types import is_bool_dtype, is_complex_dtype, is_numeric_dtype


class PredictionMetricError(ValueError):
    """Raised when prediction data cannot be evaluated safely."""


@dataclass(frozen=True)
class PredictionEvaluation:
    """Daily prediction diagnostics and their requested decompositions."""

    daily: pd.DataFrame
    summary: pd.DataFrame

    @property
    def daily_metrics(self) -> pd.DataFrame:
        """Compatibility alias that makes the table's granularity explicit."""

        return self.daily

    @property
    def overall(self) -> pd.DataFrame:
        return self.summary.loc[self.summary["scope"] == "OVERALL"].copy()

    @property
    def by_year(self) -> pd.DataFrame:
        return self.summary.loc[self.summary["scope"] == "YEAR"].copy()

    @property
    def by_quarter(self) -> pd.DataFrame:
        return self.summary.loc[self.summary["scope"] == "QUARTER"].copy()

    @property
    def by_fold(self) -> pd.DataFrame:
        return self.summary.loc[self.summary["scope"] == "FOLD"].copy()

    @property
    def by_regime(self) -> pd.DataFrame:
        return self.summary.loc[self.summary["scope"] == "REGIME"].copy()


_DAILY_COLUMNS = [
    "date",
    "fold",
    "regime",
    "universe_size",
    "scored_observations",
    "observations",
    "score_coverage",
    "coverage",
    "prediction_ic",
    "prediction_rank_ic",
    "top_k_future_return",
    "bottom_k_future_return",
    "top_bottom_spread",
    "top_count",
    "bottom_count",
    "top_label_count",
    "bottom_label_count",
    "top_label_coverage",
    "bottom_label_coverage",
]

_SUMMARY_COLUMNS = [
    "scope",
    "period",
    "prediction_ic_mean",
    "prediction_ic_std",
    "prediction_icir",
    "prediction_rank_ic_mean",
    "prediction_rank_ic_std",
    "prediction_rank_icir",
    "top_k_future_return",
    "bottom_k_future_return",
    "top_bottom_spread",
    "positive_ic_ratio",
    "positive_rank_ic_ratio",
    "valid_dates",
    "rank_ic_valid_dates",
    "observations",
    "scored_observations",
    "score_coverage",
    "coverage",
    "top_label_coverage",
    "bottom_label_coverage",
]


def _positive_integer(name: str, value: int, minimum: int = 1) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, np.integer)):
        raise PredictionMetricError("%s must be an integer" % name)
    if int(value) < minimum:
        raise PredictionMetricError("%s must be at least %d" % (name, minimum))
    return int(value)


def _normalise_dates(values: pd.Series) -> pd.Series:
    try:
        dates = pd.to_datetime(values, errors="raise")
    except (TypeError, ValueError) as exc:
        raise PredictionMetricError("date contains invalid values") from exc
    if dates.isna().any():
        raise PredictionMetricError("date must not contain missing values")
    if getattr(dates.dt, "tz", None) is not None:
        dates = dates.dt.tz_localize(None)
    return dates.dt.normalize()


def _validate_numeric(data: pd.DataFrame, column: str, *, allow_nan: bool) -> None:
    series = data[column]
    if not is_numeric_dtype(series.dtype) or is_bool_dtype(series.dtype):
        raise PredictionMetricError("%s must be numeric (not bool)" % column)
    if is_complex_dtype(series.dtype):
        raise PredictionMetricError("%s must be real-valued" % column)
    values = series.to_numpy(dtype=float, copy=False)
    if np.isinf(values).any():
        raise PredictionMetricError("%s must not contain infinity" % column)
    if not allow_nan and np.isnan(values).any():
        raise PredictionMetricError("%s must not contain missing values" % column)


def _correlation(left: pd.Series, right: pd.Series, method: str) -> float:
    if len(left) < 2 or left.nunique() < 2 or right.nunique() < 2:
        return float("nan")
    return float(left.corr(right, method=method))


def _single_value_per_date(
    data: pd.DataFrame, column: str, date_keys: Sequence[str]
) -> None:
    if data[column].isna().any():
        raise PredictionMetricError("%s must not contain missing values" % column)
    counts = data.groupby(list(date_keys), sort=False, dropna=False)[column].nunique()
    if (counts > 1).any():
        raise PredictionMetricError(
            "%s must have exactly one value within each prediction date" % column
        )


def _daily_metrics(
    data: pd.DataFrame,
    *,
    score_col: str,
    target_col: str,
    top_k: int,
    minimum_cross_section: int,
    fold_col: Optional[str],
    regime_col: Optional[str],
) -> pd.DataFrame:
    group_columns: List[str] = ["date"]
    if fold_col is not None:
        group_columns.append(fold_col)
    if regime_col is not None:
        group_columns.append(regime_col)

    rows = []
    grouped = data.groupby(group_columns, sort=True, dropna=False)
    for raw_key, cross_section in grouped:
        key = raw_key if isinstance(raw_key, tuple) else (raw_key,)
        key_values = dict(zip(group_columns, key))
        # Freeze the investable ranking using information available on the
        # signal date.  Future-label availability may reduce realised metric
        # coverage, but it must never promote the next-ranked stock into a
        # Top/Bottom-K tail after the fact.
        scored = cross_section.loc[
            cross_section[score_col].notna(),
            ["symbol", score_col, target_col],
        ].copy()
        scored.sort_values(
            [score_col, "symbol"],
            ascending=[False, True],
            kind="mergesort",
            inplace=True,
        )
        eligible = scored.loc[scored[target_col].notna()].copy()
        scored_observations = len(scored)
        observations = len(eligible)
        valid_ic = observations >= minimum_cross_section
        prediction_ic = (
            _correlation(eligible[score_col], eligible[target_col], "pearson")
            if valid_ic
            else np.nan
        )
        prediction_rank_ic = (
            _correlation(eligible[score_col], eligible[target_col], "spearman")
            if valid_ic
            else np.nan
        )

        # Use disjoint tails.  On a full CSI500 cross-section this is exactly
        # Top-K/Bottom-K; small fixtures are capped at half the available set.
        valid_selection = scored_observations >= minimum_cross_section
        tail_size = min(top_k, scored_observations // 2) if valid_selection else 0
        if tail_size:
            top_labels = scored.head(tail_size)[target_col].dropna()
            bottom_labels = scored.tail(tail_size)[target_col].dropna()
            top_label_count = int(len(top_labels))
            bottom_label_count = int(len(bottom_labels))
            top_return = (
                float(top_labels.mean()) if top_label_count else float("nan")
            )
            bottom_return = (
                float(bottom_labels.mean()) if bottom_label_count else float("nan")
            )
            spread = (
                top_return - bottom_return
                if pd.notna(top_return) and pd.notna(bottom_return)
                else float("nan")
            )
        else:
            top_return = bottom_return = spread = float("nan")
            top_label_count = bottom_label_count = 0

        rows.append(
            {
                "date": pd.Timestamp(key_values["date"]),
                "fold": key_values.get(fold_col, pd.NA),
                "regime": key_values.get(regime_col, pd.NA),
                "universe_size": int(len(cross_section)),
                "scored_observations": int(scored_observations),
                "observations": int(observations),
                "score_coverage": (
                    scored_observations / float(len(cross_section))
                    if len(cross_section)
                    else np.nan
                ),
                "coverage": (
                    observations / float(len(cross_section))
                    if len(cross_section)
                    else np.nan
                ),
                "prediction_ic": prediction_ic,
                "prediction_rank_ic": prediction_rank_ic,
                "top_k_future_return": top_return,
                "bottom_k_future_return": bottom_return,
                "top_bottom_spread": spread,
                "top_count": tail_size,
                "bottom_count": tail_size,
                "top_label_count": top_label_count,
                "bottom_label_count": bottom_label_count,
                "top_label_coverage": (
                    top_label_count / float(tail_size) if tail_size else np.nan
                ),
                "bottom_label_coverage": (
                    bottom_label_count / float(tail_size) if tail_size else np.nan
                ),
            }
        )
    return pd.DataFrame(rows, columns=_DAILY_COLUMNS)


def _safe_ratio(mean: float, std: float) -> float:
    return mean / std if pd.notna(std) and std != 0 else float("nan")


def _summarise(values: pd.DataFrame, scope: str, period: str) -> dict:
    ic = values["prediction_ic"].dropna()
    rank_ic = values["prediction_rank_ic"].dropna()
    ic_mean = float(ic.mean()) if len(ic) else float("nan")
    rank_mean = float(rank_ic.mean()) if len(rank_ic) else float("nan")
    ic_std = float(ic.std(ddof=1)) if len(ic) else float("nan")
    rank_std = float(rank_ic.std(ddof=1)) if len(rank_ic) else float("nan")
    return {
        "scope": scope,
        "period": period,
        "prediction_ic_mean": ic_mean,
        "prediction_ic_std": ic_std,
        "prediction_icir": _safe_ratio(ic_mean, ic_std),
        "prediction_rank_ic_mean": rank_mean,
        "prediction_rank_ic_std": rank_std,
        "prediction_rank_icir": _safe_ratio(rank_mean, rank_std),
        "top_k_future_return": values["top_k_future_return"].mean(),
        "bottom_k_future_return": values["bottom_k_future_return"].mean(),
        "top_bottom_spread": values["top_bottom_spread"].mean(),
        "positive_ic_ratio": float((ic > 0).mean()) if len(ic) else np.nan,
        "positive_rank_ic_ratio": (
            float((rank_ic > 0).mean()) if len(rank_ic) else np.nan
        ),
        "valid_dates": int(len(ic)),
        "rank_ic_valid_dates": int(len(rank_ic)),
        "observations": int(values["observations"].sum()),
        "scored_observations": int(values["scored_observations"].sum()),
        "score_coverage": values["score_coverage"].mean(),
        "coverage": values["coverage"].mean(),
        "top_label_coverage": values["top_label_coverage"].mean(),
        "bottom_label_coverage": values["bottom_label_coverage"].mean(),
    }


def _summary_metrics(
    daily: pd.DataFrame, *, include_fold: bool, include_regime: bool
) -> pd.DataFrame:
    if daily.empty:
        return pd.DataFrame(columns=_SUMMARY_COLUMNS)
    rows = [_summarise(daily, "OVERALL", "ALL")]

    years = daily.assign(_period=daily["date"].dt.year.astype(str))
    for period, values in years.groupby("_period", sort=True):
        rows.append(_summarise(values, "YEAR", str(period)))

    quarters = daily.assign(_period=daily["date"].dt.to_period("Q").astype(str))
    for period, values in quarters.groupby("_period", sort=True):
        rows.append(_summarise(values, "QUARTER", str(period)))

    if include_fold:
        for period, values in daily.groupby("fold", sort=True, dropna=False):
            rows.append(_summarise(values, "FOLD", str(period)))
    if include_regime:
        for period, values in daily.groupby("regime", sort=True, dropna=False):
            rows.append(_summarise(values, "REGIME", str(period)))
    return pd.DataFrame(rows, columns=_SUMMARY_COLUMNS)


def evaluate_predictions(
    frame: pd.DataFrame,
    *,
    score_col: str = "score",
    target_col: str = "EXCESS_RET5",
    top_k: int = 20,
    minimum_cross_section: int = 2,
    fold_col: Optional[str] = "fold",
    regime_col: Optional[str] = None,
) -> PredictionEvaluation:
    """Evaluate daily cross-sectional ranking quality.

    The input must contain one row per ``(date, symbol)``.  ``NaN`` labels are
    legitimate for an as-yet-unrealised test tail: they reduce coverage and
    are excluded from every realised metric.  Missing scores are handled the
    same way, while infinities and duplicate observations fail fast.

    ``fold_col`` is optional in practice: the default ``"fold"`` is used when
    present and otherwise silently disabled.  An explicitly requested regime
    column must exist and be constant within each date (and fold).
    """

    if not isinstance(frame, pd.DataFrame):
        raise PredictionMetricError("frame must be a pandas DataFrame")
    if frame.empty:
        raise PredictionMetricError("frame must not be empty")
    if frame.columns.has_duplicates:
        raise PredictionMetricError("frame must not contain duplicate column names")
    top_k = _positive_integer("top_k", top_k)
    minimum_cross_section = _positive_integer(
        "minimum_cross_section", minimum_cross_section, minimum=2
    )
    for name, value in (("score_col", score_col), ("target_col", target_col)):
        if not isinstance(value, str) or not value:
            raise PredictionMetricError("%s must be a non-empty string" % name)
    required = {"date", "symbol", score_col, target_col}
    missing = sorted(required.difference(frame.columns))
    if missing:
        raise PredictionMetricError(
            "prediction data is missing required columns: %s" % ", ".join(missing)
        )

    effective_fold = fold_col if fold_col is not None and fold_col in frame.columns else None
    if regime_col is not None and regime_col not in frame.columns:
        raise PredictionMetricError("prediction data is missing regime column %s" % regime_col)

    data = frame.copy()
    data["date"] = _normalise_dates(data["date"])
    if data["symbol"].isna().any() or data["symbol"].astype(str).str.strip().eq("").any():
        raise PredictionMetricError("symbol must not contain missing or empty values")
    data["symbol"] = data["symbol"].astype(str)
    if data.duplicated(["date", "symbol"]).any():
        duplicates = data.loc[data.duplicated(["date", "symbol"], keep=False), ["date", "symbol"]]
        sample = duplicates.head(3).astype(str).agg("/".join, axis=1).tolist()
        raise PredictionMetricError(
            "duplicate prediction observations for (date, symbol): %s" % ", ".join(sample)
        )
    _validate_numeric(data, score_col, allow_nan=True)
    _validate_numeric(data, target_col, allow_nan=True)

    date_keys = ["date"]
    if effective_fold is not None:
        _single_value_per_date(data, effective_fold, date_keys)
        date_keys.append(effective_fold)
    if regime_col is not None:
        _single_value_per_date(data, regime_col, date_keys)

    daily = _daily_metrics(
        data,
        score_col=score_col,
        target_col=target_col,
        top_k=top_k,
        minimum_cross_section=minimum_cross_section,
        fold_col=effective_fold,
        regime_col=regime_col,
    )
    return PredictionEvaluation(
        daily=daily,
        summary=_summary_metrics(
            daily,
            include_fold=effective_fold is not None,
            include_regime=regime_col is not None,
        ),
    )


__all__ = [
    "PredictionEvaluation",
    "PredictionMetricError",
    "evaluate_predictions",
]
