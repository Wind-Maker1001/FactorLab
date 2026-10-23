"""Short-horizon reversal factors."""

import pandas as pd

from .base import Factor, grouped_shift


class ReversalFactor(Factor):
    def __init__(self, days: int) -> None:
        self.days = int(days)
        self.name = "REV%d" % self.days
        self.lookback = self.days

    def compute(self, bars: pd.DataFrame) -> pd.Series:
        ordered = self.validate(bars)
        close = ordered["adj_close"]
        previous = grouped_shift(close, ordered["symbol"], self.days)
        value = -(close / previous - 1.0)
        return value.reindex(bars.index).rename(self.name)
