"""Causal portfolio policies for v86–v100, preserving the original v69 execution."""
import numpy as np
import pandas as pd

from risk_research.parent import tilt_fixed_budget
from v48_strategy import TARGET
from v69.strategy import Strategy as V69


def predict_residual(close, residual, score, risk, breadth, market):
    from sklearn.linear_model import Ridge
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler

    forward = close.shift(-5).div(close).sub(1)
    benchmark_forward = market.shift(-5).div(market).sub(1)
    rr = close.pct_change(fill_method=None)
    mm = market.pct_change(fill_method=None)
    vv = mm.rolling(63).var().replace(0,np.nan)
    beta = rr.rolling(63).cov(mm).div(vv,axis=0)
    label = (forward - beta.mul(benchmark_forward, axis=0)).div(risk*np.sqrt(5))
    blocks = []
    market21 = market.pct_change(21,fill_method=None)
    for symbol in close:
        trend = close[symbol].div(close[symbol].rolling(28).mean()).sub(1)
        blocks.append(pd.DataFrame({
            "ticker":symbol, "momentum":score[symbol],"trend":trend,
            "relative5":residual[symbol].rolling(5).sum(),
            "risk":risk[symbol],"breadth":breadth,"market21":market21,"label":label[symbol],
        }))
    features = ["momentum","trend","relative5","risk","breadth","market21"]
    panel = pd.concat(blocks).sort_index().replace([np.inf,-np.inf],np.nan).dropna(subset=features)
    calendar = close.index
    months = calendar.to_period("M")
    predictions = pd.DataFrame(np.nan,index=calendar,columns=close.columns)
    for month in months.unique():
        days = calendar[months==month]
        i = calendar.get_loc(days[0])
        if i < 5:
            continue
        # At today's close the five-session label ending today has matured.
        learn = panel.loc[calendar[max(0,i-508)]:calendar[i-5]].dropna(subset=["label"])
        score_now = panel.loc[days[0]:days[-1]]
        if learn.index.nunique()<126 or score_now.empty:
            continue
        model=make_pipeline(StandardScaler(),Ridge(alpha=80))
        model.fit(learn[features],learn.label.clip(-3,3))
        row=score_now[["ticker"]].assign(score=model.predict(score_now[features]))
        wide=row.set_index("ticker",append=True).score.unstack()
        predictions.loc[wide.index,wide.columns]=wide
    return predictions


class Policy(V69):
    rules = ()

    def __init__(self, adjusted_close, adjusted_volume, adjusted_open, *, market_index, omit_sndk=False):
        super().__init__(adjusted_close,adjusted_volume,adjusted_open,omit_sndk=omit_sndk)
        self.index = market_index.reindex(self.close.index)
        self.market_fast = self.index.rolling(63).mean()
        self.market_slow = self.index.rolling(126).mean()
        self.breadth_change = self.breadth.diff(10)
        daily = self.close.pct_change(fill_method=None)
        mm = self.index.pct_change(fill_method=None)
        market_var = mm.rolling(63).var().replace(0,np.nan)
        beta = daily.rolling(63).cov(mm).div(market_var,axis=0).clip(-1,4)
        residual = daily - beta.mul(mm,axis=0)
        self.risk = daily.rolling(42).std().replace(0,np.nan)
        self.relative10 = residual.rolling(10).sum()
        self.score = residual.rolling(42).sum().div(self.risk*np.sqrt(42))
        self.drawdown = self.close.div(self.close.rolling(42).max()).sub(1)
        down = daily.clip(upper=0).pow(2)
        self.downside = down.rolling(42).mean().pow(.5).replace(0,np.nan)
        self.down_jump = down.rolling(10).mean().div(down.rolling(63).mean().replace(0,np.nan)).pow(.5)
        market_down = mm.clip(upper=0).pow(2)
        self.market_down_jump = (market_down.rolling(10).mean()/market_down.rolling(63).mean().replace(0,np.nan)).pow(.5)
        self.fastmom = self.close.pct_change(10,fill_method=None)

        if {"ridge_tilt","ridge_confirm"}.intersection(self.rules):
            names = self.eligible_symbols()
            self.forecast = predict_residual(self.close[names],residual[names],self.score[names],
                self.risk[names],self.breadth,self.index).reindex(columns=self.close.columns)

    @staticmethod
    def cap(weights):
        gross = weights.sum()
        return weights*(.375/gross) if gross>.375 else weights

    def targets(self, previous, prices, valid_prices, held):
        weights = super().targets(previous,prices,valid_prices,held)
        for rule in self.rules:
            weights = self.cap(self.apply_rule(rule,previous,prices,held,weights))
        return weights

    def apply_rule(self, rule, t, prices, held, w):
        breadth = self.breadth.at[t]
        market = self.index.at[t]
        rising = market > self.market_slow.at[t]
        weak = breadth < .5 and market <= self.market_fast.at[t]
        score = self.score.loc[t]
        active_score = score.where(w.gt(0)).dropna()
        if rule == "residual_bonus":
            leaders = active_score.loc[active_score.gt(0)].nlargest(2).index
            w = w.mask(score.lt(0),.92*w)
            w.loc[leaders] *= 1.10
        elif rule == "crash_brake":
            if market < self.market_fast.at[t] and self.market_down_jump.at[t] >= 1.30:
                w *= .85
        elif rule == "giveback":
            weak_stock = held.gt(0)&self.drawdown.loc[t].lt(-.08)&self.relative10.loc[t].lt(0)
            w = w.mask(weak_stock,.88*w)
        elif rule == "relative_tilt":
            w = tilt_fixed_budget(w,score,.15)
        elif rule == "weak_entry":
            if weak:
                fading = self.relative10.loc[t].lt(0)&prices.le(self.ma63.loc[t])
                w = w.mask(held.le(0)&fading,0.)
        elif rule == "downside_budget":
            risk = self.downside.loc[t].copy()
            risk["TQQQ"] /= 3
            valid = risk.where(w.gt(0)).dropna()
            if len(valid) >= 3:
                rank = -np.log(risk/valid.median())*2
                w = tilt_fixed_budget(w,rank,.15)
        elif rule == "smooth_budget":
            if weak:
                w = w.mask(self.down_jump.loc[t].gt(1.35),.9*w)
            elif rising and breadth >= 2/3:
                w = w.mask(self.down_jump.loc[t].lt(1.0),1.07*w)
        elif rule == "ridge_tilt":
            w = tilt_fixed_budget(w,self.forecast.loc[t],.12)
        elif rule == "ridge_confirm":
            low = self.forecast.loc[t].lt(-.10)&self.high.loc[t]&held.le(0)
            w = w.mask(low,.5*w)
        elif rule == "size_control":
            w *= 1.05
        elif rule == "recovery_core":
            if rising and breadth >= .5 and self.breadth_change.at[t] >= .20 and w.QQQ > 0:
                w.QQQ += .012
        elif rule == "relative_add":
            if rising and breadth >= .5:
                quality = score.gt(.25)&self.fastmom.loc[t].gt(0)
                w = w.mask(quality,1.07*w)
        elif rule == "concentration_limit":
            name = self.high.loc[t]
            standard = TARGET*1.10
            oversize = name & w.gt(standard)
            w = w.mask(oversize,standard + .40*(w-standard))
        elif rule == "short_downside":
            cut = self.down_jump.loc[t].gt(1.35)&prices.lt(self.ma28.loc[t])
            w = w.mask(cut,.88*w)
        elif rule == "dual_strength":
            if rising and breadth >= 2/3:
                leaders = active_score.loc[active_score.gt(.15)].drop("TQQQ",errors="ignore").nlargest(2).index
                w.loc[leaders] += .006
            elif weak:
                w = w.mask(self.down_jump.loc[t].gt(1.20),.9*w)
        else:
            raise ValueError(f"Unregistered rule {rule}")
        return w
