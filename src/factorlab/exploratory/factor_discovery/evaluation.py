"""Development-only adapter for existing FactorLab evaluation mathematics."""

from dataclasses import dataclass
import time

import numpy as np
import pandas as pd

from factorlab.factors.preprocess import preprocess_cross_sections
from factorlab.metrics.factor import evaluate_factors

from .contracts import DiscoveryConfig, DiscoveryError, ErrorCode


@dataclass
class DiscoveryEvaluation:
    quality: pd.DataFrame
    daily_ic: pd.DataFrame
    summary: pd.DataFrame
    quantile_returns: pd.DataFrame
    label_sensitivity: pd.DataFrame
    processed: pd.DataFrame


def development_frame(factors, labels, calendar, config=DiscoveryConfig()):
    """Keep signal-only formation rows; mask labels crossing development end."""
    required = {
        "date",
        "symbol",
        "entry_date",
        "exit_date",
        "EXCESS_RET%d" % config.holding_days,
    }
    if not required.issubset(labels) or labels.duplicated(["date", "symbol"]).any():
        raise DiscoveryError(
            ErrorCode.INVALID_INPUT, "unique explicit entry/exit labels required"
        )
    if factors.duplicated(["date", "symbol"]).any():
        raise DiscoveryError(ErrorCode.INVALID_INPUT, "duplicate factor keys")
    start, end = pd.Timestamp(config.development_start), pd.Timestamp(
        config.development_end
    )
    sessions = pd.DatetimeIndex(calendar)
    names = [name for name in factors if name not in ("date", "symbol")]
    selected = factors.loc[
        factors["date"].between(start, end), ["date", "symbol"] + names
    ].copy()
    local = labels.loc[labels["date"].between(start, end), list(required)].copy()
    for name in ("date", "entry_date", "exit_date"):
        try:
            values = pd.DatetimeIndex(pd.to_datetime(local[name], errors="raise"))
        except (ValueError, TypeError) as exc:
            raise DiscoveryError(ErrorCode.INVALID_INPUT, "invalid label date") from exc
        if values.tz is not None or not values.equals(values.normalize()):
            raise DiscoveryError(
                ErrorCode.INVALID_INPUT, "label dates must be naive midnight"
            )
        local[name] = values
    target = "EXCESS_RET%d" % config.holding_days
    entry_map = pd.Series(sessions, index=sessions).shift(-1)
    exit_map = pd.Series(sessions, index=sessions).shift(-(config.holding_days + 1))
    actual = local["entry_date"].eq(local["date"].map(entry_map)) & local[
        "exit_date"
    ].eq(local["date"].map(exit_map))
    mature = (
        local["entry_date"].between(start, end)
        & local["exit_date"].between(start, end)
        & (local["entry_date"] > local["date"])
        & actual
    )
    local.loc[~mature, target] = np.nan
    local[target] = local[target].replace([np.inf, -np.inf], np.nan)
    return (
        selected.merge(local, on=["date", "symbol"], how="left", validate="one_to_one"),
        names,
    )


def evaluate_discovery(
    factors, labels, calendar, config=DiscoveryConfig(), deadline=None
):
    frame, names = development_frame(factors, labels, calendar, config)
    if frame.empty or not names:
        raise DiscoveryError(ErrorCode.INVALID_INPUT, "no development candidates")
    if (
        frame["date"].nunique() * len(names) * (config.quantiles + 1)
        > config.max_diagnostic_records
    ):
        raise DiscoveryError(
            ErrorCode.BUDGET_EXCEEDED, "evaluation diagnostic record limit"
        )

    def check_time():
        if deadline is not None and time.perf_counter() >= deadline:
            raise DiscoveryError(ErrorCode.BUDGET_EXCEEDED, "evaluation runtime budget")

    check_time()
    target = "EXCESS_RET%d" % config.holding_days
    rows = []
    for name in names:
        check_time()
        raw = frame[name]
        finite = pd.Series(np.isfinite(raw.to_numpy(dtype=float)), index=raw.index)
        count = int(finite.sum())
        # Outliers are described before clipping, using the same MAD convention.
        medians = raw.where(finite).groupby(frame["date"]).transform("median")
        deviations = (raw.where(finite) - medians).abs()
        mad = deviations.groupby(frame["date"]).transform("median")
        outliers = (
            finite & (mad > 0) & (deviations > config.mad_multiplier * 1.4826 * mad)
        )
        rows.append(
            {
                "candidate_id": name,
                "valid_count": count,
                "total_count": len(frame),
                "valid_ratio": count / len(frame),
                "missing_ratio": 1 - count / len(frame),
                "nonfinite_ratio": float((~finite & raw.notna()).mean()),
                "outlier_ratio": float(outliers.mean()),
                "evaluation_status": (
                    "EVALUATED"
                    if count / len(frame) >= config.minimum_candidate_coverage
                    else "INSUFFICIENT_COVERAGE"
                ),
            }
        )
    quality = pd.DataFrame(rows)
    processed = preprocess_cross_sections(
        frame,
        names,
        minimum_count=config.minimum_cross_section,
        mad_multiplier=config.mad_multiplier,
    )
    check_time()
    quality["processed_valid_ratio"] = [
        float(processed[name].notna().mean()) for name in names
    ]
    eligible = quality.loc[
        quality["evaluation_status"].eq("EVALUATED"), "candidate_id"
    ].tolist()
    if not eligible:
        return DiscoveryEvaluation(
            quality,
            pd.DataFrame(),
            pd.DataFrame(),
            pd.DataFrame(),
            pd.DataFrame(),
            processed,
        )
    dates = pd.DatetimeIndex(calendar)
    dates = dates[
        (dates >= config.development_start) & (dates <= config.development_end)
    ]
    evaluation = evaluate_factors(
        processed,
        eligible,
        target=target,
        quantiles=config.quantiles,
        minimum_cross_section=config.minimum_cross_section,
        calendar=dates,
    )
    check_time()
    # Diagnostic complete-label-only grouping, never used for the signal groups.
    complete = processed.loc[processed[target].notna()].copy()
    if not complete.empty:
        sensitivity = evaluate_factors(
            complete,
            eligible,
            target=target,
            quantiles=config.quantiles,
            minimum_cross_section=config.minimum_cross_section,
            calendar=dates,
        ).quantile_returns
        sensitivity = sensitivity.copy()
        sensitivity["diagnostic"] = "COMPLETE_LABEL_RESELECTION_NOT_SIGNAL_PORTFOLIO"
    else:
        sensitivity = pd.DataFrame()
    check_time()
    return DiscoveryEvaluation(
        quality,
        evaluation.daily_ic,
        evaluation.summary,
        evaluation.quantile_returns,
        sensitivity,
        processed,
    )
