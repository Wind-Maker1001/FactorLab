"""Reproducible public synthetic input and cross-version artifact generator.

This executes the selected installed implementation. No mocks, supplier calls,
financial qualification, or Golden regeneration are involved.
"""
import argparse
import hashlib
import importlib
import json
from pathlib import Path
import platform
import socket
import time

import numpy as np
import pandas as pd

WARNING = 'SYNTHETIC / SOFTWARE_ONLY / NOT FINANCIAL EVIDENCE'


def build_inputs(seed=42):
    dates = pd.bdate_range('2019-09-02', '2020-12-31')
    rng = np.random.default_rng(seed)
    symbols = ['%06d.SZ' % n for n in range(1, 11)]
    rows = []
    for number, symbol in enumerate(symbols):
        changes = rng.normal(0.0002, 0.008, len(dates))
        prices = (10 + number * 0.3) * np.cumprod(1 + changes)
        for offset, date in enumerate(dates):
            open_ = prices[offset]
            close = open_ * (1 + 0.003 * np.sin(offset / 5 + number))
            volume = 1e6 + 2000 * offset + 10000 * number
            rows.append(dict(date=date, symbol=symbol, open=open_, close=close,
                high=max(open_, close) * 1.01, low=min(open_, close) * 0.99,
                raw_open=open_, raw_close=close, raw_high=max(open_, close)*1.01,
                raw_low=min(open_, close)*0.99, adj_open=open_, adj_close=close,
                adj_high=max(open_, close)*1.01, adj_low=min(open_, close)*0.99,
                volume=volume, amount=volume*close, turnover=0.01+0.003*np.sin(offset/7+number),
                adj_factor=1.0, trade_status=1, is_st=False))
    bars = pd.DataFrame(rows).sort_values(['date','symbol']).reset_index(drop=True)
    position = np.arange(len(dates))
    benchmark_close = 5000*np.cumprod(1+0.001*np.sin(position/19)+0.0001)
    benchmark = pd.DataFrame(dict(date=dates, symbol='000905.SH',
                                 adj_open=benchmark_close, adj_close=benchmark_close))
    membership = bars.loc[bars.date.ge('2020-01-01'), ['date','symbol']].assign(is_member=True)
    status = bars[['date','symbol','trade_status','is_st']].copy()
    listings = pd.DataFrame(dict(symbol=symbols, listing_date=pd.Timestamp('2018-01-01')))
    return {'bars': bars, 'benchmark': benchmark, 'membership': membership,
            'status': status, 'listings': listings, 'calendar': pd.DataFrame({'date': dates})}


def run(output, namespace='factorlab', input_root=None, seed=42):
    started = time.perf_counter()
    def deny(*args, **kwargs):
        raise AssertionError('synthetic demo attempted network access')
    # Public synthetic runner is explicitly offline, including accidental calls.
    original_connect, original_create = socket.socket.connect, socket.create_connection
    socket.socket.connect = deny
    socket.create_connection = deny
    try:
        return _run(Path(output), namespace, input_root, seed, started)
    finally:
        socket.socket.connect, socket.create_connection = original_connect, original_create


def _run(output, namespace, input_root, seed, started):
    def module(name):
        return importlib.import_module(namespace + '.' + name)
    output.mkdir(parents=True, exist_ok=False)
    inputs = build_inputs(seed) if input_root is None else {
        name: pd.read_parquet(Path(input_root)/(name+'.parquet'))
        for name in ('bars','benchmark','membership','status','listings','calendar')}
    for name, frame in inputs.items():
        frame.to_parquet(output/(name+'.parquet'), index=False)
    calendar = pd.DatetimeIndex(inputs['calendar'].date)
    registry = module('factors.registry')
    factors = list(registry.FACTOR_REGISTRY)
    factor_frame = registry.compute_factors(inputs['bars'], factors)
    factor_frame.to_parquet(output/'factors.parquet', index=False)
    builder = module('dataset.builder')
    filters = module('universe.filters')
    dataset = builder.DatasetBuilder(builder.DatasetBuildOptions(
        factor_names=factors, expected_member_count=10, minimum_cross_section=8,
        filter_config=filters.UniverseFilterConfig(min_listed_days=1,
            zero_volume_window=1, quality_window=1, min_quality_ratio=1.0),
    )).build(inputs['bars'], inputs['benchmark'], inputs['membership'],
             inputs['status'], inputs['listings'], calendar)
    dataset.to_parquet(output/'dataset.parquet', index=False)
    dates = calendar[calendar >= pd.Timestamp('2020-01-01')]
    validation = module('validation')
    backtest = module('backtest')
    metrics = module('metrics')
    reports = module('reports')
    model_options = {
        'factor_score': {'factor_weights': {name: -1.0 if name.startswith('VOL') else 1.0 for name in factors}},
        'ridge': {'ridge_alphas': (0.1,1.0)},
        'lightgbm': {'lightgbm_candidates': ({'n_estimators':8,'num_leaves':7,'max_depth':3,'min_child_samples':2},), 'lightgbm_n_jobs':1},
        'xgboost': {'xgboost_candidates': ({'n_estimators':8,'max_depth':2},), 'xgboost_n_jobs':1},
    }
    manifest = {'schema':'FACTORLAB_SYNTHETIC_DEMO_V1', 'warning':WARNING,
        'synthetic':True, 'software_only':True, 'financial_evidence':False,
        'formal_admission':False,'forward_allowed':False,'live_allowed':False,
        'safety':['NO LIVE MONEY','PIT_UNVERIFIED','FORMAL_ADMISSION_BLOCKED'],
        'python':platform.python_version(), 'implementation':namespace,
        'seed':seed, 'factor_names':factors, 'models':{}}
    for name, options in model_options.items():
        destination = output/name
        result = validation.WalkForwardRunner(
            validation.ExpandingWalkForwardSplitter(initial_train_months=4,
                validation_months=2,test_months=2,step_months=8,purge_days=5,embargo_days=5),
            model_name=name,model_directory=destination/'fold_models',
            feature_names=factors,minimum_cross_section=8,top_k=3,random_seed=seed,**options,
        ).run(dataset,calendar=dates)
        prediction = metrics.evaluate_predictions(result.predictions,top_k=3,minimum_cross_section=8)
        first = result.predictions.date.min()
        last = pd.to_datetime(result.predictions.exit_date).max()
        bars = inputs['bars'].loc[inputs['bars'].date.between(first,last)]
        benchmark = inputs['benchmark'].loc[inputs['benchmark'].date.between(first,last)]
        scores = result.predictions[['date','symbol','score']]
        fees = backtest.FeeSchedule.a_share_default(slippage_bps=0)
        fast = backtest.run_fast_backtest(scores,bars,benchmark,
            config=backtest.FastBacktestConfig(top_k=3,minimum_cross_section=8,rebalance_days=5),fee_schedule=fees)
        execution = backtest.run_execution_backtest(scores,bars,benchmark,
            config=backtest.ExecutionBacktestConfig(top_k=3,minimum_cross_section=8,rebalance_days=5),
            fee_schedule=fees,price_limit_schedule=backtest.PriceLimitSchedule.a_share_default())
        portfolio = metrics.calculate_portfolio_metrics(fast.nav,fast.trades,fast.positions)
        execution_metrics = metrics.calculate_portfolio_metrics(execution.nav,execution.trades,execution.positions)
        reports.write_model_report(name,result,prediction,fast,portfolio,destination,
            disclaimer=WARNING,execution_backtest=execution,execution_metrics=execution_metrics)
        for label, frame in (('predictions',result.predictions),('daily',prediction.daily),
            ('fast_nav',fast.nav),('fast_trades',fast.trades),('execution_nav',execution.nav),
            ('execution_trades',execution.trades),('execution_rejections',execution.rejections)):
            frame.to_parquet(destination/(label+'.parquet'),index=False)
        manifest['models'][name] = {'predictions':len(result.predictions),'fast_trades':len(fast.trades),
            'execution_trades':len(execution.trades),'execution_rejections':len(execution.rejections)}
    manifest['elapsed_seconds'] = time.perf_counter()-started
    manifest['artifacts'] = {p.relative_to(output).as_posix():hashlib.sha256(p.read_bytes()).hexdigest()
                             for p in sorted(output.rglob('*')) if p.is_file()}
    (output/'manifest.json').write_text(json.dumps(manifest,indent=2),encoding='utf-8')
    (output/'README.md').write_text('# '+WARNING+'\n\nNO LIVE MONEY. PIT_UNVERIFIED. FORMAL_ADMISSION_BLOCKED.\n',encoding='utf-8')
    return manifest


def main():
    parser = argparse.ArgumentParser(description=WARNING)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--namespace', default='factorlab')
    parser.add_argument('--input-root', type=Path)
    args = parser.parse_args()
    print(json.dumps(run(args.output,args.namespace,args.input_root),indent=2))


if __name__ == '__main__':
    main()
