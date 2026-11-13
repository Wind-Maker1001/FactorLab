"""Run the frozen P7-R03 lifecycle experiment using an existing snapshot."""
import argparse
from factorlab.experiments.grid_lifecycle import reproduce_accounts, run_study


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--snapshot', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--design', default='configs/grid_lifecycle_nvda_hourly.json')
    parser.add_argument('--reproduce-of', help='Replay the three distinct active accounts from a completed study')
    args = parser.parse_args()
    if args.reproduce_of:
        reproduce_accounts(args.reproduce_of, args.output, snapshot=args.snapshot)
    else:
        run_study(args.output, snapshot=args.snapshot, design_path=args.design)


if __name__ == '__main__':
    main()
