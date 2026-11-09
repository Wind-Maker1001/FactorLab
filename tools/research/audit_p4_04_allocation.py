"""Replay one fixed OOS fit through existing allocation, execution and inference APIs."""
import argparse
from dataclasses import asdict, replace
from itertools import combinations
import json
from pathlib import Path
from types import SimpleNamespace
import shutil
import time

import numpy as np
import pandas as pd

from examples.research.alpha_robustness_synthetic import OPTIONS, fold_receipts, save
from factorlab.application.alpha_robustness import calendar_paired_inference, cost_accounting, random_scores
from factorlab.application.research_pipeline import feedback_execution, prediction_targets
from factorlab.backtest import AShareExecutionBacktester
from factorlab.dataset.identity import file_hash, frame_hash
from factorlab.dataset.partitioned import PartitionedDataset
from factorlab.evaluation.research import EvaluationContext, EvaluationResult, MetricValue, ResearchEvaluation, paired_inference
from factorlab.portfolio import PortfolioOptimizer
from tools.research.pipeline_config import configure, identity, load_config
from tools.research.run_pipeline import BOUNDARY, EVIDENCE, ROOT, run as fit_once

OBJECTIVES = ('minimum_variance', 'equal', 'score')
BASELINE = 'factor_score/equal'
KEYS = ['date', 'symbol', 'fold', 'EXCESS_RET5']


def ranking(frame):
    """Retain average ties; stable symbol order is presentation, not a tie-break."""
    result = frame[KEYS[:3]].copy()
    result['rank'] = frame.groupby('date').score.rank(method='average', ascending=False)
    return result.sort_values(['date', 'rank', 'symbol']).reset_index(drop=True)


def rank_distance(left, right):
    joined = left.merge(right, on=KEYS[:3], validate='one_to_one', suffixes=('_left', '_right'))
    rows = []
    for date, group in joined.groupby('date', sort=True):
        difference = (group.rank_left - group.rank_right).abs()
        rows.append(dict(date=date, securities=len(group),
            spearman=group.rank_left.corr(group.rank_right),
            changed_ranks=int((difference > 0).sum()), mean_absolute_rank_distance=float(difference.mean()),
            max_absolute_rank_distance=float(difference.max())))
    return pd.DataFrame(rows)


def weight_distance(left, right):
    """L1 security-NAV distance; cash excluded, union support retained."""
    joined = left.merge(right, on=['date', 'symbol'], how='outer', validate='one_to_one',
                        suffixes=('_left', '_right')).fillna(0.)
    joined['distance'] = (joined.target_weight_left - joined.target_weight_right).abs()
    joined['support_change'] = ((joined.target_weight_left > 1e-8) !=
                                (joined.target_weight_right > 1e-8)).astype(int)
    return joined.groupby('date', sort=True).agg(l1=('distance', 'sum'),
        changed_support=('support_change', 'sum')).reset_index()


def compare_feedback(batch_target, batch, target, incremental):
    pd.testing.assert_frame_equal(batch_target.sort_index(axis=1), target.sort_index(axis=1),
                                  check_exact=False, atol=1e-10, rtol=1e-10)
    fields = {}
    for name in ('trades', 'positions', 'nav', 'rejections', 'target_comparison'):
        left, right = getattr(batch, name), getattr(incremental, name)
        columns = sorted((set(left.columns) & set(right.columns)) - {'optimizer_turnover', 'gross_security_turnover'})
        pd.testing.assert_frame_equal(left[columns].reset_index(drop=True), right[columns].reset_index(drop=True),
                                      check_exact=False, atol=1e-8, rtol=1e-10)
        fields[name] = dict(rows=len(left), compared_columns=columns,
                           feedback_only_columns=sorted(set(right.columns) - set(left.columns)),
                           feedback_telemetry_columns=sorted(set(right.columns) & {'optimizer_turnover', 'gross_security_turnover'}))
    return fields


def execution_summary(executed, evaluated, config):
    comparison = executed.target_comparison
    deviation = comparison.assign(absolute_deviation=comparison.weight_deviation.abs()).groupby('execution_date').absolute_deviation.sum()
    residual = cost_accounting(executed.nav, executed.trades,
                              initial_cash=config.execution.initial_cash, schedule=config.fees)
    return dict(net_return=float(executed.nav.iloc[-1].strategy_nav - 1),
        costs_cny=float(executed.trades.transaction_cost.sum()), trades=len(executed.trades),
        rejections=len(executed.rejections), rejection_reasons=executed.rejections.reason.value_counts().to_dict(),
        turnover=float(executed.nav.turnover.sum()),
        mean_execution_weight_l1=float(deviation.mean()), max_execution_weight_l1=float(deviation.max()),
        share_shortfall_rows=int((comparison.actual_shares != comparison.target_shares).sum()),
        shortfall_reason_contract='Explicit rejection reasons only; other lot/cash/valuation effects not relabelled as rejections.',
        cash_reconciliation_max_residual_cny=residual,
        metrics={key: asdict(value) for key, value in evaluated.metrics.items()})


def saved_evaluation(document):
    tables = {name: pd.DataFrame(table['data']) for name, table in document['tables'].items()}
    tables['daily']['date'] = pd.to_datetime(tables['daily']['date'])
    return EvaluationResult(document['name'], document['kind'], EvaluationContext(**document['context']),
        {key: MetricValue(**value) for key, value in document['metrics'].items()},
        tables, document['receipts'], document['metadata'])


def replay_saved_ranks(output):
    """Use exact sufficient allocation statistics; never reconstruct score magnitudes."""
    from tools.research.run_pipeline import verify
    verify(output / 'training')
    dataset = PartitionedDataset(output / 'training/dataset')
    labels = dataset.read_metadata(['date', 'symbol', 'EXCESS_RET5'])
    walks, evaluations, targets, decisions, executions = {}, {}, {}, {}, {}
    for name in ('factor_score', 'ridge', 'lightgbm', 'xgboost'):
        directory = output / 'training' / name
        ranks = pd.read_csv(output / (name + '-ranks.csv'), parse_dates=['date'])
        frame = ranks.merge(labels, on=['date', 'symbol'], validate='one_to_one')
        frame['score'] = -frame.pop('rank')
        frame = frame.sort_values(['date', 'symbol']).reset_index(drop=True)
        walks[name] = SimpleNamespace(predictions=frame,
            fold_summary=pd.read_csv(directory / 'folds.csv'))
        documents = json.loads((directory / 'evaluation.json').read_text(encoding='utf-8'))
        evaluations[name] = tuple(saved_evaluation(documents[kind]) for kind in ('prediction', 'portfolio'))
        targets[name] = pd.read_csv(directory / 'targets.csv', parse_dates=['date'], float_precision='round_trip')
        decisions[name] = pd.read_csv(directory / 'decisions.csv', parse_dates=['date'])
        executed = {}
        for field in ('trades', 'positions', 'nav', 'rejections', 'target_comparison'):
            table = pd.read_csv(directory / (field + '.csv'), float_precision='round_trip')
            for column in ('date', 'signal_date', 'execution_date'):
                if column in table:
                    table[column] = pd.to_datetime(table[column])
            executed[field] = table
        executions[name] = SimpleNamespace(**executed)
    return SimpleNamespace(dataset=dataset, walk_forward=walks, evaluations=evaluations,
                           targets=targets, decisions=decisions, executions=executions)


def run(output, *, resume_ranks=False):
    started = time.perf_counter()
    output = Path(output).resolve()
    if resume_ranks:
        document = load_config(output / 'config.json')
        trained = replay_saved_ranks(output)
    else:
        output.mkdir(parents=True, exist_ok=False)
        protocol = ROOT / 'docs/validation/p4-04/PROTOCOL.md'
        shutil.copyfile(protocol, output / 'PROTOCOL.md')
        document = load_config(ROOT / 'configs/research/p4-02-synthetic.json')
        document['experiment_id'] = 'p4-04-synthetic'
        save(output / 'config.json', document)
        trained = fit_once(output / 'config.json', output / 'training')
    config, _, inputs = configure(document, output / 'unused-configuration')
    # The fixed provider is the same public fixture used by configure; no fitting.
    from examples.research.research_pipeline_synthetic import configured
    _, _, provider = configured(output / 'unused-provider')
    tables = {name: provider.tables[kind] for name, kind in dict(bars='daily_bars',
        benchmark='index_bars', status='security_status', calendar='calendar').items()}
    calendar = pd.DatetimeIndex(tables['calendar'])
    arms = {name: walk.predictions for name, walk in trained.walk_forward.items()}
    for seed in (17, 29, 43):
        arms[f'random_{seed}'] = random_scores(arms['factor_score'], seed=seed)
    reference = arms['factor_score']
    sample = reference[KEYS].sort_values(KEYS[:3]).reset_index(drop=True)
    for frame in arms.values():
        pd.testing.assert_frame_equal(sample, frame[KEYS].sort_values(KEYS[:3]).reset_index(drop=True))
    dates = calendar[(calendar >= reference.date.min()) &
        (calendar <= calendar[calendar.get_loc(reference.date.max()) + 1])]
    bars = tables['bars'].loc[tables['bars'].date.isin(dates)].merge(tables['status'],
        on=['date', 'symbol'], validate='one_to_one')
    benchmark = tables['benchmark'].loc[tables['benchmark'].date.isin(dates)]
    returns = tables['bars'].pivot(index='date', columns='symbol', values='raw_close').sort_index().pct_change(fill_method=None).iloc[1:]
    context = trained.evaluations['factor_score'][0].context
    portfolio_context = trained.evaluations['factor_score'][1].context
    evaluator = ResearchEvaluation(context)
    port_evaluator = ResearchEvaluation(portfolio_context)
    folds = fold_receipts(trained.walk_forward['factor_score'])
    rank_frames, predictions, pred_summary = {}, [], {}
    for name, frame in arms.items():
        rank_frames[name] = ranking(frame)
        if resume_ranks:
            saved = pd.read_csv(output / (name + '-ranks.csv'), parse_dates=['date'])
            pd.testing.assert_frame_equal(rank_frames[name], saved)
        else:
            rank_frames[name].to_csv(output / (name + '-ranks.csv'), index=False)
        evaluated = (trained.evaluations[name][0] if name in trained.evaluations else
            evaluator.predictions(name, frame, folds=folds, top_k=config.top_k,
                minimum_cross_section=config.minimum_cross_section))
        predictions.append(evaluated)
        pred_summary[name] = dict(rows=len(frame), dates=frame.date.nunique(),
            sample_identity=frame_hash(sample),
            score_representation=('NEGATIVE_SAVED_AVERAGE_RANK' if resume_ranks and name in trained.walk_forward else 'ORIGINAL_SCORE'),
            allocation_score_identity=frame_hash(frame[['date', 'symbol', 'fold', 'score']]),
            ranks_identity=frame_hash(rank_frames[name]),
            raw_score_statistics=(None if resume_ranks and name in trained.walk_forward else
                dict(min=float(frame.score.min()), max=float(frame.score.max()),
                     mean=float(frame.score.mean()), std=float(frame.score.std()))),
            metrics={key: asdict(value) for key, value in evaluated.metrics.items()})
    rank_pairs = {}
    for left, right in combinations(arms, 2):
        distance = rank_distance(rank_frames[left], rank_frames[right])
        rank_pairs[left + ':' + right] = dict(mean_spearman=float(distance.spearman.mean()),
            changed_dates=int((distance.changed_ranks > 0).sum()), daily=distance.to_dict('records'))
    targets, executions, portfolios, summaries, feedback = {}, {}, [], {}, {}
    for name, frame in arms.items():
        for objective in OBJECTIVES:
            arm = name + '/' + objective
            directory = output / name / objective
            directory.mkdir(parents=True, exist_ok=resume_ranks)
            parameters = dict(model_name=name, optimizer=PortfolioOptimizer(replace(config.optimizer, objective=objective)),
                              lookback=config.risk_lookback, every=config.rebalance_sessions)
            if name in trained.walk_forward and objective == 'minimum_variance':
                target, decision, executed = trained.targets[name], trained.decisions[name], trained.executions[name]
                if resume_ranks:
                    replayed, _ = prediction_targets(frame, returns, **parameters)
                    pd.testing.assert_frame_equal(target.sort_index(axis=1), replayed.sort_index(axis=1),
                        check_exact=False, atol=1e-12, rtol=1e-12)
            else:
                target, decision = prediction_targets(frame, returns, **parameters)
                executed = AShareExecutionBacktester(config.execution, fee_schedule=config.fees).run_target_weights(target, bars, benchmark)
            evaluated = port_evaluator.portfolio(arm, executed.nav, trades=executed.trades, positions=executed.positions)
            targets[arm], executions[arm] = target, executed
            portfolios.append(evaluated)
            summaries[arm] = execution_summary(executed, evaluated, config)
            save(directory / 'evaluation.json', evaluated.to_dict())
            for field, table in dict(targets=target, decisions=decision, trades=executed.trades,
                positions=executed.positions, nav=executed.nav, rejections=executed.rejections,
                target_comparison=executed.target_comparison).items():
                table.to_csv(directory / (field + '.csv'), index=False)
            if name in trained.walk_forward:
                ft, fd, fe = feedback_execution(frame, returns,
                    engine=AShareExecutionBacktester(config.execution, fee_schedule=config.fees),
                    bars=bars, benchmark=benchmark, **parameters)
                feedback[arm] = compare_feedback(target, executed, ft, fe)
                fd.to_csv(directory / 'feedback-decisions.csv', index=False)
    baseline_return = summaries[BASELINE]['net_return']
    distances = {}
    for arm, target in targets.items():
        distance = weight_distance(target, targets[BASELINE])
        summaries[arm]['net_return_minus_equal'] = summaries[arm]['net_return'] - baseline_return
        summaries[arm]['mean_target_l1_minus_equal'] = float(distance.l1.mean())
        summaries[arm]['max_target_l1_minus_equal'] = float(distance.l1.max())
        distances[arm] = distance.to_dict('records')
    model_pairs = {}
    for objective in OBJECTIVES:
        for left, right in combinations(trained.walk_forward, 2):
            a, b = left + '/' + objective, right + '/' + objective
            distance = weight_distance(targets[a], targets[b])
            pa = executions[a].positions.rename(columns={'weight': 'target_weight'})[['date', 'symbol', 'target_weight']]
            pb = executions[b].positions.rename(columns={'weight': 'target_weight'})[['date', 'symbol', 'target_weight']]
            actual = weight_distance(pa, pb)
            model_pairs[a + ':' + b] = dict(targets=distance.to_dict('records'),
                holdings=actual.to_dict('records'), max_target_l1=float(distance.l1.max()),
                max_actual_l1=float(actual.l1.max()),
                trades_identical=executions[a].trades.equals(executions[b].trades),
                nav_identical=executions[a].nav.equals(executions[b].nav))
    attribution = {}
    for name in arms:
        score, minimum = name + '/score', name + '/minimum_variance'
        distance = weight_distance(targets[score], targets[minimum])
        net = summaries[score]['net_return'] - summaries[minimum]['net_return']
        cost = -(summaries[score]['costs_cny'] - summaries[minimum]['costs_cny']) / config.execution.initial_cash
        attribution[name] = dict(daily_target_difference=distance.to_dict('records'),
            target_support_changed_dates=int((distance.changed_support > 0).sum()),
            mean_target_weight_l1=float(distance.l1.mean()), net_return_difference=net,
            fixed_fill_terminal_cost_contribution=cost, unexplained=net-cost,
            residual_status='UNEXPLAINED',
            interpretation='Terminal accounting only, not a zero-cost counterfactual or causal decomposition.')
    prediction_tests = calendar_paired_inference(predictions, calendar=dates[:-1],
        baseline='factor_score', metric='prediction_rank_ic', options=OPTIONS)
    return_tests = paired_inference(portfolios, baseline=BASELINE, metric='strategy_return', options=OPTIONS)
    # Gap-day positions and NAV remain in the same continuous execution ledger.
    absent_dates = dates[~dates.isin(pd.DatetimeIndex(reference.date.unique()))]
    gaps = {arm: dict(no_prediction_sessions=len(absent_dates),
        nav_rows_without_predictions=int(execution.nav.date.isin(absent_dates).sum()),
        held_rows_without_predictions=int(execution.positions.date.isin(absent_dates).sum()))
        for arm, execution in executions.items()}
    result = dict(evidence=EVIDENCE, boundary=BOUNDARY, config_sha256=identity(document),
        protocol_sha256=file_hash(output / 'PROTOCOL.md'), dataset_identity=trained.dataset.identity_hash,
        data_identity=inputs, sample_identity=frame_hash(sample),
        sample=dict(rows=len(sample), prediction_dates=reference.date.nunique(),
            prediction_first=reference.date.min(), prediction_last=reference.date.max(),
            execution_dates=list(dates), inference_return_days=len(dates)-1,
            cross_section_sizes=reference.groupby('date').size().to_dict()),
        training=dict(four_model_pipeline_runs=1, additional_fits=0,
            resumed_from_saved_ranks=resume_ranks,
            reuse='All 21 allocation arms share the sole run OOS rankings; original model IC evaluations retained.',
            raw_score_magnitude_status='NOT_RETAINED' if resume_ranks else 'SUMMARIZED'),
        predictions=pred_summary, rank_pairs=rank_pairs, portfolios=summaries,
        target_distance_to_equal=distances, model_pairs=model_pairs, attribution=attribution,
        feedback_equivalence=feedback, gaps=gaps, inference_options=asdict(OPTIONS),
        prediction_inference=prediction_tests.to_dict('records'), portfolio_inference=return_tests.to_dict('records'),
        prior_controls=dict(source='docs/validation/p3-12/results.json',
            sha256=file_hash(ROOT / 'docs/validation/p3-12/results.json'),
            scope='Train-shuffled Ridge and lag5 controls are prior evidence, not pooled with this run.'),
        wall_seconds=time.perf_counter()-started)
    save(output / 'results.json', result)
    files = {str(path.relative_to(output)): file_hash(path) for path in output.rglob('*')
        if path.is_file() and 'dataset' not in path.relative_to(output).parts and path.suffix != '.sqlite3'}
    save(output / 'receipt.json', dict(evidence=EVIDENCE, boundary=BOUNDARY,
        baseline='4b1baad1f661be5728cfb79ee9a4db5bc1b9f32f', output=str(output),
        source_files={str(path.relative_to(ROOT)): file_hash(path) for path in
            (Path(__file__), ROOT / 'docs/validation/p4-04/PROTOCOL.md', ROOT / 'configs/research/p4-02-synthetic.json')},
        files=files, reproduction=['python', '-m', 'tools.research.audit_p4_04_allocation', '--output', '<fresh-output>']))
    print('P4-04 completed:', output)
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=EVIDENCE + ' / ' + BOUNDARY)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--resume-ranks', action='store_true',
        help='Replay saved sufficient allocation ranks and original evaluations without fitting.')
    args = parser.parse_args()
    run(args.output, resume_ranks=args.resume_ranks)
