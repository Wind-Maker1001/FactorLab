"""Reproduce native tree compute evidence on fixed synthetic walk-forward data."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import shutil
import sys
import threading
import time

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'src'))
from factorlab.compute import ComputeConfig, ComputeUnavailableError
from factorlab.validation import ExpandingWalkForwardSplitter, WalkForwardRunner
from factorlab.dataset.identity import frame_hash
from factorlab.experiments.provenance import capture_provenance


def fixture(symbols, features):
    rng = np.random.default_rng(42)
    dates = pd.bdate_range('2020-01-02', '2020-08-31')
    n = len(dates) * symbols
    names = tuple('feature_%02d' % i for i in range(features))
    values = rng.normal(size=(n, features))
    target = .02 * values[:, 0] - .01 * values[:, 1] + rng.normal(0, .005, n)
    values[rng.random(values.shape) < .01] = np.nan
    frame = pd.DataFrame(values, columns=names)
    frame['date'] = np.repeat(dates, symbols)
    frame['symbol'] = np.tile(['S%05d' % i for i in range(symbols)], len(dates))
    frame['entry_date'] = np.repeat(pd.Series(dates).shift(-1).to_numpy(), symbols)
    frame['exit_date'] = np.repeat(pd.Series(dates).shift(-6).to_numpy(), symbols)
    frame['EXCESS_RET5'] = target
    return dates, frame, names


class DeviceMemorySample:
    """Sample whole-device used memory, not process-exclusive allocation."""
    def __init__(self):
        self.values = []
        self.stop = threading.Event()

    def __enter__(self):
        def sample():
            while not self.stop.is_set():
                result = subprocess.run(['nvidia-smi', '--query-gpu=memory.used',
                    '--format=csv,noheader,nounits'], capture_output=True, text=True)
                if result.returncode == 0:
                    self.values.extend(int(v) for v in result.stdout.splitlines())
                self.stop.wait(.1)
        self.thread = threading.Thread(target=sample, daemon=True)
        if shutil.which('nvidia-smi'):
            self.thread.start()
        return self

    def __exit__(self, *_):
        self.stop.set()
        if self.thread.ident is not None:
            self.thread.join()


def run_once(model, device, frame, names, dates, trees):
    options = dict(n_estimators=trees, max_depth=5)
    if model == 'lightgbm':
        options.update(num_leaves=31, min_child_samples=20)
    runner = WalkForwardRunner(ExpandingWalkForwardSplitter(initial_train_months=3,
        validation_months=2, test_months=2, step_months=8, purge_days=5, embargo_days=5),
        model_name=model, feature_names=names, random_seed=42,
        compute=ComputeConfig(device=device), minimum_cross_section=2, top_k=10,
        **{model + '_candidates': [options], model + '_n_jobs': 4})
    started = time.perf_counter()
    with DeviceMemorySample() as memory:
        result = runner.run(frame, calendar=dates)
        elapsed = time.perf_counter() - started
    return result, dict(elapsed_seconds=elapsed, compute_records=result.compute_records,
        device_used_memory_peak_mib=max(memory.values) if memory.values else None,
        memory_measurement='whole-device memory.used sampled at approximately 100ms; includes other processes')


def run(model, device, frame, names, dates, trees):
    trials = [run_once(model, device, frame, names, dates, trees) for _ in range(3)]
    ordered = sorted(trials, key=lambda pair: pair[1]['elapsed_seconds'])
    result, receipt = ordered[1]
    receipt['trial_elapsed_seconds'] = [pair[1]['elapsed_seconds'] for pair in trials]
    receipt['aggregation'] = 'median of three complete Walk-Forward executions; stage receipts from median trial'
    receipt['cold_first_trial_resolution_seconds'] = sum(
        r.get('resolution_seconds', 0) for r in trials[0][1]['compute_records'])
    receipt['trial_device_memory_peaks_mib'] = [pair[1]['device_used_memory_peak_mib'] for pair in trials]
    return result, receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--models', nargs='+', default=['xgboost', 'lightgbm'])
    args = parser.parse_args()
    evidence = dict(seed=42, gpu_memory_limit_enforced=False, cases=[],
        provenance=capture_provenance(Path(__file__).resolve().parents[2]),
        splitter=dict(initial_train_months=3, validation_months=2, test_months=2,
                      step_months=8, purge_days=5, embargo_days=5))
    for size, symbols, features, trees in [('small', 16, 8, 40), ('medium', 512, 32, 160)]:
        dates, frame, names = fixture(symbols, features)
        data_identity = frame_hash(frame)
        for model in args.models:
            case = dict(size=size, model=model, dataset_sha256=data_identity,
                rows=len(frame), features=features,
                n_estimators=trees, n_jobs=4)
            cpu, case['cpu'] = run(model, 'cpu', frame, names, dates, trees)
            try:
                gpu, case['gpu'] = run(model, 'gpu', frame, names, dates, trees)
            except ComputeUnavailableError as exc:
                case.update(gpu_runtime='GPU_RUNTIME_NOT_RUN', performance='PERFORMANCE_NOT_RUN', error=str(exc))
            else:
                left = cpu.predictions.sort_values(['date', 'symbol']).reset_index(drop=True)
                right = gpu.predictions.sort_values(['date', 'symbol']).reset_index(drop=True)
                pd.testing.assert_frame_equal(left[['date', 'symbol']], right[['date', 'symbol']])
                delta = np.abs(left.score.to_numpy() - right.score.to_numpy())
                rank_ic = lambda f: f.groupby('date').apply(lambda g: g.score.corr(g.EXCESS_RET5, method='spearman'), include_groups=False).mean()
                cpu_ic, gpu_ic = float(rank_ic(left)), float(rank_ic(right))
                case.update(gpu_runtime='GPU_RUNTIME_VERIFIED', performance='PERFORMANCE_VERIFIED',
                    prediction_max_abs_difference=float(delta.max()), prediction_mean_abs_difference=float(delta.mean()),
                    cpu_rank_ic=cpu_ic, gpu_rank_ic=gpu_ic, rank_ic_difference=gpu_ic-cpu_ic,
                    end_to_end_speedup=case['cpu']['elapsed_seconds']/case['gpu']['elapsed_seconds'])
            evidence['cases'].append(case)
            print(json.dumps({k:v for k,v in case.items() if k not in ('cpu','gpu')}, allow_nan=False), flush=True)
    args.output.write_text(json.dumps(evidence, indent=2, allow_nan=False) + '\n', encoding='utf-8')


if __name__ == '__main__':
    main()
