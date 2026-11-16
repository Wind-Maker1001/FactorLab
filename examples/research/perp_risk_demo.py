"""Run frozen MC-04 risk scenarios without exchange access."""
import argparse
from factorlab.experiments.perp_risk import run

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True)
    run(parser.parse_args().output)
