"""Targeted, read-only CNRDS duplicate investigation; vendor rows remain private."""
from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
import csv
import hashlib
import json
from pathlib import Path
import sys
import time
import zipfile

import pandas as pd
import psutil

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tri_source_market_audit import workbook_rows, LIMITS


def dump(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def classify(records):
    """Compare complete native lexemes, without choosing a preferred candidate."""
    fields = sorted(set().union(*(r.keys() for r in records)))
    changed = [k for k in fields if any((k in r, r.get(k)) != (k in records[0], records[0].get(k)) for r in records[1:])]
    if any(k in changed for k in ('Scode', 'Coname', 'Trddt')):
        return 'IDENTITY_AMBIGUITY', changed
    return ('CONFLICTING_VALUES' if changed else 'EXACT_DUPLICATE'), changed


def verify_member(job):
    root, archive_path, path = job
    receipt = json.loads((root / (path.parent.name + '.json')).read_text(encoding='utf-8'))
    frame = pd.read_parquet(path)
    expected = {(r.sheet, int(r.row)): json.loads(r.native_record) for r in frame.itertuples()}
    with zipfile.ZipFile(archive_path) as archive:
        raw = archive.read(receipt['member'])
        if hashlib.sha256(raw).hexdigest() != receipt['sha256']:
            raise ValueError('native member changed: ' + receipt['member'])
        dictionary = archive.read(receipt['dictionary_member'])
        if hashlib.sha256(dictionary).hexdigest() != receipt['dictionary_sha256']:
            raise ValueError('dictionary changed')
    headers, found = {}, set()
    for sheet, number, values in workbook_rows(raw):
        if number == 1:
            headers[sheet] = values
        key = (sheet, number)
        if key in expected:
            if dict(zip(headers[sheet], values)) != expected[key]:
                raise ValueError('checkpoint differs from original physical row')
            found.add(key)
    if found != expected.keys():
        raise ValueError('original physical rows missing')
    frame['member'] = receipt['member']
    frame['member_sha256'] = receipt['sha256']
    return frame, {k: receipt[k] for k in ('archive', 'member', 'sha256', 'dictionary_member', 'dictionary_sha256')}, len(found)


def write_csv(path, columns, records):
    with path.open('w', encoding='utf-8', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        writer.writerows(records)


def run(args):
    started = time.perf_counter()
    root, out, private = Path(args.checkpoints), Path(args.output), Path(args.private_output)
    out.mkdir(parents=True, exist_ok=True)
    private.mkdir(parents=True, exist_ok=True)
    paths = sorted((root / 'monthly').glob('CNRDS-*/2021-01.parquet'))
    if len(paths) != 3:
        raise ValueError('expected three pinned January source partitions')
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(verify_member, [(root, args.archive, p) for p in paths]))
    frame = pd.concat([r[0] for r in results], ignore_index=True)
    duplicates = frame.loc[frame.duplicated(['code', 'date'], keep=False)]
    classifications, conflicts, private_rows = [], [], []
    counts, differences, pairs = Counter(), Counter(), Counter()
    raw_fields = ['Dopnprc', 'Dhiprc', 'Dloprc', 'Dclsprc', 'Dtrdvol', 'Dtrdamt']
    raw_equal = 0
    for (code, date), group in duplicates.groupby(['code', 'date'], sort=True):
        records = [json.loads(s) for s in group.native_record]
        category, changed = classify(records)
        counts[category] += 1
        differences[','.join(changed)] += 1
        raw_equal += all(all(r.get(k) == records[0].get(k) for k in raw_fields) for r in records)
        locators = [dict(member=r.member, member_sha256=r.member_sha256, sheet=r.sheet, row=r.row) for r in group.itertuples()]
        identity = hashlib.sha256(json.dumps([code, date, locators], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        record = dict(evidence_id=identity, classification=category, candidate_count=len(group), differing_fields=';'.join(changed), action='NORMALIZE_IDENTICAL' if category == 'EXACT_DUPLICATE' else 'QUARANTINE_ALL')
        classifications.append(record)
        if category != 'EXACT_DUPLICATE':
            conflicts.append(record)
        private_rows.append(dict(**record, code=code, date=date, locators=locators, candidates=records, selected=None, rationale='complete lexeme equality' if category == 'EXACT_DUPLICATE' else 'no authoritative version priority'))
        pairs[tuple(sorted(set(group.member)))] += 1
    if len(duplicates) != 169028 or len(classifications) != 84514:
        raise ValueError('PR85 duplicate population not reproduced')
    with (private / 'candidate-lineage.jsonl').open('w', encoding='utf-8') as stream:
        for row in private_rows:
            stream.write(json.dumps(row, ensure_ascii=False) + '\n')
    columns = ['evidence_id', 'classification', 'candidate_count', 'differing_fields', 'action']
    write_csv(out / 'duplicate-classification.csv', columns, classifications)
    write_csv(out / 'conflicting-keys.csv', columns, conflicts)
    overlap = []
    for members, keys in sorted(pairs.items()):
        overlap.append(dict(members=';'.join(members), duplicate_keys=keys, kind='WITHIN_WORKBOOK' if len(members) == 1 else 'CROSS_WORKBOOK', proven_export_version_priority='NONE'))
    write_csv(out / 'source-overlap.csv', ['members', 'duplicate_keys', 'kind', 'proven_export_version_priority'], overlap)
    policy = dict(limits=LIMITS, complete_native_equality_required=True, raw_price_only_equality_sufficient=False, exact_duplicate_action='single equivalence class with every original locator; no preferred row', conflict_action='retain all candidates in private lineage; exclude key', version_priority=None, forbidden=['keep_first', 'keep_last', 'file_order', 'future_prices', 'cross_vendor_replacement', 'raw_deletion'], production_mutation=False)
    dump(out / 'resolution-policy.json', policy)
    impact = dict(status='PARTIAL', duplicate_keys=84514, duplicate_rows=169028, classifications={k: counts[k] for k in ['EXACT_DUPLICATE','OVERLAPPING_EXPORT','CONFLICTING_VALUES','IDENTITY_AMBIGUITY','UNRESOLVED']}, safely_normalizable_keys=counts['EXACT_DUPLICATE'], actual_native_conflict_keys=counts['CONFLICTING_VALUES'], isolated_keys=84514-counts['EXACT_DUPLICATE'], six_raw_fields_equal_keys=raw_equal, differing_field_patterns=dict(differences), additional_valid_tri_source_keys=0, csi500_research_window=['2021-09-13','2026-09-11'], csi500_affected_member_days=0, frozen_inputs_modified=False, historical_performance_modified=False, remaining_conditions=['supplier explanation of paired adjusted-field population and export construction', 'authoritative version or statistical-scope contract before any conflict resolution'], limits=LIMITS)
    dump(out / 'impact-summary.json', impact)
    dump(out / 'input-manifest.json', dict(baseline=args.baseline, reference_pr=85, reference_merge='f013210be4eca4e3bdff08b023350b8851c8a1ed', native=[r[1] for r in results], checkpoint_partitions=[str(p.relative_to(root)) for p in paths], scope='2021-01 only; original reads limited to three located archive members', code_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()))
    verification = dict(status='PASS', verified_physical_rows=sum(r[2] for r in results), original_members_verified=3, complete_native_records_equal_to_checkpoints=True, duplicate_population_reproduced=True, workers=3, seconds=time.perf_counter()-started, process_peak_bytes=getattr(psutil.Process().memory_info(), 'peak_wset', psutil.Process().memory_info().rss), memory_soft_limit_bytes=12*1024**3, full_database_rescan=False, raw_source_mutations=False)
    dump(out / 'verification.json', verification)
    summarize(args)
    print(json.dumps(dict(impact=impact, verification=verification), ensure_ascii=False), flush=True)


def summarize(args):
    """Enrich derived evidence without reopening native workbooks."""
    out, private, root = Path(args.output), Path(args.private_output), Path(args.checkpoints)
    stats = Counter()
    changed_fields = Counter()
    with (private / 'candidate-lineage.jsonl').open(encoding='utf-8') as stream:
        for line in stream:
            row = json.loads(line)
            candidates = row['candidates']
            numeric = False
            for field in row['differing_fields'].split(';'):
                changed_fields[field] += 1
                values = [r.get(field) for r in candidates]
                if field not in {'Scode', 'Coname', 'Trddt'} and all(v not in {None, '', '-', '--'} for v in values):
                    numeric |= len({Decimal(v) for v in values}) > 1
            stats['keys_with_actual_nonempty_numeric_disagreement'] += numeric
            stats['keys_with_numeric_disagreement_and_identity_ambiguity'] += numeric and row['classification'] == 'IDENTITY_AMBIGUITY'
            stats['keys_with_only_adjustment_field_differences'] += set(row['differing_fields'].split(';')) <= {'Adclsprc', 'Adclsprcp', 'Adret'}
            stats['keys_with_same_supplier_code'] += len({r['Scode'] for r in candidates}) == 1
            locators = row['locators']
            stats['keys_in_adjacent_rows_of_same_sheet_and_member'] += len(locators) == 2 and locators[0]['member'] == locators[1]['member'] and locators[0]['sheet'] == locators[1]['sheet'] and abs(int(locators[0]['row']) - int(locators[1]['row'])) == 1
            stats['keys_with_one_blank_and_one_populated_adjusted_close'] += len(candidates) == 2 and sum(r.get('Adclsprc') not in {None, ''} for r in candidates) == 1
    impact = json.loads((out / 'impact-summary.json').read_text(encoding='utf-8'))
    impact.update(stats)
    impact['keys_with_any_native_difference'] = impact['duplicate_keys']
    impact['differing_field_key_counts'] = dict(changed_fields)
    impact['numeric_definition'] = 'Two nonempty native numeric lexemes have different exact Decimal values; counts include identity-ambiguous keys and exclude blank-versus-populated alone.'
    dump(out / 'impact-summary.json', impact)
    coverage = []
    for path in sorted((root / 'monthly').glob('CNRDS-*/2021-01.parquet')):
        receipt = json.loads((root / (path.parent.name + '.json')).read_text(encoding='utf-8'))
        frame = pd.read_parquet(path, columns=['date', 'row'])
        coverage.append(dict(member=receipt['member'], january_start=frame.date.min(), january_end=frame.date.max(), january_rows=len(frame), physical_row_min=int(frame.row.astype(int).min()), physical_row_max=int(frame.row.astype(int).max())))
    manifest = json.loads((out / 'input-manifest.json').read_text(encoding='utf-8'))
    manifest['january_member_coverage'] = coverage
    manifest['code_sha256'] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    manifest['native_reader_sha256'] = hashlib.sha256(Path(__file__).with_name('tri_source_market_audit.py').read_bytes()).hexdigest()
    manifest['identity_boundary'] = 'Native Scode equality is observed; Coname disagreement is isolated, not proof of different issuers. Stable historical issuer identity remains unverified.'
    dump(out / 'input-manifest.json', manifest)
    print(json.dumps(dict(derived_statistics=dict(stats), member_coverage=coverage), ensure_ascii=False), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--checkpoints', required=True)
    parser.add_argument('--archive', required=True)
    parser.add_argument('--private-output', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--baseline', required=True)
    parser.add_argument('--summarize-only', action='store_true', help='Reuse verified private lineage; do not reopen vendor workbooks')
    args = parser.parse_args()
    (summarize if args.summarize_only else run)(args)
