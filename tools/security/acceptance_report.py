"""Bind original failures, executed JUnit outcomes and final source identities.

Reports are observations, never signed approvals. No old report is overwritten.
"""
import argparse
from collections import Counter
import csv
import hashlib
import json
from pathlib import Path
import subprocess
import xml.etree.ElementTree as ET

from fixed_validator import inventory

ROOT = Path(__file__).resolve().parents[2]


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def cases(path):
    result = {}
    if not path.is_file():
        return result
    for node in ET.parse(path).getroot().iter("testcase"):
        status = "FAIL" if node.find("failure") is not None or node.find("error") is not None else (
            "NOT_RUN" if node.find("skipped") is not None else "PASS")
        result[(node.get("classname", ""), node.get("name"))] = status
    return result


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--archive", type=Path, required=True)
    p.add_argument("--archive-extra", type=Path, action="append", default=[])
    p.add_argument("--job", type=Path, action="append", default=[])
    p.add_argument("--output", type=Path, default=ROOT / "docs/security/evidence")
    a = p.parse_args()
    a.output.mkdir(parents=True, exist_ok=True)
    def write(name, value):
        (a.output/name).write_text(json.dumps(value, indent=2, ensure_ascii=False)+"\n", encoding="utf-8")
    original = ROOT/"docs/migration/evidence/full-failure-review.json"
    review = json.loads(original.read_bytes())
    xml = ROOT/review["junit_path"]
    assert digest(xml) == review["junit_sha256"], "original XML identity changed"
    expected = {(g["module"], c["name"]): (g, c)
                for g in review["distinct_message_groups"] for c in g["cases"]}
    archived = cases(a.archive/"tests.xml")
    archive_runs = []
    archive_observation = {key:a.archive.relative_to(ROOT).as_posix() for key in archived}
    for directory in [a.archive, *a.archive_extra]:
        found = cases(directory/"tests.xml")
        archive_runs.append({"path":directory.relative_to(ROOT).as_posix(),
            "counts":dict(Counter(found.values())), "junit_sha256":digest(directory/"tests.xml"),
            "log_sha256":digest(directory/"tests.log")})
        archived.update(found)
        archive_observation.update({key:directory.relative_to(ROOT).as_posix() for key in found})
    jobs = []
    executed = {}
    for directory in a.job:
        path = directory/"tests.xml"
        found = cases(path)
        executed.update(found)
        receipt = directory/"receipt.json"
        jobs.append({"path": directory.relative_to(ROOT).as_posix(), "counts": dict(Counter(found.values())),
                     "junit_sha256": digest(path), "log_sha256": digest(directory/"tests.log"),
                     "receipt": json.loads(receipt.read_bytes()) if receipt.is_file() else None})
    new_security = {k:v for k,v in executed.items() if k[0].startswith("tests.security.")}
    evidence_default = "tests/security/test_independent_controls.py"
    rows = []
    for node in ET.parse(xml).getroot().iter("testcase"):
        for kind in ("failure", "error"):
            bad = node.find(kind)
            if bad is None:
                continue
            key = (node.get("classname", ""), node.get("name"))
            group, case = expected[key]
            assert group["kind"] == kind
            assert hashlib.sha256((bad.text or "").encode()).hexdigest() == case["trace_sha256"]
            category = group["category"]
            if not key[0]:
                outcomes = [v for (m,n),v in archived.items() if m == key[1]]
                archive_status = "PASS" if outcomes and set(outcomes)=={"PASS"} else ("FAIL" if outcomes else "NOT_RUN")
            else:
                archive_status = archived.get(key, "NOT_RUN")
            if category == "PINNED_CONTROL_FIXTURE_REQUIRES_OLD_LAYOUT":
                classification = "MASKED_ATTACK_RESTORED_IN_GENUINE_ARCHIVE_AND_REPLACED_IN_V1"
                replacement = evidence_default + "::" + (
                    "test_changed_checker_rejected_without_candidate_execution" if "changed_checker" in key[1] else
                    "test_resealing_independent_controls_never_authorizes" if "independent_controls" in key[1] else
                    "test_checker_and_receipt_joint_self_authorization_rejected")
                reason = "Original attacks need original Qbot preimages; run unchanged original bodies against real 0ec7af5 objects. V1 checks the independent source layout with a retained verifier."
            elif category == "UPSTREAM_INDEPENDENT_CONTROL_WORKFLOW_MISSING":
                classification = "QBOT_ARCHIVE_CONTROL_WITH_MIGRATED_V1_SECURITY_OBLIGATION"
                replacement = evidence_default
                reason = "Qbot checker/workflow/registry pins cannot authorize FactorLab. Preserve their exact archive tests; V1 authenticates a closed source/test/control scope and cannot issue approval."
            elif category == "ORIGINAL_GIT_OBJECT_UNAVAILABLE_IN_INDEPENDENT_REPO":
                classification = "READONLY_HISTORICAL_GIT_EVIDENCE"
                replacement = "tests/migration/test_independent_boundary.py::test_source_identity_ignores_external_git_overrides"
                reason = "Original objects are historical inputs only. No old objects are fabricated or inserted into independent project history."
            else:
                classification = "QBOT_PUBLICATION_AND_LEGALLY_CHANGED_LAYOUT_OR_VERSION"
                replacement = "tests/migration/test_independent_boundary.py; distribution-integrity.json"
                reason = "Frozen Qbot publications and source path/version assertions remain archive obligations; independent FactorLab uses its actual namespace/version. No upstream receipt is rewritten."
            rows.append({"id": len(rows)+1, "module": key[0], "name": key[1],
                         "original_outcome": kind.upper(), "original_category": category,
                         "trace_sha256": case["trace_sha256"], "classification": classification,
                         "original_execution_phase": "COLLECTION" if not key[0] else "SETUP_ERROR" if kind=="error" else "PREIMAGE_FAILURE_BEFORE_ATTACK_ASSERTION" if category=="PINNED_CONTROL_FIXTURE_REQUIRES_OLD_LAYOUT" else "TEST_BODY_FAILURE",
                         "archival_replay": archive_status, "v1_contract": replacement,
                         "archival_observation":archive_observation.get(key, a.archive.relative_to(ROOT).as_posix()),
                         "new_security_obligation":category in ("UPSTREAM_INDEPENDENT_CONTROL_WORKFLOW_MISSING", "PINNED_CONTROL_FIXTURE_REQUIRES_OLD_LAYOUT"),
                         "mapping_relation": "VERSIONED_CONTRACT_REDESIGN_NOT_IDENTICAL_TEST",
                         "v1_suite": "PASS" if new_security and set(new_security.values())=={"PASS"} else "NOT_RUN",
                         "disposition": "PASS" if archive_status=="PASS" else "PARTIAL" if archive_status=="FAIL" else "NOT_RUN",
                         "authorization": "BLOCKED", "reason": reason})
    assert len(rows) == len(expected) == 186
    write("failure-disposition-186.json", {"original_report_sha256": digest(original),
          "original_junit_sha256": digest(xml), "count":186,
          "dispositions":dict(Counter(r["disposition"] for r in rows)), "rows":rows,
          "limits":"V1 suite success is not an assertion of one-to-one test equivalence or financial certification. See contract mapping and archive outcomes."})
    with (a.output/"failure-disposition-186.csv").open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0])); writer.writeheader(); writer.writerows(rows)
    archive_receipt = json.loads((a.archive/"receipt.json").read_bytes()) if (a.archive/"receipt.json").is_file() else {}
    seven = [r for r in rows if r["original_category"]=="PINNED_CONTROL_FIXTURE_REQUIRES_OLD_LAYOUT"]
    write("archive-acceptance.json", {"status":"PASS" if archived and set(archived.values())=={"PASS"} and archive_receipt.get("source_unchanged") and not archive_receipt.get("snapshot_changed_files") else "PARTIAL",
          "counts":dict(Counter(archived.values())), "seven_preimage_attacks":seven,
          "observed_runs":archive_runs, "aggregation":"Actual named-case observations; later explicit reruns replace only matching cases. Original failed run receipts and logs remain unchanged.",
          "original_collection_error_cases_recovered":sum(1 for m,n in archived if m=="tests.market_admission.test_revision_contract"),
          "source_commit":archive_receipt.get("source_commit"), "source_unchanged":archive_receipt.get("source_unchanged"),
          "snapshot_changed_files":archive_receipt.get("snapshot_changed_files"),
          "junit_sha256":digest(a.archive/"tests.xml") if (a.archive/"tests.xml").is_file() else None,
          "receipt_sha256":digest(a.archive/"receipt.json") if (a.archive/"receipt.json").is_file() else None,
          "financial_admission":False, "historical_authenticity_review":"BLOCKED"})
    baseline = json.loads((ROOT/"local-p0/baseline/baseline.json").read_bytes())
    old_changed = [n for n,d in baseline["preserved_evidence_sha256"].items() if not (ROOT/n).is_file() or digest(ROOT/n)!=d]
    frozen = [n for n in baseline["tracked_raw_sha256"] if n.startswith(("tests/research/fixtures/", "tests/research/reference/"))]
    frozen_changed = [n for n in frozen if digest(ROOT/n)!=baseline["tracked_raw_sha256"][n]]
    current = inventory(ROOT)
    write("final-source-test-identity.json", {"head":subprocess.check_output(["git","rev-parse","HEAD"],cwd=ROOT,text=True).strip(),
          "raw_sha256":current, "git_entries":subprocess.check_output(["git","ls-files","--stage"],cwd=ROOT,text=True),
          "baseline_sha256":digest(ROOT/"local-p0/baseline/baseline.json"),
          "preserved_old_evidence_count":len(baseline["preserved_evidence_sha256"]), "changed_old_evidence":old_changed,
          "frozen_reference_count":len(frozen), "changed_golden_reference":frozen_changed,
          "status":"PASS" if not old_changed and not frozen_changed else "FAIL"})
    write("test-results.json", {"jobs":jobs, "new_attack_counts":dict(Counter(new_security.values())),
          "authorization":"BLOCKED", "formal_admission":"FORMAL_ADMISSION_BLOCKED", "pit":"PIT_UNVERIFIED",
          "live_money":"NO LIVE MONEY", "remote_ci":"NOT_RUN", "publication":"NOT_RUN"})
    print("186 rows bound to original traces; archive:",dict(Counter(archived.values())),"jobs:",len(jobs))


if __name__ == "__main__":
    main()
