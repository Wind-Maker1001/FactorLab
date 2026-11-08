"""Read-only, aggregate diagnostics for an explicitly selected local window.

Run with python -I -X utf8. ZIP members stay in memory; no vendor rows are written.
"""
from __future__ import annotations

import argparse
import csv
import json
import platform
import zipfile
from collections import Counter
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq


def profile(frame: pd.DataFrame, keys: list[str]) -> dict:
    dates = pd.to_datetime(frame['date'])
    numeric = frame.select_dtypes(include='number')
    result = {
        'rows': len(frame), 'unique_keys': len(frame.drop_duplicates(keys)),
        'duplicate_excess': int(frame.duplicated(keys).sum()),
        'start': str(dates.min().date()), 'end': str(dates.max().date()),
        'observed_dates': int(dates.nunique()),
        'securities': int(frame['instrument' if 'instrument' in frame else 'symbol'].nunique()),
        'nulls': {str(k): int(v) for k, v in frame.isna().sum().items()},
        'nonfinite_nonnull': {str(k): int(((v == float('inf')) | (v == -float('inf'))).sum()) for k, v in numeric.items()},
        'fields': {str(k): str(v) for k, v in frame.dtypes.items()},
    }
    prices = ['raw_open', 'raw_high', 'raw_low', 'raw_close'] if 'raw_open' in frame else ['open', 'high', 'low', 'close']
    if set(prices).issubset(frame):
        o, h, l, c = (frame[x] for x in prices)
        result['nonpositive_price_rows'] = int((frame[prices] <= 0).any(axis=1).sum())
        result['ohlc_order_rows'] = int(((h < l) | (h < o) | (h < c) | (l > o) | (l > c)).sum())
    result['negative_volume_rows'] = int((frame['volume'] < 0).sum()) if 'volume' in frame else None
    result['negative_amount_rows'] = int((frame['amount'] < 0).sum()) if 'amount' in frame else None
    return result


def run(args: argparse.Namespace) -> dict:
    start, end = pd.Timestamp(args.start), pd.Timestamp(args.end)
    result = {'schema': 'P3D01_READ_ONLY_DIAGNOSTIC_V1', 'python': platform.python_version(),
              'window': {'start': args.start, 'end': args.end},
              'pit': 'PIT_UNVERIFIED', 'hfq': 'HFQ_UNVERIFIED',
              'formal': 'FORMAL_ADMISSION_BLOCKED', 'money': 'NO LIVE MONEY'}
    with zipfile.ZipFile(args.membership_zip) as archive:
        members = pd.read_csv(archive.open('members_daily.csv'), encoding='utf-8-sig')
    result['membership_columns'] = list(members)
    code_key = 'stock_code'
    members['date'] = pd.to_datetime(members['date'])
    members = members.loc[members['date'].between(start, end)].copy()
    # Explicit lexical conversion of vendor exchange suffixes, not stable-ID certification.
    members['instrument'] = members[code_key].str.replace('.XSHG', '.SH', regex=False).str.replace('.XSHE', '.SZ', regex=False)
    result['membership'] = profile(members, ['date', 'instrument'])
    universe = set(members['instrument'])
    frames = []
    with zipfile.ZipFile(args.stock_zip) as archive:
        names = archive.namelist()
        tables = sorted({n.split('/')[1] for n in names if n.startswith('stock/') and len(n.split('/')) == 3})
        result['stock_tables'] = {}
        for table in tables:
            parts = [n for n in names if n.startswith(f'stock/{table}/') and n.endswith('.parquet')]
            receipts = [json.loads(archive.read(n + '.receipt.json')) for n in parts if n + '.receipt.json' in names]
            schema_name = f'stock/{table}/schema.json'
            result['stock_tables'][table] = {
                'partitions': len(parts), 'receipts': len(receipts),
                'receipt_rows_claim': sum(r['rows'] for r in receipts),
                'query_window_start_claim': min((r['window_start'] for r in receipts), default=None),
                'query_window_end_claim': max((r['window_end'] for r in receipts), default=None),
                'exported_at_utc_values': sorted({r['exported_at_utc'] for r in receipts}),
                'schema': json.loads(archive.read(schema_name)) if schema_name in names else None,
                'verification': 'RECEIPT_CLAIMS_NOT_FULL_TABLE_ROW_SCAN',
            }
        selected = []
        for name in names:
            if not name.startswith('stock/cn_stock_real_bar1d/') or not name.endswith('.parquet'):
                continue
            lo, hi = Path(name).stem.split('_')
            if pd.Timestamp(lo) > end or pd.Timestamp(hi) < start:
                continue
            frame = pq.read_table(pa.BufferReader(archive.read(name))).to_pandas()
            selected.append({'member': name, 'physical_rows': len(frame),
                             'receipt_rows': json.loads(archive.read(name + '.receipt.json'))['rows']})
            frame = frame.loc[frame['date'].between(start, end) & frame['instrument'].isin(universe)]
            frames.append(frame)
        result['scanned_partitions'] = selected
    bars = pd.concat(frames, ignore_index=True)
    result['candidate_population_bars'] = profile(bars, ['date', 'instrument'])
    joined = members[['date', 'instrument']].merge(bars, on=['date', 'instrument'], how='left', indicator=True)
    result['member_date_join'] = {
        'expected_rows': len(members), 'joined_rows': len(joined),
        'missing_keys': int((joined['_merge'] == 'left_only').sum()),
        'missing_securities': sorted(joined.loc[joined['_merge'] == 'left_only', 'instrument'].unique()),
        'dates_with_missing': int(joined.loc[joined['_merge'] == 'left_only', 'date'].nunique()),
        'matched': profile(joined.loc[joined['_merge'] == 'both'].drop(columns='_merge'), ['date', 'instrument']),
    }
    candidate = pq.read_table(Path(args.csmar_candidate) / 'market/bars.parquet').to_pandas()
    result['csmar_2017_2018'] = profile(candidate, ['date', 'symbol'])
    result['csmar_2017_2018']['manifest_coverage'] = json.loads((Path(args.csmar_candidate) / 'manifest.json').read_text())['coverage']
    registry = Path(args.inventory_root)
    files = list(csv.DictReader((registry / 'files.csv').open(encoding='utf-8-sig')))
    current = Counter()
    for row in files:
        path = Path(row['absolute_path'])
        current[(row['source'], 'EXISTS' if path.is_file() else 'MISSING')] += 1
    result['indexed_file_access'] = [{'source': s, 'status': st, 'files': count} for (s, st), count in sorted(current.items())]
    # Inspect one indexed TDX file without inferring archive-wide coverage.
    row = next(r for r in files if r['source'] == 'vipdoc' and Path(r['absolute_path']).is_file())
    import struct
    raw = Path(row['absolute_path']).read_bytes()
    decoded = list(struct.iter_unpack('<IIIIIfII', raw))
    result['tdx_sample'] = {'file_name': row['file_name'], 'bytes': len(raw), 'records': len(decoded),
                            'min_date_integer': min(r[0] for r in decoded), 'max_date_integer': max(r[0] for r in decoded),
                            'scope': 'ONE_INDEXED_FILE_ONLY; UNITS_AND_SECURITY_ID_NOT_CERTIFIED'}
    return result


def cross_source(args: argparse.Namespace) -> dict:
    """Compare an existing single-security candidate, without resolving conflicts."""
    candidate = pq.read_table(Path(args.csmar_candidate) / 'market/bars.parquet').to_pandas()
    pieces = []
    event_profiles = {}
    with zipfile.ZipFile(args.stock_zip) as archive:
        names = archive.namelist()
        for name in names:
            if not name.startswith('stock/cn_stock_real_bar1d/') or not name.endswith('.parquet'):
                continue
            lo, hi = Path(name).stem.split('_')
            if pd.Timestamp(lo) > candidate['date'].max() or pd.Timestamp(hi) < candidate['date'].min():
                continue
            frame = pq.read_table(pa.BufferReader(archive.read(name)), columns=['date', 'instrument', 'open', 'high', 'low', 'close', 'volume', 'amount']).to_pandas()
            pieces.append(frame.loc[frame['instrument'] == '601857.SH'])
        for table in ['cn_stock_dividend', 'cn_stock_allotment', 'cn_stock_adjust_factor']:
            frames = [pq.read_table(pa.BufferReader(archive.read(n))).to_pandas()
                      for n in names if n.startswith(f'stock/{table}/') and n.endswith('.parquet')]
            frame = pd.concat(frames, ignore_index=True)
            # These are observed collisions, not a vendor-certified primary key.
            observed_keys = ['date', 'instrument', 'type', 'method'] if table == 'cn_stock_adjust_factor' else ['date', 'instrument']
            event_profiles[table] = profile(frame, observed_keys)
            event_profiles[table]['key_status'] = 'CANDIDATE_KEY_NOT_VENDOR_CERTIFIED'
            for column in frame.select_dtypes(include='datetime').columns:
                event_profiles[table].setdefault('date_fields', {})[column] = {
                    'start': str(frame[column].min()), 'end': str(frame[column].max())}
    bars = pd.concat(pieces, ignore_index=True)
    comparison = candidate.merge(bars, left_on=['date', 'symbol'], right_on=['date', 'instrument'], how='outer', indicator=True)
    matched = comparison.loc[comparison['_merge'] == 'both']
    fields = {}
    for column in ['open', 'high', 'low', 'close', 'volume', 'amount']:
        left = 'raw_' + column if column in ['open', 'high', 'low', 'close'] else column + '_x'
        right = column if column in ['open', 'high', 'low', 'close'] else column + '_y'
        delta = (matched[left] - matched[right]).abs()
        fields[column] = {'nonnull_pairs': int(delta.notna().sum()),
                          'exact_differences': int((delta.dropna() != 0).sum()),
                          'max_absolute_difference': float(delta.max())}
    return {'symbol': '601857.SH', 'start': str(candidate['date'].min().date()), 'end': str(candidate['date'].max().date()),
            'csmar_rows': len(candidate), 'bigquant_rows': len(bars),
            'csmar_duplicate_keys': int(candidate.duplicated(['date', 'symbol']).sum()),
            'bigquant_duplicate_keys': int(bars.duplicated(['date', 'instrument']).sum()),
            'join_counts': {str(k): int(v) for k, v in comparison['_merge'].value_counts().items()},
            'fields': fields, 'event_profiles': event_profiles,
            'interpretation': 'NUMERIC_COMPARISON_ONLY; VENDOR_UNITS_AND_COMMON_UPSTREAM_UNKNOWN; NO_SOURCE_SELECTED'}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ['stock-zip', 'membership-zip', 'csmar-candidate', 'inventory-root', 'start', 'end', 'output']:
        parser.add_argument('--' + name, required=True)
    args = parser.parse_args()
    result = run(args)
    result['cross_source'] = cross_source(args)
    # Exclusive create prevents overwriting an earlier diagnostic receipt.
    with Path(args.output).open('x', encoding='utf-8', newline='\n') as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2)
        handle.write('\n')
    print(json.dumps({k: result[k] for k in ['window', 'candidate_population_bars', 'member_date_join', 'csmar_2017_2018', 'indexed_file_access', 'tdx_sample']}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
