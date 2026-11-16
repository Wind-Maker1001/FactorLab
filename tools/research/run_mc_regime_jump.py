"""Run bounded MC-03 close-only research; never connects to a trading venue."""
import argparse
from dataclasses import replace
import json
from pathlib import Path
import sys

from factorlab.experiments.grid_monte_carlo import historical_input
from factorlab.experiments.grid_paper import D
from factorlab.experiments.mc_regime_jump import run_study
from factorlab.experiments.provenance import capture_provenance
from factorlab.research.regime_jump import PathPlan, RegimeConfig
from factorlab.strategies.grid import GridConfig
from factorlab.strategies.grid_execution import GridRules


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--snapshot', required=True)
    parser.add_argument('--as-of', required=True)
    parser.add_argument('--threshold-as-of', required=True, help='observation-time prefix boundary, not acquisition time')
    parser.add_argument('--start', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--paths', type=int, default=16)
    parser.add_argument('--steps', type=int, default=168)
    parser.add_argument('--batch-size', type=int, default=8)
    parser.add_argument('--seed', type=int, default=710)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    config = replace(GridConfig(**json.loads((root / 'configs/grid_paper_nvda_hourly.json').read_text())), mode='geometric')
    rules = GridRules(**{k: D(v) for k, v in json.loads((root / 'configs/grid_paper_nvda_rules.json').read_text()).items()})
    inputs, receipt = historical_input(args.snapshot, args.as_of, config.initial_price)
    run_study(args.output, inputs=inputs, receipt=receipt, config=config, rules=rules,
        start=args.start, threshold_as_of=args.threshold_as_of,
        plan=PathPlan(n_paths=args.paths, n_steps=args.steps, batch_size=args.batch_size, seed=args.seed),
        regime_config=RegimeConfig(), code=capture_provenance(root)['code'], command=[sys.executable, *sys.argv])


if __name__ == '__main__':
    main()
