"""Targeted read-only missingness extraction; run with python -I -X utf8."""
import argparse
import json
import zipfile
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

FIELDS = ['open', 'high', 'low', 'close', 'amount', 'pre_close']


def normalize(codes):
    return codes.str.replace('.XSHG', '.SH', regex=False).str.replace('.XSHE', '.SZ', regex=False)


def match(members, bars):
    return members.merge(bars, on=['date', 'instrument'], how='left', indicator=True, validate='one_to_one')


def classify(row):
    if row['_merge'] == 'left_only':
        return 'MISSING_BAR_KEY', 'UNKNOWN'
    if any(pd.isna(row[f]) for f in FIELDS):
        return 'PRESENT_BAR_NULL_FIELDS', 'UNKNOWN'
    return 'COMPLETE', 'CONFIRMED'


def partitions(archive, table, start, end):
    for name in archive.namelist():
        if name.startswith(f'stock/{table}/') and name.endswith('.parquet'):
            stem = Path(name).stem
            if stem.startswith('unpartitioned'):
                yield name
            else:
                lo, hi = stem.split('_')
                if pd.Timestamp(lo) <= end and pd.Timestamp(hi) >= start:
                    yield name


def read_part(archive, name, columns=None):
    table = pq.read_table(pa.BufferReader(archive.read(name)), columns=columns)
    frame = table.to_pandas()
    frame['source_member'] = name
    frame['source_row_zero_based'] = range(len(frame))
    frame['source_receipt'] = name + '.receipt.json'
    return frame


def run(args):
    out = Path(args.private_output)
    out.mkdir(parents=True, exist_ok=False)
    start, end = pd.Timestamp('2021-09-13'), pd.Timestamp('2026-09-11')
    with zipfile.ZipFile(args.membership_zip) as z:
        members = pd.read_csv(z.open('members_daily.csv'), encoding='utf-8-sig')
    members['membership_csv_line'] = range(2, len(members) + 2)
    members['date'] = pd.to_datetime(members.date)
    members = members.loc[members.date.between(start, end)].copy()
    members['instrument'] = normalize(members.stock_code)
    pieces = []
    receipts = {}
    identity_pieces = []
    with zipfile.ZipFile(args.stock_zip) as z:
        if args.extracted_private_input:
            prior = Path(args.extracted_private_input)
            missing = pd.read_csv(prior / 'missing_keys.csv', parse_dates=['date'])
            nulls = pd.read_csv(prior / 'null_fields.csv', parse_dates=['date'])
        else:
            for name in partitions(z, 'cn_stock_real_bar1d', start, end):
                f = read_part(z, name, ['date', 'instrument', *FIELDS, 'volume', 'deal_number', 'name'])
                identity_pieces.append(f.loc[f.instrument.isin(['300114.SZ', '302132.SZ', '600068.SH'])].copy())
                f = f.loc[f.date.between(start, end) & f.instrument.isin(members.instrument)]
                pieces.append(f)
                receipts[name] = json.loads(z.read(name + '.receipt.json'))
            bars = pd.concat(pieces, ignore_index=True)
            joined = match(members, bars)
            missing = joined.loc[joined['_merge'] == 'left_only'].copy()
            nulls = joined.loc[(joined['_merge'] == 'both') & joined[FIELDS].isna().any(axis=1)].copy()
        missing.to_csv(out / 'missing_keys.csv', index=False)
        nulls.to_csv(out / 'null_fields.csv', index=False)
        targets = pd.concat([missing, nulls])
        codes = set(targets.instrument) | {'302132.SZ', '300114.SZ', '600068.SH'}
        evidence = {}
        for table in ['cn_stock_status', 'cn_stock_suspend', 'cn_stock_name_change', 'cn_stock_basic_info', 'cn_stock_static_data']:
            rows = []
            for name in partitions(z, table, start, end):
                f = read_part(z, name)
                if 'instrument' in f:
                    f = f.loc[f.instrument.isin(codes)]
                rows.append(f)
                receipt = name + '.receipt.json'
                if receipt in z.namelist():
                    receipts[name] = json.loads(z.read(receipt))
            if rows:
                f = pd.concat(rows, ignore_index=True)
                f.to_csv(out / (table + '.csv'), index=False)
                evidence[table] = {'selected_rows': len(f), 'columns': list(f)}
            schema = f'stock/{table}/schema.json'
            if schema in z.namelist():
                (out / (table + '-schema.json')).write_bytes(z.read(schema))
        if args.extracted_private_input:
            for name in partitions(z, 'cn_stock_real_bar1d', start, end):
                f = read_part(z, name, ['date', 'instrument', *FIELDS, 'volume', 'name'])
                identity_pieces.append(f.loc[f.instrument.isin(['300114.SZ', '302132.SZ', '600068.SH'])])
                receipts[name] = json.loads(z.read(name + '.receipt.json'))
        pd.concat(identity_pieces, ignore_index=True).to_csv(out / 'identity_candidate_bars.csv', index=False)
    (out / 'source_receipts.json').write_text(json.dumps(receipts, ensure_ascii=False, indent=2), encoding='utf-8')
    stats = {'window': [str(start.date()), str(end.date())], 'member_days': len(members), 'matched': len(members) - len(missing),
             'missing_keys': len(missing), 'matched_nulls': {f: int(nulls[f].isna().sum()) for f in FIELDS},
             'unique_null_security_dates': len(nulls), 'missing_by_security': missing.groupby('instrument').size().to_dict(),
             'null_by_security': nulls.groupby('instrument').size().to_dict(), 'target_evidence': evidence,
             'missing_by_date': missing.groupby('date').size().rename(index=str).to_dict(),
             'null_by_date': nulls.groupby('date').size().rename(index=str).to_dict(),
             'gates': ['NO LIVE MONEY', 'PIT_UNVERIFIED', 'HFQ_UNVERIFIED', 'FORMAL_ADMISSION_BLOCKED']}
    (out / 'statistics.json').write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(stats, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    p = argparse.ArgumentParser(__doc__)
    p.add_argument('--stock-zip', required=True)
    p.add_argument('--membership-zip', required=True)
    p.add_argument('--private-output', required=True)
    p.add_argument('--extracted-private-input', help='Reuse missing_keys.csv and null_fields.csv from an interrupted extraction')
    run(p.parse_args())
