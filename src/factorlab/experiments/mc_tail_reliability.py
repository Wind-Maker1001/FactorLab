"""Bounded CPU reliability experiments using immutable historical inputs."""
from dataclasses import asdict, replace
import json
from pathlib import Path
import tempfile
from time import perf_counter

import numpy as np
import pandas as pd

from factorlab.compute import ComputeConfig
from factorlab.experiments.grid_monte_carlo import historical_input, evaluate_path
from factorlab.experiments.grid_paper import D
from factorlab.experiments.provenance import capture_provenance
from factorlab.research.monte_carlo import MonteCarloConfig, MonteCarloRunner
from factorlab.research.monte_carlo.reliability import summarize_paths, repeat_summary
from factorlab.strategies.grid import GridConfig
from factorlab.strategies.grid_execution import GridRules

SEEDS = (1729, 2718, 31415, 65537, 104729)
SIZES = (100, 1000, 10000)
MODELS = ('gbm', 'moving_block_bootstrap')


def calibrate_window(inputs, observations):
    """Reestimate hourly GBM parameters from only eligible trailing returns."""
    if type(observations) is not int or not 2 <= observations <= len(inputs.historical_returns):
        raise ValueError('window must contain 2 through available return count observations')
    returns = np.asarray(inputs.historical_returns[-observations:])
    times = np.asarray(inputs.timestamps[-observations:])
    availability = np.asarray(inputs.available_at[-observations:])
    if np.any(times > inputs.as_of) or np.any(availability > inputs.as_of):
        raise ValueError('window contains future or unavailable observations')
    logs = np.log1p(returns)
    volatility = logs.std(axis=0, ddof=1)
    return replace(inputs, historical_returns=returns, timestamps=times, available_at=availability,
        drift=logs.mean(axis=0)+.5*volatility**2, volatility=volatility)


def oos_feasibility(inputs):
    """Evaluate whether this input supplies any realized post-availability horizon."""
    last_observation = np.max(inputs.timestamps)
    acquisition = np.max(inputs.available_at)
    retrospective = bool(acquisition >= last_observation)
    return dict(status='INSUFFICIENT_DATA', performed=False,
        last_observation=str(last_observation), latest_availability=str(acquisition),
        reason=('snapshot acquired after all observed history; no realized post-availability evaluation data'
                if retrospective else 'input has no separately evidenced post-calibration evaluation sample'),
        retrospective_split_is_pit_verified=False, observed_coverage=None)


def _write(path, value):
    path.write_text(json.dumps(value, indent=2, allow_nan=False, default=str)+'\n', encoding='utf-8')


def run_reliability(output, *, snapshot, as_of, start, budget_seconds=600.,
                    seeds=SEEDS, sizes=SIZES, resamples=999, steps=168,
                    batch_size=256, grid_paths=2, command=()):
    """Run complete seed groups when measured costs fit, recording every skipped cell.

    The wall budget is cooperative between operations, not hard preemption.
    Grid paths are the first prespecified indices, never selected by outcome.
    """
    if budget_seconds <= 0 or not seeds or len(set(seeds)) != len(seeds):
        raise ValueError('positive budget and distinct nonempty seeds required')
    if not sizes or any(type(n) is not int or n < 2 for n in sizes) or tuple(sorted(set(sizes))) != tuple(sizes):
        raise ValueError('sizes must be distinct increasing integers at least two')
    if type(grid_paths) is not int or not 0 <= grid_paths <= 4:
        raise ValueError('grid_paths must be in [0, 4] per model')
    if not 0 < batch_size <= 256:
        raise ValueError('batch_size must be in [1, 256]')
    if resamples < 2 or steps < 2:
        raise ValueError('at least two resamples and horizon steps required')
    started = perf_counter()
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    root = Path(__file__).resolve().parents[3]
    config = replace(GridConfig(**json.loads((root/'configs/grid_paper_nvda_hourly.json').read_text())), mode='geometric')
    rules = GridRules(**{k: D(v) for k, v in json.loads((root/'configs/grid_paper_nvda_rules.json').read_text()).items()})
    inputs, receipt = historical_input(snapshot, as_of, config.initial_price)
    if pd.Timestamp(start) <= pd.Timestamp(as_of):
        raise ValueError('scenario start must follow calibration as_of')
    windows = (1440, 720, len(inputs.historical_returns))
    if len(inputs.historical_returns) < 1440:
        raise ValueError('prespecified real-input design requires at least 1440 hourly returns')
    windows = tuple(dict.fromkeys(windows))
    window_inputs = {w: calibrate_window(inputs, w) for w in windows}
    definition = dict(schema_version=1, input=receipt, code=capture_provenance(root)['code'],
        command=list(command), seeds=list(seeds), sizes=list(sizes), diagnostic_paths=16,
        windows=list(windows), primary_window=1440, block_sizes=[6, 24, 72], primary_block=24,
        steps=steps, dt=1., confidence_levels=[.95, .99], interval_coverage=.95,
        resamples=resamples, bootstrap_seed_rule='(simulation_seed + 1000003) mod 2**32',
        batch_size=batch_size, device='cpu', budget_seconds=budget_seconds,
        grid_paths_per_model=grid_paths, grid_seed=seeds[0], grid_path_selection='first indices, primary window, block 24',
        grid_config=asdict(config), grid_rules={k:str(v) for k,v in asdict(rules).items()},
        grid_costs=dict(fee_rate='.0004', slippage='.0006', funding='NOT_SIMULATED', synthetic_capacity='.10'),
        design='primary sizes then one-axis sensitivities at largest affordable completed size; no full factorial',
        boundaries=['NO LIVE MONEY', 'FINANCIAL_NOT_ESTABLISHED', 'FORMAL_ADMISSION_BLOCKED'])
    _write(output/'definition.json', definition)
    rows, skipped, grid_rows = [], [], []
    group_costs = {}
    reserve = min(150., budget_seconds*.25)
    main_deadline = started+budget_seconds-reserve

    def elapsed():
        return perf_counter()-started

    def persist():
        _write(output/'results.json', dict(rows=rows, skipped=skipped, seed_summaries=repeat_summary(rows),
            grid=grid_rows, rolling_oos=oos_feasibility(inputs), elapsed_seconds=elapsed(),
            status='RUNNING', financial_status='FINANCIAL_NOT_ESTABLISHED'))

    def run_cell(model, window, block, n, seed, phase):
        begin = perf_counter()
        cfg = MonteCarloConfig(model=model, n_paths=n, n_steps=steps, seed=seed,
            dt=1., batch_size=batch_size, block_size=block, compute=ComputeConfig(device='cpu'))
        result = MonteCarloRunner(cfg).run(window_inputs[window])
        stats = summarize_paths(1-result.terminal_values/cfg.initial_value, result.maximum_drawdowns,
            resamples=resamples, bootstrap_seed=(seed+1000003) % 2**32)
        row = dict(phase=phase, model=model, window=window, block_size=block, n_paths=n,
                   seed=seed, seconds=perf_counter()-begin, statistics=stats, generation=result.metadata)
        rows.append(row)
        persist()
        return row['seconds']

    def group(model, window, block, n, phase, deadline, predicted=0.):
        if perf_counter()+predicted > deadline:
            skipped.append(dict(phase=phase, model=model, window=window, block_size=block,
                n_paths=n, seeds=list(seeds), reason='TIME_BUDGET', predicted_seconds=predicted))
            persist()
            return False
        costs = []
        for index, seed in enumerate(seeds):
            if perf_counter() >= deadline:
                skipped.append(dict(phase=phase, model=model, window=window, block_size=block,
                    n_paths=n, seeds=list(seeds[index:]), reason='TIME_BUDGET_IN_FLIGHT_GROUP'))
                persist()
                return False
            costs.append(run_cell(model, window, block, n, seed, phase))
        group_costs[(model, window, block, n)] = sum(costs)
        return True

    for model in MODELS:
        group(model, 1440, 24, 16, 'diagnostic', main_deadline)
    # Spend at most half the main budget on primary scales, retaining one-axis sensitivity time.
    primary_deadline = started+(budget_seconds-reserve)*.55
    completed = {model: [] for model in MODELS}
    for n in sizes:
        for model in MODELS:
            previous = completed[model][-1] if completed[model] else 16
            predicted = group_costs.get((model, 1440, 24, previous), 0.) * n/previous * 1.25
            if group(model, 1440, 24, n, 'primary', primary_deadline, predicted):
                completed[model].append(n)
    sensitivity_size = min([max(v) for v in completed.values() if v] or [sizes[0]])
    sensitivity = [(model, w, 24) for w in windows if w != 1440 for model in MODELS]
    sensitivity += [('moving_block_bootstrap', 1440, block) for block in (6, 72)]
    for model, window, block in sensitivity:
        predicted = group_costs.get((model, 1440, 24, sensitivity_size), 0.)*1.25
        group(model, window, block, sensitivity_size, 'sensitivity', main_deadline, predicted)

    # Only this bounded interface probe retains temporary price histories.
    for model in MODELS:
        if not grid_paths:
            skipped.append(dict(phase='grid', model=model, reason='DISABLED', path_indices=[]))
            continue
        if elapsed() >= budget_seconds:
            skipped.append(dict(phase='grid', model=model, reason='TIME_BUDGET', path_indices=list(range(grid_paths))))
            continue
        cfg = MonteCarloConfig(model=model, n_paths=grid_paths, n_steps=steps, seed=seeds[0],
            dt=1., batch_size=batch_size, block_size=24, save_full_paths=True,
            compute=ComputeConfig(device='cpu'))
        result = MonteCarloRunner(cfg).run(window_inputs[1440])
        for i, prices in enumerate(result.asset_paths[:, :, 0]):
            if elapsed() >= budget_seconds:
                skipped.append(dict(phase='grid', model=model, reason='TIME_BUDGET', path_indices=list(range(i,grid_paths))))
                break
            begin = perf_counter()
            with tempfile.TemporaryDirectory(prefix='factorlab-mc02-') as temporary:
                account = evaluate_path(prices, start=start, config=config, rules=rules,
                    capacity='.10', output=Path(temporary)/'account')
            grid_rows.append(dict(model=model, seed=seeds[0], path_index=i, seconds=perf_counter()-begin,
                metrics=account, generation=result.metadata,
                qualification='ENGINEERING_INTERFACE_ONLY_NOT_GRID_TAIL_CONVERGENCE'))
            persist()
        del result
    grid_statistics = {}
    for model in MODELS:
        sample = [r['metrics'] for r in grid_rows if r['model']==model]
        if sample:
            grid_statistics[model] = summarize_paths([1-r['terminal_nav']/1000 for r in sample],
                [r['maximum_drawdown'] for r in sample], resamples=resamples,
                bootstrap_seed=(seeds[0]+1000003) % 2**32)
    result = dict(rows=rows, skipped=skipped, seed_summaries=repeat_summary(rows),
        grid=grid_rows, grid_statistics=grid_statistics, rolling_oos=oos_feasibility(inputs),
        elapsed_seconds=elapsed(), budget_overrun_seconds=max(0., elapsed()-budget_seconds),
        status='COMPLETED', engineering_status='ENGINEERING_VALID' if grid_rows and all(
            abs(r['metrics']['reconciliation_residual']) < 1e-8 for r in grid_rows) else 'NOT_ESTABLISHED',
        financial_status='FINANCIAL_NOT_ESTABLISHED', minimum_sample_size='NOT_DETERMINED_NO_PRESPECIFIED_PRECISION_TOLERANCE')
    _write(output/'results.json', result)
    flattened = []
    for row in rows:
        for c, tail in row['statistics']['tails'].items():
            flattened.append({k:row[k] for k in ('phase','model','window','block_size','n_paths','seed','seconds')} | dict(
                confidence=c, var=tail['var'], cvar=tail['cvar'], expected_tail_paths=tail['expected_tail_paths'],
                var_lower=tail['var_interval']['lower'], var_upper=tail['var_interval']['upper'],
                cvar_lower=tail['cvar_interval']['lower'], cvar_upper=tail['cvar_interval']['upper'],
                status=tail['statistical_status']))
    pd.DataFrame(flattened).to_csv(output/'statistics.csv', index=False)
    return result
