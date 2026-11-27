"""Dedicated static-grid parameter experiments; all transitions use the existing engine."""
from dataclasses import replace
import json
from pathlib import Path
from time import perf_counter

import numpy as np

from factorlab.experiments.mc_grid_tail_convergence import batches
from factorlab.research.grid_simulation import COLUMNS, freeze_prices, simulate
from factorlab.research.monte_carlo import _loss_tail


METRICS = ('net_return', 'maximum_drawdown', 'fills', 'maximum_committed',
           'mean_committed', 'maximum_inventory', 'mean_exposure', 'maximum_exposure',
           'turnover', 'fees', 'slippage', 'terminal_inventory')
MODEL_IDS = {'gbm': 0, 'moving_block_bootstrap': 1, 'regime_historical': 2, 'jump_down': 3}


def write_json(path, value):
    from decimal import Decimal
    def encode(item):
        if isinstance(item, Decimal):
            return str(item)
        raise TypeError(f'unsupported research JSON value: {type(item).__name__}')
    Path(path).write_text(json.dumps(value, indent=2, allow_nan=False, default=encode) + '\n', encoding='utf-8')


def calibration_prefix(inputs, receipt, end):
    """Exclude validation observations before fitting any stochastic generator."""
    import pandas as pd
    cutoff = pd.Timestamp(end)
    times = pd.to_datetime(inputs.timestamps, utc=True)
    selected = times <= cutoff
    returns = np.asarray(inputs.historical_returns)[selected]
    if len(returns) < 2:
        raise ValueError('calibration prefix requires at least two returns')
    logs = np.log1p(returns)
    vol = logs.std(axis=0, ddof=1)
    drift = logs.mean(axis=0) + .5 * vol**2
    prefix = replace(inputs, drift=drift, volatility=vol, historical_returns=returns,
                     timestamps=np.asarray(inputs.timestamps)[selected],
                     available_at=np.asarray(inputs.available_at)[selected])
    identity = receipt | {'calibration_end': cutoff.isoformat(), 'returns': len(returns),
        'observations': [times[selected][0].isoformat(), times[selected][-1].isoformat()],
        'drift': float(drift[0]), 'volatility': float(vol[0]),
        'parent_snapshot_used_by_prior_research': True}
    return prefix, identity


def stream_seed(settings, stage, model, replicate):
    """Disjoint million-seed blocks include legacy per-batch increments."""
    if stage not in settings['random_domains'] or model not in MODEL_IDS:
        raise ValueError('unknown stage/model random domain')
    if not 0 <= replicate < 5:
        raise ValueError('replicate must be in [0, 5)')
    if settings[stage]['paths'] > 100000 or settings['batch_size'] > 256:
        raise ValueError('existing scenario generators support <=100000 paths and <=256 batch size')
    return (settings['random_domains'][stage] + MODEL_IDS[model] * settings['model_stride']
            + replicate * settings['replicate_stride'])


def reduce_paths(result, prices, config):
    """Reduce exact observation traces, including t=0, before releasing the batch."""
    trace = result.trace
    column = {key: trace[:, :, i] for i, key in enumerate(COLUMNS)}
    inventory = column['inventory']
    exposure = inventory * prices
    if result.fills is None:
        if config.fee_rate <= 0:
            raise ValueError('turnover reduction without fills requires positive proportional fees')
        turnover = column['total_fees'][:, -1] / float(config.fee_rate)
    else:
        turnover = np.asarray([sum(row[4] * row[5] for row in fills) for fills in result.fills])
    return np.column_stack((
        column['equity'][:, -1] / float(config.capital) - 1,
        column['maximum_drawdown'][:, -1], column['fills'][:, -1],
        column['committed'].max(axis=1), column['committed'].mean(axis=1),
        inventory.max(axis=1), exposure.mean(axis=1), exposure.max(axis=1),
        turnover, column['total_fees'][:, -1], column['total_slippage'][:, -1], inventory[:, -1]))


def passive_paths(prices, config, *, endowed=False):
    """R06 fixed-first-close LIMIT control, or exactly matched endowed holdings.

    The fixed target is one static order, admitted at t=0 and executable only
    from t=1. No risk exits or forced terminal liquidation apply to holdings.
    """
    from decimal import Decimal, ROUND_FLOOR
    r, g = config.rules, config.grid
    capital = float(config.capital)
    quantity = float(Decimal(g.initial_inventory if endowed else g.quantity) * r.quantity_unit)
    n, steps = prices.shape
    fee_rate, slip = float(config.fee_rate), float(config.slippage)
    if endowed:
        position = np.full_like(prices, quantity)
        basis = np.full_like(prices, quantity * g.initial_price)
        cash = capital - basis
        committed = basis
        fills = turnover = fees = slippage = np.zeros(n)
    else:
        limit = float((Decimal(str(g.initial_price)) * (1 + config.slippage) / r.tick_size
                       ).to_integral_value(rounding=ROUND_FLOOR) * r.tick_size)
        minimum = float(max(r.min_quantity, Decimal(g.minimum_quantity) * r.quantity_unit))
        if (quantity < minimum or quantity * limit < float(r.min_notional)
                or quantity * limit * (1 + fee_rate) > min(capital, g.max_cash_committed)):
            quantity = 0.
        execution = prices * (1 + slip)
        eligible = execution <= limit
        eligible[:, 0] = False
        target_steps = int(round(quantity / float(r.step_size)))
        capacity_steps = int(config.capacity / r.step_size)
        filled_steps = np.minimum(target_steps, np.cumsum(eligible, axis=1) * capacity_steps)
        position = filled_steps * float(r.step_size)
        increments = np.diff(filled_steps, axis=1, prepend=0) * float(r.step_size)
        notional = increments * execution
        basis = np.cumsum(notional, axis=1)
        fee = notional * fee_rate
        cash = capital - basis - np.cumsum(fee, axis=1)
        # The full order was collateralized at its upper execution limit at t=0.
        committed = basis + (quantity - position) * limit * (1 + fee_rate)
        fills = np.sum(increments > 0, axis=1)
        turnover = notional.sum(axis=1)
        fees = fee.sum(axis=1)
        slippage = np.sum(increments * prices * slip, axis=1)
    exposure = position * prices
    equity = cash + exposure
    drawdown = 1 - equity / np.maximum.accumulate(equity, axis=1)
    return np.column_stack((equity[:, -1] / capital - 1, drawdown.max(axis=1), fills,
        committed.max(axis=1), committed.mean(axis=1), position.max(axis=1),
        exposure.mean(axis=1), exposure.max(axis=1), turnover, fees, slippage, position[:, -1]))


def summarize(values):
    columns = dict(zip(METRICS, values.T))
    loss = -columns['net_return']
    output = {name: {'mean': float(v.mean()), 'quantiles': {
        str(q): float(np.quantile(v, q)) for q in (0, .5, .95, .99, 1)}}
        for name, v in columns.items()}
    output.update(n=len(values), loss_probability=float(np.mean(loss > 0)),
                  fill_probability=float(np.mean(columns['fills'] > 0)),
                  cvar95=float(_loss_tail(loss, .95)[1]),
                  cvar99=float(_loss_tail(loss, .99)[1]))
    deployed = columns['mean_committed'].mean()
    output['profit_per_mean_committed'] = (
        float(columns['net_return'].mean() * 1000 / deployed) if deployed > 0 else None)
    return output


def paired(candidate, control):
    if candidate.shape != control.shape:
        raise ValueError('paired results must have matching path identities and metrics')
    difference = candidate - control
    return {key: {'mean_difference': float(v.mean()),
                  'path_standard_error': float(v.std(ddof=1) / np.sqrt(len(v))) if len(v) > 1 else None}
            for key, v in zip(METRICS, difference.T)}


def nondominated(vectors):
    """Indices of non-dominated rows; each column is a loss to minimize."""
    vectors = np.asarray(vectors, dtype=float)
    if vectors.ndim != 2 or not np.isfinite(vectors).all():
        raise ValueError('finite two-dimensional objective matrix required')
    return [i for i, row in enumerate(vectors)
            if not np.any(np.all(vectors <= row, axis=1) & np.any(vectors < row, axis=1))]


def choose_representatives(summaries, count):
    """Pareto layers plus normalized farthest-point coverage; no business utility weights."""
    ids = sorted(summaries)
    vectors = np.asarray([[-summaries[k]['net_return']['mean'], summaries[k]['cvar95'],
        summaries[k]['cvar99'], summaries[k]['maximum_drawdown']['quantiles']['0.99'],
        summaries[k]['loss_probability'], summaries[k]['maximum_committed']['mean']]
        for k in ids])
    span = np.ptp(vectors, axis=0)
    scaled = np.divide(vectors - vectors.min(axis=0), span,
                       out=np.zeros_like(vectors), where=span > 0)
    chosen = []
    remaining = list(range(len(ids)))
    while remaining and len(chosen) < count:
        layer = [remaining[i] for i in nondominated(vectors[remaining])]
        if len(chosen) + len(layer) <= count:
            chosen.extend(layer)
        else:
            # Seed coverage with extrema on each objective, then fill maximum distance.
            anchors = list(dict.fromkeys(layer[int(np.argmin(vectors[layer, j]))]
                                         for j in range(vectors.shape[1])))
            for index in anchors:
                if len(chosen) < count:
                    chosen.append(index)
            pool = [i for i in layer if i not in chosen]
            while pool and len(chosen) < count:
                distances = np.asarray([np.min(np.sum((scaled[chosen] - scaled[i])**2, axis=1))
                                        for i in pool])
                chosen.append(pool.pop(int(np.argmax(distances))))
        remaining = [i for i in remaining if i not in layer]
    return [ids[i] for i in chosen]


def audit_population(output, *, configs, settings, inputs, generators, backend='cuda'):
    """Replay frozen D/r0 IDs; the sample never estimates population risk."""
    from factorlab.experiments.grid_parameter_validation import audit_paths
    output = Path(output)
    reports = []
    n = settings['D']['paths']
    for model in settings['models']:
        seed = stream_seed(settings, 'D', model, 0)
        chosen = {}
        for key in configs:
            metrics = np.load(output / f'D-{model}-r0' / f'{key}.npy', mmap_mode='r')
            order = np.argsort(metrics[:, 0], kind='stable')
            rng = np.random.default_rng(np.random.SeedSequence([seed, 900001]))
            random = rng.choice(n, min(settings['audit_random_paths'], n), replace=False).tolist()
            quantiles = [int(order[round(q * (n - 1))]) for q in (.25, .5, .75)]
            extremes = [int(order[0]), int(order[-1])]
            stress = [int(np.argmax(metrics[:, i])) for i in (1, 2, 3, 5)]
            chosen[key] = {'ids': sorted(set(random + quantiles + extremes + stress)),
                           'independent_random': random, 'quantiles': quantiles,
                           'extremes': extremes, 'execution_stress': stress}
        needed = set(i for selected in chosen.values() for i in selected['ids'])
        saved = {}
        for begin, raw, _ in batches(model, inputs, generators.get(model), seed=seed, begin=0, end=n,
            batch_size=settings['batch_size'], steps=settings['steps'],
            block_size=settings['bootstrap_block_size']):
            ids = sorted(i for i in needed if begin <= i < begin + len(raw))
            for i in ids:
                saved[i] = freeze_prices(raw[i - begin])
        for key, config in configs.items():
            selected = chosen[key]
            report = audit_paths(output / 'paper-audits' / model / key,
                np.asarray([saved[i] for i in selected['ids']]), config, backend=backend,
                path_ids=selected['ids'])
            reports.append({'model': model, 'config_id': key, 'seed': seed,
                            'sampling': selected, 'report': report})
    return {'policy': 'D/r0 independent random, equity quartiles/extremes and maximal drawdown/fills/commitment/inventory; not population statistics',
            'results': reports}


def run_cell(output, *, configs, model, stage, replicate, settings, inputs, generator,
             backend='cuda'):
    """A price batch is frozen once, then evaluated by every candidate and comparator."""
    directory = Path(output) / f'{stage}-{model}-r{replicate}'
    directory.mkdir(parents=True, exist_ok=False)
    n = settings[stage]['paths']
    seed = stream_seed(settings, stage, model, replicate)
    keys = ['cash', *[name for key, _ in configs for name in (key, key + '-hold', key + '-endowed')]]
    arrays = {key: np.lib.format.open_memmap(directory / f'{key}.npy', mode='w+',
              dtype='float64', shape=(n, len(METRICS))) for key in keys}
    arrays['cash'][:] = 0
    started = perf_counter()
    try:
        for begin, raw, _ in batches(model, inputs, generator, seed=seed, begin=0, end=n,
            batch_size=settings['batch_size'], steps=settings['steps'],
            block_size=settings['bootstrap_block_size']):
            prices = freeze_prices(raw)
            for key, config in configs:
                result = simulate(prices, config, backend=backend, trace=True,
                                  batch_size=settings['batch_size'], collect_fills=False)
                arrays[key][begin:begin + len(prices)] = reduce_paths(result, prices, config)
                arrays[key + '-hold'][begin:begin + len(prices)] = passive_paths(prices, config)
                arrays[key + '-endowed'][begin:begin + len(prices)] = passive_paths(prices, config, endowed=True)
    except Exception as exc:
        write_json(directory / 'status.json', {'status': 'FAILED', 'error': str(exc),
                   'model': model, 'stage': stage, 'replicate': replicate, 'seed': seed})
        raise
    for array in arrays.values():
        array.flush()
    summaries = {key: summarize(array) for key, array in arrays.items()}
    baseline = arrays['baseline']
    result = {'status': 'COMPLETE', 'model': model, 'stage': stage, 'replicate': replicate,
              'seed': seed, 'batch_size': settings['batch_size'], 'paths': n,
              'seconds': perf_counter() - started, 'metrics': list(METRICS),
              'summaries': summaries,
              'paired_vs_baseline': {key: paired(arrays[key], baseline) for key, _ in configs},
              'paired_vs_controls': {key: {control: paired(arrays[key], arrays[other])
                  for control, other in (('cash', 'cash'), ('hold', key + '-hold'),
                                         ('endowed', key + '-endowed'))} for key, _ in configs}}
    write_json(directory / 'status.json', result)
    return result
