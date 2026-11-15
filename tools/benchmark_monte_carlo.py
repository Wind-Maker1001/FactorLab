"""Bounded CPU/CUDA Monte Carlo comparison; writes compact JSON, never paths."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import platform
import time

import numpy as np
from scipy.stats import ks_2samp

from factorlab.compute import ComputeConfig, ComputeUnavailableError
from factorlab.experiments.provenance import capture_provenance
from factorlab.research.monte_carlo import MonteCarloConfig, MonteCarloInput, MonteCarloRunner


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--case', choices=('correctness', 'medium', 'chunked_stress', 'bootstrap_medium'))
    args = parser.parse_args()
    inputs = MonteCarloInput(weights=[.5, .3, .2], drift=[.06, .04, .02],
        volatility=[.2, .15, .1], correlation=[[1, .4, .2], [.4, 1, .3], [.2, .3, 1]])
    report = {'python': platform.python_version(), 'numpy': np.__version__, 'runs': [],
        **capture_provenance(Path(__file__).resolve().parents[1])}
    # Warm-up is bounded and excluded from measured runs.
    gpu_available = True
    for device in ('cpu', 'gpu'):
        try:
            MonteCarloRunner(MonteCarloConfig(n_paths=32, n_steps=4,
                compute=ComputeConfig(device=device))).run(inputs)
        except ComputeUnavailableError as exc:
            if device == 'cpu':
                raise
            gpu_available = False
            report['gpu_status'] = 'GPU_RUNTIME_NOT_RUN'
            report['gpu_reason'] = str(exc)
    cases = [('correctness', 2000, 24, 512), ('medium', 20000, 252, 4096),
             ('chunked_stress', 100000, 252, 4096)]
    history_rng = np.random.default_rng(818)
    history = history_rng.normal(0.0002, 0.01, (600, 3))
    dates = np.arange(np.datetime64('2020-01-01'), np.datetime64('2020-01-01') + 600)
    historical = MonteCarloInput(weights=[.5, .3, .2], historical_returns=history,
        timestamps=dates.astype(str).tolist(), available_at=dates.astype(str).tolist(),
        as_of=str(dates[-1]))
    cases.append(('bootstrap_medium', 20000, 252, 4096))
    if args.case:
        cases = [case for case in cases if case[0] == args.case]
    if any(case[0].startswith('bootstrap') for case in cases):
        for device in ('cpu', 'gpu') if gpu_available else ('cpu',):
            MonteCarloRunner(MonteCarloConfig(model='moving_block_bootstrap',
                n_paths=32, n_steps=4, block_size=3,
                compute=ComputeConfig(device=device))).run(historical)
    for name, paths, steps, batch in cases:
        method = 'moving_block_bootstrap' if name.startswith('bootstrap') else 'gbm'
        current_input = historical if method == 'moving_block_bootstrap' else inputs
        results = {}
        for device in ('cpu', 'gpu') if gpu_available else ('cpu',):
            cfg = MonteCarloConfig(model=method, n_paths=paths, n_steps=steps, batch_size=batch,
                dt=1/252, seed=20261115,
                compute=ComputeConfig(device=device, gpu_memory_limit_mb=512))
            start = time.perf_counter()
            result = MonteCarloRunner(cfg).run(current_input)
            elapsed = time.perf_counter() - start
            values = result.terminal_values
            results[device] = (elapsed, values, result.maximum_drawdowns)
            report['runs'].append({'case': name, 'device': device, 'paths': paths,
                'steps': steps, 'assets': 3, 'batch_size': batch, 'seconds': elapsed,
                'terminal_mean': float(values.mean()), 'terminal_std': float(values.std()),
                'var': result.var, 'cvar': result.cvar, 'metadata': result.metadata})
        if gpu_available:
            cpu_time, cpu, cpu_mdd = results['cpu']
            gpu_time, gpu, gpu_mdd = results['gpu']
            se = np.sqrt(cpu.var(ddof=1)/paths + gpu.var(ddof=1)/paths)
            difference = float(abs(cpu.mean() - gpu.mean()))
            # Two-sample distribution threshold fixed before observing results.
            ks_limit = float(np.sqrt(np.log(2 / 1e-5) / paths))
            terminal_ks = float(ks_2samp(cpu, gpu).statistic)
            drawdown_ks = float(ks_2samp(cpu_mdd, gpu_mdd).statistic)
            passed = bool(difference <= 6 * se and max(terminal_ks, drawdown_ks) <= ks_limit)
            report['runs'].append({'case': name, 'speedup': cpu_time/gpu_time,
                'mean_difference': difference, 'mean_difference_standard_error': float(se),
                'mean_tolerance': float(6 * se), 'terminal_ks': terminal_ks,
                'drawdown_ks': drawdown_ks, 'ks_tolerance': ks_limit,
                'statistical_pass': passed})
    if gpu_available:
        import cupy as cp
        properties = cp.cuda.runtime.getDeviceProperties(cp.cuda.runtime.getDevice())
        report['gpu_name'] = properties['name'].decode()
        report['gpu_status'] = 'GPU_RUNTIME_VERIFIED'
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, allow_nan=False), encoding='utf-8')
    print(json.dumps(report, indent=2, allow_nan=False))
    if any(row.get('statistical_pass') is False for row in report['runs']):
        raise SystemExit('CPU/CUDA distribution comparison failed')


if __name__ == '__main__':
    main()
