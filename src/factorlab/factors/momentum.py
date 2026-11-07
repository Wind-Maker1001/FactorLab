"""Medium-horizon price momentum factors."""

import pandas as pd

from .base import Factor, grouped_shift


class MomentumFactor(Factor):
    def __init__(self, days: int) -> None:
        self.days = int(days)
        self.name = "MOM%d" % self.days
        self.lookback = self.days

    def compute(self, bars: pd.DataFrame) -> pd.Series:
        return self._compute_ordered(self.validate(bars)).reindex(bars.index)

    def _compute_ordered(self, ordered: pd.DataFrame) -> pd.Series:
        close = ordered["adj_close"]
        previous = grouped_shift(close, ordered["symbol"], self.days)
        value = close / previous - 1.0
        return value.rename(self.name)
