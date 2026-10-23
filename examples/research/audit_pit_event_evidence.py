"""Check explicit archived event evidence offline; never reconstruct members."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import platform
import subprocess

from factorlab.errors import ProviderError
from factorlab.universe.event_evidence import (
    check_event_evidence, event_source_references, unavailable_baseline_replay_result,
    verify_local_source,
)


def _unique_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ProviderError("duplicate evidence JSON key")
        result[key] = value
    return result


def _read(path):
    raw = Path(path).read_bytes()
    if len(raw) > 4 * 1024 * 1024:
        raise ProviderError("event metadata exceeds bounded size")
    value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_keys)
    return value, {"path": str(Path(path).resolve()), "sha256": hashlib.sha256(raw).hexdigest(),
                   "bytes": len(raw)}


def audit(events, archive_root):
    """Return a reproducible checklist and file checks, including failed reads."""
    if not isinstance(events, list) or not 1 <= len(events) <= 100:
        raise ProviderError("audit requires 1 to 100 explicitly selected events")
    ids = [item.get("notice_id") for item in events]
    if len(ids) != len(set(ids)):
        raise ProviderError("duplicate event IDs require explicit version review")
    references = {}
    for event in events:
        for reference in event_source_references(event):
            path = reference["local_relative_path"]
            if path in references and references[path] != reference:
                raise ProviderError("conflicting expected source hashes")
            references[path] = reference
    checks = [verify_local_source(archive_root, item) for _, item in sorted(references.items())]
    verified = {(item["local_relative_path"], item["expected_sha256"])
                for item in checks if item["status"] == "MATCH"}
    reviews = [check_event_evidence(event, verified) for event in events]
    intact = all(item["status"] == "MATCH" for item in checks)
    return {"schema_version": 1, "scope": "EVENT_CHECKLIST_ONLY_NOT_FINANCIAL_CERTIFICATION",
            "outcome": "PARTIAL" if intact else "INPUT_INTEGRITY_FAILURE",
            "source_checks": checks, "events": reviews,
            "certified_events": 0, "certified_daily_pit_published": False,
            "local_replay": unavailable_baseline_replay_result(),
            "network_requests": 0, "software_check_completed": True}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event-index", default="docs/research/evidence/five-year-data-feasibility-v1/pit-events.json")
    parser.add_argument("--notice-ids", nargs="+", type=int, default=[13888, 13420])
    parser.add_argument("--annotations", help="Optional explicit event_annotations JSON; no inferred fields.")
    parser.add_argument("--case-reviews", nargs="*", default=[], help="Explicit case summaries containing candidate changes and source_files.")
    parser.add_argument("--archive-root", required=True, help="Original local repository root containing ignored sources.")
    parser.add_argument("--output", required=True, help="New, exclusive output directory.")
    args = parser.parse_args(argv)
    index, input_record = _read(args.event_index)
    inputs = [input_record]
    ids = args.notice_ids
    if not ids or len(set(ids)) != len(ids):
        parser.error("notice IDs must be unique")
    entries = index["events"]
    if len({item["notice_id"] for item in entries}) != len(entries):
        raise ProviderError("input event index has duplicate IDs")
    selected = [dict(item) for item in entries if item["notice_id"] in ids]
    if len(selected) != len(ids):
        parser.error("requested event is absent; no fallback is permitted")
    for review_path in args.case_reviews:
        review, record = _read(review_path)
        inputs.append(record)
        matches = [event for event in selected if event["notice_id"] == review.get("notice_id")]
        if len(matches) != 1:
            raise ProviderError("case review must match exactly one selected event")
        matches[0]["candidate_changes"] = review["candidate_changes"]
        matches[0]["case_source_files"] = review["source_files"]
    if args.annotations:
        annotations, record = _read(args.annotations)
        inputs.append(record)
        for event in selected:
            extra = annotations["event_annotations"].get(str(event["notice_id"]), {})
            allowed = {"condition_trigger_status", "condition_trigger_date", "condition_trigger_evidence",
                       "historical_identity_status", "correction_review_status", "unresolved_conflicts"}
            if extra.get("independent_actual_membership_evidence") == []:
                extra = {key: value for key, value in extra.items() if key != "independent_actual_membership_evidence"}
            if set(extra) - allowed:
                raise ProviderError("annotations cannot replace event identity, actual date, or source bytes")
            event.update(extra)
    result = audit(selected, args.archive_root)
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=False)
    artifacts = []
    for name, value in (("event-matrix.json", result), ("local-replay.json", result["local_replay"])):
        raw = (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n").encode("utf-8")
        with (output / name).open("xb") as stream:
            stream.write(raw)
        artifacts.append({"name": name, "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)})
    root = Path(__file__).resolve().parents[2]
    def git(*arguments):
        return subprocess.check_output(["git", *arguments], cwd=root, encoding="utf-8").strip()
    receipt = {"created_at_utc": datetime.now(timezone.utc).isoformat(),
               "code_head": git("rev-parse", "HEAD"), "branch": git("branch", "--show-current"),
               "working_tree_changes_present": bool(git("status", "--porcelain", "--untracked-files=normal")),
               "entrypoint_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
               "checker_sha256": hashlib.sha256((root / "qbot/research/universe/event_evidence.py").read_bytes()).hexdigest(),
               "python": platform.python_version(), "archive_root": str(Path(args.archive_root).resolve()),
               "inputs": inputs, "outputs": artifacts, "source_checks": result["source_checks"],
               "outcome": result["outcome"], "network_requests": 0,
               "software_check_completed": True, "certified_events": 0,
               "certified_daily_pit_published": False, "live_money": False}
    with (output / "receipt.json").open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(receipt, stream, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
        stream.write("\n")
    print(json.dumps({"outcome": result["outcome"], "output": str(output.resolve()),
                      "certified_events": 0, "local_replay": result["local_replay"]["status"]}))
    return 1 if result["outcome"] == "PARTIAL" else 2


if __name__ == "__main__":
    raise SystemExit(main())
