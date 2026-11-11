"""Audit frozen fold-zero price demand without manufacturing source qualification."""
import argparse
import csv
import json
from pathlib import Path
import sys
import time
import zipfile

import pandas as pd
import psutil

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT / 'tools/research')]
from factorlab.data.evidence import sha256
from factorlab.data.hub.price_consumption import PriceConsumptionError
from run_evidence_consumption import (SOURCE_ID, PRICE_FIELDS, SOURCE_PRICE_CONTRACT,
    load_source, qualified_replay_prices, replay_source_result)

START, END = pd.Timestamp('2023-03-13'), pd.Timestamp('2024-03-12')
SAFETY = ['NO LIVE MONEY', 'PIT_UNVERIFIED', 'HFQ_UNVERIFIED', 'FORMAL_ADMISSION_BLOCKED']


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')


def write_csv(path, rows, fields):
    with path.open('w', newline='', encoding='utf-8') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def overlapping(member):
    if not member.endswith('.parquet'):
        return False
    lo, hi = Path(member).stem.split('_')
    return pd.Timestamp(lo) <= END and pd.Timestamp(hi) >= START


def price_demand(bars):
    """Describe observations; positive volume is a candidate, not permission."""
    prices = bars[list(PRICE_FIELDS.values())]
    finite = prices.notna().all(axis=1) & prices.abs().lt(float('inf')).all(axis=1)
    suspended = bars.suspended.eq(1)
    positive = bars.volume.gt(0)
    return dict(rows=len(bars), symbols=bars.symbol.nunique(),
        sessions=bars.date.nunique(), complete_finite_ohlc=int(finite.sum()),
        positive_volume_complete_ohlc_not_suspended_candidates=int((finite & positive & bars.suspended.eq(0)).sum()),
        suspended_rows=int(suspended.sum()), missing_ohlc_rows=int((~finite).sum()),
        zero_or_missing_volume_rows=int((~positive).sum()),
        unknown_suspension_rows=int((~bars.suspended.isin([0, 1])).sum()),
        meaning='Observed input classes only; neither volume nor supplier state is certified here')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--original', type=Path, required=True)
    parser.add_argument('--bigquant-root', type=Path, required=True)
    parser.add_argument('--study', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--preflight', action='store_true')
    args = parser.parse_args()
    started = time.perf_counter()
    manifest_bytes = (args.original / 'input-manifest.json').read_bytes()
    manifest = json.loads(manifest_bytes)
    version = sha256(manifest_bytes)
    reviewed = json.loads(SOURCE_PRICE_CONTRACT.read_bytes())
    if version != reviewed['data_version']:
        raise ValueError('SOURCE_MANIFEST_DRIFT')
    entries = {item['member']: item for item in manifest['entries']}
    symbols = manifest['symbols']
    if len(symbols) != 128:
        raise ValueError('FROZEN_UNIVERSE_DRIFT')
    partitions, metadata = [], []
    total_compressed = total_payload = 0
    dq = {r['member']: r for r in csv.DictReader((ROOT / 'docs/validation/dq-02b/partition-quality.csv').open(encoding='utf-8'))}
    for archive_name, tables in [('stock.zip', ['cn_stock_real_bar1d', 'cn_stock_status']),
                                 ('common.zip', ['trading_days'])]:
        with zipfile.ZipFile(args.bigquant_root / archive_name) as archive:
            for table in tables:
                prefix = ('common' if table == 'trading_days' else 'stock') + '/' + table + '/'
                for member in entries:
                    if not member.startswith(prefix) or not overlapping(member):
                        continue
                    info = archive.getinfo(member)
                    receipt_bytes = archive.read(member + '.receipt.json')
                    receipt = json.loads(receipt_bytes)
                    if receipt['sha256'] != entries[member]['actual_sha256']:
                        raise ValueError('RECEIPT_MANIFEST_DRIFT')
                    total_compressed += info.compress_size
                    total_payload += info.file_size
                    metadata.append(dict(member=member, receipt=receipt))
                    if table == 'cn_stock_real_bar1d':
                        old = dq[member]
                        partitions.append(dict(member=member, sha256=receipt['sha256'],
                            receipt_sha256=sha256(receipt_bytes), compressed_bytes=info.compress_size,
                            payload_bytes=info.file_size, dq02b_status=old['status'],
                            dq02b_evidence_id=old['evidence_id'], dq02b_rows=old['rows'],
                            schema='NOT_READ', provenance='RECEIPT_MANIFEST_MATCH',
                            source_semantic_version='UNKNOWN', scoped_research_qualified_days=0))
    budget = dict(planned_price_partitions=len(partitions), all_input_partitions=len(metadata),
        compressed_read_bytes=total_compressed, parquet_payload_bytes=total_payload,
        memory_soft_limit_bytes=4 * 1024**3, temporary_disk_limit_bytes=512 * 1024**2,
        maximum_python_processes=1,
        stop_conditions=['Manifest/receipt/member mismatch', 'More than 13 price partitions',
                         'Parquet payload above 256 MiB', 'Observed process RSS above 4 GiB'],
        full_field_rescan=False, dq02b_reuse='Existing full-field statistics; not historical semantic evidence')
    print(json.dumps(budget, indent=2))
    if len(partitions) != 13 or total_payload > 256 * 1024**2:
        raise ValueError('READ_BUDGET_EXCEEDED')
    if args.preflight:
        print(json.dumps(metadata[:1], ensure_ascii=False, indent=2))
        return
    # Existing reader hashes each selected member, then projects only frozen keys.
    args.stock = args.bigquant_root / 'stock.zip'
    args.requalify_fold0 = True
    bars, calendar, identities = load_source(args, entries, symbols, START, END)
    if len(calendar) != 243:
        raise ValueError('FROZEN_CALENDAR_DRIFT')
    if psutil.Process().memory_info().rss > budget['memory_soft_limit_bytes']:
        raise ValueError('MEMORY_BUDGET_EXCEEDED')
    verified_members = {item['member']: item['sha256'] for item in identities}
    for part in partitions:
        if verified_members[part['member']] != part['sha256']:
            raise ValueError('PARTITION_IDENTITY_DRIFT')
        part['provenance'] = 'PARTITION_PROVENANCE_VERIFIED'
    source = replay_source_result(bars, manifest_bytes, source_identities=identities)
    try:
        qualified_replay_prices(source, identity=(SOURCE_ID, version))
    except PriceConsumptionError as error:
        whole = dict(status='REJECTED', gaps=list(error.gaps), rows_preserved=len(bars))
    else:
        raise ValueError('UNEXPECTED_UNREVIEWED_SCOPE_ACCEPTANCE')
    approved_keys = set(reviewed['observations'])
    keys = bars.symbol + '/' + bars.date.dt.strftime('%Y-%m-%d')
    approved = bars.loc[keys.isin(approved_keys)].copy()
    result = replay_source_result(approved, manifest_bytes, source_identities=identities)
    admitted = qualified_replay_prices(result, identity=(SOURCE_ID, version))
    pd.testing.assert_frame_equal(admitted, approved, check_exact=True)
    if len(admitted) != 4:
        raise ValueError('G03_APPROVED_SCOPE_DRIFT')
    for part in partitions:
        part['scoped_research_qualified_days'] = sum(v['member'] == part['member'] for v in reviewed['observations'].values())
        # Schema identity is pinned by the already-hashed Parquet member and G03 evidence.
        part['schema'] = 'DQ02B_PHYSICAL_SCHEMA_STATISTICS_REUSED'
    demand = price_demand(bars)
    frozen = json.loads((ROOT / 'docs/validation/p9-01/result.json').read_bytes())
    pins = frozen['private_artifact_sha256']
    def checked(name):
        path = args.study / name
        if sha256(path.read_bytes()) != pins[name]:
            raise ValueError('FROZEN_ARTIFACT_DRIFT: ' + name)
        return path
    targets = pd.read_parquet(checked('B_lightgbm-fold-0/targets.parquet'))
    target_window = targets.loc[pd.to_datetime(targets.date).between(START, END)]
    target_keys = target_window[['date', 'symbol']].drop_duplicates()
    target_inputs = target_keys.merge(bars, on=['date', 'symbol'], how='left', validate='one_to_one', indicator=True)
    predictions = pd.read_parquet(checked('candidate-predictions.parquet'), filters=[('fold', '==', 0), ('model_id', '==', 'B_lightgbm')])
    args.output.mkdir(parents=True, exist_ok=False)
    coverage = dict(status='P10_G04_BLOCKED', source_id=SOURCE_ID, data_version=version,
        frozen_scope=dict(start=str(START.date()), end_exclusive='2024-03-13', sessions=243,
                          symbols=128, maximum_symbol_session_keys=31104),
        observed_input_demand=demand, saved_lightgbm_target_rows=len(target_window),
        saved_lightgbm_target_unique_keys=len(target_keys),
        target_keys_without_source_bar=int(target_inputs['_merge'].eq('left_only').sum()),
        target_input_demand=price_demand(target_inputs),
        frozen_lightgbm_prediction_rows=len(predictions),
        qualified_symbols=admitted.symbol.nunique(), qualified_security_days=len(admitted),
        qualified_field_objects=16, fields=list(PRICE_FIELDS.values()), purpose='EXECUTION',
        unit='CNY/share', qualified_price_partitions=2, inspected_price_partitions=len(partitions),
        independently_checked_security_days=4, independent_numeric_checks='INHERITED_G03_EXACT_16',
        new_qualified_security_days=0, new_qualified_execution_sessions=0,
        consumer_input_rows_outside_existing_price_scope=len(bars)-len(admitted),
        actual_execution_price_required_unqualified_keys=None,
        actual_execution_price_required_unqualified_keys_reason='Orders/holdings and validated non-price state are needed; loaded rows and target observations are not executed orders',
        safety=SAFETY)
    write_json(args.output / 'qualified-coverage.json', coverage)
    write_json(args.output / 'consumer-results.json', dict(full_fold_price_entry=whole,
        g03=dict(status='ACCEPTED', rows=4, fields=4, values_exactly_unchanged=True,
                 original_observation_digests_checked=True),
        frozen_members_and_dates_changed=False, suspended_rows_removed=False,
        new_execution_sessions=0, account_replay='NOT_STARTED_PRICE_GATE_REJECTED'))
    blockers = dict(status='P10_G04_BLOCKED', blockers=[
        dict(id='HISTORICAL_SEMANTIC_VERSION_UNBOUND', scope='13 frozen price partitions',
             needed='Supplier versioned field definition or explicit export-batch attestation covering OHLC currency/unit, raw/fill policy, observation dates and these receipt hashes'),
        dict(id='NONPRICE_STATE_CONTRACT_INCOMPLETE',
             needed='Validated state scope before any non-price dispatch; supplier suspended alone cannot grant this'),
        dict(id='VOLUME_DISCREPANCY_UNRESOLVED', qualification='UNKNOWN',
             needed='Explain two G03 archive discrepancies beyond displayed rounding, with unit and revision/aggregation lineage'),
        dict(id='LIMIT_REFERENCE_AND_UNIT_UNVERIFIED',
             fields=['amount','pre_close','upper_limit','lower_limit','adjust_factor'], qualification='UNKNOWN'),
        dict(id='PIT_UNKNOWN', needed='Historical first supplier availability; export time is insufficient')],
        p9_d05='Merged #109: 45/243 sessions on its conditional path, then 000540 2023-05-19 valuation blocked; not a P10 price qualification')
    write_json(args.output / 'remaining-blockers.json', blockers)
    write_csv(args.output / 'partition-qualification.csv', partitions, list(partitions[0]))
    checks = [dict(symbol=k.split('/')[0], date=k.split('/')[1], fields='raw_open|raw_high|raw_low|raw_close',
                   method='G03 inherited exact decimal exchange archive comparison',
                   evidence_id=v['evidence_id'], result='16_TOTAL_EXACT_VALUES', new_check=False)
              for k, v in reviewed['observations'].items()]
    write_csv(args.output / 'independent-price-checks.csv', checks, list(checks[0]))
    write_json(args.output / 'verification.json', dict(status='P10_G04_BLOCKED',
        elapsed_seconds=time.perf_counter()-started, budget=budget,
        process_memory=psutil.Process().memory_info()._asdict(),
        market_data_written_bytes=0, original_prices_changed=False,
        tests='PENDING', safety=SAFETY))


if __name__ == '__main__':
    main()
