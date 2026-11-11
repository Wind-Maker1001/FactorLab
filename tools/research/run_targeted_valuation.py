"""P9-D03: one evidence grant, existing Stage A, one frozen Stage B prefix."""
import argparse
from dataclasses import asdict
import io
import json
from pathlib import Path
import sys
import zipfile

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT / 'tools/research'))
from factorlab.data.evidence import json_bytes, sha256
from factorlab.data.market_evidence_sidecar import POLICY
from factorlab.dataset.identity import file_hash
from factorlab.errors import SchemaError
from factorlab.research.oos_execution_recovery import recovery_bars, qualify_valuations, audit_frozen_fees
from run_real_model_walkforward import load_inputs, dump
from run_oos_execution import execute
from run_oos_recovery import evidence_inputs, stage_a


def native_target(path, vendor, expected_hash):
    from openpyxl import load_workbook
    member = 'TRDNEW_Dalyr.xlsx' if vendor == 'CSMAR' else '2023/个股日回报率-2023_1.xlsx'
    with zipfile.ZipFile(path) as archive:
        content = archive.read(member)
        dictionary = archive.read('TRDNEW_Dalyr[DES][xlsx].txt') if vendor == 'CSMAR' else None
    if sha256(content) != expected_hash:
        raise ValueError('ANCILLARY_SOURCE_DRIFT: ' + vendor)
    workbook = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    selected, scanned = [], 0
    for row in workbook.active.iter_rows(min_row=4 if vendor == 'CSMAR' else 3,
            max_col=27 if vendor == 'CSMAR' else 19, values_only=True):
        scanned += 1
        code = str(row[0]).zfill(6)
        if code > '000540':
            break
        day = str(row[1 if vendor == 'CSMAR' else 2])[:10]
        if code == '000540' and '2023-04-25' <= day <= '2023-05-05':
            selected.append(dict(date=day, values=list(row)))
    workbook.close()
    if vendor == 'CSMAR':
        by_date = {r['date']: r['values'] for r in selected}
        old, suspended, resumed = (by_date[d] for d in ('2023-04-28', '2023-05-04', '2023-05-05'))
        if (float(old[8]) != .66 or float(old[9]) != 361474595 or str(old[4]) != '0'
                or str(suspended[4]) != '1' or float(suspended[9]) != 0
                or any(float(suspended[i]) != .66 for i in (5, 6, 7, 8))
                or str(resumed[4]) != '0' or float(resumed[9]) <= 0):
            raise ValueError('CSMAR_OBSERVATION_OR_FILL_CONFLICT')
    return dict(vendor=vendor, member=member, sha256=sha256(content),
        dictionary_sha256=sha256(dictionary) if dictionary is not None else None,
        scanned_prefix_rows=scanned, selected=selected)


def run(args):
    policy = json.loads((ROOT / 'docs/validation/p9-d03/valuation-policy.json').read_bytes())
    if policy['enabled'] is not True or policy['policy'] != POLICY:
        raise SchemaError('EXPLICIT_CARRY_POLICY_REQUIRED')
    args.output.mkdir(parents=True, exist_ok=False)
    original_manifest = (args.original / 'input-manifest.json').read_bytes()
    expected = {e['member']: e['actual_sha256'] for e in json.loads(original_manifest)['entries']}
    old_candidate = json.loads((ROOT / 'configs/research/p9-d01/candidate.json').read_bytes())
    target_frames, states, identities = [], [], []
    with zipfile.ZipFile(args.bigquant_root / 'stock.zip') as archive:
        for table, destination in [('cn_stock_real_bar1d', target_frames), ('cn_stock_status', states)]:
            for month, end in [('04', '30'), ('05', '31')]:
                member = f'stock/{table}/2023-{month}-01_2023-{month}-{end}.parquet'
                content = archive.read(member)
                if sha256(content) != expected[member]:
                    raise ValueError('TARGET_SOURCE_DRIFT')
                identities.append(dict(member=member, sha256=sha256(content)))
                destination.append(pd.read_parquet(io.BytesIO(content), filters=[
                    ('instrument', 'in', ['000540.SZ', '002408.SZ']),
                    ('date', '>=', pd.Timestamp('2023-04-24')), ('date', '<=', pd.Timestamp('2023-05-09'))]))
    prices = pd.concat(target_frames).rename(columns={'instrument': 'symbol',
        **{x: 'raw_' + x for x in ('open', 'high', 'low', 'close')}})
    status = pd.concat(states).rename(columns={'instrument': 'symbol'})
    bars = prices.merge(status, on=['date', 'symbol'], validate='one_to_one')
    target = bars.loc[bars.symbol.eq('000540.SZ')].set_index('date').sort_index()
    old, suspended, resumed = (target.loc[d] for d in ('2023-04-28', '2023-05-04', '2023-05-05'))
    previous = target.loc[(target.index < pd.Timestamp('2023-05-04'))
        & target.volume.gt(0) & target.raw_close.gt(0)]
    if (previous.index.max() != pd.Timestamp('2023-04-28') or old.raw_close != .66 or old.volume != 361474595 or old.suspended != 0
            or suspended[['raw_open', 'raw_high', 'raw_low', 'raw_close']].notna().any()
            or suspended.volume != 0 or suspended.suspended != 1
            or resumed.volume <= 0 or resumed.suspended != 0
            or not target.loc['2023-04-28':'2023-05-05', 'exdr'].eq(0).all()
            or target.loc['2023-04-28':'2023-05-05', 'adjust_factor'].nunique() != 1):
        raise ValueError('TARGET_PRICE_STATUS_OR_CORPORATE_ACTION_CONFLICT')
    ancillary = [native_target(path, vendor, old_candidate['ancillary_sha256'][vendor])
        for vendor, path in [('CSMAR', args.csmar), ('CNRDS', args.cnrds)]]
    inventory = pd.read_csv(args.inventory, usecols=['file_name', 'absolute_path'])
    tdx = inventory.loc[inventory.file_name.eq('sz000540.day')]
    if not tdx.empty:
        raise ValueError('NEW_TDX_SOURCE_REQUIRES_REVIEW')
    dump(args.output / 'native-evidence.json', dict(shards=identities, ancillary=ancillary,
        tdx='SOURCE_MISSING_IN_EXISTING_INVENTORY'))
    observation = policy['observation']
    candidate = dict(target_security='000540.SZ', target_date='2023-05-04', field='valuation_mark',
        original_value=None, candidate_value=observation['price'], evidence_class='MODEL_VALUATION_ASSUMPTION',
        source_uri='bigquant://cn_stock_real_bar1d/000540.SZ/2023-04-28#raw_close',
        source_document_sha256=sha256(json_bytes(observation)),
        source_publication_time='2026-11-07T12:20:29+00:00', known_at_status='UNKNOWN',
        retrospectively_verified_at='2026-11-11T00:00:00+00:00',
        verification_status='OBSERVATION_IDENTITY_BOUND', consumption_scope='RESEARCH_CANDIDATE_ONLY',
        limitation='Ex-post valuation assumption only; no signal, raw price or execution permission',
        valuation_policy=POLICY, valuation_policy_version='1', valuation_status='MODEL_VALUATION_ASSUMPTION',
        nontradable=True, valuation_source_date=observation['date'],
        valuation_source_price=observation['price'], valuation_source_price_identity=observation['source_sha256'],
        suspension_evidence=policy['suspension_evidence'])
    old_candidate['candidates'].append(candidate)
    dump(args.output / 'combined-sidecar.json', old_candidate)
    args.recovery = json.loads((ROOT / 'configs/research/p9-02/recovery.json').read_bytes())
    args.recovery['sidecar'] = str((args.output / 'combined-sidecar.json').resolve())
    evidence, prior = evidence_inputs(args, bars, additional_observations=[observation])
    observations = {prior['security']: prior, observation['security']: observation}
    config = json.loads((ROOT / args.recovery['frozen_execution_config']).read_bytes())
    if json.loads((args.study / 'config.json').read_bytes()) != config:
        raise ValueError('FROZEN_EXECUTION_CONFIG_DRIFT')
    config = dict(config, valuation_policy=POLICY)
    value = evidence.carry('000540.SZ', '2023-05-04', shares=138800, raw_close=None,
        previous_observation=observation)
    dump(args.output / 'evidence-result.json', dict(status='P9_D03_EVIDENCE_ACCEPTED',
        valuation=asdict(value), source_manifest_sha256=sha256(original_manifest),
        shards=identities, ancillary=[{k:v for k,v in a.items() if k != 'selected'} for a in ancillary],
        tdx='SOURCE_MISSING_IN_EXISTING_INVENTORY', safety=policy['safety']))
    adapted = recovery_bars(bars, evidence, cutoff=args.recovery['st_cutoff'])
    blocked = stage_a(adapted, evidence, prior, config, args.output / 'stage-a', observations=observations)
    if blocked:
        dump(args.output / 'stage-b-result.json', dict(status='NOT_RUN', blocked=blocked))
        return
    positions = pd.read_parquet(args.output / 'stage-a/000540.SZ-positions.parquet')
    trades = pd.read_parquet(args.output / 'stage-a/000540.SZ-trades.parquet')
    if (trades.execution_date.eq('2023-05-09').any()
            or not positions.loc[positions.date.eq('2023-05-09'), 'shares'].eq(138800).all()):
        raise SchemaError('REJECTED_ORDER_AUTOMATICALLY_RETRIED')
    prediction_path = args.study / 'candidate-predictions.parquet'
    if file_hash(prediction_path) != args.recovery['candidate_prediction_sha256']:
        raise ValueError('FROZEN_PREDICTION_DRIFT')
    identity = json.loads((args.study / 'inference-identity.json').read_bytes())
    fold = identity['folds'][0]
    train = json.loads((args.original / 'config.json').read_bytes())
    full_bars, _, calendar, entries, symbols = load_inputs(args, train,
        window=(fold['test'][0], policy['stage_b']['end_inclusive']))
    if symbols != json.loads(original_manifest)['symbols']:
        raise ValueError('FROZEN_UNIVERSE_DRIFT')
    if any(e['actual_sha256'] != expected[e['member']] for e in entries):
        raise ValueError('BOUNDED_SOURCE_DRIFT')
    dates = calendar[(calendar >= fold['test'][0]) & (calendar <= policy['stage_b']['end_inclusive'])]
    scores = pd.read_parquet(prediction_path, filters=[('model_id', '==', 'B_lightgbm'), ('fold', '==', 0)])
    full_bars = recovery_bars(full_bars, evidence, cutoff=args.recovery['st_cutoff'])
    def qualifier(result, source_bars):
        audit_frozen_fees(result, config)
        return qualify_valuations(result, source_bars, evidence, prior, observations=observations)
    pin = json.loads((ROOT / 'docs/validation/p9-01/result.json').read_bytes())['private_artifact_sha256']
    for name in ('targets.parquet', 'decisions.csv'):
        relative = 'B_lightgbm-fold-0/' + name
        if file_hash(args.study / relative) != pin[relative]:
            raise ValueError('FROZEN_TARGET_OR_SCHEDULE_DRIFT')
    targets = pd.read_parquet(args.study / 'B_lightgbm-fold-0/targets.parquet')
    targets = targets.loc[targets.date.isin(dates)].reset_index(drop=True)
    decisions = pd.read_csv(args.study / 'B_lightgbm-fold-0/decisions.csv', parse_dates=['date'])
    decisions = decisions.loc[decisions.date.isin(dates)].reset_index(drop=True)
    output = args.output / 'stage-b-lightgbm-fold-0-prefix'
    try:
        result, metrics, _ = execute(scores, full_bars, dates, symbols, config, 'B_lightgbm', output,
            dataset_id=identity['data_identity'], valuation_qualifier=qualifier, target_plan=(targets, decisions))
    except SchemaError as error:
        summary = dict(status='BLOCKED', reason=str(error))
    else:
        summary = dict(status=metrics['status'], ledger=metrics['ledger'],
            valuation_status=metrics['valuation_status'],
            assumption_security_days=metrics['valuation_assumption_security_days'])
    dump(args.output / 'stage-b-result.json', dict(**summary, scheme='B_lightgbm', fold=0,
        start=str(dates[0].date()), end=str(dates[-1].date()), sessions=len(dates),
        targets_unchanged=True, candidate_prediction_sha256=args.recovery['candidate_prediction_sha256'],
        full_fold=False, full_three_folds=False))
    print(json.dumps(dict(evidence='P9_D03_EVIDENCE_ACCEPTED', stage_a='PASSED', stage_b=summary), default=str), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('original', 'study', 'bigquant-root', 'evidence-root', 'membership-zip',
                 'csmar', 'cnrds', 'inventory', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    run(parser.parse_args())


if __name__ == '__main__':
    main()
