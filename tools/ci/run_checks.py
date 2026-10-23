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

# Functional research domains, defined independently of per-round outcomes.
# Old published-asset assertions inside mixed modules remain selected here;
# they must be classified explicitly, never silently removed for green CI.
RUNTIME_MODULES = [
    'test_factors_phase1','test_dataset_phase1','test_labels_phase1','test_models_phase2',
    'test_tree_models_phase3','test_model_robustness','test_prediction_metrics_phase2',
    'test_factor_metrics_golden','test_factor_metrics_report_phase1','test_portfolio_backtest_phase2',
    'test_execution_backtest_phase3','test_validation_phase2','test_walk_forward_runner_phase2',
    'test_forward_contract_phase4','test_forward_policy_phase4','test_forward_model_bundle_phase4',
    'test_forward_signal_recorder_phase4','test_forward_temporal_read_validation','test_forward_evaluation',
    'test_forward_activation_phase4','test_forward_evidence','test_forward_full_predictions',
    'test_factor_discovery_engine','test_factor_discovery_generator',
    'test_factor_discovery_integration','test_exploratory_statistical_audit',
    'test_phase1_file_pipeline','test_phase2_synthetic_example','test_phase3_synthetic_example',
    'test_phase4_synthetic_example','test_forward_evaluation_synthetic_example',
    'test_qualification_admission_boundaries','test_local_admission_boundaries','test_market_data_qualification',
    'test_csmar_source_binding','test_csmar_security_fixes','test_csmar_date_precheck_traceability',
    'test_csmar_rejection_coordinates','test_config','test_config_local_provider',
    'test_cli_reproducibility','test_cli_phase4','test_cli_forward_evaluation',
    'test_cli_model_inference','test_cli_phase2','test_cli_stage_diagnostics',
    'test_cli_download_contract','test_cache_generation_publication','test_pit_generation_publication',
    'test_pit_snapshot_failure','test_download_repair_concurrency','test_run_manifest_atomic_write',
    'test_local_market_provider','test_security_status_import','test_schema_numeric_integrity',
    'test_schema_ohlc_relationship','test_schema_phase1','test_adjustment_foundation',
    'test_tdx_day','test_tdx_staging','test_tdx_reference','test_tdx_quality_audit',
    'test_bar_data_provenance','test_baostock_effective_semantics','test_baostock_status_provider',
    'test_akshare_failure_contract','test_akshare_provider',
]
EXTRA_MODULES = [
    'tests/market_structure/test_features.py','tests/market_structure/test_temporal.py',
    'tests/market_structure/test_diagnostics.py','tests/market_structure/test_review_calendar.py',
    'tests/market_structure/test_review_provenance.py','tests/market_structure/test_review_red_cases.py',
    'tests/market_structure/test_review_taxonomy.py','tests/market_structure/test_review_scope.py',
    'tests/market_structure/test_review_spearman.py','tests/market_structure/test_review_dependencies.py',
    'tests/market_structure/test_review_amount_fields.py','tests/market_structure/test_review_broadcast.py',
    'tests/market_structure/test_review_followup.py','tests/market_structure/test_review_identifiers.py',
    'tests/market_structure/test_review_source_policy.py','tests/market_structure/test_minute_gate_compatibility.py',
    'tests/market_admission/test_admission.py','tests/minute_data/test_yhh30m.py',
    'tests/minute_data/test_yhh30m_review.py','tests/migration',
    'tests/model_readiness/test_readiness.py','tests/model_readiness/test_comparison_contract.py',
    'tests/model_readiness/test_source_identity.py',
]
HISTORY_NODES = [
    'tests/research/test_factor_discovery_integration.py::test_old_factor_golden_configs_and_historical_artifacts_unchanged',
    'tests/model_readiness/test_source_identity.py::test_historical_git_blobs_and_eol_map_reconstruct_original_raw_receipt',
]


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--suite',choices=('runtime','full'),default='runtime')
    p.add_argument('--output',type=Path,required=True)
    args=p.parse_args()
    output=args.output.resolve()
    output.mkdir(parents=True,exist_ok=False)
    targets = (['tests/research/'+name+'.py' for name in RUNTIME_MODULES]+EXTRA_MODULES
               if args.suite=='runtime' else ['tests/research','tests/model_readiness','tests/market_structure',
                    'tests/market_admission','tests/minute_data','tests/evidence','tests/migration'])
    for name in targets:
        if not (ROOT/name).exists():
            raise FileNotFoundError('declared CI target missing: '+name)
    temp_parent=ROOT.parent/'FactorLab-independent-candidate'/'local-tmp' if os.name=='nt' else output/'tmp-parent'
    # Keep Windows temp on the source drive; allocate a fresh path only.
    if os.name=='nt':
        temp_parent=output/'tmp-parent'
    temp_parent.mkdir(parents=True,exist_ok=True)
    env=dict(os.environ,MPLBACKEND='Agg',PYTHONUTF8='1')
    deselections = ['--deselect='+node for node in HISTORY_NODES] if args.suite=='runtime' else []
    command=[sys.executable,'-m','pytest',*targets,*deselections,'--continue-on-collection-errors','-q',
             '--basetemp='+str(temp_parent/'pytest'), '--junitxml='+str(output/'tests.xml'),
             '--cov=factorlab','--cov-report=xml:'+str(output/'coverage.xml')]
    start=time.perf_counter()
    with (output/'tests.log').open('w',encoding='utf-8') as log:
        code=subprocess.call(command,cwd=ROOT,env=env,stdout=log,stderr=subprocess.STDOUT)
    source={p.relative_to(ROOT).as_posix():hashlib.sha256(p.read_bytes()).hexdigest()
            for p in (ROOT/'src/factorlab').rglob('*.py')}
    report={'schema':'FACTORLAB_INDEPENDENT_CI_V1','suite':args.suite,'targets':targets,
            'status':'PASS' if code==0 else 'FAIL','exit_code':code,'python':sys.version,
            'platform':platform.platform(),'elapsed_seconds':time.perf_counter()-start,
            'subject_sha':subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
            'history_nodes_deferred_to_full_job':HISTORY_NODES if args.suite=='runtime' else [],
            'source_sha256':source,'formal_admission':'FORMAL_ADMISSION_BLOCKED',
            'scope':'runtime domains only' if args.suite=='runtime' else 'all preserved upstream tests plus migration tests',
            'remote_github_actions_execution':bool(os.environ.get('GITHUB_ACTIONS'))}
    (output/'receipt.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(args.suite,report['status'],'exit',code,'evidence:',output)
    return code


if __name__=='__main__':
    raise SystemExit(main())
