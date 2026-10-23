"""Deterministic Top-K portfolio construction from cross-sectional scores."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

import numpy as np
import pandas as pd

from factorlab.errors import ConfigurationError, SchemaError


def _positive_integer(name: str, value: object) -> int:
    if (
        isinstance(value, (bool, np.bool_))
        or not isinstance(value, (int, np.integer))
        or int(value) <= 0
    ):
        raise ConfigurationError("%s must be a positive integer" % name)
    return int(value)


def _normalise_dates(values: Iterable[object], label: str) -> pd.DatetimeIndex:
    try:
        dates = pd.DatetimeIndex(pd.to_datetime(list(values), errors="raise")).normalize()
    except (TypeError, ValueError) as exc:
        raise SchemaError("%s contains an invalid date" % label) from exc
    if dates.tz is not None:
        dates = dates.tz_localize(None)
    if dates.has_duplicates:
        dates = dates.drop_duplicates()
    return dates.sort_values()


def rebalance_dates(
    trading_dates: Iterable[object],
    every_n_days: int = 1,
    available_signal_dates: Optional[Iterable[object]] = None,
) -> pd.DatetimeIndex:
    """Select an expanding sequence of rebalance signal dates.

    The schedule is anchored to the first available signal date and advances
    by positions in the supplied market trading calendar, never by calendar
    days.  Missing signals do not shift later scheduled dates.
    """

    every_n_days = _positive_integer("every_n_days", every_n_days)
    calendar = _normalise_dates(trading_dates, "trading_dates")
    if calendar.empty:
        return calendar
    if available_signal_dates is None:
        available = set(calendar)
    else:
        available = set(_normalise_dates(available_signal_dates, "signal dates"))
    eligible_positions = [i for i, value in enumerate(calendar) if value in available]
    if not eligible_positions:
        return pd.DatetimeIndex([], dtype="datetime64[ns]")
    anchor = eligible_positions[0]
    selected = [
        value
        for i, value in enumerate(calendar)
        if i >= anchor and (i - anchor) % every_n_days == 0 and value in available
    ]
    return pd.DatetimeIndex(selected)


def select_top_k(
    scores: pd.DataFrame,
    top_k: int = 20,
    rebalance_every: int = 1,
    trading_dates: Optional[Iterable[object]] = None,
    minimum_cross_section: int = 1,
) -> pd.DataFrame:
    """Rank each scheduled cross-section and assign equal target weights.

    Ties are resolved by canonical symbol order, making the result independent
    of upstream row order.  Non-finite scores are ineligible.  A scheduled
    rebalance requires at least ``max(top_k, minimum_cross_section)`` finite
    scores; sparse cross-sections fail instead of silently concentrating the
    portfolio or hiding a data-quality failure.
    """

    top_k = _positive_integer("top_k", top_k)
    rebalance_every = _positive_integer("rebalance_every", rebalance_every)
    minimum_cross_section = _positive_integer(
        "minimum_cross_section", minimum_cross_section
    )
    required = {"date", "symbol", "score"}
    missing = sorted(required.difference(scores.columns))
    if missing:
        raise SchemaError("Top-K scores missing columns: %s" % ", ".join(missing))

    frame = scores[["date", "symbol", "score"]].copy()
    try:
        frame["date"] = pd.to_datetime(frame["date"], errors="raise").dt.normalize()
    except (TypeError, ValueError) as exc:
        raise SchemaError("scores.date contains an invalid value") from exc
    if getattr(frame["date"].dt, "tz", None) is not None:
        frame["date"] = frame["date"].dt.tz_localize(None)
    if frame[["date", "symbol"]].isna().any().any():
        raise SchemaError("score date and symbol must not be null")
    if frame.duplicated(["date", "symbol"]).any():
        raise SchemaError("duplicate score (date, symbol) rows detected")
    frame["symbol"] = frame["symbol"].astype(str)
    raw_scores = frame["score"].copy()
    frame["score"] = pd.to_numeric(raw_scores, errors="coerce")
    invalid_scores = raw_scores.notna() & frame["score"].isna()
    if invalid_scores.any():
        raise SchemaError("score contains a non-numeric value")
    frame.loc[~np.isfinite(frame["score"]), "score"] = np.nan

    signal_dates = frame["date"].drop_duplicates()
    calendar = trading_dates if trading_dates is not None else frame["date"].drop_duplicates()
    scheduled_dates = rebalance_dates(calendar, rebalance_every, signal_dates)
    scheduled = set(scheduled_dates)
    frame = frame[frame["date"].isin(scheduled)].copy()

    required_candidates = max(top_k, int(minimum_cross_section))
    finite_counts = (
        frame.loc[frame["score"].notna()]
        .groupby("date", sort=False)
        .size()
        .reindex(scheduled_dates, fill_value=0)
    )
    insufficient = finite_counts[finite_counts < required_candidates]
    if not insufficient.empty:
        failed_date = pd.Timestamp(insufficient.index[0]).date().isoformat()
        raise SchemaError(
            "Top-K rebalance on %s has finite-score count %d; at least %d are required"
            % (failed_date, int(insufficient.iloc[0]), required_candidates)
        )
    frame = frame[frame["score"].notna()].copy()

    selected_parts = []
    for date, cross_section in frame.groupby("date", sort=True):
        ranked = cross_section.sort_values(
            ["score", "symbol"], ascending=[False, True], kind="mergesort"
        ).head(top_k)
        ranked = ranked.copy()
        ranked["rank"] = np.arange(1, len(ranked) + 1, dtype=int)
        ranked["target_weight"] = 1.0 / float(len(ranked))
        selected_parts.append(ranked)
    if not selected_parts:
        return pd.DataFrame(
            {
                "date": pd.Series(dtype="datetime64[ns]"),
                "symbol": pd.Series(dtype=str),
                "score": pd.Series(dtype=float),
                "rank": pd.Series(dtype=int),
                "target_weight": pd.Series(dtype=float),
            }
        )
    return pd.concat(selected_parts, ignore_index=True).sort_values(
        ["date", "rank", "symbol"], kind="mergesort"
    ).reset_index(drop=True)


@dataclass(frozen=True)
class TopKPortfolio:
    """Configured Top-K/equal-weight portfolio constructor."""

    top_k: int = 20
    rebalance_days: int = 5
    minimum_cross_section: int = 1

    def construct(
        self,
        scores: pd.DataFrame,
        trading_dates: Optional[Iterable[object]] = None,
    ) -> pd.DataFrame:
        return select_top_k(
            scores,
            top_k=self.top_k,
            rebalance_every=self.rebalance_days,
            trading_dates=trading_dates,
            minimum_cross_section=self.minimum_cross_section,
        )


__all__ = ["TopKPortfolio", "rebalance_dates", "select_top_k"]
