"""Publish scoped P9-D04 receipts without vendor rows or account ledgers."""
import argparse
import io
import json
from pathlib import Path
import subprocess
import sys
from xml.etree import ElementTree
import zipfile

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT / 'tools/data'))
from factorlab.data.evidence import sha256
from factorlab.data.market_evidence_sidecar import SAFETY
from audit_p3_d06 import normalize


def publish(args):
    out = args.output
    out.mkdir(parents=True, exist_ok=False)
    read = lambda p: json.loads(p.read_bytes())
    def dump(name, value):
        (out / name).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False, default=str) + '\n', encoding='utf-8')
    manifest = read(args.original / 'input-manifest.json')
    expected = {r['member']: r['actual_sha256'] for r in manifest['entries']}
    with zipfile.ZipFile(args.membership) as archive:
        payload = archive.read('members_daily.csv')
        meta = archive.read('manifest.json')
        if any(sha256(data) != expected[name] for name, data in [('members_daily.csv', payload), ('manifest.json', meta)]):
            raise ValueError('MEMBERSHIP_SOURCE_DRIFT')
        members = pd.read_csv(io.BytesIO(payload))
    members['symbol'] = normalize(members.stock_code)
    members['date'] = pd.to_datetime(members.date)
    policies = read(args.run / 'policies-private.json')
    notices = read(args.native / 'notices.json')
    native = read(args.native / 'native.json')
    acquisitions = read(args.acquisition / 'acquisition.json')
    dictionary = next(r for r in acquisitions if r['name'] == 'dictionary')
    states = []
    for policy in policies:
        symbol = policy['security']
        dates = pd.to_datetime(policy['dates'])
        count = int(members.loc[members.symbol.eq(symbol) & members.date.isin(dates)].date.nunique())
        if count != len(dates):
            raise ValueError('EXPECTED_MEMBER_DAY_COVERAGE_DIFFERS')
        source = native['securities'][symbol]
        rows = pd.DataFrame(source['cn_stock_status'])
        rows['date'] = pd.to_datetime(rows.date)
        rows = rows.loc[rows.date.isin(dates)]
        evidence = [r for r in notices if r['symbol'] == symbol[:6]]
        states.append(dict(security=symbol, exact_dates=policy['dates'], independent_suspension='DOCUMENT_CONFIRMED',
            resume_date=policy['resumption_evidence']['resume_date'], issuer_documents=evidence,
            vendor_raw_status_enums={column: sorted(rows[column].unique().tolist())
                                    for column in ('st_status', 'suspended', 'exdr', 'is_risk_warning')},
            member_days=count, prior_sidecar_covered_days=0,
            frozen_account_exposure='NOT_ASSESSED_FOLD_1_OR_2_NOT_REPLAYED',
            isolated_positive_holding_exposure='VALIDATED_CONTRACT_PATH_ONLY',
            identity='ISSUER_CODE_AND_NAME_MATCHED_AT_BOTH_BOUNDARIES',
            corporate_actions='NO_CONFLICT_OBSERVED_EXDR_0_UNCHANGED_FACTOR_AND_REORGANIZATION_NOT_IMPLEMENTED',
            corporate_action_completeness='UNVERIFIED', historical_supplier_delivery='UNKNOWN'))
        policy.update(status='W02_EVIDENCE_ACCEPTED', valuation_status='MODEL_VALUATION_ASSUMPTION',
                      raw_price_status='MISSING_NOT_BACKFILLED', tradability='NONTRADABLE',
                      source_version=dict(manifest_sha256=sha256((args.original / 'input-manifest.json').read_bytes()),
                          price_shard_sha256=policy['observation']['source_sha256']),
                      corporate_action_completeness='UNVERIFIED', financial_admission=False)
    with zipfile.ZipFile(args.stock) as archive:
        schema = archive.read('stock/cn_stock_status/schema.json')
        member = 'stock/cn_stock_status/2023-05-01_2023-05-31.parquet'
        raw = archive.read(member)
        if sha256(raw) != expected[member]:
            raise ValueError('BLOCKER_STATE_SOURCE_DRIFT')
        blocker = pd.read_parquet(io.BytesIO(raw), columns=['date', 'instrument', 'st_status', 'suspended', 'is_risk_warning'],
                                  filters=[('instrument', '==', '000540.SZ'), ('date', '==', pd.Timestamp('2023-05-15'))])
        state = json.loads(blocker.to_json(orient='records', date_format='iso'))
        receipt = json.loads(archive.read(member + '.receipt.json'))
    dump('state-contracts.json', dict(schema='P9_D04_STATE_CONTRACTS_V1', status='W02_PARTIAL',
        safety=list(SAFETY), current_dictionary=dict(**dictionary,
            acquired_on='2026-11-12', exact_statement='st_status: 0 为正常，1 为 ST，2 为 *ST',
            scope='CURRENT_PUBLIC_PAGE_ONLY', historical_effective_version='UNKNOWN',
            is_risk_warning='Before 2014-04-21 matches st_status > 0; thereafter not interchangeable'),
        exported_schema=dict(sha256=sha256(schema), metadata_source='zero_row_query',
                             st_status_type='int8', enum_contract_present=False),
        export_version=dict(exported_at_utc=receipt['exported_at_utc'], source_table='cn_stock_status',
                            historical_dictionary_binding='UNKNOWN'),
        st_status_2=dict(meaning_in_current_page='*ST', historical_contract='UNKNOWN',
            global_mapping_changed=False, original_enum_preserved=True, new_security_date_exceptions=[],
            inherited_preflight_counts={'000540.SZ':38, '000046.SZ':189, '600466.SH':23},
            inherited_member_days=26, inherited_count_source='docs/validation/p9-d02-preflight/REPORT.md',
            existing_000540_single_day_grants='UNCHANGED'), windows=states,
        timing=dict(issuer_notice_date='RECORDED_SEPARATELY_FROM_MIRROR_DISPLAY_AND_INGEST_TIME',
            policy_publication_bound='MIRROR_PREVIOUS_DATE_END_OF_DAY_NOT_EXCHANGE_TIMESTAMP_OR_SUPPLIER_DELIVERY',
            usage='EX_POST_RESEARCH_VALUATION_ONLY_NOT_SIGNAL_SELECTION')))
    dump('valuation-policies.json', dict(schema='P9_D04_VALUATION_POLICIES_V1',
        status='W02_EVIDENCE_ACCEPTED', policies=policies, new_policy_security_days=28, safety=list(SAFETY),
        distinction=['OBSERVED_RAW_CLOSE', 'MODEL_VALUATION_ASSUMPTION', 'UNVERIFIED_VALUATION', 'NONTRADABLE']))
    replay = read(args.run / 'replay-result.json')
    dump('bounded-execution-results.json', dict(schema='P9_D04_EXECUTION_V1', status='W02_PARTIAL',
        targeted_qualifications=read(args.run / 'qualification-results.json'), frozen_replay=replay,
        newly_completed_dates=['2023-05-10', '2023-05-11', '2023-05-12'],
        prior_prefix='IDENTITY_COMPARISON_ONLY_NOT_NEW_ACCEPTANCE', partial_failed_open_excluded=True,
        full_fold_performance_published=False, safety=list(SAFETY)))
    dump('remaining-blockers.json', dict(schema='P9_D04_BLOCKERS_V1', status='W02_PARTIAL', safety=list(SAFETY),
        first_actual_execution_blocker=dict(date='2023-05-15', security='000540.SZ',
            reason=replay['reason'], native_state=state, native_shard_sha256=sha256(raw),
            adapter_is_st=None, consequence='FAIL_CLOSED_NO_FILL_PERMISSION_NO_ENUM_INFERENCE',
            required='Historical vendor enum/version contract or independently verified exact-date issuer policy'),
        full_fold=dict(start='2023-03-13', end_exclusive='2024-03-13',
            last_qualified_session='2023-05-12', unresolved_after_first_blocker='NOT_EXECUTED_OR_CLAIMED_COMPLETE',
            later_000540_missing_price='FROM_2023-05-19_EXISTING_PREFLIGHT_NOT_NEW_ACCOUNT_EXPOSURE_PROOF'),
        three_windows='VALUATION_CONTRACTS_QUALIFIED_NOT_IN_FIRST_FOLD',
        unresolved_contracts=['HISTORICAL_ST_STATUS_2_VERSION_BINDING', 'PIT_UNVERIFIED', 'HFQ_UNVERIFIED',
                              'FORMAL_ADMISSION_BLOCKED', 'TOTAL_RETURN_UNAVAILABLE', 'ORDER_EXECUTABLE_UNVERIFIED'],
        todos=['TODO: obtain historical vendor status dictionary or exact issuer state for the first blocker.',
               'TODO: qualify any actually held later missing closes before further replay; no future-based universe filter.']))
    suites = list(ElementTree.parse(args.tests).getroot().iter('testsuite'))
    resources = read(args.run / 'resources.json')
    directories = [args.acquisition, args.native, *args.run.parent.glob(args.run.name.rsplit('-', 1)[0] + '-*')]
    resources['total_task_private_bytes'] = sum(p.stat().st_size for d in directories for p in d.rglob('*') if p.is_file()) + args.tests.stat().st_size
    resources.update(memory_soft_limit_bytes=4*1024**3, temporary_disk_limit_bytes=512*1024**2,
                     maximum_concurrent_python_processes=2, database_index_created=False, conflict_evidence_copied=False)
    paths = ['src/factorlab/data/market_evidence_sidecar.py', 'tools/research/run_p9_d04.py',
             'tools/research/inspect_p9_d04_sources.py', 'tools/research/investigate_p9_d04.py',
             'tools/research/publish_p9_d04.py', 'tests/research/test_p9_d04.py']
    dump('verification.json', dict(schema='P9_D04_VERIFICATION_V1', status='W02_PARTIAL',
        baseline='92c2516e408ddbd76cd94cdf1c3b8178d81c3d1f',
        prerequisite_prs=[88,92,94], dq_c01=dict(pr=104, state='UNMERGED_EVIDENCE',
            exact_head='bb35dfad42a5a7187f3c50780e7d7fb1649d5de5', consumed='docs/validation/dq-c01/REPORT.md'),
        tests=dict(count=sum(int(s.attrib['tests']) for s in suites),
                   failures=sum(int(s.attrib['failures']) for s in suites),
                   errors=sum(int(s.attrib['errors']) for s in suites),
                   seconds=sum(float(s.attrib['time']) for s in suites)), resources=resources,
        source_shards=native['shards'], code_sha256={p:sha256((ROOT/p).read_bytes()) for p in paths},
        private_receipts={p.name:sha256(p.read_bytes()) for p in args.run.glob('*.json')},
        attempts=[dict(attempt=1, result='FIRST_BLOCKER_MAY15_IDENTIFIED_BATCH_ENGINE_RETURNS_NO_PARTIAL_LEDGER'),
                  dict(attempt=2, result='SESSION_REPLAY_PREFIX_COMPARISON_REJECTED_EXTRA_DERIVED_NAV_COLUMN'),
                  dict(attempt=3, result='SESSION_REPLAY_PREFIX_ORIGINAL_FIELDS_MATCHED_NEW_THREE_SESSIONS_AUDITED'),
                  dict(attempt=4, result='SOURCE_ROWS_REPROJECTED_FROM_PINNED_SHARDS_AND_PRIOR_SIDECAR_PINNED_SAME_RESULT')],
        frozen_predictions='HASH_PINNED_NOT_RETRAINED', targets='ORIGINAL_FULL_PLAN_NO_REGENERATION',
        execution_and_fees='UNCHANGED_SHARED_ENGINE', formal_admission=False, live_money=False,
        safety=list(SAFETY)))
    print(json.dumps(dict(status='W02_PARTIAL', new_sessions=replay['new_accepted_execution_sessions'], resources=resources)))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('output', 'run', 'native', 'acquisition', 'original', 'membership', 'stock', 'tests'):
        parser.add_argument('--' + name, type=Path, required=True)
    publish(parser.parse_args())
