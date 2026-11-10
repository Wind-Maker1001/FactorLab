"""Run the frozen P8-02 study against existing local archives, without downloads."""
import argparse
from dataclasses import asdict
import hashlib
import importlib.metadata
import io
import json
from pathlib import Path
import subprocess
import sys
import time
import zipfile

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT / 'tools' / 'data'))
from audit_p3_d06 import Archive, normalize
from factorlab.dataset.identity import file_hash, frame_hash, json_hash
from factorlab.experiments.tracker import ExperimentTracker
from factorlab.research.real_model_study import (
    KEYS, TARGET, build_panel, check_source_identity, make_runner, common_predictions,
    daily_metrics, summarize_daily, paired_daily, require_raw,
)
from factorlab.validation.factor_selection import TrainFactorSelector, FactorSelectionConfig
from factorlab.validation.splitter import ExpandingWalkForwardSplitter


def dump(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False, default=str) + '\n', encoding='utf-8')


def load_inputs(args, config):
    entries = []
    with zipfile.ZipFile(args.membership_zip) as z:
        payload = z.read('members_daily.csv')
        meta = z.read('manifest.json')
        for name, raw in [('members_daily.csv', payload), ('manifest.json', meta)]:
            entries.append(dict(archive='csi500_full_history.zip', member=name, actual_sha256=hashlib.sha256(raw).hexdigest()))
        members = pd.read_csv(io.BytesIO(payload))
    members['date'] = pd.to_datetime(members.date)
    members['symbol'] = normalize(members.stock_code)
    members = members.loc[members.date.between(config['start'], config['end']), KEYS]
    initial = members.loc[members.date.eq(pd.Timestamp(config['start'])), 'symbol'].unique()
    symbols = sorted(initial, key=lambda s: hashlib.sha256(s.encode()).hexdigest())[:config['symbol_limit']]
    members = members.loc[members.symbol.isin(symbols)].sort_values(KEYS).reset_index(drop=True)
    if len(symbols) != config['symbol_limit']:
        raise ValueError('first signal day has insufficient members')
    lo, hi = pd.Timestamp(config['warm_start']), pd.Timestamp(config['tail_end'])
    common = Archive(Path(args.bigquant_root), 'common', entries)
    stock = Archive(Path(args.bigquant_root), 'stock', entries)
    try:
        sessions = common.read('trading_days', ['date', 'market_code'], lo, hi)
        calendar = pd.DatetimeIndex(sessions.loc[sessions.market_code.eq('CN'), 'date'].drop_duplicates().sort_values())
        bars = stock.read('cn_stock_real_bar1d', ['date', 'instrument', 'open', 'high', 'low', 'close', 'volume'], lo, hi, set(symbols))
        status = stock.read('cn_stock_status', ['date', 'instrument', 'suspended', 'st_status'], lo, hi, set(symbols))
    finally:
        common.zip.close()
        stock.zip.close()
    expected = json.loads((ROOT / 'docs/validation/p3-d06/input-manifest.json').read_text(encoding='utf-8'))['entries']
    check_source_identity(entries, expected)
    if bars.duplicated(KEYS).any() or status.duplicated(KEYS).any():
        raise ValueError('duplicate source keys')
    bars = bars.rename(columns={name: 'raw_' + name for name in ['open', 'high', 'low', 'close']})
    bars = bars.merge(status, on=KEYS, how='left', validate='one_to_one')
    if not members.date.isin(calendar).all():
        raise ValueError('member dates absent from calendar')
    return bars, members, calendar, entries, symbols


def run(args):
    clock = time.perf_counter()
    config = json.loads(Path(args.config).read_text(encoding='utf-8'))
    require_raw(config)
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=False)
    dump(output / 'config.json', config)
    bars, members, calendar, entries, symbols = load_inputs(args, config)
    dump(output / 'input-manifest.json', dict(entries=entries, symbols=symbols,
        identity=json_hash(entries), source_kind='REAL_CANDIDATE',
        stable_security_identity='UNVERIFIED_EXCHANGE_QUALIFIED_VENDOR_SYMBOL',
        historical_available_at=None))
    data, profile, snapshot, refs = build_panel(bars, members, calendar, config)
    data_identity = json_hash(entries)
    names = tuple(r.factor_id for r in refs)
    research_calendar = calendar[(calendar >= config['start']) & (calendar <= config['end'])]
    folds = list(ExpandingWalkForwardSplitter(**config['splitter']).split(data, calendar=research_calendar))
    if not 1 <= len(folds) <= config['max_folds']:
        raise ValueError('fold budget violated')
    freeze = dict(config_hash=json_hash(config), data_identity=data_identity,
        feature_identity=snapshot.snapshot_hash, sample_hash=frame_hash(data), profile=profile,
        definitions=[asdict(d) for d in snapshot.definitions],
        folds=[dict(fold=f.fold, train=[str(x.date()) for x in f.train_window],
            validation=[str(x.date()) for x in f.validation_window], test=[str(x.date()) for x in f.test_window],
            train_rows=len(f.train_indices), validation_rows=len(f.validation_indices), test_rows=len(f.test_indices),
            audit=asdict(f.audit), purged_dates=[str(x.date()) for x in f.purged_dates],
            embargoed_dates=[str(x.date()) for x in f.embargoed_dates]) for f in folds])
    dump(output / 'frozen-study.json', freeze)
    print(json.dumps(dict(stage='FROZEN_BEFORE_MODEL_FIT', profile=profile, folds=len(folds))), flush=True)
    if args.prepare_only:
        return
    selector = TrainFactorSelector(snapshot, refs, FactorSelectionConfig(**config['selection']))
    results, predictions, statuses = {}, [], {}
    environment = {p: importlib.metadata.version(p) for p in ['numpy', 'pandas', 'pyarrow', 'scikit-learn', 'lightgbm', 'xgboost']}
    tracker = ExperimentTracker(output / 'tracker')
    code = {k: subprocess.check_output(['git', 'rev-parse', v], cwd=ROOT, text=True).strip() for k,v in [('commit','HEAD'),('tree','HEAD^{tree}')]}
    code['source_hashes'] = {str(p.relative_to(ROOT)): file_hash(p) for p in [Path(__file__), ROOT / 'src/factorlab/research/real_model_study.py']}
    for group in ('A', 'B', 'C'):
        models = ('factor_score',) if group == 'C' else tuple(config['parameters'])
        for model in models:
            key = group + '_' + model
            selected_names = ('RAW_MOM20',) if group == 'C' else names
            runner = make_runner(config, model, selected_names, selector if group == 'B' else None)
            result = runner.run(data, calendar=research_calendar)
            results[key] = result.predictions
            for table in ['fold_summary', 'validation_metrics', 'diagnostic_audit', 'factor_selection', 'factor_quality', 'factor_redundancy', 'coefficients', 'feature_importance']:
                value = getattr(result, table)
                if not value.empty:
                    value.to_csv(output / (key + '-' + table + '.csv'), index=False)
            prediction = result.predictions.rename(columns={'date':'signal_date','score':'prediction'}).copy()
            prediction['model_id'] = key
            prediction['data_identity'] = data_identity
            prediction['feature_identity'] = json_hash(dict(snapshot=snapshot.snapshot_hash, features=selected_names, group=group))
            if group == 'B':
                identities = {int(row['fold']): json_hash(dict(snapshot=snapshot.snapshot_hash,
                    selected=row['selected_features'], directions=row['directions']))
                    for row in result.factor_selection.to_dict('records')}
                prediction['feature_identity'] = prediction.fold.map(identities)
            path_flags = data[KEYS + ['possible_action_jump_path']].rename(columns={'date':'signal_date'})
            prediction = prediction.merge(path_flags, on=['signal_date','symbol'], validate='one_to_one')
            predictions.append(prediction)
            statuses[key] = 'COMPLETED'
            tracker.create_experiment(key, dict(model=model, parameters=config['parameters'][model],
                factors=[asdict(r) for r in refs if r.factor_id in selected_names],
                dataset=dict(data_identity=data_identity, sample_hash=freeze['sample_hash']), label=dict(definition=config['label']),
                windows={stage:[freeze['folds'][0][stage][0],freeze['folds'][-1][stage][1]] for stage in ['train','validation','test']},
                walk_forward=config['splitter'], costs=dict(kind='PREDICTION_ONLY_NO_EXECUTION')))
            tracker.append_run(key, key + '-run', code=code, environment=environment, command=sys.argv,
                status='SUCCEEDED', qualification='REAL_CANDIDATE',
                validation=dict(sample_hash=frame_hash(result.predictions[['fold', *KEYS, TARGET]]), evaluation='RAW_OPEN5_RANKIC'),
                artifacts=dict(folds=dict(path=str((output / (key + '-fold_summary.csv')).resolve()))))
            print(key + ' completed', flush=True)
    common_hash = common_predictions(results)
    predictions = pd.concat(predictions, ignore_index=True)
    predictions.to_parquet(output / 'predictions.parquet', index=False)
    daily = daily_metrics(predictions, config['top_k'], config['minimum_cross_section'])
    daily.to_csv(output / 'daily.csv', index=False)
    for name, groups in [('overall',['model_id']), ('by-fold',['model_id','fold']), ('by-year',['model_id','year'])]:
        summarize_daily(daily, groups).to_csv(output / (name + '.csv'), index=False)
    paired_daily(daily).to_csv(output / 'paired.csv', index=False)
    # Diagnostic only, never a rule used to refit or select the principal models.
    clean = predictions.loc[~predictions.possible_action_jump_path]
    sensitivity = daily_metrics(clean, config['top_k'], config['minimum_cross_section'])
    summarize_daily(sensitivity, ['model_id']).to_csv(output / 'jump-path-sensitivity.csv', index=False)
    dump(output / 'result.json', dict(status='P8_02_REAL_WALKFORWARD_COMPLETED', models=statuses,
        common_test_hash=common_hash, test_rows_per_model=len(next(iter(results.values()))),
        prediction_sha256=file_hash(output / 'predictions.parquet'), environment=environment, code=code,
        elapsed_seconds=time.perf_counter()-clock, financial_boundary=config['financial_boundary']))
    print('P8_02_REAL_WALKFORWARD_COMPLETED', flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default=str(ROOT / 'configs/research/p8-02-real-models.json'))
    parser.add_argument('--bigquant-root', required=True)
    parser.add_argument('--membership-zip', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--prepare-only', action='store_true')
    run(parser.parse_args())
