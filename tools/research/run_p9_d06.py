"""Recheck the frozen fold against the accepted D05 ledger without new marks."""
import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run_p9_d05 import run


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('original', 'study', 'd03', 'd04', 'd05', 'evidence-root', 'bigquant-root', 'output'):
        parser.add_argument('--' + name, required=True, type=Path)
    args = parser.parse_args()
    run(args, continuation=args.d05)
