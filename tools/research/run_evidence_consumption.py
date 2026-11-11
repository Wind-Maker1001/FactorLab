"""Consume bound P9 evidence using saved predictions only; never train or permit UNKNOWN orders."""
import argparse
from datetime import date
import io
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import zipfile

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT / 'tools/research'))
from factorlab.backtest.execution import AShareExecutionBacktester, ExecutionBacktestConfig
from factorlab.backtest.fees import FeeRule, FeeSchedule
from factorlab.backtest.rules import PriceLimitSchedule
from factorlab.data.evidence import json_bytes, sha256
from factorlab.data.market_evidence_sidecar import ALLOWED, POLICY, SAFETY, load_sidecar
from factorlab.data.hub.contracts import QueryResult, SourceMetadata
from factorlab.data.hub.price_consumption import PriceConsumptionError, require_fields
from factorlab.dataset.identity import file_hash, json_hash
from factorlab.errors import SchemaError
from factorlab.research.oos_execution_study import make_targets, verify_predictions
from factorlab.research.evidence_consumption_study import (
    OBSERVED_ONLY, execution_bars, value_positions, audit_valuation_ledger,
)


def dump(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False,
                               default=str) + '\n', encoding='utf-8')


def require(condition, message):
    if not condition:
        raise ValueError(message)


def load_bound_evidence(args):
    sidecar = json.loads(args.sidecar.read_bytes())
    manifest = args.manifest.read_bytes()
    require(sha256(manifest) == sidecar['source_manifest_sha256'], 'SOURCE_MANIFEST_DRIFT')
    frozen_bytes = args.frozen_result.read_bytes().replace(b'\r\n', b'\n')
    require(sha256(frozen_bytes) == sidecar['frozen_result_lf_sha256'], 'FROZEN_RESULT_DRIFT')
    frozen = json.loads(frozen_bytes)
    entries = {e['member']: e for e in json.loads(manifest)['entries']}
    documents, shards = {}, []
    with zipfile.ZipFile(args.stock) as archive:
        for table in ('cn_stock_real_bar1d', 'cn_stock_status'):
            for month, end in (('04', '30'), ('05', '31')):
                member = f'stock/{table}/2023-{month}-01_2023-{month}-{end}.parquet'
                content = archive.read(member)
                digest = sha256(content)
                receipt = json.loads(archive.read(member + '.receipt.json'))
                require(digest == entries[member]['actual_sha256'] == receipt['sha256'], 'EVIDENCE_SHARD_DRIFT')
                documents[digest] = content
                shards.append(dict(member=member, sha256=digest))
                if table == 'cn_stock_status' and month == '05':
                    state = pd.read_parquet(io.BytesIO(content), filters=[
                        ('instrument', '==', '000540.SZ'), ('date', '==', pd.Timestamp('2023-05-08'))])
                    require(len(state) == 1, 'TARGET_STATE_KEY_DRIFT')
                    for field in ('st_status', 'suspended', 'is_risk_warning'):
                        require(state.iloc[0][field] == sidecar['original_supplier_state'][field], 'SUPPLIER_STATE_DRIFT')
    sources = json.loads(args.sources.read_bytes())
    for source in sources['documents']:
        content = (args.source_root / source['local_name']).read_bytes()
        require(sha256(content) == source['sha256'], 'PUBLIC_DOCUMENT_DRIFT')
        documents[source['sha256']] = content
    observation = sources['observation_identity']
    documents[sha256(json_bytes(observation))] = json_bytes(observation)
    evidence = load_sidecar(args.sidecar, source_manifest=manifest,
        originals={key: None for key in ALLOWED}, documents=documents)
    return evidence, frozen, entries, shards, {'002408.SZ': observation}


def load_source(args, entries, symbols, start, end):
    """Project only calendar, raw prices and statuses from overlapping shards."""
    identities = []
    def read(path, category, table, columns, selected=None):
        bound = end if table == 'trading_days' or args.requalify_fold0 else min(end, pd.Timestamp('2023-05-08'))
        frames = []
        with zipfile.ZipFile(path) as archive:
            for member in archive.namelist():
                if not member.startswith(f'{category}/{table}/') or not member.endswith('.parquet'):
                    continue
                receipt = json.loads(archive.read(member + '.receipt.json'))
                if pd.Timestamp(receipt['window_end']) < start or pd.Timestamp(receipt['window_start']) > bound:
                    continue
                content = archive.read(member)
                digest = sha256(content)
                require(digest == entries[member]['actual_sha256'] == receipt['sha256'], 'EXECUTION_SOURCE_DRIFT')
                filters = [('date', '>=', start), ('date', '<=', bound)]
                if selected is not None:
                    filters.append(('instrument', 'in', selected))
                frames.append(pd.read_parquet(io.BytesIO(content), columns=columns, filters=filters))
                identities.append(dict(member=member, sha256=digest))
        return pd.concat(frames, ignore_index=True)
    sessions = read(args.bigquant_root / 'common.zip', 'common', 'trading_days', ['date', 'market_code'])
    calendar = pd.DatetimeIndex(pd.to_datetime(sessions.loc[sessions.market_code.eq('CN'), 'date']).drop_duplicates().sort_values())
    bars = read(args.stock, 'stock', 'cn_stock_real_bar1d',
        ['date', 'instrument', 'open', 'high', 'low', 'close', 'volume'], symbols)
    states = read(args.stock, 'stock', 'cn_stock_status',
        ['date', 'instrument', 'suspended', 'st_status'], symbols)
    bars = bars.rename(columns={'instrument': 'symbol', **{p: 'raw_' + p for p in ('open', 'high', 'low', 'close')}})
    states = states.rename(columns={'instrument': 'symbol'})
    bars['date'], states['date'] = pd.to_datetime(bars.date), pd.to_datetime(states.date)
    require(not bars.duplicated(['date', 'symbol']).any(), 'DUPLICATE_RAW_BAR')
    bars = bars.merge(states, on=['date', 'symbol'], how='left', validate='one_to_one')
    return bars, calendar, identities


PRICE_FIELDS = {name: 'raw_' + name for name in ('open', 'high', 'low', 'close')}
SOURCE_ID = 'BigQuant/cn_stock_real_bar1d'


def qualified_replay_prices(result, *, identity, cutoff=None):
    """Consume unchanged keys only; code continuity never supplies price evidence."""
    frame = result.data
    days = pd.to_datetime(frame.date)
    invalid = (
        (frame.symbol.eq('300114.SZ') & days.ge('2025-02-17'))
        | (frame.symbol.eq('302132.SZ') & days.lt('2025-02-17'))
        | (frame.symbol.eq('600068.SH') & days.ge('2021-09-13'))
        | (frame.symbol.eq('601868.SH') & days.lt('2021-09-28'))
    )
    if invalid.any():
        raise PriceConsumptionError(['DATED_SECURITY_IDENTITY_INVALID_OR_EQUITY_SETTLEMENT_UNPROVEN'])
    for original, field in PRICE_FIELDS.items():
        semantic = result.metadata.field_semantics.get(field, {})
        if semantic.get('original_field') != original:
            raise PriceConsumptionError([field + '/ORIGINAL_FIELD_IDENTITY_UNKNOWN'])
    return require_fields(result, {field: ('EXECUTION', 'CNY/share')
                                  for field in PRICE_FIELDS.values()},
                          identity=identity, cutoff=cutoff)


def replay_source_result(bars, manifest_bytes):
    """Bind byte provenance without promoting it to economic qualification."""
    version = sha256(manifest_bytes)
    # TODO: obtain version-bound native-field units, raw/fill policy, scope,
    # evidence and availability contracts before qualifying this frozen source.
    return QueryResult(bars, SourceMetadata(SOURCE_ID, version,
        provenance={'source_manifest_sha256': version},
        field_semantics={field: {'original_field': original}
                         for original, field in PRICE_FIELDS.items()}))


def same_frame(actual, expected, keys):
    pd.testing.assert_frame_equal(actual.sort_values(keys).reset_index(drop=True),
        expected.loc[:, actual.columns].sort_values(keys).reset_index(drop=True), check_dtype=False,
        check_exact=True)


def run(args):
    evidence, frozen, entries, shards, observations = load_bound_evidence(args)
    pins = frozen['private_artifact_sha256']
    def checked(name):
        path = args.study / name
        require(file_hash(path) == pins[name], 'PRIVATE_ARTIFACT_DRIFT: ' + name)
        return path
    config = json.loads(checked('config.json').read_bytes())
    identity = json.loads(checked('inference-identity.json').read_bytes())
    require(json_hash(config) == frozen['identity']['config_hash'], 'EXECUTION_CONFIG_DRIFT')
    train_config = json.loads((args.original / 'config.json').read_bytes())
    require(json_hash(train_config) == frozen['identity']['training_config_hash'], 'TRAIN_CONFIG_DRIFT')
    require(train_config == json.loads((ROOT / 'configs/research/p8-02-real-models.json').read_bytes()), 'ORIGINAL_CONFIG_DRIFT')
    verify_predictions(args.original / 'predictions.parquet', config)
    predictions = pd.read_parquet(checked('candidate-predictions.parquet'), filters=[('fold', '==', 0)])
    manifest = json.loads((args.original / 'input-manifest.json').read_bytes())
    symbols = manifest['symbols']
    require(len(symbols) == 128 and symbols == identity['symbols'], 'SYMBOL_UNIVERSE_DRIFT')
    require(json_hash(manifest['entries']) == identity['data_identity'], 'P8_MANIFEST_DRIFT')
    test_start, test_end = identity['folds'][0]['test']
    start, end = pd.Timestamp(test_start), pd.Timestamp(test_end) - pd.Timedelta(days=1)
    # Calendar must cover the entire frozen Test window before target generation.
    bars, calendar, source_identities = load_source(args, entries, symbols, start, end)
    source = replay_source_result(bars, args.manifest.read_bytes())
    # Independent pin was verified against the sidecar before source loading.
    qualified_replay_prices(source, identity=(SOURCE_ID, evidence.source_manifest_sha256))
    require(len(calendar) > 0 and calendar[0] == start, 'FOLD_CALENDAR_DRIFT')
    observation = observations['002408.SZ']
    old = bars.loc[bars.symbol.eq('002408.SZ') & bars.date.eq(pd.Timestamp(observation['date']))]
    require(len(old) == 1 and str(old.iloc[0].raw_close) == observation['price']
        and int(old.iloc[0].volume) == observation['volume'], 'OBSERVATION_DRIFT')
    output = args.output
    output.mkdir(parents=True, exist_ok=False)
    study_config = json.loads(args.config.read_bytes())
    require(study_config['original_config_hash'] == json_hash(config), 'STUDY_CONFIG_DRIFT')
    require(study_config['candidate_prediction_sha256'] == pins['candidate-predictions.parquet'], 'STUDY_PREDICTION_DRIFT')
    require(study_config['valuation_policy'] == POLICY and study_config['minimum_window_end'] == '2023-05-08', 'STUDY_SCOPE_DRIFT')
    dump(output / 'config.json', study_config)
    dates = calendar[calendar <= pd.Timestamp('2023-05-08')]
    execution = execution_bars(bars, evidence, cutoff='2023-05-05T15:00:00+08:00')
    target_bar = bars.loc[bars.symbol.eq('000540.SZ') & bars.date.eq(pd.Timestamp('2023-05-08'))].iloc[0]
    previous = bars.loc[bars.symbol.eq('000540.SZ') & bars.date.eq(pd.Timestamp('2023-05-05'))].iloc[0]
    blocked, bounds = PriceLimitSchedule.a_share_default().blocks('000540.SZ', '2023-05-08', 'sell',
        float(target_bar.raw_open), float(previous.raw_close), is_st=True, tolerance_bps=5)
    require(blocked and bounds.lower == .60, 'ST_PRICE_LIMIT_CONTRACT_DRIFT')
    state_consumption = dict(security='000540.SZ', date='2023-05-08', raw_st_status=int(target_bar.st_status),
        is_st=True, cutoff='2023-05-05T15:00:00+08:00', previous_close=float(previous.raw_close),
        open=float(target_bar.raw_open), lower_limit=bounds.lower, sell_blocked=bool(blocked),
        diagnostic_only=True, execution_permission='UNKNOWN', order_executability='ORDER_EXECUTABLE_UNVERIFIED')
    accounts, comparisons, marks, navs = [], [], [], []
    for model in ('B_factor_score', 'B_ridge', 'B_lightgbm'):
        name = model + '-fold-0'
        account_output = output / name
        account_output.mkdir()
        scores = predictions.loc[predictions.model_id.eq(model)]
        targets, decisions = make_targets(scores, calendar, symbols, config, model)
        saved_targets = pd.read_parquet(checked(name + '/targets.parquet'))
        same_frame(targets, saved_targets, ['date', 'symbol'])
        targets = targets.loc[targets.date.isin(dates)]
        targets.to_parquet(account_output / 'targets.parquet', index=False)
        decisions.loc[decisions.date.isin(dates)].to_csv(account_output / 'decisions.csv', index=False)
        engine = AShareExecutionBacktester(ExecutionBacktestConfig(initial_cash=config['initial_cash'],
            benchmark_open_price_column='raw_open', benchmark_close_price_column='raw_close'),
            FeeSchedule([FeeRule(effective_from=date(1990, 1, 1), **config['fees'])]))
        try:
            result = engine.run_target_weights(targets, execution.loc[execution.date.isin(dates)],
                pd.DataFrame(dict(date=dates, raw_open=1., raw_close=1.)))
        except SchemaError as error:
            if model != 'B_lightgbm' or str(error) != 'execution status is missing for 2023-05-08/000540.SZ':
                raise
            accounts.append(dict(model_id=model, fold=0, status='BLOCKED', reason=str(error),
                execution_permit=False, signal_time_trading_state='UNKNOWN', total_return=None))
            break
        for field in ('trades', 'positions', 'nav', 'rejections', 'target_comparison'):
            getattr(result, field).to_parquet(account_output / (field + '.parquet'), index=False)
        saved = {}
        for field in ('trades', 'positions', 'nav', 'rejections', 'target_comparison'):
            saved[field] = pd.read_parquet(checked(name + '/' + field + '.parquet'))
        common_trades = saved['trades'].loc[saved['trades'].execution_date.isin(dates)]
        same_frame(result.trades, common_trades, ['execution_date', 'symbol', 'side'])
        suspended_trades = result.trades.loc[result.trades.symbol.eq('002408.SZ') &
            pd.to_datetime(result.trades.execution_date).between('2023-04-26', '2023-05-04')]
        require(suspended_trades.empty, 'SUSPENSION_EXECUTION_CONFLICT')
        comparisons.append(dict(model_id=model, fold=0, targets_unchanged=True,
            full_target_sha256=pins[name + '/targets.parquet'], suspension_trades=0,
            trades_unchanged=True, trades=len(result.trades), end=str(dates[-1].date())))
        audits = {}
        for scope, ledger in [('MINIMUM_REPLAY', result)] + ([('FULL_SAVED_FOLD0', SimpleNamespace(**saved))] if args.requalify_fold0 else []):
            for policy in (OBSERVED_ONLY, POLICY):
                valuations = value_positions(ledger.positions, bars, evidence, policy=policy, previous_observations=observations)
                marks.append(valuations.assign(model_id=model, scope=scope))
                if valuations.valuation_status.eq('UNVALUABLE_POSITION').any():
                    audits[scope + '/' + policy] = dict(status='BLOCKED', reason='UNVALUABLE_POSITION')
                else:
                    audits[scope + '/' + policy] = audit_valuation_ledger(ledger, bars, config['initial_cash'], evidence,
                        policy=policy, previous_observations=observations)
            navs.append(ledger.nav.assign(model_id=model, scope=scope))
        accounts.append(dict(model_id=model, fold=0, status='REPLAYED', audits=audits, total_return=None))
    require(accounts[-1]['status'] == 'BLOCKED', 'EXPECTED_STRICT_UNKNOWN_BOUNDARY_NOT_REACHED')
    pd.DataFrame(comparisons).to_csv(output / 'comparison.csv', index=False)
    pd.concat(marks, ignore_index=True).to_csv(output / 'valuations.csv', index=False)
    pd.concat(navs, ignore_index=True).to_csv(output / 'daily-nav.csv', index=False)
    remaining = [dict(model_id=m, fold=f, status='NOT_RUN', total_return=None)
        for f in range(3) for m in config['models'] if (f, m) not in {(0, a['model_id']) for a in accounts}]
    summary = dict(schema='P9_D02_EVIDENCE_CONSUMPTION_V1', status='P9_D02_BLOCKED',
        mode='SAVED_PREDICTIONS_ONLY', accounts=accounts, remaining_accounts=remaining,
        state_consumption=state_consumption,
        first_date=str(dates[0].date()), last_date=str(dates[-1].date()),
        source_manifest_sha256=evidence.source_manifest_sha256, evidence_shards=shards,
        source_shards=source_identities, candidate_prediction_sha256=pins['candidate-predictions.parquet'],
        total_return=None, three_fold_metrics=None, total_return_status='TOTAL_RETURN_UNAVAILABLE',
        execution_permit=False, formal_admission=False, safety=list(SAFETY))
    dump(output / 'result.json', summary)
    print(json.dumps(dict(status=summary['status'], accounts=len(accounts), remaining=len(remaining))))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('study', 'original', 'source-root', 'stock', 'bigquant-root', 'output'):
        parser.add_argument('--' + name, required=True, type=Path)
    parser.add_argument('--config', type=Path, default=ROOT / 'configs/research/p9-d02-evidence-consumption.json')
    parser.add_argument('--requalify-fold0', action='store_true')
    parser.add_argument('--sidecar', type=Path, default=ROOT / 'configs/research/p9-d01/candidate.json')
    parser.add_argument('--sources', type=Path, default=ROOT / 'docs/validation/p9-d01/public-sources.json')
    parser.add_argument('--manifest', type=Path, help='Defaults to the original P8 input manifest')
    parser.add_argument('--frozen-result', type=Path, default=ROOT / 'docs/validation/p9-01/result.json')
    args = parser.parse_args()
    if args.manifest is None:
        args.manifest = args.original / 'input-manifest.json'
    run(args)


if __name__ == '__main__':
    main()
