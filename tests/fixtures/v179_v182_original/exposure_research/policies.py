"""Position budget experiments layered on the unchanged v100 Qlib strategy."""
import numpy as np
from risk_research.parent import tilt_fixed_budget
from v100.strategy import Strategy as V100


class ExposurePolicy(V100):
    scale = 1.0
    core = "none"
    overlay = "none"
    gross_limit = .90

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.index_200 = self.index.rolling(200).mean()
        self.market_vol = self.index.pct_change(fill_method=None).rolling(21).std() * np.sqrt(252)

    def targets(self, previous, prices, valid_prices, held):
        weights = super().targets(previous, prices, valid_prices, held) * self.scale
        market = self.index.at[previous]
        slow = self.index_200.at[previous]
        strong = market > slow
        breadth = self.breadth.at[previous]
        if self.core == "static":
            core_weight = .20
        elif self.core == "trend":
            core_weight = .20 if strong else 0.
        elif self.core == "stepped":
            core_weight = .25 if strong else .05
        elif self.core == "breadth":
            core_weight = .30 if strong and breadth >= .5 else .05
        elif self.core == "volatility":
            volatility = self.market_vol.at[previous]
            core_weight = min(.30, .04 / volatility) if np.isfinite(volatility) and volatility > 0 and strong else .05
        elif self.core == "none":
            core_weight = 0.
        else:
            raise ValueError(self.core)

        if valid_prices["QQQ"] and np.isfinite(slow):
            weights.QQQ += core_weight
        if self.overlay == "relative":
            weights = tilt_fixed_budget(weights, self.score.loc[previous], .25)
        elif self.overlay == "downside":
            risk = self.downside.loc[previous].replace(0, np.nan)
            ranked = -np.log(risk/risk.where(weights.gt(0)).median())
            weights = tilt_fixed_budget(weights, ranked, .35)
        elif self.overlay == "volguard":
            vol = self.market_vol.at[previous]
            if np.isfinite(vol) and vol > .22:
                weights *= max(.55, .22/vol)
        elif self.overlay == "joint":
            weights = tilt_fixed_budget(weights, self.score.loc[previous], .20)
            if not strong and self.market_down_jump.at[previous] > 1.20:
                weights *= .75
        elif self.overlay != "none":
            raise ValueError(self.overlay)
        gross = weights.sum()
        if gross > self.gross_limit:
            weights *= self.gross_limit / gross
        return weights
