"""Reusable return and risk statistics for research portfolios."""

from __future__ import annotations

import math
from typing import Iterable

import numpy as np
import pandas as pd


def _returns(values: Iterable[float]) -> pd.Series:
    series = pd.to_numeric(pd.Series(values, dtype=float), errors="coerce")
    return series.replace([np.inf, -np.inf], np.nan).dropna()


def cumulative_return(returns: Iterable[float]) -> float:
    values = _returns(returns)
    if values.empty:
        return float("nan")
    return float((1.0 + values).prod() - 1.0)


def annualized_return(returns: Iterable[float], periods_per_year: int = 252) -> float:
    values = _returns(returns)
    if values.empty or periods_per_year <= 0:
        return float("nan")
    growth = float((1.0 + values).prod())
    if growth < 0:
        return float("nan")
    if growth == 0:
        return -1.0
    return float(growth ** (periods_per_year / float(len(values))) - 1.0)


def annualized_volatility(returns: Iterable[float], periods_per_year: int = 252) -> float:
    values = _returns(returns)
    if len(values) < 2 or periods_per_year <= 0:
        return float("nan")
    return float(values.std(ddof=1) * math.sqrt(periods_per_year))


def sharpe_ratio(
    returns: Iterable[float],
    risk_free_rate: float = 0.0,
    periods_per_year: int = 252,
) -> float:
    values = _returns(returns)
    if len(values) < 2 or periods_per_year <= 0:
        return float("nan")
    daily_risk_free = (1.0 + risk_free_rate) ** (1.0 / periods_per_year) - 1.0
    excess = values - daily_risk_free
    volatility = excess.std(ddof=1)
    if volatility == 0 or pd.isna(volatility):
        return float("nan")
    return float(excess.mean() / volatility * math.sqrt(periods_per_year))


def sortino_ratio(
    returns: Iterable[float],
    risk_free_rate: float = 0.0,
    periods_per_year: int = 252,
) -> float:
    values = _returns(returns)
    if values.empty or periods_per_year <= 0:
        return float("nan")
    daily_risk_free = (1.0 + risk_free_rate) ** (1.0 / periods_per_year) - 1.0
    excess = values - daily_risk_free
    downside = np.minimum(excess.to_numpy(dtype=float), 0.0)
    downside_deviation = float(np.sqrt(np.mean(np.square(downside))))
    if downside_deviation == 0:
        return float("nan")
    return float(excess.mean() / downside_deviation * math.sqrt(periods_per_year))


def drawdown_series(nav: Iterable[float]) -> pd.Series:
    values = pd.to_numeric(pd.Series(nav, dtype=float), errors="coerce")
    if values.isna().any() or (~np.isfinite(values)).any() or (values <= 0).any():
        return pd.Series(np.nan, index=values.index, dtype=float)
    # A normalized strategy begins at 1.0 immediately before its first
    # execution.  The NAV table starts after that execution, so its first row
    # may already include entry fees.  Include the origin in the running peak
    # without changing the returned index/length.
    running_peak = values.cummax().clip(lower=1.0)
    return values / running_peak - 1.0


def maximum_drawdown(nav: Iterable[float]) -> float:
    drawdowns = drawdown_series(nav).dropna()
    if drawdowns.empty:
        return float("nan")
    return float(drawdowns.min())


def calmar_ratio(
    returns: Iterable[float],
    nav: Iterable[float],
    periods_per_year: int = 252,
) -> float:
    annual_return = annualized_return(returns, periods_per_year)
    drawdown = maximum_drawdown(nav)
    if pd.isna(annual_return) or pd.isna(drawdown) or drawdown == 0:
        return float("nan")
    return float(annual_return / abs(drawdown))


def tracking_error(
    strategy_returns: Iterable[float],
    benchmark_returns: Iterable[float],
    periods_per_year: int = 252,
) -> float:
    pair = pd.concat(
        [_returns(strategy_returns).rename("strategy"), _returns(benchmark_returns).rename("benchmark")],
        axis=1,
        join="inner",
    ).dropna()
    if len(pair) < 2 or periods_per_year <= 0:
        return float("nan")
    return float((pair["strategy"] - pair["benchmark"]).std(ddof=1) * math.sqrt(periods_per_year))


def information_ratio(
    strategy_returns: Iterable[float],
    benchmark_returns: Iterable[float],
    periods_per_year: int = 252,
) -> float:
    strategy = pd.Series(strategy_returns, dtype=float).reset_index(drop=True)
    benchmark = pd.Series(benchmark_returns, dtype=float).reset_index(drop=True)
    pair = pd.concat([strategy.rename("strategy"), benchmark.rename("benchmark")], axis=1)
    pair = pair.replace([np.inf, -np.inf], np.nan).dropna()
    if len(pair) < 2 or periods_per_year <= 0:
        return float("nan")
    active = pair["strategy"] - pair["benchmark"]
    error = active.std(ddof=1) * math.sqrt(periods_per_year)
    if error == 0 or pd.isna(error):
        return float("nan")
    return float(active.mean() * periods_per_year / error)


__all__ = [
    "annualized_return",
    "annualized_volatility",
    "calmar_ratio",
    "cumulative_return",
    "drawdown_series",
    "information_ratio",
    "maximum_drawdown",
    "sharpe_ratio",
    "sortino_ratio",
    "tracking_error",
]
