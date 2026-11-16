"""Measure offline WAL account throughput before freezing MC-05-F01. NO LIVE MONEY."""
import argparse
import json
from pathlib import Path
import tempfile
from time import perf_counter

import numpy as np

from factorlab.experiments.grid_paper import D
from factorlab.experiments.mc_grid_tail_convergence import account_metrics, write
from factorlab.strategies.grid import GridConfig
from factorlab.strategies.grid_execution import GridRules


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--definition', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    definition = json.loads(Path(args.definition).read_text())
    config = GridConfig(**definition['grid'])
    rules = GridRules(**{k: D(v) for k, v in definition['rules'].items()})
    rows = []
    started = perf_counter()
    with tempfile.TemporaryDirectory(prefix='mc05-f01-planning-') as temporary:
        for name, end in (('flat', 1.), ('decline', .6)):
            before = perf_counter()
            metrics = account_metrics(np.linspace(config.initial_price, config.initial_price*end,
                definition['experiment']['steps']+1), start=definition['start'], config=config,
                rules=rules, capacity=definition['capacity'], output=Path(temporary)/name)
            rows.append(dict(scenario=name, seconds=perf_counter()-before, metrics=metrics))
    seconds = max(row['seconds'] for row in rows)
    safety = definition['experiment']['cost_safety_factor']
    elapsed = perf_counter()-started
    write(args.output, dict(purpose='pre-freeze throughput only; excluded from probability samples',
        accounts=rows, total_seconds=elapsed, planning_seconds_per_account=seconds,
        budget_seconds=900, cost_safety_factor=safety,
        projected_first_group_seconds=48*seconds*safety,
        projected_two_sizes_seconds=96*seconds*safety,
        first_group_admissible=elapsed+48*seconds*safety < 900))


if __name__ == '__main__':
    main()
