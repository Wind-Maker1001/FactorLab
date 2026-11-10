"""Read-only bounded semantic audit; never grants market admission.

Run with python -I -B -X utf8. External roots are supplied explicitly; archive
members are streamed into memory and never imported or extracted to disk.
"""
from __future__ import annotations

import argparse
from collections import Counter
import csv
from decimal import Decimal
from io import BytesIO
import json
from pathlib import Path
import subprocess
import zipfile

import openpyxl
import pandas as pd

RESTRICTIONS = ['NO LIVE MONEY', 'PIT_UNVERIFIED', 'HFQ_UNVERIFIED',
                'FORMAL_ADMISSION_BLOCKED']
STATUSES = {'MATCHED_TERMS', 'SEMANTIC_CONFLICT', 'EVENT_NOT_OBSERVED',
            'HISTORICAL_VERSION_UNKNOWN', 'INSUFFICIENT_EVIDENCE', 'NOT_APPLICABLE'}
WINDOW = ('2017-01-03', '2018-12-28')
SYMBOL = '601857.SH'


def value(item):
    """Represent unknown independently of a reported numeric zero."""
    if item is None or pd.isna(item) or item == '':
        return 'UNKNOWN'
    return str(item)


def compare_cash(left, right):
    """Compare declared CNY-per-old-share terms, allowing binary64 encoding only."""
    if value(left) == 'UNKNOWN' or value(right) == 'UNKNOWN':
        return 'INSUFFICIENT_EVIDENCE'
    return ('MATCHED_TERMS' if abs(Decimal(str(left)) - Decimal(str(right)))
            < Decimal('1e-14') else 'SEMANTIC_CONFLICT')


def compare_announcement(left, right, same_stage):
    """Dates from unestablished announcement stages are not equality candidates."""
    if not same_stage or 'UNKNOWN' in (value(left), value(right)):
        return 'INSUFFICIENT_EVIDENCE'
    return 'MATCHED_TERMS' if str(left) == str(right) else 'SEMANTIC_CONFLICT'


def archive_workbook(path, tar):
    names = subprocess.check_output([str(tar), '-tf', str(path)]).decode('gb18030').splitlines()
    member, = [name for name in names if name.endswith('.xlsx')]
    data = subprocess.check_output([str(tar), '-xOf', str(path), member])
    workbook = openpyxl.load_workbook(BytesIO(data), read_only=True, data_only=True)
    sheet = workbook.active
    rows = sheet.iter_rows(values_only=True)
    columns = next(rows)
    next(rows)  # The source workbook's second row contains Chinese field labels.
    selected = []
    scanned = 0
    progress = Counter()
    for physical_row, row in enumerate(rows, 3):
        scanned += 1
        if str(row[0]).zfill(6) == '601857':
            record = dict(zip(columns, row))
            record['_physical_row'] = physical_row
            selected.append(record)
            progress[value(record.get('ImProgress', record.get('Altprog', record.get('Seoprog'))))] += 1
    workbook.close()
    return selected, {'archive': path.name, 'member': member, 'columns': list(columns),
                      'physical_data_rows_streamed': scanned,
                      'target_security_rows_all_dates': len(selected),
                      'target_progress_all_dates': dict(progress),
                      'hash_policy': 'LOCAL_ARCHIVE_READ; NO_INHERITED_PAYLOAD_HASH_AVAILABLE'}


def bq_table(archive, table):
    members = [n for n in archive.namelist()
               if n.startswith(f'stock/{table}/') and n.endswith('.parquet')
               and n.rsplit('/', 1)[1][:4] in ('2017', '2018')]
    pieces = []
    receipts = []
    for name in members:
        frame = pd.read_parquet(archive.open(name))
        frame = frame.loc[frame.instrument == SYMBOL].copy()
        frame['_source_member'] = name
        pieces.append(frame)
        receipt = json.loads(archive.read(name + '.receipt.json'))
        receipts.append({'member': name, 'receipt': receipt})
    result = pd.concat(pieces, ignore_index=True)
    return result.loc[pd.to_datetime(result.date).between(*WINDOW)], receipts


def write_csv(path, rows):
    columns = list(dict.fromkeys(key for row in rows for key in row))
    with path.open('x', encoding='utf-8', newline='') as out:
        writer = csv.DictWriter(out, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def evidence_row(source, subject, status, evidence, observation, minimum):
    return dict(source=source, subject=subject, comparison_status=status,
                evidence=evidence, observation=observation, minimum_evidence=minimum)


def run(repo, cnrds, candidate, stock, tar, output, baseline, p8):
    inherited = json.loads((repo / 'docs/data/p3-d03-summary.json').read_text(encoding='utf-8'))
    cs = pd.read_parquet(candidate / 'provenance/TRDNEW_Cptl-0-native.parquet')
    cs = cs.loc[pd.to_datetime(cs.Exdistdt).between(*WINDOW)]
    cn_tables = {}
    cn_provenance = []
    for table in ('分红实施内容', '分红预案内容', '配股资料', '增发资料'):
        cn_tables[table], info = archive_workbook(cnrds / (table + '.7z'), tar)
        cn_provenance.append(info)
    cn = [r for r in cn_tables['分红实施内容'] if WINDOW[0] <= str(r['DRDt']) <= WINDOW[1]]
    with zipfile.ZipFile(stock) as archive:
        bq, dividend_receipts = bq_table(archive, 'cn_stock_dividend')
        allotment, allotment_receipts = bq_table(archive, 'cn_stock_allotment')
        schemas = {table: json.loads(archive.read(f'stock/{table}/schema.json'))
                   for table in ('cn_stock_real_bar1d', 'cn_stock_bar1d', 'cn_stock_adjust_factor')}
    cn_by_date = {r['DRDt']: r for r in cn}
    actions = []
    matched_cn = 0
    for row in cs.itertuples():
        day = str(row.Exdistdt)[:10]
        other, = bq.loc[pd.to_datetime(bq.ex_date) == pd.Timestamp(day)].to_dict('records')
        c = cn_by_date.get(day)
        for source, record in [('CSMAR', dict(announcement=value(row.Annodt),
                record_date='UNKNOWN', payment_date=value(row.Paydt), cash=value(row.Amount),
                bonus='UNKNOWN', capitalization='UNKNOWN', progress=value(row.Disttyp),
                source_version='prepared-v4; local mapping is not vendor revision',
                evidence='TRDNEW_Cptl.xlsx/sheet1; P3-D03 physical rows 59648-59651')),
                ('BigQuant', dict(announcement=value(other['publish_date']),
                record_date=value(other['register_date']), payment_date='UNKNOWN',
                cash=value(other['cash_before_tax']), bonus=value(other['bonus_rate']),
                capitalization=value(other['conversed_rate']), progress='UNKNOWN',
                source_version='2026-11-07 export; historical vintage UNKNOWN',
                evidence=other['_source_member'] + '; instrument=601857.SH; ex_date=' + day))]:
            actions.append(dict(source=source, security_identity=SYMBOL + '; stable_id UNKNOWN',
                event_type='CASH_DIVIDEND', effective_date=day, **record,
                new_shares_listing_date='NOT_APPLICABLE_TO_CASH', known_at='UNKNOWN',
                comparison_status=compare_cash(row.Amount, other['cash_before_tax']),
                historical_version_status='HISTORICAL_VERSION_UNKNOWN',
                coverage='UNKNOWN', announcement_comparison_status=compare_announcement(
                    row.Annodt, other['publish_date'], same_stage=False)))
        if c is not None:
            cash_status = compare_cash(row.Amount, c['IDPR_PreTax'])
            dates_status = (str(c['RecordDt'])[:10] == str(other['register_date'])[:10]
                            and str(c['DPRDt'])[:10] == str(row.Paydt)[:10])
            status = cash_status if dates_status else 'SEMANTIC_CONFLICT'
            matched_cn += status == 'MATCHED_TERMS'
            actions.append(dict(source='CNRDS', security_identity=SYMBOL + '; stable_id UNKNOWN',
                event_type='CASH_DIVIDEND', effective_date=day,
                announcement=value(c['AnnDt']), record_date=value(c['RecordDt']),
                payment_date=value(c['DPRDt']), cash=value(c['IDPR_PreTax']),
                bonus=value(c['IBnsIssRt']), capitalization=value(c['IEqTnRt']),
                progress=value(c['ImProgress']), source_version='current local export; vintage UNKNOWN',
                evidence=f"分红实施内容.7z/分红实施内容.xlsx/row={c['_physical_row']}",
                new_shares_listing_date='NOT_APPLICABLE_TO_CASH', known_at='UNKNOWN',
                comparison_status=status, historical_version_status='HISTORICAL_VERSION_UNKNOWN',
                coverage='UNKNOWN', announcement_comparison_status=compare_announcement(
                    str(row.Annodt)[:10], c['AnnDt'], same_stage=True)))
        else:
            actions.append(dict(source='CNRDS', security_identity=SYMBOL + '; stable_id UNKNOWN',
                event_type='CASH_DIVIDEND', effective_date=day,
                comparison_status='EVENT_NOT_OBSERVED', coverage='UNKNOWN',
                known_at='UNKNOWN', historical_version_status='HISTORICAL_VERSION_UNKNOWN',
                evidence='分红实施内容.7z; target SCode=601857; DRDt in research window',
                observation='No corresponding implementation row; not evidence of no economic event'))
    for source, category, observation, evidence in [
        ('CNRDS', 'RIGHTS_ISSUE', 'No target rows in local rights workbook across its exported dates', '配股资料.7z'),
        ('CNRDS', 'ADDITIONAL_ISSUE', 'No target rows in local additional-issue workbook across its exported dates', '增发资料.7z'),
        ('BigQuant', 'RIGHTS_ISSUE', f'{len(allotment)} target rows in 2017-2018', 'stock/cn_stock_allotment/2017,2018'),
        ('CSMAR;CNRDS;BigQuant', 'STOCK_DIVIDEND;CAPITALIZATION', 'No positive target terms established; empty values remain UNKNOWN', 'target dividend terms; P3-D03'),
        ('CSMAR;CNRDS;BigQuant', 'SPLIT;CONSOLIDATION;MERGER;SUCCESSION', 'No complete dedicated target event source bound', 'P3-D03; identity-membership.csv'),
        ('CSMAR;CNRDS;BigQuant', 'CANCELLATION;CORRECTION;RETROSPECTIVE_REVISION', 'No version-linked target correction chain; progress state is not revision history', 'CNRDS ImProgress; P3-D03; P3-D05')]:
        actions.append(dict(source=source, security_identity=SYMBOL, event_type=category,
            effective_date='UNKNOWN', known_at='UNKNOWN',
            comparison_status='EVENT_NOT_OBSERVED' if category in ('RIGHTS_ISSUE', 'ADDITIONAL_ISSUE') else 'INSUFFICIENT_EVIDENCE',
            coverage='UNKNOWN', evidence=evidence, observation=observation))
    proposals = [r for r in cn_tables['分红预案内容'] if str(r['Year']) in ('2017', '2018')]
    for r in proposals:
        actions.append(dict(source='CNRDS', security_identity=SYMBOL,
            event_type='ANNUAL_PROPOSAL_NOT_AN_IMPLEMENTED_EVENT', effective_date='UNKNOWN',
            accounting_year=value(r['Year']), cash=value(r['PDPR_PreTax']), known_at='UNKNOWN',
            comparison_status='INSUFFICIENT_EVIDENCE', coverage='UNKNOWN',
            evidence=f"分红预案内容.7z/row={r['_physical_row']}",
            observation='Accounting year has no announcement/effective date; do not match to September implementation by Year'))
    write_csv(output / 'corporate-actions.csv', actions)
    contracts = []
    missing_contract = 'Versioned algorithm, anchor date/value, pre-window events, precision/order, corrections and historical first delivery'
    for source, subject, observation, evidence in [
        ('CSMAR', 'RAW versus Adjprcwd', 'Adjprcwd comparable cash-reinvestment candidate is not execution RAW or certified HFQ', 'docs/data/DATAHUB.md; P3_D03_ADJUSTMENT_RECONCILIATION.md'),
        ('CSMAR', 'FwardFactor/BwardFactor versus cumulative fields', '4 event factors; cumulative factors cannot be replaced by local four-event chain', 'docs/data/p3-d03-summary.json; P3_D03_ADJUSTMENT_RECONCILIATION.md'),
        ('BigQuant', 'cn_stock_bar1d.adjust_factor versus cn_stock_adjust_factor', 'Daily and event tables share a name, not an established normalization or composition contract; no price-ratio fitting', 'stock schemas; docs/data/P3_D03_ADJUSTMENT_RECONCILIATION.md'),
        ('BigQuant', 'RAW/adjusted/pre_close and forward/backward price_gap', 'Schema declares data types only; direction, anchor, adjusted price relationship and rounding remain unestablished', 'stock/cn_stock_real_bar1d/schema.json; cn_stock_bar1d/schema.json; cn_stock_adjust_factor/schema.json'),
        ('CNRDS', 'Dclsprc versus adjusted and forward-adjusted close', 'CNSP dictionary distinguishes raw, adjusted and forward-adjusted CNY close; CRSP reference does not specify absolute HFQ contract', '数据库说明/中国上市公司股价数据库说明书.pdf p1-2; P8-03 CNSP 2018 dictionary'),
        ('ALL', 'Historical as-of reconstruction', 'Later dividends change current QFQ anchors; backward name alone does not prevent revision look-ahead', 'P3-D03; DATAHUB historical cutoff contract')]:
        contracts.append(evidence_row(source, subject, 'INSUFFICIENT_EVIDENCE', evidence, observation, missing_contract))
    for r in inherited['factor_differences_derived_not_vendor_rows']:
        contracts.append(evidence_row('CSMAR;BigQuant', '601857.SH/' + r['event_date'],
            'SEMANTIC_CONFLICT', 'docs/data/p3-d03-summary.json',
            'Inherited numerical difference, not proof either vendor is erroneous: forward BQ-CS=' +
            r['bigquant_minus_csmar_forward'] + '; backward BQ-CS=' + r['bigquant_minus_csmar_backward'] +
            '; BQ method=' + r['bigquant_method'], missing_contract))
    write_csv(output / 'adjustment-contracts.csv', contracts)
    metrics = dict(csmar_cash_candidates=len(cs), bigquant_cash_candidates=len(bq),
        cnrds_implementation_candidates=len(cn), cnrds_matched_cash_record_payment_groups=matched_cn,
        csmar_only_ex_dates=sorted(set(cs.Exdistdt.astype(str)) - set(pd.to_datetime(bq.ex_date).dt.strftime('%Y-%m-%d'))),
        bigquant_only_ex_dates=sorted(set(pd.to_datetime(bq.ex_date).dt.strftime('%Y-%m-%d')) - set(cs.Exdistdt.astype(str))),
        cnrds_unobserved_ex_dates=sorted(set(cs.Exdistdt.astype(str)) - set(cn_by_date)),
        cnrds_only_ex_dates=sorted(set(cn_by_date) - set(cs.Exdistdt.astype(str))),
        cnrds_unobserved_candidate_groups=len(cs) - len(cn),
        cnrds_accounting_year_proposals_not_event_matches=len(proposals),
        inherited_factor_difference_groups=len(inherited['factor_differences_derived_not_vendor_rows']),
        cnrds_workbooks_read=len(cn_provenance), cnrds_physical_rows_streamed=sum(
            x['physical_data_rows_streamed'] for x in cn_provenance))
    provenance = dict(cnrds=cn_provenance,
        bigquant_receipts=dividend_receipts + allotment_receipts,
        bigquant_schemas=schemas, inherited_p3_d03=inherited,
        p8_03=dict(state='OPEN_NOT_MERGED', baseline_artifact='docs/validation/p8-03/REPORT.md',
                   inspected_from='feat/p8-03-evidence-coverage',
                   inspected_commit='32279f8694b4497d837b940e7565267e7ae720b6',
                   report_available=(p8 / 'docs/validation/p8-03/REPORT.md').is_file()),
        policy='Read-only originals; no whole-archive rehash; old hashes are receipt references, not freshly verified payloads')
    return metrics, provenance


def summarize(output, baseline, metrics, provenance):
    names = ['corporate-actions.csv', 'adjustment-contracts.csv', 'identity-membership.csv',
             'pit-evidence.csv', 'financial-semantics.csv']
    matrices = {}
    for name in names:
        with (output / name).open(encoding='utf-8-sig', newline='') as stream:
            matrices[name] = list(csv.DictReader(stream))
    gaps = []
    for name, rows in matrices.items():
        for i, row in enumerate(rows, 2):
            if row['comparison_status'] not in STATUSES:
                raise ValueError(f'Unknown comparison status in {name}:{i}')
            if row['comparison_status'] != 'MATCHED_TERMS' and row['comparison_status'] != 'NOT_APPLICABLE':
                gaps.append(dict(source=row['source'], subject=row.get('subject', row.get('event_type')),
                    comparison_status=row['comparison_status'], evidence=f'{name}:{i}',
                    affected_capability='Historical signals/execution or full total return; not a ban on bounded retrospective arithmetic',
                    minimum_evidence=row.get('minimum_evidence') or
                        'Target event implementation/correction index, vendor stage/availability/version contract; do not infer absence from this export'))
    write_csv(output / 'minimum-evidence.csv', gaps)
    metrics['matrix_rows'] = {name: len(rows) for name, rows in matrices.items()}
    metrics['comparison_status_counts'] = dict(Counter(row['comparison_status'] for rows in matrices.values() for row in rows))
    result = dict(task_id='TRI_SOURCE_SEMANTICS_C', baseline_sha=baseline,
        sources=['CSMAR', 'CNRDS', 'BigQuant'],
        scope=dict(corporate_actions={"security": SYMBOL, "effective_window": list(WINDOW)},
                   other_assets='Field/version and inherited bounded identity/membership audit; no full-market daily numerical audit'),
        metrics=metrics,
        findings=['CNRDS implemented table overlaps two annual cash events but lacks the two September candidates in this export',
                  'Announcement stages cannot be equated across vendors',
                  'Four inherited price_gap factor disagreements remain algorithm/precision/anchor questions, not vendor error certification',
                  'Current retrospective membership and financial effective dates do not establish historical first availability'],
        gaps=gaps, artifacts=names + ['minimum-evidence.csv', 'REPORT.md', 'summary.json'],
        status='PARTIAL_SEMANTICS_AUDITED', restrictions=RESTRICTIONS, provenance=provenance)
    with (output / 'summary.json').open('x', encoding='utf-8') as out:
        json.dump(result, out, ensure_ascii=False, indent=2, default=str)
        out.write('\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('repo', 'cnrds', 'candidate', 'stock', 'tar', 'output', 'p8'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--baseline', required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    metrics, provenance = run(args.repo, args.cnrds, args.candidate, args.stock,
        args.tar, args.output, args.baseline, args.p8)
    summarize(args.output, args.baseline, metrics, provenance)
    print(json.dumps(metrics, ensure_ascii=False))


if __name__ == '__main__':
    main()
