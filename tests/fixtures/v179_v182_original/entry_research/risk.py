"""Balance concentration within an existing set of holdings."""
import numpy as np
import pandas as pd


class StockRisk:
    risk_rule="single_cap"

    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.risk_returns=self.close.pct_change(fill_method=None)
        self.semideviation=self.risk_returns.clip(upper=0).pow(2).rolling(63).mean().pow(.5)

    def targets(self,t,prices,valid_prices,held):
        w=super().targets(t,prices,valid_prices,held)
        before=w.copy()
        active=w[w.gt(0)].index
        if len(active)<2:
            return w
        if self.risk_rule=="single_cap":
            for s in active.difference(["QQQ","TQQQ"]):
                w[s]=min(w[s],.15)
        elif self.risk_rule=="tail_budget":
            risk=self.semideviation.loc[t,active]*w.loc[active]
            maximum=risk.sum()*.32
            for s in risk[risk.gt(maximum)&risk.gt(0)].index:
                w[s]*=maximum/risk[s]
        elif self.risk_rule=="correlation_budget":
            recent=self.risk_returns.loc[:t,active].tail(63).dropna()
            if len(recent)<42:
                return w
            cov=recent.cov()
            cov=.65*cov+.35*np.diag(np.diag(cov))
            contribution=w.loc[active]*(cov@w.loc[active])
            total=contribution.sum()
            if total>0:
                for s in contribution[contribution.gt(.40*total)].index:
                    w[s]*=np.sqrt(.40*total/contribution[s])
        elif self.risk_rule=="sector_cap":
            for sector in (["AAPL","SNDK"],["BA","GE"],["C","JPM"]):
                gross=w.loc[sector].sum()
                if gross>.30:
                    w.loc[sector]*=.30/gross
        elif self.risk_rule=="shared_winners":
            risk=self.semideviation.loc[t,active]
            if risk.notna().sum()<3:
                return w
            hot=risk.gt(risk.quantile(.75))
            for s in hot[hot].index:
                w[s]=min(w[s],.14)
        else:
            raise ValueError(self.risk_rule)
        freed=before.sum()-w.sum()
        if freed<1e-12:
            return before
        recipients=w.gt(0)&w.ge(before-1e-12)&w.lt(.22)
        recipients["TQQQ"]=False
        headroom=(.22-w.loc[recipients]).clip(upper=.04)
        if headroom.sum()>0:
            w.loc[recipients]+=headroom*min(1.,freed/headroom.sum())
        if w.sum()>before.sum()+1e-10 or w.lt(0).any():
            raise ValueError("Concentration overlay increased gross budget")
        return w
