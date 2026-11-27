"""Profile frozen Grid paths through existing authoritative Paper entry points."""
import argparse
import cProfile
from concurrent.futures import ProcessPoolExecutor
import json
from pathlib import Path
import pstats
import tempfile
from time import perf_counter

import numpy as np

from factorlab.experiments.grid_monte_carlo import close_bars, evaluate_path
from factorlab.experiments.grid_paper import D, run_experiment
from factorlab.strategies.grid import GridConfig
from factorlab.strategies.grid_execution import GridRules
from factorlab.research.grid_simulation import BatchGridConfig, COLUMNS, freeze_prices, simulate
from factorlab.trading.contracts import Market


def replay(job):
    prices, definition, mode = job
    config = GridConfig(**definition['grid'])
    rules = GridRules(**{k: D(v) for k, v in definition['rules'].items()})
    with tempfile.TemporaryDirectory(prefix='grid-profile-') as root:
        output = Path(root) / 'account'
        if mode == 'mc_files':
            metrics = evaluate_path(prices, start=definition['start'], config=config,
                rules=rules, capacity=definition['capacity'], output=output,
                provenance={'benchmark': 'frozen-grid-profile'})
            return {'terminal_nav': metrics['terminal_nav'], 'fills': metrics['fills']}
        _, artifacts = run_experiment(output, bars=close_bars(prices, definition['start']),
            config=config, rules=rules, capital=1000, fee_rate=D('.0004'),
            slippage=D('.0006'), interval='1h', market=Market.BINANCE_USDM,
            instrument_id='BINANCE:USDM:NVDAUSDT', scenario='CLOSE_ONLY_SCENARIO',
            synthetic_capacity=definition['capacity'], research_wal=True,
            provenance={'benchmark': 'frozen-grid-profile'}, return_artifacts=True,
            in_memory_store=mode == 'paper_memory')
        return {'rows': artifacts['rows'], 'trades': artifacts['trades'],
                'result': artifacts['result']}


def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument('--definition', type=Path, default=Path('configs/mc05_grid_tail.json'))
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--paths', type=int, default=8)
    parser.add_argument('--path-kind', choices=('gbm', 'oscillating'), default='gbm')
    parser.add_argument('--batch-backends', action='store_true')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    definition = json.loads(args.definition.read_text())
    generator = np.random.default_rng(1729)
    data = definition['dataset']
    increments = generator.normal(data['drift'] - .5 * data['volatility'] ** 2,
                                  data['volatility'], (args.paths, 168))
    prices = data['initial_price'] * np.exp(np.column_stack((np.zeros(args.paths),
                                                            increments.cumsum(axis=1))))
    if args.path_kind == 'oscillating':
        prices[:, 1:] = np.tile([180., 220.], 84)
    prices = freeze_prices(prices)
    np.save(args.output / 'prices.npy', prices)
    report = {'paths': args.paths, 'path_kind': args.path_kind, 'quote_decimals': 6, 'steps': 168,
              'definition': definition, 'runs': {}}
    results = {}
    for mode in ('paper_files', 'paper_memory', 'mc_files'):
        jobs = [(path, definition, mode) for path in prices]
        started = perf_counter()
        results[mode] = [replay(job) for job in jobs]
        elapsed = perf_counter() - started
        report['runs'][mode] = {'seconds': elapsed, 'paths_per_second': args.paths / elapsed}
    for disk, memory in zip(results['paper_files'], results['paper_memory']):
        if disk != memory:
            raise ValueError('File and memory Paper economic artifacts differ')
    report['file_memory_economics_exact'] = True
    for full, mc in zip(results['paper_files'], results['mc_files']):
        if full['result']['final']['equity'] != mc['terminal_nav'] or full['result']['fills'] != mc['fills']:
            raise ValueError('MC aggregate differs from Paper')
    jobs = [(path, definition, 'mc_files') for path in prices]
    started = perf_counter()
    with ProcessPoolExecutor(max_workers=8) as executor:
        parallel = list(executor.map(replay, jobs))
    elapsed = perf_counter() - started
    if parallel != results['mc_files']:
        raise ValueError('Parallel MC results differ')
    report['runs']['mc_files_8_workers'] = {'seconds': elapsed, 'paths_per_second': args.paths / elapsed}
    if args.batch_backends:
        config = BatchGridConfig(GridConfig(**definition['grid']),
            GridRules(**{k: D(v) for k, v in definition['rules'].items()}),
            capacity=D(definition['capacity']))
        for backend in ('cpu', 'cuda'):
            before = perf_counter()
            result = simulate(prices, config, backend=backend)
            elapsed = perf_counter()-before
            for expected, actual in zip(results['mc_files'], result.terminal):
                np.testing.assert_allclose(expected['terminal_nav'], actual[COLUMNS.index('equity')],
                                           rtol=1e-12, atol=5e-10)
                assert expected['fills'] == actual[COLUMNS.index('fills')]
            report['runs']['batch_'+backend+'_cold'] = dict(seconds=elapsed,
                paths_per_second=args.paths/elapsed, metadata=result.metadata)
            before = perf_counter()
            result = simulate(prices, config, backend=backend)
            elapsed = perf_counter()-before
            report['runs']['batch_'+backend+'_warm'] = dict(seconds=elapsed,
                paths_per_second=args.paths/elapsed, metadata=result.metadata)
    for mode in ('paper_memory', 'mc_files'):
        profile = cProfile.Profile()
        profile.runcall(replay, (prices[0], definition, mode))
        profile.dump_stats(str(args.output / (mode + '.prof')))
        with (args.output / (mode + '.txt')).open('w') as stream:
            pstats.Stats(profile, stream=stream).strip_dirs().sort_stats('cumulative').print_stats(60)
    (args.output / 'profile.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report['runs'], indent=2), flush=True)


if __name__ == '__main__':
    main()
