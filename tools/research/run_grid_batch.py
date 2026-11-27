"""Frozen GBM Grid risk study, with separately labelled full Paper audit sampling."""
import argparse
from decimal import Decimal
import json
from pathlib import Path
import platform
from time import perf_counter

import numpy as np

from factorlab.research.grid_simulation import BatchGridConfig, BatchGridResult, COLUMNS, freeze_prices, simulate
from factorlab.strategies.grid import GridConfig
from factorlab.strategies.grid_execution import GridRules


def configuration(path):
    definition = json.loads(path.read_text())
    config = BatchGridConfig(GridConfig(**definition['grid']),
        GridRules(**{k: Decimal(v) for k, v in definition['rules'].items()}),
        fee_rate=Decimal(definition['costs']['fee_rate']),
        slippage=Decimal(definition['costs']['slippage']), capacity=Decimal(definition['capacity']))
    return definition, config


def batches(definition, size, batch_size, seed):
    rng = np.random.default_rng(seed)
    data = definition['dataset']
    steps = definition['experiment']['steps']
    for begin in range(0, size, batch_size):
        count = min(batch_size, size - begin)
        shocks = rng.normal(data['drift'] - .5*data['volatility']**2,
                            data['volatility'], (count, steps))
        prices = data['initial_price'] * np.exp(np.column_stack((np.zeros(count), shocks.cumsum(axis=1))))
        yield begin, freeze_prices(prices)


def audit(output, definition, config, size, batch_size, seed, terminal, sample_size):
    from factorlab.experiments.grid_paper import run_experiment
    from factorlab.experiments.grid_monte_carlo import close_bars
    from factorlab.experiments.grid_fifo import attribution
    from factorlab.trading.contracts import Market
    rng = np.random.default_rng(seed + 1000003)
    random_ids = rng.choice(size, min(sample_size, size), replace=False).tolist()
    order = np.argsort(terminal[:, COLUMNS.index('equity')], kind='stable')
    representative = [int(order[round(q * (size-1))]) for q in (.25, .5, .75)]
    extreme = [int(order[0]), int(order[-1])]
    selected = set(random_ids + representative + extreme)
    records = []
    for begin, prices in batches(definition, size, batch_size, seed):
        ids = sorted(i for i in selected if begin <= i < begin+len(prices))
        if not ids:
            continue
        selected_prices = prices[np.asarray(ids)-begin]
        gpu = simulate(selected_prices, config, backend='cuda', trace=True, batch_size=1)
        cpu = simulate(selected_prices, config, backend='paper', trace=True)
        np.testing.assert_allclose(gpu.trace, cpu.trace, rtol=1e-12, atol=5e-10)
        for k, i in enumerate(ids):
            np.testing.assert_allclose(gpu.fills[k], cpu.fills[k], rtol=1e-12, atol=5e-10)
            directory = output / f'path-{i}'
            run_experiment(directory, bars=close_bars(selected_prices[k], definition['start']),
                config=config.grid, rules=config.rules, capital=config.capital,
                fee_rate=config.fee_rate, slippage=config.slippage, interval='1h',
                market=Market.BINANCE_USDM, instrument_id='BINANCE:USDM:NVDAUSDT',
                scenario='CLOSE_ONLY_SCENARIO', synthetic_capacity=config.capacity,
                research_wal=True)
            fifo = attribution(directory)['summary']
            np.testing.assert_allclose(float(fifo['final_equity']),
                                       gpu.terminal[k, COLUMNS.index('equity')],
                                       rtol=1e-12, atol=5e-10)
            np.save(directory / 'prices.npy', selected_prices[k])
            records.append(dict(path_id=i, independent_random=i in random_ids,
                representative=i in representative, extreme=i in extreme,
                paper_terminal_nav=float(fifo['final_equity']),
                gpu_terminal_nav=float(gpu.terminal[k, COLUMNS.index('equity')]),
                maximum_absolute_step_error=float(np.max(np.abs(gpu.trace[k]-cpu.trace[k])))))
    return records


def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument('--definition', type=Path, default=Path('configs/mc05_grid_tail.json'))
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--sizes', type=int, nargs='+', default=[100, 1000, 10000, 100000])
    parser.add_argument('--batch-size', type=int, default=4096)
    parser.add_argument('--seed', type=int, default=1729)
    parser.add_argument('--cpu-max-paths', type=int, default=1000000)
    parser.add_argument('--paper-max-paths', type=int, default=100)
    parser.add_argument('--audit-samples', type=int, default=3)
    parser.add_argument('--cu-per-hour', type=float)
    args = parser.parse_args()
    if (min(args.sizes) < 1 or args.cpu_max_paths < 0 or args.paper_max_paths < 0
            or args.audit_samples < 0 or args.batch_size < 1):
        parser.error('positive sizes/batch size and nonnegative sample counts required')
    definition, config = configuration(args.definition)
    args.output.mkdir(parents=True, exist_ok=False)
    report = dict(schema_version=1, evidence='RESEARCH_SIMULATION', model='gbm',
        quote_preparation='round to six decimals before freezing; same quotes for Paper and CUDA',
        seed=args.seed, definition=definition, python=platform.python_version(),
        numpy=np.__version__, sizes=[], audit_policy='independent random + representative + extremes; never used for population risk')
    for size in args.sizes:
        terminal = np.empty((size, len(COLUMNS)))
        kernel_seconds = generation_seconds = gpu_wall = setup_seconds = compile_seconds = 0.
        upload_seconds = download_seconds = 0.
        peak = 0
        started = perf_counter()
        generator = batches(definition, size, args.batch_size, args.seed)
        while True:
            before = perf_counter()
            try:
                begin, prices = next(generator)
            except StopIteration:
                break
            generation_seconds += perf_counter()-before
            before = perf_counter()
            result = simulate(prices, config, backend='cuda', batch_size=args.batch_size)
            gpu_wall += perf_counter()-before
            terminal[begin:begin+len(prices)] = result.terminal
            kernel_seconds += result.metadata['kernel_seconds']
            setup_seconds += result.metadata['setup_seconds']
            compile_seconds += result.metadata['compile_or_cache_seconds']
            upload_seconds += result.metadata['upload_and_allocation_seconds']
            download_seconds += result.metadata['output_transfer_seconds']
            peak = max(peak, result.metadata['device_allocation_peak_bytes'])
        elapsed = perf_counter()-started
        summary = BatchGridResult(terminal, None, {})
        row = dict(paths=size, wall_seconds=elapsed, generation_seconds=generation_seconds,
            cuda_call_seconds=gpu_wall, kernel_seconds=kernel_seconds, paths_per_second=size/elapsed,
            setup_seconds=setup_seconds, compile_or_cache_seconds=compile_seconds,
            upload_and_allocation_seconds=upload_seconds, output_transfer_seconds=download_seconds,
            device_allocation_peak_bytes=peak, risk=summary.risk(config.capital),
            device=result.metadata['device'], cupy=result.metadata['cupy'],
            cu_cost=elapsed/3600*args.cu_per_hour if args.cu_per_hour is not None else None)
        count = min(size, args.cpu_max_paths)
        if count:
            cpu_wall = cpu_kernel = cpu_setup = 0.
            for begin, cpu_prices in batches(definition, count, args.batch_size, args.seed):
                before = perf_counter()
                cpu = simulate(cpu_prices, config, backend='cpu')
                cpu_wall += perf_counter()-before
                cpu_kernel += cpu.metadata['kernel_seconds']
                cpu_setup += cpu.metadata['setup_seconds']
                np.testing.assert_allclose(cpu.terminal, terminal[begin:begin+len(cpu_prices)],
                                           rtol=1e-12, atol=5e-10)
            row['cpu_batch'] = dict(paths=count, call_seconds=cpu_wall, kernel_seconds=cpu_kernel,
                setup_seconds=cpu_setup, paths_per_second=count/cpu_wall, economics_match=True,
                implementation='single-threaded native CPU, same state-transition source as CUDA')
        count = min(size, args.paper_max_paths)
        if count:
            cpu_prices = next(batches(definition, count, count, args.seed))[1]
            paper = simulate(cpu_prices, config, backend='paper')
            np.testing.assert_allclose(paper.terminal, terminal[:count], rtol=1e-12, atol=5e-10)
            row['paper_reference'] = dict(paths=count, seconds=paper.metadata['seconds'],
                paths_per_second=count/paper.metadata['seconds'], economics_match=True,
                implementation='existing in-memory authoritative Paper')
        from factorlab.experiments.grid_monte_carlo import _peak_rss_bytes
        row['process_peak_rss_bytes'] = _peak_rss_bytes()
        row['cu_cost_status'] = 'SUPPLIED_PLATFORM_RATE' if args.cu_per_hour is not None else 'LOCAL_DEVICE_NO_PLATFORM_CU_RATE'
        np.save(args.output / f'terminal-{size}.npy', terminal)
        report['sizes'].append(row)
        (args.output / 'report.json').write_text(json.dumps(report, indent=2)+'\n')
        print(json.dumps(row), flush=True)
    if args.audit_samples:
        report['audits'] = audit(args.output / 'paper-audits', definition, config, size,
                                args.batch_size, args.seed, terminal, args.audit_samples)
        (args.output / 'report.json').write_text(json.dumps(report, indent=2)+'\n')


if __name__ == '__main__':
    main()
