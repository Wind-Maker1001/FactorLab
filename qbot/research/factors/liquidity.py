"""Turnover, volume and Amihud liquidity factors."""

import numpy as np
import pandas as pd

from .base import Factor, daily_return, rolling_transform


class TurnoverFactor(Factor):
    required_columns = ("date", "symbol", "turnover")

    def __init__(self, days: int) -> None:
        self.days = int(days)
        self.name = "TURN%d" % self.days
        self.lookback = self.days

    def compute(self, bars: pd.DataFrame) -> pd.Series:
        ordered = self.validate(bars)
        value = rolling_transform(
            ordered["turnover"], ordered["symbol"], self.days, "mean"
        )
        return value.reindex(bars.index).rename(self.name)


class VolumeRatioFactor(Factor):
    name = "VOLUME_RATIO"
    lookback = 20
    required_columns = ("date", "symbol", "volume")

    def compute(self, bars: pd.DataFrame) -> pd.Series:
        ordered = self.validate(bars)
        ma5 = rolling_transform(ordered["volume"], ordered["symbol"], 5, "mean")
        ma20 = rolling_transform(ordered["volume"], ordered["symbol"], 20, "mean")
        value = ma5 / ma20.mask(ma20 == 0)
        return value.replace([np.inf, -np.inf], np.nan).reindex(bars.index).rename(self.name)


class AmihudFactor(Factor):
    name = "AMIHU20"
    lookback = 21
    required_columns = ("date", "symbol", "adj_close", "amount")

    def compute(self, bars: pd.DataFrame) -> pd.Series:
        ordered = self.validate(bars)
        returns = daily_return(ordered["adj_close"], ordered["symbol"]).abs()
        amount = ordered["amount"].where(ordered["amount"] > 0)
        impact = returns / amount * 1.0e8
        impact = impact.replace([np.inf, -np.inf], np.nan)
        value = rolling_transform(impact, ordered["symbol"], 20, "mean")
        return value.reindex(bars.index).rename(self.name)
