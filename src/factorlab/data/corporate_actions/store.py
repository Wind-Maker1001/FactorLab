"""Immutable self-contained event generations; reuse never rewrites evidence."""

from dataclasses import asdict, replace
import json
import os
from pathlib import Path
import uuid
from typing import Any, Dict

from factorlab.data.evidence import SourceArtifact, fail, json_bytes, sha256
from factorlab.data.tdx import read_snapshot, safe_path
from .contracts import EventCoverage, SourceSnapshot
from .validation import event_from_dict, validate_snapshot


def snapshot_semantics(snapshot: SourceSnapshot) -> Dict[str, Any]:
    payload = asdict(snapshot)
    payload.pop("acquired_at")
    payload["artifacts"] = sorted(
        [{k: v for k, v in asdict(a).items() if k not in {"path", "locator", "acquired_at"}}
         for a in snapshot.artifacts], key=lambda a: (a["source_id"], a["sha256"]))
    payload["events"] = sorted(payload["events"], key=lambda e: (e["security_id"], e["event_id"], e["revision"]))
    payload["coverage"] = sorted(payload["coverage"], key=lambda c: (c["security_id"], c["start"], c["end"]))
    return payload


def _write(path: Path, content: bytes) -> None:
    with path.open("xb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def publish_snapshot(snapshot: SourceSnapshot, root: Path) -> Path:
    validate_snapshot(snapshot)
    identity = sha256(json_bytes(snapshot_semantics(snapshot)))
    root = safe_path(root)
    root.mkdir(parents=True, exist_ok=True)
    destination = safe_path(root / identity, root)
    if destination.exists():
        previous = load_snapshot(destination)
        if snapshot_semantics(previous) != snapshot_semantics(snapshot):
            fail("event generation identity collision")
        return destination
    temporary = safe_path(root / (".tmp-" + uuid.uuid4().hex), root)
    temporary.mkdir()
    payload = asdict(snapshot)
    artifacts = []
    for artifact in snapshot.artifacts:
        filename = artifact.sha256 + ".bin"
        target = temporary / filename
        content = artifact.read()
        if not target.exists():
            _write(target, content)
        artifacts.append(dict(asdict(artifact), path=filename))
    payload["artifacts"] = artifacts
    content = json_bytes(payload)
    _write(temporary / "snapshot.json", content)
    _write(temporary / "manifest.json", json_bytes({
        "schema": "FACTORLAB_CORPORATE_ACTION_GENERATION_V1", "generation": identity,
        "snapshot_file": "snapshot.json", "snapshot_sha256": sha256(content),
        "synthetic_only": snapshot.synthetic_only,
    }))
    restored = load_snapshot(temporary, expected_generation=identity)
    if snapshot_semantics(restored) != snapshot_semantics(snapshot):
        fail("event snapshot round-trip mismatch")
    try:
        os.rename(str(temporary), str(destination))
    except FileExistsError:
        if snapshot_semantics(load_snapshot(destination)) != snapshot_semantics(snapshot):
            fail("concurrent event publication conflict")
    return destination


def load_snapshot(folder: Path, expected_generation: str = "") -> SourceSnapshot:
    folder = safe_path(folder)
    manifest_bytes, _ = read_snapshot(folder / "manifest.json", folder)
    try:
        manifest = json.loads(manifest_bytes)
        if set(manifest) != {"schema", "generation", "snapshot_file", "snapshot_sha256", "synthetic_only"}:
            fail("event manifest fields mismatch")
        if manifest["schema"] != "FACTORLAB_CORPORATE_ACTION_GENERATION_V1" or manifest["snapshot_file"] != "snapshot.json":
            fail("unsupported event generation")
        if manifest["generation"] != (expected_generation or folder.name):
            fail("event generation locator mismatch")
        content, digest = read_snapshot(folder / "snapshot.json", folder)
        if digest != manifest["snapshot_sha256"]:
            fail("event snapshot hash mismatch")
        value = json.loads(content)
        if set(value) != set(SourceSnapshot.__dataclass_fields__):
            fail("event snapshot fields mismatch")
        artifacts = []
        for a in value["artifacts"]:
            if not isinstance(a, dict) or set(a) != set(SourceArtifact.__dataclass_fields__):
                fail("source artifact fields mismatch")
            if a["path"] != a["sha256"] + ".bin":
                fail("unsafe event artifact name")
            artifacts.append(SourceArtifact(**dict(a, path=str(safe_path(folder / a["path"], folder)))))
        events = tuple(event_from_dict(e) for e in value["events"])
        coverage = tuple(EventCoverage(**c) for c in value["coverage"])
        snapshot = SourceSnapshot(**dict(value, events=events, coverage=coverage,
                                         artifacts=tuple(artifacts), issues=tuple(value["issues"])))
        validate_snapshot(snapshot)
        if snapshot.synthetic_only != manifest["synthetic_only"]:
            fail("event synthetic boundary mismatch")
        if sha256(json_bytes(snapshot_semantics(snapshot))) != manifest["generation"]:
            fail("event semantic generation mismatch")
        return snapshot
    except (ValueError, TypeError, KeyError) as exc:
        fail("malformed event generation: %s" % type(exc).__name__)
