"""Bounded cold-asset checks for P3-D06; no reconstruction of unknown members.

Sample historical index snapshots plus the small change table, not the 113M-row
full index-component export. Native CSMAR workbooks are not reopened.
"""
import argparse
from collections import defaultdict
import json
from pathlib import Path
import sys
import time

import pandas as pd
import psutil

sys.path.insert(0, str(Path(__file__).resolve().parent))
from audit_p3_d06 import Archive, digest, write_json


def run(args):
    started = time.perf_counter()
    output = Path(args.output)
    manifest = []
    root = Path(args.bigquant_root)
    index = Archive(root, 'index', manifest)
    common = Archive(root, 'common', manifest)
    dates = common.read('trading_days', ['date', 'market_code'], pd.Timestamp('2016-11-07'), pd.Timestamp('2026-11-06'))
    calendar = dates.loc[dates.market_code.eq('CN'), 'date'].drop_duplicates().sort_values()
    snapshots = []
    for start, end in [('2016-11-07', '2016-11-30'), ('2021-08-01', '2021-08-31'), ('2026-11-01', '2026-11-06')]:
        sample = index.read('cn_stock_index_component', ['date', 'instrument', 'member_code'],
                            pd.Timestamp(start), pd.Timestamp(end), {'000905.SH'})
        count = sample.groupby('date').size()
        snapshots.append({'sample_start': start, 'sample_end': end, 'observed_start': str(sample.date.min().date()),
                          'observed_end': str(sample.date.max().date()), 'rows': len(sample),
                          'unique_member_date_keys': len(sample.drop_duplicates(['date', 'member_code'])),
                          'dates': int(sample.date.nunique()), 'securities': int(sample.member_code.nunique()),
                          'min_daily_members': int(count.min()), 'max_daily_members': int(count.max())})
    changes = index.read('cn_stock_index_change', ['date', 'instrument', 'member_code', 'inout_flag'],
                         pd.Timestamp('2016-11-07'), pd.Timestamp('2026-11-06'), {'000905.SH'})
    change_profile = {'rows': len(changes), 'unique_action_keys': len(changes.drop_duplicates(['date', 'member_code', 'inout_flag'])),
                      'start': str(changes.date.min().date()), 'end': str(changes.date.max().date()),
                      'securities': int(changes.member_code.nunique()),
                      'action_counts': {str(k): int(v) for k, v in changes.inout_flag.value_counts(dropna=False).items()},
                      'nulls': {k: int(v) for k, v in changes.isna().sum().items()},
                      'time_semantics': 'date exists; announcement/implementation/first-distribution contract UNKNOWN',
                      'certified_known_at_actions': 0}
    catalog_raw = Path(args.csmar_catalog).read_bytes()
    catalog = json.loads(catalog_raw)
    groups = defaultdict(list)
    for source in catalog['sources']:
        for member in source.get('members', []):
            if member.get('profile_available'):
                groups[member['family']].append(member)
    csmar = []
    for family, members in sorted(groups.items()):
        profiled = [m for m in members if m.get('records') is not None]
        csmar.append({'family': family, 'catalog_workbooks': len(members),
                      'workbooks_with_row_profiles': len(profiled),
                      'physical_rows_claim': sum(m['records'] for m in profiled) if profiled else None,
                      'date_min_claim': min((m['date_min'] for m in members if m.get('date_min')), default=None),
                      'date_max_claim': max((m['date_max'] for m in members if m.get('date_max')), default=None),
                      'evidence_level': 'EXISTING_NATIVE_CATALOG_NOT_RESCANNED',
                      'missing_rate': 'UNKNOWN', 'unique_security_date_keys': 'UNKNOWN'})
    manifest.append({'asset': 'CSMAR_existing_catalog', 'actual_sha256': digest(catalog_raw)})
    result = {'schema': 'P3D06_EXTENSION_CONDITIONS_V1', 'status': 'PARTIAL',
              'calendar_source': 'BigQuant CN; not independent exchange certification',
              'calendar_start': str(calendar.min().date()), 'calendar_end': str(calendar.max().date()),
              'calendar_dates': len(calendar),
              'dates_before_existing_jq_membership': int(calendar.lt(pd.Timestamp('2021-09-13')).sum()),
              'dates_after_existing_jq_membership': int(calendar.gt(pd.Timestamp('2026-09-11')).sum()),
              'csi500_component_samples': snapshots, 'csi500_change_table': change_profile,
              'full_decade_component_coverage': 'UNKNOWN_NOT_SCANNED; 121 partition receipts are not daily coverage',
              'csmar_catalog_sources': len(catalog['sources']), 'csmar_catalog_profiles': csmar,
              'input_members': manifest,
              'resources': {'wall_seconds': time.perf_counter() - started,
                            'peak_working_set_bytes': psutil.Process().memory_info().peak_wset,
                            'parquet_partitions': index.parts + common.parts,
                            'projected_parquet_physical_rows': index.physical_rows + common.physical_rows,
                            'uncompressed_bytes': index.bytes + common.bytes}}
    write_json(output / 'extension-assets.json', result)
    print(json.dumps({'extension_calendar_dates': len(calendar), 'index_snapshots': snapshots,
                      'change_rows': len(changes), 'wall_seconds': result['resources']['wall_seconds']}), flush=True)
    index.zip.close()
    common.zip.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bigquant-root', required=True)
    parser.add_argument('--csmar-catalog', required=True)
    parser.add_argument('--output', required=True, help='Existing P3-D06 aggregate directory')
    run(parser.parse_args())
