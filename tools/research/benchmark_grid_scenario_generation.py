"""Isolate CPU/CUDA price generation from sequential Grid Paper execution."""
import argparse
from pathlib import Path
import sys
from time import perf_counter

import numpy as np

from factorlab.compute import ComputeConfig
from factorlab.experiments.grid_monte_carlo import historical_input, _peak_rss_bytes
from factorlab.experiments.grid_paper import _write
from factorlab.experiments.provenance import capture_provenance
from factorlab.research.monte_carlo import MonteCarloConfig, MonteCarloRunner


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--snapshot', required=True)
    parser.add_argument('--as-of', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    inputs, receipt = historical_input(args.snapshot, args.as_of, 204.34)
    records = {}
    for model in ('gbm', 'moving_block_bootstrap'):
        samples = {}
        for device in ('cpu', 'gpu'):
            elapsed, terminals, drawdowns, receipts = [], [], [], []
            for batch in range(16):
                config = MonteCarloConfig(model=model, n_paths=16, n_steps=168, dt=1.,
                    batch_size=16, block_size=24, seed=710+batch, save_full_paths=True,
                    compute=ComputeConfig(device=device, gpu_memory_limit_mb=256))
                t = perf_counter()
                result = MonteCarloRunner(config).run(inputs)
                elapsed.append(perf_counter()-t)
                terminals.extend(result.asset_paths[:, -1, 0].tolist())
                drawdowns.extend(result.maximum_drawdowns.tolist())
                receipts.append(result.metadata)
                del result
            values = np.asarray(terminals)
            samples[device] = values
            records[f'{model}-{device}'] = dict(paths=len(values), steps=168,
                total_seconds=sum(elapsed), first_batch_seconds=elapsed[0],
                remaining_batches_seconds=sum(elapsed[1:]),
                terminal_mean=float(values.mean()), terminal_std=float(values.std(ddof=1)),
                terminal_quantiles={str(q): float(np.quantile(values, q)) for q in (.01, .05, .5, .95, .99)},
                drawdown_mean=float(np.mean(drawdowns)), receipts=receipts)
        cpu, gpu = samples['cpu'], samples['gpu']
        se = np.sqrt(cpu.var(ddof=1)/len(cpu)+gpu.var(ddof=1)/len(gpu))
        records[f'{model}-comparison'] = dict(
            mean_difference_standard_errors=float(abs(cpu.mean()-gpu.mean())/se),
            independent_streams=True, pathwise_equality_required=False,
            generator_elapsed_ratio=records[f'{model}-cpu']['total_seconds']/records[f'{model}-gpu']['total_seconds'])
    _write(Path(args.output), dict(input=receipt, records=records,
        process_peak_rss_bytes=_peak_rss_bytes(), code=capture_provenance(Path(__file__).resolve().parents[2])['code'],
        command=[sys.executable, *sys.argv],
        scope='price generation + host transfer + fixed-weight reference reduction only; no Grid execution',
        gpu_budget='private CuPy allocator only; excludes CUDA context and external library allocations',
        timing='first batch includes initialization; subsequent batches still instantiate existing Runner private pools'))


if __name__ == '__main__':
    main()
