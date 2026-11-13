"""Run the frozen independent-sample Grid study after all research gates pass."""
import argparse
from pathlib import Path

from factorlab.experiments.grid_independent import StudyBlocked, preflight, run_study


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--snapshot', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--repository', type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument('--preflight-only', action='store_true',
                        help='verify frozen identity and record admission without economic runs')
    args = parser.parse_args()
    try:
        if args.preflight_only:
            status = preflight(args.output, snapshot=args.snapshot, repository=args.repository)
            parser.exit(0 if status == 'PASS' else 2, f'research admission: {status}\n')
        run_study(args.output, snapshot=args.snapshot, repository=args.repository)
    except (StudyBlocked, FileNotFoundError) as error:
        parser.exit(2, f'P7_R04B_BLOCKED: {error}\n')


if __name__ == '__main__':
    main()
