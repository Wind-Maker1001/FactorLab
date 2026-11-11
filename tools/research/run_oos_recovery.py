"""Offline P9-02: reuse saved inference, qualify marks, stage bounded execution."""
import argparse
import io
import json
from pathlib import Path
import sys
import zipfile

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT / 'tools/research'))
from run_real_model_walkforward import load_inputs, dump
from run_oos_execution import execute
from factorlab.data.evidence import json_bytes, sha256
from factorlab.data.market_evidence_sidecar import ALLOWED, load_sidecar
from factorlab.dataset.identity import file_hash, frame_hash, json_hash
from factorlab.errors import SchemaError
from factorlab.experiments.tracker import ExperimentTracker
from factorlab.research.oos_execution_study import verify_predictions, signal_candidates, valuation_gaps
from factorlab.research.oos_execution_recovery import recovery_bars, qualify_valuations, audit_frozen_fees


def evidence_inputs(args, bars, *, additional_observations=()):
    sources = json.loads((ROOT / 'docs/validation/p9-d01/public-sources.json').read_bytes())
    observation = sources['observation_identity']
    documents = {}
    for source in sources['documents']:
        documents[source['sha256']] = (args.evidence_root / source['local_name']).read_bytes()
    manifest = (args.original / 'input-manifest.json').read_bytes()
    entries = {x['member']: x['actual_sha256'] for x in json.loads(manifest)['entries']}
    with zipfile.ZipFile(Path(args.bigquant_root) / 'stock.zip') as archive:
        member = 'stock/cn_stock_real_bar1d/2023-04-01_2023-04-30.parquet'
        content = archive.read(member)
        if sha256(content) != entries[member] or sha256(content) != observation['source_sha256']:
            raise ValueError('CARRY_SOURCE_IDENTITY_DRIFT')
        documents[sha256(content)] = content
        row = bars.loc[bars.symbol.eq('002408.SZ') & bars.date.eq('2023-04-25')].iloc[0]
        if row.raw_close != float(observation['price']) or row.volume != observation['volume']:
            raise ValueError('CARRY_OBSERVATION_DRIFT')
        member = 'stock/cn_stock_real_bar1d/2023-05-01_2023-05-31.parquet'
        content = archive.read(member)
        if sha256(content) != entries[member]:
            raise ValueError('EXECUTION_PRICE_SOURCE_IDENTITY_DRIFT')
        documents[sha256(content)] = content
        member = 'stock/cn_stock_status/2023-05-01_2023-05-31.parquet'
        content = archive.read(member)
        if sha256(content) != entries[member]:
            raise ValueError('ST_SOURCE_IDENTITY_DRIFT')
        native = pd.read_parquet(io.BytesIO(content), filters=[('instrument', '==', '000540.SZ'),
            ('date', '==', pd.Timestamp('2023-05-08'))]).iloc[0]
        if (native.suspended, native.st_status, native.is_risk_warning) != (0, 2, 1):
            raise ValueError('ST_SOURCE_VALUES_DRIFT')
    for observed in (observation, *additional_observations):
        if observed['source_sha256'] != observation['source_sha256']:
            raise ValueError('CARRY_SOURCE_IDENTITY_DRIFT')
        row = bars.loc[bars.symbol.eq(observed['security']) & bars.date.eq(observed['date'])].iloc[0]
        if row.raw_close != float(observed['price']) or row.volume != observed['volume']:
            raise ValueError('CARRY_OBSERVATION_DRIFT')
        capsule = json_bytes(observed)
        documents[sha256(capsule)] = capsule
    evidence = load_sidecar(ROOT / args.recovery['sidecar'], source_manifest=manifest,
        originals={key: None for key in ALLOWED}, documents=documents)
    return evidence, observation


def verify_saved(args, bars, members, calendar, entries, config):
    old, study = args.original, args.study
    identity = json.loads((study / 'inference-identity.json').read_bytes())
    pin = json.loads((ROOT / 'docs/validation/p9-01/result.json').read_bytes())
    for key, value in pin['identity'].items():
        if identity[key] != value:
            raise ValueError('SAVED_INFERENCE_IDENTITY_DRIFT: ' + key)
    if json_hash(entries) != identity['data_identity']:
        raise ValueError('SOURCE_INPUT_DRIFT')
    if json.loads((study / 'config.json').read_bytes()) != config:
        raise ValueError('FROZEN_EXECUTION_CONFIG_DRIFT')
    original = verify_predictions(old / 'predictions.parquet', config)
    candidates = pd.read_parquet(study / 'signal-candidates.parquet')
    if frame_hash(candidates) != identity['candidate_hash']:
        raise ValueError('SAVED_CANDIDATE_FEATURE_DRIFT')
    rebuilt = signal_candidates(bars, members, calendar)
    if frame_hash(rebuilt) != identity['candidate_hash']:
        raise ValueError('CAUSAL_CANDIDATE_DRIFT')
    prediction_path = study / 'candidate-predictions.parquet'
    if file_hash(prediction_path) != args.recovery['candidate_prediction_sha256']:
        raise ValueError('CANDIDATE_PREDICTION_DRIFT')
    predicted = pd.read_parquet(prediction_path)
    if predicted.duplicated(['model_id', 'fold', 'date', 'symbol']).any():
        raise ValueError('DUPLICATE_SAVED_PREDICTIONS')
    receipts = []
    for model in config['models']:
        selection = pd.read_csv(study / (model + '-selection.csv')).set_index('fold')
        original_selection = pd.read_csv(old / (model + '-factor_selection.csv')).set_index('fold')
        for fold in identity['folds']:
            number = fold['fold']
            fitted = study / f'{model}-fold-{number}.pkl'
            digest = file_hash(fitted)
            if digest != pin['private_artifact_sha256'][fitted.name]:
                raise ValueError('SAVED_MODEL_STATE_DRIFT: ' + fitted.name)
            for key in ('selection_hash', 'fit_source_sha256'):
                if selection.loc[number, key] != original_selection.loc[number, key]:
                    raise ValueError('TRAIN_SELECTION_DRIFT')
            part = predicted.loc[predicted.model_id.eq(model) & predicted.fold.eq(number)]
            expected = candidates.loc[candidates.date.ge(fold['test'][0]) & candidates.date.lt(fold['test'][1])]
            if not part[['date', 'symbol']].reset_index(drop=True).equals(expected[['date', 'symbol']].reset_index(drop=True)):
                raise ValueError('CANDIDATE_FOLD_KEYS_DRIFT')
            if not part.feature_identity.eq(selection.loc[number, 'selection_hash']).all():
                raise ValueError('PREDICTION_SELECTION_IDENTITY_DRIFT')
            common = original.loc[original.model_id.eq(model) & original.fold.eq(number)]
            joined = common.merge(part, left_on=['signal_date', 'symbol'], right_on=['date', 'symbol'], validate='one_to_one')
            delta = float((joined.prediction - joined.score).abs().max())
            if len(joined) != len(common) or not np.allclose(joined.prediction, joined.score, rtol=1e-10, atol=1e-12):
                raise ValueError('COMMON_PREDICTIONS_DIFFER')
            receipts.append(dict(model_id=model, fold=number, model_sha256=digest,
                selection_hash=selection.loc[number, 'selection_hash'],
                fit_source_sha256=selection.loc[number, 'fit_source_sha256'],
                common_rows=len(common), maximum_prediction_difference=delta, candidate_rows=len(part)))
    return predicted, candidates, identity, receipts


def stage_a(bars, evidence, observation, config, output, *, observations=None):
    from factorlab.backtest.execution import AShareExecutionBacktester, ExecutionBacktestConfig
    from factorlab.backtest.fees import FeeSchedule, FeeRule
    from factorlab.strategies.real_shared_portfolio import audit_ledger
    from datetime import date
    from factorlab.data.market_evidence_sidecar import POLICY
    if observations is not None and config.get('valuation_policy') != POLICY:
        raise SchemaError('EXPLICIT_CARRY_POLICY_REQUIRED')
    output.mkdir()
    engine = AShareExecutionBacktester(ExecutionBacktestConfig(initial_cash=config['initial_cash'],
        benchmark_open_price_column='raw_open', benchmark_close_price_column='raw_close'),
        FeeSchedule([FeeRule(effective_from=date(1990, 1, 1), **config['fees'])]))
    receipts, blocked = [], None
    for symbol, days, buy, sell in [
        ('002408.SZ', ['2023-04-24', '2023-04-25', '2023-04-26', '2023-04-27', '2023-04-28', '2023-05-04', '2023-05-05'], '2023-04-24', '2023-04-25'),
        ('000540.SZ', ['2023-04-25', '2023-04-26', '2023-04-27', '2023-04-28', '2023-05-04', '2023-05-05', '2023-05-08', '2023-05-09'], '2023-04-25', '2023-05-05')]:
        dates = pd.DatetimeIndex(days)
        target = pd.DataFrame(dict(date=pd.to_datetime([buy, sell]), symbol=symbol, target_weight=[.1, 0.]))
        result = engine.run_target_weights(target, bars.loc[bars.symbol.eq(symbol) & bars.date.isin(dates)],
            pd.DataFrame(dict(date=dates, raw_open=1., raw_close=1.)))
        target.to_parquet(output / f'{symbol}-targets.parquet', index=False)
        valuation_gaps(result, bars).to_csv(output / f'{symbol}-valuation-gaps.csv', index=False)
        audit_frozen_fees(result, config)
        for name in ('trades', 'positions', 'nav', 'rejections', 'target_comparison'):
            getattr(result, name).to_parquet(output / f'{symbol}-{name}.parquet', index=False)
        try:
            qualified = qualify_valuations(result, bars, evidence, observation, observations=observations)
        except SchemaError as error:
            if not str(error).startswith('UNQUALIFIED_HELD_CLOSE: '):
                raise
            blocked = dict(account='stage-a/' + symbol, reason=str(error))
            audit = dict(status='BLOCKED', reason=str(error), frozen_fees=True)
            qualified = None
        else:
            audit = audit_ledger(result, bars.loc[bars.symbol.eq(symbol) & bars.date.isin(dates)], config['initial_cash'], valuation_prices=qualified)
            qualified.to_parquet(output / f'{symbol}-valuations.parquet', index=False)
        if symbol == '002408.SZ':
            if qualified is None or qualified.valuation_assumption_used.sum() != 4 or not qualified.loc[qualified.date.eq('2023-05-05'), 'valuation_price'].eq(6.76).all():
                raise SchemaError('STAGE_A_SUSPENSION_OR_RESUMPTION_FAILED')
        else:
            rejected = result.rejections.loc[result.rejections.execution_date.eq('2023-05-08')]
            fills = result.trades.loc[result.trades.execution_date.eq('2023-05-08')]
            nav = result.nav.set_index('date')
            if (not rejected.reason.eq('sell_price_limit').any() or len(fills)
                    or nav.loc['2023-05-08', 'cash'] != nav.loc['2023-05-05', 'cash']
                    or result.positions.loc[result.positions.date.eq('2023-05-08')].empty):
                raise SchemaError('STAGE_A_SELL_REJECTION_FAILED')
        receipts.append(dict(symbol=symbol, ledger=audit, fills=len(result.trades),
            rejections=result.rejections.reason.value_counts().to_dict(),
            assumption_days=int(qualified.valuation_assumption_used.sum()) if qualified is not None else None))
    dump(output / 'result.json', dict(status='BLOCKED' if blocked else 'PASSED', accounts=receipts, blocked=blocked))
    return blocked


def run(args):
    args.recovery = json.loads((ROOT / 'configs/research/p9-02/recovery.json').read_bytes())
    config = json.loads((ROOT / args.recovery['frozen_execution_config']).read_bytes())
    train = json.loads((args.original / 'config.json').read_bytes())
    args.output.mkdir(parents=True, exist_ok=False)
    sources = [Path(__file__), ROOT/'src/factorlab/research/oos_execution_recovery.py',
        ROOT/'tools/research/run_oos_execution.py', ROOT/'src/factorlab/strategies/real_shared_portfolio.py',
        ROOT/'src/factorlab/backtest/execution.py', ROOT/'src/factorlab/backtest/fees.py']
    dump(args.output/'code-identity.json', {p.relative_to(ROOT).as_posix():file_hash(p) for p in sources})
    bars, members, calendar, entries, symbols = load_inputs(args, train)
    predicted, candidates, identity, receipts = verify_saved(args, bars, members, calendar, entries, config)
    dump(args.output / 'inference-verification.json', receipts)
    evidence, observation = evidence_inputs(args, bars)
    bars = recovery_bars(bars, evidence, cutoff=args.recovery['st_cutoff'])
    config = dict(config, valuation_policy=args.recovery['valuation_policy'])
    dump(args.output / 'config.json', dict(execution=config, recovery=args.recovery))
    def qualifier(result, source_bars):
        audit_frozen_fees(result, config)
        return qualify_valuations(result, source_bars, evidence, observation)
    blocked = stage_a(bars, evidence, observation, config, args.output / 'stage-a')
    if blocked:
        dump(args.output / 'fold-metrics.json', [])
        artifacts = {p.relative_to(args.output).as_posix():file_hash(p) for p in args.output.rglob('*') if p.is_file()}
        dump(args.output / 'result.json', dict(status='P9_02_BLOCKED', stage='A', blocked=blocked,
            completed_accounts=0, inference=receipts,
            candidate_prediction_sha256=args.recovery['candidate_prediction_sha256'],
            trade_status_semantics=args.recovery['trade_status_semantics'], safety=config['safety'], artifacts=artifacts))
        print('P9_02_BLOCKED ' + blocked['reason'], flush=True)
        return
    print('STAGE_A_PASSED', flush=True)
    tracker = ExperimentTracker(args.output / 'tracker')
    rows, navs, qualifications = [], [], []
    blocked = None
    for fold in identity['folds']:
        number = fold['fold']
        dates = calendar[(calendar >= fold['test'][0]) & (calendar < fold['test'][1])]
        for scheme in config['models'] + config['benchmarks'][:2]:
            scores = (predicted.loc[predicted.model_id.eq(scheme) & predicted.fold.eq(number)]
                if scheme in config['models'] else candidates.loc[candidates.date.isin(dates)].assign(score=lambda x:x.RAW_MOM20))
            name = f'{scheme}-fold-{number}'
            try:
                result, metrics, nav = execute(scores, bars, dates, symbols, config, scheme,
                    args.output / name, dataset_id=identity['data_identity'], valuation_qualifier=qualifier)
            except SchemaError as error:
                blocked = dict(account=name, reason=str(error))
                rows.append(dict(model_id=scheme, fold=number, metrics=dict(status='BLOCKED', reason=str(error))))
                dump(args.output / 'fold-metrics.json', rows)
                break
            metrics['trade_status_semantics'] = args.recovery['trade_status_semantics']
            dump(args.output / name / 'metrics.json', metrics)
            rows.append(dict(model_id=scheme, fold=number, metrics=metrics))
            navs.append(nav.assign(model_id=scheme, fold=number))
            qualifications.append(pd.read_parquet(args.output / name / 'valuation-qualifications.parquet').assign(model_id=scheme, fold=number))
            tracker.create_experiment(name, dict(model=scheme, parameters=config, factors=[],
                dataset=dict(candidate_hash=identity['candidate_hash']), label=dict(definition=train['label']),
                windows={key:fold[key] for key in ('train', 'validation', 'test')}, walk_forward=train['splitter'], costs=config['fees']))
            tracker.append_run(name, name+'-run', code=dict(source_sha256=file_hash(Path(__file__))),
                environment=identity['environment'], command=sys.argv, status='SUCCEEDED', qualification='REAL_CANDIDATE',
                validation=dict(ledger=metrics['ledger'], valuation_status=metrics['valuation_status']),
                artifacts=dict(metrics=dict(path=str((args.output/name/'metrics.json').resolve()))))
            dump(args.output / 'fold-metrics.json', rows)
            print(name + ' COMPLETED ' + metrics['valuation_status'], flush=True)
        if blocked:
            break
        navs.append(pd.DataFrame(dict(date=dates, strategy_nav=1., portfolio_value=config['initial_cash'],
            cash=config['initial_cash'], model_id='cash', fold=number, valuation_status='MODEL_CASH_ZERO_RETURN')))
        print(f'FOLD_{number}_LEDGERS_PASSED', flush=True)
    status = ('P9_02_PARTIAL' if navs else 'P9_02_BLOCKED') if blocked else 'P9_02_EXECUTION_RECOVERED'
    if navs:
        pd.concat(navs, ignore_index=True).to_csv(args.output / 'daily-nav.csv', index=False)
    if qualifications:
        pd.concat(qualifications, ignore_index=True).to_parquet(args.output / 'valuation-qualifications.parquet', index=False)
    artifacts = {p.relative_to(args.output).as_posix():file_hash(p) for p in args.output.rglob('*') if p.is_file() and 'tracker' not in p.parts}
    dump(args.output / 'result.json', dict(status=status, blocked=blocked, completed_accounts=sum(r['metrics']['status']=='COMPLETED' for r in rows),
        inference=receipts, candidate_prediction_sha256=args.recovery['candidate_prediction_sha256'],
        trade_status_semantics=args.recovery['trade_status_semantics'], safety=config['safety'], artifacts=artifacts))
    print(status, flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('study', 'original', 'evidence-root', 'bigquant-root', 'membership-zip', 'output'):
        parser.add_argument('--'+name, type=Path, required=True)
    run(parser.parse_args())
