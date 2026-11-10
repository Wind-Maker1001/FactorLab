"""Independent close-based moving-average crossover example."""
import numpy as np


class MyStrategy:
    required_fields = ("adj_close",)

    def __init__(self, window: int = 20):
        if window < 2:
            raise ValueError("window must be >= 2")
        self.window = window
        self.warmup_sessions = window

    def generate_targets(self, bars, calendar):
        average = bars.adj_close.rolling(self.window).mean()
        above = bars.adj_close > average
        buy = above & ~above.shift(1, fill_value=False)
        sell = ~above & above.shift(1, fill_value=False)
        targets = bars[["date", "symbol"]].copy()
        targets["target_weight"] = np.select([buy, sell], [1.0, 0.0], default=np.nan)
        return targets.tail(1)


class BuiltinDualMA:
    """Adapt the existing vectorized dual-MA implementation to prefix calls."""
    required_fields = ("adj_close", "raw_open", "raw_close", "raw_high", "raw_low",
                       "volume", "trade_status", "is_st")

    def __init__(self, window: int = 20):
        if window < 2:
            raise ValueError("window must be >= 2")
        self.window = window
        self.warmup_sessions = window + 1

    def generate_targets(self, bars, calendar):
        from factorlab.strategies.dual_ma import DualMAConfig, moving_average_signals
        config = DualMAConfig(symbol=bars.symbol.iloc[0],
            start=str(calendar[self.window].date()), end=str(calendar[-1].date()),
            short_window=1, long_window=self.window)
        return moving_average_signals(bars, calendar, config).tail(1)
