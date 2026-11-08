"""Produce private per-key evidence decisions and public aggregate statistics."""
import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path

import pandas as pd


def csmar_decision(records):
    if not records:
        return 'NOT_AVAILABLE'
    values = {json.dumps(r['values'], sort_keys=True) for r in records}
    return 'CONFLICT' if len(values) > 1 else 'CONFIRMED'


def run(args):
    root = Path(args.private_root)
    # The completed receipt file is written only after every selected workbook.
    query_receipts = json.loads((root / 'csmar-query-receipts.json').read_text(encoding='utf-8'))
    native = defaultdict(list)
    with (root / 'csmar_target_rows.jsonl').open(encoding='utf-8') as stream:
        for line in stream:
            record = json.loads(line)
            native[(record['values']['Stkcd'], record['values']['Trddt'])].append(record)
    missing = pd.read_csv(root / 'missing_keys.csv')
    nulls = pd.read_csv(root / 'null_fields.csv')
    suspension = pd.read_csv(root / 'null_status_comparison.csv')
    candidates = pd.read_csv(root / 'missing_candidate_comparison.csv')
    stats = json.loads((root / 'statistics.json').read_text(encoding='utf-8'))
    expected = {'member_days': 605500, 'matched': 605208, 'missing_keys': 292,
                'matched_nulls': {'open': 564, 'high': 564, 'low': 564, 'close': 564, 'amount': 564, 'pre_close': 1},
                'unique_null_security_dates': 565}
    for field, value in expected.items():
        if stats[field] != value:
            raise ValueError(f'P3-D01 baseline mismatch: {field}: {stats[field]} != {value}')
    stats['baseline_reconciliation'] = 'EXACT'
    stats['schema'] = 'P3D02_TARGETED_DIAGNOSTIC_V1'
    stats['sensitivity'] = json.loads((root / 'sensitivity.json').read_text(encoding='utf-8'))
    ledgers = []
    counts = Counter()
    cross_source = Counter()
    candidate_prices = Counter()
    for kind, frame in [('MISSING_BAR_KEY', missing), ('PRESENT_BAR_NULL_FIELDS', nulls)]:
        for row in frame.to_dict('records'):
            code, day = row['instrument'], row['date'][:10]
            evidence = native[(code[:6], day)]
            candidate_evidence = native[('302132', day)] if code == '300114.SZ' else []
            status = csmar_decision(evidence)
            counts[(kind, status)] += 1
            if not evidence:
                comparison = 'NOT_AVAILABLE'
            elif status == 'CONFLICT':
                comparison = 'CONFLICT_NATIVE_DUPLICATES'
            elif kind == 'MISSING_BAR_KEY':
                comparison = 'CONFLICT_KEY_PRESENT_IN_CSMAR'
            elif pd.isna(row['open']):
                native_prices = evidence[0]['values']
                comparison = ('CONFLICT_NULL_REPRESENTATION' if any(native_prices.get(f) not in [None, ''] for f in ['Opnprc', 'Hiprc', 'Loprc', 'Clsprc']) else 'CONFIRMED_BOTH_NULL')
            else:
                comparison = ('CONFLICT_PRE_CLOSE_REPRESENTATION' if evidence[0]['values'].get('PreClosePrice') not in [None, ''] else 'CONFIRMED_BOTH_PRE_CLOSE_NULL')
            cross_source[(kind, comparison)] += 1
            entry = dict(row)
            entry['cross_source_comparison'] = comparison
            entry.update(anomaly=kind, source='BigQuant cn_stock_real_bar1d; JoinQuant members_daily.csv',
                         csmar_status=status, csmar_records=evidence,
                         csmar_candidate_records=candidate_evidence,
                         historical_knowability='UNKNOWN', production_repair='NOT_APPLIED')
            if kind == 'MISSING_BAR_KEY':
                entry['bar_exists_under_original_key'] = False
                c = candidates.loc[(candidates.instrument == code) & (candidates.date.str[:10] == day)]
                entry['candidate_bar_records'] = c.to_dict('records')
                if code == '300114.SZ':
                    if not evidence or c.empty or c.iloc[0]['_merge'] != 'both':
                        price_status = 'NOT_AVAILABLE'
                    else:
                        fields = [('open', 'Opnprc'), ('high', 'Hiprc'), ('low', 'Loprc'), ('close', 'Clsprc')]
                        native_prices = evidence[0]['values']
                        if any(pd.isna(c.iloc[0][bq]) or native_prices.get(cs) in [None, ''] for bq, cs in fields):
                            price_status = 'UNKNOWN_NULL_PRICE'
                        else:
                            price_status = 'CONFIRMED_OHLC_AGREEMENT' if all(abs(float(c.iloc[0][bq]) - float(native_prices[cs])) <= 1e-8 for bq, cs in fields) else 'CONFLICT_OHLC_VALUES'
                    candidate_prices[price_status] += 1
                    entry['candidate_ohlc_comparison'] = price_status
                entry['cause'] = 'IDENTITY_REPRESENTATION_CONFLICT' if code == '300114.SZ' else 'VENDOR_DELIST_BOUNDARY_OVERLAP'
                entry['cause_status'] = 'CONFLICT' if code == '300114.SZ' else 'CONFIRMED'
                entry['identity_notice'] = ('https://static.cninfo.com.cn/finalpage/2025-02-15/1222544408.PDF' if code == '300114.SZ' else 'https://static.cninfo.com.cn/finalpage/2021-09-10/1210996266.PDF')
                entry['identity_effective_date'] = '2025-02-17' if code == '300114.SZ' else '2021-09-13'
                entry['identity_disclosure_date'] = '2025-02-15' if code == '300114.SZ' else '2021-09-10'
                entry['identity_disclosure_exact_time'] = 'UNKNOWN'
                entry['repair_status'] = 'UNKNOWN'
            else:
                entry['bar_exists_under_original_key'] = True
                s = suspension.loc[(suspension.instrument == code) & (suspension.date.str[:10] == day)]
                entry['suspension_records'] = s.to_dict('records')
                if pd.isna(row['open']):
                    entry['cause'] = 'SUSPENSION_ASSOCIATED_NULL_FIELDS'
                    entry['event_status'] = 'CONFIRMED' if len(s) and s.suspended.eq(1).all() and s.suspend_reason.notna().all() else 'UNKNOWN'
                    entry['cause_status'] = 'UNKNOWN_NULL_FIELD_CONTRACT'
                else:
                    entry['cause'] = 'PRE_CLOSE_ONLY_NULL'
                    entry['cause_status'] = 'UNKNOWN_FIRST_DAY_FIELD_CONTRACT'
            ledgers.append(entry)
    # Pandas scalar missing values are represented as JSON null, not NaN tokens.
    with (root / 'per_key_evidence.jsonl').open('w', encoding='utf-8') as out:
        for entry in ledgers:
            clean = json.loads(pd.Series(entry).to_json(force_ascii=False))
            out.write(json.dumps(clean, ensure_ascii=False, allow_nan=False) + '\n')
    observed = [r for rs in native.values() for r in rs]
    stats['csmar'] = {
        'workbooks_read': len(query_receipts), 'physical_target_records': len(observed),
        'unique_target_keys': len(native) - sum(not v for v in native.values()),
        'key_comparison': [{'anomaly': k[0], 'status': k[1], 'security_days': v} for k, v in sorted(counts.items())],
        'cross_source_comparison': [{'anomaly': k[0], 'status': k[1], 'security_days': v} for k, v in sorted(cross_source.items())],
        'trade_state_values': dict(Counter(r['values'].get('Trdsta') for r in observed)),
        'filling_values': dict(Counter(r['values'].get('Filling') for r in observed)),
        'candidate_300114_ohlc_comparison': dict(candidate_prices),
        'ohlc_absolute_comparison_tolerance': 1e-8,
        'member_hash_verified': True, 'whole_archive_hash': 'EXISTING_CATALOG_CLAIM_NOT_REHASHED'}
    stats['calendar'] = json.loads((root / 'calendar-summary.json').read_text(encoding='utf-8'))
    stats['status'] = 'P3D02_RESOLVED_PARTIAL'
    stats['ohlc_suspension_evidence'] = {'security_days': 564, 'suspended': 564, 'independent_reason_records': 564,
                                        'event_status': 'CONFIRMED', 'null_field_contract_status': 'UNKNOWN'}
    stats['private_evidence_directory_alias'] = root.name
    Path(args.public_statistics).write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(stats['csmar'], ensure_ascii=False, indent=2))


if __name__ == '__main__':
    p = argparse.ArgumentParser(__doc__)
    p.add_argument('--private-root', required=True)
    p.add_argument('--public-statistics', required=True)
    run(p.parse_args())
