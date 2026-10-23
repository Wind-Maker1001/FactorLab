"""Portable local/GitHub CI entry point. Full scope never hides blocked tests."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]

# Historical/control-bound cases remain mandatory in the complete full job.
HISTORY_NODES = [
    'tests/research/test_factor_discovery_integration.py::test_old_factor_golden_configs_and_historical_artifacts_unchanged',
    'tests/model_readiness/test_source_identity.py::test_historical_git_blobs_and_eol_map_reconstruct_original_raw_receipt',
    'tests/research/test_pit_event_evidence.py::test_existing_public_metadata_stays_unknown_without_original_bytes',
    'tests/research/test_pit_event_evidence.py::test_public_receipts_bind_exact_published_bytes_across_checkouts',
    'tests/research/test_tdx_delivery_seal.py::test_all_published_tdx_files_in_clean_checkout',
    'tests/market_structure/test_delivery.py::test_source_cli_help_and_namespace_packaging_limit_are_explicit',
    'tests/market_structure/test_delivery.py::test_protected_tracked_content_is_unchanged',
]
UPSTREAM_BOUND = {
    'tests/research/test_c01_preservation.py':'independently pinned upstream control/preimages unavailable',
    'tests/research/test_c01_source_reachability.py':'upstream independent trust/source objects unavailable',
    'tests/evidence/test_pit_archive_verifiers.py':'upstream independently pinned archive verifier unavailable',
    'tests/market_admission/test_revision_contract.py':'module-level upstream independent checker unavailable',
    'tests/market_structure/test_c01_publication_safety.py':'upstream pinned literal inventory unavailable',
    'tests/market_structure/test_publication_scope_audit.py':'upstream frozen publication controls unavailable',
    'tests/market_admission/test_p1_review.py':'mixed: runtime financial refusals selected below; upstream checker attacks in full job',
    'tests/research/status_candidate/test_preservation.py':'mixed: runtime source identity selected below; upstream controls in full job',
}
PUBLISHED_HISTORY = {
    'tests/research/test_five_year_frozen_baseline.py':'original frozen Git tree/receipt',
    'tests/research/test_exploratory_delivery.py':'unredistributed real-derived upstream publication',
    'tests/research/test_exploratory_statistical_delivery.py':'unredistributed real-derived upstream audit',
    'tests/research/test_factor_discovery_delivery.py':'upstream historical publication including private-input lineage',
}
TEST_ROOTS = ['tests/research','tests/model_readiness','tests/market_structure',
              'tests/market_admission','tests/minute_data','tests/evidence','tests/migration']


def runtime_targets():
    files=sorted({p.relative_to(ROOT).as_posix() for folder in TEST_ROOTS
                  for p in (ROOT/folder).rglob('test_*.py')})
    result=[name for name in files if name not in UPSTREAM_BOUND and name not in PUBLISHED_HISTORY]
    result += [
        'tests/market_admission/test_p1_review.py::test_mid_calendar_listing_and_pit_change_reach_financial_gate_without_fabricated_rows',
        'tests/market_admission/test_p1_review.py::test_sparse_qualified_keys_still_reject_missing_changed_and_prelisting_rows',
        'tests/research/status_candidate/test_preservation.py::test_source_identity_includes_the_integrated_adapter',
    ]
    return result


def validation_inputs():
    files = {p for directory in ('src/factorlab', 'tests', 'configs', 'requirements', 'tools/ci')
             for p in (ROOT/directory).rglob('*')
             if p.is_file() and '__pycache__' not in p.parts and p.suffix != '.pyc'}
    files.add(ROOT/'pyproject.toml')
    return {p.relative_to(ROOT).as_posix():hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(files)}


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--suite',choices=('runtime','full'),default='runtime')
    p.add_argument('--output',type=Path,required=True)
    args=p.parse_args()
    output=args.output.resolve()
    output.mkdir(parents=True,exist_ok=False)
    targets = runtime_targets() if args.suite=='runtime' else TEST_ROOTS
    for name in targets:
        if not (ROOT/name.split('::',1)[0]).exists():
            raise FileNotFoundError('declared CI target missing: '+name)
    temp_parent=ROOT.parent/'FactorLab-independent-candidate'/'local-tmp' if os.name=='nt' else output/'tmp-parent'
    # Keep Windows temp on the source drive; allocate a fresh path only.
    if os.name=='nt':
        temp_parent=output/'tmp-parent'
    temp_parent.mkdir(parents=True,exist_ok=True)
    env=dict(os.environ,MPLBACKEND='Agg',PYTHONUTF8='1',COVERAGE_FILE=str(output/'.coverage'))
    deselections = ['--deselect='+node for node in HISTORY_NODES] if args.suite=='runtime' else []
    command=[sys.executable,'-m','pytest',*targets,*deselections,'--continue-on-collection-errors','-q',
             '--basetemp='+str(temp_parent/'pytest'), '--junitxml='+str(output/'tests.xml'),
             '--cov=factorlab','--cov-report=xml:'+str(output/'coverage.xml')]
    start_sha=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()
    before=validation_inputs()
    (output/'inputs-before.json').write_text(json.dumps(before,indent=2),encoding='utf-8')
    start=time.perf_counter()
    with (output/'tests.log').open('w',encoding='utf-8') as log:
        code=subprocess.call(command,cwd=ROOT,env=env,stdout=log,stderr=subprocess.STDOUT)
    source={p.relative_to(ROOT).as_posix():hashlib.sha256(p.read_bytes()).hexdigest()
            for p in (ROOT/'src/factorlab').rglob('*.py')}
    after=validation_inputs()
    inputs_unchanged=before==after
    report={'schema':'FACTORLAB_INDEPENDENT_CI_V1','suite':args.suite,'targets':targets,
            'status':'PASS' if code==0 and inputs_unchanged else 'FAIL','exit_code':code,'python':sys.version,
            'platform':platform.platform(),'elapsed_seconds':time.perf_counter()-start,
            'subject_sha':start_sha,
            'end_sha':subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
            'validation_inputs_unchanged':inputs_unchanged,'validation_input_sha256':after,
            'history_nodes_deferred_to_full_job':HISTORY_NODES if args.suite=='runtime' else [],
            'upstream_bound_security_deferred_to_required_full_job':UPSTREAM_BOUND if args.suite=='runtime' else {},
            'published_history_deferred_to_required_full_job':PUBLISHED_HISTORY if args.suite=='runtime' else {},
            'source_sha256':source,'formal_admission':'FORMAL_ADMISSION_BLOCKED',
            'scope':'runtime domains only' if args.suite=='runtime' else 'all preserved upstream tests plus migration tests',
            'remote_github_actions_execution':bool(os.environ.get('GITHUB_ACTIONS'))}
    (output/'receipt.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(args.suite,report['status'],'exit',code,'evidence:',output)
    return code if code else (0 if inputs_unchanged else 1)


if __name__=='__main__':
    raise SystemExit(main())
