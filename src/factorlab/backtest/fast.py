"""Vector-friendly fast research backtest with next-open execution timing.

This engine intentionally operates on target weights rather than exchange
orders or board lots.  It is the fast experiment layer: daily bars cannot
reconstruct an order book, limit queues, or intraday liquidity.  Use the
separate rule-aware execution simulator when T+1, board lots, cash, suspension,
and opening price-limit approximations are required.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from factorlab.backtest.fees import FeeBreakdown, FeeSchedule
from factorlab.data.schema import normalize_symbol
from factorlab.errors import ConfigurationError, SchemaError
from factorlab.portfolio.topk import TopKPortfolio


@dataclass(frozen=True)
class FastBacktestConfig:
    top_k: int = 20
    rebalance_days: int = 5
    minimum_cross_section: int = 1
    initial_cash: float = 1_000_000.0
    price_column: str = "adj_open"
    benchmark_price_column: str = "adj_open"

    def __post_init__(self) -> None:
        for name in ("top_k", "rebalance_days", "minimum_cross_section"):
            value = getattr(self, name)
            if (
                isinstance(value, (bool, np.bool_))
                or not isinstance(value, (int, np.integer))
                or int(value) <= 0
            ):
                raise ConfigurationError("%s must be a positive integer" % name)
        if isinstance(self.initial_cash, (bool, np.bool_)):
            raise ConfigurationError("initial_cash must be finite and positive")
        try:
            cash = float(self.initial_cash)
        except (TypeError, ValueError) as exc:
            raise ConfigurationError(
                "initial_cash must be finite and positive"
            ) from exc
        if not np.isfinite(cash) or cash <= 0:
            raise ConfigurationError("initial_cash must be finite and positive")
        if not isinstance(self.price_column, str) or not self.price_column.strip():
            raise ConfigurationError("price_column must be a non-empty string")
        if (
            not isinstance(self.benchmark_price_column, str)
            or not self.benchmark_price_column.strip()
        ):
            raise ConfigurationError(
                "benchmark_price_column must be a non-empty string"
            )


@dataclass
class FastBacktestResult:
    nav: pd.DataFrame
    trades: pd.DataFrame
    positions: pd.DataFrame
    signals: pd.DataFrame


def _dates(values: pd.Series, label: str) -> pd.Series:
    try:
        result = pd.to_datetime(values, errors="raise")
    except (TypeError, ValueError) as exc:
        raise SchemaError("%s contains an invalid date" % label) from exc
    if isinstance(result.dtype, pd.DatetimeTZDtype):
        result = result.dt.tz_localize(None)
    return result.dt.normalize()


def _prepare_benchmark(frame: pd.DataFrame, price_column: str) -> pd.DataFrame:
    required = {"date", price_column}
    missing = sorted(required.difference(frame.columns))
    if missing:
        raise SchemaError("benchmark missing columns: %s" % ", ".join(missing))
    result = frame[["date", price_column]].copy()
    result["date"] = _dates(result["date"], "benchmark.date")
    if result["date"].duplicated().any():
        raise SchemaError("benchmark must have one row per trading date")
    raw_prices = result[price_column].copy()
    result[price_column] = pd.to_numeric(raw_prices, errors="coerce")
    invalid_parse = raw_prices.notna() & result[price_column].isna()
    if invalid_parse.any():
        raise SchemaError("benchmark open prices contain a non-numeric value")
    invalid = ~np.isfinite(result[price_column]) | (result[price_column] <= 0)
    if invalid.any():
        raise SchemaError("benchmark open prices must be finite and positive")
    result = result.sort_values("date", kind="mergesort").reset_index(drop=True)
    if len(result) < 2:
        raise SchemaError("benchmark requires at least two trading dates")
    return result


def _prepare_prices(frame: pd.DataFrame, price_column: str) -> pd.DataFrame:
    required = {"date", "symbol", price_column}
    missing = sorted(required.difference(frame.columns))
    if missing:
        raise SchemaError("stock bars missing columns: %s" % ", ".join(missing))
    result = frame[["date", "symbol", price_column]].copy()
    result["date"] = _dates(result["date"], "bars.date")
    try:
        result["symbol"] = result["symbol"].map(normalize_symbol)
    except Exception as exc:
        if isinstance(exc, SchemaError):
            raise
        raise SchemaError("bar symbol normalization failed") from exc
    if result.duplicated(["date", "symbol"]).any():
        raise SchemaError("duplicate bar (date, symbol) rows detected")
    raw_prices = result[price_column].copy()
    result[price_column] = pd.to_numeric(raw_prices, errors="coerce")
    invalid_parse = raw_prices.notna() & result[price_column].isna()
    if invalid_parse.any():
        raise SchemaError("stock open prices contain a non-numeric value")
    invalid = result[price_column].notna() & (
        ~np.isfinite(result[price_column]) | (result[price_column] <= 0)
    )
    if invalid.any():
        raise SchemaError("stock open prices must be positive when present")
    return result.pivot(index="date", columns="symbol", values=price_column).sort_index()


def _fee_total(
    fee_schedule: FeeSchedule,
    execution_date: pd.Timestamp,
    current_values: Dict[str, float],
    target_weights: Dict[str, float],
    pre_trade_value: float,
) -> Tuple[float, Dict[str, Tuple[float, str, FeeBreakdown]], float]:
    """Solve post-fee value and target orders by fixed-point iteration."""

    post_trade_value = pre_trade_value
    details: Dict[str, Tuple[float, str, FeeBreakdown]] = {}
    for _ in range(50):
        candidate: Dict[str, Tuple[float, str, FeeBreakdown]] = {}
        total_cost = 0.0
        symbols = sorted(set(current_values).union(target_weights))
        for symbol in symbols:
            target_value = target_weights.get(symbol, 0.0) * post_trade_value
            delta = target_value - current_values.get(symbol, 0.0)
            if abs(delta) <= max(1e-10, pre_trade_value * 1e-14):
                continue
            side = "buy" if delta > 0 else "sell"
            breakdown = fee_schedule.calculate(execution_date, side, abs(delta))
            candidate[symbol] = (delta, side, breakdown)
            total_cost += breakdown.total
        updated_value = pre_trade_value - total_cost
        if updated_value <= 0:
            raise ConfigurationError("transaction costs exhausted portfolio value")
        details = candidate
        if abs(updated_value - post_trade_value) <= max(1e-8, pre_trade_value * 1e-12):
            post_trade_value = updated_value
            break
        post_trade_value = updated_value
    else:  # pragma: no cover - defensive against pathological custom schedules
        raise ConfigurationError("transaction cost calculation did not converge")
    return post_trade_value, details, pre_trade_value - post_trade_value


class FastResearchBacktester:
    """Evaluate Top-K scores with close-signal/next-open execution."""

    def __init__(
        self,
        config: Optional[FastBacktestConfig] = None,
        fee_schedule: Optional[FeeSchedule] = None,
    ):
        self.config = config or FastBacktestConfig()
        self.fee_schedule = fee_schedule or FeeSchedule.a_share_default()

    def run(
        self,
        scores: pd.DataFrame,
        bars: pd.DataFrame,
        benchmark: pd.DataFrame,
    ) -> FastBacktestResult:
        benchmark_data = _prepare_benchmark(benchmark, self.config.benchmark_price_column)
        calendar = pd.DatetimeIndex(benchmark_data["date"])
        prices = _prepare_prices(bars, self.config.price_column).reindex(calendar)

        selected = TopKPortfolio(
            top_k=self.config.top_k,
            rebalance_days=self.config.rebalance_days,
            minimum_cross_section=self.config.minimum_cross_section,
        ).construct(scores, trading_dates=calendar)
        if selected.empty:
            raise SchemaError("no valid Top-K signals are available")

        calendar_position = {value: index for index, value in enumerate(calendar)}
        executions: Dict[pd.Timestamp, pd.DataFrame] = {}
        accepted_parts: List[pd.DataFrame] = []
        missing_execution_dates: List[pd.Timestamp] = []
        for signal_date, group in selected.groupby("date", sort=True):
            position = calendar_position.get(pd.Timestamp(signal_date))
            if position is None or position + 1 >= len(calendar):
                missing_execution_dates.append(pd.Timestamp(signal_date))
                continue
            execution_date = calendar[position + 1]
            copy = group.copy()
            copy["execution_date"] = execution_date
            executions[execution_date] = copy
            accepted_parts.append(copy)
        if missing_execution_dates:
            formatted = ", ".join(
                value.date().isoformat() for value in missing_execution_dates[:5]
            )
            raise SchemaError(
                "signal dates have no following benchmark trading date: %s" % formatted
            )
        if not executions:
            raise SchemaError("signals have no following trading date for execution")
        accepted = pd.concat(accepted_parts, ignore_index=True)

        first_execution = min(executions)
        start_position = calendar_position[first_execution]
        benchmark_prices = benchmark_data.set_index("date")[self.config.benchmark_price_column]
        portfolio_value = float(self.config.initial_cash)
        benchmark_nav = 1.0
        weights: Dict[str, float] = {}
        last_valid_marks: Dict[str, float] = {}
        nav_rows: List[dict] = []
        trade_rows: List[dict] = []
        position_rows: List[dict] = []

        for offset in range(start_position, len(calendar)):
            day = calendar[offset]
            previous_value = portfolio_value
            current_values: Dict[str, float] = {}
            current_marks: Dict[str, float] = {}
            if weights:
                for symbol, weight in weights.items():
                    current_price = prices.at[day, symbol] if symbol in prices else np.nan
                    previous_mark = last_valid_marks[symbol]
                    mark_price = (
                        previous_mark if pd.isna(current_price) else float(current_price)
                    )
                    security_return = float(mark_price / previous_mark - 1.0)
                    current_values[symbol] = previous_value * weight * (1.0 + security_return)
                    current_marks[symbol] = mark_price
                pre_trade_value = float(sum(current_values.values()))
            else:
                pre_trade_value = previous_value

            benchmark_return = 0.0
            if offset > start_position:
                previous_benchmark = float(benchmark_prices.iloc[offset - 1])
                benchmark_return = float(benchmark_prices.iloc[offset] / previous_benchmark - 1.0)
                benchmark_nav *= 1.0 + benchmark_return

            transaction_cost = 0.0
            turnover = 0.0
            signal_date = pd.NaT
            if day in executions:
                orders = executions[day].sort_values("rank", kind="mergesort")
                target_weights = dict(zip(orders["symbol"], orders["target_weight"]))
                signal_date = pd.Timestamp(orders["date"].iloc[0])
                for symbol in sorted(set(current_values).union(target_weights)):
                    if symbol not in prices.columns or pd.isna(prices.at[day, symbol]):
                        raise SchemaError(
                            "execution open is missing for %s on %s"
                            % (symbol, day.date())
                        )
                portfolio_value, details, transaction_cost = _fee_total(
                    self.fee_schedule,
                    day,
                    current_values,
                    target_weights,
                    pre_trade_value,
                )
                target_values = {
                    symbol: weight * portfolio_value
                    for symbol, weight in target_weights.items()
                }
                total_buys = sum(max(delta, 0.0) for delta, _, _ in details.values())
                total_sells = sum(max(-delta, 0.0) for delta, _, _ in details.values())
                turnover = max(total_buys, total_sells) / pre_trade_value
                order_lookup = orders.set_index("symbol")
                for symbol, (delta, side, breakdown) in details.items():
                    row = order_lookup.loc[symbol] if symbol in order_lookup.index else None
                    trade_rows.append(
                        {
                            "signal_date": signal_date,
                            "execution_date": day,
                            "symbol": symbol,
                            "side": side,
                            "score": np.nan if row is None else float(row["score"]),
                            "rank": np.nan if row is None else int(row["rank"]),
                            "previous_weight": current_values.get(symbol, 0.0) / pre_trade_value,
                            "target_weight": target_weights.get(symbol, 0.0),
                            "trade_notional": abs(delta),
                            "execution_price": float(prices.at[day, symbol]),
                            **breakdown.to_dict(),
                        }
                    )
                current_values = target_values
                weights = target_weights
                last_valid_marks = {
                    symbol: float(prices.at[day, symbol]) for symbol in weights
                }
            else:
                portfolio_value = pre_trade_value
                weights = (
                    {
                        symbol: value / portfolio_value
                        for symbol, value in current_values.items()
                        if value > 0
                    }
                    if portfolio_value > 0
                    else {}
                )
                last_valid_marks = {
                    symbol: current_marks[symbol] for symbol in weights
                }

            strategy_return = portfolio_value / previous_value - 1.0
            strategy_nav = portfolio_value / float(self.config.initial_cash)
            for symbol, weight in sorted(weights.items()):
                position_rows.append(
                    {
                        "date": day,
                        "symbol": symbol,
                        "weight": weight,
                        "market_value": portfolio_value * weight,
                        "mark_price": last_valid_marks[symbol],
                    }
                )
            nav_rows.append(
                {
                    "date": day,
                    "signal_date": signal_date,
                    "strategy_nav": strategy_nav,
                    "benchmark_nav": benchmark_nav,
                    "excess_nav": strategy_nav / benchmark_nav,
                    "portfolio_value": portfolio_value,
                    "strategy_return": strategy_return,
                    "benchmark_return": benchmark_return,
                    "transaction_cost": transaction_cost,
                    "transaction_cost_rate": transaction_cost / pre_trade_value,
                    "turnover": turnover,
                    "holdings": len(weights),
                }
            )

        nav = pd.DataFrame(nav_rows)
        trades = pd.DataFrame(trade_rows)
        positions = pd.DataFrame(position_rows)
        return FastBacktestResult(
            nav=nav,
            trades=trades,
            positions=positions,
            signals=accepted.sort_values(["date", "rank"], kind="mergesort").reset_index(drop=True),
        )


def run_fast_backtest(
    scores: pd.DataFrame,
    bars: pd.DataFrame,
    benchmark: pd.DataFrame,
    config: Optional[FastBacktestConfig] = None,
    fee_schedule: Optional[FeeSchedule] = None,
) -> FastBacktestResult:
    return FastResearchBacktester(config=config, fee_schedule=fee_schedule).run(
        scores, bars, benchmark
    )


__all__ = [
    "FastBacktestConfig",
    "FastBacktestResult",
    "FastResearchBacktester",
    "run_fast_backtest",
]
