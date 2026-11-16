"""Fixed-input paired Grid execution benchmark; NO LIVE MONEY."""
import argparse
import cProfile
from contextlib import closing
import ctypes
from ctypes import wintypes
import json
import os
import platform
import sys
from dataclasses import asdict
from pathlib import Path
import pstats
import sqlite3
import tempfile
from time import perf_counter, process_time

import numpy as np

from factorlab.experiments.grid_monte_carlo import evaluate_path, stress_paths, _peak_rss_bytes
from factorlab.experiments.grid_paper import D
from factorlab.strategies.grid import GridConfig
from factorlab.strategies.grid_execution import GridRules


def io_bytes():
    if os.name == 'nt':
        class Counters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in (
                'ReadOperationCount', 'WriteOperationCount', 'OtherOperationCount',
                'ReadTransferCount', 'WriteTransferCount', 'OtherTransferCount')]
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        kernel.GetProcessIoCounters.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters)]
        counters = Counters()
        if not kernel.GetProcessIoCounters(kernel.GetCurrentProcess(), ctypes.byref(counters)):
            raise ctypes.WinError(ctypes.get_last_error())
        return counters.WriteTransferCount
    return int(next(line.split()[1] for line in Path('/proc/self/io').read_text().splitlines()
                    if line.startswith('write_bytes:')))


def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--steps', type=int, default=168)
    parser.add_argument('--repeats', type=int, default=1)
    parser.add_argument('--profile', action='store_true')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    config = GridConfig(lower=80, upper=120, levels=4, initial_price=100,
        initial_inventory=0, initial_cash=1000, quantity=10, max_drawdown=.9,
        max_turnover=100, breakout='exit', fee_bps=4, slippage_bps=6)
    rules = GridRules(D('.01'), D('.01'), D('.01'), D('.01'), D('5'))
    paths = stress_paths(100, args.steps)
    rng = np.random.default_rng(710)
    paths['fixed_gbm'] = 100 * np.exp(np.r_[0., np.cumsum(rng.normal(-.0001, .012, args.steps))])
    returns = np.array([-.012, .008, -.004, .015, -.009, .003, .006, -.007])
    blocks = [returns[rng.integers(0, 5):][:4] for _ in range((args.steps+3)//4)]
    paths['fixed_block_bootstrap'] = 100 * np.cumprod(np.r_[1., 1+np.concatenate(blocks)[:args.steps]])
    paths['partial_exit'] = np.r_[100., 89., 88., 70., np.full(args.steps-3, 69.)]
    (args.output/'inputs.json').write_text(json.dumps({k: v.tolist() for k, v in paths.items()}))
    (args.output/'environment.json').write_text(json.dumps(dict(
        python=sys.version, platform=platform.platform(), numpy=np.__version__,
        sqlite=sqlite3.sqlite_version, config=asdict(config), rules=asdict(rules),
        steps=args.steps, repeats=args.repeats, profile=args.profile,
        timing_scope='evaluate_path, receipt and authority comparison input capture, directory cleanup',
        rss_scope='process lifetime peak working set, not incremental per-account allocation',
        write_scope='process WriteTransferCount on Windows; /proc/self/io write_bytes on Linux',
        fee_rate='.0004', slippage='.0006', seed=710), default=str, indent=2))
    results = []
    for repeat in range(args.repeats):
        for name, prices in paths.items():
            for capacity in ('.10', '.01'):
                reference = None
                for wal in ((False, True) if repeat % 2 == 0 else (True, False)):
                    profile = cProfile.Profile()
                    writes, cpu, wall = io_bytes(), process_time(), perf_counter()
                    if args.profile:
                        profile.enable()
                    with tempfile.TemporaryDirectory(prefix='factorlab-mc01-') as temporary:
                        directory = Path(temporary)/'account'
                        metrics = evaluate_path(prices, start='2026-11-15T00:00:00Z',
                            config=config, rules=rules, capacity=capacity, output=directory,
                            research_wal=wal)
                        artifacts = {f: (directory/f).read_bytes() for f in
                                     ('daily.csv', 'trades.csv', 'result.json', 'evaluation.json')}
                        with closing(sqlite3.connect(directory/'paper.sqlite')) as db:
                            authority = (db.execute('SELECT * FROM snapshot ORDER BY id').fetchall(),
                                         db.execute('SELECT * FROM events ORDER BY rowid').fetchall())
                    profile.disable()
                    elapsed, cpu_elapsed, written = perf_counter()-wall, process_time()-cpu, io_bytes()-writes
                    identity = (metrics, artifacts, authority)
                    if reference is None:
                        reference = identity
                    else:
                        assert reference == identity, (name, capacity, 'accounting mismatch')
                    label = f'{repeat}-{name}-{capacity}-{"wal" if wal else "delete"}'
                    if args.profile:
                        profile.dump_stats(str(args.output/(label+'.prof')))
                        with (args.output/(label+'.txt')).open('w') as stream:
                            pstats.Stats(profile, stream=stream).sort_stats('cumulative').print_stats(100)
                        functions = [dict(file=Path(file).name, line=line, function=function,
                            calls=values[1], self_seconds=values[2], cumulative_seconds=values[3])
                            for (file, line, function), values in pstats.Stats(profile).stats.items()]
                        (args.output/(label+'-functions.json')).write_text(json.dumps(functions))
                    row = dict(repeat=repeat, path=name, capacity=capacity, wal=wal,
                        wall_seconds=elapsed, cpu_seconds=cpu_elapsed, write_bytes=written,
                        peak_rss_bytes=_peak_rss_bytes(), metrics=metrics)
                    results.append(row)
                    (args.output/'measurements.json').write_text(json.dumps(results, indent=2))
                    print(json.dumps({k: v for k, v in row.items() if k != 'metrics'}), flush=True)
    print('All paired metrics, CSV/JSON receipts and authoritative SQLite records match.', flush=True)


if __name__ == '__main__':
    main()
