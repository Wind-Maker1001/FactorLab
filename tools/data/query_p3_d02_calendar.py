"""Compare anomaly dates against existing BigQuant trading calendar only."""
import argparse
import json
from pathlib import Path
import zipfile
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq


def run(args):
    root = Path(args.private_root)
    frames = [pd.read_csv(root / name, usecols=['date', 'instrument']) for name in ['missing_keys.csv', 'null_fields.csv']]
    targets = pd.concat(frames).drop_duplicates()
    target_dates = set(targets.date.str[:10])
    selected, receipts = [], {}
    with zipfile.ZipFile(args.common_zip) as archive:
        for name in archive.namelist():
            if not name.startswith('common/trading_days/') or not name.endswith('.parquet'):
                continue
            lo, hi = Path(name).stem.split('_')
            if hi < min(target_dates) or lo > max(target_dates):
                continue
            f = pq.read_table(pa.BufferReader(archive.read(name))).to_pandas()
            f['source_row_zero_based'] = range(len(f))
            f['source_member'] = name
            f['date'] = f.date.dt.strftime('%Y-%m-%d')
            selected.append(f.loc[f.date.isin(target_dates)])
            receipts[name] = json.loads(archive.read(name + '.receipt.json'))
    calendar = pd.concat(selected, ignore_index=True)
    calendar.to_csv(root / 'target_calendar_rows.csv', index=False)
    summary = {'source': 'BigQuant common.zip common/trading_days',
               'scope': 'ANOMALY_DATES_ONLY', 'target_dates': len(target_dates),
               'market_codes': sorted(calendar.market_code.unique().tolist()),
               'market_date_counts': calendar.groupby('market_code').date.nunique().to_dict(),
               'dates_absent_from_all_markets': sorted(target_dates - set(calendar.date)),
               'independent_exchange_calendar': 'NOT_AVAILABLE'}
    (root / 'calendar-query-receipts.json').write_text(json.dumps(receipts, ensure_ascii=False, indent=2), encoding='utf-8')
    (root / 'calendar-summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    p = argparse.ArgumentParser(__doc__)
    p.add_argument('--private-root', required=True)
    p.add_argument('--common-zip', required=True)
    run(p.parse_args())
