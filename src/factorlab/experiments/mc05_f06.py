"""F06 research orchestration around existing path models and Grid backends."""
from decimal import Decimal
import json
from pathlib import Path
import platform
import subprocess
from time import perf_counter

import numpy as np
import pandas as pd

from factorlab.experiments.provenance import capture_provenance
from factorlab.experiments.grid_monte_carlo import historical_input, _peak_rss_bytes
from factorlab.experiments.mc_grid_tail_convergence import MODELS, price_digest, write
from factorlab.experiments.mc_regime_jump import scenario_models
from factorlab.research.grid_simulation import (
    BatchGridConfig, COLUMNS, UnsupportedGridSimulation, freeze_prices, simulate,
)
from factorlab.research.monte_carlo import MonteCarloConfig, MonteCarloRunner
from factorlab.research.regime_jump import PathPlan
from factorlab.strategies.grid import GridConfig
from factorlab.strategies.grid_execution import GridRules

PROTOCOL = 'MC-05-F06-v1'
AUXILIARY = ('ever_inventory', 'terminal_exposure', 'efficiency_ratio',
             'price_return', 'largest_down_return')


def load_definition(path, root):
    study = json.loads(Path(path).read_text(encoding='utf-8'))
    definition = json.loads((Path(root) / study['frozen_definition']).read_text(encoding='utf-8'))
    definition.update(study)
    if definition['protocol'] != PROTOCOL:
        raise ValueError('unsupported research protocol')
    seeds, sizes = definition['seeds'], definition['sizes']
    if len(seeds) < 5 or len(seeds) > 8 or len(set(seeds)) != len(seeds):
        raise ValueError('five to eight distinct root seed labels required')
    if sizes != sorted(set(sizes)) or min(sizes) < 1:
        raise ValueError('positive increasing unique sizes required')
    if tuple(definition['models']) != MODELS or definition['horizons'] != [168, 720, 2160]:
        raise ValueError('F06 model and horizon slot definitions are frozen')
    if not 1 <= definition['generation_chunk_size'] <= 100000:
        raise ValueError('generation chunk outside existing generator domain')
    if not 1 <= definition['generator_batch_size'] <= 256 or definition['execution_batch_size'] < 1:
        raise ValueError('unsupported generator/execution batch size')
    if definition['rng'] != dict(version='F06-U32-PERMUTATION-v1', multiplier=2654435761,
            increment=1013904223, chunk_bits=20, seed_slots=8, horizon_slots=4,
            interpretation='unique effective seed identities, not a proof of disjoint RNG subsequences'):
        raise ValueError('unsupported RNG allocation version')
    if (max(sizes)-1)//definition['generation_chunk_size'] >= 2**20:
        raise ValueError('generation chunks exceed allocated RNG domain')
    return definition


def batch_configuration(definition):
    return BatchGridConfig(GridConfig(**definition['grid']),
        GridRules(**{k: Decimal(v) for k, v in definition['rules'].items()}),
        capital=Decimal(str(definition['grid']['initial_cash'])),
        fee_rate=Decimal(definition['costs']['fee_rate']),
        slippage=Decimal(definition['costs']['slippage']),
        capacity=Decimal(definition['capacity']))


def stream_seed(definition, model, seed, steps, chunk):
    """Injectively allocate effective uint32 seeds within the declared F06 domain."""
    if type(chunk) is not int or not 0 <= chunk < 2**20:
        raise ValueError('chunk outside RNG allocation')
    model_id = definition['models'].index(model)
    seed_id = definition['seeds'].index(seed)
    horizon_id = definition['horizons'].index(steps)
    identity = (((model_id * 4 + horizon_id) * 8 + seed_id) << 20) | chunk
    return (identity * 2654435761 + 1013904223) % 2**32


def generate_chunk(definition, inputs, generators, *, model, seed, steps, chunk):
    size = definition['generation_chunk_size']
    mapped = stream_seed(definition, model, seed, steps, chunk)
    if model in ('gbm', 'moving_block_bootstrap'):
        config = MonteCarloConfig(model=model, n_paths=size, n_steps=steps,
            seed=mapped, dt=1., batch_size=size,
            block_size=definition['bootstrap_block_size'], save_full_paths=True)
        result = MonteCarloRunner(config).run(inputs)
        prices, receipt = result.asset_paths[:, :, 0], result.metadata
    else:
        plan = PathPlan(n_paths=size, n_steps=steps,
            batch_size=definition['generator_batch_size'], seed=mapped)
        generator = generators[model]
        prices = np.concatenate([p[:, :, 0] for _, p in generator.batches(plan)])
        receipt = generator.receipt(plan)
    frozen = freeze_prices(prices)
    return frozen, dict(chunk_id=chunk, effective_seed=mapped,
        first_path_id=chunk*size, generated_paths=size,
        original_price_digest=price_digest(prices), frozen_price_digest=price_digest(frozen),
        generation=receipt)


def auxiliary(prices, terminal):
    logs = np.diff(np.log(prices), axis=1)
    variation = np.abs(logs).sum(axis=1)
    efficiency = np.divide(np.abs(logs.sum(axis=1)), variation,
        out=np.zeros(len(prices)), where=variation != 0)
    return dict(ever_inventory=terminal[:, COLUMNS.index('fills')] > 0,
        terminal_exposure=np.abs(terminal[:, COLUMNS.index('inventory')] * prices[:, -1]),
        efficiency_ratio=efficiency, price_return=prices[:, -1]/prices[:, 0]-1,
        largest_down_return=np.expm1(logs.min(axis=1)))


def execute(prices, config, *, backend, output, first_path_id, execution_batch_size):
    """Resolve supported-boundary refusals with Paper without changing the denominator."""
    try:
        return simulate(prices, config, backend=backend, batch_size=execution_batch_size).terminal, []
    except UnsupportedGridSimulation as error:
        failure = dict(first_path_id=first_path_id, paths=len(prices), batch_error=str(error))
        directory = Path(output) / 'unsupported'
        directory.mkdir(exist_ok=True)
        write(directory / f'batch-{first_path_id}.json', failure)
    rows, exceptions = [], []
    for local, path in enumerate(prices):
        identity = first_path_id+local
        try:
            result = simulate(path[None], config, backend=backend)
        except UnsupportedGridSimulation as error:
            np.save(directory / f'path-{identity}.npy', path)
            record = dict(path_id=identity, reason=str(error), resolved=False)
            write(directory / f'path-{identity}.json', record)
            result = simulate(path[None], config, backend='paper')
            record.update(resolved=True, authority='AUTHORITATIVE_PAPER_IN_MEMORY',
                terminal=result.terminal[0].tolist())
            write(directory / f'path-{identity}.json', record)
            exceptions.append(record)
        rows.append(result.terminal[0])
    return np.asarray(rows), exceptions


def study_cell(output, *, definition, inputs, generators, config, model, seed, steps,
               sizes, backend='cuda', audits=True):
    from factorlab.experiments.mc05_f06_statistics import summarize, nested_difference
    from factorlab.experiments.mc05_f06_audit import audit_selected
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    n = max(sizes)
    terminal = np.lib.format.open_memmap(output/'terminal.npy', mode='w+',
        dtype='float64', shape=(n, len(COLUMNS)))
    aux = {key: np.lib.format.open_memmap(output/f'{key}.npy', mode='w+',
        dtype='bool' if key == 'ever_inventory' else 'float64', shape=(n,)) for key in AUXILIARY}
    manifest = dict(protocol=PROTOCOL, model=model, seed=seed, steps=steps, paths=n,
        columns=COLUMNS, generation_chunk_size=definition['generation_chunk_size'],
        execution_batch_size=definition['execution_batch_size'],
        completed_paths=0, status='RUNNING', chunks=[], unsupported=[],
        times=dict(generation_seconds=0., execution_seconds=0., statistics_seconds=0.))
    write(output/'manifest.json', manifest)
    started = perf_counter()
    try:
        for chunk, begin in enumerate(range(0, n, definition['generation_chunk_size'])):
            before = perf_counter()
            prices, receipt = generate_chunk(definition, inputs, generators,
                model=model, seed=seed, steps=steps, chunk=chunk)
            manifest['times']['generation_seconds'] += perf_counter()-before
            prices = prices[:min(len(prices), n-begin)]
            before = perf_counter()
            values, exceptions = execute(prices, config, backend=backend, output=output,
                first_path_id=begin, execution_batch_size=definition['execution_batch_size'])
            manifest['times']['execution_seconds'] += perf_counter()-before
            terminal[begin:begin+len(prices)] = values
            metrics = auxiliary(prices, values)
            for key in AUXILIARY:
                aux[key][begin:begin+len(prices)] = metrics[key]
            manifest['unsupported'].extend(exceptions)
            manifest['chunks'].append(receipt)
            manifest['completed_paths'] = begin+len(prices)
        terminal.flush()
        for values in aux.values():
            values.flush()
        before = perf_counter()
        cells, differences = [], []
        previous = None
        for size in sizes:
            prefix = {key: values[:size] for key, values in aux.items()}
            statistics = summarize(terminal[:size], prefix, capital=float(config.capital))
            cells.append(dict(model=model, seed=seed, steps=steps, n_paths=size, **statistics))
            if previous is not None:
                differences.append(dict(model=model, seed=seed, steps=steps,
                    lower=previous, upper=size, result=nested_difference(terminal[:previous],
                        {key: values[:previous] for key, values in aux.items()}, terminal[:size], prefix,
                        capital=float(config.capital))))
            previous = size
        manifest['times']['statistics_seconds'] = perf_counter()-before
        write(output/'statistics.json', dict(cells=cells, nested_differences=differences))
        if audits:
            def regenerate(path_id):
                chunk, local = divmod(path_id, definition['generation_chunk_size'])
                return generate_chunk(definition, inputs, generators, model=model,
                    seed=seed, steps=steps, chunk=chunk)[0][local]
            before = perf_counter()
            audit = audit_selected(output/'paper-audits', definition=definition,
                config=config, model=model, seed=seed, steps=steps, terminal=terminal,
                auxiliary=aux, regenerate=regenerate, backend=backend)
            manifest['times']['audit_seconds'] = perf_counter()-before
            manifest['audit'] = audit
        manifest.update(status='COMPLETE', wall_seconds=perf_counter()-started,
            process_peak_rss_bytes=_peak_rss_bytes())
        write(output/'manifest.json', manifest, atomic=True)
        return dict(cells=cells, nested_differences=differences, manifest=manifest)
    except BaseException as error:
        manifest.update(status='FAILED', exception_type=type(error).__name__, exception=str(error),
            wall_seconds=perf_counter()-started)
        write(output/'manifest.json', manifest, atomic=True)
        raise


def run_study(output, *, definition, snapshot, root, steps=168, sizes=None,
              models=None, seeds=None, backend='cuda', audits=True):
    from factorlab.experiments.mc05_f06_statistics import seed_summary
    config = batch_configuration(definition)
    inputs, receipt = historical_input(snapshot, definition['as_of'], config.grid.initial_price)
    if receipt != definition['dataset']:
        raise ValueError('F06 requires the complete frozen MC05 calibration identity')
    origin = pd.Timestamp(definition['start'])
    if origin.tzinfo is None or origin <= pd.Timestamp(receipt['as_of']):
        raise ValueError('timezone-aware scenario start must follow calibration cutoff')
    if receipt['time_unit'] != 'hour' or len(inputs.assets) != 1:
        raise ValueError('F06 requires single-asset hourly calibration')
    generators, rejection = scenario_models(inputs, receipt,
        threshold_as_of=definition['threshold_as_of'])
    if rejection:
        raise ValueError('frozen regime calibration unavailable: '+rejection)
    sizes = definition['sizes'] if sizes is None else sizes
    models = definition['models'] if models is None else models
    seeds = definition['seeds'] if seeds is None else seeds
    if not sizes or sizes != sorted(set(sizes)) or min(sizes) < 1 or max(sizes) > max(definition['sizes']):
        raise ValueError('invalid requested nested sizes')
    if (not models or not seeds or len(set(models)) != len(models) or len(set(seeds)) != len(seeds)
            or steps not in definition['horizons'] or not set(models) <= set(definition['models'])
            or not set(seeds) <= set(definition['seeds'])):
        raise ValueError('request outside allocated experiment domains')
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    revision = subprocess.check_output(['git', '-C', str(root), 'rev-parse', 'HEAD'], text=True).strip()
    write(output/'definition.json', definition)
    report = dict(protocol=PROTOCOL, source_revision=revision, provenance=capture_provenance(root),
        python=platform.python_version(),
        numpy=np.__version__, backend=backend, snapshot=str(Path(snapshot).resolve()),
        local_raw_directory=str(output.resolve()), dataset=receipt, steps=steps,
        sizes=sizes, models=models, seeds=seeds, status='RUNNING', cells=[],
        nested_differences=[], completed_cells=[], financial_status='CONVERGENCE_NOT_ESTABLISHED',
        funding='NOT_SIMULATED')
    write(output/'report.json', report)
    started = perf_counter()
    try:
        for model in models:
            for seed in seeds:
                result = study_cell(output/f'{model}-{seed}-{steps}', definition=definition,
                    inputs=inputs, generators=generators, config=config, model=model,
                    seed=seed, steps=steps, sizes=sizes, backend=backend, audits=audits)
                report['cells'].extend(result['cells'])
                report['nested_differences'].extend(result['nested_differences'])
                report['completed_cells'].append(dict(model=model, seed=seed, paths=max(sizes),
                    times=result['manifest']['times'], unsupported=len(result['manifest']['unsupported'])))
                report['wall_seconds'] = perf_counter()-started
                write(output/'report.json', report, atomic=True)
                print(json.dumps(report['completed_cells'][-1]), flush=True)
        dispersion = [dict(model=model, steps=steps, n_paths=size,
            summary=seed_summary([cell for cell in report['cells']
                if cell['model'] == model and cell['n_paths'] == size]))
            for model in models for size in sizes]
        report.update(status='COMPLETE', wall_seconds=perf_counter()-started,
            seed_summary=dispersion, process_peak_rss_bytes=_peak_rss_bytes())
        write(output/'report.json', report, atomic=True)
    except BaseException as error:
        report.update(status='FAILED', exception_type=type(error).__name__, exception=str(error),
            wall_seconds=perf_counter()-started)
        write(output/'report.json', report, atomic=True)
        raise
    return report
