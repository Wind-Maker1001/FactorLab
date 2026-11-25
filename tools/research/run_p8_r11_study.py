"""CLI runner for FactorLab P8-R11 Unified F11/F08 Lifecycle Grid Study."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))
_existing_pp = os.environ.get("PYTHONPATH", "")
if str(SRC_ROOT) not in _existing_pp.split(os.pathsep):
    os.environ["PYTHONPATH"] = f"{SRC_ROOT}{os.pathsep}{_existing_pp}" if _existing_pp else str(SRC_ROOT)

from factorlab.experiments.p8_r11_study import run_p8_r11_study


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run FactorLab P8-R11 unified F11/F08 lifecycle comparative study (A/B/C/CASH x L0/L1/L2)."
    )
    parser.add_argument(
        "--r04b-snapshot",
        type=Path,
        required=True,
        help="Path to certified R04B snapshot directory.",
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        required=True,
        help="Path to local read-only R10 historical data cache directory.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("docs/validation/p8-r11"),
        help="Output directory for P8-R11 artifacts and REPORT.md.",
    )
    parser.add_argument("--screen-paths", type=int, default=64, help="MC screening paths per generator.")
    parser.add_argument("--expand-paths", type=int, default=512, help="MC expanded paths for key tail-risk generators.")
    parser.add_argument("--tail-expand-paths", type=int, default=1024, help="MC high-resolution tail paths for critical L2 pairs.")
    parser.add_argument("--cross-seed-paths", type=int, default=256, help="MC paths for cross-seed stability check.")
    parser.add_argument("--primary-seed", type=int, default=1100, help="Primary MC RNG seed.")
    parser.add_argument("--cross-seed", type=int, default=2200, help="Secondary MC RNG seed for cross-seed check.")
    parser.add_argument("--workers", type=int, default=None, help="ProcessPoolExecutor worker count.")
    args = parser.parse_args(argv)

    repo_root = Path(__file__).resolve().parents[2]
    study = run_p8_r11_study(
        repo_root=repo_root,
        r04b_snapshot=args.r04b_snapshot,
        cache_dir=args.cache_dir,
        output_dir=args.output_dir,
        screen_paths=args.screen_paths,
        expand_paths=args.expand_paths,
        tail_expand_paths=args.tail_expand_paths,
        cross_seed_paths=args.cross_seed_paths,
        primary_seed=args.primary_seed,
        cross_seed=args.cross_seed,
        max_workers=args.workers,
    )
    print(
        f"[P8-R11] Completed in {study['timing']['total_seconds']:.2f}s | "
        f"ENGINEERING_ACCEPTED={study['conclusions']['ENGINEERING_ACCEPTED']} | "
        f"RESEARCH_INCONCLUSIVE={study['conclusions']['RESEARCH_INCONCLUSIVE']} | "
        f"OOS_ADVANTAGE_SUPPORTED={study['conclusions']['OOS_ADVANTAGE_SUPPORTED']}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
