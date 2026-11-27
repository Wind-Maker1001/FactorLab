"""Risk overlays and an execution-aligned monthly return forecaster."""
import numpy as np
import pandas as pd
from sklearn.linear_model import Ridge
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from risk_research.parent import tilt_fixed_budget


def fit_scores(close, opening, names, breadth):
    prices=close[names]
    op=opening[names]
    returns=prices.pct_change(fill_method=None)
    market=returns.QQQ
    variance=market.rolling(63).var().replace(0,np.nan)
    beta=returns.rolling(63).cov(market).div(variance,axis=0).clip(-1,4)
    residual=returns-beta.mul(market,axis=0)
    risk=residual.rolling(42).std().clip(lower=.003)
    future=op.shift(-6).div(op.shift(-1)).sub(1)
    label=(future-beta.mul(future.QQQ,axis=0)).div(risk*np.sqrt(5)).clip(-3,3)
    features={
        "relative_short":residual.rolling(5).sum().div(risk*np.sqrt(5)),
        "relative_medium":residual.rolling(42).sum().div(risk*np.sqrt(42)),
        "trend":prices.div(prices.rolling(63).mean()).sub(1),
        "volatility":risk,
    }
    blocks=[]
    for name in names:
        if name in ("QQQ","TQQQ"):
            continue
        panel=pd.DataFrame({key:frame[name] for key,frame in features.items()})
        panel=panel.assign(symbol=name,breadth=breadth,market21=prices.QQQ.pct_change(21,fill_method=None),y=label[name])
        blocks.append(panel)
    columns=[*features,"breadth","market21"]
    data=pd.concat(blocks).replace([np.inf,-np.inf],np.nan).dropna(subset=columns).sort_index()
    forecasts=pd.DataFrame(np.nan,index=close.index,columns=close.columns)
    periods=close.index.to_period("M")
    for month in periods.unique():
        dates=close.index[periods==month]
        i=close.index.get_loc(dates[0])
        if i<132:
            continue
        train=data.loc[close.index[max(i-509,0)]:close.index[i-6]].dropna(subset=["y"])
        predict=data.loc[dates[0]:dates[-1]]
        if train.index.nunique()<126 or predict.empty:
            continue
        model=make_pipeline(StandardScaler(),Ridge(alpha=80))
        model.fit(train[columns],train.y)
        wide=predict[["symbol"]].assign(prediction=model.predict(predict[columns])).set_index("symbol",append=True).prediction.unstack()
        forecasts.loc[wide.index,wide.columns]=wide
    return forecasts


class Refinement:
    defence="none"
    allocation="none"

    def __init__(self, adjusted_close, adjusted_volume, adjusted_open, **kw):
        super().__init__(adjusted_close,adjusted_volume,adjusted_open,**kw)
        self.market_dd=self.index.div(self.index.rolling(252,min_periods=126).max()).sub(1)
        self.market_trend=self.index.gt(self.index.rolling(126).mean()).rolling(10).mean()
        self.trend_qqq=self.close.QQQ.div(self.close.QQQ.rolling(126).mean()).sub(1)
        ids=self.eligible_symbols()
        ret=self.close[ids].pct_change(fill_method=None)
        self.recent_tail=ret.clip(upper=0).pow(2).rolling(21).mean().pow(.5)
        if self.allocation in ("ridge","consensus","blend"):
            self.open_score=fit_scores(self.close,adjusted_open,ids,self.breadth)

    def targets(self,t,prices,valid_prices,held):
        w=super().targets(t,prices,valid_prices,held)
        dd=self.market_dd.at[t]
        trend=self.market_trend.at[t]
        if self.defence=="crisis":
            if dd<-.15 and trend<.25:
                w*=.55
        elif self.defence=="smooth":
            if np.isfinite(dd) and np.isfinite(trend):
                severity=np.clip((-dd-.06)/.20,0,1)*(1-trend)
                w*=1-.55*severity
        elif self.defence=="growth":
            if self.trend_qqq.at[t]<0 and dd<-.08:
                w.loc[["QQQ","TQQQ"]]*=.50
        elif self.defence=="cluster":
            # A target cap on JPM + C and QQQ + three daily TQQQ exposures.
            for names,units,limit in [(("JPM","C"),(1.,1.),.28),(("QQQ","TQQQ"),(1.,3.),.36)]:
                equivalent=float(np.dot(w.loc[list(names)],units))
                if equivalent>limit:
                    w.loc[list(names)]*=limit/equivalent
        elif self.defence=="recovery":
            if dd<-.10 and self.breadth.at[t]<.5 and trend<.5:
                w*=.65
        elif self.defence!="none":
            raise ValueError(self.defence)

        stocks=w.drop(["QQQ","TQQQ"])
        if self.allocation=="relative":
            w.loc[stocks.index]=tilt_fixed_budget(stocks,self.score.loc[t,stocks.index],.30)
        elif self.allocation=="tail":
            risk=self.recent_tail.loc[t].reindex(stocks.index)
            rank=-np.log(risk/risk.where(stocks.gt(0)).median()).replace([np.inf,-np.inf],np.nan)
            w.loc[stocks.index]=tilt_fixed_budget(stocks,rank,.40)
        elif self.allocation in ("ridge","consensus","blend"):
            raw=self.open_score.loc[t,stocks.index]
            eligible=stocks.gt(0)&raw.notna()
            # Cross-sectional rank on tradable active stocks at the prior close.
            if eligible.sum()>=2:
                rank=2*(raw[eligible].rank(method="average")-1)/(eligible.sum()-1)-1
                strength=self.score.loc[t,stocks.index]
                if self.allocation=="consensus":
                    rank=rank.where(np.sign(rank).eq(np.sign(strength.reindex(rank.index))),0.)
                elif self.allocation=="blend":
                    rank=.5*rank+.5*strength.clip(-1,1)
                w.loc[stocks.index]=tilt_fixed_budget(stocks,rank.reindex(stocks.index),.30)
        elif self.allocation!="none":
            raise ValueError(self.allocation)
        if (w < -1e-12).any() or w.sum() > .900000001:
            raise ValueError("Unexpected nominal exposure")
        return w
