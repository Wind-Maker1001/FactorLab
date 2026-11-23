"""Repeatable CLI runner for FactorLab P8-R10: Funding-Sensitive & Volatility-Adaptive Grid Study."""
from __future__ import annotations

import argparse
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

from factorlab.experiments.grid_r10_study import run_full_r10_study


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--r04b-snapshot",
        type=Path,
        required=True,
        help="Path to certified R04B NVDAUSDT 1h snapshot directory.",
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=REPO_ROOT.parent / "p8-r10-data-cache",
        help="Local cache directory for corroborated R07/Post-R07 Binance public 1h bars and funding.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=REPO_ROOT / "docs" / "validation" / "p8-r10",
        help="Output directory for P8-R10 CSV, JSON, and REPORT.md artifacts.",
    )
    parser.add_argument("--mc-paths", type=int, default=64, help="Stochastic paths per Monte Carlo scenario.")
    parser.add_argument("--mc-steps", type=int, default=168, help="Hours per scenario path (default 168 = 7d).")
    parser.add_argument("--workers", type=int, default=6, help="Worker processes for parallel Paper backtests.")
    parser.add_argument(
        "--offline-only",
        action="store_true",
        help="Disable public Binance supplement fetch and use local cache/R04B only.",
    )
    args = parser.parse_args(argv)

    summary = run_full_r10_study(
        repo_root=REPO_ROOT,
        r04b_snapshot=args.r04b_snapshot,
        cache_dir=args.cache_dir,
        output_dir=args.output_dir,
        mc_paths=args.mc_paths,
        mc_steps=args.mc_steps,
        max_workers=args.workers,
        allow_network_supplement=not args.offline_only,
    )
    brief = {
        "execution_model": summary["execution_model"],
        "elapsed_seconds": summary["elapsed_seconds"],
        "verdict": summary["verdict"],
        "output_dir": str(args.output_dir),
    }
    print(json.dumps(brief, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
