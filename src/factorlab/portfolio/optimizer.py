"""Label-aligned, long-only single-period portfolio optimization."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
import json
from typing import Mapping

import numpy as np
import pandas as pd
import scipy
from scipy.optimize import Bounds, LinearConstraint, linprog, minimize

from factorlab.errors import FactorLabError
from .weights import equal_weights


class OptimizationError(FactorLabError, ValueError):
    """Failure with a machine-readable status; no target portfolio is returned."""

    def __init__(self, status: str, message: str):
        self.status = status
        super().__init__(message)


def _invalid(message):
    raise OptimizationError("invalid_input", message)


@dataclass(frozen=True)
class OptimizerConfig:
    """Weights are NAV fractions; risk and expected returns use one common period."""

    objective: str = "minimum_variance"
    investment: float = 1.0
    max_weight: float = 1.0
    risk_aversion: float = 1.0
    shrinkage: float = 0.0
    industry_bounds: Mapping[str, tuple[float, float]] = field(default_factory=dict)
    industry_deviation: Mapping[str, float] = field(default_factory=dict)
    turnover_limit: float | None = None
    tolerance: float = 1e-8
    max_iterations: int = 1000
    return_period: str = "daily"


@dataclass(frozen=True)
class OptimizationInput:
    """Supply exactly one risk source, with timestamps no later than decision_time.

    expected_return_definition must describe explicitly supplied expected returns.
    Previous holdings may include securities outside the target universe (liquidations).
    """

    symbols: tuple[str, ...]
    decision_time: object
    returns: pd.DataFrame | None = None
    covariance: pd.DataFrame | None = None
    covariance_as_of: object | None = None
    scores: pd.Series | None = None
    expected_returns: pd.Series | None = None
    expected_return_definition: str | None = None
    industries: Mapping[str, str] | None = None
    benchmark_industries: Mapping[str, float] | None = None
    previous_weights: pd.Series | None = None
    minimum_weights: pd.Series | None = None


@dataclass(frozen=True)
class OptimizationResult:
    target_weights: pd.Series
    cash_weight: float
    estimated_variance: float
    estimated_volatility: float
    turnover: float | None
    industry_exposures: Mapping[str, float]
    constraint_checks: Mapping[str, bool]
    solver_status: Mapping[str, object]
    config_identity: str
    input_identity: str
    parameters: Mapping[str, object]


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False,
                                     separators=(",", ":")).encode()).hexdigest()


def _labels(index):
    return (not index.has_duplicates and all(isinstance(x, str) and x for x in index))


def _vector(series, names, label):
    if not isinstance(series, pd.Series) or not _labels(series.index) or set(series.index) != set(names):
        _invalid(f"{label} requires exactly the unique target symbols")
    try:
        result = series.reindex(names).to_numpy(dtype=float)
    except (ValueError, TypeError):
        _invalid(f"{label} must be numeric")
    if not np.isfinite(result).all():
        _invalid(f"{label} must be finite")
    return result


def _timestamp(value):
    try:
        timestamp = pd.Timestamp(value)
        if pd.isna(timestamp):
            _invalid("timestamp must not be missing")
        return timestamp.tz_localize("UTC") if timestamp.tzinfo is None else timestamp.tz_convert("UTC")
    except (TypeError, ValueError) as exc:
        raise OptimizationError("invalid_input", "invalid timestamp") from exc


class PortfolioOptimizer:
    """Solve or validate exact baseline weights without relaxing constraints."""

    def __init__(self, config: OptimizerConfig = OptimizerConfig()):
        self.config = config

    def optimize(self, data: OptimizationInput) -> OptimizationResult:
        c = self.config
        configuration = asdict(c)
        if c.objective not in {"equal", "score", "minimum_variance", "mean_variance"}:
            _invalid("unknown objective")
        numeric = [c.investment, c.max_weight, c.risk_aversion, c.shrinkage, c.tolerance]
        if not np.isfinite(numeric).all() or not (0 <= c.investment <= 1 and 0 < c.max_weight <= 1
                and c.risk_aversion > 0 and 0 <= c.shrinkage <= 1 and 0 < c.tolerance <= 1e-5):
            _invalid("invalid budget, cap, risk aversion, shrinkage or tolerance")
        if isinstance(c.max_iterations, bool) or not isinstance(c.max_iterations, int) or c.max_iterations < 1 or not c.return_period:
            _invalid("positive integer max_iterations and return_period required")
        if not all(isinstance(name, str) and name for name in data.symbols):
            _invalid("symbols must be nonempty strings")
        names = sorted(data.symbols)
        if not names or not _labels(pd.Index(names)):
            _invalid("nonempty unique string symbols required")
        n = len(names)
        decision = _timestamp(data.decision_time)
        if (data.returns is None) == (data.covariance is None):
            _invalid("supply exactly one of returns and covariance")
        if data.returns is not None:
            r = data.returns
            if not isinstance(r.index, pd.DatetimeIndex) or not r.index.is_unique or not r.index.is_monotonic_increasing or r.index.hasnans:
                _invalid("returns require strictly increasing unique timestamps")
            if len(r) < 2 or not _labels(r.columns) or set(r.columns) != set(names):
                _invalid("returns require two observations and exactly the target symbols")
            if _timestamp(r.index[-1]) > decision:
                _invalid("future returns are forbidden")
            try:
                observations = r.reindex(columns=names).to_numpy(dtype=float)
            except (TypeError, ValueError):
                _invalid("returns must be numeric")
            if not np.isfinite(observations).all():
                _invalid("returns must be complete and finite")
            matrix = np.atleast_2d(np.cov(observations, rowvar=False, ddof=1))
            risk_source = {"method": "sample", "dates": [str(_timestamp(x)) for x in r.index],
                           "observations": observations.tolist()}
        else:
            if data.covariance_as_of is None or _timestamp(data.covariance_as_of) > decision:
                _invalid("covariance_as_of must not exceed decision_time")
            frame = data.covariance
            if not _labels(frame.index) or set(frame.index) != set(names):
                _invalid("covariance rows must match target symbols")
            matrix = np.stack([_vector(frame.loc[name], names, "covariance") for name in names])
            risk_source = {"method": "supplied", "as_of": str(_timestamp(data.covariance_as_of)), "matrix": matrix.tolist()}
        if not np.isfinite(matrix).all():
            _invalid("covariance estimation produced nonfinite values")
        scale = max(float(np.max(np.abs(matrix))), np.finfo(float).tiny)
        numerical_tolerance = 1e-10 * scale
        if np.max(np.abs(matrix - matrix.T)) > numerical_tolerance:
            _invalid("covariance must be symmetric")
        matrix = (matrix + matrix.T) / 2
        eigenvalues, eigenvectors = np.linalg.eigh(matrix)
        if eigenvalues.min() < -numerical_tolerance:
            _invalid("covariance must be positive semidefinite")
        if eigenvalues.min() < 0:
            matrix = (eigenvectors * np.maximum(eigenvalues, 0)) @ eigenvectors.T
        matrix = (1 - c.shrinkage) * matrix + c.shrinkage * np.diag(np.diag(matrix))
        scores = None if data.scores is None else _vector(data.scores, names, "scores")
        mu = None if data.expected_returns is None else _vector(data.expected_returns, names, "expected_returns")
        if c.objective == "mean_variance" and (mu is None or not data.expected_return_definition):
            _invalid("mean_variance requires explicit expected_returns and their definition")
        if c.objective == "score" and (scores is None or (scores < 0).any() or scores.max() <= 0):
            _invalid("score objective requires nonnegative scores with positive total")
        previous = None
        exits = 0.0
        previous_record = None
        if data.previous_weights is not None:
            p = data.previous_weights
            if not _labels(p.index):
                _invalid("previous_weights require unique string symbols")
            values = _vector(p, sorted(p.index), "previous_weights")
            if (values < 0).any() or values.sum() > 1 + c.tolerance:
                _invalid("previous_weights must be long-only with sum <= 1")
            p = pd.Series(values, index=sorted(p.index))
            previous = p.reindex(names, fill_value=0).to_numpy(dtype=float)
            exits = float(p.loc[~p.index.isin(names)].sum())
            previous_record = dict(zip(sorted(p.index), values.tolist()))
        turnover_active = c.turnover_limit is not None
        if turnover_active and (previous is None or not np.isfinite(c.turnover_limit) or c.turnover_limit < 0):
            _invalid("turnover limit requires previous holdings and a nonnegative finite limit")
        minimum = np.zeros(n)
        minimum_record = None
        if data.minimum_weights is not None:
            floors = data.minimum_weights
            if not _labels(floors.index):
                _invalid("minimum_weights require unique string symbols")
            values = _vector(floors, sorted(floors.index), "minimum_weights")
            if (values < 0).any():
                _invalid("minimum_weights must be nonnegative")
            floors = pd.Series(values, index=sorted(floors.index))
            if (floors.loc[~floors.index.isin(names)] > c.tolerance).any():
                raise OptimizationError("infeasible", "locked holding is outside target universe")
            minimum = floors.reindex(names, fill_value=0).to_numpy(dtype=float)
            minimum_record = floors.to_dict()
            if (minimum > c.max_weight).any() or minimum.sum() > c.investment + c.tolerance:
                raise OptimizationError("infeasible", "locked holdings exceed budget or weight cap")
        dimensions = 2*n if turnover_active else n
        rows, limits, row_names = [], [], []
        industries = dict(data.industries or {})
        benchmark = dict(data.benchmark_industries or {})
        if any(not isinstance(key, str) or not key for key in benchmark):
            _invalid("benchmark industry names must be nonempty strings")
        if benchmark and (not np.isfinite(list(benchmark.values())).all()
                          or any(not 0 <= value <= 1 for value in benchmark.values())
                          or sum(benchmark.values()) > 1 + c.tolerance):
            _invalid("benchmark industry exposures must be finite NAV fractions with sum <= 1")
        requested = set(c.industry_bounds) | set(c.industry_deviation)
        if any(not isinstance(key, str) or not key for key in requested):
            _invalid("constraint industry names must be nonempty strings")
        if (requested or data.industries is not None) and (set(industries) != set(names) or any(not isinstance(x, str) or not x for x in industries.values())):
            _invalid("industry constraints require complete industry labels")
        for sector in sorted(requested):
            lower, upper = c.industry_bounds.get(sector, (0., 1.))
            if not np.isfinite([lower, upper]).all() or not 0 <= lower <= upper <= 1:
                _invalid("industry bounds must satisfy 0 <= lower <= upper <= 1")
            if sector in c.industry_deviation:
                delta = c.industry_deviation[sector]
                base = benchmark.get(sector)
                if base is None or not np.isfinite([base, delta]).all() or not 0 <= base <= 1 or delta < 0:
                    _invalid("industry deviation requires finite benchmark exposure and nonnegative deviation")
                lower, upper = max(lower, base-delta), min(upper, base+delta)
            exposure = np.array([float(industries.get(name) == sector) for name in names] + [0.]*(dimensions-n))
            rows.extend([exposure, -exposure]); limits.extend([upper, -lower])
            row_names.extend([f"industry:{sector}:upper", f"industry:{sector}:lower"])
        if turnover_active:
            for i in range(n):
                row = np.zeros(dimensions); row[i] = 1; row[n+i] = -1
                rows.extend([row, np.concatenate([-row[:n], row[n:]])])
                limits.extend([previous[i], -previous[i]])
                row_names.extend([f"trade:{i}:positive", f"trade:{i}:negative"])
            rows.append(np.r_[np.zeros(n), np.ones(n)])
            limits.append(c.turnover_limit-exits); row_names.append("turnover_aux")
        a = np.array(rows).reshape((-1, dimensions))
        b = np.array(limits)
        budget = np.r_[np.ones(n), np.zeros(dimensions-n)]
        bounds = [(float(lower), c.max_weight) for lower in minimum] + [(0, None)]*(dimensions-n)
        feasible = linprog(np.zeros(dimensions), A_ub=a if len(a) else None,
                           b_ub=b if len(a) else None, A_eq=budget[None, :],
                           b_eq=[c.investment], bounds=bounds, method="highs")
        if not feasible.success:
            raise OptimizationError("infeasible" if feasible.status == 2 else "solver_failed", feasible.message)
        if c.objective in {"equal", "score"}:
            if c.objective == "equal":
                weights = equal_weights(names).to_numpy()*c.investment
            else:
                normalized = scores/scores.max()
                weights = normalized/normalized.sum()*c.investment
            solution = np.r_[weights, np.abs(weights-previous)] if turnover_active else weights
            status = {"status": 0, "success": True, "message": "exact baseline", "method": "closed_form"}
        else:
            quadratic = matrix if c.objective == "minimum_variance" else c.risk_aversion/2*matrix
            linear = np.zeros(n) if c.objective == "minimum_variance" else -mu
            objective_scale = max(float(np.max(np.abs(quadratic))), float(np.max(np.abs(linear))), np.finfo(float).tiny)
            q, l = quadratic/objective_scale, linear/objective_scale
            def objective(x):
                return float(x[:n] @ q @ x[:n] + l @ x[:n])
            def gradient(x):
                return np.r_[2*q @ x[:n]+l, np.zeros(dimensions-n)]
            constraints = [LinearConstraint(budget[None, :], c.investment, c.investment)]
            if len(a):
                constraints.append(LinearConstraint(a, -np.inf, b))
            result = minimize(objective, feasible.x, jac=gradient, method="SLSQP",
                              bounds=Bounds(np.r_[minimum, np.zeros(dimensions-n)], np.r_[np.full(n, c.max_weight), np.full(dimensions-n, np.inf)]),
                              constraints=constraints, options={"ftol": min(c.tolerance*.01, 1e-12), "maxiter": c.max_iterations})
            if not result.success:
                raise OptimizationError("solver_failed", str(result.message))
            solution = result.x
            weights = solution[:n]
            status = {"status": int(result.status), "success": True, "message": str(result.message), "method": "SLSQP", "iterations": int(result.nit)}
        turnover = None if previous is None else float(np.abs(weights-previous).sum()+exits)
        checks = {"finite": bool(np.isfinite(weights).all()),
                  "budget": bool(abs(weights.sum()-c.investment) <= c.tolerance),
                  "long_only": bool(weights.min() >= -c.tolerance),
                  "max_weight": bool(weights.max() <= c.max_weight+c.tolerance)}
        checks.update({label: bool(row @ solution <= bound+c.tolerance) for label, row, bound in zip(row_names, a, b)})
        if minimum_record is not None:
            checks["minimum_weights"] = bool((weights >= minimum-c.tolerance).all())
        if turnover_active:
            checks["turnover"] = bool(turnover <= c.turnover_limit+c.tolerance)
        if not all(checks.values()):
            raise OptimizationError("infeasible" if c.objective in {"equal", "score"} else "solver_failed", "returned weights violate constraints")
        exposures = {sector: float(sum(weights[i] for i, name in enumerate(names) if industries.get(name) == sector)) for sector in sorted(set(industries.values()))}
        variance = float(weights @ matrix @ weights)
        record = {"symbols": names, "decision_time": str(decision), "risk": risk_source,
                  "scores": None if scores is None else scores.tolist(), "expected_returns": None if mu is None else mu.tolist(),
                  "expected_return_definition": data.expected_return_definition, "industries": industries,
                  "benchmark_industries": benchmark, "previous_weights": previous_record}
        if minimum_record is not None:
            record["minimum_weights"] = minimum_record
        parameters = {"config": configuration, "inputs": record, "numpy": np.__version__, "scipy": scipy.__version__,
                      "covariance_roundoff_tolerance": numerical_tolerance}
        return OptimizationResult(pd.Series(weights, index=pd.Index(names, name="symbol"), name="target_weight"),
                                  1-c.investment, variance, float(np.sqrt(max(variance, 0))), turnover,
                                  exposures, checks, status, _hash(configuration), _hash(record), parameters)
