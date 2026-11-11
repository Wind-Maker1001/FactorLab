"""Integrate committed audit evidence; never open supplier data."""
from __future__ import annotations

import argparse
import collections
import csv
import io
import json
from pathlib import Path
import subprocess
import time

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'docs/validation/dq-c01'
GATES = ['NO LIVE MONEY', 'PIT_UNVERIFIED', 'HFQ_UNVERIFIED', 'FORMAL_ADMISSION_BLOCKED']
CONTENT = {'FULL_CONTENT_SCANNED', 'PARTIAL_CONTENT_SCANNED', 'METADATA_ONLY', 'NOT_VERIFIED'}
KEYS = {'KEY_EXACT_VERIFIED', 'CANDIDATE_KEY_EXACT', 'KEY_CHECK_BLOCKED_RESOURCE', 'KEY_CONTRACT_UNKNOWN', 'KEY_CONFLICT_OBSERVED', 'NOT_ASSESSED'}
ADMISSION = {'READY_SCOPED', 'CONDITIONALLY_USABLE', 'BLOCKED', 'NOT_ASSESSED'}


def git(*args):
    return subprocess.check_output(['git', '-C', str(ROOT), *args]).decode('utf-8-sig').strip()


class Evidence:
    def __init__(self, main, bigquant, state):
        self.main, self.bigquant, self.state = main, bigquant, state
        self.entries = {}

    def text(self, path, scope):
        sha = self.bigquant if '/dq-02b/' in path else self.main
        identity = f'{sha}:{path}'
        data = git('show', identity)
        self.entries[path] = dict(path=path, audit=path.split('/')[2], commit=sha,
                                  blob=git('rev-parse', identity),
                                  version_time=git('show', '-s', '--format=%cI', sha),
                                  integration_state=self.state if '/dq-02b/' in path else 'MAIN_ACCEPTED',
                                  verification_status='COMMITTED_OBJECT_READ', evidence_scope=scope)
        return data

    def csv(self, path, scope):
        return list(csv.DictReader(io.StringIO(self.text(path, scope))))

    def json(self, path, scope):
        return json.loads(self.text(path, scope))


def path(audit, name):
    return f'docs/validation/{audit}/{name}'


def ref(audit, name):
    return path(audit, name)


def write_csv(name, rows):
    with (OUT / name).open('w', newline='', encoding='utf-8') as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def key_status(row, partial=False):
    if int(row['conflicting_keys']) > 0:
        return 'KEY_CONFLICT_OBSERVED'
    if partial:
        return 'NOT_ASSESSED'
    if json.loads(row['candidate_key_variants']):
        return 'CANDIDATE_KEY_EXACT'
    return 'KEY_CONTRACT_UNKNOWN'


def validate(rows, tables, native, native_keys, bq_keys, joins):
    base = [r for r in rows if r['record_scope'] == 'TABLE']
    identity = lambda r: (r['source'], r['logical_table'])
    assert len(base) == len({identity(r) for r in base}) == 156
    assert {identity(r) for r in base} == {identity(r) for r in tables}
    assert collections.Counter(r['source'] for r in base) == {'CSMAR': 28, 'CNRDS': 67, 'BigQuant': 61}
    assert sum(int(r['partitions_discovered']) for r in base) == 3550
    assert sum(int(r['partitions_content_scanned']) for r in base) == 3507
    assert sum(int(r['partitions_inherited_limited']) for r in base) == 43
    assert sum(int(r['actual_scanned_rows']) for r in base if r['source'] == 'CSMAR') == 105936113
    assert sum(int(r['actual_scanned_rows']) for r in base if r['source'] == 'CNRDS') == 43523306
    assert sum(int(r['actual_scanned_rows']) for r in base if r['source'] == 'BigQuant') == 450958087
    assert collections.Counter(r['status'] for r in bq_keys) == {'CANDIDATE_KEY_EXACT': 20, 'KEY_CHECK_BLOCKED_RESOURCE': 16, 'KEY_CONTRACT_UNKNOWN': 25}
    assert collections.Counter(r['key_status'] for r in base if r['source'] == 'BigQuant') == collections.Counter(r['status'] for r in bq_keys)
    conflicts = {(r['source'], r['table']) for r in native_keys if int(r['conflicting_keys']) > 0}
    assert len(conflicts) == 21
    assert all(r['key_status'] == 'KEY_CONFLICT_OBSERVED' for r in base if identity(r) in conflicts)
    for r in rows:
        assert r['content_status'] in CONTENT and r['key_status'] in KEYS and r['research_admission'] in ADMISSION
        assert r['key_status'] != 'KEY_EXACT_VERIFIED'
        assert r['pit_status'] == 'PIT_UNVERIFIED' and r['hfq_status'] == 'HFQ_UNVERIFIED'
        assert r['evidence_ref'] and r['evidence_commit'] and r['evidence_scope']
        assert r['research_admission'] not in {'READY_SCOPED', 'CONDITIONALLY_USABLE'} or r['conditions']
        if r['content_status'] == 'PARTIAL_CONTENT_SCANNED':
            assert r['total_physical_rows'] == '' and r['unscanned_rows'] == ''
            assert r['key_status'] != 'KEY_EXACT_VERIFIED'
    assert joins['BigQuant']['NO_PRICE_KEY'] == 292 and joins['BigQuant']['MISSING_OHLC'] == 564
    assert joins['CSMAR']['NO_PRICE_KEY'] == 19 and joins['CNRDS']['NO_PRICE_KEY'] == 26641
    assert all(sum(v.values()) == 605500 for v in joins.values())
    return dict(source_isolation=True, table_denominator=True, partition_conservation=True,
                row_scopes_not_added=True, inherited_43_not_promoted=True,
                bigquant_20_16_25=True, conflicts_21_not_certified=True,
                unknown_rows_preserved=True, financial_gates_preserved=True,
                admission_has_conditions=True, membership_price_conservation=True)


def build(main, bigquant, state):
    start = time.perf_counter()
    ev = Evidence(main, bigquant, state)
    tables = ev.csv(path('dq-01', 'logical-tables.csv'), 'registered source-isolated logical tables; workbook sheet discovery boundary retained')
    coverage = ev.json(path('dq-01', 'verification.json'), 'asset/table/partition registration, not new content scans')['coverage']
    native = ev.csv(path('dq-02', 'table-quality.csv'), 'actual all-field native reads versus limited inherited market members')
    nk = ev.csv(path('dq-02', 'primary-key-quality.csv'), 'within-source candidate keys, not supplier business contracts')
    ev.json(path('dq-02', 'verification.json'), '349 members, 149459419 actual rows')
    bt = ev.csv(path('dq-02b', 'table-quality.csv'), 'all 3201 Parquet partitions actual content reads')
    bk = ev.csv(path('dq-02b', 'key-quality.csv'), 'exact candidate grouping versus resource/recovery/contract gaps')
    ba = ev.csv(path('dq-02b', 'asset-coverage.csv'), '10 ZIP assets, 9 containing Parquet')
    bv = ev.json(path('dq-02b', 'verification.json'), 'content and candidate-key conservation')
    budget = ev.json(path('dq-02b', 'resource-budget.json'), 'recorded limits and measured resource fields; not DQC01 budget')
    fields = ev.csv(path('dq-02b', 'field-quality.csv'), 'field-partition aggregate statistics; not financial-semantic certification')
    nonfinite = sum(int(r['nan']) > 0 or int(r['inf']) > 0 for r in fields)
    for audit in ('dq-01', 'dq-02', 'dq-03', 'dq-02b', 'p9-d02-preflight', 'p9-c02', 'p10-g01', 'p10-d02', 'p9-d03'):
        ev.text(path(audit, 'REPORT.md'), 'report interpretation and scope restrictions')
    # The implementation is versioned evidence, not executed by this integrator.
    code = git('show', f'{bigquant}:tools/data/dq02b_columnar.py')
    resource_reason_512 = '512 MiB per-table ceiling' in code
    ev.entries['tools/data/dq02b_columnar.py'] = dict(path='tools/data/dq02b_columnar.py', audit='dq-02b', commit=bigquant,
        blob=git('rev-parse', f'{bigquant}:tools/data/dq02b_columnar.py'), version_time=git('show', '-s', '--format=%cI', bigquant),
        integration_state=state, verification_status='COMMITTED_OBJECT_READ', evidence_scope='resource bound code/text only; not executed')
    jrows = ev.csv(path('dq-03', 'join-coverage.csv'), 'CSI500 2021-09-13..2026-09-11, mutually exclusive price statuses')
    joins = {s: collections.Counter() for s in ('CSMAR', 'CNRDS', 'BigQuant')}
    for r in jrows:
        if r['relation'] == 'membership_price':
            joins[r['source']][r['status']] += int(r['count'])
    dv = ev.json(path('dq-03', 'verification.json'), 'bounded identity, financial, industry, holdings and event checks')
    for audit, name, scope in [
        ('dq-03', 'financial-temporal-checks.csv', 'FS_BComins report-type and historical version diagnostics'),
        ('dq-03', 'industry-history-checks.csv', '2023-03 representative industry join'),
        ('dq-03', 'corporate-action-links.csv', '16 event identities and stage evidence'),
        ('p9-c02', 'results.json', 'two frozen first-fold accounts; nine unresolved events'),
        ('p9-d02-preflight', 'summary.json', '128 fixed securities, 92928 test security-days'),
        ('p9-d03', 'REPORT.md', 'targeted valuation policy, not execution permission'),
        ('p10-g01', 'price-contract.json', 'version-bound field consumption requirements'),
        ('p10-g01', 'validation-cases.json', 'qualified control object versus rejected candidate/fill objects'),
        ('p10-d02', 'summary.json', 'price source discrepancies; not global equivalence')]:
        ev.text(path(audit, name), scope)
    nmap = {(r['source'], r['table']): r for r in native}
    kmap = {(r['source'], r['table']): r for r in nk}
    bmap = {r['table']: r for r in bt}
    bkmap = {r['table']: r for r in bk}
    rows = []
    for t in tables:
        s, table = t['source'], t['logical_table']
        bq = s == 'BigQuant'
        q = bmap[table] if bq else nmap[s, table]
        ks = bkmap[table]['status'] if bq else key_status(kmap[s, table], q['status'] != 'FULL_CONTENT_SCANNED')
        scanned = int(q['full_content_partitions'] if bq else q['read_partitions'])
        discovered = int(t['partitions'])
        assert discovered == int(q['partitions'] if bq else q['discovered_partitions'])
        status = 'FULL_CONTENT_SCANNED' if scanned == discovered else 'PARTIAL_CONTENT_SCANNED'
        audit = 'dq-02b' if bq else 'dq-02'
        refs = [ref('dq-01', 'logical-tables.csv'), ref(audit, 'table-quality.csv'), ref(audit, 'key-quality.csv' if bq else 'primary-key-quality.csv')]
        category = t['category']
        use = {'stock_daily': 'RETROSPECTIVE_PRICE_EXPLORATION', 'financial_statements': 'FINANCIAL_FACTOR_EXPLORATION',
               'adjustment_factors': 'ADJUSTMENT_RECIPE_EXPLORATION', 'trading_status': 'HISTORICAL_STATE_DIAGNOSTIC',
               'indices_constituents': 'HISTORICAL_MEMBERSHIP_EXPLORATION', 'corporate_actions': 'EVENT_DISCOVERY_NOT_CASH_POSTING',
               'industry': 'SYSTEM_QUALIFIED_INDUSTRY_EXPLORATION'}.get(category, 'TABLE_STATISTICS_EXPLORATION')
        blockers = [ks] if ks in {'KEY_CONFLICT_OBSERVED', 'KEY_CHECK_BLOCKED_RESOURCE', 'KEY_CONTRACT_UNKNOWN', 'NOT_ASSESSED'} else []
        blockers += ['BUSINESS_KEY_NOT_CERTIFIED', 'SEMANTICS_NOT_CERTIFIED', 'PIT_UNVERIFIED', 'HFQ_UNVERIFIED']
        if status != 'FULL_CONTENT_SCANNED':
            blockers.append('HISTORICAL_ALL_FIELD_COVERAGE_UNKNOWN')
        conditions = 'Exploration only; bind source/asset/member version; preserve nulls, report types, revisions and event dimensions; no keep-first/last; resolve relevant conflicts before numeric consumption; no formal/live admission'
        if category == 'stock_daily':
            conditions += '; field-level raw identity/units/status/coverage contract required; no cross-source auto-selection; no fill as trade'
        if category in {'financial_statements', 'industry', 'indices_constituents'}:
            conditions += '; no historical signal-time claim without first-public/vendor-availability and version evidence; no current snapshot backfill'
        if category == 'corporate_actions':
            conditions += '; event presence does not prove implementation, entitlement, payment or tax'
        if category == 'adjustment_factors':
            conditions += '; adjustment type/method/event chain must be independently bound; factor existence is not HFQ certification'
        rows.append(dict(source=s, logical_table=table, record_scope='TABLE', category=category,
            asset_scope=t['assets'], partitions_discovered=discovered, partitions_content_scanned=scanned,
            partitions_inherited_limited=discovered-scanned, content_status=status, key_status=ks,
            business_key_contract='NOT_CONFIRMED', semantic_status='NOT_ASSESSED', pit_status='PIT_UNVERIFIED', hfq_status='HFQ_UNVERIFIED',
            actual_scanned_rows=q['rows'] if bq else q['readable_scanned_rows'],
            declared_rows='', inherited_evidence_rows='' if bq else q['inherited_selected_rows'],
            total_physical_rows=q['rows'] if bq else q['physical_data_rows'], unscanned_rows='',
            research_use_case=use, research_admission='CONDITIONALLY_USABLE', blockers=';'.join(blockers), conditions=conditions,
            evidence_ref=';'.join(refs), evidence_commit=json.dumps({p: ev.entries[p]['commit'] for p in refs}, sort_keys=True),
            evidence_scope='Registered partitions only; actual all-field reads separate from inherited selected rows/fields/windows; no workbook-sheet completeness claim',
            evidence_state=state if bq else 'MAIN_ACCEPTED', confidence='HIGH'))
    def scoped(source, table, use, admission, scope, blockers, conditions, audits):
        base = next(r for r in rows if r['source'] == source and r['logical_table'] == table and r['record_scope'] == 'TABLE')
        r = dict(base)
        refs = base['evidence_ref'].split(';') + audits
        r.update(record_scope='USE_CASE', research_use_case=use, research_admission=admission,
                 semantic_status='SCOPED_EVIDENCE_ONLY', evidence_scope=scope, blockers=blockers, conditions=conditions,
                 evidence_ref=';'.join(refs), evidence_commit=json.dumps({p: ev.entries[p]['commit'] for p in refs}, sort_keys=True))
        rows.append(r)
    for s, table in [('BigQuant', 'stock/cn_stock_bar1d'), ('CSMAR', 'TRDNEW_Dalyr'), ('CNRDS', '个股日回报率')]:
        j = joins[s]
        scoped(s, table, 'CSI500_RETROSPECTIVE_RAW_INPUT', 'CONDITIONALLY_USABLE',
            'CSI500 2021-09-13..2026-09-11;1211 sessions;932 codes;605500 member-days;' + json.dumps(j, sort_keys=True),
            'IDENTITY_UNKNOWN;MEMBERSHIP_TIME_UNKNOWN;PIT_UNVERIFIED;HFQ_UNVERIFIED;NO_BLANKET_RAW_QUALIFICATION',
            'Use only individually qualified source/version/field/date contracts; retain missing-key/OHLC states; no alias backfill, survivor filter or model valuation as trade; P10 scoped entry only; legacy DataFrame API is not globally guarded',
            [ref('dq-03', 'join-coverage.csv'), ref('dq-03', 'REPORT.md'), ref('p10-g01', 'REPORT.md')])
    scoped('CSMAR', 'TRDNEW_Dalyr', '601857_RETROSPECTIVE_RAW_CONTROL', 'READY_SCOPED',
        'P10-G01 fixed CSMAR 601857.SH control object, 2017-2018, 487 observed days; only exact version and fields in upstream contract',
        'PIT_UNVERIFIED;HFQ_UNVERIFIED;FORMAL_ADMISSION_BLOCKED',
        'Retrospective raw labels/factors and fixed compatibility probe only; require exact upstream dictionary, source version and nonfill qualification; no extrapolation to CSI500 or other objects; cutoff queries reject unknown availability',
        [ref('p10-g01', 'REPORT.md'), ref('p10-g01', 'price-contract.json'), ref('p10-g01', 'validation-cases.json')])
    scoped('BigQuant', 'stock/cn_stock_status', 'SIGNAL_TIME_STATUS_OR_EXECUTION', 'BLOCKED',
        'P9-D02 128 securities / 92928 Test security-days; st_status=2 adapter loss 250 days, 26 member-days',
        'ENUM_2_CONTRACT_UNKNOWN;FIRST_AVAILABLE_UNKNOWN',
        'Keep raw enum; do not map all 2 to true; only independently qualified exact issuer/date fact may be used; P9 diagnostics never alter historical candidate pool',
        [ref('p9-d02-preflight', 'summary.json'), ref('p9-d02-preflight', 'REPORT.md')])
    scoped('BigQuant', 'index/cn_stock_index_component', 'CSI500_EX_POST_HISTORICAL_MEMBERS', 'CONDITIONALLY_USABLE',
        'DQ03 exact historical CSI500 member keys, 605500 member-days; not all index-component tables',
        'MEMBERSHIP_TIME_UNKNOWN;IDENTITY_UNKNOWN;PIT_UNVERIFIED',
        'Use exact-date historical rows only after binding the DQ03 membership asset identity to this consumer; no current-member replacement; knowledge_as_of remains rejected; do not assume logical-table name proves the separate CSI500 archive lineage',
        [ref('dq-03', 'verification.json'), ref('dq-03', 'REPORT.md')])
    # The CSI500 audit used a separate archive: no fabricated table-to-asset lineage.
    rows[-1]['research_admission'] = 'NOT_ASSESSED'
    rows[-1]['blockers'] += ';INSUFFICIENT_EVIDENCE_TABLE_TO_CSI500_ARCHIVE_BINDING'
    scoped('CSMAR', 'FS_BComins', 'HISTORICAL_PIT_FINANCIAL_SIGNAL', 'BLOCKED',
        '4878 records; report-type join 4878 unique versus 2673 company-period keys; 885 member-linked records PIT_UNKNOWN;22 later-correction candidates',
        'FIRST_PUBLIC_VALUES_MISSING;REVISION_CHAIN_MISSING;PIT_UNVERIFIED',
        'Keep Typrep A/B; do not backfill current corrected values or convert EndDate to flow-period end without contract; no claim current models consumed leakage',
        [ref('dq-03', 'financial-temporal-checks.csv'), ref('dq-03', 'REPORT.md')])
    for table in ('stock/cn_stock_dividend', 'stock/cn_stock_allotment'):
        scoped('BigQuant', table, 'SAVED_P9_INVESTOR_TOTAL_RETURN', 'BLOCKED',
            'P9-C02 FactorScore/Ridge fold0;9 dividend candidates/14 account-event intersections; no allotment intersection observed in bounded query',
            'IMPLEMENTATION_PAYMENT_TAX_AND_COMPLETENESS_UNKNOWN;PIT_UNVERIFIED',
            'Preserve frozen raw-price-return ledger; no cash posting from supplier record; independent implementation, entitlement, payment and tax evidence required; unknown totals stay null',
            [ref('p9-c02', 'results.json'), ref('p9-c02', 'REPORT.md'), ref('dq-03', 'corporate-action-links.csv')])
    impacts = []
    def impact(i, source, table, scope, observed, kind, route, severity, action, mitigation, refs):
        impacts.append(dict(issue_id=i, source=source, affected_table=table, affected_scope=scope,
            observed_evidence=observed, actual_or_potential=kind, affected_research_path=route,
            severity=severity, confidence='HIGH', required_action=action, existing_mitigation=mitigation,
            evidence_ref=';'.join(refs)))
    for s, table in [('BigQuant', 'stock/cn_stock_bar1d'), ('CSMAR', 'TRDNEW_Dalyr'), ('CNRDS', '个股日回报率')]:
        impact('PRICE_'+s, s, table, 'CSI500 2021-09-13..2026-09-11;605500 member-days', json.dumps(joins[s], sort_keys=True),
            'OBSERVED_INPUT', 'bounded historical price join; not newly measured model PnL', 'HIGH',
            'Resolve exact missing intervals or preserve exclusion/blocking at price consumption, not signal-time universe filtering',
            'No one-to-many price expansion observed; missing price is not silently aliased', [ref('dq-03', 'join-coverage.csv'), ref('dq-03', 'REPORT.md')])
    impact('P9_STATUS', 'BigQuant', 'stock/cn_stock_status', 'P9 fixed128 Test cohort;000540 2023-05-08',
        'st_status=2:250 security-days,26 member-days; recorded LightGBM abort; no returned partial ledger',
        'OBSERVED_EXECUTION_BLOCK', 'real OOS execution', 'HIGH', 'Bind enum semantics and exact historical issuer fact before adapter consumption',
        'Unsupported enum rejected; diagnostics are ex post, not signal-time facts', [ref('p9-d02-preflight', 'REPORT.md')])
    impact('P9_VALUATION', 'BigQuant', 'stock/cn_stock_bar1d', 'FactorScore/Ridge fold0 002408 four days each',
        '8 saved account-days missing true close; targeted valuation policies remain separate from trade prices', 'OBSERVED_ACCOUNT_INPUT',
        'saved portfolio valuation; not investor total return', 'HIGH', 'Retain exact P9-D03 policy/version; extend only to newly exposed bounded intervals',
        'Separate Valuation object; no carry injected into raw_close; frozen policy approval tracked separately',
        [ref('p9-d02-preflight', 'REPORT.md'), ref('p9-d03', 'REPORT.md')])
    impact('CORPORATE_CASH', 'BigQuant', 'stock/cn_stock_dividend', 'two frozen fold0 accounts;9 events/14 intersections',
        '0 fully implemented-and-payment-verified events; actual receivable/paid/tax totals unknown, not zero', 'OBSERVED_EVIDENCE_GAP',
        'investor total return and distributable cash', 'CRITICAL', 'Obtain nine event implementation/publication/payment/tax and bounded full-event coverage',
        'EVENT_UNRESOLVED; no unverified postings; raw-price-return label retained', [ref('p9-c02', 'REPORT.md'), ref('p9-c02', 'results.json')])
    impact('IDENTITY_TRANSITION', 'BigQuant', 'stock/cn_stock_bar1d', '300114/302132 and600068/601868;current CSI500 window',
        '283 old-code missing days;9 post-delisting member-days; exchange/share transition not a 1:1 alias', 'OBSERVED_INPUT',
        'historical price/rights joins', 'HIGH', 'Bind date-effective IDs and nine membership/rights transition days',
        'No undated alias substitution or fabricated normal trades', [ref('dq-03', 'REPORT.md')])
    impact('INDUSTRY_JOIN', 'BigQuant', 'industry/cn_stock_industry_component', '2023-03;11500 member-days',
        'Joining without classification system yields34500 rows (+23000)', 'POTENTIAL_RESEARCH_RISK',
        'industry exposure/factor join; not demonstrated model consumption', 'HIGH', 'Preserve cs/sw2014/sw2021 and obtain hierarchy/version/interval definitions',
        'System-aware keys avoid observed expansion', [ref('dq-03', 'industry-history-checks.csv'), ref('dq-03', 'REPORT.md')])
    impact('FINANCIAL_PIT', 'CSMAR', 'FS_BComins', '885 member-linked records;22 later-correction candidates',
        '885 PIT_UNKNOWN; no original public values or version chain', 'POTENTIAL_RESEARCH_RISK',
        'historical financial signals; actual model leakage not established', 'HIGH', 'Obtain first-public value/version chain for intended consumed records only',
        'No bank-table PIT certification; report types retained', [ref('dq-03', 'financial-temporal-checks.csv'), ref('dq-03', 'REPORT.md')])
    impact('CNRDS_ISOLATED', 'CNRDS', '个股日回报率', '2021-01;outside CSI500 window',
        '84514 ambiguous keys quarantined;5300 name-conflict keys/265 codes;0 direct current-window member-days', 'MITIGATED_RISK',
        'future extension into2021-01 only', 'MEDIUM', 'Keep quarantine; obtain independent identity/version evidence only if consuming interval',
        'All84514 keys isolated without selecting a row', [ref('dq-03', 'REPORT.md')])
    impact('PRICE_BASIS', 'CSMAR;CNRDS;BigQuant', 'TRDNEW_Dalyr;stock/cn_stock_adjust_factor;个股日回报率',
        'P10 exact field objects and targeted windows only', 'Adjusted candidates/fills are not raw prices; old DataFrame API is not globally guarded',
        'MITIGATED_RISK', 'raw labels/factors/execution;adjusted research', 'HIGH', 'Bind actual consumed source/version/field contracts and migrate caller to scoped entries',
        'P10 scoped adapters reject missing qualification, model carry and unknown cutoff availability; no whole-library guarantee',
        [ref('p10-g01', 'REPORT.md'), ref('p10-g01', 'price-contract.json'), ref('p10-d02', 'summary.json')])
    impact('CANDIDATE_CONFLICTS', 'CSMAR;CNRDS', '21 tables listed by DQ02 conflicting_keys>0',
        'within-source candidate-key scope only', '21 candidate conflict tables; no supplier business-key certification',
        'POTENTIAL_RESEARCH_RISK', 'only future/actual consumers of affected tables, not all FactorLab models', 'MEDIUM',
        'Confirm business dimensions and consumed conflict groups before selecting versions', 'No keep-first/last or supplier record deletion',
        [ref('dq-02', 'primary-key-quality.csv'), ref('dq-02', 'REPORT.md')])
    work = []
    def todo(i, priority, scope, minimum, uncertainty, raw, checkpoint, cost, stop, refs):
        work.append(dict(task_id=i, priority=priority, affected_scope=scope, existing_evidence=';'.join(refs),
            minimum_new_check=minimum, uncertainty_removed=uncertainty, requires_raw_data=raw,
            reusable_checkpoint=checkpoint, execution_cost_and_resource_risk=cost, stop_condition=stop))
    todo('W01', 'P0', 'saved P9 nine dividend events and bounded positive-holding intervals',
         'Bind issuer implementation/cancellation chain, public time, registration entitlement, payment settlement and account tax; query full event types only within held intervals',
         'Investor total return/receivable/payment/tax and event completeness', 'YES_TARGETED_EVENT_AND_ACCOUNT_RECORDS',
         'P9-C02 frozen holdings,9 unresolved events,14 intersections;DQ03 event IDs',
         'Small event set; external settlement availability is main risk; do not stream all CNRDS history',
         'All intended postings have independent contracts, or retain EVENT_UNRESOLVED and total-return block', [ref('p9-c02', 'REPORT.md'), ref('dq-03', 'corporate-action-links.csv')])
    todo('W02', 'P0', '000540 exact abort;002185/600095/601615 bounded missing intervals',
         'Acquire versioned enum2 dictionary and exact issuer status/public time; bind prior true close and independently chosen nontrade valuation policy for new research versions',
         'Real OOS status abort and future held-position valuation gaps, without mapping all2 globally', 'YES_TARGETED_NOTICES_AND_EXISTING_CHECKPOINTS',
         'P9 preflight exact gaps;P9-D01 sidecars;P9-D03 policies', 'Few notices/intervals; no all-market scan; unknown vendor enum may remain blocking',
         'Each consumed security/date has exact status/valuation qualification or remains blocked; carry never grants trade permission', [ref('p9-d02-preflight', 'REPORT.md'), ref('p9-d03', 'REPORT.md')])
    todo('W03', 'P1', 'actual next CSI500 raw-price consumer and300114/600068 transitions',
         'Bind source/version/field units and raw/fill policy to P10 scoped entry; verify date-effective identity and nine rights/membership boundary days using existing targeted checkpoints',
         'Unqualified raw inputs, undated aliasing and unprotected legacy consumption', 'YES_TARGETED_CONTRACTS_NOT_FULL_SCAN',
         'DQ03 price joins;P10 targeted controls/source contracts;frozen consumer input identity',
         'Metadata plus limited issuer documents; no new raw row index; stop at unresolved identity',
         'Intended consumer has complete bounded contract or explicit gaps/rejection; no library-wide qualification', [ref('dq-03', 'REPORT.md'), ref('p10-g01', 'price-contract.json')])
    todo('W04', 'P1', 'intended financial factor records, starting with representative FS_BComins',
         'Acquire first-public values/time and revisions; preserve report types, units and accounting-period semantics',
         'Historical PIT and financial-value interpretation', 'YES_TARGETED_VERSIONS', 'DQ03 885 records/22 risk locators;DQ02 field/key stats',
         'Vendor historical versions may be unavailable; do not infer missing history from correction lists',
         'Every consumed value has a qualifying historical version or PIT remains blocked', [ref('dq-03', 'financial-temporal-checks.csv')])
    todo('W05', 'P1', 'actual historical membership/industry consumers',
         'Bind CSI500 separate archive identity;confirm availability, inclusion/exclusion endpoints and classification-system/version/initial state only for intended consumer',
         'Table/archive lineage, signal-time membership and system-free join expansion', 'YES_TARGETED_CONTRACTS',
         'DQ03 605500 member keys and2023-03 system-aware industry join', 'Small contract review; no DQ03 rerun',
         'Exact archive/version/system and time semantics bound or keep signal-time admission unknown', [ref('dq-03', 'verification.json'), ref('dq-03', 'industry-history-checks.csv')])
    todo('W06', 'P2', 'only consumed conflict tables or BigQuant resource-blocked candidate keys',
         'Confirm supplier business key first; if needed perform bounded exact key/nonkey comparison with explicit budget and recoverable key checkpoint design',
         '21 native candidate conflicts;16 blocked BigQuant checks;25 unknown contracts', 'YES_ONLY_AFTER_CONSUMER_AND_BUDGET_SELECTED',
         'DQ02 key statistics;DQ02B partition receipts;do not copy14.63GiB private index',
         'Potentially expensive;no default full rescan;64MiB recorded key ceiling unchanged',
         'Consumed key contract/check closes or stops at budget and remains blocked/unknown', [ref('dq-02', 'primary-key-quality.csv'), ref('dq-02b', 'key-quality.csv')])
    todo('W07', 'P3', 'DQ02B resource narrative and integration state',
         'Reconcile stale512MiB reason text with64MiB budget/implementation and pin accepted commit after PR review',
         'Resource-document inconsistency;does not invalidate completed content counts', 'NO',
         'Exact DQ02B HEAD report,budget and key checker source', 'Metadata-only;no scan or limit increase',
         'One consistent documented limit with precise revision provenance;otherwise retain discrepancy', [ref('dq-02b', 'REPORT.md'), ref('dq-02b', 'resource-budget.json'), 'tools/data/dq02b_columnar.py'])
    checks = validate(rows, tables, native, nk, bk, joins)
    assert sum(int(r['rows']) for r in bt) == bv['rows'] == sum(int(r['rows']) for r in ba)
    assert nonfinite == bv['nonfinite_field_partitions'] == 181
    assert budget['compact_key_memory_limit_bytes'] == 64 * 1024**2 and resource_reason_512
    checks.update(evidence_paths_exist_at_exact_commit=True, bigquant_integration_state_explicit=state in {'UNMERGED_EVIDENCE', 'MAIN_ACCEPTED'}, field_nonfinite_conservation=True)
    OUT.mkdir(exist_ok=True)
    write_csv('data-admission-matrix.csv', rows)
    write_csv('research-impact.csv', impacts)
    write_csv('remaining-work.csv', work)
    index = dict(task='DQ-C01', main_commit=main, bigquant_commit=bigquant, bigquant_state=state,
        evidence=list(ev.entries.values()),
        enums=dict(content=sorted(CONTENT), key=sorted(KEYS), admission=sorted(ADMISSION),
            semantic={'NOT_ASSESSED': 'No table-wide financial semantic contract', 'SCOPED_EVIDENCE_ONLY': 'Only exact evidence_scope, not table-wide approval'},
            confidence={'HIGH': 'Direct committed counts or explicit scoped restriction, not probability of economic correctness'},
            record_scope={'TABLE': 'Unique coverage denominator', 'USE_CASE': 'Reuses table evidence; never add rows or partitions'},
            pit={'PIT_UNVERIFIED': 'First-public/vendor historical availability not certified'},
            hfq={'HFQ_UNVERIFIED': 'Adjustment/event chain not certified'}),
        admission_definitions={'READY_SCOPED': 'Only specified use, object, version, fields and preconditions',
            'CONDITIONALLY_USABLE': 'Finite retrospective exploration subject to conditions; not preapproved numeric model input',
            'BLOCKED': 'Known evidence gap directly blocks stated use', 'NOT_ASSESSED': 'Insufficient evidence for stated use'},
        key_definitions={'KEY_EXACT_VERIFIED': 'Confirmed business uniqueness contract plus complete exact check',
            'CANDIDATE_KEY_EXACT': 'Exact candidate comparison only; duplicates may remain',
            'KEY_CHECK_BLOCKED_RESOURCE': 'Resource ceiling or checkpoint-only recovery prevents exact check',
            'KEY_CONTRACT_UNKNOWN': 'No explicit candidate/business key contract',
            'KEY_CONFLICT_OBSERVED': 'Candidate key maps to different full native content; not certified business error',
            'NOT_ASSESSED': 'Scope incomplete or no applicable full-scope check'},
        null_contract='Empty CSV cell / JSON null means unknown or not separately established; zero is an observed count only',
        resource_discrepancy=dict(recorded_limit_bytes=67108864, reason_text_mib=512, resolution='UNRESOLVED_DOCUMENTATION_NO_LIMIT_INCREASE'),
        gates=GATES)
    (OUT / 'evidence-index.json').write_text(json.dumps(index, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    v = dict(task='DQ-C01', status='DQC01_EVIDENCE_MATRIX_COMPLETE', gates=GATES, checks=checks,
        base_table_count=156, use_case_rows=len(rows)-156, assets=146, partitions_discovered=3550,
        partitions_actual_content_scanned=3507, partitions_limited_inherited=43,
        dq01_registration_snapshot=coverage, actual_scan_rows_by_audit={'DQ02_CSMAR':105936113,'DQ02_CNRDS':43523306,'DQ02B_BigQuant':450958087},
        row_total_policy='Never add DQ01/DQ03/P9/P10 inherited or diagnostic rows; actual audit counts are scoped reads, not certified absolute local row total',
        unscanned_historical_rows=None, declared_rows_total=None, unique_local_rows_total=None,
        price_join_counts=joins, bigquant_state=state, main_commit=main, bigquant_commit=bigquant,
        resources=dict(worker_processes=1, raw_vendor_files_opened=0, database_bytes=0, raw_row_index_bytes=0,
            integration_seconds=time.perf_counter()-start, memory_soft_limit_bytes=2*1024**3,
            temp_disk_budget_bytes=256*1024**2, temporary_materialized_evidence_bytes=0),
        tests={'status':'PENDING_TARGETED_RUN'})
    (OUT / 'verification.json').write_text(json.dumps(v, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    return v


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--main', required=True)
    parser.add_argument('--bigquant', required=True)
    parser.add_argument('--bigquant-state', choices=['UNMERGED_EVIDENCE', 'MAIN_ACCEPTED'], required=True)
    args = parser.parse_args()
    print(json.dumps(build(args.main, args.bigquant, args.bigquant_state), ensure_ascii=False, default=dict))
