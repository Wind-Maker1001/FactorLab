"""Run the predeclared P3-12 synthetic negative-control experiment."""
import argparse
from dataclasses import asdict
from datetime import date
import json
from pathlib import Path

import numpy as np
import pandas as pd

from examples.research.signal_propagation_synthetic import fixture, fit
from factorlab.application.alpha_robustness import (
    ShuffledTrainingRunner, calendar_paired_inference, common_prediction_samples, cost_accounting,
    delayed_scores, random_scores)
from factorlab.application.signal_propagation import BOUNDARY, prediction_targets
from factorlab.backtest import AShareExecutionBacktester, ExecutionBacktestConfig
from factorlab.backtest.fees import FeeSchedule
from factorlab.dataset.identity import frame_hash, file_hash
from factorlab.evaluation.research import EvaluationContext, ResearchEvaluation, paired_inference, reprice_costs
from factorlab.experiments import ExperimentTracker
from factorlab.experiments.provenance import capture_provenance
from factorlab.metrics.robustness import RobustnessOptions
from factorlab.factors.registry_v2 import FactorRegistryV2

OPTIONS = RobustnessOptions(hac_lags=5, bootstrap_block_days=10,
    bootstrap_repetitions=500, min_valid_days=30, min_coverage=.8, holding_days=5, random_seed=42)
PROTOCOL = dict(schema=1, boundary=BOUNDARY, source='SYNTHETIC',
    random_seeds=[17, 29, 43], shuffle_seed=17, lag_sessions=5,
    shuffle='finite Train targets permuted within date; Validation and Test untouched',
    prediction_baseline='factor_score', portfolio_baseline='equal',
    families='separate RankIC and net-return Holm families; no joint cross-family claim',
    alpha=.05, inference=asdict(OPTIONS),
    failure='structural leakage, identity, timing, reproducibility, accounting or inference contract failure; not random profit',
    costs=dict(base=dict(commission_bps=3., slippage_bps=5.),
        slippage=dict(commission_bps=3., slippage_bps=20.),
        commission=dict(commission_bps=15., slippage_bps=5.)),
    allocation=dict(investment=.8, max_weight=.15, rebalance_sessions=5,
        fixed_rule='first six symbols in ascending canonical order; equal weights'))


def jsonable(value):
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    if isinstance(value, (date, pd.Timestamp, Path)):
        return str(value)
    if isinstance(value, (np.integer, np.bool_)):
        return value.item()
    if isinstance(value, (float, np.floating)):
        return float(value) if np.isfinite(value) else None
    return value


def save(path, value):
    path.write_text(json.dumps(jsonable(value), indent=2, allow_nan=False) + '\n', encoding='utf-8')


def fold_receipts(walk):
    f = walk.fold_summary
    return pd.DataFrame(dict(fold=f.fold, train_end=f.train_window_end_exclusive,
        validation_start=f.validation_window_start, validation_end=f.validation_window_end_exclusive,
        test_start=f.test_window_start, test_end=f.test_window_end_exclusive, label_end=f.train_max_exit_date))


def run(output):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    save(output / 'protocol.json', PROTOCOL)
    bars, benchmark, dates, membership, status, listings = fixture()
    dataset, walks, settings, splitter = fit(bars, benchmark, dates, membership, status, listings)
    shuffled = ShuffledTrainingRunner(splitter, model_name='ridge', feature_names=('MOM20', 'REV1'),
        top_k=10, minimum_cross_section=5, shuffle_seed=17, **settings['ridge']).run(
            dataset, calendar=dates[65:250])
    arms = {name: walk.predictions for name, walk in walks.items()}
    arms['shuffled_ridge'] = shuffled.predictions
    for seed in PROTOCOL['random_seeds']:
        arms[f'random_{seed}'] = random_scores(walks['factor_score'].predictions, seed=seed)
    for name, walk in walks.items():
        arms[name + '_lag5'] = delayed_scores(walk.predictions, dates, sessions=5)
    original_counts = {name: len(frame) for name, frame in arms.items()}
    arms = common_prediction_samples(arms)
    reference = arms['factor_score']
    evaluation_dates = pd.DatetimeIndex(sorted(reference.date.unique()))
    execution_dates = dates[(dates >= evaluation_dates[0]) &
        (dates <= dates[dates.get_loc(evaluation_dates[-1]) + 1])]
    sample = dict(prediction_dates=list(evaluation_dates), execution_dates=list(execution_dates),
        rows=len(reference), securities=reference.symbol.nunique(), original_rows=original_counts,
        common_identity=frame_hash(reference[['date', 'symbol', 'fold', 'EXCESS_RET5']]),
        dataset_identity=frame_hash(dataset), bars_identity=frame_hash(bars),
        prediction_arms=list(arms), portfolio_arms=list(arms) + ['equal', 'fixed_rule'])
    save(output / 'sample.json', sample)
    context = dict(dataset_id=frame_hash(dataset), universe_id=frame_hash(membership),
        label='EXCESS_RET5', label_id='EXCESS_RET5:next_open:5', benchmark_id=frame_hash(benchmark),
        holding_days=5, source_kind='SYNTHETIC')
    pred_evaluator = ResearchEvaluation(EvaluationContext(
        calendar=tuple(evaluation_dates.strftime('%Y-%m-%d')), **context))
    port_evaluator = ResearchEvaluation(EvaluationContext(
        calendar=tuple(execution_dates[1:].strftime('%Y-%m-%d')), **context))
    receipts = fold_receipts(walks['factor_score'])
    predictions = [pred_evaluator.predictions(name, frame, folds=receipts, top_k=10,
        minimum_cross_section=5) for name, frame in arms.items()]
    returns = bars.pivot(index='date', columns='symbol', values='raw_close').sort_index().pct_change(fill_method=None).iloc[1:]
    targets = {name: prediction_targets(frame, returns, objective='score')[0] for name, frame in arms.items()}
    targets['equal'] = prediction_targets(reference, returns, objective='equal')[0]
    fixed = targets['equal'].copy()
    selected = sorted(reference.symbol.unique())[:6]
    fixed['target_weight'] = np.where(fixed.symbol.isin(selected), .8 / len(selected), 0.)
    targets['fixed_rule'] = fixed
    config = ExecutionBacktestConfig(initial_cash=100000, minimum_cross_section=5)
    schedules = {name: FeeSchedule.a_share_default(**params) for name, params in PROTOCOL['costs'].items()}
    execution_bars = bars.loc[bars.date.isin(execution_dates)].merge(status,
        on=['date', 'symbol'], validate='one_to_one')
    execution_benchmark = benchmark.loc[benchmark.date.isin(execution_dates)]
    tracker = ExperimentTracker(output / 'experiments')
    provenance = capture_provenance(Path(__file__).resolve().parents[2])
    portfolio_results, summary, accounting = [], {}, {}
    all_evaluations = {result.name: result.to_dict() for result in predictions}
    for name, target in targets.items():
        scenarios = list(schedules) if name in walks else ['base']
        base_trades = None
        for scenario in scenarios:
            identifier = name if scenario == 'base' else name + '_' + scenario
            schedule = schedules[scenario]
            result = AShareExecutionBacktester(config, fee_schedule=schedule).run_target_weights(
                target, execution_bars, execution_benchmark)
            evaluated = port_evaluator.portfolio(identifier, result.nav,
                trades=result.trades, positions=result.positions)
            costs = float(result.trades.transaction_cost.sum())
            cash_residual = cost_accounting(result.nav, result.trades,
                initial_cash=config.initial_cash, schedule=schedule)
            if scenario == 'base':
                portfolio_results.append(evaluated)
                base_trades = result.trades
            repriced = reprice_costs(base_trades, {scenario: schedule})
            if (repriced.cost_delta < -1e-8).any():
                raise AssertionError('stress schedule reduced fixed-fill costs')
            accounting[identifier] = dict(max_cash_residual=cash_residual, ledger_cost=costs,
                nav_cost=float(result.nav.transaction_cost.sum()),
                fixed_fill_cost_delta=float(repriced.cost_delta.sum()),
                actual_fill_identity=frame_hash(result.trades), target_identity=frame_hash(target))
            summary[identifier] = dict(final_nav=float(result.nav.iloc[-1].strategy_nav),
                net_return=float(result.nav.iloc[-1].strategy_nav - 1), costs=costs,
                trades=len(result.trades), rejections=len(result.rejections),
                turnover=float(result.nav.turnover.sum()))
            all_evaluations['portfolio:' + identifier] = evaluated.to_dict()
            record = output / (identifier + '.json')
            save(record, dict(summary=summary[identifier], accounting=accounting[identifier],
                evaluation=evaluated.to_dict()))
            experiment_id = 'p312-' + identifier.replace('_', '-')
            tracker.create_experiment(experiment_id, jsonable(dict(model=name,
                parameters=dict(arm=name, scenario=scenario, protocol=PROTOCOL),
                factors=[asdict(FactorRegistryV2.with_builtins().freeze().get(factor, '1').ref)
                    for factor in ('MOM20', 'REV1')], dataset=dict(sha256=context['dataset_id']),
                label=dict(definition=context['label_id']),
                windows={part:[str(walks['ridge'].fold_summary[part + '_start'].min()),
                    str(walks['ridge'].fold_summary[part + '_end'].max())] for part in ('train','validation','test')},
                walk_forward=vars(splitter), costs=dict(execution=asdict(config),
                    schedule=[asdict(rule) for rule in schedule.rules]))))
            tracker.append_run(experiment_id, experiment_id, **provenance,
                command=['python', '-m', 'examples.research.alpha_robustness_synthetic', '--output', str(output)],
                status='SUCCEEDED', qualification='SYNTHETIC',
                artifacts=dict(summary=dict(path=str(record.resolve()), sha256=file_hash(record))),
                prediction_metrics={}, portfolio_metrics={k:v.value for k,v in evaluated.metrics.items()},
                validation=dict(boundary=BOUNDARY))
    prediction_inference = calendar_paired_inference(predictions,
        calendar=execution_dates[:-1], baseline='factor_score',
        metric='prediction_rank_ic', options=OPTIONS)
    portfolio_inference = paired_inference(portfolio_results, baseline='equal',
        metric='strategy_return', options=OPTIONS)
    result = dict(boundary=BOUNDARY, status='P312_ENGINEERING_ACCEPTED',
        protocol=PROTOCOL, sample=sample,
        rank_ic={r.name:asdict(r.metrics['prediction_rank_ic_mean']) for r in predictions},
        portfolios=summary, accounting=accounting,
        prediction_inference=prediction_inference.to_dict('records'),
        portfolio_inference=portfolio_inference.to_dict('records'),
        provenance=provenance, evaluations=all_evaluations)
    save(output / 'summary.json', result)
    print(json.dumps(jsonable(dict(status=result['status'], portfolios=summary)), indent=2))
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=BOUNDARY)
    parser.add_argument('--output', required=True, type=Path)
    run(parser.parse_args().output)
