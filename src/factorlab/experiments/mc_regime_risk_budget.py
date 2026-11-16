"""Paired, bounded risk budgets using the authoritative close-only Grid account."""
from dataclasses import asdict, replace
import json
from pathlib import Path
import shutil
import tempfile
from time import perf_counter

import numpy as np
import pandas as pd

from factorlab.dataset.identity import json_hash
from factorlab.experiments.grid_monte_carlo import distribution, evaluate_path, stress_paths
from factorlab.experiments.grid_paper import D, _write
from factorlab.experiments.grid_regime_budget import RegimeBudgetPolicy, make_budget_factory
from factorlab.experiments.mc_regime_jump import scenario_models
from factorlab.experiments.tracker import ExperimentTracker
from factorlab.research.regime_jump import PathPlan, RegimeConfig

VERSION = 1
QUALIFICATION = 'FINANCIAL_NOT_ESTABLISHED'
POLICY = {state: list(budget) for state, budget in RegimeBudgetPolicy.table.items()}
METRICS = ('terminal_nav', 'net_return', 'maximum_drawdown', 'traded_quantity', 'fills',
    'fees', 'slippage', 'average_inventory', 'max_inventory', 'average_exposure',
    'peak_exposure', 'average_exposure_nav', 'peak_exposure_nav', 'participation_rate',
    'inventory_mean_age_hours', 'inventory_max_age_hours', 'position_hours',
    'risk_exit_quantity', 'risk_exit_fills', 'unfinished_exit_quantity',
    'residual_inventory_loss', 'capital_utilization', 'zero_fills', 'budget_overshoot_rate')


def calibration_state(inputs, receipt, threshold_as_of, initial_price):
    """MC-03 observable feature convention, with actual availability enforced."""
    returns = np.asarray(inputs.historical_returns, dtype=float)
    times = pd.DatetimeIndex(pd.to_datetime(inputs.timestamps, utc=True))
    available = pd.DatetimeIndex(pd.to_datetime(inputs.available_at, utc=True))
    cutoff = pd.Timestamp(receipt['as_of'])
    threshold_cutoff = pd.Timestamp(threshold_as_of)
    if (returns.ndim != 2 or returns.shape[1] != 1 or not np.isfinite(returns).all()
            or (returns <= -1).any() or len(times) != len(returns)
            or len(available) != len(times) or not times.is_monotonic_increasing
            or times.has_duplicates or (available < times).any()
            or (available > cutoff).any() or (times > cutoff).any()):
        raise ValueError('complete single-asset history must be observed and available by as_of')
    if threshold_cutoff.tzinfo is None or cutoff.tzinfo is None or threshold_cutoff >= cutoff:
        raise ValueError('timezone-aware threshold prefix must precede as_of')
    split = int(times.searchsorted(threshold_cutoff, side='right'))
    if split < 25 or len(returns) < 24:
        raise ValueError('24-return feature history and threshold prefix required')
    logs = np.log1p(returns[:, 0])
    vol = np.lib.stride_tricks.sliding_window_view(logs[:split], 24).std(axis=1, ddof=1)
    threshold = float(np.quantile(vol, .5))
    tail = logs[-24:]
    prices = initial_price * np.exp(np.r_[-np.cumsum(tail[::-1])[::-1], 0.])
    identity = dict(lookback=24, volatility_threshold=threshold, volatility_quantile=.5,
        threshold_observation_end=times[split - 1].isoformat(),
        threshold_available_at_max=available[:split].max().isoformat(),
        seed_observations=[times[-24].isoformat(), times[-1].isoformat()],
        seed_available_at_max=available[-24:].max().isoformat(),
        seed_prices=prices.tolist(), seed_kind='historical returns rebased to frozen initial price',
        decision_lag_bars=1, latent_generator_labels_used=False)
    return threshold, prices.tolist(), identity


def account_metrics(directory, metrics, budget_events=()):
    """Reduce original account exports before deleting the temporary account."""
    directory = Path(directory)
    nav = pd.read_csv(directory / 'daily.csv')
    trades = pd.read_csv(directory / 'trades.csv')
    result = json.loads((directory / 'result.json').read_text(encoding='utf-8'))
    evaluation = json.loads((directory / 'evaluation.json').read_text(encoding='utf-8'))
    # End-of-hour snapshots have equal weight, including the initial anchor hour.
    exposure = nav.net_exposure.abs()
    ratios = exposure / nav.equity
    risk_ids = {key for key, order in result['orders'].items() if order['purpose'] == 'RISK_EXIT'}
    exits = trades.loc[trades.order_id.isin(risk_ids)]
    exit_requested = bool(risk_ids) or any(r.get('state') == 'EXITING' for r in result['risk'])
    incomplete = float(nav.inventory.iloc[-1]) if exit_requested else 0.
    events = pd.DataFrame(budget_events)
    overshoot = (float((nav.inventory.to_numpy() > events.inventory_cap.astype(float).to_numpy()).mean())
                 if len(events) else 0.)
    row = dict(metrics, net_return=metrics['terminal_nav'] / 1000 - 1,
        traded_quantity=float(trades.executed_shares.sum()),
        buy_quantity=float(trades.loc[trades.side.eq('BUY'), 'executed_shares'].sum()),
        sell_quantity=float(trades.loc[trades.side.eq('SELL'), 'executed_shares'].sum()),
        traded_notional=float(trades.trade_notional.sum()),
        average_inventory=float(nav.inventory.mean()), average_exposure=float(exposure.mean()),
        peak_exposure=float(exposure.max()), average_exposure_nav=float(ratios.mean()),
        peak_exposure_nav=float(ratios.max()), participation_rate=float(nav.inventory.ne(0).mean()),
        capital_utilization=float(exposure.mean() / 1000),
        risk_exit_fills=len(exits), risk_exit_quantity=float(exits.executed_shares.sum()),
        unfinished_exit_quantity=incomplete, zero_fills=int(len(trades) == 0),
        budget_overshoot_rate=overshoot,
        paused_rate=float(nav.state.eq('PAUSED').mean()),
        exiting_rate=float(nav.state.eq('EXITING').mean()),
        exited_rate=float(nav.state.eq('EXITED').mean()),
        buy_cancellations=sum(result['orders'][r['order_id']]['intent']['side'] == 'BUY'
                              and r['status'] == 'CANCELED' for r in result['lifecycle']))
    reconciliation = dict(fifo_residual=metrics['reconciliation_residual'],
        terminal_nav=metrics['terminal_nav'], exported_terminal_equity=float(nav.equity.iloc[-1]),
        actual_fees=metrics['fees'], ledger_fees=float(trades.fee.sum()),
        actual_slippage=metrics['slippage'], ledger_slippage=float(trades.slippage.sum()),
        fills=metrics['fills'], ledger_fills=len(trades),
        terminal_inventory=metrics['terminal_inventory'],
        exported_terminal_inventory=float(nav.inventory.iloc[-1]))
    return row, reconciliation, evaluation


def execute_account(prices, *, output, config, rules, start, capacity,
                    threshold, seed_prices, arm, static_scale=1.):
    instances = []
    factory = None
    if arm != 'A':
        create = make_budget_factory(volatility_threshold=threshold, seed_prices=seed_prices,
            policy='dynamic' if arm == 'B' else 'static', static_scale=static_scale)

        def factory(*args, **kwargs):
            grid = create(*args, **kwargs)
            instances.append(grid)
            return grid

    metrics = evaluate_path(prices, start=start, config=config, rules=rules, capacity=capacity,
        output=output, research_wal=True, grid_factory=factory)
    events = instances[0].budget_events if instances else []
    return account_metrics(output, metrics, events)


def paired_rows(rows):
    pairs = {}
    for row in rows:
        key = (row['model'], row['path_id'])
        pairs.setdefault(key, {})[row['arm']] = row
    output = []
    for (model, path_id), arms in pairs.items():
        for baseline in ('A', 'static'):
            if baseline not in arms or 'B' not in arms:
                continue
            a, b = arms[baseline], arms['B']
            output.append(dict(model=model, path_id=path_id, stochastic=b['stochastic'],
                contrast='B-' + baseline, path_hash=b['path_hash'],
                **{key: b[key] - a[key] for key in METRICS}))
    return output


def summarize(rows, paired):
    models = sorted({r['model'] for r in rows if r['stochastic']})
    groups = {}
    for model in models:
        for arm in ('A', 'B', 'static'):
            sample = [r for r in rows if r['model'] == model and r['arm'] == arm]
            if not sample:
                continue
            groups[f'{model}/{arm}'] = distribution(sample) | dict(
                means={metric: float(np.mean([r[metric] for r in sample])) for metric in METRICS},
                effective_tail_paths=.05 * len(sample))
    differences = {}
    for model in models:
        for contrast in ('B-A', 'B-static'):
            sample = [r for r in paired if r['model'] == model and r['contrast'] == contrast]
            if sample:
                differences[f'{model}/{contrast}'] = dict(paths=len(sample),
                    means={key: float(np.mean([r[key] for r in sample])) for key in METRICS},
                    medians={key: float(np.median([r[key] for r in sample])) for key in METRICS})
    return dict(distributions=groups, paired=differences,
        deterministic=[r for r in rows if not r['stochastic']],
        deterministic_in_probability_distribution=False)


def run_study(output, *, inputs, receipt, config, rules, start, threshold_as_of,
              plan=PathPlan(n_paths=16, n_steps=168, batch_size=4, seed=714),
              models=('regime_historical', 'regime_high_persistence', 'jump_down'),
              calibration_seed=10714, calibration_paths=2, static_control=True,
              capacity='0.10', max_seconds=1800., regime_config=RegimeConfig(),
              code=None, command=()):
    if (config.mode == 'atr' or plan.n_paths > 64 or not 2 <= plan.n_steps <= 336
            or not 0 < max_seconds <= 1800 or not 1 <= calibration_paths <= 8):
        raise ValueError('non-ATR study requires bounded paths, steps, calibration and time')
    if (len(inputs.assets) != 1 or inputs.assets[0] != 'BINANCE:USDM:NVDAUSDT'
            or receipt.get('time_unit') != 'hour'
            or float(inputs.initial_prices[0]) != config.initial_price):
        raise ValueError('single-asset hourly Grid and scenario anchor must agree')
    origin, cutoff = pd.Timestamp(start), pd.Timestamp(receipt['as_of'])
    if origin.tzinfo is None or origin <= cutoff:
        raise ValueError('scenario start must follow available calibration cutoff')
    if calibration_seed == plan.seed:
        raise ValueError('calibration and evaluation require disjoint seed namespaces')
    if not models or len(set(models)) != len(models):
        raise ValueError('unique explicit scenario models required')
    plan.check_memory(1)
    threshold, seeds, state_identity = calibration_state(inputs, receipt, threshold_as_of, config.initial_price)
    generators, rejection = scenario_models(inputs, receipt, threshold_as_of=threshold_as_of,
        regime_config=regime_config)
    unavailable = [name for name in models if name not in generators]
    if unavailable:
        raise ValueError(f'requested scenario calibration unavailable: {unavailable}; {rejection}')
    calibrate = replace(plan, n_paths=calibration_paths, seed=calibration_seed)
    root = Path(output)
    root.mkdir(parents=True, exist_ok=False)
    started = perf_counter()
    generation = {name: generators[name].receipt(plan) for name in models}
    definition = dict(model='p7-r14/regime-risk-budget',
        parameters=dict(version=VERSION, grid=asdict(config), rules={k: str(v) for k, v in asdict(rules).items()},
            policy=POLICY, observable_state=state_identity, plan=asdict(plan),
            models=list(models), capacity=capacity, start=start, max_seconds=max_seconds,
            calibration_plan=asdict(calibrate) if static_control else None,
            static_control=static_control, static_rule='clip(mean_exposure_B/mean_exposure_A,0,1); no search',
            zero_baseline_exposure_rule='static_scale=0; exposure matching unavailable',
            budget_exit='EXISTING_FULL_POSITION_RISK_EXIT; NO_AUTO_RESTART'),
        dataset=receipt, factors=[], label={}, windows=dict(train=receipt['observations'],
            validation=None, test=None), walk_forward=dict(mode='FROZEN_SYNTHETIC_ONLY'),
        costs=dict(initial_equity=1000, fee_rate='.0004', slippage='.0006', funding='NOT_SIMULATED'))
    _write(root / 'definition.json', definition)
    _write(root / 'generation.json', generation)
    _write(root / 'status.json', dict(status='RUNNING', qualification=QUALIFICATION))
    rows, calibrations, reconciliation, tails, evaluations = [], [], [], {}, {}
    scales = {}
    exhausted = False
    tracker = ExperimentTracker(root / 'tracker')
    tracker.create_experiment('p7-r14', definition)

    def elapsed():
        return perf_counter() - started

    with tempfile.TemporaryDirectory(prefix='factorlab-r14-') as temporary:
        account = Path(temporary) / 'account'

        def execute(prices, model, path_id, stochastic, arms, phase):
            path_hash = json_hash(dict(start=start, prices=np.asarray(prices).tolist()))
            pair = []
            for arm in arms:
                metrics, audit, evaluation = execute_account(prices, output=account,
                    config=config, rules=rules, start=start, capacity=capacity,
                    threshold=threshold, seed_prices=seeds, arm=arm,
                    static_scale=scales.get(model, 1.))
                shutil.rmtree(account)
                pair.append(dict(model=model, path_id=path_id, stochastic=stochastic,
                    arm=arm, path_hash=path_hash, phase=phase, **metrics))
                reconciliation.append(dict(model=model, path_id=path_id, arm=arm, phase=phase, **audit))
                evaluations[f'{phase}/{model}/{path_id}/{arm}'] = evaluation
            return pair

        if static_control:
            for name in models:
                for begin, batch in generators[name].batches(calibrate):
                    for i, prices in enumerate(batch[:, :, 0]):
                        if elapsed() >= max_seconds:
                            exhausted = True
                            break
                        calibrations.extend(execute(prices, name, begin + i, True, ('A', 'B'), 'calibration'))
                    if exhausted:
                        break
                if exhausted:
                    break
                sample = [r for r in calibrations if r['model'] == name]
                a = np.mean([r['average_exposure'] for r in sample if r['arm'] == 'A'])
                b = np.mean([r['average_exposure'] for r in sample if r['arm'] == 'B'])
                scales[name] = float(np.clip(b / a, 0, 1)) if a else 0.
            # Deterministic stress controls use a pooled calibration-only scale.
            if not exhausted:
                a = np.mean([r['average_exposure'] for r in calibrations if r['arm'] == 'A'])
                b = np.mean([r['average_exposure'] for r in calibrations if r['arm'] == 'B'])
                scales['deterministic'] = float(np.clip(b / a, 0, 1)) if a else 0.
        _write(root / 'calibration.json', dict(scales=scales, rows=calibrations,
            generation={name: generators[name].receipt(calibrate) for name in models} if static_control else {},
            selection='single exposure-ratio formula; no evaluation data'))
        arms = ('A', 'B', 'static') if static_control else ('A', 'B')

        def save_pair(name, path_id, prices, stochastic):
            pair = execute(prices, name, path_id, stochastic, arms, 'evaluation')
            rows.extend(pair)
            pd.DataFrame(rows).to_csv(root / 'paths.csv', index=False)
            by_arm = {r['arm']: r for r in pair}
            scores = {'worst_A': by_arm['A']['terminal_nav'],
                      'worst_B_minus_A': by_arm['B']['terminal_nav'] - by_arm['A']['terminal_nav']}
            for selection, score in scores.items():
                key = name + '/' + selection
                if key not in tails or score < tails[key]['score']:
                    tails[key] = dict(model=name, path_id=path_id, score=score,
                        selection=selection, path_hash=by_arm['A']['path_hash'],
                        prices=np.asarray(prices).tolist(), accounts=pair)
            print(f'{name}/{path_id}: ' + ' '.join(f'{r["arm"]}={r["terminal_nav"]:.6f}' for r in pair), flush=True)

        if not exhausted:
            for name in models:
                for begin, batch in generators[name].batches(plan):
                    for i, prices in enumerate(batch[:, :, 0]):
                        if elapsed() >= max_seconds:
                            exhausted = True
                            break
                        save_pair(name, begin + i, prices, True)
                    if exhausted:
                        break
                if exhausted:
                    break
        if not exhausted:
            for name, prices in stress_paths(config.initial_price, plan.n_steps).items():
                if elapsed() >= max_seconds:
                    exhausted = True
                    break
                if static_control:
                    scales['deterministic_' + name] = scales['deterministic']
                save_pair('deterministic_' + name, 0, prices, False)
    pairs = paired_rows(rows)
    summary = summarize(rows, pairs)
    summary.update(status='TIME_BUDGET_EXHAUSTED' if exhausted else 'SUCCEEDED',
        qualification=QUALIFICATION, static_scales=scales, historical_calibration_rejection=rejection,
        definition_hash=json_hash(definition), paths_completed=len(rows) // len(arms),
        account_evaluations=len(reconciliation), elapsed_seconds=elapsed(),
        metric_definition=dict(loss='1-terminal_NAV/1000; negative means gain',
            var='linear loss quantile', cvar='fractional worst 5% loss mean; per model, not pooled',
            exposure='absolute marked inventory notional; equal end-of-hour snapshot weights including anchor',
            capital_utilization='mean marked absolute inventory notional / initial equity',
            inventory_age='existing FIFO quantity-weighted realized and terminal-open ages',
            unfinished_exit='confirmed residual inventory whenever full risk exit was requested',
            static_control='calibration-only exposure ratio; realized evaluation exposure need not match'),
        evidence=['NO LIVE MONEY', 'SIMULATED_CLOSE_ONLY_PRICES', 'SYNTHETIC_PAPER_EXECUTION'],
        exchange_execution_evidence=False, funding='NOT_SIMULATED')
    pd.DataFrame(rows).to_csv(root / 'paths.csv', index=False)
    pd.DataFrame(pairs).to_csv(root / 'paired.csv', index=False)
    _write(root / 'summary.json', summary)
    _write(root / 'reconciliation.json', reconciliation)
    _write(root / 'evaluation.json', evaluations)
    _write(root / 'representative-tails.json', tails)
    _write(root / 'status.json', dict(status=summary['status'], qualification=QUALIFICATION))
    tracker.append_run('p7-r14', 'study', code=code or dict(commit=None, tree=None),
        command=list(command), status='FAILED' if exhausted else 'SUCCEEDED', qualification='SYNTHETIC',
        environment=dict(numpy=np.__version__), validation=dict(qualification=QUALIFICATION),
        artifacts={name: dict(path=name) for name in ('summary.json', 'paths.csv', 'paired.csv',
            'calibration.json', 'reconciliation.json', 'evaluation.json', 'representative-tails.json')})
    return summary
