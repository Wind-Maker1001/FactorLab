"""Offline P9-01: verify frozen evidence, reconstruct once, then gate execution."""
import argparse
from dataclasses import asdict
from datetime import date
import importlib.metadata
import json
from pathlib import Path
import subprocess
import sys

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT / 'tools/research'))
from run_real_model_walkforward import load_inputs, dump
from factorlab.backtest.execution import AShareExecutionBacktester, ExecutionBacktestConfig
from factorlab.backtest.fees import FeeRule, FeeSchedule
from factorlab.dataset.identity import file_hash, frame_hash, json_hash
from factorlab.experiments.tracker import ExperimentTracker
from factorlab.errors import SchemaError
from factorlab.research.real_model_study import KEYS, TARGET, build_panel, make_runner
from factorlab.research.oos_execution_study import (
    verify_predictions, signal_candidates, FittedFoldCapture, compare_reconstruction,
    make_targets, execution_bars, valuation_gaps,
)
from factorlab.strategies.real_shared_portfolio import audit_ledger
from factorlab.evaluation.research import EvaluationContext, ResearchEvaluation
from factorlab.validation.factor_selection import TrainFactorSelector, FactorSelectionConfig
from factorlab.validation.splitter import ExpandingWalkForwardSplitter


def execute(scores, bars, dates, symbols, config, scheme, output, *, dataset_id,
            valuation_qualifier=None):
    output.mkdir()
    targets, decisions = make_targets(scores, dates, symbols, config, scheme)
    targets.to_parquet(output / 'targets.parquet', index=False)
    decisions.to_csv(output / 'decisions.csv', index=False)
    engine = AShareExecutionBacktester(ExecutionBacktestConfig(initial_cash=config['initial_cash'],
        benchmark_open_price_column='raw_open', benchmark_close_price_column='raw_close'),
        FeeSchedule([FeeRule(effective_from=date(1990, 1, 1), **config['fees'])]))
    # An explicitly modelled zero-interest cash comparator supplies source sessions;
    # it is not a manufactured security or a market-index return series.
    reference = pd.DataFrame(dict(date=dates, raw_open=1., raw_close=1.))
    result = engine.run_target_weights(targets, bars.loc[bars.date.isin(dates)], reference)
    for name in ('trades', 'positions', 'nav', 'rejections', 'target_comparison'):
        getattr(result, name).to_parquet(output / (name + '.parquet'), index=False)
    gaps = valuation_gaps(result, bars)
    gaps.to_csv(output / 'valuation-gaps.csv', index=False)
    qualifications = None
    if valuation_qualifier is not None:
        qualifications = valuation_qualifier(result, bars)
        qualifications.to_parquet(output / 'valuation-qualifications.parquet', index=False)
    if len(gaps) and qualifications is None:
        return result, dict(status='PARTIAL', reason='UNOBSERVED_HELD_CLOSE',
            first_unreliable_date=str(gaps.date.min().date()), affected_security_days=len(gaps),
            total_return_status='TOTAL_RETURN_UNAVAILABLE', total_return=None), None
    audit = audit_ledger(result, bars.loc[bars.date.isin(dates)], config['initial_cash'],
        valuation_prices=qualifications)
    nav = result.nav.set_index('date').reindex(dates)
    if not nav.index[nav.strategy_nav.isna()].equals(dates[:1]):
        raise SchemaError('missing execution sessions other than initial cash day')
    nav.loc[dates[0], ['strategy_nav', 'portfolio_value', 'cash', 'holdings', 'transaction_cost',
        'turnover', 'buy_notional', 'sell_notional', 'executed_orders']] = [1., config['initial_cash'], config['initial_cash'], 0, 0., 0., 0., 0., 0]
    nav['strategy_return'] = nav.strategy_nav.pct_change(fill_method=None).fillna(0.)
    nav['benchmark_nav'], nav['benchmark_return'], nav['excess_nav'] = 1., 0., nav.strategy_nav
    nav['exposure'] = 1 - nav.cash / nav.portfolio_value
    nav = nav.rename_axis('date').reset_index()
    context = EvaluationContext(dataset_id=dataset_id, universe_id='P9_01_FROZEN_SIGNAL_CANDIDATES',
        label=config['return_basis'], label_id='RAW_PRICE', benchmark_id='MODEL_CASH_ZERO_RETURN_NOT_INDEX',
        calendar=tuple(dates.strftime('%Y-%m-%d')), holding_days=5, source_kind='REAL_CANDIDATE')
    evaluation = ResearchEvaluation(context).portfolio(scheme, nav, trades=result.trades, positions=result.positions)
    metrics = {name:metric.value for name,metric in evaluation.metrics.items()}
    metrics.update(average_exposure=float(nav.exposure.mean()), rejection_count=len(result.rejections),
        rejection_reasons=result.rejections.reason.value_counts().to_dict(),
        mean_absolute_target_deviation=float(result.target_comparison.weight_deviation.abs().mean()),
        maximum_absolute_target_deviation=float(result.target_comparison.weight_deviation.abs().max()),
        fees={name:float(result.trades[name].sum()) for name in ('commission','stamp_duty','transfer_fee','side_fee','slippage','transaction_cost')},
        market_index_status='UNAVAILABLE', total_return_status='TOTAL_RETURN_UNAVAILABLE', total_return=None)
    nav.to_csv(output / 'daily-nav.csv', index=False)
    fees = metrics['fees']
    minimum_impact = (result.trades.commission - result.trades.trade_notional * config['fees']['commission_bps'] / 10000).clip(lower=0).sum()
    partial = result.rejections.merge(result.trades[['signal_date', 'execution_date', 'symbol', 'side']],
        on=['signal_date', 'execution_date', 'symbol', 'side'], how='inner')
    terminal = result.positions.loc[result.positions.date.eq(dates[-1])]
    metrics.update(status='COMPLETED', ledger=audit,
        average_holdings=float(nav.holdings.mean()), minimum_commission_increment=float(minimum_impact),
        partial_fill_records=len(partial), cost_over_initial_cash=fees['transaction_cost']/config['initial_cash'],
        same_fill_gross_price_pnl=float(nav.portfolio_value.iloc[-1] - config['initial_cash'] + fees['transaction_cost']),
        same_fill_gross_price_return=float(nav.strategy_nav.iloc[-1] - 1 + fees['transaction_cost']/config['initial_cash']),
        terminal_holdings=len(terminal), terminal_market_value=float(terminal.market_value.sum()),
        valuation_status='OBSERVED_RAW_CLOSE', trades_count=len(result.trades),
        target_positive_mean_absolute_deviation=float(result.target_comparison.loc[result.target_comparison.target_weight.gt(0), 'weight_deviation'].abs().mean()))
    if qualifications is not None:
        assumed = qualifications.valuation_assumption_used
        metrics.update(valuation_status=('CONDITIONAL_ON_CONFIRMED_SUSPENSION_CARRY'
                if assumed.any() else 'OBSERVED_RAW_CLOSE'),
            valuation_policy=config['valuation_policy'],
            valuation_assumption_security_days=int(assumed.sum()),
            valuation_assumption_account_days=int(qualifications.loc[assumed, 'date'].nunique()),
            valuation_assumption_position_fraction=float(assumed.mean()) if len(assumed) else 0.,
            valuation_assumption_mean_equity_fraction=float(qualifications.loc[assumed].groupby('date')
                .affected_portfolio_weight.sum().reindex(dates, fill_value=0).mean()),
            valuation_assumption_max_equity_fraction=float(qualifications.loc[assumed].groupby('date')
                .affected_portfolio_weight.sum().max()) if assumed.any() else 0.,
            suspended_holding_days=int(qualifications.confirmed_suspended.sum()))
        nav['valuation_status'] = metrics['valuation_status']
        nav.to_csv(output / 'daily-nav.csv', index=False)
    dump(output / 'metrics.json', metrics)
    return result, metrics, nav


def run(args):
    config = json.loads(Path(args.config).read_text(encoding='utf-8'))
    old_root = Path(args.original)
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=False)
    dump(output / 'config.json', config)
    original = verify_predictions(old_root / 'predictions.parquet', config)
    train_config = json.loads((old_root / 'config.json').read_text(encoding='utf-8'))
    expected_config = json.loads((ROOT / 'configs/research/p8-02-real-models.json').read_text(encoding='utf-8'))
    frozen = json.loads((old_root / 'frozen-study.json').read_text(encoding='utf-8'))
    if train_config != expected_config or json_hash(train_config) != frozen['config_hash']:
        raise ValueError('FROZEN_TRAIN_CONFIG_DRIFT')
    old_result = json.loads((old_root / 'result.json').read_text(encoding='utf-8'))
    environment = {p: importlib.metadata.version(p) for p in old_result['environment']}
    if environment != old_result['environment']:
        raise ValueError('RECONSTRUCTION_ENVIRONMENT_DRIFT')
    bars, members, calendar, entries, symbols = load_inputs(args, train_config)
    if json_hash(entries) != frozen['data_identity']:
        raise ValueError('SOURCE_MANIFEST_DRIFT')
    data, profile, snapshot, refs = build_panel(bars, members, calendar, train_config)
    if frame_hash(data) != frozen['sample_hash'] or snapshot.snapshot_hash != frozen['feature_identity']:
        raise ValueError('ORIGINAL_TRAINING_SAMPLE_OR_FEATURE_DRIFT')
    candidates = signal_candidates(bars, members, calendar)
    names = tuple(r.factor_id for r in refs)
    overlap = data[KEYS + list(names)].merge(candidates, on=KEYS, validate='one_to_one', suffixes=('_old', '_new'))
    if len(overlap) != len(data) or not np.array_equal(overlap[[n+'_old' for n in names]].to_numpy(), overlap[[n+'_new' for n in names]].to_numpy()):
        raise ValueError('SIGNAL_FEATURE_RECONSTRUCTION_DRIFT')
    research_calendar = calendar[(calendar >= train_config['start']) & (calendar <= train_config['end'])]
    folds = list(ExpandingWalkForwardSplitter(**train_config['splitter']).split(data, calendar=research_calendar))
    for fold, pin in zip(folds, frozen['folds'], strict=True):
        for stage in ('train', 'validation', 'test'):
            if [str(x.date()) for x in getattr(fold, stage+'_window')] != pin[stage] or len(getattr(fold, stage+'_indices')) != pin[stage+'_rows']:
                raise ValueError('FOLD_IDENTITY_DRIFT')
    code = {k: subprocess.check_output(['git', 'rev-parse', v], cwd=ROOT, text=True).strip() for k,v in [('commit','HEAD'),('tree','HEAD^{tree}')]}
    source_paths = [Path(__file__), ROOT / 'src/factorlab/research/oos_execution_study.py',
        ROOT / 'src/factorlab/research/real_model_study.py', ROOT / 'src/factorlab/validation/walk_forward.py',
        ROOT / 'src/factorlab/backtest/execution.py', ROOT / 'src/factorlab/backtest/fees.py']
    code['source_hashes'] = {p.relative_to(ROOT).as_posix():file_hash(p) for p in source_paths}
    identity = dict(kind='P9_01_DETERMINISTIC_MODEL_RECONSTRUCTION_NOT_ORIGINAL_REPLAY',
        original_prediction_sha256=config['prediction_sha256'], common_test_hash=config['common_test_hash'],
        original_sample_hash=frozen['sample_hash'], data_identity=frozen['data_identity'],
        feature_identity=snapshot.snapshot_hash, candidate_hash=frame_hash(candidates),
        candidate_rows=len(candidates), original_profile=profile, environment=environment, code=code,
        config_hash=json_hash(config), training_config_hash=json_hash(train_config), folds=frozen['folds'],
        entries=entries, symbols=symbols)
    dump(output / 'frozen-study.json', identity)
    candidates.to_parquet(output / 'signal-candidates.parquet', index=False)
    print('INPUTS_AND_EXECUTION_CONTRACT_FROZEN', flush=True)
    selector = TrainFactorSelector(snapshot, refs, FactorSelectionConfig(**train_config['selection']))
    predictions, comparisons = [], []
    for model_id in config['models']:
        model = model_id.removeprefix('B_')
        capture = FittedFoldCapture(make_runner(train_config, model, names, selector))
        rebuilt = capture.run(data, research_calendar)
        audit = compare_reconstruction(original.loc[original.model_id.eq(model_id)], rebuilt.predictions)
        comparisons.extend(dict(model_id=model_id, **r) for r in audit)
        rebuilt.factor_selection.to_csv(output / (model_id+'-selection.csv'), index=False)
        for fold in folds:
            selection = rebuilt.factor_selection.loc[rebuilt.factor_selection.fold.eq(fold.fold)].iloc[0]
            old_selection = pd.read_csv(old_root / (model_id+'-factor_selection.csv')).set_index('fold').loc[fold.fold]
            if selection.selection_hash != old_selection.selection_hash or selection.fit_source_sha256 != old_selection.fit_source_sha256:
                raise ValueError('TRAIN_SELECTION_OR_FIT_SOURCE_DRIFT')
            fitted, features = capture.fitted[fold.fold]
            test = candidates.loc[candidates.date.ge(fold.test_window[0]) & candidates.date.lt(fold.test_window[1])].copy()
            for name, direction in selection.directions.items():
                test[name] *= direction
            test['score'] = fitted.predict(test.loc[:, features])
            test['fold'], test['model_id'] = fold.fold, model_id
            test['feature_identity'] = selection.selection_hash
            predictions.append(test[KEYS + ['score', 'fold', 'model_id', 'feature_identity']])
            fitted.save(output / (model_id + '-fold-' + str(fold.fold) + '.pkl'))
        print(model_id + ' reconstruction verified', flush=True)
    predicted = pd.concat(predictions, ignore_index=True)
    predicted.to_parquet(output / 'candidate-predictions.parquet', index=False)
    dump(output / 'reconstruction.json', comparisons)
    identity['candidate_prediction_sha256'] = file_hash(output / 'candidate-predictions.parquet')
    dump(output / 'inference-identity.json', identity)
    bars = execution_bars(bars)
    first = folds[0]
    dates = calendar[(calendar >= first.test_window[0]) & (calendar < first.test_window[1])][:config['pilot_sessions']]
    scores = predicted.loc[predicted.model_id.eq(config['models'][0]) & predicted.fold.eq(first.fold)]
    _, pilot, _ = execute(scores, bars, dates, symbols, config, config['models'][0], output / 'pilot', dataset_id=frozen['data_identity'])
    dump(output / 'pilot.json', pilot)
    if pilot['status'] != 'COMPLETED':
        dump(output / 'result.json', dict(status='P9_01_BLOCKED', stage='PILOT', pilot=pilot, safety=config['safety']))
        print('P9_01_BLOCKED', flush=True)
        return
    print('PILOT_PASSED_FULL_STUDY_STARTED', flush=True)
    tracker = ExperimentTracker(output / 'tracker')
    metrics_rows, nav_parts, failures = [], [], []
    for fold in folds:
        dates = calendar[(calendar >= fold.test_window[0]) & (calendar < fold.test_window[1])]
        for scheme in config['models'] + config['benchmarks'][:2]:
            if scheme in config['models']:
                scores = predicted.loc[predicted.model_id.eq(scheme) & predicted.fold.eq(fold.fold)]
            else:
                scores = candidates.loc[candidates.date.isin(dates)].assign(score=lambda x:x.RAW_MOM20)
            name = scheme+'-fold-'+str(fold.fold)
            try:
                result, metrics, nav = execute(scores, bars, dates, symbols, config, scheme, output / name, dataset_id=frozen['data_identity'])
            except SchemaError as error:
                if not str(error).startswith('execution status is missing for '):
                    raise
                # Unknown source status is a data boundary, not permission to
                # fabricate an executable state or retry with a different pool.
                metrics_rows.append(dict(model_id=scheme, fold=fold.fold,
                    metrics=dict(status='BLOCKED', reason=str(error), total_return=None,
                        total_return_status='TOTAL_RETURN_UNAVAILABLE')))
                dump(output / 'fold-metrics.json', metrics_rows)
                dump(output / 'result.json', dict(status='P9_01_BLOCKED',
                    reason=str(error), interrupted_account=name,
                    completed_account_metrics=metrics_rows, safety=config['safety']))
                return
            dump(output / name / 'metrics.json', metrics)
            metrics_rows.append(dict(model_id=scheme, fold=fold.fold, metrics=metrics))
            if nav is None:
                failures.append(name)
            else:
                nav_parts.append(nav.assign(model_id=scheme, fold=fold.fold))
            tracker.create_experiment(name, dict(model=scheme, parameters=config,
                factors=[asdict(r) for r in refs], dataset=dict(candidate_hash=identity['candidate_hash']),
                label=dict(definition=train_config['label']), windows={s:[str(x.date()) for x in getattr(fold,s+'_window')] for s in ('train','validation','test')},
                walk_forward=train_config['splitter'], costs=config['fees']))
            tracker.append_run(name, name+'-run', code=code, environment=environment, command=sys.argv,
                status='SUCCEEDED' if nav is not None else 'FAILED', qualification='REAL_CANDIDATE',
                validation=dict(status=metrics['status']), artifacts=dict(metrics=dict(path=str((output/name/'metrics.json').resolve()))))
            print(name+' '+metrics['status'], flush=True)
        nav_parts.append(pd.DataFrame(dict(date=dates, strategy_nav=1., portfolio_value=config['initial_cash'],
            cash=config['initial_cash'], model_id='cash', fold=fold.fold)))
    dump(output / 'fold-metrics.json', metrics_rows)
    if nav_parts:
        pd.concat(nav_parts, ignore_index=True).to_csv(output / 'daily-nav.csv', index=False)
    artifacts = {p.relative_to(output).as_posix():file_hash(p) for p in output.rglob('*') if p.is_file() and 'tracker' not in p.parts}
    dump(output / 'result.json', dict(status='P9_01_PARTIAL' if failures else 'P9_01_REAL_OOS_EXECUTION_COMPLETED',
        failed_accounts=failures, artifacts=artifacts, safety=config['safety']))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default=str(ROOT / 'configs/research/p9-01-oos-execution.json'))
    parser.add_argument('--original', required=True)
    parser.add_argument('--bigquant-root', required=True)
    parser.add_argument('--membership-zip', required=True)
    parser.add_argument('--output', required=True)
    run(parser.parse_args())
