"""CLI entry point for FactorLab standardized external strategy independent validation (P10-04)."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from factorlab.research.external_validation.config import (
    ExternalValidationConfig,
    load_validation_config,
)
from factorlab.research.external_validation.pipeline import run_external_validation_pipeline


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("configs/research/p10_04_external_validation.json"),
        help="Path to frozen external validation configuration JSON.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="Output directory for evidence JSON/CSV artifacts and ExperimentTracker store.",
    )
    parser.add_argument("--skip-qlib", action="store_true", help="Skip isolated Qlib reference replay.")
    parser.add_argument("--skip-mc", action="store_true", help="Skip Tier-1 and Tier-2 Monte Carlo studies.")
    args = parser.parse_args(argv)

    cfg: ExternalValidationConfig = load_validation_config(args.config)
    result = run_external_validation_pipeline(
        cfg,
        args.output,
        skip_qlib=args.skip_qlib,
        skip_mc=args.skip_mc,
    )
    print(json.dumps(result["summary"], indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
