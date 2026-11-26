"""Invest a share of the remaining capital only after observing last session's data."""
import numpy as np


class Reserve:
    reserve_rule="fixed"
    limit=.90

    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.index_long=self.index.rolling(200).mean()
        self.rebound=self.index.div(self.index.rolling(21).min()).sub(1)
        self.dispersion=self.close[self.eligible_symbols()].pct_change(21,fill_method=None).std(axis=1)
        self.dispersion_mid=self.dispersion.rolling(126).median()

    def targets(self,t,prices,valid_prices,held):
        w=super().targets(t,prices,valid_prices,held)
        total=w.sum()
        if total<=0 or total>=self.limit-1e-10:
            return w
        room=self.limit-total
        up=self.index.at[t]>self.index_long.at[t]
        breadth=self.breadth.at[t]
        mode=self.reserve_rule
        if mode=="fixed":
            add=min(room,.10*total)
        elif mode=="headroom":
            add=min(room*.25,.12)
        elif mode=="trend":
            add=min(room*.40,.15) if up else 0.
        elif mode=="width":
            add=min(room*.50,.15) if up and breadth>=.5 else 0.
        elif mode=="continuation":
            add=min(room*.55,.18) if up and self.index.at[t]>self.market_fast.at[t] and breadth>=.5 else 0.
        elif mode=="rebound":
            add=min(room*.40,.12) if self.rebound.at[t]>.05 and self.breadth_change.at[t]>.15 else 0.
        elif mode=="dispersion":
            add=min(room*.40,.15) if up and self.dispersion.at[t]>self.dispersion_mid.at[t] and breadth>=.5 else 0.
        elif mode=="quality":
            add=min(room*.50,.15) if up and breadth>=.5 else 0.
        elif mode=="seasoned":
            add=min(room*.40,.15) if up else 0.
        elif mode=="breadth_slope":
            quality=np.clip((breadth-.30)/.45,0,1) if np.isfinite(breadth) else 0.
            add=min(room*.50,.18)*quality if up else 0.
        else:
            raise ValueError(mode)
        if add<=0:
            return w
        # Restore risk to established positions without overriding reduced initial positions
        # when the variant explicitly requires confirmation.
        eligible=w.gt(0)
        if mode in ("quality","seasoned"):
            eligible &= held.gt(0)
        if mode=="quality":
            eligible &= self.relative10.loc[t].gt(0)&self.score.loc[t].gt(0)
        recipients=w.where(eligible,0.)
        if recipients.sum()<=0:
            return w
        extra=recipients*(add/recipients.sum())
        # Put a separate ceiling on incremental exposure per name, including leveraged ETF.
        extra=extra.clip(upper=.04)
        extra.TQQQ=min(extra.TQQQ,.012)
        out=w+extra
        if out.sum()>self.limit+1e-10 or out.lt(0).any():
            raise ValueError("Reserve exceeded")
        return out
