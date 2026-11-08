"""Reproducible synthetic integration, never historical financial evidence."""
import argparse
from dataclasses import replace
import json
from pathlib import Path
import time

import numpy as np
import pandas as pd

from factorlab.application.research_pipeline import (
    BOUNDARY, HubQuery, ResearchPipeline, ResearchPipelineConfig,
)
from factorlab.backtest import ExecutionBacktestConfig
from factorlab.data.hub import DataHub
from factorlab.data.hub.contracts import QueryResult, SourceMetadata
from factorlab.dataset import DatasetBuilder, DatasetBuildOptions
from factorlab.experiments import ExperimentTracker
from factorlab.exploratory.factor_discovery.synthetic import make_synthetic_data
from factorlab.factors.registry_v2 import FactorRegistryV2, FactorDataContext
from factorlab.portfolio import OptimizerConfig
from factorlab.universe.filters import UniverseFilterConfig
from factorlab.validation import ExpandingWalkForwardSplitter
from factorlab.validation.factor_selection import TrainFactorSelector, FactorSelectionConfig


class SyntheticProvider:
    """Explicit in-memory synthetic provider; no historical availability claim."""
    capabilities = frozenset({'daily_bars', 'index_bars', 'membership', 'security_status', 'listings', 'calendar'})

    def __init__(self, tables):
        self.tables = tables

    def read(self, kind, *, symbol, security_id, start, end, knowledge_as_of):
        if knowledge_as_of is not None:
            raise ValueError('synthetic provider has no vendor knowledge history')
        value = self.tables[kind]
        if kind == 'calendar':
            value = value[(value >= start) & (value <= end)]
        else:
            value = value.loc[value.symbol.eq(symbol)].copy()
            if 'date' in value:
                value = value.loc[value.date.between(start, end)]
        return QueryResult(value, SourceMetadata('synthetic-p307b', 'seed-20261014-v1',
            calendar_id='synthetic-bday-v1', calendar_kind='SYNTHETIC',
            provenance={'source_kind': 'SYNTHETIC', 'generator': 'make_synthetic_data'}))


def configured(output, *, dynamic=True, models=None):
    bars, benchmark, dates = make_synthetic_data(stock_count=27, sessions=270)
    # Exclude the generator's deliberately gapped securities; no price imputation.
    bars = bars.loc[~bars.symbol.isin(['000002.SZ', '000003.SZ', '000004.SZ'])].copy()
    symbols = tuple(sorted(bars.symbol.unique()))
    benchmark['adj_close'] = benchmark.adj_open
    grid = pd.MultiIndex.from_product([dates, symbols], names=['date', 'symbol']).to_frame(index=False)
    membership = grid.assign(is_member=True)
    status = grid.assign(trade_status=1, is_st=False)
    # An observable execution restriction leaves an unfilled target in the ledger.
    status.loc[status.symbol.eq(symbols[0]) & status.date.between(dates[145], dates[155]), 'trade_status'] = 0
    listings = pd.DataFrame(dict(symbol=symbols, listing_date=dates[0]))
    provider = SyntheticProvider(dict(daily_bars=bars, index_bars=benchmark,
        membership=membership, security_status=status, listings=listings, calendar=dates))
    snapshot = FactorRegistryV2.with_builtins().freeze()
    refs = tuple(snapshot.get(name, '1').ref for name in ('MOM20', 'REV1'))
    builder = DatasetBuilder(DatasetBuildOptions(holding_days=5, expected_member_count=24,
        minimum_cross_section=5, filter_config=UniverseFilterConfig(min_listed_days=1,
            zero_volume_window=1, quality_window=5, min_quality_ratio=.8)),
        snapshot=snapshot, factor_refs=refs,
        data_context=FactorDataContext('synthetic-p307b', 'SYNTHETIC_CLOSE_ONLY'),
        data_version='seed-20261014-v1', calendar_id='synthetic-bday-v1')
    options = dict(factor_score=dict(factor_weights={'MOM20': .5, 'REV1': .5}),
        ridge=dict(ridge_alphas=(.1, 1.)),
        lightgbm=dict(lightgbm_candidates=[dict(n_estimators=8, num_leaves=7, min_child_samples=10)]),
        xgboost=dict(xgboost_candidates=[dict(n_estimators=8, max_depth=2)]))
    if dynamic:
        selector = TrainFactorSelector(snapshot, refs, FactorSelectionConfig(k=2,
            minimum_cross_section=5, minimum_dates=10, minimum_abs_rank_ic=0.,
            minimum_direction_consistency=0.))
        for model, settings in options.items():
            settings['factor_selector'] = selector
            if model == 'factor_score':
                settings.pop('factor_weights')
                settings['dynamic_factor_score_weighting'] = 'equal'
    if models is not None:
        options = {name: options[name] for name in models}
    query = lambda kind, symbol=None: HubQuery('synthetic', 'seed-20261014-v1', kind,
        str(dates[0].date()), str(dates[-1].date()), symbol)
    queries = {key: tuple(query(kind, symbol) for symbol in symbols)
        for key, kind in [('bars', 'daily_bars'), ('membership', 'membership'),
                          ('status', 'security_status'), ('listings', 'listings')]}
    queries.update(benchmark=(query('index_bars', '000905.SH'),), calendar=(query('calendar'),))
    config = ResearchPipelineConfig('p307b-synthetic', Path(output), queries, builder,
        ExpandingWalkForwardSplitter(initial_train_months=2, validation_months=1,
            test_months=1, step_months=2, purge_days=5, embargo_days=5), options,
        OptimizerConfig(objective='minimum_variance', investment=.8, max_weight=.15, shrinkage=.2),
        ExecutionBacktestConfig(initial_cash=100000, minimum_cross_section=5),
        'SYNTHETIC', str(dates[65].date()), str(dates[249].date()))
    return config, DataHub({'synthetic': provider}), provider


def run(output, execution_mode='batch', turnover_limit=None):
    started = time.perf_counter()
    config, hub, _ = configured(output)
    config = replace(config, execution_mode=execution_mode,
        experiment_id='p313-synthetic-' + execution_mode,
        optimizer=replace(config.optimizer, turnover_limit=turnover_limit))
    result = ResearchPipeline(config).run(hub,
        tracker=ExperimentTracker(Path(output) / 'experiments'),
        repo_root=Path(__file__).resolve().parents[2],
        command=['python', '-m', 'examples.research.research_pipeline_synthetic', '--output', str(output),
                 '--execution-mode', execution_mode] +
                ([] if turnover_limit is None else ['--turnover-limit', str(turnover_limit)]))
    summary = dict(boundary=BOUNDARY, synthetic=True, execution_mode=execution_mode,
        turnover_limit=turnover_limit, seconds=time.perf_counter() - started,
        dataset_identity=result.dataset.identity_hash, models={})
    for model, walk in result.walk_forward.items():
        execution = result.executions[model]
        assert 2 <= len(walk.fold_summary) <= 3
        nav = execution.nav.set_index('date')
        market = execution.positions.groupby('date').market_value.sum().reindex(nav.index, fill_value=0)
        np.testing.assert_allclose(nav.cash + market, nav.portfolio_value, rtol=1e-12)
        np.testing.assert_allclose(nav.portfolio_value/config.execution.initial_cash, nav.strategy_nav, rtol=1e-12)
        np.testing.assert_allclose(config.execution.initial_cash +
            (nav.sell_notional-nav.buy_notional-nav.transaction_cost).cumsum(), nav.cash, rtol=1e-12)
        pd.testing.assert_frame_equal(result.evaluations[model][1].tables['daily'], execution.nav)
        decision = result.decisions[model]
        if execution_mode == 'incremental_feedback':
            assert (decision.decision_time.dt.hour == 15).all()
            assert (decision.execution_time > decision.decision_time).all()
            for row in decision.itertuples():
                weights, target = json.loads(row.actual_weights), json.loads(row.target_weights)
                expected = sum(abs(target.get(s, 0)-weights.get(s, 0)) for s in set(weights) | set(target))
                np.testing.assert_allclose(row.estimated_gross_security_turnover, expected, atol=1e-8)
                if turnover_limit is not None:
                    assert expected <= turnover_limit + 1e-8
        summary['models'][model] = dict(folds=len(walk.fold_summary), predictions=len(walk.predictions),
            rank_ic=result.evaluations[model][0].metrics['prediction_rank_ic_mean'].value,
            trades=len(execution.trades), rejections=len(execution.rejections),
            final_nav=float(execution.nav.iloc[-1].strategy_nav),
            final_cash=float(execution.nav.iloc[-1].cash))
    summary['queried_runs'] = len(result.tracker.query(status='SUCCEEDED'))
    summary['compared_runs'] = len(result.tracker.compare(result.run_ids))
    summary['reproduction'] = result.tracker.reproduction(result.run_ids[0])
    (Path(output) / 'summary.json').write_text(json.dumps(summary, indent=2, allow_nan=False) + '\n', encoding='utf-8')
    print(json.dumps({k: v for k, v in summary.items() if k != 'reproduction'}, indent=2))
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=BOUNDARY)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--execution-mode', choices=['batch', 'incremental_feedback'], default='batch')
    parser.add_argument('--turnover-limit', type=float)
    args = parser.parse_args()
    run(args.output, args.execution_mode, args.turnover_limit)
