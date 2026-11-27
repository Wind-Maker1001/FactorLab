"""MC-GRID-OPT-01 static-grid parameter research. NO LIVE MONEY."""
import argparse
from dataclasses import replace
import json
from pathlib import Path
import sys

from factorlab.experiments.grid_monte_carlo import historical_input
from factorlab.experiments.grid_parameter_space import _build, describe, load_base, one_factor, sample_configs
from factorlab.experiments.grid_parameter_study import calibration_prefix, run_cell, write_json
from factorlab.experiments.mc_regime_jump import scenario_models


def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument('--definition', type=Path, default=Path('configs/research/mc_grid_opt_01.json'))
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--stage', choices=('freeze-history', 'pilot', 'A', 'B', 'C', 'D'), required=True)
    parser.add_argument('--previous', type=Path)
    parser.add_argument('--historical-manifest', type=Path)
    parser.add_argument('--backend', choices=('cpu', 'cuda'), default='cuda')
    parser.add_argument('--pilot-paths', type=int, default=16)
    args = parser.parse_args()
    settings = json.loads(args.definition.read_text())
    if args.stage == 'freeze-history':
        from factorlab.experiments.grid_parameter_validation import freeze_windows
        manifest = freeze_windows(settings['snapshot'],
            start=settings['historical_validation']['start'], end=settings['historical_validation']['end'])
        args.output.mkdir(parents=True, exist_ok=False)
        write_json(args.output / 'historical-manifest.json', manifest)
        return
    frozen = json.loads(Path(settings['baseline']).read_text())
    base = load_base(settings['baseline'])
    inputs, receipt = historical_input(settings['snapshot'], frozen['as_of'], base.grid.initial_price)
    for field in ('data_version', 'bars_sha256'):
        if receipt[field] != frozen['dataset'][field]:
            raise ValueError(f'frozen calibration mismatch: {field}')
    inputs, receipt = calibration_prefix(inputs, receipt, settings['calibration_end'])
    generators, rejection = scenario_models(inputs, receipt, threshold_as_of=frozen['threshold_as_of'])
    missing = set(settings['models']) - {'gbm', 'moving_block_bootstrap'} - set(generators)
    if missing:
        raise ValueError(f'required model calibration failed: {sorted(missing)}: {rejection}')
    output = args.output
    output.mkdir(parents=True, exist_ok=False)
    import shutil
    source = Path(__file__).resolve().parents[2]
    archive = output / 'source'
    for relative in ('tools/research/run_grid_parameter_study.py',
                     'src/factorlab/experiments/grid_parameter_space.py',
                     'src/factorlab/experiments/grid_parameter_study.py',
                     'src/factorlab/experiments/grid_parameter_validation.py',
                     'src/factorlab/experiments/grid_parameter_inference.py',
                     'src/factorlab/research/grid_simulation/__init__.py'):
        target = archive / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / relative, target)
    axis_rows = one_factor(base)
    rejected = [{'id': key, 'axis': axis, 'value': value}
                for key, axis, value, config in axis_rows if config is None]
    configs = {'baseline': base, 'arithmetic-baseline': replace(base, grid=replace(base.grid, mode='arithmetic'))}
    selection = None
    if args.stage in ('B', 'C', 'D'):
        from factorlab.experiments.grid_parameter_inference import summarize_stage
        if args.previous is None:
            raise ValueError('later stages require --previous completed-stage directory')
        if args.historical_manifest is None:
            raise ValueError('historical validation manifest must be frozen before selection')
        historical = json.loads(args.historical_manifest.read_text())
        import pandas as pd
        if pd.Timestamp(historical['calibration_end_close_endpoint']) != pd.Timestamp(settings['calibration_end']):
            raise ValueError('calibration and historical-validation boundary mismatch')
        if args.stage in ('C', 'D'):
            prior_manifest = json.loads((args.previous / 'historical-manifest.json').read_text())
            if prior_manifest['id'] != historical['id']:
                raise ValueError('historical validation manifest changed after selection began')
        write_json(output / 'historical-manifest.json', historical)
        previous = json.loads((args.previous / 'definition.json').read_text())
        if previous['stage'] != {'B': 'A', 'C': 'B', 'D': 'C'}[args.stage]:
            raise ValueError('previous stage is not the required predecessor')
        cells = [json.loads(path.read_text()) for path in args.previous.glob('*/status.json')]
        expected = {(model, replicate) for model in previous['settings']['models']
                    for replicate in range(previous['settings'][previous['stage']]['replicates'])}
        if (len(cells) != len(expected) or any(cell['status'] != 'COMPLETE' for cell in cells)
                or {(cell['model'], cell['replicate']) for cell in cells} != expected):
            raise ValueError('previous stage has incomplete or failed model/replicate cells')
        previous_summary = summarize_stage(args.previous)
        if args.stage == 'B':
            candidates, failures = sample_configs(base, settings['B']['configurations'], settings['design_seed'])
            configs.update(candidates)
            rejected.extend(failures)
            write_json(output / 'screening-design.json', {'previous_stage': str(args.previous),
                'A_summary': previous_summary,
                'design': 'balanced marginal strata retain inactive single-factor levels for interaction testing',
                'selection_data': 'A only; historical validation is frozen and not scored'})
        else:
            if args.stage == 'C':
                from factorlab.experiments.grid_parameter_inference import select_refinement
                selection = select_refinement(args.previous, settings['C']['configurations'])
            else:
                selection = json.loads((args.previous / 'selection.json').read_text())['ids']
            configs.update({key: _build(previous['configurations'][key]) for key in selection})
            write_json(output / 'selection.json', {'ids': selection, 'source': str(args.previous),
                'rule': 'Pareto layers and objective-space coverage; no unique utility optimum'})
    else:
        configs.update({key: config for key, _, _, config in axis_rows if config is not None})
    stage = 'A' if args.stage == 'pilot' else args.stage
    if args.stage == 'pilot':
        if not 1 <= args.pilot_paths <= 100000:
            raise ValueError('pilot paths must be in [1,100000]')
        settings[stage] = settings[stage] | {'paths': args.pilot_paths, 'replicates': 1}
        configs = dict(list(configs.items())[:4])
    write_json(output / 'definition.json', {'settings': settings, 'calibration': receipt,
        'command': [sys.executable, *sys.argv], 'stage': args.stage,
        'configurations': {key: describe(config) for key, config in configs.items()},
        'one_factor_axes': [{'id': key, 'axis': axis, 'value': value}
                            for key, axis, value, _ in axis_rows], 'rejected': rejected,
        'models': {key: value.receipt() for key, value in generators.items() if key in settings['models']}})
    for model in settings['models']:
        for replicate in range(settings[stage]['replicates']):
            cell = run_cell(output, configs=list(configs.items()), model=model, stage=stage,
                replicate=replicate, settings=settings, inputs=inputs,
                generator=generators.get(model), backend=args.backend)
            print(json.dumps({'model': model, 'replicate': replicate, 'paths': cell['paths'],
                              'configurations': len(configs), 'seconds': cell['seconds']}), flush=True)
    if args.stage in ('C', 'D'):
        from factorlab.experiments.grid_parameter_inference import summarize_stage
        write_json(output / 'summary.json', summarize_stage(output))
    if args.stage == 'D':
        from factorlab.experiments.grid_parameter_inference import assess_validation
        from factorlab.experiments.grid_parameter_validation import replay_windows
        assessment = assess_validation(output, selection, searched=settings['B']['configurations'])
        write_json(output / 'assessment.json', assessment)
        write_json(output / 'historical-results.json', replay_windows(historical, list(configs.items()), backend=args.backend))
        from factorlab.experiments.grid_parameter_study import audit_population
        audited = ['baseline', *(assessment['final_ids'] or selection[:3])]
        write_json(output / 'paper-audits.json', audit_population(output, configs={key: configs[key] for key in audited},
            settings=settings, inputs=inputs, generators=generators, backend=args.backend))


if __name__ == '__main__':
    main()
