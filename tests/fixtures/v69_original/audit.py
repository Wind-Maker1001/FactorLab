"""Recalculate daily positions, cash, PnL and decision timing directly from market data and fills."""
import hashlib
import json
from pathlib import Path
import numpy as np
import pandas as pd

ROOT=Path(__file__).resolve().parent
P=json.loads((ROOT/"protocol.json").read_text(encoding="utf-8"))
A=list(P["universe"])
B=pd.Series(P["weights"])
N=float(P["cash"])
ARMS=["cash","qqq32","passive","v47","v48"]
DATA=ROOT/"data"

def account_folder(arm,case,window):
    d=ROOT/arm if arm.startswith("v") else ROOT/"controls"/arm
    return d/case/window

def load(name):
    return pd.read_parquet(DATA/(name+".parquet"))

def change(x):
    r=x.pct_change()
    r.iloc[0]=x.iloc[0]/N-1.
    return r

def independent(eod,book,orders,decisions,kind,cost,full_closes,opening,vol,omit_sndk):
    close=full_closes.reindex(eod.index)
    prev=full_closes.shift().reindex(eod.index)
    pure_qty=pd.DataFrame(0.,index=eod.index,columns=A)
    post_fill=pd.DataFrame(0.,index=eod.index,columns=A)
    trade_pnl=pd.DataFrame(0.,index=eod.index,columns=A)
    fee=pd.Series(0.,index=eod.index)
    for t in orders.itertuples(index=False):
        side=1 if t.side=="buy" else -1
        if t.quantity<=0:continue
        if abs(t.price-opening.at[t.date,t.ticker])>max(.00005,t.price*1e-7):
            raise ValueError(f"{kind} filled away from open {t.date} {t.ticker}")
        if not np.isclose(t.price*t.quantity,t.turnover,atol=1e-8,rtol=1e-12):
            raise ValueError("turnover mismatch")
        if not np.isclose(t.fee,t.turnover*cost,atol=1e-7,rtol=1e-12):
            raise ValueError("fees mismatch")
        cap=.001*vol.shift().at[t.date,t.ticker]
        if t.quantity>cap+1e-5:
            raise ValueError("exceeds causal liquidity cap")
        pure_qty.at[t.date,t.ticker]+=side*t.quantity
        post_fill.at[t.date,t.ticker]-=side*t.turnover+t.fee
        trade_pnl.at[t.date,t.ticker]+=side*t.quantity*(close.at[t.date,t.ticker]-t.price)-t.fee
        fee.at[t.date]+=t.fee
    own_cash=N+post_fill.sum(axis=1).cumsum()
    qty=pure_qty.cumsum()
    if own_cash.min()<-1e-6 or qty.min().min()<-1e-6:
        raise ValueError("short or borrowed")
    if (qty.abs().gt(1e-8)&close.isna()).any().any():
        raise ValueError("used missing security valuation")
    by_symbol=(qty.shift(fill_value=0.)*(close-prev)).fillna(0.)+trade_pnl
    marks=(qty*close).fillna(0.)
    nav=own_cash+marks.sum(axis=1)
    pnl_error=float((N+by_symbol.sum(axis=1).cumsum()-nav).abs().max())
    cash_error=float((own_cash-book["cash"]).abs().max())
    qty_error=float((qty-book[A]).abs().max().max())
    nav_error=float((nav-eod.account).abs().max())
    fee_error=float((fee.cumsum()-eod.total_cost).abs().max())
    if max(pnl_error,cash_error,nav_error,fee_error)>.03 or qty_error>1e-6:
        raise ValueError(f"independent Qlib mismatch: {kind}, {pnl_error}, {cash_error}, {nav_error}, {fee_error}, {qty_error}")
    if len(decisions):
        groups=decisions.groupby("date")
        for date,g in groups:
            previous=full_closes.index[full_closes.index.get_loc(date)-1]
            if not g.using_close.eq(previous).all():
                raise ValueError("signal did not use previous session")
            history=full_closes.loc[:previous]
            r=history.iloc[-P["trend_days"]:].count().eq(P["trend_days"])
            bull=history.iloc[-1].gt(history.iloc[-P["trend_days"]:].mean())
            slow=history.QQQ.iloc[-1]>history.QQQ.iloc[-P["slow_days"]:].mean()
            if kind=="qqq32":
                w=B*0.
                w["QQQ"]=.32
            else:
                w=B.where(r if kind=="passive" else r&bull,0.)
                if kind=="v48" and not slow:w[["QQQ","TQQQ"]]=0.
            if omit_sndk:
                w.loc["SNDK"]=0.
            if not np.allclose(g.set_index("ticker").target_weight.reindex(A),w,atol=1e-10):
                raise ValueError("not the frozen rule")
        expected_dates=pd.Series(eod.index,index=eod.index).groupby([eod.index.isocalendar().year,eod.index.isocalendar().week]).min()
        if not set(groups.groups)==set(expected_dates):
            raise ValueError("weekly calendar error")
    metrics=pd.DataFrame({"nav":eod.account,"fee":fee,"value":marks.sum(axis=1),"QQQ":close.QQQ})
    metrics["weight"]=metrics.value/metrics.nav
    metrics["index_equivalent_etf_weight"]=(marks.QQQ+3.*marks.TQQQ)/metrics.nav
    return metrics,by_symbol,max(pnl_error,cash_error,nav_error,fee_error)

def interval(a,b):
    """Simultaneous 95% intervals for the two planned aligned deltas within a window."""
    values=np.column_stack((a,b))
    if not np.isfinite(values).all():raise ValueError("non-finite paired return")
    n=len(values)
    rng=np.random.default_rng(47)
    draws=[]
    for _ in range(32):
        i=rng.integers(n,size=(100,(n+19)//20))
        ix=((i[:,:,None]+np.arange(20))%n).reshape(100,-1)[:,:n]
        draws.extend(values[ix].mean(axis=1))
    draws=np.asarray(draws)
    m=values.mean(axis=0)
    s=draws.std(axis=0,ddof=1)
    crit=np.quantile((np.abs(draws-m)/np.maximum(1e-12,s)).max(axis=1),.95)
    for name,mu,sd in zip(["v47-passive","v48-v47"],m,s):
        yield name,*(1e4*np.array([mu,mu-crit*sd,mu+crit*sd]))

def main():
    quotes={s:load(s) for s in A}
    close=pd.concat({s:x.adjusted for s,x in quotes.items()},axis=1)
    opening=pd.concat({s:x.open*x.adjust_ratio for s,x in quotes.items()},axis=1)
    volume=pd.concat({s:x.volume/x.adjust_ratio for s,x in quotes.items()},axis=1)
    r=pd.read_csv(DATA/"recent_open_check.csv",parse_dates=["date"])
    minute=r.pivot(index="date",columns="ticker",values="adj_minute_open")
    minute_open=opening.copy()
    minute_open.loc[minute.index,minute.columns]=minute
    manifest=json.loads((DATA/"manifest.json").read_text(encoding="utf8"))
    for s,md in manifest["symbols"].items():
        for provider in ["yahoo","nasdaq"]:
            name=md if provider=="yahoo" else md["nasdaq"]
            if "sha256" in name:
                digest=hashlib.sha256((DATA/"raw"/f"{s}_{provider}.json").read_bytes()).hexdigest()
                if digest!=name["sha256"]:raise ValueError("changed market snapshot")
    output=[]; attribution=[]; uncertainty=[]; curves=[]
    for window,(a,b) in P["windows"].items():
        scenarios=[("normal",ARMS)]
        if window=="recent":
            scenarios.append(("minute_open",ARMS))
        else:
            scenarios.append(("stress",ARMS[1:]))
        if window=="main":
            scenarios.append(("no_sndk",["passive","v47","v48"]))
        for regime,arms in scenarios:
            series={}
            for name in arms:
                d=account_folder(name,regime,window)
                q=pd.read_parquet(d/"qlib_report.parquet")
                p=pd.read_parquet(d/"positions.parquet")
                t=pd.read_csv(d/"trades.csv",parse_dates=["date"])
                intent=pd.read_csv(d/"decisions.csv",parse_dates=["date","using_close"])
                cost=P["costs"]["stress" if regime=="stress" else "normal"]
                ledger,attrib,err=independent(q,p,t,intent,name,cost,close,
                    minute_open if regime=="minute_open" else opening,volume,regime=="no_sndk")
                rr=change(ledger.nav)
                series[name]=rr
                market=close.QQQ.pct_change().reindex(q.index)
                beta=rr.cov(market)/market.var()
                annual=(q.account.iloc[-1]/N)**(252./len(q))-1
                path=np.r_[N,q.account.to_numpy()]
                dd=float((path/np.maximum.accumulate(path)-1.).min())
                active=t[t.quantity>0]
                s=active.loc[active.ticker.eq("SNDK")].date.min()
                output.append(dict(version=name,case=regime,window=window,first=q.index[0],last=q.index[-1],sessions=len(q),
                         net=q.account.iloc[-1]-N,ret=q.account.iloc[-1]/N-1.,annualized=annual if len(q)>=200 else None,
                         max_dd=dd,sharpe=rr.mean()/rr.std()*np.sqrt(252) if rr.std()>0 and len(q)>=30 else None,
                         beta_qqq=beta,intercept_daily_bp=(rr.mean()-beta*market.mean())*1e4,
                         avg_weight=ledger.weight.mean(),etf_risk_proxy=ledger.index_equivalent_etf_weight.mean(),fee=t.fee.sum(),
                         turnover=t.turnover.sum()/N,fills=len(active),sndk_first=s,postings_max_error=err))
                for asset in A:
                    f=active[active.ticker.eq(asset)]
                    attribution.append(dict(version=name,case=regime,window=window,ticker=asset,net_pnl=attrib[asset].sum(),
                                            fee=f.fee.sum(),fills=len(f),turnover=f.turnover.sum()))
                ledger=ledger.assign(version=name,case=regime,window=window)
                curves.append(ledger)
            if regime=="normal" and window!="recent":
                for label,avg,low,high in interval(series["v47"]-series["passive"],series["v48"]-series["v47"]):
                    uncertainty.append(dict(window=window,comparison=label,mean_bp=avg,lower=low,upper=high))
    frame=pd.DataFrame(output)
    assert len(frame)==40
    frame.to_csv(ROOT/"results.csv",index=False)
    pd.DataFrame(attribution).to_csv(ROOT/"asset_attribution.csv",index=False)
    pd.DataFrame(uncertainty).to_csv(ROOT/"uncertainty.csv",index=False)
    pd.concat(curves).rename_axis("session").reset_index().to_parquet(ROOT/"equity_paths.parquet",index=False)
    print(f"{len(frame)} audited ledgers; max independent error {frame.postings_max_error.max():.6f} USD")

if __name__=="__main__":
    main()
