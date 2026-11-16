"""Run frozen P7-R14 paired research; no network or trading-venue access."""
import argparse
from dataclasses import replace
import json
from pathlib import Path

from factorlab.experiments.grid_monte_carlo import historical_input
from factorlab.experiments.grid_paper import D
from factorlab.experiments.mc_regime_risk_budget import POLICY, run_study
from factorlab.experiments.provenance import capture_provenance
from factorlab.research.regime_jump import PathPlan
from factorlab.strategies.grid import GridConfig
from factorlab.strategies.grid_execution import GridRules


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--snapshot', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--config', default='configs/research/p7-r14-regime-risk-budget.json')
    parser.add_argument('--smoke', action='store_true', help='frozen 2-model/2-path/48-hour A/B engineering gate')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    design = json.loads(Path(args.config).read_text(encoding='utf-8'))
    if (design['version'] != 1 or design['policy'] != POLICY or design['lookback'] != 24
            or design['decision_lag_bars'] != 1 or design['baseline_mode'] != 'geometric'):
        raise ValueError('configuration does not identify the implemented frozen v1 policy')
    config = replace(GridConfig(**json.loads((root / design['baseline_config']).read_text())),
        mode=design['baseline_mode'])
    rules = GridRules(**{k: D(v) for k, v in json.loads((root / design['baseline_rules']).read_text()).items()})
    inputs, receipt = historical_input(args.snapshot, design['as_of'], config.initial_price)
    plan = PathPlan(n_paths=2 if args.smoke else design['paths'],
        n_steps=48 if args.smoke else design['steps'],
        batch_size=2 if args.smoke else design['batch_size'], seed=design['seed'],
        max_batch_bytes=design['max_batch_bytes'])
    command = ['python', 'tools/research/run_mc_regime_risk_budget.py',
        '--config', args.config, '--snapshot', '<SNAPSHOT>', '--output', '<NEW_OUTPUT>']
    if args.smoke:
        command.append('--smoke')
    result = run_study(args.output, inputs=inputs, receipt=receipt, config=config, rules=rules,
        start=design['start'], threshold_as_of=design['threshold_as_of'], plan=plan,
        models=('jump_background', 'jump_down') if args.smoke else tuple(design['models']),
        calibration_seed=design['calibration_seed'], calibration_paths=design['calibration_paths'],
        static_control=not args.smoke, capacity=design['capacity'],
        max_seconds=300 if args.smoke else design['max_seconds'],
        code=capture_provenance(root)['code'], command=command)
    print(json.dumps({key: result[key] for key in ('status', 'qualification', 'account_evaluations', 'elapsed_seconds')},
                     sort_keys=True), flush=True)
    return 0 if result['status'] == 'SUCCEEDED' else 2


if __name__ == '__main__':
    raise SystemExit(main())
