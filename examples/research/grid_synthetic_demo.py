"""Run or replay the deterministic synthetic grid comparison."""

import argparse
from pathlib import Path

from factorlab.experiments.grid_synthetic import run_experiment


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path, help="new evidence directory")
    parser.add_argument("--replay", type=Path, help="saved manifest.json")
    args = parser.parse_args()
    print(run_experiment(args.output, replay=args.replay))


if __name__ == "__main__":
    main()
