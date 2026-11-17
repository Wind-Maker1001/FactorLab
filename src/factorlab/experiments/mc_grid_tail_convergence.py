"""Participation-conditioned tail uncertainty for complete Grid Paper accounts."""
from collections import deque
from concurrent.futures import ProcessPoolExecutor
from contextlib import ExitStack, closing, contextmanager, nullcontext
from functools import partial
from hashlib import sha256
import json
from multiprocessing import get_context
from pathlib import Path
import shutil
import tempfile
from time import perf_counter, process_time

import numpy as np
import pandas as pd

from factorlab._identity import source_context_root
from factorlab.experiments.grid_monte_carlo import evaluate_path, stress_paths, _peak_rss_bytes
from factorlab.experiments.mc_regime_jump import scenario_models
from factorlab.experiments.provenance import capture_provenance
from factorlab.research.monte_carlo import MonteCarloConfig, MonteCarloRunner
from factorlab.research.monte_carlo.reliability import summarize_paths, wilson_interval
from factorlab.research.regime_jump import PathPlan

MODELS = ('gbm', 'moving_block_bootstrap', 'regime_historical', 'jump_down')
GROUPS = ('unconditional', 'traded', 'ever_inventory', 'terminal_inventory')


def write(path, value, *, atomic=False):
    path = Path(path)
    target = path.with_suffix(path.suffix + '.tmp') if atomic else path
    target.write_text(json.dumps(value, indent=2, allow_nan=False, default=str) + '\n', encoding='utf-8')
    if atomic:
        target.replace(path)


def _lag_one_autocorrelation(values):
    if values.size < 3:
        return None
    previous, current = values[:-1], values[1:]
    if np.all(previous == previous[0]) or np.all(current == current[0]):
        return None
    correlation = float(np.corrcoef(previous, current)[0, 1])
    return correlation if np.isfinite(correlation) else None


def account_metrics(prices, *, start, config, rules, capacity, output, provenance=None,
                    accounting_evidence=False, diagnostic=None, latent=None):
    metrics = evaluate_path(prices, start=start, config=config, rules=rules,
                            capacity=capacity, output=output, research_wal=True, provenance=provenance,
                            accounting_evidence=accounting_evidence)
    nav = pd.read_csv(Path(output) / 'daily.csv')
    inventory = nav.inventory.abs()
    exposure = nav.net_exposure.abs()
    result = json.loads((Path(output) / 'result.json').read_text())
    pending_exit = any(o['purpose'] == 'RISK_EXIT' and
        o['status'] not in ('FILLED', 'REJECTED', 'CANCELED') for o in result['orders'].values())
    row = metrics | dict(
        initial_capital=1000, minimum_nav=float(nav.equity.min()),
        terminal_mark_unrealized_pnl=float(nav.unrealized.iloc[-1]),
        terminal_realized_gross_pnl=float(nav.realized_gross.iloc[-1]),
        fifo_realized_pnl=metrics['normal_sell_net'] + metrics['risk_exit_net'],
        fifo_unrealized_pnl=metrics['residual_inventory_pnl'],
        temporary_account_bytes=sum(p.stat().st_size for p in Path(output).iterdir() if p.is_file()),
        participation_class=('zero_fills' if not metrics['fills'] else
            'traded_terminal_inventory' if metrics['terminal_inventory'] else 'traded_flat'),
        traded=bool(metrics['fills']), ever_inventory=bool(inventory.ne(0).any()),
        has_terminal_inventory=bool(metrics['terminal_inventory']),
        inventory_cap=bool((inventory >= float(config.max_inventory * rules.quantity_unit)).any()),
        risk_exit=bool(metrics['exit_triggers']),
        incomplete_exit=bool(pending_exit or (metrics['final_state'] == 'EXITING' and metrics['terminal_inventory'])),
        loss=bool(metrics['terminal_nav'] < 1000), zero_fills=not bool(metrics['fills']),
        capital_utilization_mean=float(exposure.mean()/float(config.initial_cash)),
        capital_utilization_peak=float(exposure.max()/float(config.initial_cash)),
        inventory_mean=float(inventory.mean()), holding_hours=int(inventory.ne(0).sum()),
        exposure_mean=float(exposure.mean()), exposure_peak=float(exposure.max()),
        exposure_initial_capital_mean=float(exposure.mean()/1000),
        exposure_initial_capital_peak=float(exposure.max()/1000),
        exposure_nav_mean=float((exposure/nav.equity).mean()),
        exposure_nav_peak=float((exposure/nav.equity).max()))
    if diagnostic is not None:
        diagnostic(row, nav, result, latent, prices)
    return row


def _replay_account(prices, latent, *, start, config, rules, capacity, temporary,
                    include_path_evidence, provenance, diagnostic=None):
    wall, cpu = perf_counter(), process_time()
    with tempfile.TemporaryDirectory(dir=temporary, prefix='account-') as directory:
        account = Path(directory)/'paper'
        row = account_metrics(prices, start=start, config=config, rules=rules,
                              capacity=capacity, output=account, provenance=provenance,
                              accounting_evidence=diagnostic is not None, diagnostic=diagnostic, latent=latent)
        if diagnostic is not None:
            row['price_sha256'] = price_digest(prices)
            for key in ('reconciliation_residual', 'nav_residual', 'inventory_residual'):
                if row[key] != 0:
                    raise ValueError(f'Paper account {key}: {row[key]}')
        if include_path_evidence:
            returns = np.diff(prices)/prices[:-1]
            row.update(price_minimum=float(np.min(prices)),
                worst_hour_return=float(returns.min()),
                worst_24h_return=float(np.expm1(np.lib.stride_tricks.sliding_window_view(
                    np.log1p(returns), 24).sum(axis=1).min())) if len(returns) >= 24 else None,
                return_autocorrelation_1=_lag_one_autocorrelation(returns),
                squared_return_autocorrelation_1=_lag_one_autocorrelation(returns**2))
        if latent is not None:
            nav = pd.read_csv(account/'daily.csv')
            for event in latent:
                if 'step' in event:
                    step = event['step']
                    event.update(price_before=float(prices[step-1]), price_after=float(prices[step]),
                        terminal_price=float(prices[-1]),
                        subsequent_peak_price=float(np.max(prices[step:])),
                        nav_before=float(nav.equity.iloc[step-1]), nav_after=float(nav.equity.iloc[step]),
                        subsequent_minimum_nav=float(nav.equity.iloc[step:].min()))
                if 'state' in event:
                    section = nav.iloc[event['begin']+1:event['begin']+event['size']+1]
                    event.update(inventory_peak=float(section.inventory.max()),
                        exposure_mean=float(section.net_exposure.abs().mean()),
                        nav_change=float(section.equity.iloc[-1]-nav.equity.iloc[event['begin']]),
                        maximum_drawdown=float(section.drawdown.max()),
                        traded_hours=int(section.turnover.ne(0).sum()),
                        inventory_hours=int(section.inventory.ne(0).sum()),
                        fees=float(section.fee.sum()), slippage=float(section.slippage.sum()))
        before_cleanup = perf_counter()
    return row, latent, before_cleanup-wall, perf_counter()-before_cleanup, process_time()-cpu


def price_digest(prices):
    """Identify ordered IEEE-754 close prices, including the initial anchor."""
    return sha256(np.asarray(prices, dtype='<f8').tobytes()).hexdigest()


def _account_results(executor, jobs, replay, max_workers):
    if executor is None:
        for identity, prices, latent in jobs:
            yield identity, prices, replay(prices, latent)
        return

    # Consume in submission order so equal-NAV ties and seed prefixes stay frozen.
    pending = deque()
    try:
        for identity, prices, latent in jobs:
            future = executor.submit(replay, prices, latent)
            pending.append((identity, prices, future))
            if len(pending) == max_workers:
                identity, prices, future = pending.popleft()
                yield identity, prices, future.result()
        while pending:
            identity, prices, future = pending.popleft()
            yield identity, prices, future.result()
    finally:
        for _, _, future in pending:
            future.cancel()


@contextmanager
def _record_interruption(output, model, size, completed_by_model):
    try:
        yield
    except BaseException as error:
        write(output/'failure.json', dict(status='ACCOUNT_REPLAY_ABORTED', model=model,
            size=size, exception_type=type(error).__name__, message=str(error),
            completed_paths_per_seed_by_model=completed_by_model))
        raise


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
                                'inventory_cap', 'risk_exit', 'incomplete_exit', 'loss', 'zero_fills')},
            participation_counts={key: sum(r['participation_class'] == key for r in sample)
                for key in ('zero_fills', 'traded_flat', 'traded_terminal_inventory')},
            distributions={key: {str(q): float(np.quantile([r[key] for r in sample], q))
                for q in (0, .5, .95, .99, 1)} for key in (
                    'terminal_inventory', 'max_inventory', 'inventory_mean',
                    'residual_inventory_loss', 'exit_triggers', 'exit_orders_unfilled',
                    'fills', 'position_hours', 'holding_hours', 'exposure_mean', 'exposure_peak',
                    'exposure_initial_capital_mean', 'exposure_initial_capital_peak',
                    'exposure_nav_mean', 'exposure_nav_peak', 'maximum_drawdown',
                    'fees', 'slippage', 'capital_utilization_mean', 'capital_utilization_peak',
                    'reconciliation_residual')})
    return output


def batches(model, inputs, generator, *, seed, begin, end, batch_size, steps, block_size=24):
    if model in ('gbm', 'moving_block_bootstrap'):
        for offset in range(begin, end, batch_size):
            cfg = MonteCarloConfig(model=model, n_paths=min(batch_size, end-offset),
                n_steps=steps, seed=seed+offset//batch_size, dt=1., batch_size=batch_size,
                block_size=block_size, save_full_paths=True)
            result = MonteCarloRunner(cfg).run(inputs)
            yield offset, result.asset_paths[:, :, 0], result.metadata
    else:
        plan = PathPlan(seed=seed, n_paths=end, n_steps=steps, batch_size=batch_size)
        for offset, paths in generator.batches(plan):
            if offset >= begin:
                yield offset, paths[:, :, 0], generator.receipt(plan)


def seed_dispersion(cells, expected_seeds=None):
    output = []
    for model in MODELS:
        for size in sorted({c['n_paths'] for c in cells}):
            samples = [c for c in cells if c['model'] == model and c['n_paths'] == size]
            if not samples:
                continue
            complete = expected_seeds is None or sorted(c['seed'] for c in samples) == sorted(expected_seeds)
            for group in GROUPS:
                eligible = [c for c in samples if c['groups'][group]['n']]
                if not complete or len(eligible) != len(samples):
                    output.append(dict(model=model, n_paths=size, group=group,
                        status='INSUFFICIENT_DATA', seeds=[c['seed'] for c in eligible],
                        metrics={}, reason='incomplete planned seed group or empty conditional seed'))
                    continue
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


def convergence_differences(cells):
    output = []
    for model in MODELS:
        sizes = sorted({c['n_paths'] for c in cells if c['model'] == model})
        for lower, upper in zip(sizes, sizes[1:]):
            for current in (c for c in cells if c['model'] == model and c['n_paths'] == upper):
                previous = next(c for c in cells if c['model'] == model and
                    c['seed'] == current['seed'] and c['n_paths'] == lower)
                for group in GROUPS:
                    a, b = previous['groups'][group], current['groups'][group]
                    if not a['n'] or not b['n']:
                        continue
                    output.append(dict(model=model, seed=current['seed'], group=group,
                        lower=lower, upper=upper, status='CONVERGENCE_NOT_ESTABLISHED',
                        differences={confidence: {name:
                            b['statistics']['tails'][confidence][name] -
                            a['statistics']['tails'][confidence][name]
                            for name in ('var', 'cvar')} for confidence in ('0.95', '0.99')}))
    return output


def run_study(output, *, inputs, receipt, config, rules, definition, code=None, command=(), max_workers=1,
              resource_planner=None, group_observer=None, diagnostic=None):
    """Run admitted groups in frozen order. Workers execute only offline Paper accounts."""
    if type(max_workers) is not int or not 1 <= max_workers <= 8:
        raise ValueError('max_workers must be an integer in [1, 8]')
    settings = definition['experiment']
    if (not settings['seeds'] or len(set(settings['seeds'])) != len(settings['seeds'])
        or not settings['sizes'] or settings['sizes'] != sorted(set(settings['sizes']))
        or any(n <= 0 or n % settings['batch_size'] for n in settings['sizes'])
        or settings['budget_seconds'] <= 0 or settings['cost_safety_factor'] < 1):
        raise ValueError('distinct seeds, increasing batch-aligned positive sizes and positive budget required')
    if settings.get('coverage_first', False) and settings.get('admission_unit') != 'model_all_seeds':
        raise ValueError('coverage_first requires model_all_seeds admission')
    if not settings.get('run_deterministic', True) and not settings.get('coverage_first', False):
        raise ValueError('run_deterministic=False requires coverage_first and frozen model planning costs')
    if pd.Timestamp(definition['start']) <= pd.Timestamp(receipt['as_of']):
        raise ValueError('simulation start must follow calibration cutoff')
    if float(inputs.initial_prices[0]) != config.initial_price or len(inputs.assets) != 1:
        raise ValueError('single asset price anchor must match Grid')
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    started, cpu = perf_counter(), process_time()
    provenance = capture_provenance(source_context_root())
    write(output/'definition.json', definition | dict(dataset=receipt,
        code=provenance['code'] if code is None else code, command=list(command),
        account_replay=dict(max_workers=max_workers)))
    settings = definition['experiment']
    models, rejection = scenario_models(inputs, receipt, threshold_as_of=definition['threshold_as_of'])
    active = [m for m in definition['models'] if m in ('gbm', 'moving_block_bootstrap') or m in models]
    for model, frozen in definition.get('model_parameters', {}).items():
        if model in models and models[model].receipt()['parameters'] != frozen:
            raise ValueError(f'frozen model parameters differ: {model}')
    per_model = settings.get('admission_unit', 'all_models') == 'model_all_seeds'
    completed_by_model = dict.fromkeys(active, 0)
    rows, deterministic, cells, decisions, generation = [], [], [], [], []
    tails = {}
    execution_seconds = statistics_seconds = generation_seconds = cleanup_seconds = 0.
    path_evidence = []
    completed = 0
    worker_cpu_seconds = 0.
    scratch_per_account = definition.get('resource_planning', {}).get('expected_temporary_disk_bytes', 0)
    required_disk = max_workers * scratch_per_account
    with tempfile.TemporaryDirectory(prefix='factorlab-mc05-') as temporary, ExitStack() as stack:
        executor = (stack.enter_context(ProcessPoolExecutor(max_workers=max_workers, mp_context=get_context('spawn')))
                    if max_workers > 1 else None)
        replay = partial(_replay_account, start=definition['start'], config=config, rules=rules,
            capacity=definition['capacity'], temporary=temporary,
            include_path_evidence=settings.get('path_evidence', False),
            provenance=provenance, diagnostic=diagnostic)

        def check_scratch():
            if executor is not None and shutil.disk_usage(temporary).free < required_disk:
                raise OSError(f'parallel account scratch requires {required_disk} free bytes in {temporary}')

        def collect(identity, prices, result):
            nonlocal execution_seconds, cleanup_seconds, worker_cpu_seconds
            metrics, latent, elapsed, cleanup, cpu_used = result
            row = identity | metrics
            execution_seconds += elapsed
            cleanup_seconds += cleanup
            worker_cpu_seconds += cpu_used
            if latent is not None:
                path_evidence.append(identity | dict(events=latent))
            key = identity['model']
            if key not in tails or row['terminal_nav'] < tails[key]['terminal_nav']:
                tails[key] = dict(terminal_nav=row['terminal_nav'], identity=identity,
                    prices=prices.tolist())
            return row

        if settings.get('run_deterministic', True):
            jobs = ((dict(model='deterministic_'+name, seed=None, path_id=0), prices, None)
                    for name, prices in stress_paths(config.initial_price, settings['steps']).items())
            with _record_interruption(output, 'deterministic', None, completed_by_model):
                check_scratch()
                with closing(_account_results(executor, jobs, replay, max_workers)) as results:
                    deterministic.extend(collect(*result) for result in results)

        def stochastic_jobs(selected, begin, size):
            nonlocal generation_seconds
            for model in selected:
                for seed in settings['seeds']:
                    iterator = iter(batches(model, inputs, models.get(model), seed=seed,
                        begin=begin, end=size, batch_size=settings['batch_size'], steps=settings['steps'],
                        block_size=definition.get('bootstrap', {}).get('block_size', 24)))
                    while True:
                        before = perf_counter()
                        batch = next(iterator, None)
                        generation_seconds += perf_counter()-before
                        if batch is None:
                            break
                        offset, paths, metadata = batch
                        generation.append(dict(model=model, seed=seed, begin=offset, metadata=metadata))
                        for i, prices in enumerate(paths):
                            latent = None
                            if settings.get('path_evidence', False) and model in models:
                                before = perf_counter()
                                latent = models[model].path_evidence(PathPlan(seed=seed,
                                    n_paths=size, n_steps=settings['steps'], batch_size=settings['batch_size']), offset+i)
                                generation_seconds += perf_counter()-before
                            yield dict(model=model, seed=seed, path_id=offset+i), prices, latent

        schedule = ([(model, size) for size in settings['sizes'] for model in active]
                    if per_model and settings.get('coverage_first', False) else
                    [(model, size) for model in active for size in settings['sizes']]
                    if per_model else [(None, size) for size in settings['sizes']])
        rejected_models = set()
        for selected_model, size in schedule:
            if selected_model in rejected_models:
                if resource_planner is not None:
                    begin = completed_by_model[selected_model]
                    decisions.append(dict(model=selected_model, size=size, begin=begin,
                        additional_accounts=(size-begin)*len(settings['seeds']), admitted=False,
                        status='RESOURCE_NOT_ADMITTED', reasons=['EARLIER_TIER_NOT_ADMITTED']))
                    write(output/'decisions.json', decisions)
                continue
            selected = [selected_model] if per_model else active
            begin = completed_by_model[selected_model] if per_model else completed
            accounts = (size-begin)*len(selected)*len(settings['seeds'])
            elapsed = perf_counter()-started
            if resource_planner is not None:
                plan = resource_planner.admit(model=selected_model, size=size, begin=begin,
                    accounts=accounts, elapsed=elapsed, temporary=temporary, output=output, settings=settings)
            elif settings.get('coverage_first', False):
                planning = definition['resource_planning']['models'][selected_model]
                measured = planning['seconds_per_account_including_cleanup']
                projected = (accounts*measured + planning['group_generation_statistics_seconds']) * settings['cost_safety_factor']
            else:
                measured = execution_seconds/(len(rows)+len(deterministic))
                projected = (accounts*measured + generation_seconds + statistics_seconds)*settings['cost_safety_factor']
            if resource_planner is None:
                reserve = definition.get('resource_planning', {}).get('output_reserve_seconds', 0)
                plan = dict(projected_additional_seconds=projected,
                            admitted=elapsed+projected+reserve <= settings['budget_seconds'])
            admitted = plan['admitted']
            decisions.append(dict(model=selected_model, size=size, begin=begin,
                additional_accounts=accounts, elapsed_seconds=elapsed) | plan |
                dict(status='RESOURCE_ADMITTED' if admitted else 'RESOURCE_NOT_ADMITTED'))
            write(output/'decisions.json', decisions)
            if not admitted:
                if per_model:
                    rejected_models.add(selected_model)
                    continue
                break
            pending_cells = []
            observation = (group_observer(selected_model, size, temporary, output)
                           if group_observer is not None else nullcontext())
            with _record_interruption(output, selected_model, size, completed_by_model), observation:
                check_scratch()
                jobs = stochastic_jobs(selected, begin, size)
                with closing(_account_results(executor, jobs, replay, max_workers)) as results:
                    for model in selected:
                        for seed in settings['seeds']:
                            for _ in range(size-begin):
                                row = collect(*next(results))
                                # Keep a diagnostic prefix if the group is interrupted.
                                pd.DataFrame([row]).to_csv(output/'paths.csv', mode='a',
                                    header=not rows, index=False)
                                rows.append(row)
                            before = perf_counter()
                            sample = [r for r in rows if r['model']==model and r['seed']==seed]
                            pending_cells.append(dict(model=model, seed=seed, n_paths=size,
                                groups=grouped_statistics(sample, resamples=settings['resamples'], bootstrap_seed=seed)))
                            statistics_seconds += perf_counter()-before
                cells.extend(pending_cells)
                if diagnostic is not None:
                    write(output/'generation.json', generation)
                    write(output/'tails.json', tails)
                    write(output/'path-evidence.json', path_evidence)
                else:
                    pd.DataFrame(rows).to_csv(output/'paths.csv', index=False)
                write(output/'groups.json', cells, atomic=diagnostic is not None)
                for model in selected:
                    completed_by_model[model] = size
                completed = min(completed_by_model.values())
    result = dict(engineering_status='ENGINEERING_VALID',
        statistical_status=('STATISTICALLY_UNSTABLE' if any(
            tail['statistical_status'] == 'STATISTICALLY_UNSTABLE'
            for cell in cells for group in cell['groups'].values() if group['n']
            for tail in group['statistics']['tails'].values()) else
            'PRECISION_REPORTED_NOT_CERTIFIED' if cells else 'INSUFFICIENT_DATA'),
        financial_status='FINANCIAL_NOT_ESTABLISHED', execution_authorization='NO LIVE MONEY',
        completed_paths_per_seed=completed,
        historical_calibration_rejection=rejection, decisions=decisions,
        model_execution_status={m: ('CALIBRATION_REJECTED' if m not in active else
            'COMPLETED' if completed_by_model[m] == settings['sizes'][-1] else
            'RESOURCE_NOT_ADMITTED') for m in definition['models']},
        deterministic_in_probability_distribution=False, deterministic=deterministic,
        cash_benchmark=dict(initial_nav=1000, terminal_nav=1000, exposure=0),
        completed_paths_per_seed_by_model=completed_by_model,
        seed_dispersion=seed_dispersion(cells, settings['seeds']),
        convergence=convergence_differences(cells),
        performance=dict(total_seconds=perf_counter()-started, cpu_seconds=process_time()-cpu,
            max_workers=max_workers, worker_account_cpu_seconds=worker_cpu_seconds if max_workers > 1 else 0.,
            execution_seconds_scope='sum of account wall times, including overlaps',
            cpu_seconds_scope='main process; worker account CPU is reported separately',
            execution_seconds=execution_seconds, cleanup_seconds=cleanup_seconds, generation_seconds=generation_seconds,
            statistics_seconds=statistics_seconds, accounts=len(rows)+len(deterministic),
            process_lifetime_peak_rss_bytes=_peak_rss_bytes(),
            budget_overrun_seconds=max(0., perf_counter()-started-settings['budget_seconds'])),
        uncertainty='conditional simulation uncertainty; model calibration uncertainty not quantified')
    if diagnostic is None or not rows:
        pd.DataFrame(rows, columns=list(rows[0]) if rows else ['model', 'seed', 'path_id']).to_csv(output/'paths.csv', index=False)
    write(output/'groups.json', cells, atomic=diagnostic is not None)
    write(output/'generation.json', generation)
    write(output/'tails.json', tails)
    if settings.get('path_evidence', False):
        write(output/'path-evidence.json', path_evidence)
    pd.DataFrame(deterministic).to_csv(output/'deterministic.csv', index=False)
    result['performance'].update(total_seconds=perf_counter()-started,
        cpu_seconds=process_time()-cpu,
        budget_overrun_seconds=max(0., perf_counter()-started-settings['budget_seconds']))
    write(output/'summary.json', result)
    return result
