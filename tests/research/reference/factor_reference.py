"""Cross-sectional factor evaluation with transparent daily aggregates."""

from dataclasses import dataclass
import math
from typing import Iterable, List, Optional, Tuple

import numpy as np
import pandas as pd

from qbot.research.data.calendar import TradingCalendar
from qbot.research.errors import ConfigurationError, SchemaError


@dataclass
class FactorEvaluation:
    daily_ic: pd.DataFrame
    summary: pd.DataFrame
    quantile_returns: pd.DataFrame
    autocorrelation: pd.DataFrame
    turnover: pd.DataFrame
    correlation: pd.DataFrame


def _safe_correlation(left: pd.Series, right: pd.Series, method: str) -> float:
    pair = pd.concat([left, right], axis=1).replace([np.inf, -np.inf], np.nan).dropna()
    if len(pair) < 2 or pair.iloc[:, 0].nunique() < 2 or pair.iloc[:, 1].nunique() < 2:
        return float("nan")
    return float(pair.iloc[:, 0].corr(pair.iloc[:, 1], method=method))


def _daily_ic(
    frame: pd.DataFrame,
    factors: List[str],
    target: str,
    minimum_cross_section: int,
    scope: str,
) -> pd.DataFrame:
    rows = []
    for date, cross_section in frame.groupby("date", sort=True):
        for factor in factors:
            pairs = cross_section[[factor, target]].replace([np.inf, -np.inf], np.nan).dropna()
            count = len(pairs)
            if count < minimum_cross_section:
                pearson = float("nan")
                rank_ic = float("nan")
            else:
                pearson = _safe_correlation(pairs[factor], pairs[target], "pearson")
                rank_ic = _safe_correlation(pairs[factor], pairs[target], "spearman")
            rows.append(
                {
                    "scope": scope,
                    "date": date,
                    "factor": factor,
                    "observations": count,
                    "coverage": count / float(len(cross_section)) if len(cross_section) else np.nan,
                    "IC": pearson,
                    "RankIC": rank_ic,
                }
            )
    return pd.DataFrame(rows)


def _summarise_ic(daily: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for (scope, factor), values in daily.groupby(["scope", "factor"], sort=True):
        ic = values["IC"].dropna()
        rank_ic = values["RankIC"].dropna()
        ic_std = ic.std(ddof=1)
        rank_std = rank_ic.std(ddof=1)
        rows.append(
            {
                "scope": scope,
                "factor": factor,
                "IC Mean": ic.mean(),
                "IC Std": ic_std,
                "ICIR": ic.mean() / ic_std if pd.notna(ic_std) and ic_std != 0 else np.nan,
                "RankIC Mean": rank_ic.mean(),
                "RankIC Std": rank_std,
                "RankICIR": (
                    rank_ic.mean() / rank_std
                    if pd.notna(rank_std) and rank_std != 0
                    else np.nan
                ),
                "IC > 0 Ratio": (ic > 0).mean() if len(ic) else np.nan,
                "RankIC > 0 Ratio": (rank_ic > 0).mean() if len(rank_ic) else np.nan,
                "Valid Dates": int(ic.notna().sum()),
                "RankIC Valid Dates": int(rank_ic.notna().sum()),
                "Coverage": values["coverage"].mean(),
            }
        )
    return pd.DataFrame(rows)


def _quantile_returns(
    frame: pd.DataFrame,
    factors: List[str],
    target: str,
    quantiles: int,
    minimum_cross_section: int,
    scope: str,
) -> pd.DataFrame:
    rows = []
    labels = ["Q%d" % number for number in range(1, quantiles + 1)]
    for date, cross_section in frame.groupby("date", sort=True):
        for factor in factors:
            # Quantile membership is formed from the contemporaneous factor
            # only.  Whether a future label later becomes observable cannot
            # change bucket boundaries or promote another security.
            available = cross_section[[factor, target]].replace(
                [np.inf, -np.inf], np.nan
            )
            available = available.loc[available[factor].notna()].copy()
            if len(available) < max(minimum_cross_section, quantiles):
                continue
            try:
                buckets = pd.qcut(
                    available[factor].rank(method="first"),
                    q=quantiles,
                    labels=labels,
                )
            except ValueError:
                continue
            realised = available[target].notna()
            means = available.groupby(buckets, observed=False)[target].mean()
            bucket_counts = buckets.value_counts(sort=False)
            label_counts = buckets[realised].value_counts(sort=False)
            for label in labels:
                selected_count = int(bucket_counts.get(label, 0))
                labelled_count = int(label_counts.get(label, 0))
                rows.append(
                    {
                        "scope": scope,
                        "date": date,
                        "factor": factor,
                        "quantile": label,
                        "future_return": means.get(label, np.nan),
                        "observations": selected_count,
                        "labeled_observations": labelled_count,
                        "label_coverage": (
                            labelled_count / float(selected_count)
                            if selected_count
                            else np.nan
                        ),
                    }
                )
            tail_selected = int(
                bucket_counts.get(labels[-1], 0) + bucket_counts.get(labels[0], 0)
            )
            tail_labelled = int(
                label_counts.get(labels[-1], 0) + label_counts.get(labels[0], 0)
            )
            rows.append(
                {
                    "scope": scope,
                    "date": date,
                    "factor": factor,
                    "quantile": "Q%d-Q1" % quantiles,
                    "future_return": means.get(labels[-1], np.nan) - means.get(labels[0], np.nan),
                    "observations": tail_selected,
                    "labeled_observations": tail_labelled,
                    "label_coverage": (
                        tail_labelled / float(tail_selected)
                        if tail_selected
                        else np.nan
                    ),
                }
            )
    return pd.DataFrame(rows)


def _autocorrelation(
    frame: pd.DataFrame,
    factors: List[str],
    minimum_cross_section: int,
    scope: str,
    dates: pd.DatetimeIndex,
) -> pd.DataFrame:
    rows = []
    cross_sections = dict(tuple(frame.groupby("date", sort=True)))
    empty = frame.iloc[:0]
    for factor in factors:
        previous: Optional[pd.DataFrame] = None
        for date in dates:
            current = cross_sections.get(date, empty)[["symbol", factor]].rename(
                columns={factor: "current"}
            )
            if previous is not None:
                joined = current.merge(previous, on="symbol", how="inner").dropna()
                value = (
                    _safe_correlation(joined["current"], joined["previous"], "spearman")
                    if len(joined) >= minimum_cross_section
                    else np.nan
                )
                rows.append(
                    {
                        "scope": scope,
                        "date": date,
                        "factor": factor,
                        "autocorrelation": value,
                        "observations": len(joined),
                    }
                )
            previous = current.rename(columns={"current": "previous"})
    return pd.DataFrame(rows)


def _top_quantile_turnover(
    frame: pd.DataFrame,
    factors: List[str],
    quantiles: int,
    minimum_cross_section: int,
    scope: str,
    dates: pd.DatetimeIndex,
) -> pd.DataFrame:
    rows = []
    cross_sections = dict(tuple(frame.groupby("date", sort=True)))
    empty = frame.iloc[:0]
    for factor in factors:
        previous_weights = {}
        for date in dates:
            cross_section = cross_sections.get(date, empty)
            available = cross_section[["symbol", factor]].replace([np.inf, -np.inf], np.nan).dropna()
            if len(available) < minimum_cross_section:
                # A missing/invalid session is not a zero-weight portfolio and
                # must not be skipped to compare two non-adjacent sessions.
                previous_weights = {}
                continue
            count = max(1, int(math.ceil(len(available) / float(quantiles))))
            top = available.nlargest(count, factor, keep="first")["symbol"].tolist()
            current_weights = {symbol: 1.0 / len(top) for symbol in top}
            if previous_weights:
                symbols = set(previous_weights).union(current_weights)
                turnover = 0.5 * sum(
                    abs(current_weights.get(symbol, 0.0) - previous_weights.get(symbol, 0.0))
                    for symbol in symbols
                )
                rows.append(
                    {
                        "scope": scope,
                        "date": date,
                        "factor": factor,
                        "top_quantile_turnover": turnover,
                        "holdings": len(top),
                    }
                )
            previous_weights = current_weights
    return pd.DataFrame(rows)


def _factor_correlation(frame: pd.DataFrame, factors: List[str]) -> pd.DataFrame:
    matrices = []
    for _, cross_section in frame.groupby("date", sort=True):
        matrix = cross_section[factors].corr(method="pearson", min_periods=2)
        matrices.append(matrix)
    if not matrices:
        return pd.DataFrame(index=factors, columns=factors, dtype=float)
    stack = np.stack([matrix.reindex(index=factors, columns=factors).to_numpy() for matrix in matrices])
    with np.errstate(invalid="ignore"):
        averaged = np.nanmean(stack, axis=0)
    return pd.DataFrame(averaged, index=factors, columns=factors)


def evaluate_factors(
    frame: pd.DataFrame,
    factor_names: Iterable[str],
    target: str = "EXCESS_RET5",
    quantiles: int = 5,
    minimum_cross_section: int = 20,
    group_column: Optional[str] = None,
    calendar: Optional[Iterable[object]] = None,
) -> FactorEvaluation:
    """Evaluate factors overall and optionally within named market regimes."""

    factors = list(factor_names)
    required = {"date", "symbol", target}.union(factors)
    if not required.issubset(frame.columns):
        raise SchemaError("Factor evaluation missing columns %s" % sorted(required.difference(frame.columns)))
    if quantiles < 2:
        raise ConfigurationError("quantiles must be at least 2")
    if minimum_cross_section < quantiles:
        raise ConfigurationError("minimum_cross_section must be >= quantiles")
    data = frame.copy()
    data["date"] = pd.to_datetime(data["date"], errors="raise").dt.normalize()
    if data["date"].isna().any() or data.duplicated(["date", "symbol"]).any():
        raise SchemaError("Factor evaluation requires valid unique (date, symbol) keys")
    observed_dates = pd.DatetimeIndex(data["date"].drop_duplicates().sort_values())
    dates = TradingCalendar(
        observed_dates if calendar is None else pd.DatetimeIndex(list(calendar))
    ).dates
    if len(observed_dates.difference(dates)):
        raise SchemaError("Factor evaluation dates must belong to the supplied calendar")
    scopes: List[Tuple[str, pd.DataFrame]] = [("ALL", data)]
    if group_column is not None:
        if group_column not in data:
            raise SchemaError("Group column %s is missing" % group_column)
        per_date_groups = data.dropna(subset=[group_column]).groupby("date")[
            group_column
        ].nunique()
        if per_date_groups.gt(1).any():
            raise SchemaError(
                "Group column %s must have at most one value per date"
                % group_column
            )
        scopes.extend(
            (str(value), group.copy())
            for value, group in data.dropna(subset=[group_column]).groupby(group_column, sort=True)
        )
    daily_parts = []
    quantile_parts = []
    autocorrelation_parts = []
    turnover_parts = []
    for scope, subset in scopes:
        daily_parts.append(_daily_ic(subset, factors, target, minimum_cross_section, scope))
        quantile_parts.append(
            _quantile_returns(subset, factors, target, quantiles, minimum_cross_section, scope)
        )
    # Temporal metrics must always compare adjacent dates on the complete
    # market calendar.  Filtering to one regime first would incorrectly treat
    # BULL -> BEAR -> BULL as two adjacent BULL observations.
    all_autocorrelation = _autocorrelation(
        data, factors, minimum_cross_section, "ALL", dates
    )
    all_turnover = _top_quantile_turnover(
        data, factors, quantiles, minimum_cross_section, "ALL", dates
    )
    autocorrelation_parts.append(all_autocorrelation)
    turnover_parts.append(all_turnover)
    if group_column is not None:
        date_groups = (
            data.dropna(subset=[group_column])[["date", group_column]]
            .drop_duplicates("date")
            .set_index("date")[group_column]
        )
        for value in sorted(date_groups.astype(str).unique()):
            selected_dates = date_groups.index[date_groups.astype(str) == value]
            if not all_autocorrelation.empty:
                scoped_autocorrelation = all_autocorrelation.loc[
                    all_autocorrelation["date"].isin(selected_dates)
                ].copy()
                scoped_autocorrelation["scope"] = value
                autocorrelation_parts.append(scoped_autocorrelation)
            if not all_turnover.empty:
                scoped_turnover = all_turnover.loc[
                    all_turnover["date"].isin(selected_dates)
                ].copy()
                scoped_turnover["scope"] = value
                turnover_parts.append(scoped_turnover)
    daily = pd.concat(daily_parts, ignore_index=True) if daily_parts else pd.DataFrame()
    quantile_frame = (
        pd.concat(quantile_parts, ignore_index=True)
        if any(not part.empty for part in quantile_parts)
        else pd.DataFrame(
            columns=[
                "scope",
                "date",
                "factor",
                "quantile",
                "future_return",
                "observations",
                "labeled_observations",
                "label_coverage",
            ]
        )
    )
    autocorrelation_frame = (
        pd.concat(autocorrelation_parts, ignore_index=True)
        if any(not part.empty for part in autocorrelation_parts)
        else pd.DataFrame(columns=["scope", "date", "factor", "autocorrelation", "observations"])
    )
    turnover_frame = (
        pd.concat(turnover_parts, ignore_index=True)
        if any(not part.empty for part in turnover_parts)
        else pd.DataFrame(columns=["scope", "date", "factor", "top_quantile_turnover", "holdings"])
    )
    return FactorEvaluation(
        daily_ic=daily,
        summary=_summarise_ic(daily),
        quantile_returns=quantile_frame,
        autocorrelation=autocorrelation_frame,
        turnover=turnover_frame,
        correlation=_factor_correlation(data, factors),
    )
