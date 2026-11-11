"""P9-D04 exact-window valuation qualification and frozen fold replay."""
import argparse
from datetime import date
import io
import json
from pathlib import Path
import sys
import time
import zipfile

import pandas as pd
import psutil

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT / 'tools/research'))
from factorlab.data.evidence import json_bytes, sha256
from factorlab.data.market_evidence_sidecar import SCOPED_SUSPENSIONS, POLICY, load_sidecar
from factorlab.dataset.identity import file_hash
from factorlab.backtest.execution import AShareExecutionBacktester, ExecutionBacktestConfig
from factorlab.backtest.fees import FeeRule, FeeSchedule
from factorlab.errors import SchemaError
from factorlab.research.oos_execution_recovery import qualify_valuations, audit_frozen_fees, recovery_bars
from factorlab.research.oos_execution_study import execution_bars
from factorlab.strategies.real_shared_portfolio import audit_ledger
from run_real_model_walkforward import load_inputs, dump

NOTICES = {
    '002185.SZ': ('AN202509241749841477', 'AN202510161763333821', '2025-09-24'),
    '600095.SH': ('AN202503161644424709', 'AN202503281648738377', '2025-03-16'),
    '601615.SH': ('AN202601121816954404', 'AN202601221818287212', '2026-01-12'),
}


def qualify_windows(args):
    manifest = (args.original / 'input-manifest.json').read_bytes()
    expected = {r['member']: r['actual_sha256'] for r in json.loads(manifest)['entries']}
    native = json.loads((args.native / 'native.json').read_bytes())
    prior_pin = json.loads((ROOT / 'docs/validation/p9-d03/verification.json').read_bytes())['private_artifact_sha256']
    if file_hash(args.previous / 'combined-sidecar.json') != prior_pin['combined-sidecar.json']:
        raise ValueError('PREVIOUS_SIDECAR_DRIFT')
    payload = json.loads((args.previous / 'combined-sidecar.json').read_bytes())
    documents, observations, all_bars, receipts, policies = {}, {}, [], [], []
    native_frames = {'cn_stock_real_bar1d': [], 'cn_stock_status': []}
    config = json.loads((args.study / 'config.json').read_bytes())
    # Previous grants retain their original document and observation bindings.
    sources = json.loads((ROOT / 'docs/validation/p9-d01/public-sources.json').read_bytes())
    for source in sources['documents']:
        documents[source['sha256']] = (args.evidence_root / source['local_name']).read_bytes()
    with zipfile.ZipFile(args.bigquant_root / 'stock.zip') as archive:
        for entry in native['shards']:
            data = archive.read(entry['member'])
            if sha256(data) != expected[entry['member']] or sha256(data) != entry['sha256']:
                raise ValueError('FROZEN_NATIVE_SOURCE_DRIFT')
            documents[sha256(data)] = data
            table = entry['member'].split('/')[1]
            columns = (['date', 'instrument', 'open', 'high', 'low', 'close', 'volume', 'adjust_factor']
                       if table == 'cn_stock_real_bar1d' else
                       ['date', 'instrument', 'st_status', 'suspended', 'exdr', 'is_risk_warning'])
            native_frames[table].append(pd.read_parquet(io.BytesIO(data), columns=columns,
                filters=[('instrument', 'in', list(SCOPED_SUSPENSIONS))]))
        for table in ('cn_stock_real_bar1d', 'cn_stock_status'):
            member = f'stock/{table}/2023-05-01_2023-05-31.parquet'
            content = archive.read(member)
            if sha256(content) != expected[member]:
                raise ValueError('PREVIOUS_SOURCE_DRIFT')
            documents[sha256(content)] = content
        for symbol in ('002408.SZ', '000540.SZ'):
            policy_path = ('docs/validation/p9-d03/valuation-policy.json' if symbol == '000540.SZ'
                           else 'docs/validation/p9-d01/public-sources.json')
            policy = json.loads((ROOT / policy_path).read_bytes())
            observation = policy['observation'] if symbol == '000540.SZ' else policy['observation_identity']
            observations[symbol] = observation
            capsule = json_bytes(observation)
            documents[sha256(capsule)] = capsule
            member = 'stock/cn_stock_real_bar1d/2023-04-01_2023-04-30.parquet'
            content = archive.read(member)
            if sha256(content) != observation['source_sha256']:
                raise ValueError('PREVIOUS_OBSERVATION_SOURCE_DRIFT')
            documents[sha256(content)] = content
        risk = json.loads((ROOT / 'docs/validation/p9-d03/valuation-policy.json').read_bytes())['suspension_evidence']
        documents[risk['document_sha256']] = (args.evidence_root / 'issuer-risk/document.pdf').read_bytes()
    for symbol, (observed_day, start, resume, days) in SCOPED_SUSPENSIONS.items():
        lower = observed_day[:7] + '-01'
        upper = str(pd.Period(resume[:7], freq='M').end_time.date())
        selected = []
        for table in ('cn_stock_real_bar1d', 'cn_stock_status'):
            frame = pd.concat(native_frames[table])
            selected.append(frame.loc[frame.instrument.eq(symbol) & frame.date.between(lower, upper)]
                            .sort_values('date').reset_index(drop=True))
        prices, status = selected
        bars = prices.merge(status, on=['date', 'instrument'], validate='one_to_one').rename(
            columns={'instrument': 'symbol', **{c: 'raw_' + c for c in ('open', 'high', 'low', 'close')}})
        observed = bars.loc[(bars.date < start) & bars.volume.gt(0) & bars.raw_close.gt(0)].iloc[-1]
        gap = bars.loc[bars.date.isin(pd.to_datetime(days))]
        boundary = bars.loc[bars.date.between(observed_day, resume)]
        resumed = bars.loc[bars.date.eq(resume)].iloc[0]
        if (str(observed.date.date()) != observed_day or len(gap) != len(days)
                or not gap[['raw_open', 'raw_high', 'raw_low', 'raw_close']].isna().all().all()
                or not gap.volume.eq(0).all() or not gap.suspended.eq(1).all()
                or not boundary.exdr.eq(0).all() or boundary.adjust_factor.nunique() != 1
                or resumed.volume <= 0 or resumed.suspended != 0):
            raise ValueError('WINDOW_PRICE_STATE_OR_ACTION_CONFLICT: ' + symbol)
        member = next(r['member'] for r in native['shards'] if 'cn_stock_real_bar1d/' in r['member']
                      and r['member'].split('/')[-1][:7] == observed_day[:7])
        observation = dict(security=symbol, date=observed_day, field='raw_close',
                           status='OBSERVED_RAW_CLOSE', price=str(observed.raw_close),
                           volume=int(observed.volume), source_sha256=expected[member])
        observations[symbol] = observation
        capsule = json_bytes(observation)
        documents[sha256(capsule)] = capsule
        initial, resumption, publication = NOTICES[symbol]
        source_documents = []
        for code in (initial, resumption):
            content = (args.native / code / 'document.pdf').read_bytes()
            documents[sha256(content)] = content
            source_documents.append(sha256(content))
        suspension = dict(security=symbol, start_date=start, resume_date=resume,
                          document_sha256=source_documents[0], publication_time=publication + 'T23:59:59+08:00',
                          evidence_class='ISSUER_CONFIRMED_SUSPENSION', verification_status='DOCUMENT_CONFIRMED')
        resumption_fact = dict(security=symbol, resume_date=resume, document_sha256=source_documents[1],
                               verification_status='DOCUMENT_CONFIRMED')
        for day in days:
            payload['candidates'].append(dict(target_security=symbol, target_date=day, field='valuation_mark',
                original_value=None, candidate_value=observation['price'], evidence_class='MODEL_VALUATION_ASSUMPTION',
                source_uri=f'bigquant://cn_stock_real_bar1d/{symbol}/{observed_day}#raw_close',
                source_document_sha256=sha256(capsule), source_publication_time=publication + 'T23:59:59+08:00',
                known_at_status='EX_POST_ONLY', retrospectively_verified_at='2026-11-12T00:00:00+08:00',
                verification_status='OBSERVATION_IDENTITY_BOUND', consumption_scope='RESEARCH_CANDIDATE_ONLY',
                limitation='Independent model mark only; no raw price, signal or execution authorization',
                valuation_policy=POLICY, valuation_policy_version='1', valuation_status='MODEL_VALUATION_ASSUMPTION',
                nontradable=True, valuation_source_date=observed_day, valuation_source_price=observation['price'],
                valuation_source_price_identity=observation['source_sha256'], suspension_evidence=suspension,
                resumption_evidence=resumption_fact))
        policies.append(dict(security=symbol, dates=list(days), observation=observation,
                             suspension_evidence=suspension, resumption_evidence=resumption_fact,
                             policy=POLICY, version='1', enabled=True, allowed_uses=['EX_POST_RESEARCH_BOOK_VALUATION'],
                             raw_close=None, nontradable=True, source_manifest_sha256=sha256(manifest)))
        all_bars.append(bars)
    dump(args.output / 'combined-sidecar.json', payload)
    evidence = load_sidecar(args.output / 'combined-sidecar.json', source_manifest=manifest,
        originals={(r['target_security'], r['target_date'], r['field']): r['original_value']
                   for r in payload['candidates']}, documents=documents)
    for bars, policy in zip(all_bars, policies, strict=True):
        symbol, observed_day = policy['security'], policy['observation']['date']
        resume, days = policy['resumption_evidence']['resume_date'], policy['dates']
        before = bars.loc[bars.date.lt(observed_day)].date.max()
        dates = pd.DatetimeIndex([before, pd.Timestamp(observed_day), *pd.to_datetime(days), pd.Timestamp(resume)])
        targets = pd.DataFrame(dict(date=[before, pd.Timestamp(observed_day)], symbol=symbol, target_weight=[.1, 0.]))
        engine = AShareExecutionBacktester(ExecutionBacktestConfig(initial_cash=config['initial_cash'],
            benchmark_open_price_column='raw_open', benchmark_close_price_column='raw_close'),
            FeeSchedule([FeeRule(effective_from=date(1990, 1, 1), **config['fees'])]))
        source = execution_bars(bars)
        result = engine.run_target_weights(targets, source.loc[source.date.isin(dates)],
            pd.DataFrame(dict(date=dates, raw_open=1., raw_close=1.)))
        qualified = qualify_valuations(result, source, evidence, None, observations=observations)
        audit_frozen_fees(result, config)
        audit = audit_ledger(result, source.loc[source.date.isin(dates)], config['initial_cash'], valuation_prices=qualified)
        if (qualified.valuation_assumption_used.sum() != len(days)
                or result.trades.execution_date.isin(pd.to_datetime(days)).any()
                or not result.rejections.reason.eq('suspended').any()):
            raise SchemaError('SUSPENSION_CONSUMPTION_CONFLICT')
        for name in ('positions', 'nav', 'trades', 'rejections'):
            getattr(result, name).to_parquet(args.output / f'{symbol}-{name}.parquet', index=False)
        qualified.to_parquet(args.output / f'{symbol}-qualifications.parquet', index=False)
        receipts.append(dict(security=symbol, scope='ISOLATED_REAL_DATA_CONTRACT_PATH_NOT_FROZEN_ACCOUNT',
                             assumption_days=len(days), suspension_fills=0, rejected_exit='suspended',
                             ledger=audit, resumed_valuation='OBSERVED_RAW_CLOSE'))
    dump(args.output / 'policies-private.json', policies)
    dump(args.output / 'qualification-results.json', receipts)
    return evidence, observations


def replay(args, evidence, observations):
    config = json.loads((args.study / 'config.json').read_bytes())
    pin = json.loads((ROOT / 'docs/validation/p9-01/result.json').read_bytes())['private_artifact_sha256']
    for name in ('candidate-predictions.parquet', 'B_lightgbm-fold-0/targets.parquet', 'B_lightgbm-fold-0/decisions.csv'):
        if file_hash(args.study / name) != pin[name]:
            raise ValueError('FROZEN_PLAN_OR_PREDICTION_DRIFT: ' + name)
    identity = json.loads((args.study / 'inference-identity.json').read_bytes())
    lo, hi = identity['folds'][0]['test']
    train = json.loads((args.original / 'config.json').read_bytes())
    bars, members, calendar, entries, symbols = load_inputs(args, train, window=(lo, hi))
    expected = {r['member']: r['actual_sha256'] for r in json.loads((args.original / 'input-manifest.json').read_bytes())['entries']}
    if symbols != json.loads((args.original / 'input-manifest.json').read_bytes())['symbols']:
        raise ValueError('FROZEN_UNIVERSE_DRIFT')
    if any(r['actual_sha256'] != expected[r['member']] for r in entries):
        raise ValueError('FROZEN_SOURCE_DRIFT')
    dates = calendar[(calendar >= lo) & (calendar < hi)]
    targets = pd.read_parquet(args.study / 'B_lightgbm-fold-0/targets.parquet')
    decisions = pd.read_csv(args.study / 'B_lightgbm-fold-0/decisions.csv', parse_dates=['date'])
    bars = recovery_bars(bars, evidence, cutoff=json.loads((ROOT / 'configs/research/p9-02/recovery.json').read_bytes())['st_cutoff'])
    prefix = args.previous / 'stage-b-lightgbm-fold-0-prefix'
    prefix_plan = targets.loc[targets.date.le('2023-05-09')].reset_index(drop=True)
    if not prefix_plan.equals(pd.read_parquet(prefix / 'targets.parquet')):
        raise ValueError('VERIFIED_PREFIX_TARGET_DRIFT')
    receipt = dict(start=lo, end_exclusive=hi, source_sessions=len(dates), full_fold=False,
                   frozen_targets=True, retrained=False, factors_recomputed=False,
                   prior_prefix_target_identity=True, prior_prefix_ledger_identity='NOT_RETURNED_IF_ENGINE_ABORTS')
    def qualifier(result, source):
        for name in ('trades', 'positions', 'nav', 'rejections', 'target_comparison'):
            old = pd.read_parquet(prefix / f'{name}.parquet')
            now = getattr(result, name)
            day_field = 'execution_date' if name in ('trades', 'rejections', 'target_comparison') else 'date'
            pd.testing.assert_frame_equal(now.loc[now[day_field].le('2023-05-09'), old.columns].reset_index(drop=True), old)
        receipt['prior_prefix_ledger_identity'] = 'MATCHED'
        audit_frozen_fees(result, config)
        return qualify_valuations(result, source, evidence, None, observations=observations)
    config = dict(config, valuation_policy=POLICY)
    engine = AShareExecutionBacktester(ExecutionBacktestConfig(initial_cash=config['initial_cash'],
        benchmark_open_price_column='raw_open', benchmark_close_price_column='raw_close'),
        FeeSchedule([FeeRule(effective_from=date(1990, 1, 1), **config['fees'])]))
    session = engine.start_session(bars.loc[bars.date.isin(dates)],
        pd.DataFrame(dict(date=dates, raw_open=1., raw_close=1.)), signal_date=dates[0])
    grouped = {day: group for day, group in targets.groupby('date', sort=True)}
    receipt.update(status='COMPLETED', full_fold=True)
    for signal_day in dates[:-1]:
        if signal_day in grouped:
            group = grouped[signal_day]
            session.submit_targets(group.set_index('symbol').target_weight.to_dict())
        try:
            session.advance()
        except SchemaError as error:
            receipt.update(status='BLOCKED', full_fold=False, reason=str(error))
            break
    completed_day = session.snapshot.as_of.normalize()
    result = session.result
    # A failed open may append partial fills before the error. Only closed
    # sessions enter the accepted diagnostic receipt and ledger audit.
    from dataclasses import replace
    fields = {}
    for name in ('trades', 'positions', 'nav', 'rejections', 'target_comparison'):
        frame = getattr(result, name)
        day_field = 'execution_date' if name in ('trades', 'rejections', 'target_comparison') else 'date'
        fields[name] = frame.loc[frame[day_field].le(completed_day)].reset_index(drop=True)
    result = replace(result, **fields)
    qualified = qualifier(result, bars)
    audit = audit_ledger(result, bars.loc[bars.date.le(completed_day)], config['initial_cash'], valuation_prices=qualified)
    for name in fields:
        getattr(result, name).to_parquet(args.output / f'fold-0-{name}.parquet', index=False)
    qualified.to_parquet(args.output / 'fold-0-qualifications.parquet', index=False)
    receipt.update(last_completed_session=str(completed_day.date()), ledger=audit,
                   new_accepted_execution_sessions=int(((dates > '2023-05-09') & (dates <= completed_day)).sum()))
    dump(args.output / 'replay-result.json', receipt)
    print(json.dumps(receipt), flush=True)


def main(args):
    start = time.perf_counter()
    args.output.mkdir(parents=True, exist_ok=False)
    evidence, observations = qualify_windows(args)
    replay(args, evidence, observations)
    process = psutil.Process()
    memory = process.memory_info()
    dump(args.output / 'resources.json', dict(elapsed_seconds=time.perf_counter() - start,
         peak_working_set_bytes=memory.peak_wset, processes=1, numerical_threads=1,
         output_bytes=sum(p.stat().st_size for p in args.output.rglob('*') if p.is_file())))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('original', 'study', 'previous', 'native', 'bigquant-root', 'membership-zip', 'evidence-root', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    main(parser.parse_args())
