"""Filesystem counterexamples only; synthetic files certify no financial data."""

import json

import pytest

from examples.research import audit_tdx_quality as audit


@pytest.mark.parametrize("mutation", ["modify", "delete", "output_only"])
def test_actual_audit_snapshot_protects_existing_siblings(tmp_path, monkeypatch, mutation):
    private = tmp_path / "private"
    run = private / "results/diagnostics/tdx-data-quality-audit/new-run"
    run.mkdir(parents=True)
    sibling = run / "old-review.json"
    sibling.write_bytes(b"historical receipt")
    output = run / "asset-audit"
    manifest = {"source_root": str(private / "source"),
                "configuration": {"staging_root": str(private / "staging")}}
    monkeypatch.setattr(audit, "read_json", lambda unused: manifest)
    monkeypatch.setattr(audit.subprocess, "check_output", lambda *a, **k: b"")
    snapshot = audit.stat_inventory
    observed = {}

    def capture(roots, exclude):
        observed.update(roots=roots, exclude=exclude, before=snapshot(roots, exclude))
        return observed["before"]

    class StopAfterSnapshot(Exception):
        pass

    def stop(*args, **kwargs):
        raise StopAfterSnapshot()

    monkeypatch.setattr(audit, "stat_inventory", capture)
    monkeypatch.setattr(audit, "verify_pilot", stop)
    with pytest.raises(StopAfterSnapshot):
        audit.audit_assets(private, output, private / "pilot/manifest.json", private / "decoder.py")
    if mutation == "modify":
        sibling.write_bytes(b"modified historical receipt; a different size")
    elif mutation == "delete":
        sibling.unlink()
    (output / "new-output.json").write_text(json.dumps({"new": True}), encoding="utf-8")
    after = snapshot(observed["roots"], observed["exclude"])
    changed = sorted(k for k, value in observed["before"].items() if after.get(k) != value)
    assert str(sibling) in observed["before"], "existing sibling omitted from actual audit snapshot"
    assert observed["exclude"] == output
    assert changed == ([] if mutation == "output_only" else [str(sibling)])
    assert all(output not in audit.Path(name).parents for name in after)
    result = audit.preservation_result(observed["before"], after)
    assert result["changed"] == changed
    assert result["status"] == ("PASS" if mutation == "output_only" else "FAIL")
