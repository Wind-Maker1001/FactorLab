"""Causal daily long-only indicator strategies; targets are intents, not fills."""
from dataclasses import dataclass

import numpy as np
import pandas as pd

from factorlab.errors import ConfigurationError, SchemaError
from factorlab.strategies.dual_ma import DualMAConfig, prepare_inputs


def _window(name, value):
    if isinstance(value, (bool, np.bool_)) or not isinstance(value, (int, np.integer)) or value < 2:
        raise ConfigurationError(name + " must be an integer >= 2")


def _positive(name, value):
    if isinstance(value, (bool, np.bool_)) or not isinstance(value, (int, float, np.number)) or not np.isfinite(value) or value <= 0:
        raise ConfigurationError(name + " must be finite and positive")


@dataclass(frozen=True)
class DailyStrategyConfig:
    symbol: str = "601857.SH"
    start: str = "2018-01-01"
    end: str = "2018-12-31"
    initial_cash: float = 100000.0
    max_weight: float = 1.0
    board_lot: int = 100
    allow_short: bool = False
    signal_frequency: str = "daily"
    execution_timing: str = "next_open"

    def __post_init__(self):
        validated = DualMAConfig(symbol=self.symbol, start=self.start, end=self.end,
            short_window=1, long_window=self.long_window, initial_cash=self.initial_cash,
            max_weight=self.max_weight, board_lot=self.board_lot, allow_short=self.allow_short,
            signal_frequency=self.signal_frequency, execution_timing=self.execution_timing)
        object.__setattr__(self, "symbol", validated.symbol)


@dataclass(frozen=True)
class BollingerConfig(DailyStrategyConfig):
    window: int = 20
    deviations: float = 2.0
    mode: str = "mean_reversion"

    @property
    def long_window(self):
        return self.window

    def __post_init__(self):
        _window("window", self.window)
        _positive("deviations", self.deviations)
        if self.mode not in {"mean_reversion", "breakout"}:
            raise ConfigurationError("mode must be mean_reversion or breakout")
        super().__post_init__()


@dataclass(frozen=True)
class RSIConfig(DailyStrategyConfig):
    window: int = 14
    oversold: float = 30.0
    overbought: float = 70.0
    exit_rule: str = "overbought"

    @property
    def long_window(self):
        return self.window + 1

    def __post_init__(self):
        _window("window", self.window)
        for name in ("oversold", "overbought"):
            _positive(name, getattr(self, name))
        if not 0 < self.oversold < 50 < self.overbought < 100:
            raise ConfigurationError("require 0 < oversold < 50 < overbought < 100")
        if self.exit_rule not in {"overbought", "midline"}:
            raise ConfigurationError("exit_rule must be overbought or midline")
        super().__post_init__()


@dataclass(frozen=True)
class MACDConfig(DailyStrategyConfig):
    fast: int = 12
    slow: int = 26
    signal_window: int = 9
    zero_filter: bool = False

    @property
    def long_window(self):
        return self.slow + self.signal_window - 1

    def __post_init__(self):
        for name in ("fast", "slow", "signal_window"):
            _window(name, getattr(self, name))
        if self.fast >= self.slow:
            raise ConfigurationError("fast must be smaller than slow")
        if type(self.zero_filter) is not bool:
            raise ConfigurationError("zero_filter must be boolean")
        super().__post_init__()


@dataclass(frozen=True)
class CTAConfig(DailyStrategyConfig):
    entry_window: int = 20
    exit_window: int = 10
    atr_window: int = 14
    trend_window: int = 50
    atr_multiple: float = 3.0
    risk_fraction: float = 0.01

    @property
    def long_window(self):
        return max(self.entry_window, self.exit_window, self.atr_window + 1, self.trend_window)

    def __post_init__(self):
        for name in ("entry_window", "exit_window", "atr_window", "trend_window"):
            _window(name, getattr(self, name))
        for name in ("atr_multiple", "risk_fraction"):
            _positive(name, getattr(self, name))
        if self.risk_fraction > 1:
            raise ConfigurationError("risk_fraction must be <= 1")
        super().__post_init__()


def wilder_average(values, window):
    """Arithmetic seed of n consecutive values, then alpha=1/n; gaps reset seed."""
    _window("window", window)
    values = pd.Series(values, dtype=float)
    if np.isinf(values).any():
        raise SchemaError("indicator observations cannot contain infinity")
    output = np.full(len(values), np.nan)
    seed, state = [], np.nan
    for i, value in enumerate(values):
        if pd.isna(value):
            seed, state = [], np.nan
        elif pd.isna(state):
            seed.append(value)
            if len(seed) == window:
                state = float(np.mean(seed))
                output[i] = state
        else:
            state += (value - state) / window
            output[i] = state
    return pd.Series(output, index=values.index)


def wilder_rsi(close, window=14):
    """n changes seed RSI; flat=50, only gains=100, only losses=0; gaps reset."""
    close = pd.Series(close, dtype=float)
    delta = close.diff()
    gain = wilder_average(delta.clip(lower=0), window)
    loss = wilder_average(-delta.clip(upper=0), window)
    denominator = gain + loss
    result = 100 * gain / denominator.where(denominator.ne(0))
    return result.mask(denominator.eq(0), 50.0)


def seeded_ema(values, window):
    """SMA seed after n consecutive observations, then alpha=2/(n+1)."""
    _window("window", window)
    values = pd.Series(values, dtype=float)
    if np.isinf(values).any():
        raise SchemaError("indicator observations cannot contain infinity")
    output = np.full(len(values), np.nan)
    seed, state = [], np.nan
    for i, value in enumerate(values):
        if pd.isna(value):
            seed, state = [], np.nan
        elif pd.isna(state):
            seed.append(value)
            if len(seed) == window:
                state = float(np.mean(seed))
                output[i] = state
        else:
            state += 2 * (value - state) / (window + 1)
            output[i] = state
    return pd.Series(output, index=values.index)


def _up(left, right):
    return (left.shift(1) <= right.shift(1)) & (left > right)


def _down(left, right):
    return (left.shift(1) >= right.shift(1)) & (left < right)


def technical_signals(bars, calendar, config, *, signal_price_column="adj_close"):
    """One row per research close; NaN holds intent, zero cancels/liquidates.

    No warmup intent is carried into research. CTA trailing stops track the
    entry *intent* close, not an assumed fill or an intraday execution price.
    signal_price_column selects raw_close or the default adj_close explicitly;
    raw mode uses raw OHLC without consuming or aliasing adjustment fields.
    """
    if type(config) not in {BollingerConfig, RSIConfig, MACDConfig, CTAConfig}:
        raise ConfigurationError("unsupported technical strategy config")
    frame, research = prepare_inputs(bars, calendar, config, signal_price_column=signal_price_column)
    result = frame[["date", "symbol", signal_price_column]].copy()
    close = frame[signal_price_column]
    if isinstance(config, BollingerConfig):
        middle = close.rolling(config.window).mean()
        std = close.rolling(config.window).std(ddof=0)
        upper, lower = middle + config.deviations * std, middle - config.deviations * std
        result["middle"], result["upper"], result["lower"] = middle, upper, lower
        result["upper_breakout"], result["lower_breakout"] = _up(close, upper), _down(close, lower)
        result["lower_reentry"], result["upper_reentry"] = _up(close, lower), _down(close, upper)
        if config.mode == "mean_reversion":
            buy, sell = result.lower_reentry, close >= middle
        else:
            buy, sell = result.upper_breakout, close < middle
    elif isinstance(config, RSIConfig):
        rsi = wilder_rsi(close, config.window)
        result["rsi"] = rsi
        buy = _up(rsi, pd.Series(config.oversold, index=close.index))
        exit_level = config.overbought if config.exit_rule == "overbought" else 50.0
        sell = _down(rsi, pd.Series(exit_level, index=close.index))
    elif isinstance(config, MACDConfig):
        fast, slow = seeded_ema(close, config.fast), seeded_ema(close, config.slow)
        dif = fast - slow
        dea = seeded_ema(dif, config.signal_window)
        result["ema_fast"], result["ema_slow"] = fast, slow
        result["dif"], result["dea"], result["histogram"] = dif, dea, 2 * (dif - dea)
        buy, sell = _up(dif, dea), _down(dif, dea)
        if config.zero_filter:
            buy &= dif > 0
            sell |= dif < 0
    else:
        if signal_price_column == "raw_close":
            high, low = frame.raw_high, frame.raw_low
        else:
            ratio = close / frame.raw_close
            high, low = frame.raw_high * ratio, frame.raw_low * ratio
        previous = close.shift(1)
        tr = pd.concat([high - low, (high - previous).abs(), (low - previous).abs()], axis=1).max(axis=1)
        tr.iloc[0] = np.nan
        atr = wilder_average(tr, config.atr_window)
        upper = high.shift(1).rolling(config.entry_window).max()
        lower = low.shift(1).rolling(config.exit_window).min()
        trend = close.rolling(config.trend_window).mean()
        result["channel_upper"], result["channel_lower"] = upper, lower
        result["atr"], result["trend"] = atr, trend
        buy, sell = (close > upper) & (close > trend) & (atr > 0), (close < lower) | (close < trend)
    result["signal"], result["target_weight"] = "hold", np.nan
    result["exit_reason"] = ""
    # TODO: Qualified event/fill feedback is required for fill-anchored intraday stops.
    active, stop = False, np.nan
    result["stop_level"] = np.nan
    for i in result.index[result.date.isin(research)]:
        stopped = isinstance(config, CTAConfig) and active and close.iloc[i] <= stop
        if bool(sell.iloc[i]) or stopped:
            # Explicit zero also cancels an entry rejected at an earlier open.
            result.loc[i, ["signal", "target_weight", "exit_reason"]] = ["sell", 0.0, "atr_stop" if stopped else "rule_exit"]
            active, stop = False, np.nan
        elif bool(buy.iloc[i]) and not active:
            weight = config.max_weight
            if isinstance(config, CTAConfig):
                distance = config.atr_multiple * atr.iloc[i]
                weight = min(weight, config.risk_fraction * close.iloc[i] / distance)
                stop = close.iloc[i] - distance
            result.loc[i, ["signal", "target_weight"]] = ["buy", weight]
            active = True
        if isinstance(config, CTAConfig) and active:
            # Today's updated trailing stop can first trigger on a later close.
            stop = max(stop, close.iloc[i] - config.atr_multiple * atr.iloc[i])
            result.loc[i, "stop_level"] = stop
    return result.loc[result.date.isin(research)].reset_index(drop=True)
