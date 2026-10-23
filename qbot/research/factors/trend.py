"""Moving-average trend factors."""

import numpy as np
import pandas as pd

from .base import Factor, rolling_transform


class TrendFactor(Factor):
    def __init__(self, name: str) -> None:
        supported = {"PRICE_MA20", "MA5_MA20", "MA20_MA60"}
        if name not in supported:
            raise ValueError("Unsupported trend factor %s" % name)
        self.name = name
        self.lookback = 60 if name == "MA20_MA60" else 20

    def compute(self, bars: pd.DataFrame) -> pd.Series:
        ordered = self.validate(bars)
        close = ordered["adj_close"]
        symbols = ordered["symbol"]
        ma20 = rolling_transform(close, symbols, 20, "mean")
        if self.name == "PRICE_MA20":
            value = close / ma20.mask(ma20 == 0) - 1.0
        elif self.name == "MA5_MA20":
            ma5 = rolling_transform(close, symbols, 5, "mean")
            value = ma5 / ma20.mask(ma20 == 0) - 1.0
        else:
            ma60 = rolling_transform(close, symbols, 60, "mean")
            value = ma20 / ma60.mask(ma60 == 0) - 1.0
        return value.replace([np.inf, -np.inf], np.nan).reindex(bars.index).rename(self.name)
