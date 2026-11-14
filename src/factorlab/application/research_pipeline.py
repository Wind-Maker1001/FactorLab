"""Thin, software-only research orchestration over the existing domain engines."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
from pathlib import Path
from typing import Mapping

import pandas as pd

from factorlab.backtest import AShareExecutionBacktester, ExecutionBacktestConfig
from factorlab.backtest.fees import FeeSchedule
from factorlab.backtest.capacity import ResearchExecutionScenario
from factorlab.data.hub import DataHub
from factorlab.dataset import DatasetBuilder
from factorlab.dataset.identity import file_hash, frame_hash
from factorlab.dataset.partitioned import ChunkedConfig
from factorlab.evaluation.research import EvaluationContext, ResearchEvaluation, compare_results
from factorlab.experiments import ExperimentTracker
from factorlab.experiments.provenance import capture_provenance
from factorlab.portfolio import OptimizationInput, OptimizerConfig, PortfolioOptimizer
from factorlab.portfolio.feedback import ExecutionFeedback, SaleRestriction
from factorlab.validation import ExpandingWalkForwardSplitter, WalkForwardRunner

BOUNDARY = 'NO LIVE MONEY / PIT_UNVERIFIED / HFQ_UNVERIFIED / FORMAL_ADMISSION_BLOCKED'


@dataclass(frozen=True)
class HubQuery:
    """One explicitly version-pinned source query; date bounds include warmup."""
    source: str
    version: str
    kind: str
    start: str
    end: str
    symbol: str | None = None


@dataclass(frozen=True)
class ResearchPipelineConfig:
    """Builder pins factors/labels/preprocessing; splitter pins all temporal folds.

    Queries must supply bars, benchmark, membership, status, listings and calendar.
    Each value is a tuple to permit per-security DataHub readers. Synthetic mode
    requires SYNTHETIC provenance on every response, not merely this setting.
    Score allocation uses nonnegative cross-sectional percentile ranks, not returns.
    """
    experiment_id: str
    output: Path
    queries: Mapping[str, tuple[HubQuery, ...]]
    builder: DatasetBuilder
    splitter: ExpandingWalkForwardSplitter
    models: Mapping[str, Mapping[str, object]]
    optimizer: OptimizerConfig
    execution: ExecutionBacktestConfig
    source_kind: str
    research_start: str
    research_end: str
    risk_lookback: int = 40
    rebalance_sessions: int = 5
    top_k: int = 10
    minimum_cross_section: int = 5
    chunked: ChunkedConfig = field(default_factory=ChunkedConfig)
    fees: FeeSchedule = field(default_factory=FeeSchedule.a_share_default)
    execution_mode: str = 'batch'
    sale_restrictions: tuple[SaleRestriction, ...] = ()
    research_scenario: ResearchExecutionScenario | None = None

    def __post_init__(self):
        if self.execution_mode not in {'batch', 'incremental_feedback'}:
            raise ValueError('execution_mode must be batch or incremental_feedback')
        if self.execution_mode == 'batch' and self.sale_restrictions:
            raise ValueError('sale_restrictions require incremental_feedback')


@dataclass(frozen=True)
class ResearchPipelineResult:
    dataset: object
    walk_forward: Mapping[str, object]
    targets: Mapping[str, pd.DataFrame]
    decisions: Mapping[str, pd.DataFrame]
    executions: Mapping[str, object]
    evaluations: Mapping[str, object]
    comparison: pd.DataFrame
    run_ids: tuple[str, ...]
    tracker: ExperimentTracker


def _json_value(value):
    return json.loads(json.dumps(value, default=str, allow_nan=False))


def _model_configuration(options):
    configuration = dict(options)
    if 'factor_selector' in configuration:
        selector = configuration['factor_selector']
        configuration['factor_selector'] = dict(rule=selector.RULE_VERSION,
            snapshot_hash=selector.snapshot.snapshot_hash,
            factor_refs=[asdict(ref) for ref in selector.factor_refs],
            config=asdict(selector.config))
    return _json_value(configuration)


def prediction_targets(predictions, returns, *, model_name, optimizer, lookback, every):
    """Adapt OOS scores to complete targets using only trailing close-known risk.

    No prior target is passed as actual holdings. Turnover-constrained optimization
    needs an incremental execution-state API and is deliberately not accepted here.
    """
    if optimizer.config.turnover_limit is not None:
        raise ValueError('turnover_limit requires actual incremental execution holdings')
    if optimizer.config.objective == 'mean_variance':
        raise ValueError('mean_variance requires separately PIT-qualified expected returns; scores are not returns')
    if lookback < 2 or every < 1:
        raise ValueError('risk lookback >= 2 and rebalance interval >= 1 required')
    targets, receipts = [], []
    for date, group in list(predictions.groupby('date', sort=True))[::every]:
        group = group.sort_values('symbol')
        symbols = tuple(group.symbol)
        risk = returns.loc[returns.index <= date, list(symbols)].tail(lookback)
        scores = group.set_index('symbol').score.rank(method='average', pct=True)
        optimized = optimizer.optimize(OptimizationInput(
            symbols=symbols, decision_time=date, returns=risk, scores=scores))
        target = optimized.target_weights.rename_axis('symbol').rename('target_weight').reset_index()
        target['date'] = date
        targets.append(target)
        receipts.append(dict(date=date, prediction_time=date, model_name=model_name,
            fold=int(group.fold.iloc[0]), symbols=json.dumps(symbols),
            prediction_identity=frame_hash(group[['date', 'symbol', 'fold', 'score']].reset_index(drop=True)),
            risk_first_date=risk.index.min(), risk_last_date=risk.index.max(),
            risk_rows=len(risk), input_identity=optimized.input_identity,
            config_identity=optimized.config_identity,
            solver_status=json.dumps(optimized.solver_status, sort_keys=True),
            target_weights=json.dumps(optimized.target_weights.to_dict(), sort_keys=True)))
    return pd.concat(targets, ignore_index=True), pd.DataFrame(receipts)


def feedback_execution(predictions, returns, *, model_name, optimizer, lookback, every,
                       engine, bars, benchmark, sale_restrictions=()):
    """Allocate at known local closes, advancing the existing ledger on every day."""
    if optimizer.config.objective == 'mean_variance':
        raise ValueError('mean_variance requires separately PIT-qualified expected returns; scores are not returns')
    if lookback < 2 or every < 1:
        raise ValueError('risk lookback >= 2 and rebalance interval >= 1 required')
    groups = dict(list(predictions.groupby('date', sort=True))[::every])
    session = engine.start_session(bars, benchmark, signal_date=predictions.date.min())
    feedback = ExecutionFeedback(session, optimizer)
    targets, receipts = [], []
    for day in pd.DatetimeIndex(benchmark.date).sort_values():
        if day < predictions.date.min():
            continue
        if day != session.snapshot.as_of.normalize():
            feedback.advance()
        if day not in groups:
            continue
        group = groups[day].sort_values('symbol')
        symbols = tuple(group.symbol)
        risk = returns.loc[returns.index <= day, list(symbols)].tail(lookback)
        scores = group.set_index('symbol').score.rank(method='average', pct=True)
        decision = feedback.optimize(OptimizationInput(symbols=symbols,
            decision_time=session.snapshot.as_of, returns=risk, scores=scores),
            signal_time=day + pd.Timedelta(hours=15),
            sale_restrictions=tuple(r for r in sale_restrictions
                if pd.Timestamp(r.known_at) <= session.snapshot.as_of
                and pd.Timestamp(r.through) >= session.next_execution_date))
        optimized, snapshot = decision.optimization, decision.snapshot
        target = optimized.target_weights.rename_axis('symbol').rename('target_weight').reset_index()
        target['date'] = day
        targets.append(target)
        receipts.append(dict(date=day, prediction_time=day, model_name=model_name,
            fold=int(group.fold.iloc[0]), symbols=json.dumps(symbols),
            prediction_identity=frame_hash(group[['date', 'symbol', 'fold', 'score']].reset_index(drop=True)),
            risk_first_date=risk.index.min(), risk_last_date=risk.index.max(), risk_rows=len(risk),
            input_identity=optimized.input_identity, config_identity=optimized.config_identity,
            solver_status=json.dumps(optimized.solver_status, sort_keys=True),
            target_weights=json.dumps(optimized.target_weights.to_dict(), sort_keys=True),
            decision_time=decision.decision_time, execution_time=decision.execution_time,
            actual_weights=json.dumps(snapshot.security_weights.to_dict(), sort_keys=True),
            previous_targets=json.dumps(dict(snapshot.last_targets), sort_keys=True),
            cash=snapshot.cash, nav=snapshot.nav, cash_weight=snapshot.cash_weight,
            estimated_gross_security_turnover=optimized.turnover))
    return pd.concat(targets, ignore_index=True), pd.DataFrame(receipts), session.result


class ResearchPipeline:
    """Acquire, build, fit, allocate, execute, evaluate and record without new engines."""

    def __init__(self, config: ResearchPipelineConfig):
        self.config = config

    def run(self, hub: DataHub, *, tracker: ExperimentTracker, repo_root: Path,
            command: list[str], qualification_provider=None) -> ResearchPipelineResult:
        c = self.config
        if c.source_kind not in {'SYNTHETIC', 'REAL_CANDIDATE'}:
            raise ValueError('explicit SYNTHETIC or REAL_CANDIDATE source required')
        if c.builder.snapshot is None:
            raise ValueError('pipeline requires a pinned FactorRegistry V2 builder')
        if set(c.queries) != {'bars', 'benchmark', 'membership', 'status', 'listings', 'calendar'}:
            raise ValueError('six explicit input query groups required')
        if not c.models:
            raise ValueError('at least one predeclared model required')
        tables, sources = {}, {}
        for name, queries in c.queries.items():
            values, metadata = [], []
            for query in queries:
                response = hub.read(query.source, query.kind, start=query.start,
                    end=query.end, symbol=query.symbol)
                if response.metadata.data_version != query.version:
                    raise ValueError('DataHub version differs from pinned query')
                if c.source_kind == 'SYNTHETIC' and response.metadata.provenance.get('source_kind') != 'SYNTHETIC':
                    raise ValueError('synthetic mode requires explicitly synthetic provider provenance')
                values.append(response.data)
                metadata.append(asdict(response.metadata))
            if name == 'calendar':
                if len(values) != 1:
                    raise ValueError('one authoritative calendar required')
                tables[name] = pd.DatetimeIndex(values[0])
            else:
                tables[name] = pd.concat(values, ignore_index=True)
            sources[name] = metadata
        calendar = tables['calendar']
        research_dates = calendar[(calendar >= c.research_start) & (calendar <= c.research_end)]
        if research_dates.empty or research_dates[-1] >= calendar[-1]:
            raise ValueError('research interval requires a subsequent execution/label calendar')
        membership = tables['membership']
        membership = membership.loc[membership.date.isin(research_dates)].copy()
        tables['membership'] = membership
        # REAL_CANDIDATE remains exploratory; formal admission belongs to formal consumers.
        if c.builder.data_version != c.queries['bars'][0].version:
            raise ValueError('builder data version must bind the selected bars version')
        output = Path(c.output)
        output.mkdir(parents=True, exist_ok=True)
        dataset = c.builder.build_chunked(tables['bars'], tables['benchmark'], membership,
            tables['status'], tables['listings'], calendar,
            output_dir=output / 'dataset', config=c.chunked)
        closes = tables['bars'].pivot(index='date', columns='symbol', values='raw_close').sort_index()
        returns = closes.pct_change(fill_method=None).iloc[1:]
        provenance = capture_provenance(repo_root)
        runs, walks, targets, decisions, executions, evaluations = [], {}, {}, {}, {}, {}
        prediction_evaluations = []
        for model, options in c.models.items():
            directory = output / model
            directory.mkdir()
            runner_options = dict(options)
            runner_options.setdefault('target_col', 'EXCESS_RET%d' % c.builder.options.holding_days)
            runner = WalkForwardRunner(c.splitter, model_name=model,
                feature_names=c.builder.factor_names, top_k=c.top_k,
                minimum_cross_section=c.minimum_cross_section, **runner_options)
            walk = runner.run_partitioned(dataset, calendar=research_dates)
            model_identity = frame_hash(walk.diagnostic_audit)
            fit_identities = {fold: frame_hash(group[['model_name', 'model_parameters',
                'feature_names', 'fit_source_sha256']].drop_duplicates().reset_index(drop=True))
                for fold, group in walk.diagnostic_audit.groupby('fold')}
            first, last = walk.predictions.date.min(), walk.predictions.date.max()
            execution_dates = calendar[(calendar >= first) & (calendar <= calendar[calendar.get_loc(last) + 1])]
            bars = tables['bars'].loc[tables['bars'].date.isin(execution_dates)].merge(
                tables['status'], on=['date', 'symbol'], validate='one_to_one')
            benchmark = tables['benchmark'].loc[tables['benchmark'].date.isin(execution_dates)]
            engine = AShareExecutionBacktester(c.execution, fee_schedule=c.fees,
                research_scenario=c.research_scenario)
            allocation = dict(model_name=model, optimizer=PortfolioOptimizer(c.optimizer),
                lookback=c.risk_lookback, every=c.rebalance_sessions)
            if c.execution_mode == 'incremental_feedback':
                target, decision, executed = feedback_execution(walk.predictions, returns,
                    engine=engine, bars=bars, benchmark=benchmark,
                    sale_restrictions=c.sale_restrictions, **allocation)
            else:
                target, decision = prediction_targets(walk.predictions, returns, **allocation)
                executed = engine.run_target_weights(target, bars, benchmark)
            decision['model_identity'] = decision.fold.map(fit_identities)
            context = dict(dataset_id=dataset.identity_hash,
                universe_id=frame_hash(membership), label=runner.target_col,
                label_id=json.dumps(dict(target=runner.target_col, holding_days=c.builder.options.holding_days,
                    entry_price=c.builder.options.entry_price, implementation='factorlab.labels.forward_return'), sort_keys=True),
                benchmark_id=frame_hash(tables['benchmark']), holding_days=c.builder.options.holding_days,
                source_kind=c.source_kind)
            folds = walk.fold_summary.rename(columns={
                'train_window_end_exclusive': 'train_end_bound',
                'validation_window_start': 'validation_start_bound',
                'validation_window_end_exclusive': 'validation_end_bound',
                'test_window_start': 'test_start_bound', 'test_window_end_exclusive': 'test_end_bound'})
            receipts = pd.DataFrame(dict(fold=folds.fold, train_end=folds.train_end_bound,
                validation_start=folds.validation_start_bound, validation_end=folds.validation_end_bound,
                test_start=folds.test_start_bound, test_end=folds.test_end_bound,
                label_end=folds.train_max_exit_date))
            pred_calendar = tuple(pd.DatetimeIndex(sorted(walk.predictions.date.unique())).strftime('%Y-%m-%d'))
            pred_eval = ResearchEvaluation(EvaluationContext(calendar=pred_calendar, **context)).walk_forward(
                model, walk, folds=receipts, top_k=c.top_k, minimum_cross_section=c.minimum_cross_section)
            port_eval = ResearchEvaluation(EvaluationContext(
                calendar=tuple(execution_dates[1:].strftime('%Y-%m-%d')), **context)).portfolio(
                    model, executed.nav, trades=executed.trades, positions=executed.positions)
            artifacts = {}
            for name, frame in dict(decisions=decision, targets=target, folds=walk.fold_summary,
                    selection=walk.factor_selection, fit_audit=walk.diagnostic_audit,
                    trades=executed.trades, positions=executed.positions, nav=executed.nav,
                    rejections=executed.rejections, target_comparison=executed.target_comparison).items():
                path = directory / (name + '.csv')
                frame.to_csv(path, index=False)
                artifacts[name] = dict(path=str(path.resolve()), sha256=file_hash(path))
            report = directory / 'evaluation.json'
            report.write_text(json.dumps(dict(prediction=pred_eval.to_dict(), portfolio=port_eval.to_dict()),
                indent=2, allow_nan=False) + '\n', encoding='utf-8')
            artifacts['report'] = dict(path=str(report.resolve()), sha256=file_hash(report))
            experiment_id = c.experiment_id + '-' + model
            if c.execution_mode != 'batch':
                experiment_id += '-' + c.execution_mode
            definition = dict(model=model, parameters=_json_value(dict(model_options=_model_configuration(options),
                execution_mode=c.execution_mode,
                sale_restrictions=_json_value([asdict(r) for r in c.sale_restrictions]),
                execution_sources={name: file_hash(Path(__file__).resolve().parents[1] / name)
                    for name in ('application/research_pipeline.py', 'portfolio/feedback.py',
                                 'portfolio/optimizer.py', 'backtest/session.py', 'backtest/execution.py')},
                portfolio=asdict(c.optimizer), risk_lookback=c.risk_lookback,
                rebalance_sessions=c.rebalance_sessions, execution=asdict(c.execution))),
                factors=dataset.manifest['binding']['factors'],
                dataset=dict(sha256=dataset.identity_hash, sources=sources),
                label=dict(definition=context['label_id']),
                windows={name: [str(walk.fold_summary[name + '_start'].min().date()),
                    str(walk.fold_summary[name + '_end'].max().date())] for name in ('train', 'validation', 'test')},
                walk_forward=_json_value(vars(c.splitter)),
                costs=_json_value(dict(execution=asdict(c.execution),
                    fee_rules=[asdict(rule) for rule in c.fees.rules],
                    **({} if c.research_scenario is None else dict(
                        research_scenario=asdict(c.research_scenario),
                        research_source=file_hash(Path(__file__).resolve().parents[1] / 'backtest/capacity.py'))))))
            tracker.create_experiment(experiment_id, definition)
            tracker.append_run(experiment_id, experiment_id, **provenance, command=command,
                status='SUCCEEDED', qualification=c.source_kind,
                prediction_metrics={k: v.value for k, v in pred_eval.metrics.items()},
                portfolio_metrics={k: v.value for k, v in port_eval.metrics.items()}, artifacts=artifacts,
                validation=dict(sample_hash=pred_eval.receipts['observations'],
                    evaluation=dict(context=asdict(pred_eval.context), costs=definition['costs']),
                    boundary=BOUNDARY, model_identity=model_identity,
                    prediction_receipts=dict(pred_eval.receipts),
                    portfolio_receipts=dict(port_eval.receipts)))
            runs.append(experiment_id)
            walks[model], targets[model], decisions[model] = walk, target, decision
            executions[model], evaluations[model] = executed, (pred_eval, port_eval)
            prediction_evaluations.append(pred_eval)
        comparison = (compare_results(prediction_evaluations) if len(runs) > 1 else pd.DataFrame())
        if len(runs) > 1:
            tracker.compare(runs)
            compare_results([pair[1] for pair in evaluations.values()])
        comparison.to_csv(output / 'comparison.csv', index=False)
        return ResearchPipelineResult(dataset, walks, targets, decisions, executions,
            evaluations, comparison, tuple(runs), tracker)
