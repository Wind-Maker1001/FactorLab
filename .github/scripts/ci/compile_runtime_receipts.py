"""Compile public aggregate receipts from immutable downloaded Actions evidence."""

import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re

from benchmark import write_json
from summarize_runtime import summarize


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def seconds(start, end):
    if not start or not end:
        return "UNKNOWN"
    return (
        datetime.fromisoformat(end.replace("Z", "+00:00"))
        - datetime.fromisoformat(start.replace("Z", "+00:00"))
    ).total_seconds()


def compile_run(directory):
    run = load(directory / "run.json")
    jobs = load(directory / "jobs.json")["jobs"]
    job_log = (directory / "run.log").read_text(encoding="utf-8")
    download = load(directory / "download-receipt.json")
    for artifact in download["artifacts"]:
        for name, expected_sha256 in artifact["files_sha256"].items():
            path = directory / artifact["name"] / name
            if hashlib.sha256(path.read_bytes()).hexdigest() != expected_sha256:
                raise ValueError("Downloaded artifact changed: " + str(path))
    row = {
        "run_id": run["id"],
        "url": run["html_url"],
        "event": run["event"],
        "run_head_sha": run["head_sha"],
        "conclusion": run["conclusion"],
        "workflow_wall_seconds": seconds(run["created_at"], run["updated_at"]),
        "created_to_run_start_seconds": seconds(
            run["created_at"], run["run_started_at"]
        ),
        "runner_provisioning_separate_from_queue_seconds": "UNKNOWN",
        "restored_cache_keys_from_log": re.findall(
            r"Cache restored from key: ([^\r\n]+)", job_log
        ),
        "jobs": [
            {
                "name": job["name"],
                "conclusion": job["conclusion"],
                "seconds": seconds(job["started_at"], job["completed_at"]),
                "steps": [
                    {
                        "name": step["name"],
                        "conclusion": step["conclusion"],
                        "seconds": seconds(step["started_at"], step["completed_at"]),
                    }
                    for step in job["steps"]
                ],
            }
            for job in jobs
        ],
        "artifacts": download["artifacts"],
    }
    evidence = list(directory.glob("*/environment.json"))
    if not evidence:
        return row
    if len(evidence) != 1:
        raise ValueError("Ambiguous execution evidence directories")
    root = evidence[0].parent
    environment = load(evidence[0])
    row["environment"] = environment
    if (root / "result.json").exists():
        row["result"] = load(root / "result.json")
    if (root / "validation.json").exists():
        row["validation"] = load(root / "validation.json")
    if (root / "pytest-controller.json").exists():
        summary = summarize(root)
        row["timings"] = {
            key: value
            for key, value in summary.items()
            if key not in ("slowest_40", "profile_model_interfaces", "result")
        }
    log = root / "pytest.log"
    if log.exists():
        # Pytest's own terminal summary differs from parent-process wall time.
        matches = re.findall(
            r"(\d+) passed[^\n]*? in ([0-9.]+)s", log.read_text(encoding="utf-8")
        )
        if matches:
            row["pytest_summary_seconds"] = float(matches[-1][1])
    fixture = root / "fixture-equivalence.json"
    if fixture.exists():
        row["fixture_equivalence"] = load(fixture)
    row["execution_evidence_files_sha256"] = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.iterdir())
        if path.is_file()
        and path.suffix in (".json", ".xml", ".txt", ".log")
        and path.name != "summary.json"
    }
    return row


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("runs", nargs="+", type=int)
    parser.add_argument("--downloads", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError("Refuse to overwrite existing aggregate receipts")
    write_json(
        args.output,
        [compile_run(args.downloads / str(run_id)) for run_id in args.runs],
    )
