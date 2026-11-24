import numpy as np
from v61.strategy import Strategy as Parent


class Strategy(Parent):
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        daily=self.close[self.eligible_symbols()].pct_change(fill_method=None)
        market_var=daily.QQQ.rolling(42).var().replace(0,np.nan)
        cov=daily.rolling(42).cov(daily.QQQ)
        residual=(daily.rolling(42).var()-cov.pow(2).div(market_var,axis=0)).clip(lower=0).pow(.5)
        self.high=residual.gt(residual.quantile(.75,axis=1),axis=0).reindex(columns=self.close.columns,fill_value=False)
        cross=self.close.gt(self.ma28*(1+.5*self.vol))
        self.confirmed=cross&cross.shift(1,fill_value=False)

    def targets(self,previous,prices,valid_prices,held):
        w=super().targets(previous,prices,valid_prices,held)
        return w.mask(held.le(0)&self.high.loc[previous]&~self.confirmed.loc[previous],0.)
