"""Bounded evidence inventory; never produces a QualificationReport.

Run from trusted repository code with Python -I -B -X utf8.
"""
from __future__ import annotations

import argparse
from collections import Counter
import csv
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import subprocess

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT / 'tools/data'))
import pandas as pd
from audit_p3_d06 import Archive

BASE = 'e27d2f893a9f432ea67f80a69f4e9300a9796a13'
SECURITY = '601857.SH'
START, END = '2017-01-03', '2018-12-28'
BOUNDARY = 'NO LIVE MONEY / PIT_UNVERIFIED / HFQ_UNVERIFIED / FORMAL_ADMISSION_BLOCKED'
VERSIONS = {
    'csmar': 'sha256:d77417709364d664f1041418c51a434e836a72cbebbc53f7b2158556478ccb64',
    'bigquant': 'sha256:eb816b086d75984f726bd436a9d479a35571922914dcf5d952bb43b172a4c4d6',
}
COLUMNS = ['condition', 'security', 'date_or_period', 'source', 'source_version',
           'evidence_id', 'original_path_or_url', 'content_sha256', 'effective_at',
           'announced_at', 'known_at', 'exported_at', 'observed_result',
           'independent_reference', 'status', 'blocker', 'next_action']
UPSTREAM = [
    'docs/validation/p3-d07/REPORT.md',
    'docs/validation/p3-d07/real-multisource.json',
    'docs/validation/p3-d07/minimum-evidence.csv',
    'docs/validation/p3-d06/README.md',
    'docs/validation/p3-d06/membership-identity-status.md',
    'docs/validation/p3-d06/extension-assets.json',
    'docs/validation/p3-d05/REPORT.md',
    'docs/validation/p3-d05/real.json',
    'docs/validation/p3-d05/ACTION_MATRIX.csv',
    'docs/validation/p3-d05/official-responses.json',
    'docs/data/P3_D02_IDENTITY_BOUNDARIES.md',
    'docs/data/P3_D03_ACTION_EVIDENCE.md',
    'docs/data/P3_D03_ADJUSTMENT_RECONCILIATION.md',
    'docs/data/p3-d03-summary.json',
    'docs/data/P3_D04_PIT_EVIDENCE_REPORT.md',
    'tools/data/audit_p3_d03.py', 'tools/data/audit_p3_d05.py',
    'tools/data/audit_p3_d06.py', 'src/factorlab/data/admission.py',
    'src/factorlab/data/csmar/profiles.py',
]


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def repository_bytes(name):
    """Bind immutable Git blob bytes, independent of checkout newline conversion."""
    raw = subprocess.check_output(['git', 'show', BASE + ':' + name], cwd=ROOT)
    if (ROOT / name).read_bytes().replace(b'\r\n', b'\n') != raw.replace(b'\r\n', b'\n'):
        raise ValueError('upstream working file differs from pinned base: ' + name)
    return raw


def read_json(path):
    return json.loads(path.read_bytes())


def write_json(path, value):
    with path.open('x', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write('\n')


def classify(*, complete, conflict=False, applicable=True):
    """Classify a specified local condition, not a financial qualification."""
    if not applicable:
        return 'NOT_APPLICABLE'
    if conflict:
        return 'FAIL'
    return 'PASS' if complete else 'UNKNOWN'


def validate_observations(data, upstream):
    if data['security'] != SECURITY or data['schema'] != 'P3D05_REAL_EVIDENCE_V1':
        raise ValueError('wrong security or observation schema')
    for name, expected in VERSIONS.items():
        src = data['sources'][name]
        if src['query']['version'] != expected or src['source']['data_version'] != expected:
            raise ValueError('mixed source versions: ' + name)
        if (src['query']['start'], src['query']['end']) != (START, END):
            raise ValueError('wrong observation period')
        if src['source']['provider_available_at'] is not None:
            raise ValueError('historical availability requires separate original evidence')
    # D07 and this run must identify the same prepared and selected-shard versions.
    for name, expected in VERSIONS.items():
        matches = [item for item in upstream['sources']
                   if item['query']['source'] == name and item['query']['symbol'] == SECURITY]
        if len(matches) != 1:
            raise ValueError('D07 target source query is missing or ambiguous')
        prior = matches[0]
        if prior['query']['version'] != expected or prior['source']['data_version'] != expected:
            raise ValueError('D07 source identity does not match')
        if name == 'bigquant':
            current_binding = data['sources'][name]['source']['provenance']['selected_archive_binding']
            if current_binding != prior['source']['provenance']['selected_archive_binding']:
                raise ValueError('D07 selected shard/receipt identity does not match')
    if data['cross_source']['merged'] or data['cross_source']['authoritative_source'] is not None:
        raise ValueError('source conflicts must not be merged or adjudicated')
    if data['eligibility']['formal_admission']['status'] != 'FAIL' or data['boundary'] != BOUNDARY:
        raise ValueError('financial admission boundary changed')


def validate_rows(rows, index):
    ids = {entry['evidence_id']: entry for entry in index}
    seen = set()
    for row in rows:
        key = row['condition'], row['date_or_period']
        if key in seen:
            raise ValueError('duplicate condition-period')
        seen.add(key)
        if row['status'] not in {'PASS', 'FAIL', 'UNKNOWN', 'NOT_APPLICABLE'}:
            raise ValueError('invalid evidence status')
        entry = ids[row['evidence_id']]
        if row['content_sha256'] != entry['content_sha256']:
            raise ValueError('evidence hash binding mismatch')
        allowed_versions = [entry['source_version'], *entry.get('source_versions', {}).values()]
        if row['source_version'] not in allowed_versions:
            raise ValueError('evidence version binding mismatch')
        if row['known_at'] != 'UNKNOWN':
            raise ValueError('this inventory has no historical known_at originals')
        if row['condition'].startswith(('PIT_', 'HFQ_', 'EVENT_completeness_')) and row['status'] != 'UNKNOWN':
            raise ValueError('formal historical condition cannot be certified by this inventory')
        if row['condition'] == 'EVENT_cash_terms' and row['effective_at'] != row['date_or_period']:
            raise ValueError('event implementation date must remain the ex-date')
        if row['condition'] == 'EVENT_original_official_pdf' and row['status'] == 'PASS':
            if entry['location_kind'] != 'PRIVATE_OFFICIAL_PDF':
                raise ValueError('official PDF requires verified original bytes')
        if row['condition'] == 'RAW_exact_cross_source_values' and row['status'] != 'FAIL':
            raise ValueError('observed exact-value conflicts must remain FAIL')
        if row['condition'] == 'FORMAL_admission' and row['status'] != 'FAIL':
            raise ValueError('formal admission must remain blocked')
        if row['status'] == 'UNKNOWN' and (not row['blocker'] or not row['next_action']):
            raise ValueError('unknown conditions require a scoped next action')


def verify_bundle(directory):
    receipt = read_json(directory / 'receipt.json')
    for name, expected in receipt['artifacts'].items():
        if digest((directory / name).read_bytes()) != expected:
            raise ValueError('artifact hash mismatch: ' + name)
    validate_observations(read_json(directory / 'local-observations.json'), read_json(ROOT / UPSTREAM[1]))
    index = read_json(directory / 'evidence-index.json')
    for item in index:
        if item['location_kind'] == 'BUNDLE_FILE':
            if digest((directory / item['original_path_or_url']).read_bytes()) != item['content_sha256']:
                raise ValueError('bundle evidence identity mismatch: ' + item['evidence_id'])
        if item['location_kind'] == 'REPOSITORY_FILE':
            if item['repository_revision'] != BASE:
                raise ValueError('repository evidence revision changed')
            checked_out = (ROOT / item['original_path_or_url']).read_bytes().replace(b'\r\n', b'\n')
            if digest(checked_out) != item['content_sha256']:
                raise ValueError('repository evidence changed: ' + item['evidence_id'])
    with (directory / 'EVIDENCE_MATRIX.csv').open(encoding='utf-8', newline='') as stream:
        rows = list(csv.DictReader(stream))
    validate_rows(rows, index)
    counts = dict(Counter(row['status'] for row in rows))
    if counts != receipt['matrix_counts'] or receipt['boundary'] != BOUNDARY:
        raise ValueError('receipt summary mismatch')
    return {'status': 'PASS', 'scope': 'LOCAL_ENGINEERING_EVIDENCE_BINDINGS_ONLY',
            'matrix_counts': counts, 'boundary': BOUNDARY}


def run(args):
    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=False)
    observations = read_json(Path(args.observations))
    validate_observations(observations, read_json(ROOT / UPSTREAM[1]))
    evidence_index = []

    def bind(eid, location, raw, *, version='CURRENT_REPOSITORY_SNAPSHOT', kind='REPOSITORY_FILE',
             level='THIS_RUN_BYTES_VERIFIED', **extra):
        item = dict(evidence_id=eid, original_path_or_url=location,
                    content_sha256=digest(raw), source_version=version,
                    location_kind=kind, evidence_level=level, **extra)
        evidence_index.append(item)
        return item

    for number, name in enumerate(UPSTREAM):
        bind(f'U{number:02}', name, repository_bytes(name), level='UPSTREAM_RESULT_NOT_RECOMPUTED',
             repository_revision=BASE, hash_representation='GIT_BLOB_BYTES')
    obs_bytes = Path(args.observations).read_bytes()
    obs = bind('OBS', 'local-observations.json', obs_bytes, kind='BUNDLE_FILE',
               version='D05_READER_REEXECUTED_P4D01', source_versions=VERSIONS)
    (out / 'local-observations.json').write_bytes(obs_bytes)
    candidate = Path(args.candidate)
    manifest_bytes = (candidate / 'manifest.json').read_bytes()
    manifest = json.loads(manifest_bytes)
    if 'sha256:' + digest(manifest_bytes) != VERSIONS['csmar']:
        raise ValueError('CSMAR manifest version mismatch')
    bind('CS_MANIFEST', 'CS/manifest.json', manifest_bytes, version=VERSIONS['csmar'], kind='PRIVATE_ORIGINAL')
    # Include the event and factor inputs which D03 reads outside its own hash checks.
    selected = dict(observations['native']['verified_files'])
    selected.update(observations['sources']['csmar']['source']['provenance']['selected_files'])
    for name in ['provenance/TRDNEW_Cptl-0-native.parquet',
                 'provenance/TRD_AdjustFactor-0-native.parquet']:
        selected[name] = manifest['files'][name]
    for number, (name, expected) in enumerate(sorted(selected.items())):
        raw = (candidate / name).read_bytes()
        if digest(raw) != expected or manifest['files'][name] != expected:
            raise ValueError('CSMAR original identity mismatch: ' + name)
        bind(f'CS{number:02}', 'CS/' + name, raw, version=VERSIONS['csmar'], kind='PRIVATE_ORIGINAL')

    # Reuse the existing bounded archive reader; reject any receipt mismatch it reports.
    bindings = []
    stock = Archive(Path(args.bigquant_root), 'stock', bindings)
    index = Archive(Path(args.bigquant_root), 'index', bindings)
    start, end = pd.Timestamp(START), pd.Timestamp(END)
    scans = {}
    try:
        for table, columns in [
            ('cn_stock_dividend', None), ('cn_stock_allotment', None),
            ('cn_stock_adjust_factor', None),
            ('cn_stock_bar1d', ['date', 'instrument', 'open', 'high', 'low', 'close', 'adjust_factor', 'turn']),
        ]:
            frame = stock.read(table, columns, start, end, [SECURITY])
            scans[table] = {'target_rows': len(frame),
                            'nulls': {k: int(v) for k, v in frame.isna().sum().items()}}
        component = index.read('cn_stock_index_component', ['date', 'instrument', 'member_code'],
                               start, end, ['000905.SH'])
        target = component.loc[component.member_code.eq(SECURITY)]
        changes = index.read('cn_stock_index_change', ['date', 'instrument', 'member_code', 'inout_flag'],
                             start, end, ['000905.SH'])
        target_changes = changes.loc[changes.member_code.eq(SECURITY)]
        member_dates = pd.DatetimeIndex(component.date.unique())
        scans['csi500_membership'] = {
            'index': '000905.SH', 'index_rows': len(component), 'index_dates': len(member_dates),
            'first': str(member_dates.min().date()), 'last': str(member_dates.max().date()),
            'target_member_rows': len(target), 'target_unique_dates': int(target.date.nunique()),
            'target_change_rows': len(target_changes),
            'known_at': 'UNKNOWN', 'historical_membership_qualification': 'UNKNOWN',
            'meaning': 'CURRENT_VENDOR_OBSERVATION_NOT_OFFICIAL_MEMBERSHIP_OR_NONMEMBERSHIP_PROOF',
        }
        for number, entry in enumerate(bindings):
            if not entry['hash_matches_receipt']:
                raise ValueError('BQ receipt mismatch: ' + entry['member'])
            archive = stock if entry['archive'] == 'stock.zip' else index
            receipt_raw = archive.zip.read(entry['member'] + '.receipt.json')
            evidence_index.append(dict(evidence_id=f'BQ{number:03}',
                original_path_or_url='BQ/' + entry['archive'] + '!' + entry['member'],
                content_sha256=entry['actual_sha256'], source_version='sha256:' + entry['actual_sha256'],
                location_kind='PRIVATE_ARCHIVE_MEMBER', evidence_level='THIS_RUN_BYTES_VERIFIED',
                receipt_content_sha256=digest(receipt_raw), **entry))
        for archive, tables in [(stock, ['cn_stock_dividend', 'cn_stock_allotment', 'cn_stock_adjust_factor', 'cn_stock_bar1d']),
                                (index, ['cn_stock_index_component', 'cn_stock_index_change'])]:
            for table in tables:
                name = archive.category + '/' + table + '/schema.json'
                raw = archive.zip.read(name)
                bind('SCHEMA_' + table, 'BQ/' + archive.category + '.zip!' + name, raw,
                     kind='PRIVATE_ARCHIVE_MEMBER', version='sha256:' + digest(raw))
    finally:
        stock.zip.close()
        index.zip.close()

    # Verify saved official PDFs against their historical receipts, without downloading.
    official = read_json(Path(args.evidence) / 'corroborated-events-final.json')
    for entry in official:
        notice = entry['official_evidence']
        name = 'official-' + notice['name'] + '.pdf'
        raw = (Path(args.evidence) / name).read_bytes()
        if not raw.startswith(b'%PDF-') or digest(raw) != notice['sha256']:
            raise ValueError('official PDF identity mismatch: ' + name)
        bind('PDF_' + notice['name'], 'EV/' + name, raw, kind='PRIVATE_OFFICIAL_PDF',
             version='sha256:' + notice['sha256'], acquired_at=notice['retrieved_at'])
    bind('EV_RECEIPT', 'EV/corroborated-events-final.json',
         (Path(args.evidence) / 'corroborated-events-final.json').read_bytes(), kind='PRIVATE_RECEIPT')
    write_json(out / 'additional-observations.json', scans)
    extra = bind('EXTRA', 'additional-observations.json', (out / 'additional-observations.json').read_bytes(),
                 kind='BUNDLE_FILE', version='P4D01_BOUNDED_LOCAL_SCAN')
    rows = []

    def row(condition, result, *, status='UNKNOWN', evidence=obs, source='CSMAR / BigQuant',
            period=START + '/' + END, effective='UNKNOWN', announced='UNKNOWN',
            independent='UNKNOWN', blocker='历史版本或独立原件不足', action='R01;R02', exported='UNKNOWN', version=None):
        rows.append(dict(zip(COLUMNS, [condition, SECURITY, period, source, version or evidence['source_version'],
            evidence['evidence_id'], evidence['original_path_or_url'], evidence['content_sha256'],
            effective, announced, 'UNKNOWN', exported, result, independent, status,
            '' if status in ('PASS', 'NOT_APPLICABLE') else blocker,
            '' if status in ('PASS', 'NOT_APPLICABLE') else action])))

    for name in VERSIONS:
        cov = observations['sources'][name]['checks']['coverage']
        row('RAW_' + name + '_observed_grid', json.dumps(cov, ensure_ascii=False),
            status=cov['status'], source=name, version=VERSIONS[name],
            independent='对方供应商观察；上游独立性UNKNOWN')
    cross = observations['cross_source']
    row('RAW_exact_cross_source_values', json.dumps(cross, ensure_ascii=False),
        status=classify(complete=True, conflict=bool(cross['differences'])),
        blocker='4个amount精确值冲突；原因UNRESOLVED', action='R03')
    row('RAW_native_conversion', json.dumps(observations['native'], ensure_ascii=False),
        status=classify(complete=observations['native']['native_decimal_to_prepared_binary64_mismatches'] == 0),
        source='CSMAR native → prepared-v4')
    row('RAW_amount_cause', 'UNRESOLVED：1 ULP距离不是供应商舍入或修订证明', action='R03')
    row('RAW_turnover_contract', 'BQ turn有候选；单位及历史字段合同UNKNOWN；CS prepared未给出合格turnover', action='R03')
    for name in ('security_status', 'calendar'):
        item = observations[name]
        complete = item['rows'] == 487 and item['duplicate_keys'] == 0
        row('OBS_' + name, json.dumps({k: v for k, v in item.items() if 'binding' not in k}, ensure_ascii=False),
            status=classify(complete=complete), source='BigQuant', independent='同供应商独立表，非独立官方来源')
    row('OBS_csi500_target_interval', json.dumps(scans['csi500_membership'], ensure_ascii=False),
        evidence=extra, status='PASS', source='BigQuant cn_stock_index_component / cn_stock_index_change',
        independent='D06/JQ完整日成员包仅覆盖2021-09-13起，未覆盖本研究窗')
    for condition, result, request in [
        ('PIT_membership', '当前BQ成员/变更观察不证明历史成员或非成员资格；不得用当前名单回填', 'R04'),
        ('PIT_security_identity', '当前公司/上市快照和公告代码可关联；稳定ID与历史映射版本UNKNOWN', 'R05'),
        ('PIT_ST_suspension_listing', '487行原码及停牌表0行不证明官方无ST/无停牌/持续上市', 'R05'),
        ('PIT_official_calendar', '487个BQ CN会话已观察；SSE独立会话与临时调整未绑定', 'R06'),
        ('PIT_first_distribution', '两源首次分发、渠道、时区、历史同版载荷UNKNOWN', 'R01;R02'),
        ('PIT_revision_withdrawal', '原始版本、替换/撤销、晚到修订和as-of回放链UNKNOWN', 'R01;R02'),
        ('HFQ_algorithm', 'CS Adjprcwd/Adjprcnd与BQ bar1d/price_gap不是同一认证合同', 'R07'),
        ('HFQ_absolute_anchor', '四事件局部链不包含窗前事件；绝对锚点及累计方向合同UNKNOWN', 'R07'),
        ('HFQ_independent_reference', '公告是事件条款参考，不是独立HFQ价格/因子参考', 'R08'),
        ('HFQ_event_revision_chain', '正文全集、历史删除替换、2019以后追溯更正链UNKNOWN', 'R09;R10'),
    ]:
        row(condition, result, action=request)
    action_matrix = list(csv.DictReader((ROOT / 'docs/validation/p3-d05/ACTION_MATRIX.csv').open(encoding='utf-8')))
    for event in action_matrix:
        date = event['event_ex_date']
        pdf_id = 'PDF_' + ('201806' if date == '2018-06-21' else '201809')
        ref = next(e for e in evidence_index if e['evidence_id'] == pdf_id) if date.startswith('2018') else evidence_index[14]
        row('EVENT_cash_terms', json.dumps(event, ensure_ascii=False), period=date,
            status='PASS', evidence=ref, source='发行人原件' if date.startswith('2018') else 'D04镜像条款（继承未复核原件）',
            effective=date, announced=event['official_SSEDATE'] + ' [SSEDATE日期标签；精确公开时间UNKNOWN]',
            independent='CS/BQ现金候选与公告条款；登记日=' + event['record_date'],
            exported='原件取得时间见evidence-index；非历史known_at')
        row('EVENT_original_official_pdf', '2018保存原件字节匹配历史收据' if date.startswith('2018') else '2017官方端点既有响应为挑战页，不是PDF',
            period=date, evidence=ref, status='PASS' if date.startswith('2018') else 'UNKNOWN',
            action='R09', effective=date)
        row('EVENT_new_share_listing', '仅限该次现金分配，无新增股份上市日', period=date,
            status='NOT_APPLICABLE', evidence=ref, effective=date)
    for category in ['stock_dividend', 'capitalization', 'rights', 'split_merge', 'other_capital_change']:
        row('EVENT_completeness_' + category,
            '候选空值/零行/未发现记录不构成无事件证明；适用性UNKNOWN', action='R10')
    row('OBS_adjusted_price_candidates', json.dumps(scans['cn_stock_bar1d'], ensure_ascii=False),
        evidence=extra, status='PASS', source='BigQuant cn_stock_bar1d',
        independent='CSMAR调整价字段为另一合同；不作融合')
    row('FORMAL_admission', '本次D05读取器调用既有门禁；两源均REJECTED；非QualificationReport',
        status='FAIL', blocker='正式可重放资格包未取得', action='R01;R02;R04;R05;R06;R07;R08;R09;R10')
    validate_rows(rows, evidence_index)
    write_json(out / 'evidence-index.json', evidence_index)
    with (out / 'EVIDENCE_MATRIX.csv').open('x', encoding='utf-8', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    artifacts = {name: digest((out / name).read_bytes()) for name in [
        'local-observations.json', 'additional-observations.json', 'evidence-index.json', 'EVIDENCE_MATRIX.csv']}
    write_json(out / 'receipt.json', dict(schema='P4D01_LOCAL_EVIDENCE_V1', status='PARTIAL',
        created_at_utc=datetime.now(timezone.utc).isoformat(), security=SECURITY, period=[START, END],
        boundary=BOUNDARY, qualification_report=False,
        matrix_counts=dict(Counter(r['status'] for r in rows)), artifacts=artifacts,
        provenance='D05_READER_REEXECUTED; D03_D04_D06_D07_RESULTS_EXPLICITLY_REUSED',
        repository_evidence_binding='Pinned base Git blob bytes; private originals retain exact raw byte hashes',
        raw_shard_binding='local-observations.json sources.bigquant.source.provenance.selected_archive_binding',
        original_replay='CSMAR prepared/native and selected BQ receipts; original XLSX/large ZIP historical references not rescanned'))
    return verify_bundle(out)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--verify', type=Path)
    parser.add_argument('--observations', type=Path)
    parser.add_argument('--candidate', type=Path)
    parser.add_argument('--bigquant-root', type=Path)
    parser.add_argument('--evidence', type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if args.verify:
        result = verify_bundle(args.verify)
    else:
        for name in ['observations', 'candidate', 'bigquant_root', 'evidence', 'output']:
            if getattr(args, name) is None:
                parser.error('--' + name.replace('_', '-') + ' is required for a new scan')
        result = run(args)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
