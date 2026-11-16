"""Bounded MC-03 comparisons through the existing close-only Grid account."""
from dataclasses import asdict
from pathlib import Path
import shutil
import tempfile
from time import perf_counter

import numpy as np
import pandas as pd

from factorlab.experiments.grid_monte_carlo import distribution, evaluate_path, stress_paths
from factorlab.experiments.grid_paper import _write
from factorlab.experiments.tracker import ExperimentTracker
from factorlab.research.monte_carlo import MonteCarloConfig, MonteCarloRunner
from factorlab.research.regime_jump import CalibrationError, JumpConfig, JumpStress, PathPlan, RegimeBootstrap, RegimeConfig


def scenario_models(inputs, receipt, *, threshold_as_of, regime_config=RegimeConfig()):
    """Return frozen generators, or explicit rejection of historical calibration."""
    times = pd.to_datetime(inputs.timestamps, utc=True)
    available = pd.to_datetime(inputs.available_at, utc=True)
    common = dict(returns=inputs.historical_returns, timestamps=times, available_at=available,
        as_of=receipt['as_of'], threshold_as_of=threshold_as_of, assets=inputs.assets,
        initial_prices=inputs.initial_prices, source=receipt, config=regime_config)
    models, rejection = {}, None
    try:
        historical = RegimeBootstrap(**common)
    except CalibrationError as exc:
        rejection = str(exc)
        try:
            pressure = RegimeBootstrap(**common, transition=np.eye(4))
        except CalibrationError:
            baseline = None
        else:
            baseline = pressure.receipt()['parameters']['transition']
    else:
        models['regime_historical'] = historical
        baseline = historical.receipt()['parameters']['transition']
    if baseline is not None:
        high = np.array(baseline)
        high[2:] = [0.01, 0.01, 0.49, 0.49]
        models['regime_high_persistence'] = RegimeBootstrap(**common, transition=high)
        models['regime_down_persistence'] = RegimeBootstrap(**common,
            transition=np.tile([.49, .01, .49, .01], (4, 1)))
    logs = np.log1p(np.asarray(inputs.historical_returns))
    drift, vol = tuple(logs.mean(axis=0)), tuple(logs.std(axis=0, ddof=1))
    for name, probability, size in [('jump_background', 0., 0.),
                                   ('jump_down', 1 / 168, np.log(.7)),
                                   ('jump_up', 1 / 168, np.log(1.3))]:
        models[name] = JumpStress(assets=inputs.assets, initial_prices=inputs.initial_prices,
            as_of=receipt['as_of'], source=receipt,
            config=JumpConfig(log_drift=drift, volatility=vol,
                jump_probability=probability, jump_log_sizes=(float(size),) * len(inputs.assets)))
    return models, rejection


def run_study(output, *, inputs, receipt, config, rules, start, threshold_as_of,
              plan=PathPlan(), regime_config=RegimeConfig(), code=None, command=()):
    """Keep only compact rows and one terminal-worst price path per arm."""
    if len(inputs.assets) != 1 or inputs.assets[0] != 'BINANCE:USDM:NVDAUSDT':
        raise ValueError('existing Grid adapter requires the single NVDAUSDT asset')
    if plan.n_paths > 64 or plan.n_steps > 336:
        raise ValueError('Grid comparison is bounded to 64 paths/model and 336 steps')
    if plan.seed + (plan.n_paths - 1) // plan.batch_size >= 2**32:
        raise ValueError('legacy Runner batch seeds exceed [0, 2**32)')
    origin = pd.Timestamp(start)
    if origin.tzinfo is None or origin <= pd.Timestamp(receipt['as_of']):
        raise ValueError('timezone-aware scenario start must follow calibration cutoff')
    if not np.isclose(float(inputs.initial_prices[0]), config.initial_price, rtol=0, atol=0):
        raise ValueError('Grid and scenario initial prices must agree')
    if receipt.get('time_unit') != 'hour':
        raise ValueError('Grid adapter requires hourly input cadence')
    models, rejection = scenario_models(inputs, receipt,
        threshold_as_of=threshold_as_of, regime_config=regime_config)
    plan.check_memory(1)
    directory = Path(output)
    directory.mkdir(parents=True, exist_ok=False)
    rows, generation, representatives = [], {}, {}
    started = perf_counter()
    generation_seconds = 0.
    with tempfile.TemporaryDirectory(prefix='factorlab-mc03-') as temporary:
        account = Path(temporary) / 'account'

        def execute(model, path_id, prices, stochastic):
            result = evaluate_path(prices, start=start, config=config, rules=rules,
                capacity='0.10', output=account)
            rows.append(dict(model=model, path_id=path_id, stochastic=stochastic, **result))
            if model not in representatives or result['terminal_nav'] < representatives[model]['terminal_nav']:
                representatives[model] = dict(path_id=path_id, terminal_nav=result['terminal_nav'],
                    start=start, prices=prices.tolist())
            shutil.rmtree(account)

        for model in ('gbm', 'moving_block_bootstrap'):
            generation[model] = []
            for batch_id, begin in enumerate(range(0, plan.n_paths, plan.batch_size)):
                before = perf_counter()
                cfg = MonteCarloConfig(model=model, n_paths=min(plan.batch_size, plan.n_paths - begin),
                    n_steps=plan.n_steps, dt=1., seed=plan.seed + batch_id, batch_size=plan.batch_size,
                    block_size=regime_config.block_size, save_full_paths=True)
                result = MonteCarloRunner(cfg).run(inputs)
                generation_seconds += perf_counter() - before
                generation[model].append(dict(begin=begin, metadata=result.metadata))
                for i, prices in enumerate(result.asset_paths[:, :, 0]):
                    execute(model, begin + i, prices, True)
                del result
        for name, generator in models.items():
            generation[name] = generator.receipt(plan)
            iterator = generator.batches(plan)
            while True:
                before = perf_counter()
                batch = next(iterator, None)
                generation_seconds += perf_counter() - before
                if batch is None:
                    break
                begin, paths = batch
                for i, prices in enumerate(paths[:, :, 0]):
                    execute(name, begin + i, prices, True)
                del paths, batch
        for name, prices in stress_paths(config.initial_price, plan.n_steps).items():
            execute('deterministic_' + name, 0, prices, False)
    summaries = {name: distribution([r for r in rows if r['model'] == name])
        for name in ('gbm', 'moving_block_bootstrap', *models)}
    definition = dict(model='mc_regime_jump/v1', parameters=dict(plan=asdict(plan),
        regime=asdict(regime_config), threshold_as_of=threshold_as_of, start=start,
        grid=asdict(config), rules={k: str(v) for k, v in asdict(rules).items()},
        synthetic_capacity='0.10', scenario='CLOSE_ONLY_SCENARIO'), dataset=receipt,
        factors=[], label={}, windows=dict(train=receipt['observations'], validation=None, test=None),
        walk_forward={}, costs=dict(fee_rate='.0004', slippage='.0006', funding='NOT_SIMULATED'))
    summary = dict(distributions=summaries, deterministic=[r for r in rows if not r['stochastic']],
        historical_calibration_rejection=rejection, confidence=.95,
        tail_observations=.05 * plan.n_paths, deterministic_in_probability_distribution=False,
        qualification='SYNTHETIC_RISK_SCENARIO; NOT_MARKET_STATISTICALLY_VALIDATED',
        risk_definition=dict(loss='1-terminal_NAV/1000', var='linear loss quantile',
            cvar='fractional worst (1-confidence)*n loss mean', maximum_drawdown='hourly fraction from running peak including initial equity'),
        evidence=['NO LIVE MONEY', 'SIMULATED_CLOSE_ONLY_PRICES', 'SYNTHETIC_PAPER_EXECUTION'],
        performance=dict(total_seconds=perf_counter() - started, generation_seconds=generation_seconds,
            max_retained_representative_paths=len(representatives),
            max_batch_path_bytes=min(plan.n_paths, plan.batch_size) * (plan.n_steps + 1) * 8))
    for name, data in [('definition', definition), ('generation', generation), ('summary', summary),
                       ('representative-paths', representatives)]:
        _write(directory / f'{name}.json', data)
    pd.DataFrame(rows).to_csv(directory / 'paths.csv', index=False)
    tracker = ExperimentTracker(directory / 'tracker')
    tracker.create_experiment('mc-03', definition)
    tracker.append_run('mc-03', 'study', code=code or dict(commit=None, tree=None),
        command=list(command), status='SUCCEEDED', qualification='SYNTHETIC',
        environment=dict(numpy=np.__version__), validation=dict(scenario='CLOSE_ONLY_SCENARIO'),
        artifacts=dict(summary=dict(path='summary.json'), paths=dict(path='paths.csv')))
    return summary
