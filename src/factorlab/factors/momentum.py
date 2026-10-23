"""Medium-horizon price momentum factors."""

import pandas as pd

from .base import Factor, grouped_shift


class MomentumFactor(Factor):
    def __init__(self, days: int) -> None:
        self.days = int(days)
        self.name = "MOM%d" % self.days
        self.lookback = self.days

    def compute(self, bars: pd.DataFrame) -> pd.Series:
        ordered = self.validate(bars)
        close = ordered["adj_close"]
        previous = grouped_shift(close, ordered["symbol"], self.days)
        value = close / previous - 1.0
        return value.reindex(bars.index).rename(self.name)
