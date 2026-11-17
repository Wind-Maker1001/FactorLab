"""Calibrate alternative-model accounts before freezing F02. NO LIVE MONEY."""
import argparse
import json
from pathlib import Path
import shutil
import tempfile
from time import perf_counter

import numpy as np

from factorlab.experiments.grid_monte_carlo import historical_input, _peak_rss_bytes
from factorlab.experiments.grid_paper import D
from factorlab.experiments.mc_grid_tail_convergence import account_metrics, batches, grouped_statistics, write
from factorlab.experiments.mc_regime_jump import scenario_models
from factorlab.strategies.grid import GridConfig
from factorlab.strategies.grid_execution import GridRules
from factorlab.trading.storage import PaperStore


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--snapshot', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=False)
    definition = json.loads(Path('configs/mc05_f01_grid_tail.json').read_text())
    config = GridConfig(**definition['grid'])
    rules = GridRules(**{k: D(v) for k, v in definition['rules'].items()})
    started = perf_counter()
    inputs, receipt = historical_input(args.snapshot, definition['as_of'], config.initial_price)
    for field in ('data_version', 'bars_sha256'):
        if receipt[field] != definition['dataset'][field]:
            raise ValueError(f'frozen input identity differs: {field}')
    models, rejection = scenario_models(inputs, receipt, threshold_as_of=definition['threshold_as_of'])
    order = ['jump_down', 'regime_historical', 'moving_block_bootstrap']
    planning, accounts, calibrations = {}, [], {}
    with tempfile.TemporaryDirectory(prefix='mc05-f02-planning-') as temporary:
        root = Path(temporary)
        before = perf_counter()
        store = PaperStore(root/'creation.sqlite', research_wal=True)
        store.close()
        creation_seconds = perf_counter()-before
        sqlite_bytes = (root/'creation.sqlite').stat().st_size
        before = perf_counter()
        (root/'creation.sqlite').unlink()
        sqlite_cleanup_seconds = perf_counter()-before
        for model in order:
            if model == 'regime_historical' and model not in models:
                planning[model] = dict(status='CALIBRATION_REJECTED', reason=rejection)
                continue
            before = perf_counter()
            _, paths, metadata = next(batches(model, inputs, models.get(model), seed=8675309,
                begin=0, end=8, batch_size=8, steps=168))
            generation_seconds = perf_counter()-before
            calibrations[model] = metadata
            rows, times, cleanups = [], [], []
            for path_id, prices in enumerate(paths[:2]):
                before = perf_counter()
                row = account_metrics(prices, start=definition['start'], config=config, rules=rules,
                    capacity=definition['capacity'], output=root/'account')
                seconds = perf_counter()-before
                before = perf_counter()
                shutil.rmtree(root/'account')
                cleanup = perf_counter()-before
                times.append(seconds)
                cleanups.append(cleanup)
                rows.append(row)
                accounts.append(dict(model=model, seed=8675309, path_id=path_id,
                    seconds=seconds, cleanup_seconds=cleanup, metrics=row))
            before = perf_counter()
            grouped_statistics(rows, resamples=999, bootstrap_seed=8675309)
            statistics_seconds = perf_counter()-before
            planning[model] = dict(seconds_per_account_including_cleanup=max(t+c for t,c in zip(times, cleanups)),
                group_generation_statistics_seconds=6*generation_seconds + 3*statistics_seconds,
                generation_batch_seconds=generation_seconds, statistics_two_accounts_seconds=statistics_seconds,
                account_seconds=times, cleanup_seconds=cleanups)
    returns = np.asarray(inputs.historical_returns)[:, 0]
    def correlation(values, lag):
        return float(np.corrcoef(values[:-lag], values[lag:])[0, 1])
    history = dict(block_size=24, returns=len(returns),
        autocorrelations={str(lag): dict(returns=correlation(returns, lag),
            squared_returns=correlation(returns**2, lag)) for lag in (1, 6, 24)},
        worst_hour=float(returns.min()), worst_24h_compound=float(np.expm1(
            np.lib.stride_tricks.sliding_window_view(np.log1p(returns), 24).sum(axis=1).min())))
    write(output/'planning.json', dict(purpose='PRECALIBRATION_ONLY; excluded from formal samples and budget',
        seed=8675309, accounts=accounts, models=planning, calibration_rejection=rejection,
        total_seconds=perf_counter()-started, process_lifetime_peak_rss_bytes=_peak_rss_bytes(),
        sqlite_creation_close_seconds=creation_seconds, empty_sqlite_bytes=sqlite_bytes,
        sqlite_unlink_seconds=sqlite_cleanup_seconds, cost_safety_factor=1.2,
        projected_144_account_seconds=1.2*sum(48*p['seconds_per_account_including_cleanup']+
            p['group_generation_statistics_seconds'] for p in planning.values() if 'seconds_per_account_including_cleanup' in p),
        max_batch_workspace_bytes=8*8*169*8, history=history))
    write(output/'model-calibration.json', calibrations)


if __name__ == '__main__':
    main()
