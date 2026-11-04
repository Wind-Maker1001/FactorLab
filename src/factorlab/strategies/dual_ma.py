"""Causal close-based crossover signals and a software-only research runner.

The input calendar includes warmup sessions. No pre-start order is carried in.
A genuine crossing on the first research close is eligible; an already bullish
ordering is not an entry. Raw prices execute/mark, adjusted closes signal.
Corporate-action accounting is not implemented: variable adjustment ratios and
nonempty action ledgers are refused. This module grants no financial admission.
"""
from dataclasses import dataclass

import numpy as np
import pandas as pd

from factorlab.backtest.execution import ExecutionBacktestConfig
from factorlab.backtest.fees import FeeSchedule
from factorlab.backtest.target import run_target_positions
from factorlab.data.schema import normalize_symbol
from factorlab.errors import ConfigurationError, SchemaError


def daily_dates(values, label):
    try:
        dates = pd.DatetimeIndex(pd.to_datetime(values, errors="raise"))
    except (TypeError, ValueError) as exc:
        raise SchemaError(label + " contains invalid dates") from exc
    if (dates.hasnans or dates.tz is not None or dates.has_duplicates
            or not dates.equals(dates.normalize()) or not dates.is_monotonic_increasing):
        raise SchemaError(label + " must be unique, ordered, timezone-free daily dates")
    return dates


@dataclass(frozen=True)
class DualMAConfig:
    symbol: str = "601857.SH"
    start: str = "2018-01-01"
    end: str = "2018-12-31"
    short_window: int = 5
    long_window: int = 20
    initial_cash: float = 100000.0
    max_weight: float = 1.0
    board_lot: int = 100
    allow_short: bool = False
    signal_frequency: str = "daily"
    execution_timing: str = "next_open"

    def __post_init__(self):
        object.__setattr__(self, "symbol", normalize_symbol(self.symbol))
        for name in ("short_window", "long_window", "board_lot"):
            value = getattr(self, name)
            if isinstance(value, (bool, np.bool_)) or not isinstance(value, (int, np.integer)) or value < 1:
                raise ConfigurationError(name + " must be a positive integer")
        if self.short_window >= self.long_window:
            raise ConfigurationError("short_window must be smaller than long_window")
        for name in ("initial_cash", "max_weight"):
            value = getattr(self, name)
            if isinstance(value, (bool, np.bool_)) or not isinstance(value, (int, float, np.number)) or not np.isfinite(value) or value <= 0:
                raise ConfigurationError(name + " must be finite and positive")
        if self.max_weight > 1:
            raise ConfigurationError("max_weight must be <= 1")
        if self.allow_short is not False or self.signal_frequency != "daily" or self.execution_timing != "next_open":
            raise ConfigurationError("only long-only daily close / next_open execution is supported")
        try:
            start, end = pd.Timestamp(self.start), pd.Timestamp(self.end)
            valid = (not pd.isna(start) and not pd.isna(end) and start.tzinfo is None
                     and end.tzinfo is None and start == start.normalize()
                     and end == end.normalize() and start <= end)
        except (TypeError, ValueError) as exc:
            raise ConfigurationError("invalid research dates") from exc
        if not valid:
            raise ConfigurationError("invalid research dates")


def prepare_inputs(bars, calendar, config):
    dates = daily_dates(calendar, "calendar")
    if dates.empty:
        raise SchemaError("calendar is empty")
    required = {"date", "symbol", "adj_close", "raw_open", "raw_high", "raw_low",
                "raw_close", "volume", "trade_status", "is_st"}
    if not isinstance(bars, pd.DataFrame) or not required.issubset(bars):
        raise SchemaError("bars missing required columns: " + ", ".join(sorted(required)))
    frame = bars.copy()
    frame["date"] = daily_dates(frame.date, "bars.date")
    if not frame.symbol.map(normalize_symbol).eq(config.symbol).all():
        raise SchemaError("bars must contain exactly the configured security")
    frame["symbol"] = config.symbol
    if not pd.DatetimeIndex(frame.date).equals(dates):
        raise SchemaError("bars must cover every supplied calendar session, including warmup")
    for column in ("adj_close", "raw_open", "raw_high", "raw_low", "raw_close", "volume"):
        raw = frame[column]
        values = pd.to_numeric(raw, errors="coerce")
        if raw.map(lambda x: isinstance(x, (bool, np.bool_))).any() or not np.isfinite(values).all():
            raise SchemaError(column + " must contain finite numeric values")
        if (values < 0 if column == "volume" else values <= 0).any():
            raise SchemaError(column + " contains invalid prices/volume")
        frame[column] = values.astype(float)
    if ((frame.raw_high < frame[["raw_open", "raw_close", "raw_low"]].max(axis=1))
            | (frame.raw_low > frame[["raw_open", "raw_close", "raw_high"]].min(axis=1))).any():
        raise SchemaError("raw OHLC relationship is invalid")
    # Do not infer unknown status as tradable/non-ST.
    for column in ("trade_status", "is_st"):
        if not frame[column].map(lambda x: type(x) in (bool, int, np.bool_, np.int64, np.int32) and x in (0, 1)).all():
            raise SchemaError(column + " requires explicit boolean/0/1 status")
    research = dates[(dates >= pd.Timestamp(config.start)) & (dates <= pd.Timestamp(config.end))]
    if len(research) < 2:
        raise SchemaError("research requires at least two sessions")
    if (dates < research[0]).sum() < config.long_window:
        raise SchemaError("insufficient warmup: require long_window sessions before first research close")
    return frame.reset_index(drop=True), research


def moving_average_signals(bars, calendar, config=None):
    config = config or DualMAConfig()
    frame, research = prepare_inputs(bars, calendar, config)
    short = frame.adj_close.rolling(config.short_window, min_periods=config.short_window).mean()
    long = frame.adj_close.rolling(config.long_window, min_periods=config.long_window).mean()
    buy = (short.shift(1) <= long.shift(1)) & (short > long)
    sell = (short.shift(1) >= long.shift(1)) & (short < long)
    result = frame[["date", "symbol", "adj_close"]].copy()
    result["ma_short"], result["ma_long"] = short, long
    result["signal"] = np.select([buy, sell], ["buy", "sell"], default="hold")
    result["target_weight"] = np.select([buy, sell], [config.max_weight, 0.0], default=np.nan)
    return result.loc[result.date.isin(research)].reset_index(drop=True)


def run_software_backtest(bars, calendar, config=None, *, fee_schedule=None, corporate_actions=None):
    """Low-level SOFTWARE_ONLY calculation, like the existing research engines.

    Never a real-data admission API. The CLI only executes generated synthetic
    inputs. Real consumers must independently satisfy existing formal/execution
    admission, which currently refuses. No corporate-action total return claim.
    """
    config = config or DualMAConfig()
    frame, research = prepare_inputs(bars, calendar, config)
    if corporate_actions is not None and len(corporate_actions):
        raise SchemaError("corporate-action execution ledger is unsupported")
    ratio = frame.adj_close / frame.raw_close
    if not np.allclose(ratio, ratio.iloc[0], rtol=1e-10, atol=1e-12):
        raise SchemaError("corporate-action/variable adjustment ratio requires qualified execution ledger")
    signals = moving_average_signals(frame, calendar, config)
    execution = ExecutionBacktestConfig(initial_cash=config.initial_cash, board_lot=config.board_lot)
    fees = fee_schedule or FeeSchedule.a_share_default()
    result = run_target_positions(signals, frame, research, execution, fees)
    benchmark_targets = signals.copy()
    benchmark_targets["target_weight"] = np.nan
    # Known buy-and-hold mandate before the first research open; no price signal.
    benchmark_targets.loc[0, "target_weight"] = config.max_weight
    benchmark = run_target_positions(benchmark_targets, frame, research, execution, fees, buy_at_first_open=True)
    return {"signals": signals, "strategy": result, "benchmark": benchmark,
            "scope": "SOFTWARE_ONLY / NOT FINANCIAL EVIDENCE", "financial_admission": "FORMAL_ADMISSION_BLOCKED"}
