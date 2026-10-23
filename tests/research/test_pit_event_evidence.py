"""Synthetic evidence attacks and offline public audit; no market certification."""

from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest

from examples.research.audit_pit_event_evidence import audit, main
from qbot.research.errors import ProviderError
from qbot.research.universe.event_evidence import check_event_evidence, verify_local_source


def fixture(tmp_path):
    def source(name, content):
        raw = content.encode("utf-8")
        (tmp_path / name).write_bytes(raw)
        return {"local_relative_path": name, "sha256": hashlib.sha256(raw).hexdigest()}
    body, attachment, receipt, actual = [source(name + ".json", name) for name in ("body", "attachment", "receipt", "actual")]
    changes = [{"action": "ADD", "symbol": "600001.SH"}, {"action": "REMOVE", "symbol": "000001.SZ"}]
    delta = hashlib.sha256(json.dumps(sorted((r["action"], r["symbol"]) for r in changes),
                                    separators=(",", ":")).encode("utf-8")).hexdigest()
    event = {"notice_id": 1, "announcement_publish_date": "2021-01-01",
             "planned_effective_after_close_date": "2021-01-04",
             "actual_effective_member_date": "2021-01-05",
             "candidate_add_count": 1, "candidate_remove_count": 1,
             "candidate_changes": changes, "unresolved_add_symbol_count": 0,
             "source_evidence": {"local_relative_path": body["local_relative_path"], "raw_content_sha256": body["sha256"]},
             "attachments": [{"local_relative_path": attachment["local_relative_path"],
                              "raw_content_sha256": attachment["sha256"],
                              "receipt_relative_path": receipt["local_relative_path"], "receipt_sha256": receipt["sha256"]}],
             "historical_identity_status": "INDEPENDENTLY_REVIEWED",
             "correction_review_status": "INDEPENDENTLY_REVIEWED",
             "independent_actual_membership_evidence": [{**actual, "kind": "ACTUAL_INDEX_MEMBERSHIP",
                 "index_code": "000905", "notice_id": 1, "effective_member_date": "2021-01-05",
                 "boundary": "FIRST_INCLUDED_TRADING_SESSION", "coverage": "FULL_EVENT_DELTA", "delta_sha256": delta}]}
    return event


def test_synthetic_complete_checklist_never_certifies_or_replays(tmp_path):
    result = audit([fixture(tmp_path)], tmp_path)
    assert result["events"][0]["event_status"] == "REVIEW_READY_NOT_CERTIFIED"
    assert result["events"][0]["certified_event"] is False
    assert result["certified_daily_pit_published"] is False
    assert result["local_replay"]["membership_rows_generated"] == 0
    assert result["local_replay"]["daily_member_counts"] is None


@pytest.mark.parametrize("kind", ["BAOSTOCK_UPDATE_DATE", "TRADING_CALENDAR", "ANNOUNCEMENT_PUBLICATION", "LISTING_CONFIRMATION"])
def test_other_dates_and_condition_triggers_never_prove_membership(tmp_path, kind):
    event = fixture(tmp_path)
    event["independent_actual_membership_evidence"][0]["kind"] = kind
    result = audit([event], tmp_path)["events"][0]
    assert result["actual_effective_member_date"] is None
    assert result["actual_date_status"] == "UNKNOWN"
    assert "UNSUPPORTED_ACTUAL_DATE_CLAIM" in result["missing_requirements"]


def test_same_sha_at_another_path_cannot_rescue_missing_actual_or_trigger(tmp_path):
    event = fixture(tmp_path)
    actual = event["independent_actual_membership_evidence"][0]
    event["case_source_files"] = [{"local_relative_path": actual["local_relative_path"], "sha256": actual["sha256"]}]
    actual["local_relative_path"] = "missing.json"
    event.update({"conditional_effective_clause": "upon listing", "condition_trigger_status": "INDEPENDENTLY_CONFIRMED",
                  "condition_trigger_date": "2021-01-05", "condition_trigger_evidence": [deepcopy(actual)]})
    result = audit([event], tmp_path)
    assert result["outcome"] == "INPUT_INTEGRITY_FAILURE"
    review = result["events"][0]
    assert review["actual_effective_member_date"] is None
    assert review["condition_trigger_status"] == "UNKNOWN"
    assert review["event_status"] == "CANDIDATE_ONLY"


def test_trigger_date_never_copied_to_actual_and_unknown_symbol_not_filled(tmp_path):
    event = fixture(tmp_path)
    trigger = event.pop("independent_actual_membership_evidence")
    event.update({"actual_effective_member_date": None, "conditional_effective_clause": "upon listing",
                  "condition_trigger_status": "INDEPENDENTLY_CONFIRMED", "condition_trigger_date": "2021-01-05",
                  "condition_trigger_evidence": trigger, "unresolved_add_symbol_count": 1})
    event["candidate_changes"][0].update({"raw_code": "-", "symbol": None})
    before = deepcopy(event)
    review = audit([event], tmp_path)["events"][0]
    assert event == before
    assert review["condition_trigger_status"] == "INDEPENDENTLY_CONFIRMED"
    assert review["actual_effective_member_date"] is None
    assert "HISTORICAL_ADD_SYMBOL_UNRESOLVED" in review["missing_requirements"]


@pytest.mark.parametrize("mutation", ["wrong_delta", "same_notice", "wrong_index", "wrong_boundary", "duplicate_change"])
def test_independent_actual_must_bind_this_specific_delta(tmp_path, mutation):
    event = fixture(tmp_path)
    actual = event["independent_actual_membership_evidence"][0]
    if mutation == "wrong_delta":
        actual["delta_sha256"] = "0" * 64
    elif mutation == "same_notice":
        actual.update({"local_relative_path": "body.json", "sha256": event["source_evidence"]["raw_content_sha256"]})
    elif mutation == "wrong_index":
        actual["index_code"] = "000300"
    elif mutation == "wrong_boundary":
        actual["boundary"] = "AFTER_CLOSE_PLAN"
    else:
        event["candidate_changes"].append(deepcopy(event["candidate_changes"][0]))
    assert audit([event], tmp_path)["events"][0]["actual_effective_member_date"] is None


@pytest.mark.parametrize("material", ["body", "attachment", "receipt"])
@pytest.mark.parametrize("reuse", ["same_path", "same_bytes_other_path", "recomputed_sha"])
@pytest.mark.parametrize("conditional", [False, True], ids=["actual", "actual_and_trigger"])
def test_notice_material_cannot_be_reused_as_independent_evidence(tmp_path, material, reuse, conditional):
    event = fixture(tmp_path)
    if material == "body":
        owner, path_key, hash_key = event["source_evidence"], "local_relative_path", "raw_content_sha256"
    elif material == "attachment":
        owner, path_key, hash_key = event["attachments"][0], "local_relative_path", "raw_content_sha256"
    else:
        owner, path_key, hash_key = event["attachments"][0], "receipt_relative_path", "receipt_sha256"
    path = owner[path_key]
    original_sha = owner[hash_key]
    if reuse == "recomputed_sha":
        # Even rebinding changed retrieval bytes cannot change their evidence role.
        (tmp_path / path).write_bytes((tmp_path / path).read_bytes() + b"\nretrieved again")
        owner[hash_key] = hashlib.sha256((tmp_path / path).read_bytes()).hexdigest()
        assert owner[hash_key] != original_sha
    elif reuse == "same_bytes_other_path":
        path = "renamed-independent.json"
        (tmp_path / path).write_bytes((tmp_path / owner[path_key]).read_bytes())
    # Recompute from disk in every case; a valid hash is not independent evidence.
    digest = hashlib.sha256((tmp_path / path).read_bytes()).hexdigest()
    assert digest == owner[hash_key]
    actual = event["independent_actual_membership_evidence"][0]
    actual.update({"local_relative_path": path, "sha256": digest})
    if conditional:
        event.update({"conditional_effective_clause": "upon listing",
                      "condition_trigger_status": "INDEPENDENTLY_CONFIRMED",
                      "condition_trigger_date": "2021-01-05",
                      "condition_trigger_evidence": [deepcopy(actual)]})
    before = deepcopy(event)
    result = audit([event], tmp_path)
    assert event == before
    assert result["outcome"] == "PARTIAL"
    assert all(item["status"] == "MATCH" for item in result["source_checks"])
    review = result["events"][0]
    assert review["claimed_actual_effective_member_date"] == "2021-01-05"
    assert review["actual_effective_member_date"] is None
    assert review["actual_date_status"] == "UNKNOWN"
    assert review["event_status"] == "CANDIDATE_ONLY"
    assert "INDEPENDENT_ACTUAL_MEMBERSHIP_EVIDENCE_MISSING" in review["missing_requirements"]
    assert "UNSUPPORTED_ACTUAL_DATE_CLAIM" in review["missing_requirements"]
    if conditional:
        assert review["condition_trigger_status"] == "UNKNOWN"
        assert "CONDITION_TRIGGER_CONFIRMATION_MISSING" in review["missing_requirements"]
    assert review["formal_admission"] == "UNCHANGED_BLOCKED"
    assert review["point_in_time_universe_verified"] is False
    assert review["certified_event"] is False
    assert review["daily_pit_publication_allowed"] is False
    assert result["certified_events"] == 0
    assert result["certified_daily_pit_published"] is False
    assert result["local_replay"]["certified_events_applied"] == 0
    assert result["local_replay"]["membership_rows_generated"] == 0
    assert result["local_replay"]["daily_member_counts"] is None


@pytest.mark.parametrize("path", ["../outside", "C:/outside", "/outside", "folder\\file"])
def test_archive_reference_cannot_escape_root(tmp_path, path):
    with pytest.raises(ProviderError):
        verify_local_source(tmp_path, {"local_relative_path": path, "sha256": "a" * 64})


def test_tampered_bytes_block_event_and_duplicate_events_rejected(tmp_path):
    event = fixture(tmp_path)
    (tmp_path / "actual.json").write_text("tampered", encoding="utf-8")
    result = audit([event], tmp_path)
    assert result["outcome"] == "INPUT_INTEGRITY_FAILURE"
    assert result["events"][0]["actual_effective_member_date"] is None
    with pytest.raises(ProviderError, match="duplicate"):
        audit([event, event], tmp_path)


def test_cli_partial_exit_and_exclusive_receipt_without_network(tmp_path):
    event = fixture(tmp_path)
    event["actual_effective_member_date"] = None
    event["independent_actual_membership_evidence"] = []
    index = tmp_path / "index.json"
    index.write_text(json.dumps({"events": [event]}), encoding="utf-8")
    output = tmp_path / "result"
    args = ["--event-index", str(index), "--notice-ids", "1", "--archive-root", str(tmp_path), "--output", str(output)]
    assert main(args) == 1
    receipt = json.loads((output / "receipt.json").read_text(encoding="utf-8"))
    assert receipt["network_requests"] == 0 and receipt["live_money"] is False
    for item in receipt["outputs"]:
        assert hashlib.sha256((output / item["name"]).read_bytes()).hexdigest() == item["sha256"]
    with pytest.raises(FileExistsError):
        main(args)


def test_existing_public_metadata_stays_unknown_without_original_bytes():
    root = Path(__file__).resolve().parents[2]
    events = json.loads((root / "docs/research/evidence/five-year-data-feasibility-v1/pit-events.json").read_text(encoding="utf-8"))["events"]
    selected = [event for event in events if event["notice_id"] in (13888, 13420)]
    assert len(selected) == 2
    for event in selected:
        review = check_event_evidence(event)
        assert review["actual_effective_member_date"] is None
        assert review["daily_pit_publication_allowed"] is False


def test_public_receipts_bind_exact_published_bytes_across_checkouts():
    root = Path(__file__).resolve().parents[2]
    evidence = root / "docs/research/evidence/pit-effective-membership-evidence-v1"
    for receipt_name, field in (("baseline-receipt.json", "public_outputs"),
                                ("regular-receipt.json", "public_files"),
                                ("conditional-receipt.json", "files")):
        receipt = json.loads((evidence / receipt_name).read_text(encoding="utf-8"))
        for item in receipt[field]:
            path = item.get("local_relative_path") or item["path"]
            raw = (root / path).read_bytes()
            assert len(raw) == item["bytes"]
            assert hashlib.sha256(raw).hexdigest() == item["sha256"]
    receipt = json.loads((evidence / "event-audit-v1/receipt.json").read_text(encoding="utf-8"))
    for item in receipt["outputs"]:
        assert hashlib.sha256((evidence / "event-audit-v1" / item["name"]).read_bytes()).hexdigest() == item["sha256"]
