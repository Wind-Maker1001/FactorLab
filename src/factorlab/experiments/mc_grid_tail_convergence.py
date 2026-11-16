"""Participation-conditioned tail uncertainty for complete Grid Paper accounts."""
import json
from pathlib import Path
import shutil
import tempfile
from time import perf_counter, process_time

import numpy as np
import pandas as pd

from factorlab.experiments.grid_monte_carlo import evaluate_path, stress_paths, _peak_rss_bytes
from factorlab.experiments.mc_regime_jump import scenario_models
from factorlab.research.monte_carlo import MonteCarloConfig, MonteCarloRunner
from factorlab.research.monte_carlo.reliability import summarize_paths, wilson_interval
from factorlab.research.regime_jump import PathPlan

MODELS = ('gbm', 'moving_block_bootstrap', 'regime_historical', 'jump_down')
GROUPS = ('unconditional', 'traded', 'ever_inventory', 'terminal_inventory')


def write(path, value):
    Path(path).write_text(json.dumps(value, indent=2, allow_nan=False, default=str) + '\n', encoding='utf-8')


def account_metrics(prices, *, start, config, rules, capacity, output):
    metrics = evaluate_path(prices, start=start, config=config, rules=rules,
                            capacity=capacity, output=output, research_wal=True)
    nav = pd.read_csv(Path(output) / 'daily.csv')
    inventory = nav.inventory.abs()
    exposure = nav.net_exposure.abs()
    result = json.loads((Path(output) / 'result.json').read_text())
    pending_exit = any(o['purpose'] == 'RISK_EXIT' and
        o['status'] not in ('FILLED', 'REJECTED', 'CANCELED') for o in result['orders'].values())
    return metrics | dict(
        participation_class=('zero_fills' if not metrics['fills'] else
            'traded_terminal_inventory' if metrics['terminal_inventory'] else 'traded_flat'),
        traded=bool(metrics['fills']), ever_inventory=bool(inventory.ne(0).any()),
        has_terminal_inventory=bool(metrics['terminal_inventory']),
        inventory_cap=bool((inventory >= float(config.max_inventory * rules.quantity_unit)).any()),
        risk_exit=bool(metrics['exit_triggers']),
        incomplete_exit=bool(pending_exit or (metrics['final_state'] == 'EXITING' and metrics['terminal_inventory'])),
        inventory_mean=float(inventory.mean()), holding_hours=int(inventory.ne(0).sum()),
        exposure_mean=float(exposure.mean()), exposure_peak=float(exposure.max()),
        exposure_initial_capital_mean=float(exposure.mean()/1000),
        exposure_initial_capital_peak=float(exposure.max()/1000),
        exposure_nav_mean=float((exposure/nav.equity).mean()),
        exposure_nav_peak=float((exposure/nav.equity).max()))


def grouped_statistics(rows, *, resamples=999, bootstrap_seed=0):
    total = len(rows)
    output = {}
    for group in GROUPS:
        sample = [r for r in rows if group == 'unconditional' or
                  r[{'traded': 'traded', 'ever_inventory': 'ever_inventory',
                     'terminal_inventory': 'has_terminal_inventory'}[group]]]
        n = len(sample)
        result = dict(n=n, population_n=total,
            population_fraction=wilson_interval(n, total) if total else None,
            conditioning=group)
        if not n:
            output[group] = result | dict(status='INSUFFICIENT_DATA')
            continue
        statistics = summarize_paths([1-r['terminal_nav']/1000 for r in sample],
            [r['maximum_drawdown'] for r in sample], resamples=resamples,
            bootstrap_seed=bootstrap_seed)
        output[group] = result | dict(statistics=statistics,
            events={key: wilson_interval(sum(bool(r[key]) for r in sample), n)
                    for key in ('traded', 'ever_inventory', 'has_terminal_inventory',
                                'inventory_cap', 'risk_exit', 'incomplete_exit')},
            participation_counts={key: sum(r['participation_class'] == key for r in sample)
                for key in ('zero_fills', 'traded_flat', 'traded_terminal_inventory')},
            distributions={key: {str(q): float(np.quantile([r[key] for r in sample], q))
                for q in (0, .5, .95, .99, 1)} for key in (
                    'terminal_inventory', 'max_inventory', 'inventory_mean',
                    'residual_inventory_loss', 'exit_triggers', 'exit_orders_unfilled',
                    'fills', 'position_hours', 'holding_hours', 'exposure_mean', 'exposure_peak',
                    'exposure_initial_capital_mean', 'exposure_initial_capital_peak',
                    'exposure_nav_mean', 'exposure_nav_peak')})
    return output


def batches(model, inputs, generator, *, seed, begin, end, batch_size, steps):
    if model in ('gbm', 'moving_block_bootstrap'):
        for offset in range(begin, end, batch_size):
            cfg = MonteCarloConfig(model=model, n_paths=min(batch_size, end-offset),
                n_steps=steps, seed=seed+offset//batch_size, dt=1., batch_size=batch_size,
                block_size=24, save_full_paths=True)
            result = MonteCarloRunner(cfg).run(inputs)
            yield offset, result.asset_paths[:, :, 0], result.metadata
    else:
        plan = PathPlan(seed=seed, n_paths=end, n_steps=steps, batch_size=batch_size)
        for offset, paths in generator.batches(plan):
            if offset >= begin:
                yield offset, paths[:, :, 0], generator.receipt(plan)


def seed_dispersion(cells):
    output = []
    for model in MODELS:
        for size in sorted({c['n_paths'] for c in cells}):
            samples = [c for c in cells if c['model'] == model and c['n_paths'] == size]
            for group in GROUPS:
                eligible = [c for c in samples if c['groups'][group]['n']]
                metrics = {}
                for confidence in ('.95', '.99'):
                    for name in ('var', 'cvar'):
                        values = [c['groups'][group]['statistics']['tails'][str(float(confidence))][name] for c in eligible]
                        metrics[name+confidence] = dict(minimum=min(values), maximum=max(values),
                            seed_sd=float(np.std(values, ddof=1)) if len(values)>1 else None) if values else None
                output.append(dict(model=model, n_paths=size, group=group,
                    seeds=[c['seed'] for c in eligible], metrics=metrics,
                    meaning='descriptive seed dispersion; not market prediction interval'))
    return output


def run_study(output, *, inputs, receipt, config, rules, definition, code=None, command=()):
    settings = definition['experiment']
    if (not settings['seeds'] or len(set(settings['seeds'])) != len(settings['seeds'])
        or not settings['sizes'] or settings['sizes'] != sorted(set(settings['sizes']))
        or any(n <= 0 or n % settings['batch_size'] for n in settings['sizes'])
        or settings['budget_seconds'] <= 0 or settings['cost_safety_factor'] < 1):
        raise ValueError('distinct seeds, increasing batch-aligned positive sizes and positive budget required')
    if pd.Timestamp(definition['start']) <= pd.Timestamp(receipt['as_of']):
        raise ValueError('simulation start must follow calibration cutoff')
    if float(inputs.initial_prices[0]) != config.initial_price or len(inputs.assets) != 1:
        raise ValueError('single asset price anchor must match Grid')
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    started, cpu = perf_counter(), process_time()
    write(output/'definition.json', definition | dict(dataset=receipt, code=code, command=list(command)))
    settings = definition['experiment']
    models, rejection = scenario_models(inputs, receipt, threshold_as_of=definition['threshold_as_of'])
    active = [m for m in MODELS if m in ('gbm', 'moving_block_bootstrap') or m in models]
    rows, deterministic, cells, decisions, generation = [], [], [], [], []
    tails = {}
    execution_seconds = statistics_seconds = generation_seconds = 0.
    completed = 0
    with tempfile.TemporaryDirectory(prefix='factorlab-mc05-') as temporary:
        account = Path(temporary)/'account'
        def execute(prices, identity):
            nonlocal execution_seconds
            before = perf_counter()
            row = identity | account_metrics(prices, start=definition['start'], config=config,
                rules=rules, capacity=definition['capacity'], output=account)
            execution_seconds += perf_counter()-before
            key = identity['model']
            if key not in tails or row['terminal_nav'] < tails[key]['terminal_nav']:
                tails[key] = dict(terminal_nav=row['terminal_nav'], identity=identity,
                    prices=prices.tolist())
            shutil.rmtree(account)
            return row
        for name, prices in stress_paths(config.initial_price, settings['steps']).items():
            deterministic.append(execute(prices, dict(model='deterministic_'+name, seed=None, path_id=0)))
        for size in settings['sizes']:
            accounts = (size-completed)*len(active)*len(settings['seeds'])
            elapsed = perf_counter()-started
            measured = execution_seconds/(len(rows)+len(deterministic))
            projected = accounts*measured*settings['cost_safety_factor'] + statistics_seconds
            admitted = elapsed+projected <= settings['budget_seconds']
            decisions.append(dict(size=size, additional_accounts=accounts, elapsed_seconds=elapsed,
                projected_additional_seconds=projected, admitted=admitted))
            if not admitted:
                break
            for model in active:
                for seed in settings['seeds']:
                    iterator = iter(batches(model, inputs, models.get(model), seed=seed,
                        begin=completed, end=size, batch_size=settings['batch_size'], steps=settings['steps']))
                    while True:
                        before = perf_counter()
                        batch = next(iterator, None)
                        generation_seconds += perf_counter()-before
                        if batch is None:
                            break
                        offset, paths, metadata = batch
                        generation.append(dict(model=model, seed=seed, begin=offset, metadata=metadata))
                        for i, prices in enumerate(paths):
                            rows.append(execute(prices, dict(model=model, seed=seed, path_id=offset+i)))
                    before = perf_counter()
                    sample = [r for r in rows if r['model']==model and r['seed']==seed]
                    cells.append(dict(model=model, seed=seed, n_paths=size,
                        groups=grouped_statistics(sample, resamples=settings['resamples'], bootstrap_seed=seed)))
                    statistics_seconds += perf_counter()-before
            completed = size
            pd.DataFrame(rows).to_csv(output/'paths.csv', index=False)
            write(output/'groups.json', cells)
    result = dict(engineering_status='ENGINEERING_VALID',
        statistical_status=('STATISTICALLY_UNSTABLE' if any(
            tail['statistical_status'] == 'STATISTICALLY_UNSTABLE'
            for cell in cells for group in cell['groups'].values() if group['n']
            for tail in group['statistics']['tails'].values()) else
            'PRECISION_REPORTED_NOT_CERTIFIED' if cells else 'INSUFFICIENT_DATA'),
        financial_status='FINANCIAL_NOT_ESTABLISHED', completed_paths_per_seed=completed,
        historical_calibration_rejection=rejection, decisions=decisions,
        deterministic_in_probability_distribution=False, deterministic=deterministic,
        cash_benchmark=dict(initial_nav=1000, terminal_nav=1000, exposure=0),
        seed_dispersion=seed_dispersion(cells),
        performance=dict(total_seconds=perf_counter()-started, cpu_seconds=process_time()-cpu,
            execution_seconds=execution_seconds, generation_seconds=generation_seconds,
            statistics_seconds=statistics_seconds, accounts=len(rows)+len(deterministic),
            process_lifetime_peak_rss_bytes=_peak_rss_bytes(),
            budget_overrun_seconds=max(0., perf_counter()-started-settings['budget_seconds'])),
        uncertainty='conditional simulation uncertainty; model calibration uncertainty not quantified')
    pd.DataFrame(rows, columns=list(rows[0]) if rows else ['model', 'seed', 'path_id']).to_csv(output/'paths.csv', index=False)
    write(output/'groups.json', cells)
    write(output/'summary.json', result)
    write(output/'generation.json', generation)
    write(output/'tails.json', tails)
    pd.DataFrame(deterministic).to_csv(output/'deterministic.csv', index=False)
    return result
