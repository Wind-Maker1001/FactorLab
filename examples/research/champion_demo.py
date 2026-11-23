"""Reproducible paired multi-strategy SOFTWARE ONLY experiment; no data download."""
import argparse
import json
from pathlib import Path
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from factorlab.dataset.identity import file_hash
from factorlab.research.automatic import run, write_json


def prepare(output):
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    dates = pd.bdate_range("2021-01-01", periods=620)
    rng = np.random.default_rng(20261123)
    returns = .0005+.008*rng.standard_normal(len(dates))
    close = 30*np.cumprod(1+returns)
    opening = np.r_[30., close[:-1]]
    bars = pd.DataFrame(dict(date=dates, symbol="601857.SH", raw_open=opening,
        raw_high=np.maximum(close, opening)*1.005, raw_low=np.minimum(close, opening)*.995,
        raw_close=close, adj_close=close, volume=5000000., trade_status=1, is_st=0))
    bars.to_parquet(output / "bars.parquet", index=False)
    source = Path(__file__).with_name("champion_strategies.py")
    (output / "strategies.py").write_bytes(source.read_bytes())
    source_hash = file_hash(output / "strategies.py")
    config = dict(schema="FACTORLAB_AUTO_RESEARCH_V1", study_id="synthetic-paired-v1",
        data=dict(bars="bars.parquet", sha256=file_hash(output / "bars.parquet"), source_kind="SYNTHETIC"),
        windows={name: [str(dates[a].date()), str(dates[b].date())]
                 for name, a, b in (("train", 30, 219), ("validation", 220, 359), ("test", 360, 619))},
        common=dict(symbol="601857.SH", initial_cash=100000., max_weight=1., board_lot=100,
            funding_bps=0, execution="a_share_daily_long_only_next_open_t1", fee_rules=[dict(
                effective_from="2020-01-01", commission_bps=3., slippage_bps=5.,
                stamp_duty_bps=5., minimum_commission_cny=5.)]),
        policy={}, strategies=[dict(strategy_id=name.lower(), file="strategies.py", **{"class": name},
            sha256=source_hash, parameters={}, grid=grid) for name, grid in (
                ("Trend", {"window": [5, 10, 20]}), ("TrainMean", {"multiplier": [.95, 1., 1.05]}))],
        champion=dict(strategy_id="cash", strategy={"file": "strategies.py", "class": "Cash", "sha256": source_hash}, parameters={}))
    write_json(output / "config.json", config)
    return output / "config.json"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    root = Path(args.output).resolve()
    config = prepare(root)
    report = run(config, output=root / "run", store=root / "tracker", state_path=root / "champion.json",
                 command=[sys.executable, *sys.argv])
    print(json.dumps({k: report[k] for k in ("reason", "promoted", "selected")}, indent=2))


if __name__ == "__main__":
    main()
