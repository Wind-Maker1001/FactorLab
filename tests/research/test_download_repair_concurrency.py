"""Deterministic writer interleavings; artificial bars are not market evidence.

Each race is injected after an actual forced fetch and production Parquet
publication but immediately before the production ledger's repair operation.
No timing assumptions, sleeps, thread scheduling or network access are needed.
"""

import json

import pytest

from factorlab import cli
from factorlab.data.download_status import (
    assert_no_unresolved,
    list_unresolved,
)
from factorlab.errors import DataQualityError
from factorlab.reproducibility import RunManifest
from tests.research.test_cli_download_contract import (
    SYMBOLS,
    _seed_complete_cache,
    _seed_fatal,
    download_context,
)


def _repair(context, run):
    dates = context["benchmark"]["date"]
    return cli._update_guarded_bars(
        context["config"], run, context["provider"], context["cache"],
        SYMBOLS[0], dates.min(), dates.max(),
    )


def _events(context):
    return [
        json.loads(path.read_text(encoding="utf-8"))
        for path in context["receipt_root"].glob("receipt.*.json")
    ]


def test_new_same_symbol_fatal_after_refetch_cannot_be_swept_into_old_repair(
    download_context, monkeypatch,
):
    context = download_context
    _seed_complete_cache(context)
    before = context["cache"].load_snapshot(SYMBOLS[0])
    old = _seed_fatal(context)
    run = RunManifest(context["config"], "synthetic-repair-race")
    real_resolve = cli.resolve_fatal
    inserted = []

    def record_new_fatal_before_resolve(root, **kwargs):
        # Same dates, already inside the refetched window: interval coverage
        # alone cannot prove this independently observed failure was repaired.
        assert kwargs["expected_fatal_receipt_ids"] == [old["receipt_id"]]
        assert context["provider"].calls
        snapshot = context["cache"].load_snapshot(SYMBOLS[0])
        assert snapshot.metadata_sha256 == kwargs["cache_metadata_sha256"]
        inserted.append(_seed_fatal(context))
        return real_resolve(root, **kwargs)

    monkeypatch.setattr(cli, "resolve_fatal", record_new_fatal_before_resolve)

    with pytest.raises(DataQualityError, match="UNRESOLVED DOWNLOAD CONTRACT"):
        _repair(context, run)

    pending_ids = {item["receipt_id"] for item in list_unresolved(context["receipt_root"])}
    assert {old["receipt_id"], inserted[0]["receipt_id"]}.issubset(pending_ids)
    assert not any(event["kind"] == "resolution" for event in _events(context))
    assert not run.payload.get("resolved_download_receipts")
    assert context["cache"].load_snapshot(SYMBOLS[0]).metadata_sha256 != before.metadata_sha256
    with pytest.raises(DataQualityError, match="UNRESOLVED DOWNLOAD CONTRACT"):
        assert_no_unresolved(context["receipt_root"])
    with pytest.raises(DataQualityError, match="UNRESOLVED DOWNLOAD CONTRACT"):
        cli._load_bar_inputs(context["config"], context["membership"])


def test_new_other_symbol_fatal_does_not_invalidate_current_symbol_repair(
    download_context, monkeypatch,
):
    context = download_context
    _seed_complete_cache(context)
    old = _seed_fatal(context)
    run = RunManifest(context["config"], "synthetic-independent-repair")
    real_resolve = cli.resolve_fatal
    inserted = []

    def record_other_symbol_before_resolve(root, **kwargs):
        assert kwargs["expected_fatal_receipt_ids"] == [old["receipt_id"]]
        inserted.append(_seed_fatal(context, symbol=SYMBOLS[1]))
        return real_resolve(root, **kwargs)

    monkeypatch.setattr(cli, "resolve_fatal", record_other_symbol_before_resolve)
    repaired = _repair(context, run)

    assert not repaired.empty
    assert list_unresolved(context["receipt_root"]) == inserted
    assert run.payload["resolved_download_receipts"] == [old["receipt_id"]]
    resolutions = [event for event in _events(context) if event["kind"] == "resolution"]
    assert len(resolutions) == 1
    assert resolutions[0]["resolution"]["fatal_receipt_ids"] == [old["receipt_id"]]
    with pytest.raises(DataQualityError, match=SYMBOLS[1].replace(".", r"\.")):
        cli._load_bar_inputs(context["config"], context["membership"])


def test_later_repair_does_not_publish_fake_resolution_after_competitor_cleared_old(
    download_context, monkeypatch,
):
    context = download_context
    _seed_complete_cache(context)
    old = _seed_fatal(context)
    run = RunManifest(context["config"], "synthetic-later-repair")
    real_resolve = cli.resolve_fatal

    def competing_repair_before_resolve(root, **kwargs):
        # Both callers have actually fetched and verified the same clean
        # response.  A later observer must not invent another resolution.
        dates = context["benchmark"]["date"]
        competitor = context["provider"].get_daily_bars(
            SYMBOLS[0], dates.min(), dates.max(),
        )
        context["cache"].update(
            SYMBOLS[0], competitor, dates.min(), dates.max(),
            source=type(context["provider"]).__name__,
            source_version=context["provider"].cache_source_version(SYMBOLS[0]),
        )
        snapshot = context["cache"].load_snapshot(SYMBOLS[0])
        competitor_args = dict(
            kwargs, run_id="synthetic-competitor-repair",
            cache_metadata_sha256=snapshot.metadata_sha256,
        )
        assert real_resolve(root, **competitor_args) == [old["receipt_id"]]
        paths_before = set(context["receipt_root"].glob("receipt.*.json"))
        resolved = real_resolve(root, **kwargs)
        assert set(context["receipt_root"].glob("receipt.*.json")) == paths_before
        return resolved

    monkeypatch.setattr(cli, "resolve_fatal", competing_repair_before_resolve)
    repaired = _repair(context, run)

    assert not repaired.empty
    assert list_unresolved(context["receipt_root"]) == []
    assert run.payload["resolved_download_receipts"] == []
    assert len(context["provider"].calls) == 2
    resolutions = [event for event in _events(context) if event["kind"] == "resolution"]
    assert len(resolutions) == 1
    assert resolutions[0]["context"]["run_id"] == "synthetic-competitor-repair"
    assert resolutions[0]["resolution"]["fatal_receipt_ids"] == [old["receipt_id"]]

