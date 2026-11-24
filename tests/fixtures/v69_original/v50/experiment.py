"""Run from the US research directory: python -m v50.experiment select|evaluate."""
import argparse
import hashlib
import json

import numpy as np
import pandas as pd
import qlib
from qlib.backtest import backtest

from audit import independent
from v48_strategy import ROOT, TICKERS
from v49.strategy import CAPITAL, V49Strategy
from v50.strategy import VERSION, V50Strategy, histories

PLAN = json.loads((VERSION / "protocol.json").read_text(encoding="utf-8"))
RATIOS = ["sortino", "calmar", "sharpe"]
FILL_COLS = ["date", "ticker", "side", "quantity", "requested", "price", "turnover", "fee"]


def fingerprint():
    files = [VERSION / x for x in ("protocol.json", "strategy.py", "experiment.py")]
    files += [ROOT / "v49" / "strategy.py", ROOT / "v48_strategy.py", ROOT / "data" / "manifest.json"]
    files += [ROOT / "data" / f"{asset}.parquet" for asset in TICKERS]
    return {str(f.relative_to(ROOT)): hashlib.sha256(f.read_bytes()).hexdigest() for f in files}


def risk_metrics(returns, market):
    r = np.asarray(returns)
    nav = np.r_[1.0, (1 + r).cumprod()]
    dd = float((nav / np.maximum.accumulate(nav) - 1).min())
    metrics = dict(net_return=nav[-1] - 1, max_dd=dd)
    keys = ["cagr", *RATIOS, "beta", "alpha"]
    if len(r) < 30:
        return {**metrics, **dict.fromkeys(keys, np.nan)}
    annual = nav[-1] ** (252 / len(r)) - 1
    down = np.sqrt(np.mean(np.minimum(r, 0) ** 2))
    sd = r.std(ddof=1)
    m = np.asarray(market)
    beta = np.cov(r, m, ddof=1)[0, 1] / m.var(ddof=1) if m.var() > 0 else np.nan
    return {**metrics, "cagr": annual, "sortino": np.sqrt(252) * r.mean() / down if down else np.nan,
            "calmar": annual / abs(dd) if dd else np.nan, "sharpe": np.sqrt(252) * r.mean() / sd if sd else np.nan,
            "beta": beta, "alpha": (r.mean() - beta * m.mean()) * 252}


def run(rule, window, case="normal"):
    start, end = PLAN[window]
    close, volume, opening = histories(end)
    alt_open = case == "minute_open"
    provider = "provider_minute_open" if alt_open else "provider"
    qlib.init(provider_uri=str(ROOT / provider), region="us", kernels=1,
              expression_cache=None, dataset_cache=None, logging_level=40)
    omit = case == "no_sndk"
    if rule == "reference" and not omit:
        strategy = V49Strategy(close, volume)
    else:
        strategy = V50Strategy(close, volume, opening, rule=rule, omit_sndk=omit)
    cost = 0.003 if case == "stress" else 0.001
    reports, _ = backtest(start_time=start, end_time=end, strategy=strategy,
        executor={"class": "SimulatorExecutor", "module_path": "qlib.backtest.executor",
                  "kwargs": {"time_per_step": "day", "generate_portfolio_metrics": True, "trade_type": "serial"}},
        account=CAPITAL, benchmark="QQQ", exchange_kwargs={"freq": "day", "codes": TICKERS,
        "deal_price": "open", "limit_threshold": None, "trade_unit": None, "min_cost": 0,
        "open_cost": cost, "close_cost": cost})
    daily, positions = reports["1day"]
    assert daily.index.equals(close.loc[start:end].index)
    fills = pd.DataFrame(strategy.fills, columns=FILL_COLS)
    book = pd.DataFrame({d: {"cash": p.get_cash(), **p.get_stock_amount_dict()} for d, p in positions.items()}).T
    book = book.reindex(index=daily.index, columns=["cash", *TICKERS]).fillna(0)
    if alt_open:
        first_print = pd.read_csv(ROOT / "data" / "recent_open_check.csv", parse_dates=["date"])
        alternate = first_print.pivot(index="date", columns="ticker", values="adj_minute_open")
        opening.update(alternate)
    ledger, attribution, error = independent(daily, book, fills, pd.DataFrame(), rule, cost,
                                              close, opening, volume, omit)
    if omit:
        assert not fills.ticker.eq("SNDK").any()
    out = VERSION / "runs" / window / case / rule
    out.mkdir(exist_ok=True, parents=True)
    if hasattr(strategy, "decisions"):
        decisions = pd.DataFrame(strategy.decisions).set_index("date")
        expect = pd.Series(daily.index, index=daily.index).groupby(
            [daily.index.isocalendar().year, daily.index.isocalendar().week]).first()
        assert list(expect) == list(decisions.index)
        prev = close.index[close.index.get_indexer(decisions.index) - 1]
        assert list(decisions.signal_date) == list(prev)
        assert decisions[TICKERS].sum(axis=1).le(0.3711).all() and decisions[TICKERS].ge(0).all().all()
        decisions.to_csv(out / "targets.csv")
    if rule == "reference" and window in ("main", "smoke"):
        name = "recent" if window == "smoke" else "main"
        archive = ROOT / "v48" / case / name / "qlib_report.parquet"
        old = pd.read_parquet(archive)
        pd.testing.assert_frame_equal(daily, old, atol=1e-8, rtol=0)
    r = ledger.nav.pct_change()
    r.iloc[0] = ledger.nav.iloc[0] / CAPITAL - 1
    market = close.QQQ.pct_change().loc[r.index]
    assert np.isfinite(r).all() and np.isfinite(market).all()
    ledger.assign(net_daily=r, qqq_daily=market).to_parquet(out / "daily.parquet")
    fills.to_csv(out / "fills.csv", index=False)
    attribution.sum().rename("net_usd").to_csv(out / "pnl_by_asset.csv")
    row = dict(rule=rule, window=window, case=case, first=str(r.index[0].date()), last=str(r.index[-1].date()),
               sessions=len(r), exposure=ledger.weight.mean(), fees=fills.fee.sum(), fills=int(fills.quantity.gt(0).sum()),
               cash_pnl=ledger.nav.iloc[-1] - CAPITAL, max_ledger_error=error, **risk_metrics(r, market))
    print(window, case, rule, {k: round(row[k], 4) for k in ["net_return", *RATIOS, "beta"]}, flush=True)
    return row


def hurdle(x, base):
    return bool(x[RATIOS].gt(base[RATIOS]).all() and x.net_return >= base.net_return
                and x.exposure >= 0.90 * base.exposure and x.beta <= base.beta + 0.08)


def select():
    rows = pd.DataFrame([run(rule, "development") for rule in ["reference", *PLAN["candidates"]]]).set_index("rule")
    base = rows.loc["reference"]
    choices = rows.loc[PLAN["candidates"]].copy()
    choices["pass"] = [hurdle(row, base) for _, row in choices.iterrows()]
    eligible = choices.loc[choices["pass"]]
    ranking = (eligible if len(eligible) else choices).copy()
    ranking["rank_sum"] = ranking[RATIOS].rank(ascending=False).sum(axis=1)
    winner = ranking.sort_values(["rank_sum", "sortino"], ascending=[True, False], kind="stable").index[0]
    rows.to_csv(VERSION / "development.csv")
    selection = {"selected": winner, "development_passed": bool(len(eligible)),
                 "qualified": list(eligible.index), "ranking": ranking.rank_sum.to_dict(), "hashes": fingerprint()}
    path = VERSION / "selection.json"
    if path.exists() and json.loads(path.read_text()) != selection:
        raise RuntimeError("Existing selection differs; use a separate version for a new experiment.")
    path.write_text(json.dumps(selection, indent=2), encoding="utf-8")
    print("FROZEN", winner, "development_passed", bool(len(eligible)), flush=True)


def intervals(window, winner):
    base = VERSION / "runs" / window / "normal"
    a = pd.read_parquet(base / winner / "daily.parquet")
    b = pd.read_parquet(base / "reference" / "daily.parquet")
    assert a.index.equals(b.index)
    arrays = [a.net_daily.to_numpy(), b.net_daily.to_numpy(), a.qqq_daily.to_numpy()]
    n = len(a)
    rng = np.random.default_rng(50)
    samples = []
    for _ in range(2000):
        starts = rng.integers(n, size=(n + 19) // 20)
        ids = ((starts[:, None] + np.arange(20)) % n).ravel()[:n]
        x, y, m = (s[ids] for s in arrays)
        left, right = risk_metrics(x, m), risk_metrics(y, m)
        samples.append({k: left[k] - right[k] for k in [*RATIOS, "alpha", "beta"]})
    sample = pd.DataFrame(samples)
    return [{"window": window, "metric": k, "lower": sample[k].quantile(.025), "upper": sample[k].quantile(.975)}
            for k in sample]


def evaluate():
    lock = json.loads((VERSION / "selection.json").read_text(encoding="utf-8"))
    if lock["hashes"] != fingerprint():
        raise RuntimeError("Locked strategy, protocol or data changed.")
    winner = lock["selected"]
    rows, checks = [], {}
    for window, case in (("validation", "normal"), ("validation", "stress"), ("main", "normal"),
                         ("main", "stress"), ("main", "no_sndk"), ("smoke", "normal"), ("smoke", "minute_open")):
        base, trial = (pd.Series(run(rule, window, case)) for rule in ("reference", winner))
        rows.extend([base, trial])
        if window != "smoke":
            checks[f"{window}_{case}"] = (hurdle(trial, base) if case == "normal" else bool(
                trial[RATIOS].ge(base[RATIOS]).all() and trial.net_return >= base.net_return))
    pd.DataFrame(rows).to_csv(VERSION / "evaluation.csv", index=False)
    bounds = intervals("validation", winner) + intervals("main", winner)
    pd.DataFrame(bounds).to_csv(VERSION / "intervals.csv", index=False)
    decision = {"challenger": winner, "development": lock["development_passed"], "checks": checks,
                "promote": bool(lock["development_passed"] and all(checks.values()))}
    (VERSION / "verdict.json").write_text(json.dumps(decision, indent=2), encoding="utf-8")
    print(decision, flush=True)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("phase", choices=["select", "evaluate"])
    if p.parse_args().phase == "select":
        select()
    else:
        evaluate()
