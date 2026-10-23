"""Evaluation-only uncertainty for already-produced historical test scores.

The observational unit is a market trading day, not a stock-day. Missing days
stay on the supplied benchmark calendar. No model is fitted, ranked, selected,
or rescored here. Bartlett HAC and moving-block bootstrap need weak-dependence
and stability assumptions; they do not turn an unverified dataset into research.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import math
from typing import Any, Dict, Sequence

import numpy as np
import pandas as pd
from pandas.api.types import is_bool_dtype, is_complex_dtype, is_float_dtype, is_numeric_dtype
from scipy.stats import norm


MODEL_NAMES = ("factor_score", "ridge", "lightgbm", "xgboost")
INFERENCE_METRICS = ("prediction_rank_ic", "prediction_ic", "top_bottom_spread")
PRIMARY_FAMILY_SIZE = 3


class RobustnessError(ValueError):
    """Unsafe inputs or an invalid pre-declared inference protocol."""


@dataclass(frozen=True)
class RobustnessOptions:
    confidence_level: float = 0.95
    hac_lags: int = 20
    bootstrap_block_days: int = 20
    bootstrap_repetitions: int = 2000
    min_valid_days: int = 60
    min_coverage: float = 0.8
    holding_days: int = 5
    random_seed: int = 42

    def __post_init__(self) -> None:
        for name in (
            "hac_lags", "bootstrap_block_days", "bootstrap_repetitions",
            "min_valid_days", "holding_days", "random_seed",
        ):
            value = getattr(self, name)
            if isinstance(value, (bool, np.bool_)) or not isinstance(value, (int, np.integer)):
                raise RobustnessError("%s must be an integer" % name)
            object.__setattr__(self, name, int(value))
        if self.holding_days < 1 or self.hac_lags < max(0, self.holding_days - 1):
            raise RobustnessError("hac_lags must cover holding_days - 1 overlapping lags")
        if self.bootstrap_block_days < self.holding_days:
            raise RobustnessError("bootstrap_block_days must be at least holding_days")
        if self.bootstrap_repetitions < 100:
            raise RobustnessError("bootstrap_repetitions must be at least 100")
        if self.min_valid_days < max(2, self.hac_lags + 1, 2 * self.bootstrap_block_days):
            raise RobustnessError("min_valid_days must exceed hac_lags and cover two blocks")
        if not 0 <= self.random_seed <= 2 ** 32 - 1:
            raise RobustnessError("random_seed must fit an unsigned 32-bit integer")
        for name in ("confidence_level", "min_coverage"):
            value = getattr(self, name)
            if isinstance(value, (bool, np.bool_)) or not isinstance(value, (int, float, np.number)):
                raise RobustnessError("%s must be a finite real number" % name)
            if np.iscomplexobj(value) or not math.isfinite(float(value)):
                raise RobustnessError("%s must be a finite real number" % name)
            object.__setattr__(self, name, float(value))
        if not 0 < self.confidence_level < 1 or not 0 < self.min_coverage <= 1:
            raise RobustnessError("confidence_level and min_coverage must be valid probabilities")


@dataclass(frozen=True)
class RobustnessEvaluation:
    daily: pd.DataFrame
    summary: pd.DataFrame
    audit: pd.DataFrame
    protocol: Dict[str, Any]
    evidence: Dict[str, str]


def inference_protocol(options: RobustnessOptions) -> Dict[str, Any]:
    """Fixed hypothesis family; this is not a model-selection policy."""
    if not isinstance(options, RobustnessOptions):
        raise RobustnessError("options must be RobustnessOptions")
    return {
        "schema_version": 1,
        "stage": "HISTORICAL_TEST",
        "unit": "market_trading_day",
        "baseline": "factor_score",
        "models": list(MODEL_NAMES),
        "metrics": list(INFERENCE_METRICS),
        "primary_metric": "prediction_rank_ic",
        "primary_scope": "OVERALL",
        "primary_contrast": "PAIRED_MINUS_BASELINE",
        "primary_family_size": PRIMARY_FAMILY_SIZE,
        "alternative": "two_sided",
        "adjustment": "holm_fixed_family",
        "hac_kernel": "bartlett",
        "hac_finite_sample_correction": False,
        "bootstrap": "non_circular_overlapping_moving_blocks_percentile",
        "bootstrap_min_valid_fraction": 0.95,
        "missing_policy": "preserve_calendar_slots_no_imputation",
        "automatic_model_selection": False,
        "options": asdict(options),
    }


def _real_values(values: Any, name: str) -> np.ndarray:
    try:
        series = pd.Series(values)
    except (ValueError, TypeError) as exc:
        raise RobustnessError("%s must be a one-dimensional numeric sequence" % name) from exc
    if (not is_numeric_dtype(series.dtype) or is_bool_dtype(series.dtype)
            or is_complex_dtype(series.dtype)):
        raise RobustnessError("%s must be real numeric values, with NaN allowed" % name)
    array = series.to_numpy(dtype=float, na_value=np.nan)
    if np.isinf(array).any():
        raise RobustnessError("%s must not contain infinity" % name)
    return array


def holm_adjust(p_values: Sequence[float], family_size: int = PRIMARY_FAMILY_SIZE) -> np.ndarray:
    """Holm adjusted p-values without shrinking a family when tests are missing."""
    p = _real_values(p_values, "p_values")
    if (isinstance(family_size, (bool, np.bool_))
            or not isinstance(family_size, (int, np.integer)) or family_size < len(p)):
        raise RobustnessError("family_size must be an integer at least the number of tests")
    valid = np.flatnonzero(np.isfinite(p))
    if ((p[valid] < 0) | (p[valid] > 1)).any():
        raise RobustnessError("p_values must be between zero and one")
    order = valid[np.argsort(p[valid], kind="stable")]
    adjusted = np.full(len(p), np.nan)
    adjusted[order] = np.minimum(1.0, np.maximum.accumulate(
        p[order] * (family_size - np.arange(len(order)))
    ))
    return adjusted


def _child_seed(seed: int, identity: str) -> int:
    digest = hashlib.sha256((str(seed) + "\n" + identity).encode("utf-8")).digest()
    return int.from_bytes(digest[:4], byteorder="big")


def _bootstrap_means(values: np.ndarray, options: RobustnessOptions, seed: int) -> np.ndarray:
    """Sample whole calendar blocks, including missing slots, without wrapping."""
    total = len(values)
    block = options.bootstrap_block_days
    rng = np.random.default_rng(seed)
    blocks = int(math.ceil(total / block))
    offsets = np.arange(block)
    means = np.full(options.bootstrap_repetitions, np.nan)
    for first in range(0, len(means), 128):
        count = min(128, len(means) - first)
        starts = rng.integers(0, total - block + 1, size=(count, blocks))
        indices = (starts[:, :, None] + offsets).reshape(count, -1)[:, :total]
        samples = values[indices]
        observed = np.isfinite(samples)
        denominators = observed.sum(axis=1)
        numerators = np.where(observed, samples, 0.0).sum(axis=1)
        np.divide(numerators, denominators, out=means[first:first + count], where=denominators > 0)
    return means


def mean_uncertainty(
    values: Sequence[float], *, signal_dates: int, options: RobustnessOptions,
    identity: str = "mean",
) -> Dict[str, Any]:
    """Mean, HAC normal CI/p-value and percentile block-bootstrap CI.

    ``values`` occupies consecutive benchmark trading-day slots, with NaN for
    missing metrics or excluded subgroup days. ``signal_dates`` is the count
    of scheduled signal dates belonging to this scope, not all calendar slots.
    Zero residual contribution for an absent observation is a sandwich score,
    NOT an imputed zero IC/return. Inference is conditional on observed coverage.
    """
    if not isinstance(options, RobustnessOptions):
        raise RobustnessError("options must be RobustnessOptions")
    array = _real_values(values, "values")
    if (isinstance(signal_dates, (bool, np.bool_))
            or not isinstance(signal_dates, (int, np.integer))
            or not 0 <= signal_dates <= len(array)):
        raise RobustnessError("signal_dates must be an integer within calendar slots")
    if not isinstance(identity, str) or not identity:
        raise RobustnessError("identity must be a nonempty string")
    observed = np.isfinite(array)
    count = int(observed.sum())
    if count > signal_dates:
        raise RobustnessError("valid observations exceed scheduled signal_dates")
    mean = float(array[observed].mean()) if count else np.nan
    if count and not math.isfinite(mean):
        raise RobustnessError("non-finite arithmetic in the sample mean")
    coverage = count / signal_dates if signal_dates else np.nan
    seed = _child_seed(options.random_seed, identity)
    result = {
        "calendar_slots": len(array), "signal_dates": int(signal_dates),
        "valid_dates": count, "missing_metric_dates": int(signal_dates) - count,
        "coverage": coverage, "mean": mean, "status": "OK",
        "hac_standard_error": np.nan, "hac_statistic": np.nan,
        "hac_p_value": np.nan, "hac_ci_lower": np.nan, "hac_ci_upper": np.nan,
        "bootstrap_status": "NOT_RUN", "bootstrap_valid_repetitions": 0,
        "bootstrap_ci_lower": np.nan, "bootstrap_ci_upper": np.nan,
        "bootstrap_seed": seed,
    }
    if count < options.min_valid_days:
        result["status"] = "INSUFFICIENT_DATES"
        return result
    if coverage < options.min_coverage:
        result["status"] = "INSUFFICIENT_COVERAGE"
        return result
    residual = np.zeros(len(array))
    residual[observed] = array[observed] - mean
    base = float(residual @ residual)
    if not math.isfinite(base):
        raise RobustnessError("non-finite arithmetic in the HAC covariance")
    if base == 0.0 or np.ptp(array[observed]) == 0.0:
        result["status"] = "DEGENERATE_VARIANCE"
        return result
    variance_sum = base
    for lag in range(1, options.hac_lags + 1):
        variance_sum += 2.0 * (1.0 - lag / (options.hac_lags + 1.0)) * float(
            residual[lag:] @ residual[:-lag]
        )
    if not math.isfinite(variance_sum):
        raise RobustnessError("non-finite arithmetic in the HAC covariance")
    tolerance = np.finfo(float).eps * base * (2 * options.hac_lags + 1) * 16
    if variance_sum < -tolerance:
        raise RobustnessError("Bartlett HAC covariance is not positive semidefinite")
    if variance_sum <= tolerance:
        result["status"] = "DEGENERATE_VARIANCE"
        return result
    se = math.sqrt(variance_sum) / count
    statistic = mean / se
    if not math.isfinite(statistic):
        raise RobustnessError("non-finite arithmetic in the HAC statistic")
    # isf avoids rounding (1 + confidence) / 2 to one near confidence=1.
    critical = float(norm.isf((1.0 - options.confidence_level) / 2.0))
    result.update(
        hac_standard_error=se, hac_statistic=statistic,
        hac_p_value=float(2.0 * norm.sf(abs(statistic))),
        hac_ci_lower=mean - critical * se, hac_ci_upper=mean + critical * se,
    )
    bootstrap = _bootstrap_means(array, options, seed)
    valid_bootstrap = bootstrap[np.isfinite(bootstrap)]
    result["bootstrap_valid_repetitions"] = int(len(valid_bootstrap))
    if len(valid_bootstrap) < math.ceil(options.bootstrap_repetitions * 0.95):
        result["bootstrap_status"] = "INSUFFICIENT_VALID_REPLICATES"
    else:
        tail = (1.0 - options.confidence_level) / 2.0
        lower, upper = np.quantile(valid_bootstrap, [tail, 1.0 - tail], method="linear")
        result.update(bootstrap_status="OK", bootstrap_ci_lower=float(lower), bootstrap_ci_upper=float(upper))
    return result


def _dates(values: Any, name: str) -> pd.DatetimeIndex:
    raw = pd.Index(values)
    if is_numeric_dtype(raw.dtype) or is_bool_dtype(raw.dtype):
        raise RobustnessError("%s must contain date-only values" % name)
    try:
        dates = pd.DatetimeIndex(pd.to_datetime(raw, errors="raise"))
        if dates.hasnans or dates.tz is not None or not dates.equals(dates.normalize()):
            raise ValueError("not date-only")
        return dates
    except (ValueError, TypeError, AttributeError) as exc:
        raise RobustnessError("%s must contain complete tz-naive trading dates" % name) from exc


def _frame_hash(frame: pd.DataFrame) -> str:
    return hashlib.sha256(frame.to_csv(
        index=False, date_format="%Y-%m-%d", float_format="%.17g", lineterminator="\n",
    ).encode("utf-8")).hexdigest()


def validate_robustness_evaluation(result: RobustnessEvaluation) -> None:
    """Check a produced result has not changed before reporting; no recomputation."""
    if not isinstance(result, RobustnessEvaluation):
        raise RobustnessError("robustness must be a RobustnessEvaluation")
    try:
        options = RobustnessOptions(**result.protocol["options"])
        if result.protocol != inference_protocol(options):
            raise RobustnessError("unknown inference protocol")
        tables = {"daily": result.daily, "summary": result.summary, "audit": result.audit}
        expected = {
            name + "_sha256": _frame_hash(table) for name, table in tables.items()
            if isinstance(table, pd.DataFrame) and not table.empty and not table.columns.has_duplicates
        }
        expected["protocol_sha256"] = hashlib.sha256(json.dumps(
            result.protocol, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")).hexdigest()
        if len(expected) != 4 or expected != result.evidence:
            raise RobustnessError("inference tables/protocol differ from produced evidence")
        primary = result.summary.loc[result.summary["primary_hypothesis"]]
        if (len(primary) != PRIMARY_FAMILY_SIZE or set(primary["model_name"]) != set(MODEL_NAMES[1:])
                or not primary["scope"].eq("OVERALL").all()
                or not primary["metric"].eq("prediction_rank_ic").all()
                or not primary["contrast"].eq("PAIRED_MINUS_BASELINE").all()):
            raise RobustnessError("inference primary family differs from fixed protocol")
    except RobustnessError:
        raise
    except Exception as exc:
        raise RobustnessError("invalid produced inference evidence: %s" % exc) from exc


def evaluate_model_robustness(
    daily: pd.DataFrame, calendar: pd.DatetimeIndex,
    options: RobustnessOptions = RobustnessOptions(),
) -> RobustnessEvaluation:
    """Compare the four fixed models on identical historical test dates only.

    Calendar ownership/authenticity must be established by the caller. The
    evaluator validates alignment, not the historical source. It never infers
    trading days from business-day frequencies or a compacted score sequence.
    Only the three OVERALL RankIC deltas constitute the primary Holm family;
    other metrics/scopes and all individual CIs are exploratory/unadjusted.
    """
    if not isinstance(options, RobustnessOptions):
        raise RobustnessError("options must be RobustnessOptions")
    if not isinstance(daily, pd.DataFrame) or daily.columns.has_duplicates or daily.empty:
        raise RobustnessError("daily must be a nonempty DataFrame with unique columns")
    required = {"date", "fold", "stage", "model_name", *INFERENCE_METRICS}
    if not required.issubset(daily):
        raise RobustnessError("daily is missing columns: %s" % sorted(required.difference(daily)))
    grid = _dates(calendar, "calendar")
    if grid.empty or grid.has_duplicates or not grid.is_monotonic_increasing:
        raise RobustnessError("calendar must be nonempty, unique and increasing")
    fields = ["date", "fold", "stage", "model_name"]
    if "regime" in daily:
        fields.append("regime")
    frame = daily.loc[:, fields + list(INFERENCE_METRICS)].copy()
    frame["date"] = _dates(frame["date"], "daily.date")
    if not frame["date"].isin(grid).all():
        raise RobustnessError("daily dates are absent from the supplied benchmark calendar")
    if frame["stage"].isna().any() or not frame["stage"].eq("HISTORICAL_TEST").all():
        raise RobustnessError("inference accepts HISTORICAL_TEST only; Train/Validation are not independent test")
    if (not frame["model_name"].isin(MODEL_NAMES).all() or set(frame["model_name"]) != set(MODEL_NAMES)):
        raise RobustnessError("daily must contain exactly the four fixed model names")
    if (not is_numeric_dtype(frame["fold"].dtype) or is_bool_dtype(frame["fold"].dtype)
            or is_complex_dtype(frame["fold"].dtype) or frame["fold"].isna().any()
            or not np.isfinite(frame["fold"].to_numpy(dtype=float)).all() or (frame["fold"] < 0).any()
            or (is_float_dtype(frame["fold"].dtype) and frame["fold"].ge(float(2 ** 63)).any())
            or frame["fold"].gt(np.iinfo(np.int64).max).any()
            or not frame["fold"].eq(frame["fold"].astype(int)).all()):
        raise RobustnessError("fold must contain non-negative integers")
    frame["fold"] = frame["fold"].astype("int64")
    if frame.duplicated(["date", "model_name"]).any():
        raise RobustnessError("duplicate model/date keys or overlapping test folds")
    has_regime = "regime" in frame
    if has_regime and not frame["regime"].map(lambda value: isinstance(value, str) and bool(value.strip())).all():
        raise RobustnessError("regime must contain nonempty strings when supplied")
    for metric in INFERENCE_METRICS:
        frame[metric] = _real_values(frame[metric], metric)
        if metric != "top_bottom_spread" and frame[metric].abs().gt(1.0 + 1e-12).any():
            raise RobustnessError("IC values must lie between -1 and one")
    cohort_columns = ["date", "fold"] + (["regime"] if has_regime else [])
    cohorts = {
        model: frame.loc[frame["model_name"].eq(model), cohort_columns].sort_values("date").reset_index(drop=True)
        for model in MODEL_NAMES
    }
    cohort = cohorts[MODEL_NAMES[0]]
    if any(not cohort.equals(cohorts[model]) for model in MODEL_NAMES[1:]):
        raise RobustnessError("models must share identical test date/fold/regime cohorts")
    frame = frame.sort_values(["date", "model_name"]).reset_index(drop=True)
    index = pd.DatetimeIndex(cohort["date"])
    scopes = [("OVERALL", "ALL", np.ones(len(cohort), dtype=bool))]
    for scope, values in (
        ("YEAR", index.strftime("%Y")), ("QUARTER", index.to_period("Q").astype(str)),
        ("FOLD", cohort["fold"].astype(str).to_numpy()),
    ):
        for period in sorted(set(values)):
            scopes.append((scope, str(period), np.asarray(values == period)))
    if has_regime:
        for period in sorted(cohort["regime"].unique()):
            scopes.append(("REGIME", period, cohort["regime"].eq(period).to_numpy()))
    rows = []
    audit_rows = []
    for scope, period, mask in scopes:
        dates = index[mask]
        slots = grid[(grid >= dates.min()) & (grid <= dates.max())]
        for metric in INFERENCE_METRICS:
            wide = frame.pivot(index="date", columns="model_name", values=metric)
            wide = wide.reindex(index=slots, columns=list(MODEL_NAMES))
            wide.loc[~wide.index.isin(dates), :] = np.nan
            for contrast, model in (
                [("MODEL_MEAN", name) for name in MODEL_NAMES]
                + [("PAIRED_MINUS_BASELINE", name) for name in MODEL_NAMES[1:]]
            ):
                values = wide[model].to_numpy(dtype=float)
                if contrast == "PAIRED_MINUS_BASELINE":
                    values = values - wide["factor_score"].to_numpy(dtype=float)
                identity = json.dumps([scope, period, metric, contrast, model], separators=(",", ":"))
                estimate = mean_uncertainty(values, signal_dates=len(dates), options=options, identity=identity)
                primary = scope == "OVERALL" and metric == "prediction_rank_ic" and contrast == "PAIRED_MINUS_BASELINE"
                key = {"scope": scope, "period": period, "metric": metric, "contrast": contrast, "model_name": model}
                rows.append({
                    **key, **estimate, "baseline": "factor_score" if contrast == "PAIRED_MINUS_BASELINE" else None,
                    "primary_hypothesis": primary, "holm_adjusted_p_value": np.nan,
                    "holm_reject": None, "confidence_level": options.confidence_level,
                    "hac_lags": options.hac_lags, "bootstrap_block_days": options.bootstrap_block_days,
                    "bootstrap_repetitions": options.bootstrap_repetitions,
                })
                observed_values = pd.DataFrame({"date": slots, "value": values})
                audit_rows.append({
                    **key, "stage": "HISTORICAL_TEST", "selection_usage": "evaluation_only",
                    "start": slots.min(), "end": slots.max(),
                    "calendar_sha256": _frame_hash(pd.DataFrame({"date": slots})),
                    "scope_signal_sha256": _frame_hash(pd.DataFrame({"date": dates})),
                    "observed_metric_sha256": _frame_hash(observed_values),
                    "bootstrap_seed": estimate["bootstrap_seed"],
                    "inference_assumptions": "conditional_observed_coverage_weak_dependence_stability",
                })
    summary = pd.DataFrame(rows)
    primary_rows = summary.index[summary["primary_hypothesis"]]
    adjusted = holm_adjust(summary.loc[primary_rows, "hac_p_value"], PRIMARY_FAMILY_SIZE)
    summary.loc[primary_rows, "holm_adjusted_p_value"] = adjusted
    for row, value in zip(primary_rows, adjusted):
        if np.isfinite(value):
            summary.at[row, "holm_reject"] = bool(value <= 1.0 - options.confidence_level)
    summary["holm_reject"] = summary["holm_reject"].astype("boolean")
    audit = pd.DataFrame(audit_rows)
    protocol = inference_protocol(options)
    evidence = {
        name + "_sha256": _frame_hash(table)
        for name, table in (("daily", frame), ("summary", summary), ("audit", audit))
    }
    evidence["protocol_sha256"] = hashlib.sha256(json.dumps(
        protocol, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    return RobustnessEvaluation(frame, summary, audit, protocol, evidence)


__all__ = [
    "RobustnessError", "RobustnessEvaluation", "RobustnessOptions",
    "evaluate_model_robustness", "holm_adjust", "inference_protocol", "mean_uncertainty",
    "validate_robustness_evaluation",
]
