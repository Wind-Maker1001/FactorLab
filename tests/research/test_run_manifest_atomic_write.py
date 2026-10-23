"""Windows sharing failures must retry the same fully flushed manifest."""

from datetime import date
import errno
import json
import logging
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from qbot.research.config import (
    DataConfig, PathsConfig, ProjectConfig, ResearchConfig, UniverseConfig,
)
import qbot.research.reproducibility as reproducibility


def _windows_error(code):
    error = PermissionError(errno.EACCES, "injected Windows sharing failure")
    error.winerror = code
    return error


def _existing_manifest(tmp_path):
    destination = tmp_path / "manifest.json"
    destination.write_text('{"status": "previous"}\n', encoding="utf-8")
    previous = destination.read_bytes()
    old_temporary = tmp_path / ".manifest.json.tmp-unrelated-old-call"
    old_temporary.write_bytes(b"retain old failure evidence")
    return destination, previous, old_temporary


def _assert_current_temporary_cleaned(destination, old_temporary):
    assert set(destination.parent.glob(".manifest.json.tmp-*")) == {old_temporary}
    assert old_temporary.read_bytes() == b"retain old failure evidence"


def test_atomic_write_preserves_serializer_format_and_unrelated_temporary(tmp_path):
    destination, _, old_temporary = _existing_manifest(tmp_path)
    payload = {"z": [2, 1], "研究": "中文", "a": None}

    reproducibility._atomic_json(payload, destination)

    assert destination.read_text(encoding="utf-8") == (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )
    assert b"\\u" not in destination.read_bytes()
    _assert_current_temporary_cleaned(destination, old_temporary)


@pytest.mark.parametrize("code", [5, 32, 33])
@pytest.mark.parametrize("failure_count", [1, 2])
def test_transient_windows_failure_retries_same_closed_flushed_complete_bytes(
    tmp_path, monkeypatch, caplog, code, failure_count
):
    destination, previous, old_temporary = _existing_manifest(tmp_path)
    payload = {"status": "success", "values": list(range(30)), "说明": "完整清单"}
    replace = reproducibility.os.replace
    fsync = reproducibility.os.fsync
    dumps = reproducibility.json.dumps
    attempts, pauses, descriptors, serializations = [], [], [], []

    def count_serialization(value, *args, **kwargs):
        serializations.append(value)
        return dumps(value, *args, **kwargs)

    def record_fsync(descriptor):
        descriptors.append(descriptor)
        return fsync(descriptor)

    def replace_with_transient_failure(source, target):
        assert len(descriptors) == 1
        with pytest.raises(OSError):
            os.fstat(descriptors[0])
        assert Path(target) == destination
        assert Path(source).is_file()
        assert json.loads(Path(source).read_bytes()) == payload
        assert destination.read_bytes() == previous
        attempts.append((source, target, Path(source).read_bytes()))
        if len(attempts) <= failure_count:
            raise _windows_error(code)
        return replace(source, target)

    monkeypatch.setattr(reproducibility.json, "dumps", count_serialization)
    monkeypatch.setattr(reproducibility.os, "fsync", record_fsync)
    monkeypatch.setattr(reproducibility.os, "replace", replace_with_transient_failure)
    monkeypatch.setattr(reproducibility.time, "sleep", pauses.append)
    caplog.set_level(logging.WARNING, logger=reproducibility.__name__)

    reproducibility._atomic_json(payload, destination)

    assert len(attempts) == failure_count + 1
    assert len(set(attempts)) == 1, "retry must not generate a new path or rewrite bytes"
    assert len(serializations) == len(descriptors) == 1
    assert pauses == list(reproducibility._MANIFEST_REPLACE_RETRY_DELAYS[:failure_count])
    assert len(caplog.records) == failure_count
    assert all("Transient Windows manifest replace failure" in record.message for record in caplog.records)
    assert json.loads(destination.read_bytes()) == payload
    _assert_current_temporary_cleaned(destination, old_temporary)


def test_durable_windows_failure_exhausts_bounded_retry_without_replacing_previous(
    tmp_path, monkeypatch, caplog
):
    destination, previous, old_temporary = _existing_manifest(tmp_path)
    attempts, pauses = [], []
    error = _windows_error(5)

    def always_fail(source, target):
        attempts.append((source, target, Path(source).read_bytes()))
        assert destination.read_bytes() == previous
        raise error

    monkeypatch.setattr(reproducibility.os, "replace", always_fail)
    monkeypatch.setattr(reproducibility.time, "sleep", pauses.append)
    caplog.set_level(logging.WARNING, logger=reproducibility.__name__)

    with pytest.raises(PermissionError) as failure:
        reproducibility._atomic_json({"status": "new"}, destination)

    assert failure.value is error
    assert len(attempts) == len(reproducibility._MANIFEST_REPLACE_RETRY_DELAYS) + 1
    assert len(set(attempts)) == 1
    assert pauses == list(reproducibility._MANIFEST_REPLACE_RETRY_DELAYS)
    assert len(caplog.records) == len(pauses)
    assert destination.read_bytes() == previous
    _assert_current_temporary_cleaned(destination, old_temporary)


@pytest.mark.parametrize(
    "error",
    [
        PermissionError(errno.EACCES, "ordinary permission denied"),
        _windows_error(1314),
        OSError(errno.EIO, "disk I/O error"),
        RuntimeError("unexpected replacement failure"),
    ],
    ids=["no-winerror", "ineligible-winerror", "disk-io", "non-oserror"],
)
def test_non_transient_replacement_errors_are_never_retried(
    tmp_path, monkeypatch, error
):
    destination, previous, old_temporary = _existing_manifest(tmp_path)
    attempts, pauses = [], []

    def fail(source, target):
        attempts.append((source, target))
        raise error

    monkeypatch.setattr(reproducibility.os, "replace", fail)
    monkeypatch.setattr(reproducibility.time, "sleep", pauses.append)

    with pytest.raises(type(error)) as failure:
        reproducibility._atomic_json({"status": "new"}, destination)

    assert failure.value is error
    assert len(attempts) == 1
    assert not pauses
    assert destination.read_bytes() == previous
    _assert_current_temporary_cleaned(destination, old_temporary)


@pytest.mark.parametrize("stage", ["json", "write", "flush", "fsync"])
def test_serialization_and_write_failures_clean_current_temporary_without_commit(
    tmp_path, monkeypatch, stage
):
    destination, previous, old_temporary = _existing_manifest(tmp_path)
    attempts, pauses = [], []
    error = OSError("injected %s failure" % stage)
    original_open = Path.open

    class BrokenWrite:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return self.stream.__exit__(*args)

        def write(self, content):
            if stage == "write":
                self.stream.write(content[:3])
                raise error
            return self.stream.write(content)

        def flush(self):
            if stage == "flush":
                raise error
            return self.stream.flush()

        def fileno(self):
            return self.stream.fileno()

    def open_with_fault(path, *args, **kwargs):
        stream = original_open(path, *args, **kwargs)
        return BrokenWrite(stream) if args and args[0] == "x" else stream

    def fail(*args, **kwargs):
        raise error

    if stage == "json":
        monkeypatch.setattr(reproducibility.json, "dumps", fail)
    elif stage == "fsync":
        monkeypatch.setattr(reproducibility.os, "fsync", fail)
    else:
        monkeypatch.setattr(Path, "open", open_with_fault)
    monkeypatch.setattr(reproducibility.os, "replace", lambda *args: attempts.append(args))
    monkeypatch.setattr(reproducibility.time, "sleep", pauses.append)

    with pytest.raises(OSError) as failure:
        reproducibility._atomic_json({"status": "new"}, destination)

    assert failure.value is error
    assert not attempts and not pauses
    assert destination.read_bytes() == previous
    _assert_current_temporary_cleaned(destination, old_temporary)


def test_nonserializable_payload_is_rejected_without_touching_existing_manifest(tmp_path):
    destination, previous, old_temporary = _existing_manifest(tmp_path)
    with pytest.raises(TypeError, match="not JSON serializable"):
        reproducibility._atomic_json({"invalid": object()}, destination)
    assert destination.read_bytes() == previous
    _assert_current_temporary_cleaned(destination, old_temporary)


def test_uuid_temporary_collision_does_not_overwrite_or_remove_existing_evidence(
    tmp_path, monkeypatch
):
    destination, previous, old_temporary = _existing_manifest(tmp_path)
    monkeypatch.setattr(
        reproducibility.uuid, "uuid4", lambda: SimpleNamespace(hex="unrelated-old-call")
    )
    with pytest.raises(FileExistsError):
        reproducibility._atomic_json({"status": "new"}, destination)
    assert destination.read_bytes() == previous
    _assert_current_temporary_cleaned(destination, old_temporary)


def test_failed_first_publication_leaves_no_partial_manifest(tmp_path, monkeypatch):
    destination = tmp_path / "new-run" / "manifest.json"
    monkeypatch.setattr(reproducibility.time, "sleep", lambda delay: None)

    def fail(source, target):
        raise _windows_error(32)

    monkeypatch.setattr(reproducibility.os, "replace", fail)
    with pytest.raises(PermissionError):
        reproducibility._atomic_json({"status": "running"}, destination)
    assert not destination.exists()
    assert not list(destination.parent.glob(".manifest.json.tmp-*"))


def test_cleanup_failure_does_not_mask_original_commit_error(tmp_path, monkeypatch, caplog):
    destination, previous, _ = _existing_manifest(tmp_path)
    error = OSError(errno.ENOSPC, "no space left for commit")
    original_unlink = Path.unlink

    def fail_commit(source, target):
        raise error

    def fail_cleanup(path, *args, **kwargs):
        if path.name.startswith(".manifest.json.tmp-"):
            raise PermissionError("cleanup blocked")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(reproducibility.os, "replace", fail_commit)
    monkeypatch.setattr(Path, "unlink", fail_cleanup)
    caplog.set_level(logging.WARNING, logger=reproducibility.__name__)
    with pytest.raises(OSError) as failure:
        reproducibility._atomic_json({"status": "new"}, destination)
    assert failure.value is error
    assert destination.read_bytes() == previous
    assert "Cannot remove current manifest temporary" in caplog.text


@pytest.fixture
def config(tmp_path, monkeypatch):
    result = ResearchConfig(
        project=ProjectConfig(name="atomic-manifest-test"),
        data=DataConfig(start=date(2024, 1, 2), end=date(2024, 1, 3)),
        universe=UniverseConfig(),
        factors=("REV1",),
        paths=PathsConfig(data_root=tmp_path / "data", results_root=tmp_path / "results"),
    )
    object.__setattr__(result, "repo_root", tmp_path)
    result.validate()
    monkeypatch.setattr(reproducibility, "git_commit", lambda root: "a" * 40)
    monkeypatch.setattr(
        reproducibility, "git_worktree_state",
        lambda root: {"git_dirty": False, "git_diff_sha256": None},
    )
    monkeypatch.setattr(reproducibility, "package_versions", lambda: {"numpy": "fixture"})
    monkeypatch.setattr(reproducibility.time, "sleep", lambda delay: None)
    return result


def test_run_manifest_initial_add_output_failures_and_finish_retry_durably(config, monkeypatch):
    replace = reproducibility.os.replace
    attempts_by_source = {}

    def retry_every_commit_once(source, target):
        attempts_by_source[source] = attempts_by_source.get(source, 0) + 1
        if attempts_by_source[source] == 1:
            raise _windows_error(5)
        return replace(source, target)

    monkeypatch.setattr(reproducibility.os, "replace", retry_every_commit_once)
    run = reproducibility.RunManifest(config, "atomic-test")
    assert json.loads(run.path.read_bytes())["status"] == "running"
    run.add_output("artifact", config.paths.results_root / "artifact.csv")
    assert "artifact" in json.loads(run.path.read_bytes())["outputs"]
    run.add_failures("symbols", ["600000.SH"])
    assert json.loads(run.path.read_bytes())["failures"]["symbols"] == ["600000.SH"]
    run.finish("success")
    stored = json.loads(run.path.read_bytes())
    assert stored == run.payload
    assert stored["status"] == "success"
    assert stored["completed_at"] is not None
    assert list(attempts_by_source.values()) == [2, 2, 2, 2]
    assert not list(run.path.parent.glob(".manifest.json.tmp-*"))


@pytest.mark.parametrize("operation", ["add_output", "add_failures", "finish"])
def test_run_manifest_failed_lifecycle_commit_preserves_disk_then_can_retry(
    config, monkeypatch, operation
):
    run = reproducibility.RunManifest(config, "atomic-test")
    previous = run.path.read_bytes()
    error = _windows_error(33)

    def fail(source, target):
        raise error

    def update():
        if operation == "add_output":
            run.add_output("artifact", "artifact.parquet")
        elif operation == "add_failures":
            run.add_failures("symbols", ["600000.SH"])
        else:
            run.finish("failed", "research failure")

    with monkeypatch.context() as patch:
        patch.setattr(reproducibility.os, "replace", fail)
        with pytest.raises(PermissionError) as failure:
            update()
        assert failure.value is error
    assert run.path.read_bytes() == previous
    assert not list(run.path.parent.glob(".manifest.json.tmp-*"))
    update()
    assert json.loads(run.path.read_bytes()) == run.payload
