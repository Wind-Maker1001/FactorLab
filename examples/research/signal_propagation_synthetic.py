"""Run four fixed models against three synthetic portfolio objectives."""
import argparse
from dataclasses import asdict
import json
from pathlib import Path

import pandas as pd

from factorlab.application.signal_propagation import BOUNDARY, OBJECTIVES, prediction_targets, pairwise_transmission
from factorlab.backtest import AShareExecutionBacktester, ExecutionBacktestConfig
from factorlab.dataset import DatasetBuilder, DatasetBuildOptions
from factorlab.dataset.identity import frame_hash, file_hash
from factorlab.evaluation.research import EvaluationContext, ResearchEvaluation, compare_results
from factorlab.experiments import ExperimentTracker
from factorlab.experiments.provenance import capture_provenance
from factorlab.exploratory.factor_discovery.synthetic import make_synthetic_data
from factorlab.factors.registry_v2 import FactorRegistryV2, FactorDataContext
from factorlab.universe.filters import UniverseFilterConfig
from factorlab.validation import ExpandingWalkForwardSplitter, WalkForwardRunner


def fixture():
    bars, benchmark, dates = make_synthetic_data(stock_count=27, sessions=270)
    bars = bars.loc[~bars.symbol.isin(['000002.SZ', '000003.SZ', '000004.SZ'])].copy()
    benchmark['adj_close'] = benchmark.adj_open
    symbols = tuple(sorted(bars.symbol.unique()))
    grid = pd.MultiIndex.from_product([dates, symbols], names=['date', 'symbol']).to_frame(index=False)
    status = grid.assign(trade_status=1, is_st=False)
    status.loc[status.symbol.eq(symbols[0]) & status.date.between(dates[145], dates[155]), 'trade_status'] = 0
    return bars, benchmark, dates, grid.assign(is_member=True), status, pd.DataFrame(dict(symbol=symbols, listing_date=dates[0]))


def fit(bars, benchmark, dates, membership, status, listings):
    snapshot = FactorRegistryV2.with_builtins().freeze()
    refs = tuple(snapshot.get(name, '1').ref for name in ('MOM20', 'REV1'))
    builder = DatasetBuilder(DatasetBuildOptions(holding_days=5, expected_member_count=24,
        minimum_cross_section=5, filter_config=UniverseFilterConfig(min_listed_days=1,
            zero_volume_window=1, quality_window=5, min_quality_ratio=.8)),
        snapshot=snapshot, factor_refs=refs,
        data_context=FactorDataContext('synthetic-p309', 'SYNTHETIC_CLOSE_ONLY'),
        data_version='seed-20261014-v1', calendar_id='synthetic-bday-v1')
    dataset = builder.build(bars, benchmark, membership, status, listings, dates)
    dataset = dataset.loc[dataset.date.isin(dates[65:250])].reset_index(drop=True)
    splitter = ExpandingWalkForwardSplitter(initial_train_months=2, validation_months=1,
        test_months=1, step_months=2, purge_days=5, embargo_days=5)
    options = dict(factor_score=dict(factor_weights={'MOM20': .5, 'REV1': .5}),
        ridge=dict(ridge_alphas=(.1, 1.)),
        lightgbm=dict(lightgbm_candidates=[dict(n_estimators=8, num_leaves=7, min_child_samples=10)]),
        xgboost=dict(xgboost_candidates=[dict(n_estimators=8, max_depth=2)]))
    walks = {model: WalkForwardRunner(splitter, model_name=model,
        feature_names=builder.factor_names, top_k=10, minimum_cross_section=5,
        **settings).run(dataset, calendar=dates[65:250]) for model, settings in options.items()}
    return dataset, walks, options, splitter


def run(output):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    bars, benchmark, dates, membership, status, listings = fixture()
    dataset, walks, options, splitter = fit(bars, benchmark, dates, membership, status, listings)
    returns = bars.pivot(index='date', columns='symbol', values='raw_close').sort_index().pct_change(fill_method=None).iloc[1:]
    tracker = ExperimentTracker(output / 'experiments')
    provenance = capture_provenance(Path(__file__).resolve().parents[2])
    summaries, predictions_eval = {}, []
    context = dict(dataset_id=frame_hash(dataset), universe_id=frame_hash(membership),
        label='EXCESS_RET5', label_id='EXCESS_RET5:next_open:5', benchmark_id=frame_hash(benchmark),
        holding_days=5, source_kind='SYNTHETIC')
    for objective in OBJECTIVES:
        targets, executions, portfolio_evals = {}, {}, []
        summaries[objective] = {}
        for model, walk in walks.items():
            directory = output / objective / model
            directory.mkdir(parents=True)
            target, decisions, ranks = prediction_targets(walk.predictions, returns, objective=objective)
            first, last = walk.predictions.date.min(), walk.predictions.date.max()
            execution_dates = dates[(dates >= first) & (dates <= dates[dates.get_loc(last) + 1])]
            execution_config = ExecutionBacktestConfig(initial_cash=100000, minimum_cross_section=5)
            executed = AShareExecutionBacktester(execution_config).run_target_weights(target,
                bars.loc[bars.date.isin(execution_dates)].merge(status, on=['date', 'symbol'], validate='one_to_one'),
                benchmark.loc[benchmark.date.isin(execution_dates)])
            folds = walk.fold_summary
            receipts = pd.DataFrame(dict(fold=folds.fold, train_end=folds.train_window_end_exclusive,
                validation_start=folds.validation_window_start, validation_end=folds.validation_window_end_exclusive,
                test_start=folds.test_window_start, test_end=folds.test_window_end_exclusive,
                label_end=folds.train_max_exit_date))
            pred_eval = ResearchEvaluation(EvaluationContext(calendar=tuple(pd.DatetimeIndex(
                sorted(walk.predictions.date.unique())).strftime('%Y-%m-%d')), **context)).walk_forward(
                model, walk, folds=receipts, top_k=10, minimum_cross_section=5)
            port_eval = ResearchEvaluation(EvaluationContext(calendar=tuple(execution_dates[1:].strftime('%Y-%m-%d')),
                **context)).portfolio(model, executed.nav, trades=executed.trades, positions=executed.positions)
            portfolio_evals.append(port_eval)
            if objective == 'equal':
                predictions_eval.append(pred_eval)
            artifacts = {}
            for name, frame in dict(predictions=walk.predictions, fit_audit=walk.diagnostic_audit,
                    folds=folds, rankings=ranks, decisions=decisions, targets=target,
                    trades=executed.trades, rejections=executed.rejections, positions=executed.positions,
                    nav=executed.nav, target_comparison=executed.target_comparison).items():
                path = directory / (name + '.csv')
                frame.to_csv(path, index=False)
                artifacts[name] = dict(path=str(path.resolve()), sha256=file_hash(path))
            metrics = dict(rank_ic=pred_eval.metrics['prediction_rank_ic_mean'].value,
                final_nav=float(executed.nav.iloc[-1].strategy_nav), trades=len(executed.trades),
                rejections=len(executed.rejections), costs=float(executed.trades.transaction_cost.sum()),
                turnover=float(executed.nav.turnover.sum()), prediction_identity=frame_hash(walk.predictions),
                target_identity=frame_hash(target))
            summaries[objective][model] = metrics
            (directory / 'evaluation.json').write_text(json.dumps(dict(prediction=pred_eval.to_dict(),
                portfolio=port_eval.to_dict()), indent=2, allow_nan=False), encoding='utf-8')
            identifier = 'p309-' + objective + '-' + model
            tracker.create_experiment(identifier, dict(model=model, parameters=dict(model=options[model], objective=objective),
                factors=[asdict(FactorRegistryV2.with_builtins().freeze().get(name, '1').ref)
                    for name in ('MOM20', 'REV1')], dataset=dict(sha256=context['dataset_id']),
                label=dict(definition=context['label_id']),
                windows={name:[str(folds[name + '_start'].min().date()),
                    str(folds[name + '_end'].max().date())] for name in ('train', 'validation', 'test')},
                walk_forward=vars(splitter), costs=asdict(execution_config)))
            tracker.append_run(identifier, identifier, **provenance,
                command=['python', '-m', 'examples.research.signal_propagation_synthetic', '--output', str(output)],
                status='SUCCEEDED', qualification='SYNTHETIC', artifacts=artifacts,
                prediction_metrics={k:v.value for k,v in pred_eval.metrics.items()},
                portfolio_metrics={k:v.value for k,v in port_eval.metrics.items()},
                validation=dict(boundary=BOUNDARY))
            targets[model], executions[model] = target, executed
        compare_results(portfolio_evals).to_csv(output / (objective + '-evaluation.csv'), index=False)
        contrasts, rate = pairwise_transmission(targets, executions,
            predictions={model:walk.predictions for model, walk in walks.items()})
        contrasts.to_csv(output / (objective + '-contrasts.csv'), index=False)
        summaries[objective]['transmission'] = rate
    compare_results(predictions_eval).to_csv(output / 'prediction-comparison.csv', index=False)
    summary = dict(boundary=BOUNDARY, synthetic=True, results=summaries)
    (output / 'summary.json').write_text(json.dumps(summary, indent=2, allow_nan=False), encoding='utf-8')
    print(json.dumps(summary, indent=2, allow_nan=False))
    return summary


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=BOUNDARY)
    parser.add_argument('--output', required=True, type=Path)
    run(parser.parse_args().output)
