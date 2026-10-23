"""Read exact parallel-branch contracts without importing or merging its tree."""

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import types

import pandas as pd


def check(framework_root, output, dataset):
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=framework_root, text=True).strip()
    path = "qbot/research/exploratory/market_structure/contracts.py"
    content = subprocess.check_output(["git", "show", commit + ":" + path], cwd=framework_root)
    module = types.ModuleType("yhh_independent_structure_contract_probe")
    sys.modules[module.__name__] = module
    exec(compile(content, path, "exec"), module.__dict__)
    results = {}
    def rejected(name, operation, reason):
        try:
            operation()
        except module.ContractError as exc:
            if reason not in str(exc):
                raise
            results[name] = {"status": "PASS_REJECTION", "reason": str(exc)}
        else:
            raise ValueError("UNEXPECTED_REAL_DATA_ACCEPTANCE: " + name)
    rejected("naive_vendor_timestamp", lambda: module.instant(pd.Timestamp("2026-09-30 15:30:00")), "timezone-aware")
    rejected("real_compute_request", lambda: module.ComputeRequest(
        observation_start="2026-09-30T01:30:00Z", observation_end="2026-09-30T02:00:00Z",
        as_of="2026-09-30T02:00:00Z", decision_time="2026-09-30T02:00:00Z",
        frequency=30, universe=("000001.SZ",), session_id="2026-09-30", synthetic_only=False), "REAL_RESEARCH_BLOCKED")
    rejected("unverified_real_record", lambda: module.Record(
        fact_id="YHH_REAL_DATA_REJECTION_PROBE", revision=1, kind="minute", payload={},
        valid_from="2026-09-30T01:30:00Z", published_at="2026-09-30T02:00:00Z",
        available_at="2026-09-30T02:00:00Z", captured_at="2026-10-18T12:00:00Z",
        observation_end="2026-09-30T02:00:00Z", evidence=("REJECTION_TEST_ONLY",),
        source="YHH_UNVERIFIED"), "synthetic identity")
    catalog = json.loads((dataset / "dataset.json").read_text(encoding="utf-8"))
    result = {"schema": "YHH_MARKET_STRUCTURE_COMPATIBILITY_REVIEW_V1", "framework_commit": commit,
              "contract_path": path, "contract_blob_sha256": hashlib.sha256(content).hexdigest(),
              "source_contract": catalog["schema"], "target_contract": module.SCHEMA,
              "boundary_probes": results, "automatic_real_data_connection": "BLOCKED_AS_DESIGNED",
              "missing_prerequisites": ["CERTIFIED_TIMEZONE_AND_INTERVALS", "ONE_MINUTE_SOURCE_GRANULARITY",
                                        "PUBLISHED_AVAILABLE_CAPTURED_CLOCKS", "STABLE_HISTORICAL_SECURITY_IDENTITY",
                                        "VERIFIED_PRICE_AND_VOLUME_UNITS", "FLOW_AND_LIMIT_STATE_DEFINITIONS",
                                        "SEPARATELY_REVIEWED_REAL_SOURCE_ADAPTER"],
              "branch_merged": False, "features_added_or_computed": False, "formal_admission": False,
              "verdict": "PASS_ISOLATION;DIRECT_REAL_INPUT_INCOMPATIBLE"}
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        raise ValueError("NEW_COMPATIBILITY_RECEIPT_REQUIRED")
    output.write_text(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--framework-root", required=True, type=Path)
    parser.add_argument("--dataset", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    check(args.framework_root, args.output, args.dataset)
