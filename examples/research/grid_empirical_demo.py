"""Offline fixed-parameter hourly Grid study; existing snapshots only."""
import argparse
from pathlib import Path

from factorlab.experiments.grid_empirical import run_study


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--snapshot', type=Path, required=True)
    parser.add_argument('--funding-snapshot', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--config', type=Path, default=Path('configs/grid_paper_nvda_hourly.json'))
    parser.add_argument('--rules', type=Path, default=Path('configs/grid_paper_nvda_rules.json'))
    args = parser.parse_args()
    run_study(args.output, snapshot=args.snapshot, funding_snapshot=args.funding_snapshot,
              config_path=args.config, rules_path=args.rules)


if __name__ == '__main__':
    main()
