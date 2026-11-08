"""Summarize private P3-D02 extraction without publishing source rows."""
import argparse
import json
from pathlib import Path
import pandas as pd


def run(root, catalog):
    root = Path(root)
    n = pd.read_csv(root / 'null_fields.csv', parse_dates=['date'])
    m = pd.read_csv(root / 'missing_keys.csv', parse_dates=['date'])
    s = pd.read_csv(root / 'cn_stock_status.csv', parse_dates=['date'])
    u = pd.read_csv(root / 'cn_stock_suspend.csv', parse_dates=['date'])
    b = pd.read_csv(root / 'identity_candidate_bars.csv', parse_dates=['date'])
    j = n.merge(s, on=['date', 'instrument'], how='left', suffixes=('', '_status'), validate='one_to_one')
    j = j.merge(u, on=['date', 'instrument'], how='left', suffixes=('', '_suspend'))
    j.to_csv(root / 'null_status_comparison.csv', index=False)
    result = {'null_status': j.groupby(['suspended', 'suspend_reason'], dropna=False).size().to_string(),
              'null_zero_volume': int((n.volume == 0).sum()),
              'null_zero_deals': int((n.deal_number == 0).sum()),
              'null_only_pre_close': n.loc[n.open.notna(), ['date', 'instrument']].astype(str).to_dict('records'),
              'missing_ranges': m.groupby('instrument').date.agg(['min', 'max', 'count']).astype(str).to_dict('index'),
              'candidate_ranges': b.groupby('instrument').date.agg(['min', 'max', 'count']).astype(str).to_dict('index')}
    candidate = m[['date', 'instrument']].copy()
    candidate['candidate_instrument'] = candidate.instrument.replace({'300114.SZ': '302132.SZ'})
    candidate = candidate.merge(b.rename(columns={'instrument': 'candidate_instrument'}), on=['date', 'candidate_instrument'], how='left', indicator=True)
    candidate.to_csv(root / 'missing_candidate_comparison.csv', index=False)
    result['candidate_matches'] = candidate.groupby(['instrument', '_merge'], observed=True).size().to_string()
    basic = pd.read_csv(root / 'cn_stock_basic_info.csv')
    selected = basic.loc[basic.instrument.isin(['300114.SZ', '302132.SZ', '600068.SH'])]
    result['identity_basic'] = selected.to_dict('records')
    cat = json.loads(Path(catalog).read_text(encoding='utf-8'))
    entries = []
    for source in cat['sources']:
        for member in source['members']:
            if member.get('family') in ['daily', 'daily_price', 'daily_trading'] or 'Dalyr' in member['name']:
                entries.append({'source': source.get('path'), 'source_name': source.get('name'), 'member': member['name'], 'date_min': member.get('date_min'), 'date_max': member.get('date_max'), 'family': member.get('family'), 'target_codes': [c for c in ['300114', '302132', '600068'] if c in member.get('codes', [])]})
    result['csmar_catalog_daily'] = entries
    (root / 'comparison-summary.json').write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


if __name__ == '__main__':
    p = argparse.ArgumentParser(__doc__)
    p.add_argument('--private-root', required=True)
    p.add_argument('--catalog', required=True)
    a = p.parse_args()
    run(a.private_root, a.catalog)
