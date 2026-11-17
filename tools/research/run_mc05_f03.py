"""Run the frozen MC-05-F03 offline experiment. NO LIVE MONEY."""
from time import perf_counter

STARTED = perf_counter()

import argparse
from copy import deepcopy
import json
from pathlib import Path

from factorlab.experiments.grid_monte_carlo import historical_input
from factorlab.experiments.grid_paper import D
from factorlab.experiments.mc_grid_tail_convergence import run_study, write
from factorlab.strategies.grid import GridConfig
from factorlab.strategies.grid_execution import GridRules


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('phase', choices=('pilot', 'formal', 'report'))
    parser.add_argument('--snapshot', required=True)
    parser.add_argument('--definition', default='configs/mc05_f03_grid_tail.json')
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--calibration', type=Path)
    args = parser.parse_args()

    study = read_json(args.definition)
    parent = read_json(study['frozen_definition'])
    definition = deepcopy(parent)
    definition.update(study)
    planning = definition['resource_planning']
    planning.pop('receipt')
    definition.pop('output_directory')
    config = GridConfig(**definition['grid'])
    rules = GridRules(**{key: D(value) for key, value in definition['rules'].items()})
    inputs, receipt = historical_input(args.snapshot, definition['as_of'], config.initial_price)
    if receipt != definition['dataset']:
        raise ValueError('F03 requires the complete F02 frozen data calibration identity')
    context = dict(inputs=inputs, receipt=receipt, config=config, rules=rules, definition=definition)

    from factorlab.experiments.mc_grid_tail_f03_financial import capture_account_diagnostics
    from factorlab.experiments.mc_grid_tail_f03_resources import calibrate, ResourceMonitor, ResourcePlanner

    if args.phase == 'pilot':
        launch_overhead = perf_counter() - STARTED
        calibration = calibrate(args.output, **context, diagnostic=capture_account_diagnostics)
        calibration['cli_preparation_wall_seconds'] = launch_overhead
        write(args.output/'calibration.json', calibration)
        return
    if args.calibration is None:
        parser.error('--calibration is required for formal and report')
    calibration = read_json(args.calibration)
    if args.phase == 'formal':
        planner = ResourcePlanner(calibration, definition)
        definition['resource_planning'] = {
            'expected_temporary_disk_bytes': planning['expected_temporary_disk_bytes'],
            'method': 'F03 end-to-end measured parallel throughput',
        }
        from factorlab.experiments.mc_grid_tail_f03_statistics import PROTOCOL
        definition['convergence_protocol'] = PROTOCOL
        monitor = ResourceMonitor()
        launch_overhead = perf_counter() - STARTED
        try:
            with monitor.measure() as actual:
                run_study(args.output, **context, max_workers=definition['resources']['max_workers'],
                    resource_planner=planner, group_observer=monitor.observe,
                    diagnostic=capture_account_diagnostics)
        finally:
            if args.output.is_dir():
                actual['cli_preparation_wall_seconds'] = launch_overhead
                write(args.output/'resources.json', actual)
    else:
        from factorlab.experiments.mc_grid_tail_f03_report import report
        report(args.output, calibration=calibration, **context)


if __name__ == '__main__':
    main()
