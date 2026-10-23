"""Daily-bar A-share execution simulator for out-of-sample research.

This engine deliberately models only rules observable from daily data. A
signal produced after day ``t`` closes is first executable at day ``t+1``
open. Orders use integer board lots, cash accounting, date-effective fees,
T+1 sellability, suspension checks, and conservative opening price-limit
rejections. It never claims to reconstruct order-book queues.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from qbot.research.backtest.fees import FeeBreakdown, FeeSchedule
from qbot.research.backtest.rules import PriceLimitSchedule
from qbot.research.data.schema import normalize_symbol
from qbot.research.errors import ConfigurationError, SchemaError
from qbot.research.portfolio.topk import TopKPortfolio


@dataclass(frozen=True)
class ExecutionBacktestConfig:
    top_k: int = 20
    rebalance_days: int = 5
    minimum_cross_section: int = 20
    initial_cash: float = 1_000_000.0
    board_lot: int = 100
    t_plus_one: bool = True
    reject_suspended: bool = True
    price_limit_tolerance_bps: float = 5.0
    open_price_column: str = "raw_open"
    close_price_column: str = "raw_close"
    benchmark_open_price_column: str = "adj_open"
    benchmark_close_price_column: str = "adj_close"

    def __post_init__(self) -> None:
        for name in ("top_k", "rebalance_days", "minimum_cross_section", "board_lot"):
            value = getattr(self, name)
            if (
                isinstance(value, (bool, np.bool_))
                or not isinstance(value, (int, np.integer))
                or int(value) <= 0
            ):
                raise ConfigurationError("%s must be a positive integer" % name)
        if isinstance(self.initial_cash, bool):
            raise ConfigurationError("initial_cash must be finite and positive")
        try:
            initial_cash = float(self.initial_cash)
        except (TypeError, ValueError) as exc:
            raise ConfigurationError(
                "initial_cash must be finite and positive"
            ) from exc
        if not math.isfinite(initial_cash) or initial_cash <= 0:
            raise ConfigurationError("initial_cash must be finite and positive")
        if type(self.t_plus_one) is not bool:
            raise ConfigurationError("t_plus_one must be boolean")
        if type(self.reject_suspended) is not bool:
            raise ConfigurationError("reject_suspended must be boolean")
        try:
            tolerance = float(self.price_limit_tolerance_bps)
        except (TypeError, ValueError) as exc:
            raise ConfigurationError(
                "price_limit_tolerance_bps must be in [0, 10000)"
            ) from exc
        if not math.isfinite(tolerance) or tolerance < 0 or tolerance >= 10_000:
            raise ConfigurationError(
                "price_limit_tolerance_bps must be in [0, 10000)"
            )
        for name in (
            "open_price_column",
            "close_price_column",
            "benchmark_open_price_column",
            "benchmark_close_price_column",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise ConfigurationError("%s must be a non-empty string" % name)


@dataclass(frozen=True)
class ExecutionBacktestResult:
    nav: pd.DataFrame
    trades: pd.DataFrame
    positions: pd.DataFrame
    rejections: pd.DataFrame
    signals: pd.DataFrame


@dataclass
class _PositionLot:
    acquired_on: pd.Timestamp
    shares: int


class PositionBook:
    """FIFO share lots with an explicit T+1 sellability query."""

    def __init__(self) -> None:
        self._lots: Dict[str, List[_PositionLot]] = {}

    def total_shares(self, symbol: str) -> int:
        return int(sum(lot.shares for lot in self._lots.get(symbol, ())))

    def sellable_shares(
        self, symbol: str, trade_date: object, *, t_plus_one: bool = True
    ) -> int:
        day = pd.Timestamp(trade_date).normalize()
        return int(
            sum(
                lot.shares
                for lot in self._lots.get(symbol, ())
                if not t_plus_one or lot.acquired_on < day
            )
        )

    def buy(self, symbol: str, trade_date: object, shares: int) -> None:
        quantity = _share_quantity(shares)
        if quantity == 0:
            return
        day = pd.Timestamp(trade_date).normalize()
        self._lots.setdefault(symbol, []).append(_PositionLot(day, quantity))

    def sell(
        self,
        symbol: str,
        trade_date: object,
        shares: int,
        *,
        t_plus_one: bool = True,
    ) -> None:
        quantity = _share_quantity(shares)
        if quantity > self.sellable_shares(
            symbol, trade_date, t_plus_one=t_plus_one
        ):
            raise ConfigurationError("attempted to sell non-sellable shares")
        remaining = quantity
        day = pd.Timestamp(trade_date).normalize()
        lots = self._lots.get(symbol, [])
        for lot in lots:
            if remaining == 0:
                break
            if t_plus_one and lot.acquired_on >= day:
                continue
            taken = min(lot.shares, remaining)
            lot.shares -= taken
            remaining -= taken
        self._lots[symbol] = [lot for lot in lots if lot.shares > 0]
        if not self._lots[symbol]:
            self._lots.pop(symbol, None)

    def symbols(self) -> Tuple[str, ...]:
        return tuple(sorted(self._lots))


def _share_quantity(value: object) -> int:
    if (
        isinstance(value, (bool, np.bool_))
        or not isinstance(value, (int, np.integer))
        or int(value) < 0
    ):
        raise ConfigurationError("shares must be a non-negative integer")
    return int(value)


def _normalise_dates(values: pd.Series, name: str) -> pd.Series:
    try:
        result = pd.to_datetime(values, errors="raise")
    except (TypeError, ValueError) as exc:
        raise SchemaError("%s contains invalid dates" % name) from exc
    if result.isna().any():
        raise SchemaError("%s must not contain missing dates" % name)
    if getattr(result.dt, "tz", None) is not None:
        result = result.dt.tz_localize(None)
    return result.dt.normalize()


def _prepare_bars(bars: pd.DataFrame, config: ExecutionBacktestConfig) -> pd.DataFrame:
    if not isinstance(bars, pd.DataFrame) or bars.empty:
        raise SchemaError("execution bars must be a non-empty DataFrame")
    required = {
        "date",
        "symbol",
        "volume",
        "trade_status",
        "is_st",
        config.open_price_column,
        config.close_price_column,
    }
    missing = sorted(required.difference(bars.columns))
    if missing:
        raise SchemaError("execution bars missing columns: %s" % ", ".join(missing))
    frame = bars.copy()
    frame["date"] = _normalise_dates(frame["date"], "bars.date")
    frame["symbol"] = frame["symbol"].map(normalize_symbol)
    if frame.duplicated(["date", "symbol"]).any():
        raise SchemaError("execution bars contain duplicate (date, symbol) rows")
    for column in (
        config.open_price_column,
        config.close_price_column,
        "volume",
    ):
        raw = frame[column]
        frame[column] = pd.to_numeric(raw, errors="coerce")
        if (raw.notna() & frame[column].isna()).any():
            raise SchemaError("execution bars %s contains non-numeric values" % column)
        values = frame[column].to_numpy(dtype=float, copy=False)
        if np.isinf(values).any():
            raise SchemaError("execution bars %s contains infinity" % column)
    for column in (config.open_price_column, config.close_price_column):
        if (frame[column].dropna() <= 0.0).any():
            raise SchemaError("execution prices must be positive when present")
    if (frame["volume"].dropna() < 0.0).any():
        raise SchemaError("execution volume must be non-negative")
    frame.sort_values(["symbol", "date"], kind="mergesort", inplace=True)
    frame["_previous_close"] = frame.groupby("symbol", sort=False)[
        config.close_price_column
    ].transform(lambda values: values.ffill().shift(1))
    frame.sort_values(["date", "symbol"], kind="mergesort", inplace=True)
    return frame.reset_index(drop=True)


def _prepare_benchmark(
    benchmark: pd.DataFrame,
    open_price_column: str,
    close_price_column: str,
) -> pd.DataFrame:
    if not isinstance(benchmark, pd.DataFrame) or benchmark.empty:
        raise SchemaError("benchmark must be a non-empty DataFrame")
    required = {"date", open_price_column, close_price_column}
    missing = sorted(required.difference(benchmark.columns))
    if missing:
        raise SchemaError("benchmark missing columns: %s" % ", ".join(missing))
    columns = ["date", open_price_column]
    if close_price_column != open_price_column:
        columns.append(close_price_column)
    result = benchmark.loc[:, columns].copy()
    result["date"] = _normalise_dates(result["date"], "benchmark.date")
    if result["date"].duplicated().any():
        raise SchemaError("benchmark contains duplicate dates")
    for price_column in (open_price_column, close_price_column):
        raw = result[price_column]
        result[price_column] = pd.to_numeric(raw, errors="coerce")
        if (raw.notna() & result[price_column].isna()).any():
            raise SchemaError(
                "benchmark %s contains non-numeric values" % price_column
            )
        values = result[price_column].to_numpy(dtype=float, copy=False)
        if (
            result[price_column].isna().any()
            or np.isinf(values).any()
            or (result[price_column] <= 0.0).any()
        ):
            raise SchemaError("benchmark prices must be finite and positive")
    return result.sort_values("date", kind="mergesort").reset_index(drop=True)


def _is_st(value: object) -> bool:
    if value is None or value is pd.NA or (
        isinstance(value, float) and np.isnan(value)
    ):
        return False
    if isinstance(value, str):
        return value.strip().lower() in {
            "1",
            "true",
            "yes",
            "y",
            "st",
            "*st",
            "是",
        }
    return bool(value)


def _is_suspended(row: pd.Series, reject_suspended: bool) -> bool:
    if not reject_suspended:
        return False
    if pd.isna(row["volume"]) or float(row["volume"]) <= 0.0:
        return True
    status = row.get("trade_status", pd.NA)
    if status is pd.NA or pd.isna(status):
        return False
    if isinstance(status, str):
        return status.strip().lower() in {
            "0",
            "false",
            "suspended",
            "停牌",
        }
    return not bool(status)


def _affordable_quantity(
    cash: float,
    price: float,
    requested: int,
    board_lot: int,
    trade_date: pd.Timestamp,
    fees: FeeSchedule,
) -> int:
    requested_lots = requested // board_lot
    low, high = 0, requested_lots
    while low < high:
        middle = (low + high + 1) // 2
        quantity = middle * board_lot
        notional = quantity * price
        required_cash = notional + fees.calculate(
            trade_date, "buy", notional
        ).total
        if required_cash <= cash + 1e-9:
            low = middle
        else:
            high = middle - 1
    return low * board_lot


_TRADE_COLUMNS = [
    "signal_date",
    "execution_date",
    "symbol",
    "side",
    "score",
    "rank",
    "requested_shares",
    "executed_shares",
    "execution_price",
    "trade_notional",
    "commission",
    "stamp_duty",
    "transfer_fee",
    "side_fee",
    "slippage",
    "transaction_cost",
    "cash_after",
]
_REJECTION_COLUMNS = [
    "signal_date",
    "execution_date",
    "symbol",
    "side",
    "score",
    "rank",
    "requested_shares",
    "rejected_shares",
    "reason",
]
_POSITION_COLUMNS = [
    "date",
    "symbol",
    "shares",
    "sellable_shares",
    "mark_price",
    "market_value",
    "weight",
]


class AShareExecutionBacktester:
    """Execute Top-K targets with observable mainland-market constraints."""

    def __init__(
        self,
        config: Optional[ExecutionBacktestConfig] = None,
        fee_schedule: Optional[FeeSchedule] = None,
        price_limit_schedule: Optional[PriceLimitSchedule] = None,
    ) -> None:
        self.config = config or ExecutionBacktestConfig()
        self.fee_schedule = fee_schedule or FeeSchedule.a_share_default()
        self.price_limit_schedule = (
            price_limit_schedule or PriceLimitSchedule.a_share_default()
        )

    def run(
        self,
        scores: pd.DataFrame,
        bars: pd.DataFrame,
        benchmark: pd.DataFrame,
    ) -> ExecutionBacktestResult:
        prepared_bars = _prepare_bars(bars, self.config)
        benchmark_data = _prepare_benchmark(
            benchmark,
            self.config.benchmark_open_price_column,
            self.config.benchmark_close_price_column,
        )
        calendar = pd.DatetimeIndex(benchmark_data["date"])
        selected = TopKPortfolio(
            top_k=self.config.top_k,
            rebalance_days=self.config.rebalance_days,
            minimum_cross_section=self.config.minimum_cross_section,
        ).construct(scores, trading_dates=calendar)
        if selected.empty:
            raise SchemaError("no valid Top-K signals are available")

        calendar_position = {day: number for number, day in enumerate(calendar)}
        executions: Dict[pd.Timestamp, pd.DataFrame] = {}
        accepted: List[pd.DataFrame] = []
        for signal_date, group in selected.groupby("date", sort=True):
            signal_day = pd.Timestamp(signal_date).normalize()
            position = calendar_position.get(signal_day)
            if position is None or position + 1 >= len(calendar):
                raise SchemaError(
                    "signal date %s has no next benchmark trading date"
                    % signal_day.date()
                )
            execution_day = calendar[position + 1]
            payload = group.copy()
            payload["execution_date"] = execution_day
            executions[execution_day] = payload
            accepted.append(payload)

        benchmark_by_date = benchmark_data.set_index("date")
        benchmark_close = benchmark_by_date[
            self.config.benchmark_close_price_column
        ]
        first_execution = min(executions)
        start_position = calendar_position[first_execution]
        # Strategy capital is first exposed at the execution-day open, so the
        # benchmark uses the same starting instant.  Daily NAV is then marked
        # at the close for both legs.
        benchmark_base = float(
            benchmark_by_date.loc[
                first_execution, self.config.benchmark_open_price_column
            ]
        )
        book = PositionBook()
        cash = float(self.config.initial_cash)
        last_marks: Dict[str, float] = {}
        nav_rows: List[dict] = []
        trade_rows: List[dict] = []
        rejection_rows: List[dict] = []
        position_rows: List[dict] = []

        for day in calendar[start_position:]:
            day_bars = prepared_bars.loc[
                prepared_bars["date"] == day
            ].set_index("symbol", drop=False)
            open_values: Dict[str, float] = {}
            pretrade_value = cash
            for symbol in book.symbols():
                row = day_bars.loc[symbol] if symbol in day_bars.index else None
                raw_open = (
                    np.nan if row is None else row[self.config.open_price_column]
                )
                mark = (
                    last_marks.get(symbol, np.nan)
                    if pd.isna(raw_open)
                    else float(raw_open)
                )
                if not math.isfinite(mark) or mark <= 0.0:
                    raise SchemaError(
                        "held position %s has no usable mark on %s"
                        % (symbol, day.date())
                    )
                open_values[symbol] = mark
                pretrade_value += book.total_shares(symbol) * mark

            daily_buy_notional = 0.0
            daily_sell_notional = 0.0
            daily_cost = 0.0
            daily_trade_count = 0
            signal_date = pd.NaT
            if day in executions:
                orders = executions[day].sort_values("rank", kind="mergesort")
                signal_date = pd.Timestamp(orders["date"].iloc[0])
                order_lookup = orders.set_index("symbol")
                target_shares: Dict[str, int] = {}
                for symbol in sorted(set(order_lookup.index)):
                    row = day_bars.loc[symbol] if symbol in day_bars.index else None
                    price = (
                        np.nan if row is None else row[self.config.open_price_column]
                    )
                    if pd.isna(price) or float(price) <= 0.0:
                        target_shares[symbol] = book.total_shares(symbol)
                    else:
                        weight = float(order_lookup.loc[symbol, "target_weight"])
                        lots = math.floor(
                            pretrade_value
                            * weight
                            / float(price)
                            / self.config.board_lot
                        )
                        target_shares[symbol] = int(lots * self.config.board_lot)

                for symbol in book.symbols():
                    current = book.total_shares(symbol)
                    requested = max(current - target_shares.get(symbol, 0), 0)
                    if requested == 0:
                        continue
                    metadata = (
                        order_lookup.loc[symbol]
                        if symbol in order_lookup.index
                        else None
                    )
                    executed = self._execute_order(
                        book=book,
                        day_bars=day_bars,
                        trade_rows=trade_rows,
                        rejection_rows=rejection_rows,
                        signal_date=signal_date,
                        execution_date=day,
                        symbol=symbol,
                        side="sell",
                        requested_shares=requested,
                        cash=cash,
                        metadata=metadata,
                    )
                    if executed is not None:
                        _, notional, breakdown, cash = executed
                        daily_sell_notional += notional
                        daily_cost += breakdown.total
                        daily_trade_count += 1

                for metadata in orders.itertuples(index=False):
                    symbol = str(metadata.symbol)
                    current = book.total_shares(symbol)
                    requested = max(target_shares.get(symbol, current) - current, 0)
                    if requested == 0:
                        # A selected, unheld symbol with no opening price must
                        # still leave an auditable rejection rather than vanish.
                        if current == 0 and (
                            symbol not in day_bars.index
                            or pd.isna(
                                day_bars.loc[symbol, self.config.open_price_column]
                            )
                        ):
                            self._record_rejection(
                                rejection_rows,
                                signal_date,
                                day,
                                symbol,
                                "buy",
                                metadata.score,
                                metadata.rank,
                                self.config.board_lot,
                                self.config.board_lot,
                                "missing_open",
                            )
                        continue
                    executed = self._execute_order(
                        book=book,
                        day_bars=day_bars,
                        trade_rows=trade_rows,
                        rejection_rows=rejection_rows,
                        signal_date=signal_date,
                        execution_date=day,
                        symbol=symbol,
                        side="buy",
                        requested_shares=requested,
                        cash=cash,
                        metadata=pd.Series(metadata._asdict()),
                    )
                    if executed is not None:
                        _, notional, breakdown, cash = executed
                        daily_buy_notional += notional
                        daily_cost += breakdown.total
                        daily_trade_count += 1

            portfolio_value = cash
            marks: Dict[str, float] = {}
            for symbol in book.symbols():
                row = day_bars.loc[symbol] if symbol in day_bars.index else None
                raw_close = (
                    np.nan if row is None else row[self.config.close_price_column]
                )
                mark = (
                    last_marks.get(symbol, open_values.get(symbol, np.nan))
                    if pd.isna(raw_close)
                    else float(raw_close)
                )
                if not math.isfinite(mark) or mark <= 0.0:
                    raise SchemaError(
                        "held position %s has no usable close mark on %s"
                        % (symbol, day.date())
                    )
                marks[symbol] = mark
                portfolio_value += book.total_shares(symbol) * mark
            last_marks = marks
            turnover = (
                max(daily_buy_notional, daily_sell_notional) / pretrade_value
                if pretrade_value > 0
                else 0.0
            )
            benchmark_nav = float(benchmark_close.loc[day]) / benchmark_base
            strategy_nav = portfolio_value / float(self.config.initial_cash)
            for symbol in book.symbols():
                shares = book.total_shares(symbol)
                market_value = shares * marks[symbol]
                position_rows.append(
                    {
                        "date": day,
                        "symbol": symbol,
                        "shares": shares,
                        "sellable_shares": book.sellable_shares(
                            symbol,
                            day,
                            t_plus_one=self.config.t_plus_one,
                        ),
                        "mark_price": marks[symbol],
                        "market_value": market_value,
                        "weight": (
                            market_value / portfolio_value
                            if portfolio_value > 0
                            else np.nan
                        ),
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
                    "cash": cash,
                    "strategy_return": (
                        strategy_nav - 1.0
                        if not nav_rows
                        else strategy_nav / nav_rows[-1]["strategy_nav"] - 1.0
                    ),
                    "benchmark_return": (
                        benchmark_nav - 1.0
                        if not nav_rows
                        else benchmark_nav / nav_rows[-1]["benchmark_nav"] - 1.0
                    ),
                    "transaction_cost": daily_cost,
                    "transaction_cost_rate": (
                        daily_cost / pretrade_value if pretrade_value > 0 else 0.0
                    ),
                    "turnover": turnover,
                    "holdings": len(book.symbols()),
                    "executed_orders": daily_trade_count,
                }
            )

        return ExecutionBacktestResult(
            nav=pd.DataFrame(nav_rows),
            trades=pd.DataFrame(trade_rows, columns=_TRADE_COLUMNS),
            positions=pd.DataFrame(position_rows, columns=_POSITION_COLUMNS),
            rejections=pd.DataFrame(rejection_rows, columns=_REJECTION_COLUMNS),
            signals=pd.concat(accepted, ignore_index=True)
            .sort_values(["date", "rank", "symbol"], kind="mergesort")
            .reset_index(drop=True),
        )

    @staticmethod
    def _record_rejection(
        rows: List[dict],
        signal_date: pd.Timestamp,
        execution_date: pd.Timestamp,
        symbol: str,
        side: str,
        score: object,
        rank: object,
        requested_shares: int,
        rejected_shares: int,
        reason: str,
    ) -> None:
        if rejected_shares <= 0:
            return
        rows.append(
            {
                "signal_date": signal_date,
                "execution_date": execution_date,
                "symbol": symbol,
                "side": side,
                "score": score,
                "rank": rank,
                "requested_shares": requested_shares,
                "rejected_shares": int(rejected_shares),
                "reason": reason,
            }
        )

    def _execute_order(
        self,
        *,
        book: PositionBook,
        day_bars: pd.DataFrame,
        trade_rows: List[dict],
        rejection_rows: List[dict],
        signal_date: pd.Timestamp,
        execution_date: pd.Timestamp,
        symbol: str,
        side: str,
        requested_shares: int,
        cash: float,
        metadata: Optional[pd.Series],
    ) -> Optional[Tuple[int, float, FeeBreakdown, float]]:
        rank = np.nan if metadata is None else metadata.get("rank", np.nan)
        score = np.nan if metadata is None else metadata.get("score", np.nan)

        def reject(quantity: int, reason: str) -> None:
            self._record_rejection(
                rejection_rows,
                signal_date,
                execution_date,
                symbol,
                side,
                score,
                rank,
                requested_shares,
                quantity,
                reason,
            )

        if symbol not in day_bars.index:
            reject(requested_shares, "missing_bar")
            return None
        row = day_bars.loc[symbol]
        if pd.isna(row["trade_status"]) or pd.isna(row["is_st"]):
            raise SchemaError(
                "execution status is missing for %s/%s"
                % (execution_date.date(), symbol)
            )
        if _is_suspended(row, self.config.reject_suspended):
            reject(requested_shares, "suspended")
            return None
        price = row[self.config.open_price_column]
        if pd.isna(price) or float(price) <= 0.0:
            reject(requested_shares, "missing_open")
            return None
        previous_close = row["_previous_close"]
        if pd.isna(previous_close) or float(previous_close) <= 0.0:
            reject(requested_shares, "missing_previous_close")
            return None
        blocked, _ = self.price_limit_schedule.blocks(
            symbol,
            execution_date,
            side,
            float(price),
            float(previous_close),
            is_st=_is_st(row.get("is_st", False)),
            tolerance_bps=self.config.price_limit_tolerance_bps,
        )
        if blocked:
            reject(requested_shares, "%s_price_limit" % side)
            return None

        quantity = int(requested_shares)
        if side == "sell":
            sellable = book.sellable_shares(
                symbol,
                execution_date,
                t_plus_one=self.config.t_plus_one,
            )
            quantity = min(quantity, sellable)
            reject(requested_shares - quantity, "t_plus_one")
        else:
            lot_rounded = quantity // self.config.board_lot * self.config.board_lot
            reject(quantity - lot_rounded, "board_lot")
            quantity = _affordable_quantity(
                cash,
                float(price),
                lot_rounded,
                self.config.board_lot,
                execution_date,
                self.fee_schedule,
            )
            reject(lot_rounded - quantity, "cash_constraint")
        if quantity <= 0:
            return None

        notional = float(quantity * float(price))
        breakdown = self.fee_schedule.calculate(execution_date, side, notional)
        if side == "sell":
            book.sell(
                symbol,
                execution_date,
                quantity,
                t_plus_one=self.config.t_plus_one,
            )
            cash_after = cash + notional - breakdown.total
        else:
            required_cash = notional + breakdown.total
            if required_cash > cash + 1e-8:  # pragma: no cover
                raise ConfigurationError("buy order exceeds available cash")
            book.buy(symbol, execution_date, quantity)
            cash_after = cash - required_cash
        trade_rows.append(
            {
                "signal_date": signal_date,
                "execution_date": execution_date,
                "symbol": symbol,
                "side": side,
                "score": score,
                "rank": rank,
                "requested_shares": requested_shares,
                "executed_shares": quantity,
                "execution_price": float(price),
                "trade_notional": notional,
                **breakdown.to_dict(),
                "cash_after": cash_after,
            }
        )
        return quantity, notional, breakdown, cash_after


def run_execution_backtest(
    scores: pd.DataFrame,
    bars: pd.DataFrame,
    benchmark: pd.DataFrame,
    config: Optional[ExecutionBacktestConfig] = None,
    fee_schedule: Optional[FeeSchedule] = None,
    price_limit_schedule: Optional[PriceLimitSchedule] = None,
) -> ExecutionBacktestResult:
    return AShareExecutionBacktester(
        config=config,
        fee_schedule=fee_schedule,
        price_limit_schedule=price_limit_schedule,
    ).run(scores, bars, benchmark)


__all__ = [
    "AShareExecutionBacktester",
    "ExecutionBacktestConfig",
    "ExecutionBacktestResult",
    "PositionBook",
    "run_execution_backtest",
]
