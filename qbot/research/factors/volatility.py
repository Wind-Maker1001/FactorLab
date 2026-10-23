"""Historical realised-volatility factors."""

import pandas as pd

from .base import Factor, daily_return, rolling_transform


class VolatilityFactor(Factor):
    def __init__(self, days: int) -> None:
        self.days = int(days)
        self.name = "VOL%d" % self.days
        self.lookback = self.days + 1

    def compute(self, bars: pd.DataFrame) -> pd.Series:
        ordered = self.validate(bars)
        returns = daily_return(ordered["adj_close"], ordered["symbol"])
        value = rolling_transform(returns, ordered["symbol"], self.days, "std")
        return value.reindex(bars.index).rename(self.name)
