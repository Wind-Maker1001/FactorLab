"""Continue the frozen D04 LightGBM fold using scoped historical ST evidence."""
import argparse
from datetime import date
import io
import json
from pathlib import Path
import sys
import time
import zipfile

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT / 'tools/research'))
from factorlab.backtest.execution import AShareExecutionBacktester, ExecutionBacktestConfig
from factorlab.backtest.fees import FeeRule, FeeSchedule
from factorlab.data.evidence import json_bytes, sha256
from factorlab.data.market_evidence_sidecar import (
    POLICY, ST_EXECUTION_DATES, ST_EXECUTION_KNOWN, ST_EXECUTION_NOTICE_SHA256,
    ST_EXECUTION_PUBLICATION_DATE, ST_EXECUTION_SECURITY, load_sidecar,
)
from factorlab.dataset.identity import file_hash
from factorlab.errors import SchemaError
from factorlab.research.oos_execution_recovery import (
    apply_issuer_execution_st, audit_frozen_fees, qualify_valuations, recovery_bars,
)
from factorlab.strategies.real_shared_portfolio import audit_ledger
from p9_execution_preflight import KEYS, PRICES, checked, dump, read_table


VALIDATION = ROOT / 'docs/validation'
LEDGER_FIELDS = ('trades', 'positions', 'nav', 'rejections', 'target_comparison')


def load_market(root, manifest, start, end, documents):
    pins = {(row['archive'], row['member']): row['actual_sha256'] for row in manifest['entries']}
    symbols, entries = manifest['symbols'], []
    with zipfile.ZipFile(root / 'common.zip') as archive:
        calendar = read_table(archive, 'common', 'trading_days', ['date', 'market_code'],
                              start, end, None, pins, entries, documents)
    with zipfile.ZipFile(root / 'stock.zip') as archive:
        price_columns = ['date', 'instrument', 'open', 'high', 'low', 'close', 'volume']
        state_columns = ['date', 'instrument', 'suspended', 'st_status', 'is_risk_warning']
        prices = read_table(archive, 'stock', 'cn_stock_real_bar1d', price_columns,
                            start, end, symbols, pins, entries, documents)
        states = read_table(archive, 'stock', 'cn_stock_status', state_columns,
                            start, end, symbols, pins, entries, documents)
    prices = prices.rename(columns=dict(zip(price_columns[2:6], PRICES, strict=True)))
    bars = prices.merge(states[KEYS + state_columns[2:]], on=KEYS, how='left', validate='one_to_one')
    return bars, pd.DatetimeIndex(calendar.date.sort_values()), entries


def load_evidence(args, bars, manifest, payload, documents, sources, observations):
    for source in sources['documents']:
        documents[source['sha256']] = (args.evidence_root / source['local_name']).read_bytes()
    native = bars.set_index(['symbol', 'date'])
    for symbol, observation in observations.items():
        prior = native.loc[(symbol, pd.Timestamp(observation['date']))]
        if (str(prior.raw_close) != observation['price'] or prior.volume != observation['volume']
                or prior.suspended != 0 or prior.source_sha256 != observation['source_sha256']):
            raise SchemaError(f'PRE_SUSPENSION_OBSERVATION_DRIFT: {symbol}/{observation["date"]}')
        capsule = json_bytes(observation)
        documents[sha256(capsule)] = capsule

    notice = next(source for source in sources['documents'] if source['sha256'] == ST_EXECUTION_NOTICE_SHA256)
    for day in ST_EXECUTION_DATES:
        payload['candidates'].append(dict(target_security=ST_EXECUTION_SECURITY, target_date=day,
            field='is_st', original_value=None, candidate_value=True, evidence_class='ISSUER_RISK_DESIGNATION',
            source_uri=notice['uri'], source_document_sha256=notice['sha256'],
            source_publication_time=ST_EXECUTION_PUBLICATION_DATE, known_at_status=ST_EXECUTION_KNOWN,
            retrospectively_verified_at='2026-11-12', verification_status='DOCUMENT_CONFIRMED',
            consumption_scope='RESEARCH_CANDIDATE_ONLY', limitation='Issuer designation for frozen replay status only'))

    originals = {}
    for row in payload['candidates']:
        symbol, day, field = row['target_security'], row['target_date'], row['field']
        source = native.loc[(symbol, pd.Timestamp(day))]
        if field == 'is_st' and source.st_status != 2:
            raise SchemaError(f'TARGETED_ST_SOURCE_DRIFT: {day}/{symbol}')
        value = source.raw_close if field in ('suspension_confirmed', 'valuation_mark') else None
        originals[symbol, day, field] = None if pd.isna(value) else float(value)
    path = args.output / 'combined-sidecar.json'
    dump(path, payload)
    return load_sidecar(path, source_manifest=manifest, originals=originals, documents=documents)


def qualify_session_prices(snapshot, bars, marks):
    """Check observed marks and fills only for securities consumed this session."""
    symbols = set(marks.loc[~marks.valuation_assumption_used.astype(bool), 'symbol'])
    symbols.update(fill['symbol'] for fill in snapshot.last_fills)
    source = bars.set_index('symbol').reindex(sorted(symbols))
    prices = source[PRICES + ['volume']].to_numpy(dtype=float)
    observed = (np.isfinite(prices).all(axis=1) & (prices > 0).all(axis=1)
                & source.raw_low.le(source[['raw_open', 'raw_close']].min(axis=1))
                & source.raw_high.ge(source[['raw_open', 'raw_close']].max(axis=1))
                & source.suspended.eq(0))
    day = snapshot.as_of.date()
    if not observed.all():
        raise SchemaError(f'UNQUALIFIED_OBSERVED_BAR: {day}/{observed.index[~observed][0]}')
    for fill in snapshot.last_fills:
        bar = source.loc[fill['symbol']]
        if bar.raw_open != fill['execution_price'] or bar.trade_status != 1 or pd.isna(bar.is_st):
            raise SchemaError(f'UNQUALIFIED_EXECUTION_OPEN: {day}/{fill["symbol"]}')


def advance(session, targets):
    signal_day = session.snapshot.as_of.normalize()
    if signal_day in targets:
        session.submit_targets(targets[signal_day])
    return session.advance()


def continue_fold(session, dates, targets, bars, evidence, observations, accepted):
    """Retain detached ledger frames from the last session to pass qualification."""
    qualified, blocker = [], None
    for day in dates:
        previous = session.snapshot
        try:
            snapshot = advance(session, targets)
            candidate = session.result
            source = bars.loc[bars.date.eq(day)]
            marks = qualify_valuations(candidate, source, evidence, None,
                                       observations=observations, session_date=day)
            qualify_session_prices(snapshot, source, marks)
        except SchemaError as error:
            blocker = dict(date=str(day.date()), reason=str(error), prior_000540_shares=sum(
                holding.shares for holding in previous.holdings if holding.symbol == ST_EXECUTION_SECURITY))
            break
        accepted = candidate
        qualified.append(marks)
    return accepted, qualified, blocker


def run(args, *, continuation=None):
    import psutil

    started = time.perf_counter()
    p9 = json.loads((VALIDATION / 'p9-01/result.json').read_bytes())
    pins = p9['private_artifact_sha256']
    config = json.loads(checked((args.study / 'config.json').read_bytes(), pins['config.json']))
    identity = json.loads(checked((args.study / 'inference-identity.json').read_bytes(), pins['inference-identity.json']))
    target_name = 'B_lightgbm-fold-0/targets.parquet'
    target_bytes = checked((args.study / target_name).read_bytes(), pins[target_name])
    targets = pd.read_parquet(io.BytesIO(target_bytes))
    frozen_hashes = {name: pins[name] for name in ('config.json', 'inference-identity.json', target_name)}
    for name in ('candidate-predictions.parquet', 'B_lightgbm-fold-0/decisions.csv'):
        frozen_hashes[name] = file_hash(args.study / name)
        if frozen_hashes[name] != pins[name]:
            raise ValueError('FROZEN_ARTIFACT_DRIFT: ' + name)
    original_prediction_hash = file_hash(args.original / 'predictions.parquet')
    if original_prediction_hash != p9['identity']['original_prediction_sha256']:
        raise ValueError('ORIGINAL_PREDICTION_DRIFT')

    d03 = json.loads((VALIDATION / 'p9-d03/evidence.json').read_bytes())
    manifest_bytes = checked((args.original / 'input-manifest.json').read_bytes(), d03['source_manifest_sha256'])
    manifest = json.loads(manifest_bytes)
    if manifest['symbols'] != identity['symbols']:
        raise ValueError('FROZEN_SECURITY_SET_DRIFT')
    d04 = json.loads((VALIDATION / 'p9-d04/verification.json').read_bytes())['private_receipts']
    baseline = json.loads(checked((args.d04 / 'replay-result.json').read_bytes(), d04['replay-result.json']))
    start, end = map(pd.Timestamp, identity['folds'][0]['test'])
    baseline_day = pd.Timestamp(baseline['last_completed_session'])
    reference_directory, reference_label = args.d04, 'D04'
    if continuation is not None:
        reference_directory = continuation
        reference_label = 'D05'
        verification = json.loads((VALIDATION / 'p9-d05/verification.json').read_bytes())
        for name, digest in verification['private_accepted_artifacts_sha256'].items():
            checked((continuation / name).read_bytes(), digest)
        baseline = json.loads((VALIDATION / 'p9-d05/execution-results.json').read_bytes())
        baseline_day = pd.Timestamp(baseline['last_qualified_session'])
    sources = json.loads((VALIDATION / 'p9-d01/public-sources.json').read_bytes())
    policy = json.loads((VALIDATION / 'p9-d03/valuation-policy.json').read_bytes())
    observations = {'002408.SZ': sources['observation_identity'], ST_EXECUTION_SECURITY: policy['observation']}
    sidecar_name = 'combined-sidecar.json'
    sidecar_pin = json.loads((VALIDATION / 'p9-d03/verification.json').read_bytes())['private_artifact_sha256'][sidecar_name]
    payload = json.loads(checked((args.d03 / sidecar_name).read_bytes(), sidecar_pin))
    references = {item['source_sha256'] for item in observations.values()}
    references.update(row['source_document_sha256'] for row in payload['candidates'])
    documents = dict.fromkeys(references)
    bars, dates, entries = load_market(args.bigquant_root, manifest, start, end - pd.Timedelta(days=1), documents)
    if (len(dates) != baseline['source_sessions'] or dates[0] != start or dates.has_duplicates
            or not targets.date.isin(dates[:-1]).all()):
        raise ValueError('FROZEN_CALENDAR_OR_PLAN_DRIFT')

    args.output.mkdir(parents=True, exist_ok=False)
    retained = {digest: content for digest, content in documents.items() if content is not None}
    evidence = load_evidence(args, bars, manifest_bytes, payload, retained, sources, observations)
    cutoff = json.loads((ROOT / 'configs/research/p9-02/recovery.json').read_bytes())['st_cutoff']
    bars = apply_issuer_execution_st(recovery_bars(bars, evidence, cutoff=cutoff), evidence)
    engine = AShareExecutionBacktester(ExecutionBacktestConfig(initial_cash=config['initial_cash'],
        benchmark_open_price_column='raw_open', benchmark_close_price_column='raw_close'),
        FeeSchedule([FeeRule(effective_from=date(1990, 1, 1), **config['fees'])]))
    benchmark = pd.DataFrame(dict(date=dates, raw_open=1., raw_close=1.))
    session = engine.start_session(bars, benchmark, signal_date=start)
    plan = {day: group.set_index('symbol').target_weight.to_dict() for day, group in targets.groupby('date')}
    for _ in dates[(dates > start) & (dates <= baseline_day)]:
        advance(session, plan)
    prior = session.result
    prior_marks = qualify_valuations(prior, bars, evidence, None, observations=observations)
    reference_frames = {name: getattr(prior, name) for name in LEDGER_FIELDS}
    reference_frames['qualifications'] = prior_marks
    for name, actual in reference_frames.items():
        expected = pd.read_parquet(reference_directory / f'fold-0-{name}.parquet')
        pd.testing.assert_frame_equal(actual, expected, check_exact=True, obj=f'{reference_label} {name}')

    accepted, new_marks, blocker = continue_fold(session, dates[dates > baseline_day], plan,
                                                  bars, evidence, observations, prior)
    qualified = pd.concat([prior_marks, *new_marks], ignore_index=True)
    completed_day = accepted.nav.date.iloc[-1]
    audit_frozen_fees(accepted, config)
    ledger = audit_ledger(accepted, bars.loc[bars.date.le(completed_day)], config['initial_cash'],
                          valuation_prices=qualified)
    for name in LEDGER_FIELDS:
        getattr(accepted, name).to_parquet(args.output / f'fold-0-{name}.parquet', index=False)
    qualified.to_parquet(args.output / 'fold-0-qualifications.parquet', index=False)

    added = accepted.nav.loc[accepted.nav.date.gt(baseline_day), 'date']
    first_day = pd.Timestamp(ST_EXECUTION_DATES[0])
    first = bars.loc[bars.symbol.eq(ST_EXECUTION_SECURITY) & bars.date.eq(first_day)].iloc[0]
    fills = accepted.trades.loc[accepted.trades.symbol.eq(ST_EXECUTION_SECURITY)]
    rejections = accepted.rejections.loc[accepted.rejections.symbol.eq(ST_EXECUTION_SECURITY)]
    task = 'P9_D06' if continuation is not None else 'P9_D05'
    status = (task + '_FULL_FOLD_ACCEPTED' if blocker is None else
              task + '_PARTIAL' if len(added) else task + '_BLOCKED')
    receipt = dict(status=status, source_sessions=len(dates), prior_qualified_sessions=len(prior.nav) + 1,
        accepted_sessions=len(accepted.nav) + 1, account_close_rows=len(accepted.nav),
        newly_qualified_dates=[str(day.date()) for day in added], last_qualified_session=str(completed_day.date()),
        prior_tables_exact=list(reference_frames), blocker=blocker, ledger=ledger, frozen_fee_receipts='PASSED',
        new_assumed_marks=int(qualified.loc[qualified.date.gt(baseline_day), 'valuation_assumption_used'].sum()),
        may15_state=dict(st_status=int(first.st_status), suspended=int(first.suspended),
            is_risk_warning=int(first.is_risk_warning), issuer_is_st=bool(first.is_st)),
        new_000540_fills=int(fills.execution_date.gt(baseline_day).sum()),
        new_000540_rejection_counts=rejections.loc[rejections.execution_date.gt(baseline_day), 'reason'].value_counts().to_dict(),
        frozen_hashes=frozen_hashes, original_prediction_sha256=original_prediction_hash,
        source_manifest_sha256=evidence.source_manifest_sha256, source_shards=entries,
        private_receipt_sha256={path.name: file_hash(path) for path in args.output.iterdir()})
    if continuation is not None:
        holding = accepted.positions.loc[accepted.positions.symbol.eq(ST_EXECUTION_SECURITY)
                                         & accepted.positions.date.eq(completed_day)].iloc[0]
        observation = bars.loc[bars.symbol.eq(ST_EXECUTION_SECURITY)
                               & bars.date.eq(completed_day)].iloc[0]
        receipt['last_000540_observation'] = dict(date=str(completed_day.date()),
            shares=int(holding.shares), raw_close=float(observation.raw_close),
            volume=float(observation.volume), suspended=int(observation.suspended),
            source_sha256=observation.source_sha256)
        if blocker is not None:
            failed = bars.loc[bars.symbol.eq(ST_EXECUTION_SECURITY)
                              & bars.date.eq(pd.Timestamp(blocker['date']))].iloc[0]
            receipt['blocked_source'] = dict(raw_close=None if pd.isna(failed.raw_close)
                else float(failed.raw_close), suspended=int(failed.suspended),
                source_sha256=failed.source_sha256)
    dump(args.output / 'replay-result.json', receipt)
    resources = dict(wall_seconds=time.perf_counter() - started,
        peak_working_set_bytes=psutil.Process().memory_info().peak_wset,
        replay_output_bytes=sum(path.stat().st_size for path in args.output.iterdir()), python_processes=1)
    print(json.dumps(dict(status=status, new_sessions=len(added), last_qualified_session=str(completed_day.date()),
                          blocker=blocker, resources=resources)), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('original', 'study', 'd03', 'd04', 'evidence-root', 'bigquant-root', 'output'):
        parser.add_argument('--' + name, required=True, type=Path)
    run(parser.parse_args())
