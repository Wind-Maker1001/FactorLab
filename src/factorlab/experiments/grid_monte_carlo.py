"""Bounded price scenarios executed by the existing Grid Paper authority."""
from dataclasses import asdict
import json
from pathlib import Path
import os
import shutil
import tempfile
from time import perf_counter

import numpy as np
import pandas as pd

from factorlab.compute import ComputeConfig
from factorlab.experiments.grid_empirical import load_hourly
from factorlab.experiments.grid_fifo import attribution
from factorlab.experiments.grid_lifecycle import lifecycle_metrics
from factorlab.experiments.grid_paper import D, _write, run_experiment
from factorlab.experiments.tracker import ExperimentTracker
from factorlab.research.monte_carlo import MonteCarloConfig, MonteCarloInput, MonteCarloRunner, _loss_tail
from factorlab.strategies.grid import GridConfig
from factorlab.strategies.grid_execution import GridRules
from factorlab.trading.contracts import Market

SCENARIO = 'CLOSE_ONLY_SCENARIO'
QUANTILES = (.01, .05, .5, .95, .99)


def _peak_rss_bytes():
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        class Counters(ctypes.Structure):
            _fields_ = [('cb', wintypes.DWORD), ('PageFaultCount', wintypes.DWORD)] + [
                (name, ctypes.c_size_t) for name in ('PeakWorkingSetSize', 'WorkingSetSize',
                'QuotaPeakPagedPoolUsage', 'QuotaPagedPoolUsage', 'QuotaPeakNonPagedPoolUsage',
                'QuotaNonPagedPoolUsage', 'PagefileUsage', 'PeakPagefileUsage')]
        value = Counters()
        value.cb = ctypes.sizeof(value)
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        api = ctypes.WinDLL('psapi', use_last_error=True).GetProcessMemoryInfo
        api.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
        if not api(kernel.GetCurrentProcess(), ctypes.byref(value), value.cb):
            raise ctypes.WinError(ctypes.get_last_error())
        return value.PeakWorkingSetSize
    import resource
    import sys
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * (1 if sys.platform == 'darwin' else 1024)


def historical_input(snapshot, as_of, initial_price):
    """Use snapshot acquisition as conservative availability for every return."""
    bars, identity = load_hourly(snapshot)
    cutoff = pd.Timestamp(as_of)
    acquired = pd.Timestamp(identity['acquired_at'])
    if cutoff.tzinfo is None or acquired > cutoff:
        raise ValueError('as_of must be timezone-aware and no earlier than snapshot acquisition')
    manifest = json.loads((Path(snapshot) / 'manifest.json').read_text())
    times = pd.to_datetime([b['time'] for b in bars], utc=True) + pd.Timedelta(hours=1)
    prices = np.asarray([b['close'] for b in bars], dtype=float)
    if times[-1] > cutoff:
        raise ValueError('historical observations exceed as_of')
    returns = prices[1:] / prices[:-1] - 1
    logs = np.log1p(returns)
    volatility = float(logs.std(ddof=1))
    drift = float(logs.mean() + .5 * volatility ** 2)
    available = np.maximum(times[1:].tz_localize(None).to_numpy(), acquired.tz_convert('UTC').tz_localize(None).to_datetime64())
    inputs = MonteCarloInput(weights=[1.], drift=[drift], volatility=[volatility],
        historical_returns=returns[:, None], timestamps=times[1:].tz_localize(None).to_numpy(),
        available_at=available, as_of=cutoff.tz_convert('UTC').tz_localize(None).to_datetime64(),
        assets=(identity['instrument_id'],), initial_prices=[initial_price])
    receipt = dict(data_version=identity['data_version'], bars_sha256=identity['bars_sha256'],
        source=manifest['source'], acquired_at=identity['acquired_at'], as_of=cutoff.isoformat(),
        observations=[times[1].isoformat(), times[-1].isoformat()], returns=len(returns),
        time_unit='hour', dt=1, gbm_estimator='sample log-return mean + half sample variance; ddof=1 volatility',
        drift=drift, volatility=volatility, initial_price=initial_price,
        price_anchor='frozen Grid initial price; historical returns, not historical price levels',
        availability='max(observation close, snapshot acquisition); not retrospective research availability')
    return inputs, receipt


def close_bars(prices, start):
    dates = pd.date_range(start, periods=len(prices), freq='h')
    if dates.tz is None:
        raise ValueError('scenario start requires UTC timezone')
    return [dict(id=f'step-{i}', time=t.tz_convert('UTC').isoformat(), price=float(p),
                 close=float(p), completed_bar=True) for i, (t, p) in enumerate(zip(dates, prices))]


def evaluate_path(prices, *, start, config, rules, capacity, output):
    """Persist one auditable temporary account; aggregate before disposal."""
    output = Path(output)
    run_experiment(output, bars=close_bars(prices, start), config=config, rules=rules,
        capital=1000, fee_rate=D('.0004'), slippage=D('.0006'), interval='1h',
        market=Market.BINANCE_USDM, instrument_id='BINANCE:USDM:NVDAUSDT',
        scenario=SCENARIO, synthetic_capacity=capacity)
    result = json.loads((output / 'result.json').read_text())
    nav = pd.read_csv(output / 'daily.csv')
    fifo = attribution(output)['summary']
    life = lifecycle_metrics(output)
    return dict(terminal_nav=float(fifo['final_equity']), maximum_drawdown=float(nav.drawdown.max()),
        terminal_inventory=float(fifo['residual_inventory']), max_inventory=float(nav.inventory.max()),
        inventory_mean_age_hours=life['inventory_mean_age_hours'],
        inventory_max_age_hours=life['inventory_max_age_hours'], position_hours=life['position_hours'],
        residual_inventory_pnl=float(fifo['residual']['net']),
        residual_inventory_loss=max(0., -float(fifo['residual']['net'])),
        completed_cycle_net=float(fifo['completed_cycle']['net']), grid_cycles=fifo['grid_cycles'],
        normal_sell_net=float(fifo['normal_sell']['net']), risk_exit_net=float(fifo['risk_exit']['net']),
        fees=float(fifo['actual_fees']), slippage=float(fifo['actual_slippage']),
        reconciliation_residual=float(fifo['reconciliation_residual']),
        exit_triggers=life['exit_triggers'], exit_orders_unfilled=life['exit_orders_unfilled'],
        exit_partial_or_unfilled=sum(o['purpose'] == 'RISK_EXIT' and o['status'] not in {'FILLED', 'CANCELED', 'REJECTED'}
            for o in result['orders'].values()), final_state=life['final_state'], fills=result['fills'])


def distribution(rows, confidence=.95):
    terminals = np.asarray([r['terminal_nav'] for r in rows])
    var, cvar = _loss_tail(1 - terminals / 1000, confidence)
    numeric = ('terminal_nav', 'maximum_drawdown', 'terminal_inventory', 'max_inventory',
        'inventory_mean_age_hours', 'residual_inventory_loss', 'completed_cycle_net', 'fees', 'slippage')
    return dict(paths=len(rows), var=var, cvar=cvar, confidence=confidence,
        loss_probability=float(np.mean(terminals < 1000)),
        quantiles={key: {str(q): float(np.quantile([r[key] for r in rows], q)) for q in QUANTILES}
                   for key in numeric})


def stress_paths(initial, steps):
    """Deterministic price stress inputs, excluded from stochastic probabilities."""
    x = np.linspace(0, 1, steps + 1)
    return {
        'continuous_decline': initial * (1 - .4*x),
        'slow_decline': initial * (1 - .15*x),
        'low_volatility': initial * (1 + .001*np.sin(12*np.pi*x)),
        'range_then_gap': initial * np.where(x < .75, 1 + .02*np.sin(16*np.pi*x), .7),
        'insufficient_rebound': initial * np.interp(x, [0, .6, 1], [1, .65, .78]),
        'fast_reversal': initial * np.interp(x, [0, .45, .55, 1], [1, .85, 1.12, 1]),
    }


def run_study(output, *, inputs, receipt, config, rules, start, n_paths=16, n_steps=168,
              batch_size=8, seed=710, compute=ComputeConfig(), code=None, command=()):
    if n_paths <= 0 or not 1 <= batch_size <= 256 or n_steps < 2:
        raise ValueError('positive path count, batch size in [1, 256] and at least two steps required')
    if pd.Timestamp(start) <= pd.Timestamp(receipt['as_of']):
        raise ValueError('scenario start must follow input as_of')
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    started = perf_counter()
    rows, generation, performance = [], [], dict(generation_seconds=0., grid_seconds=0.)
    tail = {}
    capacities = ('0.10', '0.01')
    with tempfile.TemporaryDirectory(prefix='factorlab-r10-') as temporary:
        temporary = Path(temporary)
        def execute(model, path_id, prices):
            for capacity in capacities:
                t = perf_counter()
                directory = temporary / 'account'
                metrics = evaluate_path(prices, start=start, config=config, rules=rules,
                    capacity=capacity, output=directory)
                performance['grid_seconds'] += perf_counter() - t
                row = dict(model=model, path_id=path_id, capacity=capacity, arm='grid', **metrics)
                rows.append(row)
                key = f'{model}-{capacity}'
                if key not in tail or metrics['terminal_nav'] < tail[key]['terminal_nav']:
                    target = output / 'tails' / key
                    if target.exists():
                        shutil.rmtree(target)
                    target.mkdir(parents=True)
                    for name in ('daily.csv', 'trades.csv', 'result.json'):
                        shutil.copyfile(directory / name, target / name)
                    _write(target / 'prices.json', dict(start=start, prices=prices.tolist(), path_id=path_id))
                    tail[key] = dict(path_id=path_id, **metrics)
                shutil.rmtree(directory)
            # Endowed unlevered fixed exposure, zero turnover/cost/funding, not a trade.
            units = 1000 / prices[0]
            for arm, navs in (('cash', np.full(len(prices), 1000.)), ('fixed_holding', units*prices)):
                rows.append(dict(model=model, path_id=path_id, capacity='not_applicable', arm=arm,
                    terminal_nav=float(navs[-1]), maximum_drawdown=float(np.max(1-navs/np.maximum.accumulate(navs)))))
        for model in ('gbm', 'moving_block_bootstrap'):
            for batch_id, begin in enumerate(range(0, n_paths, batch_size)):
                cfg = MonteCarloConfig(model=model, n_paths=min(batch_size, n_paths-begin),
                    n_steps=n_steps, seed=seed+batch_id, dt=1., batch_size=batch_size,
                    block_size=24, save_full_paths=True, compute=compute)
                result = MonteCarloRunner(cfg).run(inputs)
                generation.append(dict(model=model, begin=begin, metadata=result.metadata))
                performance['generation_seconds'] += result.metadata['elapsed_seconds']
                for i, prices in enumerate(result.asset_paths[:, :, 0]):
                    execute(model, begin+i, prices)
                del result
        for name, prices in stress_paths(config.initial_price, n_steps).items():
            execute('deterministic', name, prices)
    summary = {}
    for model in ('gbm', 'moving_block_bootstrap'):
        for capacity in capacities:
            sample = [r for r in rows if r['model'] == model and r['capacity'] == capacity]
            summary[f'{model}-{capacity}'] = distribution(sample)
        for arm in ('cash', 'fixed_holding'):
            sample = [r for r in rows if r['model'] == model and r['arm'] == arm]
            terminals = np.asarray([r['terminal_nav'] for r in sample])
            var, cvar = _loss_tail(1-terminals/1000, .95)
            summary[f'{model}-{arm}'] = dict(var=var, cvar=cvar,
                terminal_quantiles={str(q): float(np.quantile(terminals, q)) for q in QUANTILES},
                maximum_drawdown_quantiles={str(q): float(np.quantile([r['maximum_drawdown'] for r in sample], q)) for q in QUANTILES})
    performance['total_seconds'] = perf_counter()-started
    performance['process_peak_rss_bytes'] = _peak_rss_bytes()
    performance['full_asset_path_batch_bytes'] = min(batch_size, n_paths)*(n_steps+1)*8
    performance['generation_timing_scope'] = 'Runner.run including generation, host transfer and fixed-weight reference reduction'
    performance['grid_timing_scope'] = 'sequential CPU Paper accounts, SQLite, exports, FIFO and lifecycle evaluation'
    definition = dict(model='grid_monte_carlo', parameters=dict(grid=asdict(config),
        rules={k: str(v) for k, v in asdict(rules).items()}, paths=n_paths, steps=n_steps,
        batch_size=batch_size, seed=seed, capacities=capacities, scenario=SCENARIO),
        factors=[], dataset=receipt, label={}, windows=dict(train=receipt.get('observations'), validation=None, test=None),
        walk_forward={}, costs=dict(fee_rate='.0004', slippage='.0006', funding='NOT_SIMULATED'))
    _write(output / 'definition.json', definition)
    _write(output / 'generation.json', generation)
    _write(output / 'summary.json', dict(distributions=summary, tails=tail, performance=performance,
        evidence=['SIMULATED_PRICE_PATHS', 'SYNTHETIC_PAPER_EXECUTION', 'NO LIVE MONEY'],
        exchange_execution_evidence=False, funding='NOT_SIMULATED',
        risk_definition=dict(loss='1-terminal_nav/1000; negative means gain',
            var='linear quantile', cvar='fractional worst (1-confidence)*n losses',
            drawdown='positive fraction below running peak including initial equity, hourly'),
        deterministic_stresses_in_probability_distribution=False))
    pd.DataFrame(rows).to_csv(output / 'paths.csv', index=False)
    tracker = ExperimentTracker(output / 'tracker')
    tracker.create_experiment('p7-r10', definition)
    tracker.append_run('p7-r10', 'study', code=code or dict(commit=None, tree=None),
        command=list(command), status='SUCCEEDED', qualification='SYNTHETIC',
        environment=dict(generation=generation), validation=dict(scenario=SCENARIO),
        artifacts=dict(summary=dict(path='summary.json'), paths=dict(path='paths.csv')))
    return summary
