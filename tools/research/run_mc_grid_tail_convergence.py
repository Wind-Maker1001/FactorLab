"""Run frozen MC-05 offline Paper account research. NO LIVE MONEY."""
import argparse
import json
from pathlib import Path
import sys

from factorlab.experiments.grid_monte_carlo import historical_input
from factorlab.experiments.grid_paper import D
from factorlab.experiments.mc_grid_tail_convergence import run_study
from factorlab.experiments.provenance import capture_provenance
from factorlab.strategies.grid import GridConfig
from factorlab.strategies.grid_execution import GridRules


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--snapshot', required=True)
    parser.add_argument('--definition', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    definition = json.loads(Path(args.definition).read_text())
    config = GridConfig(**definition['grid'])
    rules = GridRules(**{k: D(v) for k, v in definition['rules'].items()})
    inputs, receipt = historical_input(args.snapshot, definition['as_of'], config.initial_price)
    for field in ('data_version', 'bars_sha256'):
        if receipt[field] != definition['dataset'][field]:
            raise ValueError(f'frozen input identity differs: {field}')
    run_study(args.output, inputs=inputs, receipt=receipt, config=config, rules=rules,
        definition=definition, code=capture_provenance(Path(__file__).resolve().parents[2])['code'],
        command=[sys.executable, *sys.argv])


if __name__ == '__main__':
    main()
