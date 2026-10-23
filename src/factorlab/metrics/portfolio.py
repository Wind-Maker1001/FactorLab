"""Aggregate strategy, benchmark, risk, turnover, and cost metrics."""

from __future__ import annotations

from typing import Dict, Optional

import numpy as np
import pandas as pd

from factorlab.errors import ConfigurationError, SchemaError
from factorlab.metrics.risk import (
    annualized_return,
    annualized_volatility,
    calmar_ratio,
    cumulative_return,
    information_ratio,
    maximum_drawdown,
    sharpe_ratio,
    sortino_ratio,
    tracking_error,
)


def calculate_portfolio_metrics(
    nav: pd.DataFrame,
    trades: Optional[pd.DataFrame] = None,
    positions: Optional[pd.DataFrame] = None,
    periods_per_year: int = 252,
    risk_free_rate: float = 0.0,
) -> Dict[str, float]:
    """Calculate the portfolio metric set from a daily NAV table."""

    if periods_per_year <= 0:
        raise ConfigurationError("periods_per_year must be positive")
    required = {
        "date",
        "strategy_nav",
        "benchmark_nav",
        "strategy_return",
        "benchmark_return",
    }
    missing = sorted(required.difference(nav.columns))
    if missing:
        raise SchemaError("NAV table missing columns: %s" % ", ".join(missing))
    if nav.empty:
        raise SchemaError("NAV table must not be empty")
    frame = nav.copy()
    frame["date"] = pd.to_datetime(frame["date"], errors="raise").dt.normalize()
    frame = frame.sort_values("date", kind="mergesort")
    if frame["date"].duplicated().any():
        raise SchemaError("NAV table must have one row per date")
    strategy = pd.to_numeric(frame["strategy_return"], errors="coerce")
    benchmark = pd.to_numeric(frame["benchmark_return"], errors="coerce")
    strategy_nav = pd.to_numeric(frame["strategy_nav"], errors="coerce")

    strategy_cumulative = cumulative_return(strategy)
    benchmark_cumulative = cumulative_return(benchmark)
    total_cost = (
        float(pd.to_numeric(frame["transaction_cost"], errors="coerce").fillna(0.0).sum())
        if "transaction_cost" in frame
        else 0.0
    )
    total_turnover = (
        float(pd.to_numeric(frame["turnover"], errors="coerce").fillna(0.0).sum())
        if "turnover" in frame
        else 0.0
    )
    if trades is None:
        number_of_trades = 0
    elif "trade_notional" in trades:
        notionals = pd.to_numeric(trades["trade_notional"], errors="coerce").fillna(0.0)
        number_of_trades = int((notionals > 0).sum())
    else:
        number_of_trades = int(len(trades))

    if "holdings" in frame:
        average_holdings = float(pd.to_numeric(frame["holdings"], errors="coerce").mean())
    elif positions is not None and not positions.empty:
        if not {"date", "symbol"}.issubset(positions.columns):
            raise SchemaError("positions require date and symbol columns")
        counts = positions.groupby("date")["symbol"].nunique()
        all_dates = pd.Index(frame["date"])
        average_holdings = float(counts.reindex(all_dates, fill_value=0).mean())
    else:
        average_holdings = 0.0

    valid_strategy = strategy.replace([np.inf, -np.inf], np.nan).dropna()
    return {
        "cumulative_return": strategy_cumulative,
        "annualized_return": annualized_return(strategy, periods_per_year),
        "annualized_volatility": annualized_volatility(strategy, periods_per_year),
        "sharpe_ratio": sharpe_ratio(strategy, risk_free_rate, periods_per_year),
        "sortino_ratio": sortino_ratio(strategy, risk_free_rate, periods_per_year),
        "maximum_drawdown": maximum_drawdown(strategy_nav),
        "calmar_ratio": calmar_ratio(strategy, strategy_nav, periods_per_year),
        "benchmark_return": benchmark_cumulative,
        "excess_return": strategy_cumulative - benchmark_cumulative,
        "tracking_error": tracking_error(strategy, benchmark, periods_per_year),
        "information_ratio": information_ratio(strategy, benchmark, periods_per_year),
        "win_rate": float((valid_strategy > 0).mean()) if len(valid_strategy) else np.nan,
        "portfolio_turnover": total_turnover,
        "total_transaction_cost": total_cost,
        "number_of_trades": number_of_trades,
        "average_holdings": average_holdings,
    }


__all__ = ["calculate_portfolio_metrics"]
