"""Test only signals observable before the execution session."""
import numpy as np
import pandas as pd
from v125.strategy import Strategy as Parent


class EntryPolicy(Parent):
    entry_rule="half"

    def __init__(self,adjusted_close,adjusted_volume,adjusted_open,**kwargs):
        super().__init__(adjusted_close,adjusted_volume,adjusted_open,**kwargs)
        crosses=self.close.gt(self.ma28*(1+.5*self.vol))
        ready=self.ma28.notna()&self.vol.notna()
        above=crosses.where(ready)
        self.up3=above.rolling(3).sum()
        self.up5=above.rolling(5).sum()
        side=self.close.gt(self.ma28)
        flip=side.ne(side.shift(1))
        self.flips=flip.where(ready&ready.shift(1,fill_value=False)).rolling(21).sum()
        self.extended=self.close.div(self.ma28).sub(1).div(self.vol.replace(0,np.nan))
        lower=self.close.lt(self.ma28*(1-.5*self.vol))
        self.down2=(lower&lower.shift(1,fill_value=False))
        self.weak5=self.close.pct_change(5,fill_method=None).lt(0)

    def targets(self,t,prices,valid_prices,held):
        target=super().targets(t,prices,valid_prices,held)
        new=held.le(0)&target.gt(0)
        rule=self.entry_rule
        if rule=="half":
            return target.mask(new,.5*target)
        if rule=="three":
            return target.mask(new&self.up3.loc[t].lt(3),.50*target)
        if rule=="majority":
            return target.mask(new&self.up5.loc[t].lt(3),.30*target)
        if rule=="noise":
            return target.mask(new&self.flips.loc[t].ge(4),.45*target)
        if rule=="stretch":
            return target.mask(new&self.extended.loc[t].gt(2.0),.60*target)
        if rule=="cooldown":
            recent=t-self.close.index[max(0,self.close.index.get_loc(t)-10)]
            latest={r["ticker"]:r["date"] for r in self.fills
                    if r["side"]=="sell" and r["quantity"]>0 and r["date"]<=t}
            fast_return=pd.Series({s:s in latest and (t-latest[s])<=recent for s in target.index})
            return target.mask(new&fast_return&self.up3.loc[t].lt(3),.5*target)
        if rule=="fading":
            return target.mask(new&self.weak5.loc[t]&self.relative10.loc[t].lt(0),.40*target)
        if rule=="persistent":
            return target.mask(new&self.up5.loc[t].lt(4)&self.flips.loc[t].ge(3),.35*target)
        if rule=="patient_exit":
            # Preserve all ETF exits and any exit during broad market weakness.
            if self.index.at[t]<=self.market_fast.at[t] or self.breadth.at[t]<.5:
                return target
            value=(held*prices.where(valid_prices)).fillna(0.)
            gross=float(self.trade_position.get_cash()+value.sum())
            if gross<=0 or not np.isfinite(gross):
                raise ValueError("Invalid account")
            candidate=held.gt(0)&target.eq(0)&valid_prices&~self.down2.loc[t]&prices.ge(self.ma63.loc[t])
            candidate.loc[["QQQ","TQQQ"]]=False
            extra=(value/gross).where(candidate,0.)*.35
            room=max(0.,.90-target.sum())
            if extra.sum()>room:
                extra*=room/extra.sum()
            return target+extra
        if rule=="intersection":
            cautious=new&self.up3.loc[t].lt(3)&self.flips.loc[t].ge(3)
            return target.mask(cautious,.45*target)
        raise ValueError(rule)
