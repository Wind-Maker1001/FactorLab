"""Python 3.9 source entry point. Synthetic data only; no network mode."""

import argparse

from .runner import run_synthetic


def main():
    parser = argparse.ArgumentParser(
        description="SYNTHETIC_TEST_ONLY / NO LIVE MONEY / REAL_RESEARCH_BLOCKED"
    )
    parser.add_argument(
        "--output", required=True, help="fresh output directory; never overwrite"
    )
    parser.add_argument(
        "--frequencies", nargs="+", type=int, choices=(5, 15, 30), default=[5, 15, 30]
    )
    parser.add_argument("--seed", type=int, default=20261018)
    args = parser.parse_args()
    receipt, _ = run_synthetic(args.output, tuple(args.frequencies), args.seed)
    print(receipt["software_status"] + " / " + receipt["real_research_status"])


if __name__ == "__main__":
    main()
