"""Replay the original v69 strategy through its original Qlib execution adapter."""
import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

from beta_research.engine import separate, simulate
from risk_research.replay import digest
from v50.experiment import risk_metrics

ROOT = Path(__file__).resolve().parent
FIXTURE = ROOT / "fixture"
CONFIG = ROOT / "original_configs" / "research_2023_2026" / "protocol.json"


def assert_hashes(root, manifest):
    for name, expected in manifest.items():
        if digest(root / name) != expected:
            raise ValueError(f"SHA-256 mismatch: {root / name}")


def verify_metrics(actual, expected):
    for key, value in expected.items():
        if isinstance(value, (float, int)):
            if not np.isclose(actual[key], value, atol=1e-7, rtol=0):
                raise ValueError(f"Replay mismatch: {key}, {actual[key]} != {value}")
        elif actual[key] != value:
            raise ValueError(f"Replay mismatch: {key}")


def run(output, verify, market_dir):
    plan = json.loads(CONFIG.read_text(encoding="utf-8"))
    start, end = plan["target"]
    ref = FIXTURE / "v69_target_normal"
    market_receipt = json.loads((market_dir / "manifest.json").read_text(encoding="utf-8"))
    original = json.loads((ref / "result.json").read_text(encoding="utf-8"))
    assert_hashes(market_dir, market_receipt["hashes"])
    assert_hashes(ref, original["outputs"])
    market = separate(market_dir, start, end)
    sig = "-".join([digest(CONFIG), digest(market_dir/"manifest.json"), digest(Path(__file__))])
    stats = simulate(
        "v69", "target", "normal", market, output=output, signature=sig, cap=.375,
        omit_sndk=False, cost=.001,
    )

    result = {"qlib_qqq_metrics": stats}
    nav = pd.read_parquet(output / "daily.parquet")
    sp500 = pd.read_parquet(market_dir / "benchmark.parquet").close
    benchmark_daily = sp500.pct_change(fill_method=None).reindex(nav.index)
    if not np.isfinite(benchmark_daily).all():
        raise ValueError("Missing S&P 500 return in the strategy calendar")
    marked = risk_metrics(nav.net_daily, benchmark_daily)
    marked["benchmark_return"] = float((1 + benchmark_daily).prod() - 1)
    result["sp500_total_return_metrics"] = marked

    if verify:
        verify_metrics(stats, original["metrics"])
        pd.testing.assert_frame_equal(
            nav, pd.read_parquet(ref / "daily.parquet"),
            atol=1e-8, rtol=0, check_exact=False,
        )
        for filename in ("targets.csv", "fills.csv", "attribution.csv"):
            pd.testing.assert_frame_equal(
                pd.read_csv(output / filename), pd.read_csv(ref / filename),
                atol=1e-8, rtol=0, check_exact=False,
            )
        reference = pd.read_csv(ref / "sp500_report.csv").query("case == 'normal'").iloc[0]
        verify_metrics(marked, {key: reference[key] for key in marked})
        result["reference_replay_verified"] = True
    (output / "comparison.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps({"sessions": stats["days"], "traded_fills": stats["fills"],
                      "net_return": stats["net_return"], "max_dd": stats["max_dd"],
                      "sp500_alpha": marked["alpha"], "verified": bool(verify)}, indent=2))


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--output", type=Path, default=ROOT / "replay_output")
    p.add_argument("--market-dir", type=Path, default=FIXTURE / "market")
    p.add_argument("--verify", action="store_true")
    args = p.parse_args()
    run(args.output, args.verify, args.market_dir.resolve())
