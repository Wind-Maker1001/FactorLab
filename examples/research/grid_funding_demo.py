"""Overlay observed historical funding without executing strategies."""
import argparse
from factorlab.experiments.grid_funding import run

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--evidence', required=True)
parser.add_argument('--funding', required=True)
parser.add_argument('--output', required=True)
if __name__ == '__main__':
    args = parser.parse_args()
    run(args.evidence, args.funding, args.output)
