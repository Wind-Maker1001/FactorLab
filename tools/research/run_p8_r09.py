"""Repeatable CLI runner for FactorLab P8-R09: F11/F08 Complete Offline Lifecycle & Tail Risk Study."""
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

from factorlab.experiments.p8_r09_study import run_p8_r09_study


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cloud-snapshot",
        required=True,
        type=Path,
        help="Path to the read-only Azure Demo R08 snapshot directory.",
    )
    parser.add_argument(
        "--windows-snapshot",
        type=Path,
        default=None,
        help="Optional path to the sealed Windows pre-handoff snapshot directory.",
    )
    parser.add_argument(
        "--r04b-snapshot",
        required=True,
        type=Path,
        help="Path to the certified R04B NVDAUSDT 1h snapshot directory.",
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
        default=REPO_ROOT / "docs" / "validation" / "p8-r09",
        help="Output directory for P8-R09 CSV/JSON artifacts and REPORT.md.",
    )
    parser.add_argument("--n-paths", type=int, default=200, help="Primary paired stochastic paths per scenario.")
    parser.add_argument("--sensitivity-paths", type=int, default=128, help="Paths per execution/funding sensitivity cell.")
    parser.add_argument("--n-steps", type=int, default=168, help="Simulation horizon in hours (default 168 = 7d).")
    parser.add_argument("--seed", type=int, default=909, help="Deterministic RNG seed.")
    parser.add_argument("--workers", type=int, default=None, help="ProcessPoolExecutor worker count.")
    args = parser.parse_args(argv)

    study = run_p8_r09_study(
        cloud_snapshot=args.cloud_snapshot,
        windows_snapshot=args.windows_snapshot,
        r04b_snapshot=args.r04b_snapshot,
        r04b_funding_csv=args.r04b_funding_csv if args.r04b_funding_csv and args.r04b_funding_csv.is_file() else None,
        output_dir=args.output_dir,
        n_paths=args.n_paths,
        sensitivity_paths=args.sensitivity_paths,
        n_steps=args.n_steps,
        seed=args.seed,
        max_workers=args.workers,
    )
    brief = {
        "conclusions": {k: v for k, v in study["conclusions"].items() if k != "notes"},
        "official_replay_status": study["official_demo_replay"]["authoritative_replay"]["status"],
        "historical_rows": len(study["historical_windows"]["rows"]),
        "stochastic_rows": len(study["stochastic_paired"]),
        "sensitivity_rows": len(study["execution_and_funding_sensitivity"]),
        "deterministic_rows": len(study["deterministic_stress"]),
        "elapsed_seconds": study["configuration"]["elapsed_seconds"],
        "output_dir": str(args.output_dir),
    }
    print(json.dumps(brief, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
