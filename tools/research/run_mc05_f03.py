"""Run the frozen MC-05-F03 offline experiment. NO LIVE MONEY."""
from time import perf_counter
import argparse
from copy import deepcopy
import json
from pathlib import Path

STARTED = perf_counter()

from factorlab.experiments.grid_monte_carlo import historical_input
from factorlab.experiments.grid_paper import D
from factorlab.experiments.mc_grid_tail_convergence import run_study, write
from factorlab.strategies.grid import GridConfig
from factorlab.strategies.grid_execution import GridRules


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def read_calibration(path, definition):
    calibration = read_json(path)
    pilot = read_json(path.with_name('definition.json'))
    for field in ('dataset', 'models', 'model_parameters', 'bootstrap', 'grid', 'rules', 'capacity',
                  'costs', 'start', 'threshold_as_of', 'resources'):
        if pilot[field] != definition[field]:
            raise ValueError(f'parallel pilot and formal study disagree: {field}')
    expected = definition['experiment'] | dict(seeds=[definition['resources']['pilot_seed']],
        sizes=[definition['resources']['pilot_paths_per_model']],
        budget_seconds=definition['resources']['pilot_budget_seconds'])
    if pilot['experiment'] != expected or calibration['max_workers'] != definition['resources']['max_workers']:
        raise ValueError('parallel pilot and formal study differ in replay settings')
    return calibration


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
    calibration = read_calibration(args.calibration, definition)
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
        output_existed = args.output.exists()
        try:
            with monitor.measure() as actual:
                run_study(args.output, **context, max_workers=definition['resources']['max_workers'],
                    resource_planner=planner, group_observer=monitor.observe,
                    diagnostic=capture_account_diagnostics)
        finally:
            if not output_existed and args.output.is_dir():
                actual['cli_preparation_wall_seconds'] = launch_overhead
                write(args.output/'resources.json', actual)
    else:
        from factorlab.experiments.mc_grid_tail_f03_report import report
        executed = read_json(args.output/'definition.json')
        run_keys = ('research_id', 'experiment', 'dataset', 'grid', 'rules', 'start', 'threshold_as_of',
                    'costs', 'capacity', 'models', 'model_parameters', 'model_versions', 'bootstrap')
        for field in run_keys:
            if executed[field] != definition[field]:
                raise ValueError(f'report definition differs from completed run: {field}')
        report(args.output, calibration=calibration, **context)


if __name__ == '__main__':
    main()
