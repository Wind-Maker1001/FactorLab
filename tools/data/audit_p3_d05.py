"""Source-separated PetroChina evidence audit. Run with Python -I -X utf8."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
from io import BytesIO
import json
import math
from pathlib import Path
import platform
import sys
import zipfile

# Only trusted repository code is added under isolated Python.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import candidate_preflight as p308
import audit_p3_d03 as d03

import pandas as pd
from factorlab.data.evidence import sha256
from factorlab.data.governance import diagnose_tables
from factorlab.data.hub import DataHub
from factorlab.diagnostics.candidate_preflight import CandidateQuery, preflight

START, END = '2017-01-03', '2018-12-28'
FIELDS = ['raw_open', 'raw_high', 'raw_low', 'raw_close', 'volume', 'amount']


def boundaries(dates):
    dates = pd.DatetimeIndex(dates)
    return {str(year): {'count': len(part), 'first': str(part.min().date()) if len(part) else None,
                       'last': str(part.max().date()) if len(part) else None}
            for year in (2017, 2018) for part in [dates[dates.year == year]]}


def exact_differences(left, right):
    """Public deltas locate defects without publishing either private price series."""
    conflicts = diagnose_tables(left, right, source=left.metadata.source_id,
        version=left.metadata.data_version, other_source=right.metadata.source_id,
        other_version=right.metadata.data_version, fields=FIELDS)
    public, private = [], []
    for conflict in conflicts:
        x, y = conflict['left']['value'], conflict['right']['value']
        row = {'date': str(pd.Timestamp(conflict['key'][0]).date()),
               'field': conflict['field'], 'type': conflict['type']}
        if pd.notna(x) and pd.notna(y) and math.isfinite(x) and math.isfinite(y):
            delta = Decimal.from_float(float(y)) - Decimal.from_float(float(x))
            row.update(bigquant_minus_csmar_exact=str(delta),
                       absolute_delta=float(abs(delta)),
                       ulps_at_csmar=float(abs(delta) / Decimal.from_float(math.ulp(float(x)))))
        public.append(row)
        private.append(dict(row, csmar=repr(x), bigquant=repr(y)))
    return public, private


def archive_table(archive, table, *, symbol=True, date_column='date', snapshot=False):
    """Read only overlapping partitions, bind selected payloads and receipt claims."""
    pieces, bindings = [], []
    for name in sorted(archive.namelist()):
        if not name.startswith(table + '/') or not name.endswith('.parquet'):
            continue
        if not snapshot:
            lo, hi = Path(name).stem.split('_')
            if pd.Timestamp(lo) > pd.Timestamp(END) or pd.Timestamp(hi) < pd.Timestamp(START):
                continue
        payload = archive.read(name)
        receipt_bytes = archive.read(name + '.receipt.json')
        receipt = json.loads(receipt_bytes)
        frame = pd.read_parquet(BytesIO(payload))
        if symbol:
            frame = frame.loc[frame.instrument.eq('601857.SH')].copy()
        frame['_member'] = name
        frame['_row'] = frame.index
        if not snapshot:
            frame = frame.loc[pd.to_datetime(frame[date_column]).between(START, END)]
        pieces.append(frame)
        bindings.append({'member': name, 'sha256': sha256(payload),
                         'receipt_sha256': sha256(receipt_bytes),
                         'exported_at_utc': receipt['exported_at_utc'],
                         'receipt_payload_hash_matches': receipt['sha256'] == sha256(payload)})
    schema = archive.read(table + '/schema.json')
    binding = {'members': bindings, 'schema_sha256': sha256(schema)}
    return pd.concat(pieces, ignore_index=True), binding


def native_audit(root, csmar_frame, differences):
    manifest = json.loads((root / 'manifest.json').read_bytes())
    verified = {}
    def read(name):
        relative = 'provenance/' + name
        payload = (root / relative).read_bytes()
        if sha256(payload) != manifest['files'][relative]:
            raise ValueError('native evidence hash mismatch: ' + relative)
        verified[relative] = manifest['files'][relative]
        return pd.read_parquet(BytesIO(payload))
    native = read('TRDNEW_Dalyr-0-native.parquet')
    lineage = read('TRDNEW_Dalyr-0-lineage.parquet')
    company = read('TRDNEW_Co-0-native.parquet')
    dates = pd.to_datetime(native.Trddt)
    selected = native.loc[dates.between(START, END)]
    actual = selected.loc[selected.Filling.eq('0')]
    a = actual.assign(date=pd.to_datetime(actual.Trddt)).set_index('date')
    b = csmar_frame.set_index('date')
    conversion = sum(float(Decimal(a.at[day, 'Dnvaltrd'])) != float(b.at[day, 'amount'])
                     for day in a.index.intersection(b.index))
    locations = []
    private = []
    for row in differences:
        day = row['date']
        loc = lineage.loc[lineage.native_date.eq(day)].iloc[0]
        original = a.at[pd.Timestamp(day), 'Dnvaltrd']
        locations.append({'date': day, 'member': loc.member, 'sheet': loc.sheet,
                          'row_number': int(loc.row_number),
                          'native_values_sha256': loc.native_values_sha256})
        private.append({'date': day, 'native_Dnvaltrd': original})
    return {'verified_files': verified, 'native_rows_in_window': len(selected),
            'filling_counts': dict(Counter(selected.Filling)),
            'native_duplicate_keys': int(selected.duplicated(['Stkcd', 'Trddt']).sum()),
            'unfilled_keys_equal_prepared': set(a.index) == set(b.index),
            'native_decimal_to_prepared_binary64_mismatches': conversion,
            'Trdsta_counts_unfilled': dict(Counter(actual.Trdsta)),
            'amount_locations': locations,
            'company_current_snapshot_rows': len(company),
            'identity_qualification': 'UNKNOWN',
            'historical_status_qualification': 'UNKNOWN'}, private


def status_calendar(stock_zip, common_zip, dates):
    with zipfile.ZipFile(stock_zip) as archive:
        status, sb = archive_table(archive, 'stock/cn_stock_status')
        suspend, ub = archive_table(archive, 'stock/cn_stock_suspend')
        basic, bb = archive_table(archive, 'stock/cn_stock_basic_info', snapshot=True)
    with zipfile.ZipFile(common_zip) as archive:
        calendar, cb = archive_table(archive, 'common/trading_days', symbol=False)
    calendar = calendar.loc[calendar.market_code.eq('CN')]
    keys = set(pd.to_datetime(dates))
    cs, ss = set(calendar.date), set(status.date)
    return {'security_status': {'rows': len(status), 'duplicate_keys': int(status.duplicated(['date', 'instrument']).sum()),
        'missing_observed_dates': len(keys - ss), 'extra_dates': len(ss - keys),
        'nulls': {k: int(status[k].isna().sum()) for k in status if not k.startswith('_')},
        'raw_code_counts': {k: {str(v): int(n) for v, n in status[k].value_counts(dropna=False).items()}
                            for k in ('st_status', 'is_risk_warning', 'suspended', 'price_limit_status', 'exdr')},
        'suspend_table_target_rows': len(suspend), 'basic_snapshot_target_rows': len(basic),
        'qualification': 'UNKNOWN', 'binding': sb, 'suspend_binding': ub, 'basic_binding': bb},
        'calendar': {'source': 'BigQuant/common/trading_days', 'market_code': 'CN',
        'rows': len(calendar), 'duplicate_keys': int(calendar.duplicated(['date', 'market_code']).sum()),
        'boundaries': boundaries(calendar.date), 'missing_bar_dates': len(cs - keys),
        'bar_dates_outside_calendar': len(keys - cs),
        'independent_of_security_observations': True, 'independent_official_source': 'UNKNOWN',
        'qualification': 'UNKNOWN', 'binding': cb}}


def raw_eligibility(sources):
    required = {'RAW_MOM5', 'RAW_MOM20', 'RAW_MOM60', 'RAW_PRICE_MA20', 'VOLUME_RATIO'}
    for source in sources.values():
        if any(source['checks'][k]['status'] != 'PASS'
               for k in ('coverage', 'duplicates', 'missing', 'ohlc', 'anomalies')):
            return 'FAIL'
        factors = {f['definition']['factor_id']: f for f in source['factors']}
        if not required.issubset(factors):
            return 'UNKNOWN'
        if any(factors[k]['status'] != 'PASS' or factors[k]['denominator'] == 0
               or factors[k]['valid_samples'] != factors[k]['denominator'] for k in required):
            return 'FAIL'
    return 'PASS' if len(sources) == 2 else 'UNKNOWN'


def run(args):
    cs, actions = p308.csmar(args.csmar_candidate)
    bq = p308.bigquant(args.stock_zip, START, END)
    hub = DataHub({'csmar': cs, 'bigquant': bq})
    sources, results = {}, {}
    for name, adapter in (('csmar', cs), ('bigquant', bq)):
        dates = pd.DatetimeIndex(sorted(adapter.frame.date.unique()))
        query = CandidateQuery(name, adapter.metadata.data_version, '601857.SH',
                               START, END, '2018-01-01', END)
        diagnostic = preflight(hub, query, calendar=dates, calendar_id=name + '-observed-dates',
            calendar_kind='OBSERVED_SECURITY_SESSIONS_ONLY',
            units={'prices': 'CNY candidate', 'volume': 'candidate shares; BQ contract UNKNOWN',
                   'amount': 'candidate CNY; BQ precision contract UNKNOWN'},
            identity_evidence='CURRENT_NATIVE_COMPANY_RECORD' if name == 'csmar' else None,
            action_evidence=actions if name == 'csmar' else None)
        sources[name] = diagnostic.to_dict()
        sources[name]['formal_gate'] = p308.formal_refusal(diagnostic, adapter.frame, dates, query)
        sources[name]['observed_boundaries'] = boundaries(dates)
        results[name] = hub.read(name, 'daily_bars', symbol='601857.SH', start=START, end=END)
    differences, private = exact_differences(results['csmar'], results['bigquant'])
    native, original = native_audit(args.csmar_candidate, cs.frame, differences)
    state = status_calendar(args.stock_zip, args.common_zip, cs.frame.date)
    action = d03.run(args.csmar_candidate, args.stock_zip, args.evidence)
    report = {'schema': 'P3D05_REAL_EVIDENCE_V1', 'status': 'P3D05_PARTIAL',
        'created_at_utc': datetime.now(timezone.utc).isoformat(), 'python': platform.python_version(),
        'boundary': p308.BOUNDARY, 'security': '601857.SH', 'sources': sources,
        'cross_source': {'status': 'FAIL' if differences else 'PASS',
            'meaning': 'EXACT_VALUES_NOT_CERTIFIED_SEMANTICS', 'differences': differences,
            'date_keys_equal': set(cs.frame.date) == set(bq.frame.date),
            'merged': False, 'authoritative_source': None, 'cause': 'UNKNOWN'},
        'native': native, **state, 'actions': action,
        'eligibility': {
            'raw_feature_computability': {'status': raw_eligibility(sources), 'scope': 'RETROSPECTIVE_ONLY', 'evidence': 'sources'},
            'adjusted_feature_computability': {'status': 'UNKNOWN', 'reason': 'Unqualified adjusted-price contract', 'evidence': 'actions'},
            'historical_labels': {'status': 'UNKNOWN', 'reason': 'PIT, HFQ and historical status unverified', 'evidence': 'sources'},
            'corporate_action_execution': {'status': 'UNKNOWN', 'reason': 'Coverage, revision, tax and settlement evidence incomplete', 'evidence': 'actions'},
            'formal_admission': {'status': 'FAIL', 'reason': 'Existing formal gate rejected both diagnostics', 'evidence': 'sources.*.formal_gate'}},
        'execution': {'labels_generated': False, 'backtest_run': False}}
    return report, {'differences': private, 'native_amounts': original,
                    'source_versions': {k: v.metadata.data_version for k, v in results.items()}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('csmar-candidate', 'stock-zip', 'common-zip', 'evidence', 'output', 'private-output'):
        parser.add_argument('--' + name, type=Path, required=True)
    args = parser.parse_args()
    report, private = run(args)
    for path, content in ((args.private_output, private), (args.output, report)):
        with path.open('x', encoding='utf-8') as stream:
            json.dump(content, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write('\n')
    print(json.dumps({'status': report['status'], 'cross_source': report['cross_source'],
                      'eligibility': report['eligibility']}, ensure_ascii=False))


if __name__ == '__main__':
    main()
