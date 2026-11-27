"""MC-GRID-GPU-02 same-path synthetic performance and financial pilot.

Example: python tools/research/run_grid_dynamic_batch.py --paths 1000 --output NEW_DIR
Run scales separately when reserving the GPU; the default measures CPU and CUDA.
"""
import argparse
from dataclasses import asdict, replace
from decimal import Decimal
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
import threading
from time import perf_counter

import numpy as np
import psutil

from factorlab.experiments.grid_funding_adaptive import (
    FundingPolicy, VolatilityPolicy, make_grid_config,
)
from factorlab.experiments.grid_monte_carlo import _peak_rss_bytes
from factorlab.research.grid_simulation import (
    BatchGridConfig, BatchGridResult, BatchMarketInputs, COLUMNS, freeze_prices, simulate,
)
from factorlab.strategies.grid_execution import GridRules

STEPS = 168
SUBSTEPS = 8
WARMUP = 48
HOUR_US = 3_600_000_000
# 2026-11-15 00:59:59.999999 UTC: completed hourly bar, not its open.
ANCHOR_US = int(datetime(2026, 11, 15, 1, tzinfo=timezone.utc).timestamp()) * 1_000_000 - 1
SIGMA = .008
DRIFT = 0.
INITIAL = 200.


def profiles():
    grid = make_grid_config(INITIAL, half_width_pct=.015,
                            max_cash_committed=1000., fee_bps=4., slippage_bps=6.)
    rules = GridRules(quantity_unit=Decimal('.10'), step_size=Decimal('.01'),
                      tick_size=Decimal('.01'), min_quantity=Decimal('.01'),
                      min_notional=Decimal('5'))
    base = BatchGridConfig(grid, rules, capital=Decimal('1000'),
                           fee_rate=Decimal('.0004'), slippage=Decimal('.0006'),
                           capacity=Decimal('.10'))
    return {
        'static': base,
        'ATR': replace(base, grid=replace(grid, mode='atr', breakout='rebuild')),
        'C': replace(base, volatility_policy=VolatilityPolicy(reference_vol_1h=SIGMA)),
        'B': replace(base, funding_policy=FundingPolicy()),
    }


def frozen_batches(size, batch_size, seed):
    """Generate actual substep GBM extrema; rounding is shared by every engine."""
    rng = np.random.default_rng(seed)
    times = ANCHOR_US + np.arange(STEPS + 1, dtype=np.int64) * HOUR_US
    # Whole-hour settlements lie inside (previous close, current close].
    hours = np.arange(-24, STEPS, dtype=np.int64)
    funding_times = ANCHOR_US + 1 + hours * HOUR_US
    rates = np.where(hours < 56, .0000125,
                     np.where(hours < 112, .00015, -.000025))
    # Exogenous common marks keep rational funding packing O(events), not O(paths*events).
    marks = freeze_prices(INITIAL * (1.0002 + .01 * np.sin(hours / 24.)))
    for begin in range(0, size, batch_size):
        count = min(batch_size, size - begin)
        # Per-path contiguous draws make paths independent of batching and scale.
        increments = rng.normal((DRIFT - .5 * SIGMA ** 2) / SUBSTEPS,
                                SIGMA / np.sqrt(SUBSTEPS),
                                (count, (WARMUP + STEPS) * SUBSTEPS))
        process = np.exp(np.column_stack((np.zeros(count), increments.cumsum(axis=1))))
        process *= INITIAL / process[:, WARMUP * SUBSTEPS, None]
        process = freeze_prices(process)
        hourly = process[:, ::SUBSTEPS]
        prices = np.ascontiguousarray(hourly[:, WARMUP:])
        # The anchor OHLC is also from an actual simulated preceding hour.
        starts = (np.arange(WARMUP - 1, WARMUP + STEPS) * SUBSTEPS)
        highs = np.empty_like(prices)
        lows = np.empty_like(prices)
        for sub in range(SUBSTEPS + 1):
            samples = process[:, starts + sub]
            if sub == 0:
                highs[:] = lows[:] = samples
            else:
                np.maximum(highs, samples, out=highs)
                np.minimum(lows, samples, out=lows)
        warmup = np.ascontiguousarray(hourly[:, :WARMUP + 1])
        market = BatchMarketInputs(observation_times=times, highs=highs, lows=lows,
                                  funding_times=funding_times, funding_rates=rates,
                                  funding_marks=marks, expected_funding_times=funding_times,
                                  warmup_closes=warmup)
        yield begin, prices, market


class ResourceSampler:
    """Sample process RSS and real NVML usage, not allocator reservations."""
    def __init__(self, cuda, device_index):
        self.process = psutil.Process()
        self.stop = threading.Event()
        self.rss = 0
        self.device = self.gpu_process = None
        self.nvml_status = 'NOT_REQUESTED_CPU_ONLY'
        self.process_status = 'NOT_REQUESTED_CPU_ONLY'
        self.nvml = None
        if cuda:
            try:
                import pynvml
            except ImportError:
                self.nvml_status = self.process_status = 'PYNVML_NOT_INSTALLED'
            else:
                try:
                    pynvml.nvmlInit()
                    self.handle = pynvml.nvmlDeviceGetHandleByIndex(device_index)
                except pynvml.NVMLError as exc:
                    self.nvml_status = self.process_status = type(exc).__name__
                else:
                    self.nvml = pynvml
                    self.nvml_status = self.process_status = 'AVAILABLE'

    def sample(self):
        self.rss = max(self.rss, self.process.memory_info().rss)
        if self.nvml is None:
            return
        nvml = self.nvml
        try:
            used = int(nvml.nvmlDeviceGetMemoryInfo(self.handle).used)
        except nvml.NVMLError as exc:
            self.nvml_status = type(exc).__name__
        else:
            self.device = max(self.device or 0, used)
        try:
            processes = nvml.nvmlDeviceGetComputeRunningProcesses(self.handle)
        except nvml.NVMLError as exc:
            self.process_status = type(exc).__name__
        else:
            own = [p.usedGpuMemory for p in processes if p.pid == os.getpid()]
            unavailable = nvml.NVML_VALUE_NOT_AVAILABLE_ulonglong.value
            if any(value is None or value == unavailable for value in own):
                self.process_status = 'UNAVAILABLE_WDDM_OR_DRIVER'
            elif own:
                self.gpu_process = max(self.gpu_process or 0, sum(own))
            else:
                self.process_status = 'PROCESS_NOT_LISTED'

    def _run(self):
        while not self.stop.wait(.05):
            self.sample()

    def __enter__(self):
        self.sample()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.stop.set()
        self.thread.join()
        self.sample()
        if self.nvml is not None:
            self.nvml.nvmlShutdown()

    def report(self):
        return dict(process_sampled_peak_rss_bytes=self.rss,
                    process_lifetime_os_peak_rss_bytes=_peak_rss_bytes(),
                    nvml_device_sampled_peak_used_bytes=self.device,
                    nvml_process_sampled_peak_used_bytes=self.gpu_process,
                    nvml_device_status=self.nvml_status,
                    nvml_process_status=self.process_status,
                    sample_interval_seconds=.05,
                    scope='whole process/device, including prior allocator caches; sampled peaks, not continuous maxima')


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, default=str, allow_nan=False) + '\n', encoding='utf-8')


def compare(left, right):
    np.testing.assert_allclose(left.terminal, right.terminal, rtol=1e-12, atol=5e-10)
    np.testing.assert_allclose(left.trace, right.trace, rtol=1e-12, atol=5e-10)
    discrete = [COLUMNS.index(name) for name in ('fills', 'state', 'reason', 'rebuilds',
                                                'buy_blocked', 'pending_rebuild', 'last_rebuild')]
    np.testing.assert_array_equal(left.trace[:, :, discrete], right.trace[:, :, discrete])
    for expected, actual in zip(left.fills, right.fills, strict=True):
        np.testing.assert_array_equal(expected[:, :5], actual[:, :5])
        np.testing.assert_array_equal(expected[:, 8], actual[:, 8])
    for expected, actual in zip(left.fills, right.fills, strict=True):
        np.testing.assert_allclose(expected, actual, rtol=1e-12, atol=5e-10)
    from factorlab.research.grid_simulation.paper import canonical_order_events
    for expected, actual in zip(left.events, right.events, strict=True):
        np.testing.assert_allclose(canonical_order_events(expected), canonical_order_events(actual),
                                   rtol=1e-12, atol=5e-10)


def financial_summary(terminal, config):
    result = BatchGridResult(terminal, None, {})
    metrics = {}
    for name in ('equity', 'maximum_drawdown', 'fills', 'total_fees',
                 'total_slippage', 'total_funding_pnl', 'rebuilds'):
        values = terminal[:, COLUMNS.index(name)]
        metrics[name] = dict(mean=float(values.mean()), minimum=float(values.min()),
                             p05=float(np.quantile(values, .05)), median=float(np.median(values)),
                             p95=float(np.quantile(values, .95)), maximum=float(values.max()))
    return dict(metrics=metrics, risk=result.risk(config.capital))


def run_scale(args, size, configs, backends):
    root = args.output / str(size)
    root.mkdir()
    terminals = {(name, backend): np.lib.format.open_memmap(
        root / f'{name}-{backend}-terminal.npy', mode='w+', dtype=np.float64,
        shape=(size, len(COLUMNS))) for name in configs for backend in backends}
    totals = {(name, backend): dict(full_call_seconds=0., kernel_seconds=0., batches=0,
                                   setup_seconds=0., compile_or_cache_seconds=0.,
                                   upload_and_allocation_seconds=0., output_transfer_seconds=0.,
                                   device_allocation_peak_bytes=0)
              for name in configs for backend in backends}
    # Untimed paths are not manufactured: cold calls use the exact first measured batch.
    cold = {}
    generation_seconds = 0.
    generator = frozen_batches(size, args.batch_size, args.seed)
    with ResourceSampler('cuda' in backends, args.nvml_device) as resources:
        with (root / 'receipts.jsonl').open('w', encoding='utf-8') as receipts:
            while True:
                started = perf_counter()
                try:
                    begin, prices, market = next(generator)
                except StopIteration:
                    break
                generation_seconds += perf_counter() - started
                for name, config in configs.items():
                    results = {}
                    for backend in backends:
                        key = (name, backend)
                        if begin == 0:
                            started = perf_counter()
                            first = simulate(prices, config, market=market, backend=backend,
                                             batch_size=args.batch_size)
                            cold[f'{name}/{backend}'] = dict(
                                first_call_seconds=perf_counter() - started,
                                metadata=first.metadata,
                                scope='first call for this profile; process caches may already be warm')
                        started = perf_counter()
                        result = simulate(prices, config, market=market, backend=backend,
                                          batch_size=args.batch_size)
                        wall = perf_counter() - started
                        if begin == 0:
                            np.testing.assert_allclose(first.terminal, result.terminal,
                                                       rtol=1e-12, atol=5e-10)
                        terminals[key][begin:begin + len(prices)] = result.terminal
                        stats = totals[key]
                        stats['full_call_seconds'] += wall
                        stats['batches'] += 1
                        for metric in ('kernel_seconds', 'setup_seconds', 'compile_or_cache_seconds',
                                       'upload_and_allocation_seconds', 'output_transfer_seconds'):
                            stats[metric] += result.metadata.get(metric, 0.)
                        stats['device_allocation_peak_bytes'] = max(
                            stats['device_allocation_peak_bytes'],
                            result.metadata.get('device_allocation_peak_bytes', 0))
                        receipts.write(json.dumps(dict(profile=name, backend=backend, begin=begin,
                                                       paths=len(prices), full_call_seconds=wall,
                                                       metadata=result.metadata), default=str) + '\n')
                        results[backend] = result
                    if len(backends) == 2:
                        np.testing.assert_allclose(results['cpu'].terminal, results['cuda'].terminal,
                                                   rtol=1e-12, atol=5e-10)
                resources.sample()
    resource_report = resources.report()
    runs = {}
    for (name, backend), stats in totals.items():
        terminal = terminals[name, backend]
        terminal.flush()
        stats['shared_generation_seconds'] = generation_seconds
        stats['attributed_end_to_end_seconds'] = generation_seconds + stats['full_call_seconds']
        stats['paths_per_full_call_second'] = size / stats['full_call_seconds']
        stats['paths_per_attributed_e2e_second'] = size / stats['attributed_end_to_end_seconds']
        stats['financial'] = financial_summary(terminal, configs[name])
        runs[f'{name}/{backend}'] = stats
    # Selected random IDs are independent of financial outcomes, and bounded in count.
    ids = sorted(np.random.default_rng(args.seed + 1_000_003).choice(
        size, min(args.audit_paths, size), replace=False).tolist())
    audits = []
    if ids:
        selected = set(ids)
        for begin, prices, market in frozen_batches(size, args.batch_size, args.seed):
            local = [i - begin for i in ids if begin <= i < begin + len(prices)]
            if not local:
                continue
            indices = np.asarray(local)
            selected_market = replace(market, highs=market.highs[indices], lows=market.lows[indices],
                                      warmup_closes=market.warmup_closes[indices])
            for name, config in configs.items():
                paper = simulate(prices[indices], config, market=selected_market,
                                 backend='paper', trace=True, batch_size=1)
                for backend in backends:
                    actual = simulate(prices[indices], config, market=selected_market,
                                      backend=backend, trace=True, batch_size=1)
                    compare(paper, actual)
                    for row, path in enumerate(indices + begin):
                        np.savez_compressed(root / f'audit-{name}-{backend}-{path}.npz',
                                            prices=prices[indices[row]],
                                            highs=selected_market.highs[row], lows=selected_market.lows[row],
                                            observation_times=selected_market.observation_times,
                                            funding_times=selected_market.funding_times,
                                            funding_rates=selected_market.funding_rates,
                                            funding_marks=selected_market.funding_marks,
                                            warmup_closes=selected_market.warmup_closes[row],
                                            paper_steps=paper.trace[row], native_steps=actual.trace[row],
                                            paper_fills=paper.fills[row], native_fills=actual.fills[row],
                                            paper_audits=paper.events[row], native_audits=actual.events[row])
                    audits.append(dict(profile=name, backend=backend,
                                       paths=(indices + begin).tolist(), step_fill_audit_match=True))
            selected.difference_update((indices + begin).tolist())
            if not selected:
                break
    financial = {}
    reference_backend = backends[0]
    static = terminals['static', reference_backend][:, COLUMNS.index('equity')]
    for name in configs:
        delta = terminals[name, reference_backend][:, COLUMNS.index('equity')] - static
        financial[name] = dict(paired_nav_minus_static_mean=float(delta.mean()),
                               paired_nav_minus_static_p05=float(np.quantile(delta, .05)),
                               paired_nav_minus_static_p95=float(np.quantile(delta, .95)))
    report = dict(paths=size, shared_path_generation_seconds=generation_seconds,
                  timing_policy='Each profile/backend E2E attributes the SAME shared generation time once; these E2E values are not additive. Full-call wall includes wrapper input validation. Cold calls and selected audits excluded from warm totals.',
                  cold_calls=cold, warm_runs=runs, resources=resource_report,
                  selected_paper_comparisons=audits, paired_financial=financial,
                  financial_interpretation='Synthetic pilot only; no financial advantage claim. Funding is settled in-step at fixed 0.00000001 HALF_EVEN quantum, not a terminal overlay. Slippage is embedded in fills, not deducted twice.')
    write_json(root / 'report.json', report)
    return report


def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--paths', type=int, nargs='+', choices=(1000, 10000, 100000, 1000000), default=[1000])
    parser.add_argument('--backend', choices=('both', 'cpu', 'cuda'), default='both')
    parser.add_argument('--batch-size', type=int, default=4096)
    parser.add_argument('--seed', type=int, default=1729)
    parser.add_argument('--audit-paths', type=int, default=2)
    parser.add_argument('--nvml-device', type=int, default=0,
                        help='Physical NVML device index, independent of CUDA_VISIBLE_DEVICES')
    args = parser.parse_args()
    if not 1 <= args.batch_size <= 4096 or not 0 <= args.audit_paths <= 8:
        parser.error('batch-size must be 1..4096 and audit-paths 0..8')
    if len(set(args.paths)) != len(args.paths):
        parser.error('path scales must not repeat')
    configs = profiles()
    backends = ('cpu', 'cuda') if args.backend == 'both' else (args.backend,)
    args.output.mkdir(parents=True, exist_ok=False)
    report = dict(schema_version=1, evidence='RESEARCH_SIMULATION',
                  experiment='MC-GRID-GPU-02', python=platform.python_version(), numpy=np.__version__,
                  seed=args.seed, steps=STEPS, intrabar_substeps=SUBSTEPS, warmup_hours=WARMUP,
                  hourly_drift=DRIFT, hourly_sigma=SIGMA, initial_price=INITIAL,
                  anchor_close_utc_microseconds=ANCHOR_US,
                  model='GBM at eight equal intrabar substeps; true sampled-process OHLC extrema, not close-derived envelopes; six-decimal shared quote freeze',
                  funding='whole-hour settlements inside observation intervals: +0.125bps/h until hour56, +1.5bps/h until hour112, then -0.25bps/h; 24-hour causal history; common exogenous six-decimal marks=200*(1.0002+0.01*sin(hour/24)), not path closes',
                  output_policy='bounded <=4096 input/output batches; compact terminal memmaps; only selected small Paper traces/fills/audits',
                  comparison_policy='identical seeded paths, real OHLC and funding schedules for every profile/backend and Paper; native CPU is serial',
                  configuration={name: asdict(config) for name, config in configs.items()},
                  columns=COLUMNS, scales=[])
    write_json(args.output / 'report.json', report)
    for size in args.paths:
        scale = run_scale(args, size, configs, backends)
        report['scales'].append(scale)
        write_json(args.output / 'report.json', report)
        print(json.dumps(dict(paths=size, report=str(args.output / str(size) / 'report.json'))), flush=True)


if __name__ == '__main__':
    main()
