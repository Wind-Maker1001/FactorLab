"""Bounded real-byte software pilot; no financial admission, no library copy.

Run with the pinned research Python from the worktree root. All raw rows and
references stay in the operator's private output directory, outside Git.
"""

import argparse
import csv
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pandas as pd

from factorlab.data.csmar.binding import parser_sha256, reference, runtime
from factorlab.data.csmar.profiles import SOURCE_ID
from factorlab.data.evidence import SourceArtifact, json_bytes, sha256, utc_now
from factorlab.data.market_qualification import QualificationInputs, publish_qualification
from factorlab.universe.identity import IdentitySnapshot


def hash_file(path):
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(4 * 1024 ** 2), b""):
            h.update(block)
    return h.hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--audit", required=True, type=Path)
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if shutil.disk_usage(args.output.parent).free < 2 * 1024 ** 3:
        raise RuntimeError("pilot free space floor")
    args.output.mkdir(exist_ok=False)
    scratch = args.output / "scratch"
    scratch.mkdir()
    audit = args.audit
    delivery = json.loads((audit / "DELIVERY_MANIFEST.json").read_bytes())
    reports = ["FULL_DATA_AUDIT_REPORT.md", "ASSET_MANIFEST.json", "TABLE_SCHEMA_INVENTORY.csv", "DATA_COVERAGE_MATRIX.csv",
               "MARKET_CHECKS.json", "ADJUSTMENT_AUDIT.md", "IDENTITY_STATUS_AUDIT.md", "CANDIDATE_RECONSTRUCTION.json",
               "CSI500_MEMBERSHIP_COVERAGE.csv", "CSI500_OFFICIAL_EVENT_COMPARISON.csv", "SOFTWARE_BOUNDARY_OBSERVATION.json",
               "FORMAL_ADMISSION_MATRIX.json", "MISSING_DATA_REQUIREMENTS.md"]
    entries = {Path(x["path"]).name: x for x in delivery["required_artifacts"]}
    checks = {}
    for name in reports:
        actual = hash_file(audit / name)
        expected = entries.get(name, {}).get("sha256")
        checks[name] = {"sha256": actual, "delivery_manifest_sha256": expected,
                        "status": "MATCH" if actual == expected else "NOT_BOUND_IN_DELIVERY" if expected is None else "MISMATCH"}
        if expected is not None and actual != expected:
            raise RuntimeError("prior report hash mismatch: " + name)
    assets = json.loads((audit / "ASSET_MANIFEST.json").read_bytes())["assets"]
    inventory = list(csv.DictReader((audit / "TABLE_SCHEMA_INVENTORY.csv").open(encoding="utf-8-sig")))
    stats = {}
    for asset in assets:
        path = args.source_root / asset["name"]
        info = path.stat()
        if info.st_size != asset["size_bytes"] or info.st_mtime_ns != asset["mtime_ns"]:
            raise RuntimeError("source inventory metadata mismatch: " + asset["name"])
        stats[asset["name"]] = [info.st_size, info.st_mtime_ns]
    (args.output / "audit-input-verification.json").write_bytes(json_bytes({"reports": checks, "source_metadata": stats,
        "scope": "47 source stat checks; selected inputs fully hashed by replay; not a new all-library audit"}))
    # Freeze selection independently of returns/current index membership.
    choices = [("TRDNEW_Dalyr", "dbbc5be8bc68ab5b", "000001", "2021-09-13", "2021-09-17"),
               ("IDX_Idxtrd", "110c38fdf32a5102", "000905", "2021-09-13", "2021-09-17"),
               ("TRD_AdjustFactor", "1943de1ef71dc8f1", "000001", "2021-01-01", "2026-10-20"),
               ("TRDNEW_Co", "92eedaafcdc28a9e", "000001", "1991-01-01", "2026-10-20"),
               ("TRDNEW_Cptl", "47a26c8d466fb182", "000001", "2021-01-01", "2026-10-20"),
               ("TRDNEW_Capchg", "8edcd667b4056c32", "000001", "2021-01-01", "2026-10-20"),
               ("IDX_Chgsmp", "4938b5313dbd8b57", "000905", "2021-01-01", "2026-10-20"),
               # Available weights contain 000906, intentionally retain original
               # code as an observation, never pass it as CSI500 membership.
               ("IDX_Smprat", "ac36e1280086b659", "000906", "2021-09-13", "2021-09-17")]
    results = []
    for family, aid, code, start, end in choices:
        asset = next(x for x in assets if x["asset_id"] == aid)
        rows = [x for x in inventory if x["asset_id"] == aid and x["family"] == family and x["member"] == family + ".xlsx"]
        ref = reference(source_name=asset["name"], source_sha256=asset["sha256"], source_bytes=asset["size_bytes"],
                        family=family, member=family + ".xlsx", member_sha256=rows[0]["member_sha256"],
                        sheet_records={x["sheet"]: int(x["rows_processed"]) for x in rows}, codes=[code], start=start, end=end)
        ref_path = args.output / (family + "-request.json")
        ref_path.write_bytes(json_bytes(ref))
        kwargs = ["--source-root", str(args.source_root), "--scratch", str(scratch)]
        candidate = args.output / family / "candidate.json"
        commands = [[sys.executable, "-B", "-m", "factorlab.data.csmar", "build", str(ref_path), *kwargs, "--output", str(candidate.parent)],
                    [sys.executable, "-B", "-m", "factorlab.data.csmar", "verify", str(candidate), *kwargs]]
        runs = []
        for n, command in enumerate(commands):
            print(json.dumps({"family": family, "step": "build" if n == 0 else "independent_verify", "status": "STARTED"}), flush=True)
            run = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf8")
            (args.output / (family + "-%d.stdout.json" % n)).write_text(run.stdout, encoding="utf8")
            (args.output / (family + "-%d.stderr.log" % n)).write_text(run.stderr, encoding="utf8")
            if run.returncode:
                raise RuntimeError("pilot child failed: " + family + "\n" + run.stderr)
            metrics = json.loads(run.stdout)
            runs.append({"command": command, "exit_code": run.returncode, "metrics": metrics})
            print(json.dumps({"family": family, "step": n, "scanned": metrics["scanned_records"], "selected": metrics["selected_records"],
                              "seconds": metrics["elapsed_seconds"], "peak_bytes": metrics["process_lifetime_peak_memory_bytes"]}), flush=True)
        for key in ("raw_sha256", "native_sha256", "lineage_sha256"):
            if runs[0]["metrics"][key] != runs[1]["metrics"][key]:
                raise RuntimeError("independent process replay mismatch")
        results.append({"family": family, "candidate": str(candidate), "request": ref, "runs": runs,
                        "exact_replay": True, "maximum_numeric_difference": 0})
        (args.output / "pilot-progress.json").write_bytes(json_bytes(results))
        if sum(p.stat().st_size for p in args.output.rglob("*") if p.is_file()) > 2 * 1024 ** 3:
            raise RuntimeError("pilot output 2 GiB cap")
    # Exercise the unchanged qualification engine using truly incomplete facts.
    def artifact(family):
        candidate = json.loads((args.output / family / "candidate.json").read_bytes())
        path = args.output / family / "references" / candidate["reference"]
        return SourceArtifact(str(path), sha256(path.read_bytes()), path.stat().st_size, SOURCE_ID,
                              utc_now(), "local-reference-receipt-not-source-acquisition")
    raw = pd.read_parquet(args.output / "TRDNEW_Dalyr/raw.parquet")
    benchmark_raw = pd.read_parquet(args.output / "IDX_Idxtrd/raw.parquet")
    dates = pd.DatetimeIndex(benchmark_raw.date)
    keys = raw[["date", "symbol"]]
    empty_status = pd.DataFrame(columns=["date", "symbol", "trade_status", "is_st"])
    empty_listings = pd.DataFrame(columns=["symbol", "listing_date"])
    empty_pit = pd.DataFrame(columns=["date", "symbol", "is_member"])
    inputs = QualificationInputs(raw, keys.assign(turnover=float("nan")), IdentitySnapshot((), ()), (), {}, {},
                                 "local-csmar-snapshot:" + results[0]["request"]["source_sha256"], empty_status, empty_listings,
                                 empty_pit, (artifact("TRDNEW_Dalyr"),), pd.DataFrame({"date": dates}), artifact("IDX_Idxtrd"),
                                 artifact("TRDNEW_Dalyr"), artifact("IDX_Chgsmp"), benchmark_source=artifact("IDX_Idxtrd"),
                                 research_start="2021-09-13", research_end="2021-09-17")
    os.environ["FACTORLAB_CSMAR_ROOT"] = str(args.source_root)
    os.environ["FACTORLAB_CSMAR_SCRATCH"] = str(scratch)
    print(json.dumps({"step": "existing_partial_qualification_publish_and_replay", "status": "STARTED"}), flush=True)
    # Existing contract can safely express incomplete stages. Publish under this
    # isolated pilot directory only; LocalParquetProvider refuses its admission.
    qualification = publish_qualification(inputs, keys, dates, stocks=raw, benchmark=benchmark_raw,
                                          output_root=args.output / "partial-qualification")
    from factorlab.data.market_qualification import QualificationReport
    report = QualificationReport(**json.loads(qualification.read_bytes())["report"])
    if report.stages["RAW"] != "PASS" or report.formal_admission or report.execution_admission:
        raise RuntimeError("unexpected qualification boundary result")
    (args.output / "qualification-observation.json").write_bytes(json_bytes(asdict(report)))
    for asset in assets:
        info = (args.source_root / asset["name"]).stat()
        if stats[asset["name"]] != [info.st_size, info.st_mtime_ns]:
            raise RuntimeError("original metadata changed during pilot")
    receipt = {"schema": "CSMAR_REAL_BYTE_SOFTWARE_PILOT_V1", "results": results, "runtime": runtime(),
               "parser_sha256": parser_sha256(), "qualification_observation": asdict(report),
               "partial_qualification_manifest": str(qualification),
               "output_bytes": sum(p.stat().st_size for p in args.output.rglob("*") if p.is_file()),
               "scratch_peak_bound_bytes": 192 * 1024 ** 2, "scratch_remaining_files": len(list(scratch.iterdir())),
               "financial_evidence": "UNKNOWN", "formal_status": "FORMAL_ADMISSION_BLOCKED", "no_live_money": True}
    (args.output / "pilot-receipt.json").write_bytes(json_bytes(receipt))
    print(json.dumps({"complete": True, "tables": len(results), "RAW": report.stages["RAW"], "FORMAL": report.stages["FORMAL"]}), flush=True)


if __name__ == "__main__":
    main()
