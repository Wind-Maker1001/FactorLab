import numpy as np
from v125.strategy import Strategy as Growth
from v165.strategy import Strategy as ReservePolicy


class Blend(ReservePolicy):
    blend_rule="half"

    def targets(self,t,prices,valid_prices,held):
        growth=Growth.targets(self,t,prices,valid_prices,held)
        defensive=super().targets(t,prices,valid_prices,held)
        rule=self.blend_rule
        if rule in ("quarter","half","three_quarters"):
            share={"quarter":.25,"half":.50,"three_quarters":.75}[rule]
        elif rule=="breadth":
            b=self.breadth.at[t]
            share=float(np.clip((b-.2)/.6,.20,.80)) if np.isfinite(b) else .50
        elif rule=="trend":
            share=.75 if self.index.at[t]>self.index_long.at[t] and self.breadth.at[t]>=.5 else .25
        elif rule=="downside":
            share=.25 if self.market_down_jump.at[t]>1.25 else .70
        elif rule=="confirmed":
            active=growth.gt(0)
            score=self.confirmed.loc[t].where(active).dropna()
            share=.75 if len(score) and score.mean()>=2/3 else .35
        elif rule=="market_recovery":
            share=.75 if self.breadth_change.at[t]>.10 and self.index.at[t]>self.market_fast.at[t] else .40
        else:
            raise ValueError(rule)
        allocation=growth*share+defensive*(1-share)
        if not np.isfinite(allocation).all() or allocation.lt(0).any() or allocation.sum()>.90000001:
            raise ValueError("Blended exposure invariant failed")
        return allocation
