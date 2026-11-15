"""Run bounded close-only Grid scenarios; never connects to a trading venue."""
import argparse
from dataclasses import replace
import json
from pathlib import Path
import sys

import pandas as pd

from factorlab.compute import ComputeConfig
from factorlab.experiments.grid_monte_carlo import historical_input, run_study
from factorlab.experiments.grid_paper import D
from factorlab.experiments.provenance import capture_provenance
from factorlab.strategies.grid import GridConfig
from factorlab.strategies.grid_execution import GridRules


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--snapshot', required=True)
    parser.add_argument('--as-of', required=True)
    parser.add_argument('--start', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--paths', type=int, default=16)
    parser.add_argument('--steps', type=int, default=168)
    parser.add_argument('--batch-size', type=int, default=8)
    parser.add_argument('--seed', type=int, default=710)
    parser.add_argument('--device', choices=['cpu', 'gpu'], default='cpu')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    config = replace(GridConfig(**json.loads((root/'configs/grid_paper_nvda_hourly.json').read_text())), mode='geometric')
    rules = GridRules(**{k: D(v) for k, v in json.loads((root/'configs/grid_paper_nvda_rules.json').read_text()).items()})
    inputs, receipt = historical_input(args.snapshot, args.as_of, config.initial_price)
    if pd.Timestamp(args.start) <= pd.Timestamp(args.as_of):
        raise ValueError('scenario start must follow input as_of')
    code = capture_provenance(root)['code']
    run_study(args.output, inputs=inputs, receipt=receipt, config=config, rules=rules,
        start=args.start, n_paths=args.paths, n_steps=args.steps, batch_size=args.batch_size,
        seed=args.seed, compute=ComputeConfig(device=args.device), code=code,
        command=[sys.executable, *sys.argv])


if __name__ == '__main__':
    main()
