"""Shared accounting for new research batches. Each account is replayed and checked independently."""
from dataclasses import dataclass
from pathlib import Path
import importlib
import json
import numpy as np
import pandas as pd
import qlib
from qlib.backtest import backtest
from audit import independent
from risk_research.replay import market as archived_market, digest, write_json
from v49.strategy import CAPITAL
from v48_strategy import ROOT,TICKERS
from v50.strategy import V50Strategy
from v50.experiment import FILL_COLS,risk_metrics


@dataclass
class Market:
    start: str
    end: str
    provider: Path
    names: list
    close: pd.DataFrame
    volume: pd.DataFrame
    opening: pd.DataFrame


def archived(window,case):
    return Market(*archived_market(window,case))


def separate(data,start,end):
    bars={s:pd.read_parquet(data/f"{s}.parquet").loc[:end] for s in TICKERS if (data/f"{s}.parquet").exists()}
    names=[s for s,q in bars.items() if not q.empty]
    series=[pd.DataFrame({s:q[k] for s,q in bars.items()}).reindex(columns=TICKERS) for k in ("close","volume","open")]
    return Market(start,end,data/"provider",names,*series)


def checkpoints(out,signature):
    receipt=out/"result.json"
    if not receipt.exists():
        return None
    meta=json.loads(receipt.read_text(encoding="utf-8"))
    if meta["lock"]!=signature or any(digest(out/k)!=h for k,h in meta["outputs"].items()):
        raise ValueError(f"Invalid checkpoint {out}")
    return meta["metrics"]


def simulate(version,window,case,market,*,output,signature,cap,omit_sndk=False,
             strategy_kwargs=None,cost=None,exclude=()):
    old=checkpoints(output,signature)
    if old is not None:
        return old
    m=market
    kw=dict(omit_sndk=omit_sndk)
    if strategy_kwargs:
        kw.update(strategy_kwargs)
    if version in ("v49","v50"):
        st=V50Strategy(m.close,m.volume,m.opening,rule="reference" if version=="v49" else "band",**kw)
    else:
        st=importlib.import_module(version+".strategy").Strategy(m.close,m.volume,m.opening,**kw)
    fee_rate=cost if cost is not None else (.003 if case=="stress" else .001)
    qlib.init(provider_uri=str(m.provider),region="us",kernels=1,expression_cache=None,dataset_cache=None,logging_level=40)
    reports,_=backtest(start_time=m.start,end_time=m.end,strategy=st,account=CAPITAL,benchmark="QQQ",
        executor={"class":"SimulatorExecutor","module_path":"qlib.backtest.executor","kwargs":{
            "time_per_step":"day","generate_portfolio_metrics":True,"trade_type":"serial"}},
        exchange_kwargs={"freq":"day","codes":m.names,"deal_price":"open","limit_threshold":None,"trade_unit":None,
                         "min_cost":0,"open_cost":fee_rate,"close_cost":fee_rate})
    daily,positions=reports["1day"]
    if not daily.index.equals(m.close.loc[m.start:m.end].index):
        raise ValueError("Unexpected Qlib calendar")
    fills=pd.DataFrame(st.fills,columns=FILL_COLS)
    book=pd.DataFrame({d:{"cash":s.get_cash(),**s.get_stock_amount_dict()} for d,s in positions.items()}).T
    book=book.reindex(index=daily.index,columns=["cash",*TICKERS]).fillna(0)
    op=m.opening
    if case=="minute_open":
        op=op.copy()
        first=pd.read_csv(ROOT/"data"/"recent_open_check.csv",parse_dates=["date"])
        op.update(first.pivot(index="date",columns="ticker",values="adj_minute_open"))
    ledger,attr,error=independent(daily,book,fills,pd.DataFrame(),version,fee_rate,m.close,op,m.volume,omit_sndk)
    signal=pd.DataFrame(st.decisions).set_index("date")
    iso=daily.index.isocalendar()
    weekly=pd.Series(daily.index,index=daily.index).groupby([iso.year,iso.week]).first()
    assert list(signal.index)==list(weekly)
    assert list(signal.signal_date)==list(m.close.index[m.close.index.get_indexer(signal.index)-1])
    weights=signal[TICKERS]
    assert weights.ge(0).all().all() and weights.sum(axis=1).le(cap+1e-8).all()
    dropped=set(exclude)|({"SNDK"} if omit_sndk else set())
    if dropped:
        assert not fills.ticker.isin(dropped).any() and weights[list(dropped)].eq(0).all().all()
    for s in m.names:
        active=fills[fills.ticker.eq(s)]
        if not active.empty and active.date.min()<m.close[s].first_valid_index():
            raise ValueError("Prelisting fill")
    r=ledger.nav.pct_change()
    r.iloc[0]=ledger.nav.iloc[0]/CAPITAL-1
    qqq=m.close.QQQ.pct_change().loc[r.index]
    assert np.isfinite(r).all() and np.isfinite(qqq).all()
    row=dict(version=version,window=window,case=case,start=str(r.index[0].date()),end=str(r.index[-1].date()),
             days=len(r),fills=int(fills.quantity.gt(0).sum()),fees=float(fills.fee.sum()),exposure=ledger.weight.mean(),
             net_usd=ledger.nav.iloc[-1]-CAPITAL,error=error,**risk_metrics(r,qqq))
    row={k:None if isinstance(v,(float,np.floating)) and not np.isfinite(v) else v for k,v in row.items()}
    output.mkdir(exist_ok=True,parents=True)
    ledger.assign(net_daily=r,qqq_daily=qqq).to_parquet(output/"daily.parquet")
    fills.to_csv(output/"fills.csv",index=False)
    attr.sum().rename("net_usd").to_csv(output/"attribution.csv")
    signal.to_csv(output/"targets.csv")
    write_json(output/"result.json",dict(lock=signature,metrics=row,outputs={k:digest(output/k)
        for k in ["daily.parquet","fills.csv","attribution.csv","targets.csv"]}))
    return row
