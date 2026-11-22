"""Repeatable CLI runner for FactorLab R08: Binance Demo Grid Empirical Performance Analysis."""
from __future__ import annotations

import argparse
from decimal import Decimal
import json
import os
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))
existing_pythonpath = os.environ.get("PYTHONPATH", "")
if str(SRC_ROOT) not in existing_pythonpath.split(os.pathsep):
    os.environ["PYTHONPATH"] = (
        f"{SRC_ROOT}{os.pathsep}{existing_pythonpath}" if existing_pythonpath else str(SRC_ROOT)
    )

from factorlab.experiments.demo_grid_performance import (
    DEFAULT_OPENING_WALLET,
    DEFAULT_STRATEGY_CAP_USDT,
    run_full_r08_analysis,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cloud-snapshot",
        required=True,
        type=Path,
        help="Path to the verified read-only Azure cloud snapshot directory.",
    )
    parser.add_argument(
        "--windows-snapshot",
        type=Path,
        default=None,
        help="Optional path to the sealed Windows pre-handoff snapshot for deduplication audit.",
    )
    parser.add_argument(
        "--r04b-snapshot",
        type=Path,
        default=None,
        help="Optional path to certified R04B NVDAUSDT 1h snapshot for historical window replay & MC/Bootstrap.",
    )
    parser.add_argument(
        "--r04b-funding-csv",
        type=Path,
        default=REPO_ROOT / "docs" / "validation" / "p7-r09" / "R04B-base-baseline-funding-settlements.csv",
        help="Optional path to historical R04B funding settlements CSV.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=REPO_ROOT / "docs" / "validation" / "p8-r08",
        help="Output directory for compact sanitized JSON, CSV, and REPORT.md artifacts.",
    )
    parser.add_argument("--mc-paths", type=int, default=64, help="Stochastic paths per tail-risk scenario.")
    parser.add_argument("--mc-steps", type=int, default=168, help="Horizon in hours for tail-risk paths (default 168 = 7d).")
    parser.add_argument("--seed", type=int, default=808, help="Deterministic RNG seed for Monte Carlo / Bootstrap.")
    parser.add_argument(
        "--opening-wallet",
        type=Decimal,
        default=DEFAULT_OPENING_WALLET,
        help="Opening Demo account wallet balance in USDT (default 5000.00000000).",
    )
    parser.add_argument(
        "--strategy-cap-usdt",
        type=Decimal,
        default=DEFAULT_STRATEGY_CAP_USDT,
        help="Per-Grid committed-cash cap in USDT (default 7.00000000).",
    )
    args = parser.parse_args(argv)

    analysis = run_full_r08_analysis(
        args.cloud_snapshot,
        windows_snapshot=args.windows_snapshot,
        r04b_snapshot=args.r04b_snapshot,
        r04b_funding_csv=args.r04b_funding_csv if args.r04b_funding_csv and args.r04b_funding_csv.is_file() else None,
        output_dir=args.output_dir,
        mc_paths=args.mc_paths,
        mc_steps=args.mc_steps,
        seed=args.seed,
        opening_wallet=args.opening_wallet,
        strategy_cap_usdt=args.strategy_cap_usdt,
    )
    emp = analysis["empirical"]
    brief = {
        "consistency_all_passed": analysis["consistency"]["all_checks_passed"],
        "cloud_is_strict_superset": analysis["dedup_audit"].get("cloud_is_strict_superset"),
        "fifo_reconciled_within_quantum": emp["fifo_reconciliation"]["reconciled_within_quantum"],
        "final_equity_usdt": emp["fifo_reconciliation"]["final_equity_usdt"],
        "grid_net_pnl_usdt": emp["cohorts"]["all_grid"]["net_pnl_usdt"],
        "grid_net_return_on_7u_pct": emp["capital_basis_metrics"]["strategy_committed_cap_base"]["grid_net_return_pct"],
        "output_dir": str(args.output_dir),
    }
    print(json.dumps(brief, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
