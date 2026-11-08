"""Typed research evaluation; no fitting, selection, or execution simulation."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
import math
from typing import Any, Mapping

import numpy as np
import pandas as pd

from factorlab.backtest.fees import FeeSchedule
from factorlab.dataset.identity import frame_hash
from factorlab.metrics.factor import evaluate_factors
from factorlab.metrics.portfolio import calculate_portfolio_metrics
from factorlab.metrics.prediction import evaluate_predictions
from factorlab.metrics.robustness import RobustnessOptions, holm_adjust, mean_uncertainty
from factorlab.validation.ablation_statistics import summarize_ablation


EVIDENCE = (
    "NO LIVE MONEY", "PIT_UNVERIFIED", "HFQ_UNVERIFIED", "FORMAL_ADMISSION_BLOCKED",
)


@dataclass(frozen=True)
class EvaluationContext:
    """Caller-declared identities and estimand; identities do not certify admission.

    ``calendar`` is the complete, ordered trading calendar of the test window.
    ``risk_free_rate`` is an effective annual rate; ICIR remains unannualized.
    Different benchmarks/horizons require separate comparison groups.
    """

    dataset_id: str
    universe_id: str
    label: str
    label_id: str
    benchmark_id: str
    calendar: tuple[str, ...]
    holding_days: int
    frequency: str = "daily"
    periods_per_year: int = 252
    risk_free_rate: float = 0.0
    source_kind: str = "SYNTHETIC"
    stage: str = "HISTORICAL_TEST"

    def __post_init__(self):
        for name in ("dataset_id", "universe_id", "label", "label_id", "benchmark_id", "frequency"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name).strip():
                raise ValueError(name + " must be nonempty")
        if self.stage != "HISTORICAL_TEST":
            raise ValueError("only HISTORICAL_TEST results are eligible")
        if self.source_kind not in {"SYNTHETIC", "REAL_CANDIDATE"}:
            raise ValueError("source_kind must be SYNTHETIC or REAL_CANDIDATE")
        for name in ("holding_days", "periods_per_year"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(name + " must be a positive integer")
        if not math.isfinite(self.risk_free_rate) or self.risk_free_rate <= -1:
            raise ValueError("invalid effective annual risk-free rate")
        dates = _dates(pd.Series(self.calendar))
        if dates.empty or dates.duplicated().any() or not dates.is_monotonic_increasing:
            raise ValueError("calendar must be nonempty, ordered and unique")
        object.__setattr__(self, "calendar", tuple(d.strftime("%Y-%m-%d") for d in dates))


@dataclass(frozen=True)
class MetricValue:
    value: float | None
    status: str
    reason: str = ""


def _metric(value, reason="undefined or insufficient observations"):
    return (MetricValue(float(value), "OK") if pd.notna(value) and math.isfinite(float(value))
            else MetricValue(None, "NOT_COMPUTABLE", reason))


def _dates(values):
    dates = pd.to_datetime(values, errors="raise")
    if dates.isna().any() or dates.dt.tz is not None:
        raise ValueError("dates must be nonmissing timezone-naive session dates")
    if not dates.eq(dates.dt.normalize()).all():
        raise ValueError("dates must be normalized session dates")
    return dates


def _finite(frame, columns, *, missing=False):
    for name in columns:
        if name not in frame:
            raise ValueError("missing column: " + name)
        values = frame[name]
        if (not pd.api.types.is_numeric_dtype(values) or pd.api.types.is_bool_dtype(values)
                or pd.api.types.is_complex_dtype(values)):
            raise ValueError(name + " must be real numeric")
        array = values.to_numpy(dtype=float)
        if np.isinf(array).any() or (not missing and np.isnan(array).any()):
            raise ValueError(name + " contains invalid observations")


@dataclass(frozen=True)
class EvaluationResult:
    """Versioned tables and receipts, serializable without a report dependency."""

    name: str
    kind: str
    context: EvaluationContext
    metrics: Mapping[str, MetricValue]
    tables: Mapping[str, pd.DataFrame]
    receipts: Mapping[str, str]
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self):
        """Return strict JSON-compatible data, preserving undefined values as null."""
        tables = {k: json.loads(v.to_json(orient="table", date_format="iso"))
                  for k, v in self.tables.items()}
        return {"schema_version": 1, "name": self.name, "kind": self.kind,
                "context": asdict(self.context),
                "metrics": {k: asdict(v) for k, v in self.metrics.items()},
                "tables": tables, "receipts": dict(self.receipts),
                "metadata": dict(self.metadata), "evidence": list(EVIDENCE),
                "financial_evidence": False}


class ResearchEvaluation:
    """Evaluate completed research artifacts under a caller-declared context."""

    def __init__(self, context: EvaluationContext):
        self.context = context

    def _sample(self, frame, keys):
        frame = frame.copy()
        if frame.columns.duplicated().any():
            raise ValueError("duplicate columns")
        frame["date"] = _dates(frame["date"])
        if frame.empty or frame.duplicated(keys).any():
            raise ValueError("nonempty unique sample required")
        if tuple(sorted(frame.date.dt.strftime("%Y-%m-%d").unique())) != self.context.calendar:
            raise ValueError("sample dates must exactly cover the evaluation calendar")
        return frame.sort_values(keys).reset_index(drop=True)

    def predictions(self, name, frame, *, folds, top_k=20, minimum_cross_section=2):
        """Evaluate OOS scores with explicit exclusive train/validation/test bounds.

        ``folds`` requires fold, train_end, validation_start, validation_end,
        test_start and test_end. End bounds are exclusive. Label maturity must
        be proved by label_end on the fold receipt (no later than train_end).
        """
        sample = self._sample(frame, ["date", "symbol"])
        if sample.symbol.isna().any():
            raise ValueError("missing security identity")
        required = {"fold", "train_end", "validation_start", "validation_end", "test_start", "test_end", "label_end"}
        if not required.issubset(folds) or folds.empty or folds.fold.duplicated().any():
            raise ValueError("unique complete fold receipts required")
        if set(sample.fold) != set(folds.fold):
            raise ValueError("prediction and fold identities differ")
        bounds = folds.copy()
        for column in required - {"fold"}:
            bounds[column] = _dates(bounds[column])
        for row in bounds.itertuples():
            if not (row.label_end <= row.train_end <= row.validation_start
                    < row.validation_end <= row.test_start < row.test_end):
                raise ValueError("unsafe train/validation/test boundaries or immature training labels")
            dates = sample.loc[sample.fold.eq(row.fold), "date"]
            if not dates.between(row.test_start, row.test_end, inclusive="left").all():
                raise ValueError("prediction outside its test window")
        result = evaluate_predictions(sample, target_col=self.context.label,
            top_k=top_k, minimum_cross_section=minimum_cross_section, fold_col="fold")
        overall = result.overall.iloc[0]
        metrics = {k: _metric(overall[k]) for k in (
            "prediction_rank_ic_mean", "prediction_rank_icir", "prediction_ic_mean", "prediction_icir")}
        identity = sample[["date", "symbol", "fold", self.context.label]]
        receipts = {"observations": frame_hash(identity), "folds": frame_hash(bounds),
                    "finite_pairs": frame_hash(sample.loc[sample.score.notna() & sample[self.context.label].notna(), ["date", "symbol"]])}
        return EvaluationResult(name, "prediction", self.context, metrics,
            {"daily": result.daily.copy(), "summary": result.summary.copy()}, receipts,
            {"top_k": top_k, "minimum_cross_section": minimum_cross_section})

    def walk_forward(self, name, result, *, folds, top_k=20, minimum_cross_section=2):
        """Adapt a completed WalkForwardResult; never fit or reselect a model.

        Explicit fold receipts are required even for legacy results whose audit
        metadata does not carry a complete dataset or label-maturity identity.
        """
        return self.predictions(name, result.predictions, folds=folds,
            top_k=top_k, minimum_cross_section=minimum_cross_section)

    def factors(self, name, frame, factor_names, *, minimum_cross_section=20, quantiles=5):
        """Evaluate factor diagnostics, not executable returns or fitted models."""
        sample = self._sample(frame, ["date", "symbol"])
        factor_names = list(factor_names)
        result = evaluate_factors(sample, factor_names, target=self.context.label,
            minimum_cross_section=minimum_cross_section, quantiles=quantiles,
            calendar=pd.to_datetime(self.context.calendar))
        metrics = {str(row["scope"]) + ":" + str(row["factor"]) + ":" + key: _metric(row[key])
                   for row in result.summary.to_dict("records")
                   for key in ("IC Mean", "ICIR", "RankIC Mean", "RankICIR")}
        return EvaluationResult(name, "factor", self.context, metrics,
            {key: getattr(result, key).copy() for key in (
                "daily_ic", "summary", "quantile_returns", "autocorrelation", "turnover", "correlation")},
            {"observations": frame_hash(sample[["date", "symbol", self.context.label]]),
             **{"finite_pairs:" + factor: frame_hash(sample.loc[
                 np.isfinite(sample[factor]) & np.isfinite(sample[self.context.label]),
                 ["date", "symbol"]]) for factor in factor_names}},
            {"factors": list(factor_names), "minimum_cross_section": minimum_cross_section,
             "quantiles": quantiles})

    def benchmark(self, name, nav):
        """Evaluate a benchmark-only NAV using the same portfolio conventions."""
        return self.portfolio(name, nav, benchmark_only=True)

    def portfolio(self, name, nav, *, trades=None, positions=None, benchmark_only=False):
        """Evaluate full-window normalized NAV and existing execution receipts."""
        sample = self._sample(nav, ["date"])
        columns = ["strategy_nav", "benchmark_nav", "strategy_return", "benchmark_return"]
        _finite(sample, columns)
        for prefix in ("strategy", "benchmark"):
            returns = sample[prefix + "_return"].to_numpy()
            if (returns <= -1).any() or not np.allclose(
                    np.cumprod(1 + returns), sample[prefix + "_nav"], rtol=1e-9, atol=1e-12):
                raise ValueError("NAV must reconcile to returns from normalized origin 1")
        if benchmark_only and not np.array_equal(sample.strategy_return, sample.benchmark_return):
            raise ValueError("benchmark-only results must use benchmark returns")
        for column in ("transaction_cost", "turnover"):
            if column in sample:
                _finite(sample, [column])
                if (sample[column] < 0).any():
                    raise ValueError(column + " must be nonnegative")
        tables = {"daily": sample.copy()}
        if trades is not None:
            trades = trades.copy()
            _finite(trades, ["trade_notional", "transaction_cost"])
            if (trades[["trade_notional", "transaction_cost"]] < 0).any().any():
                raise ValueError("negative execution receipts")
            dates = _dates(trades.execution_date)
            if not dates.isin(sample.date).all():
                raise ValueError("trade outside evaluation window")
            if "transaction_cost" in sample:
                costs = trades.assign(date=dates).groupby("date").transaction_cost.sum()
                if not np.allclose(costs.reindex(sample.date, fill_value=0), sample.transaction_cost):
                    raise ValueError("trade and NAV costs disagree")
            tables["trades"] = trades
        raw = calculate_portfolio_metrics(sample, trades, positions,
            self.context.periods_per_year, self.context.risk_free_rate)
        metrics = {k: _metric(v) for k, v in raw.items()}
        for metric, column in (("total_transaction_cost", "transaction_cost"),
                               ("portfolio_turnover", "turnover")):
            if column not in sample:
                metrics[metric] = MetricValue(None, "NOT_COMPUTABLE", "missing " + column + " receipt")
        if trades is None:
            metrics["number_of_trades"] = MetricValue(None, "NOT_COMPUTABLE", "missing trade ledger")
        if "holdings" not in sample and positions is None:
            metrics["average_holdings"] = MetricValue(None, "NOT_COMPUTABLE", "missing positions")
        receipts = {"benchmark": frame_hash(sample[["date", "benchmark_return", "benchmark_nav"]])}
        return EvaluationResult(name, "benchmark" if benchmark_only else "portfolio",
            self.context, metrics, tables, receipts)

    def ablation(self, name, daily):
        """Recompute existing receipt-based descriptive factorial comparisons."""
        sample = self._sample(daily, ["model", "group", "fold", "date"])
        metrics, contrasts, volatility = summarize_ablation(sample)
        return EvaluationResult(name, "ablation", self.context, {},
            {"daily": sample, "summary": metrics, "contrasts": contrasts, "volatility": volatility},
            {"daily": frame_hash(sample)}, {"inference": "descriptive_only"})


def _compatible(left, right):
    layers = {"benchmark": "portfolio"}
    if (left.context != right.context
            or layers.get(left.kind, left.kind) != layers.get(right.kind, right.kind)):
        raise ValueError("incompatible research context or result layer")
    if left.receipts != right.receipts or left.metadata != right.metadata:
        raise ValueError("sample, benchmark, folds or evaluation settings differ")


def compare_results(results):
    """Compare strictly identical estimands without ranking or selecting a winner."""
    results = list(results)
    if len(results) < 2 or len({r.name for r in results}) != len(results):
        raise ValueError("at least two uniquely named results required")
    for result in results[1:]:
        _compatible(results[0], result)
    return pd.DataFrame([{"name": r.name, "metric": k, **asdict(v)}
                         for r in results for k, v in r.metrics.items()])


def paired_inference(results, *, baseline, metric, options: RobustnessOptions):
    """Two-sided HAC/block-bootstrap inference on predeclared daily contrasts.

    Holm's family is every supplied non-baseline arm, including unavailable
    tests. No stock-day pooling or fold-level independence assumption is used.
    """
    results = list(results)
    compare_results(results)
    by_name = {r.name: r for r in results}
    reference = by_name[baseline]
    if reference.kind not in {"prediction", "portfolio", "benchmark"}:
        raise ValueError("paired inference requires prediction or portfolio daily metrics")
    allowed = ({"prediction_rank_ic", "prediction_ic", "top_bottom_spread"}
               if reference.kind == "prediction" else {"strategy_return"})
    if metric not in allowed or options.holding_days != reference.context.holding_days:
        raise ValueError("metric or inference holding horizon mismatch")
    calendar = pd.to_datetime(reference.context.calendar)
    def values(result):
        daily = result.tables["daily"]
        if daily.date.duplicated().any():
            raise ValueError("one observation per trading day required")
        return daily.set_index("date")[metric].reindex(calendar).to_numpy(dtype=float)
    base = values(reference)
    rows = []
    for result in results:
        if result.name == baseline:
            continue
        row = mean_uncertainty(values(result) - base, signal_dates=len(calendar),
            options=options, identity=baseline + ":" + result.name + ":" + metric)
        rows.append({"name": result.name, "baseline": baseline, "metric": metric,
                     "null_hypothesis": "mean paired daily difference = 0", **row})
    adjusted = holm_adjust([r["hac_p_value"] for r in rows], family_size=len(rows))
    for row, p in zip(rows, adjusted):
        row.update(holm_p_value=p, family_size=len(rows), unit="market_trading_day",
                   confidence_level=options.confidence_level,
                   bootstrap_block_days=options.bootstrap_block_days,
                   hac_lags=options.hac_lags, bootstrap_repetitions=options.bootstrap_repetitions,
                   random_seed=options.random_seed, min_valid_days=options.min_valid_days,
                   min_coverage=options.min_coverage, holding_days=options.holding_days,
                   adjustment="holm_fixed_family", alternative="two_sided",
                   bootstrap="non_circular_overlapping_moving_blocks_percentile",
                   start=calendar[0], end=calendar[-1])
    return pd.DataFrame(rows)


def reprice_costs(trades, scenarios: Mapping[str, FeeSchedule]):
    """Reprice actual fills only; returns cost receipts, never scenario NAV.

    Slippage is the fee schedule's explicit additive cost component. Recorded
    fills stay fixed, so changed cash feasibility or rejected orders are not
    simulated. Execution-constraint comparisons require separate actual runs.
    """
    required = {"execution_date", "side", "trade_notional", "transaction_cost", "executed_shares", "execution_price"}
    if not required.issubset(trades):
        raise ValueError("complete actual-fill ledger required")
    _finite(trades, list(required - {"execution_date", "side"}))
    dates = _dates(trades.execution_date)
    if not trades.side.isin(["BUY", "SELL", "buy", "sell"]).all():
        raise ValueError("side must be buy or sell")
    if (trades[["trade_notional", "transaction_cost", "executed_shares", "execution_price"]] < 0).any().any():
        raise ValueError("negative fill ledger values")
    if not np.allclose(trades.trade_notional, trades.executed_shares * trades.execution_price):
        raise ValueError("fill notional mismatch")
    if not scenarios:
        raise ValueError("explicit named cost scenarios required")
    rows = []
    for name, schedule in scenarios.items():
        for position, (row, day) in enumerate(zip(trades.itertuples(), dates)):
            fee = schedule.calculate(day, row.side, row.trade_notional)
            rows.append({"scenario": name, "fill": position, "execution_date": day,
                "scope": "FIXED_ACTUAL_FILLS_COST_ONLY", "recorded_cost": row.transaction_cost,
                "cost_delta": fee.total - row.transaction_cost, **fee.to_dict()})
    return pd.DataFrame(rows, columns=["scenario", "fill", "execution_date", "scope", "recorded_cost",
        "cost_delta", "commission", "stamp_duty", "transfer_fee", "side_fee", "slippage", "transaction_cost"])
