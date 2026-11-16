"""Run bounded, offline MC-02 conditional tail reliability research."""
import argparse
import json
import sys

from factorlab.experiments.mc_tail_reliability import run_reliability


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--snapshot', required=True)
    parser.add_argument('--as-of', required=True)
    parser.add_argument('--start', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--budget-seconds', type=float, default=600.)
    parser.add_argument('--resamples', type=int, default=999)
    parser.add_argument('--steps', type=int, default=168)
    parser.add_argument('--batch-size', type=int, default=256)
    parser.add_argument('--grid-paths', type=int, default=2)
    args = parser.parse_args(argv)
    result = run_reliability(**vars(args), command=[sys.executable, *sys.argv])
    print(json.dumps(dict(status=result['status'], cells=len(result['rows']),
        skipped=len(result['skipped']), elapsed_seconds=result['elapsed_seconds'],
        engineering_status=result['engineering_status'], financial_status=result['financial_status'])))


if __name__ == '__main__':
    main()
