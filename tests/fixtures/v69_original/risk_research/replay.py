"""Native Qlib replay, immutable input lock and restartable per-account outputs."""
import argparse
import hashlib
import importlib
import json
from pathlib import Path

import numpy as np
import pandas as pd
import qlib
from qlib.backtest import backtest

from audit import independent
from v48_strategy import ROOT, TICKERS
from v49.strategy import V49Strategy, CAPITAL
from v50.experiment import FILL_COLS, risk_metrics
from v50.strategy import V50Strategy, histories

HERE = Path(__file__).resolve().parent
PLAN = json.loads((HERE / "protocol.json").read_text(encoding="utf-8"))
VERSIONS = ["v49", "v50", *PLAN["hypotheses"]]
CASES = [("development","normal"),("validation","normal"),("main","normal"),
         ("main","no_sndk"),("main","stress"),("validation","stress"),
         ("older_a","normal"),("older_b","normal"),("older_c","normal"),
         ("smoke","normal"),("smoke","minute_open")]


def write_json(path, value):
    temp = path.with_suffix(".partial")
    temp.write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False),encoding="utf-8")
    temp.replace(path)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def lock_inputs():
    files = [HERE / x for x in ("protocol.json","parent.py","replay.py","older_data.py")]
    files += [ROOT / v / "strategy.py" for v in VERSIONS]
    files += [ROOT / x for x in ("audit.py","v48_strategy.py","v50/experiment.py")]
    files += [ROOT / "data" / f"{s}.parquet" for s in TICKERS]
    files += list((HERE/"older").glob("*.parquet"))
    files += [HERE/"older"/"manifest.json",ROOT/"data"/"recent_open_check.csv"]
    for provider in (ROOT/"provider",ROOT/"provider_minute_open",HERE/"older"/"provider"):
        files.extend(f for f in provider.rglob("*") if f.is_file())
    state={str(f.relative_to(ROOT)):digest(f) for f in sorted(set(files))}
    path=HERE/"locked.json"
    if path.exists():
        if json.loads(path.read_text(encoding="utf-8")) != state:
            raise RuntimeError("Frozen inputs changed; create a new batch before rerunning.")
    else:
        write_json(path,state)
    return digest(path)


def market(window,case):
    start,end=PLAN["windows"][window]
    if window.startswith("older"):
        provider=HERE/"older"/"provider"
        names=[s for s in TICKERS if s!="SNDK"]
        bars={s:pd.read_parquet(HERE/"older"/f"{s}.parquet").loc[:end] for s in names}
        close,volume,opening=(pd.DataFrame({s:b[k] for s,b in bars.items()}).reindex(columns=TICKERS)
                              for k in ("close","volume","open"))
    else:
        provider=ROOT/("provider_minute_open" if case=="minute_open" else "provider")
        close,volume,opening=histories(end)
        names=TICKERS
    return start,end,provider,names,close,volume,opening


def account(version,window,case,signature):
    out=(ROOT/version/"runs" if version not in ["v49","v50"] else HERE/"controls"/version)/window/case
    receipt=out/"result.json"
    if receipt.exists():
        meta=json.loads(receipt.read_text(encoding="utf-8"))
        if meta["lock"]!=signature or any(digest(out/f)!=h for f,h in meta["outputs"].items()):
            raise RuntimeError(f"Checkpoint mismatch in {out}")
        return meta["metrics"]
    begin,end,provider,names,close,volume,opening=market(window,case)
    omit=window.startswith("older") or case=="no_sndk"
    qlib.init(provider_uri=str(provider),region="us",kernels=1,expression_cache=None,dataset_cache=None,logging_level=40)
    if version=="v49" and not omit:
        strategy=V49Strategy(close,volume)
    elif version in ("v49","v50"):
        strategy=V50Strategy(close,volume,opening,rule="reference" if version=="v49" else "band",omit_sndk=omit)
    else:
        strategy=importlib.import_module(f"{version}.strategy").Strategy(close,volume,opening,omit_sndk=omit)
    rate=PLAN["costs"].get(case,PLAN["costs"]["normal"])
    reports,_=backtest(start_time=begin,end_time=end,strategy=strategy,account=CAPITAL,benchmark="QQQ",
        executor={"class":"SimulatorExecutor","module_path":"qlib.backtest.executor","kwargs":{
            "time_per_step":"day","generate_portfolio_metrics":True,"trade_type":"serial"}},
        exchange_kwargs={"freq":"day","codes":names,"deal_price":"open","limit_threshold":None,"trade_unit":None,
                         "min_cost":0,"open_cost":rate,"close_cost":rate})
    daily,positions=reports["1day"]
    if not daily.index.equals(close.loc[begin:end].index):
        raise ValueError("Calendar differs")
    fills=pd.DataFrame(strategy.fills,columns=FILL_COLS)
    if omit and fills.ticker.eq("SNDK").any():
        raise ValueError("excluded SNDK received orders")
    book=pd.DataFrame({d:{"cash":p.get_cash(),**p.get_stock_amount_dict()} for d,p in positions.items()}).T
    book=book.reindex(index=daily.index,columns=["cash",*TICKERS]).fillna(0)
    if case=="minute_open":
        minute=pd.read_csv(ROOT/"data"/"recent_open_check.csv",parse_dates=["date"])
        opening=opening.copy()
        opening.update(minute.pivot(index="date",columns="ticker",values="adj_minute_open"))
    ledger,by_asset,error=independent(daily,book,fills,pd.DataFrame(),version,rate,close,opening,volume,omit)
    if hasattr(strategy,"decisions"):
        decisions=pd.DataFrame(strategy.decisions).set_index("date")
        weeks=daily.index.isocalendar()
        expected=pd.Series(daily.index,index=daily.index).groupby([weeks.year,weeks.week]).first()
        assert list(expected)==list(decisions.index)
        assert list(decisions.signal_date)==list(close.index[close.index.get_indexer(decisions.index)-1])
        assert decisions[TICKERS].ge(0).all().all() and decisions[TICKERS].sum(axis=1).le(.322667+1e-8).all()
        if omit:
            assert decisions.SNDK.eq(0.).all()
    r=ledger.nav.pct_change()
    r.iloc[0]=ledger.nav.iloc[0]/CAPITAL-1
    m=close.QQQ.pct_change().loc[r.index]
    assert r.notna().all() and m.notna().all()
    if version in ("v49","v50") and not window.startswith("older"):
        old_rule="band" if version=="v50" else "reference"
        archive=ROOT/"v50"/"runs"/window/case/old_rule/"daily.parquet"
        if archive.exists():
            old=pd.read_parquet(archive)
            pd.testing.assert_series_equal(ledger.nav,old.nav,atol=1e-8,rtol=0)
    row=dict(version=version,window=window,case=case,start=str(r.index[0].date()),end=str(r.index[-1].date()),
             days=len(r),fills=int(fills.quantity.gt(0).sum()),fees=float(fills.fee.sum()),exposure=ledger.weight.mean(),
             net_usd=float(ledger.nav.iloc[-1]-CAPITAL),error=error,**risk_metrics(r,m))
    row={k:(None if isinstance(v,(float,np.floating)) and not np.isfinite(v) else v) for k,v in row.items()}
    out.mkdir(exist_ok=True,parents=True)
    ledger.assign(net_daily=r,qqq_daily=m).to_parquet(out/"daily.parquet")
    fills.to_csv(out/"fills.csv",index=False)
    by_asset.sum().rename("net_usd").to_csv(out/"attribution.csv")
    outputs=["daily.parquet","fills.csv","attribution.csv"]
    if hasattr(strategy,"decisions"):
        decisions.to_csv(out/"targets.csv")
        outputs.append("targets.csv")
    write_json(receipt,dict(lock=signature,outputs={f:digest(out/f) for f in outputs},metrics=row))
    return row


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--versions",nargs="+",choices=VERSIONS,default=VERSIONS)
    args=parser.parse_args()
    signature=lock_inputs()
    rows=[]
    for version in args.versions:
        for window,case in CASES:
            row=account(version,window,case,signature)
            rows.append(row)
            print(version,window,case,row["days"],f'{row["net_return"]:.3%}',flush=True)
        out=HERE/"controls"/version if version in ("v49","v50") else ROOT/version
        pd.DataFrame([r for r in rows if r["version"]==version]).to_csv(out/"summary.csv",index=False)

if __name__=="__main__":
    main()
