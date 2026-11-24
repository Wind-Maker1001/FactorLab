"""Replay all version archives with the S&P 500 total return benchmark."""
import argparse
import importlib
import inspect
import json

import numpy as np
import pandas as pd

from beta_research.engine import separate, simulate
from continuous_research.build_data import DATA as SEALED
from continuous_research.run import lock as prior_lock
from research_2023_2026.quotes import HERE, THROUGH, archived_index
from risk_research.replay import digest, write_json
from v48_strategy import ROOT, TICKERS
from v50.experiment import risk_metrics


P = json.loads((HERE/"protocol.json").read_text(encoding="utf-8"))
CONTROLS = ("v49","v69","v70")
BATCHES = {i:tuple(f"v{v}" for v in range(66+5*i,71+5*i)) for i in (1,2,3)}
RATIOS = ["sortino","calmar","sharpe"]
CASES = {"normal":.001,"stress":.003,"no_sndk":.001}


def phase_plan(batch):
    if batch==1:
        return {"parent":"v69","versions":P["versions"]}
    return json.loads((HERE/f"batch{batch}.json").read_text(encoding="utf-8"))


def lock_batch(batch,create=False):
    phases=[phase_plan(i) for i in range(1,batch+1)]
    files=[HERE/n for n in ["protocol.json","run.py","quotes.py"]]
    for i in range(2,batch+1):
        files += [HERE/f"batch{i}.json",HERE/f"selection{i-1}.json",HERE/f"lock{i-1}.json"]
    if batch>1:
        files += [HERE/"overlay.py"]
    versions=[*CONTROLS,*[v for phase in phases for v in phase["versions"]]]
    files += [ROOT/v/"strategy.py" for v in versions]
    base=prior_lock()
    record=dict(base=base,batch=batch,sources={f.relative_to(ROOT).as_posix():digest(f) for f in files})
    stamp=HERE/f"lock{batch}.json"
    if stamp.exists():
        if json.loads(stamp.read_text(encoding="utf-8"))!=record:
            raise ValueError("Frozen version or dependency changed")
    elif create:
        write_json(stamp,record)
    else:
        raise ValueError("Phase must be frozen before replay")
    return digest(stamp)


def market_data(window):
    if window in P["selection"]:
        a,b=P["selection"][window]
        market=separate(SEALED,a,b)
        return market,archived_index()
    folder=HERE/"snapshot"/THROUGH
    manifest=json.loads((folder/"manifest.json").read_text(encoding="utf-8"))
    if manifest["actual_end"]!=THROUGH or any(digest(folder/name)!=sha for name,sha in manifest["hashes"].items()):
        raise ValueError("Final research snapshot changed")
    start,_=P["target"]
    market=separate(folder,start,THROUGH)
    if str(market.close.index[-1].date())!=THROUGH:
        raise ValueError("Missing last session")
    return market,pd.read_parquet(folder/"benchmark.parquet").close


def execute(v,window,case,signature):
    m,index=market_data(window)
    out=HERE/"controls"/v if v in CONTROLS else ROOT/v
    kwargs={}
    if v not in CONTROLS:
        cls=importlib.import_module(f"{v}.strategy").Strategy
        if "market_index" in inspect.signature(cls).parameters:
            kwargs["market_index"]=index
    d=out/"runs"/window/case
    info=simulate(v,window,case,m,output=d,signature=signature,cap=.375,
                  omit_sndk=case=="no_sndk" or window.startswith("dev"),strategy_kwargs=kwargs,cost=CASES[case])
    day=pd.read_parquet(d/"daily.parquet")
    market_r=index.pct_change(fill_method=None).reindex(day.index)
    if not np.isfinite(market_r).all():
        raise ValueError("S&P 500 benchmark sessions do not match")
    info.update(risk_metrics(day.net_daily,market_r),benchmark_return=float((1+market_r).prod()-1))
    print(v,window,case,len(day),f'{info["net_return"]:+.3%}',f'A {info["alpha"]:+.3%}',flush=True)
    return info


def baseline_signature():
    return digest(HERE/"protocol.json")+"-"+prior_lock()


def nominate(batch):
    versions=BATCHES[batch]
    ref=pd.read_csv(HERE/"controls"/"v69"/"dev.csv").set_index("window")
    tbl=pd.concat([pd.read_csv(ROOT/v/"dev.csv") for v in versions])
    def passed(row):
        b=ref.loc[row.window]
        return (row.net_return>=b.net_return and row.alpha>=b.alpha and row.exposure>=.95*b.exposure
                and row.beta<=b.beta+.04 and all(getattr(row,k)>=b[k] for k in RATIOS))
    tbl["passed"]=[passed(row) for row in tbl.itertuples()]
    tbl["rank"]=tbl.groupby("window")[[*RATIOS,"alpha"]].rank(ascending=False).sum(axis=1)
    score=tbl.groupby("version").agg(windows_passed=("passed","sum"),rank_sum=("rank","sum"))
    score=score.sort_values(["windows_passed","rank_sum"],ascending=[False,True],kind="stable")
    winner=score.index[0]
    decision=dict(selected=winner,batch=batch,lock=lock_batch(batch),ranking=score.to_dict(orient="index"),
                  exploratory=int(score.iloc[0].windows_passed)==0)
    p=HERE/f"selection{batch}.json"
    if p.exists() and decision!=json.loads(p.read_text(encoding="utf-8")):
        raise ValueError("Frozen selection differs")
    write_json(p,decision)
    print(decision,flush=True)


def phase_for(v):
    return next(i for i,group in BATCHES.items() if v in group)


def final_signature():
    for batch in BATCHES:
        d=json.loads((HERE/f"selection{batch}.json").read_text(encoding="utf-8"))
        if lock_batch(batch)!=d["lock"]:
            raise ValueError(f"Selection {batch} differs")
    return "-".join([digest(HERE/f"selection{i}.json") for i in BATCHES]+[digest(HERE/"snapshot"/THROUGH/"manifest.json")])


def main():
    p=argparse.ArgumentParser()
    p.add_argument("stage",choices=["freeze","develop","nominate","target"])
    p.add_argument("group",choices=["0","1","2","3"])
    p.add_argument("versions",nargs="*")
    args=p.parse_args(); batch=int(args.group)
    if args.stage=="freeze":
        print(lock_batch(batch,create=True),flush=True)
        return
    if args.stage=="nominate":
        nominate(batch)
        return
    known=BATCHES.get(batch,CONTROLS)
    versions=args.versions or known
    if not set(versions)<=set(known):
        raise ValueError("Unexpected version for batch")
    if args.stage=="develop":
        signature=lock_batch(batch) if batch else baseline_signature()
        cases=[(w,"normal") for w in P["selection"]]
        filename="dev.csv"
    else:
        signature=final_signature()
        cases=[("target",c) for c in CASES]
        filename="target.csv"
    for v in versions:
        rows=[execute(v,w,c,signature) for w,c in cases]
        out=HERE/"controls"/v if v in CONTROLS else ROOT/v
        pd.DataFrame(rows).to_csv(out/filename,index=False)


if __name__=="__main__":
    main()
