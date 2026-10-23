"""Fatal download evidence must survive retries, cache hits and corruption."""

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import subprocess
import sys
from threading import Event
from types import SimpleNamespace

import pandas as pd
import pytest

from qbot.research.data import download_status as status
from qbot.research.errors import DataQualityError


@pytest.fixture
def tmp_path(tmp_path_factory):
    # A real ledger is a dedicated, initially nonexistent directory.  Existing
    # empty directories cannot prove no first-publication failure occurred.
    return tmp_path_factory.mktemp("download-status") / "ledger"


def _context(**overrides):
    values = dict(symbol="600000.SH", provider_class="example.AKShareProvider",
                  source_version="raw-hfq-v1", start="2024-01-02", end="2024-01-12",
                  run_id="run-a", git_commit="a" * 40)
    values.update(overrides)
    return values


def _fatal(root, **overrides):
    return status.record_fatal(root, **_context(**overrides),
                               error_type="SchemaError", error_message="invalid OHLC")


def _resolve(root, **overrides):
    values = _context(run_id="run-repair")
    values.update(cache_metadata_sha256="b" * 64, force_refetched=True,
                  schema_verified=True, refetched_rows=9)
    values.update(overrides)
    if "expected_fatal_receipt_ids" not in values:
        values["expected_fatal_receipt_ids"] = [
            item["receipt_id"] for item in status.list_unresolved(root)
            if item["context"]["symbol"] == values["symbol"]
        ] or ["c" * 32]  # No-op cases still supply the required valid claim.
    return status.resolve_fatal(root, **values)


def _rewrite(path, change):
    value = json.loads(path.read_text(encoding="utf-8"))
    change(value)
    path.write_text(json.dumps(value), encoding="utf-8")


def test_absent_legacy_receipts_are_clean_and_not_created(tmp_path):
    root = tmp_path / "nonexistent"
    assert status.list_unresolved(root) == []
    status.assert_no_unresolved(root)
    assert not root.exists()
    assert not root.parent.exists()


def test_existing_empty_dedicated_ledger_cannot_masquerade_as_clean(tmp_path):
    tmp_path.mkdir()
    with pytest.raises(DataQualityError, match="incomplete initialization"):
        status.list_unresolved(tmp_path)
    with pytest.raises(DataQualityError, match="incomplete initialization"):
        status.assert_no_unresolved(tmp_path)
    with pytest.raises(DataQualityError, match="incomplete initialization"):
        _fatal(tmp_path)


def test_fatal_persists_across_attempts_and_records_contract(tmp_path):
    first = _fatal(tmp_path)
    second = _fatal(tmp_path, run_id="another-attempt")
    pending = status.list_unresolved(tmp_path)
    assert [item["receipt_id"] for item in pending] == [first["receipt_id"], second["receipt_id"]]
    assert pending[0]["context"] == _context()
    assert pending[0]["fatal"] == {"error_type": "SchemaError", "error_message": "invalid OHLC"}
    assert pending[1]["previous_receipt_id"] == first["receipt_id"]
    with pytest.raises(DataQualityError, match="verified refetch required"):
        status.assert_no_unresolved(tmp_path)


def test_unverified_source_is_explicit_null_not_unknown(tmp_path):
    event = _fatal(tmp_path, source_version=None)
    assert event["context"]["source_version"] is None
    assert status.list_unresolved(tmp_path)[0]["context"]["source_version"] is None
    with pytest.raises(DataQualityError, match="not null"):
        _resolve(tmp_path, source_version=None)


def test_complete_forced_success_preserves_history_and_resolves(tmp_path):
    first = _fatal(tmp_path)
    path = tmp_path / ("receipt.%s.json" % first["receipt_id"])
    content = path.read_bytes()
    assert _resolve(tmp_path) == [first["receipt_id"]]
    assert status.list_unresolved(tmp_path) == []
    status.assert_no_unresolved(tmp_path)
    assert path.read_bytes() == content
    assert len(list(tmp_path.glob("receipt.*.json"))) == 2


def test_new_provider_must_really_repair_not_just_hide_old_provider(tmp_path):
    first = _fatal(tmp_path)
    assert status.list_unresolved(tmp_path)[0]["context"]["provider_class"] == "example.AKShareProvider"
    assert _resolve(tmp_path, provider_class="example.LocalParquetProvider",
                    source_version="local-bundle-v2") == [first["receipt_id"]]
    assert status.list_unresolved(tmp_path) == []


def test_refetch_must_cover_all_same_symbol_unresolved_windows(tmp_path):
    first = _fatal(tmp_path)
    second = _fatal(tmp_path, start="2023-12-01", end="2023-12-20")
    with pytest.raises(DataQualityError, match="every unresolved window"):
        _resolve(tmp_path)
    assert len(status.list_unresolved(tmp_path)) == 2
    assert _resolve(tmp_path, start="2023-11-30") == [first["receipt_id"], second["receipt_id"]]


def test_other_symbols_remain_blocking_after_repair(tmp_path):
    _fatal(tmp_path)
    other = _fatal(tmp_path, symbol="000001.SZ")
    _resolve(tmp_path)
    assert [item["receipt_id"] for item in status.list_unresolved(tmp_path)] == [other["receipt_id"]]
    with pytest.raises(DataQualityError, match="000001.SZ"):
        status.assert_no_unresolved(tmp_path)


def test_new_same_symbol_fatal_cannot_be_cleared_by_an_earlier_refetch(tmp_path):
    first = _fatal(tmp_path)
    observed = [first["receipt_id"]]
    # This event is published after the repair observed the first receipt.
    # Its narrower dates are already inside that repair's fetched interval;
    # range coverage alone must not clear evidence this fetch never observed.
    later = _fatal(tmp_path, run_id="concurrent-writer", start="2024-01-03", end="2024-01-11")
    before = {path.name: path.read_bytes() for path in tmp_path.glob("*.json")}
    with pytest.raises(DataQualityError, match="not observed before refetch"):
        _resolve(tmp_path, expected_fatal_receipt_ids=observed)
    assert [item["receipt_id"] for item in status.list_unresolved(tmp_path)] == [first["receipt_id"], later["receipt_id"]]
    assert {path.name: path.read_bytes() for path in tmp_path.glob("*.json")} == before


def test_new_other_symbol_fatal_does_not_invalidate_observed_repair(tmp_path):
    first = _fatal(tmp_path)
    observed = [first["receipt_id"]]
    other = _fatal(tmp_path, symbol="000001.SZ", run_id="concurrent-other-stock")
    assert _resolve(tmp_path, expected_fatal_receipt_ids=observed) == observed
    assert [item["receipt_id"] for item in status.list_unresolved(tmp_path)] == [other["receipt_id"]]
    with pytest.raises(DataQualityError, match="000001.SZ"):
        status.assert_no_unresolved(tmp_path)


def test_already_repaired_observation_is_noop_without_rewriting_history(tmp_path):
    first = _fatal(tmp_path)
    observed = [first["receipt_id"]]
    assert _resolve(tmp_path, expected_fatal_receipt_ids=observed) == observed
    before = {path.name: path.read_bytes() for path in tmp_path.glob("*.json")}
    assert _resolve(tmp_path, run_id="slower-repair", expected_fatal_receipt_ids=observed) == []
    assert status.list_unresolved(tmp_path) == []
    assert {path.name: path.read_bytes() for path in tmp_path.glob("*.json")} == before


def test_current_nonempty_subset_of_claimed_observation_is_rejected(tmp_path):
    first = _fatal(tmp_path)
    with pytest.raises(DataQualityError, match="differs from the pre-refetch observation"):
        _resolve(tmp_path, expected_fatal_receipt_ids=[first["receipt_id"], "c" * 32])
    assert [item["receipt_id"] for item in status.list_unresolved(tmp_path)] == [first["receipt_id"]]
    assert len(list(tmp_path.glob("receipt.*.json"))) == 1


@pytest.mark.parametrize("observed", [
    [], None, "c" * 32, ("c" * 32,), {}, [None], [True], [7],
    ["c" * 31], ["C" * 32], ["not-a-receipt"], ["c" * 32, "c" * 32],
])
def test_empty_malformed_or_duplicate_observed_receipt_ids_rejected(tmp_path, observed):
    first = _fatal(tmp_path)
    with pytest.raises(DataQualityError, match="expected fatal receipt IDs"):
        _resolve(tmp_path, expected_fatal_receipt_ids=observed)
    assert [item["receipt_id"] for item in status.list_unresolved(tmp_path)] == [first["receipt_id"]]
    assert len(list(tmp_path.glob("receipt.*.json"))) == 1


def test_pre_refetch_observed_ids_are_required_api_argument(tmp_path):
    first = _fatal(tmp_path)
    with pytest.raises(TypeError, match="expected_fatal_receipt_ids"):
        status.resolve_fatal(tmp_path, **_context(run_id="run-repair"),
                             cache_metadata_sha256="b" * 64, force_refetched=True,
                             schema_verified=True, refetched_rows=9)
    assert [item["receipt_id"] for item in status.list_unresolved(tmp_path)] == [first["receipt_id"]]


@pytest.mark.parametrize("override", [
    {"force_refetched": False}, {"force_refetched": 1},
    {"schema_verified": False}, {"schema_verified": 1},
    {"refetched_rows": 0}, {"refetched_rows": -1},
    {"refetched_rows": True}, {"refetched_rows": 9.0},
    {"cache_metadata_sha256": "fake"},
])
def test_cache_skip_empty_response_or_unverified_claim_cannot_resolve(tmp_path, override):
    first = _fatal(tmp_path)
    with pytest.raises(DataQualityError):
        _resolve(tmp_path, **override)
    assert [item["receipt_id"] for item in status.list_unresolved(tmp_path)] == [first["receipt_id"]]


@pytest.mark.parametrize("override", [
    {"start": "2024-01-03"}, {"end": "2024-01-11"},
])
def test_partial_success_range_cannot_resolve(tmp_path, override):
    _fatal(tmp_path)
    with pytest.raises(DataQualityError, match="every unresolved window"):
        _resolve(tmp_path, **override)


def test_no_pending_success_does_not_create_history(tmp_path):
    root = tmp_path / "absent"
    assert _resolve(root) == []
    assert not root.exists()


@pytest.mark.parametrize("override", [
    {"symbol": "600000"}, {"symbol": "600000.sh"},
    {"provider_class": ""}, {"source_version": ""},
    {"start": "2024-1-2"}, {"start": "2024-02-30"},
    {"start": "2024-02-01"}, {"start": "2024-01-02T00:00:00"},
    {"run_id": ""}, {"git_commit": "short"},
    {"start": pd.NaT}, {"start": pd.Timestamp("2024-01-02 12:00:00")},
    {"start": pd.Timestamp("2024-01-02", tz="Asia/Shanghai")},
])
def test_invalid_context_fails_before_publication(tmp_path, override):
    with pytest.raises(DataQualityError):
        _fatal(tmp_path / "absent", **override)
    assert not (tmp_path / "absent").exists()


def test_timestamp_trading_date_input_canonicalized(tmp_path):
    first = _fatal(tmp_path, start=pd.Timestamp("2024-01-02"), end=pd.Timestamp("2024-01-12"))
    assert first["context"]["start"] == "2024-01-02"


@pytest.mark.parametrize("operation", ["delete_pointer", "delete_receipt", "truncate_receipt", "unknown_field", "delete_source_field", "tamper_hash", "unknown_file", "duplicate_json"])
def test_partial_missing_corrupt_or_unknown_evidence_blocks(tmp_path, operation):
    first = _fatal(tmp_path)
    path = tmp_path / ("receipt.%s.json" % first["receipt_id"])
    pointer = tmp_path / "current.json"
    if operation == "delete_pointer":
        pointer.unlink()
    elif operation == "delete_receipt":
        path.unlink()
    elif operation == "truncate_receipt":
        path.write_text("{", encoding="utf-8")
    elif operation == "unknown_field":
        _rewrite(path, lambda value: value.update(unknown="skip"))
    elif operation == "delete_source_field":
        _rewrite(path, lambda value: value["context"].pop("source_version"))
    elif operation == "tamper_hash":
        _rewrite(pointer, lambda value: value.update(latest_receipt_sha256="0" * 64))
    elif operation == "unknown_file":
        (tmp_path / "ignore-errors.json").write_text("{}", encoding="utf-8")
    else:
        pointer.write_text('{"schema_version":1,"schema_version":1}', encoding="utf-8")
    with pytest.raises(DataQualityError):
        status.assert_no_unresolved(tmp_path)


def test_pointer_publish_failure_preserves_orphan_and_blocks_rerun(tmp_path, monkeypatch):
    first = _fatal(tmp_path)
    pointer = (tmp_path / "current.json").read_bytes()
    def failed_replace(*args):
        raise PermissionError("simulated Windows replace failure")
    with monkeypatch.context() as patch:
        patch.setattr(status.os, "replace", failed_replace)
        with pytest.raises(DataQualityError, match="inspect orphan evidence"):
            _fatal(tmp_path, run_id="failed-publication")
    assert (tmp_path / "current.json").read_bytes() == pointer
    assert len(list(tmp_path.glob("receipt.*.json"))) == 2
    assert not list(tmp_path.glob(".current.*.tmp"))
    with pytest.raises(DataQualityError, match="count mismatch"):
        status.list_unresolved(tmp_path)
    with pytest.raises(DataQualityError, match="count mismatch"):
        _fatal(tmp_path)
    assert (tmp_path / ("receipt.%s.json" % first["receipt_id"])).exists()


def test_initial_pointer_failure_also_fails_closed(tmp_path, monkeypatch):
    def failed_replace(*args):
        raise OSError("pointer failed")
    monkeypatch.setattr(status.os, "replace", failed_replace)
    with pytest.raises(DataQualityError):
        _fatal(tmp_path)
    with pytest.raises(DataQualityError, match="pointer is missing"):
        status.list_unresolved(tmp_path)


def test_receipt_collision_never_overwrites_prior_event(tmp_path, monkeypatch):
    first = _fatal(tmp_path)
    path = tmp_path / ("receipt.%s.json" % first["receipt_id"])
    content = path.read_bytes()
    monkeypatch.setattr(status.uuid, "uuid4", lambda: SimpleNamespace(hex=first["receipt_id"]))
    with pytest.raises(DataQualityError, match="exclusively publish"):
        _fatal(tmp_path)
    assert path.read_bytes() == content
    assert len(status.list_unresolved(tmp_path)) == 1


def test_noncooperating_history_change_is_not_absorbed_into_new_hash_chain(tmp_path, monkeypatch):
    first = _fatal(tmp_path)
    path = tmp_path / ("receipt.%s.json" % first["receipt_id"])
    real_load = status._load
    def changed_after_read(root, **kwargs):
        loaded = real_load(root, **kwargs)
        path.write_bytes(path.read_bytes() + b"\n")
        return loaded
    monkeypatch.setattr(status, "_load", changed_after_read)
    with pytest.raises(DataQualityError, match="changed before publication"):
        _fatal(tmp_path)
    assert len(list(tmp_path.glob("receipt.*.json"))) == 1


@pytest.mark.parametrize("value", [True, 1.0, "1"])
def test_receipt_schema_version_requires_an_actual_integer(tmp_path, value):
    first = _fatal(tmp_path)
    path = tmp_path / ("receipt.%s.json" % first["receipt_id"])
    _rewrite(path, lambda receipt: receipt.update(schema_version=value))
    with pytest.raises(DataQualityError):
        status.list_unresolved(tmp_path)


def test_ledger_root_and_writer_lock_must_have_correct_file_kind(tmp_path):
    tmp_path.mkdir()
    root_as_file = tmp_path.parent / "file"
    root_as_file.write_text("not a directory", encoding="utf-8")
    with pytest.raises(DataQualityError, match="not a directory"):
        status.list_unresolved(root_as_file)
    (tmp_path / ".writer.lock").mkdir()
    with pytest.raises(DataQualityError, match="not a regular file"):
        _fatal(tmp_path)


@pytest.mark.parametrize("failure", ["create", "write"])
def test_first_immutable_receipt_failure_leaves_a_blocking_ledger(tmp_path, monkeypatch, failure):
    real_open = Path.open
    class FailedWrite:
        def __init__(self, handle):
            self.handle = handle
        def __enter__(self):
            self.handle.__enter__()
            return self
        def __exit__(self, *args):
            return self.handle.__exit__(*args)
        def write(self, content):
            raise OSError("simulated first receipt write failure")
    def fail_receipt_open(path, mode="r", *args, **kwargs):
        if path.name.startswith("receipt.") and mode == "xb":
            if failure == "create":
                raise PermissionError("simulated first receipt create failure")
            return FailedWrite(real_open(path, mode, *args, **kwargs))
        return real_open(path, mode, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(Path, "open", fail_receipt_open)
        with pytest.raises(DataQualityError, match="exclusively publish"):
            _fatal(tmp_path)
    assert tmp_path.is_dir()
    assert not (tmp_path / "current.json").exists()
    with pytest.raises(DataQualityError):
        status.assert_no_unresolved(tmp_path)
    with pytest.raises(DataQualityError):
        _fatal(tmp_path, run_id="retry-cannot-ignore-first-failure")


def test_legacy_lock_only_ledger_is_blocking_not_clean(tmp_path):
    tmp_path.mkdir()
    (tmp_path / ".writer.lock").write_bytes(b"\0")
    with pytest.raises(DataQualityError, match="incomplete initialization"):
        status.assert_no_unresolved(tmp_path)


def test_first_writer_holds_bootstrap_lock_until_receipt_is_committed(tmp_path, monkeypatch):
    publishing = Event()
    release = Event()
    second_started = Event()
    reader_started = Event()
    real_publish = status._publish
    def paused_publish(*args, **kwargs):
        if not publishing.is_set():
            publishing.set()
            assert release.wait(timeout=5), "test did not release bootstrap writer"
        return real_publish(*args, **kwargs)
    def second_writer():
        second_started.set()
        return _fatal(tmp_path, run_id="second-writer")
    def reader():
        reader_started.set()
        return status.list_unresolved(tmp_path)
    monkeypatch.setattr(status, "_publish", paused_publish)
    with ThreadPoolExecutor(max_workers=3) as pool:
        first = pool.submit(_fatal, tmp_path)
        assert publishing.wait(timeout=5)
        assert tmp_path.is_dir()
        assert not (tmp_path / "current.json").exists()
        second = pool.submit(second_writer)
        observation = pool.submit(reader)
        try:
            assert second_started.wait(timeout=5)
            assert reader_started.wait(timeout=5)
            assert not second.done()
            assert not observation.done()
        finally:
            release.set()
        first.result(timeout=10)
        second.result(timeout=10)
        assert observation.result(timeout=10)
    assert len(status.list_unresolved(tmp_path)) == 2


@pytest.mark.parametrize("failure", ["lock_write", "lock_flush", "root_mkdir"])
def test_first_lock_initialization_or_root_creation_failure_cannot_be_clean(tmp_path, monkeypatch, failure):
    real_open, real_mkdir = Path.open, Path.mkdir
    sibling = tmp_path.parent / (".%s.writer.lock" % tmp_path.name)
    class FailedInitialization:
        def __init__(self, handle):
            self.handle = handle
        def __enter__(self):
            self.handle.__enter__()
            return self
        def __exit__(self, *args):
            return self.handle.__exit__(*args)
        def __getattr__(self, name):
            return getattr(self.handle, name)
        def write(self, content):
            if failure == "lock_write":
                raise OSError("simulated first lock initialization write failure")
            return self.handle.write(content)
        def flush(self):
            raise OSError("simulated first lock initialization flush failure")
    def fail_lock_open(path, mode="r", *args, **kwargs):
        handle = real_open(path, mode, *args, **kwargs)
        if path == sibling and mode == "x+b" and failure != "root_mkdir":
            return FailedInitialization(handle)
        return handle
    def fail_root_mkdir(path, *args, **kwargs):
        if path == tmp_path:
            raise OSError("simulated first ledger root mkdir failure")
        return real_mkdir(path, *args, **kwargs)
    monkeypatch.setattr(status, "_LOCK_TIMEOUT_SECONDS", 0.1)
    with monkeypatch.context() as patch:
        patch.setattr(Path, "open", fail_lock_open)
        if failure == "root_mkdir":
            patch.setattr(Path, "mkdir", fail_root_mkdir)
        with pytest.raises(DataQualityError):
            _fatal(tmp_path)
    assert not tmp_path.exists()
    assert sibling.is_file()
    with pytest.raises(DataQualityError, match="incomplete initialization"):
        status.assert_no_unresolved(tmp_path)
    with pytest.raises(DataQualityError, match="incomplete initialization"):
        _resolve(tmp_path)
    with pytest.raises(DataQualityError, match="incomplete initialization"):
        _fatal(tmp_path, run_id="cannot-discard-failed-bootstrap")
    assert not tmp_path.exists()


def test_missing_root_reader_waits_for_live_first_writer_before_rechecking(tmp_path, monkeypatch):
    root_creation = Event()
    release = Event()
    reader_started = Event()
    real_mkdir = Path.mkdir
    def paused_mkdir(path, *args, **kwargs):
        if path == tmp_path:
            root_creation.set()
            assert release.wait(timeout=5)
        return real_mkdir(path, *args, **kwargs)
    def reader():
        reader_started.set()
        return status.list_unresolved(tmp_path)
    monkeypatch.setattr(Path, "mkdir", paused_mkdir)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(_fatal, tmp_path)
        assert root_creation.wait(timeout=5)
        assert not tmp_path.exists()
        assert (tmp_path.parent / (".%s.writer.lock" % tmp_path.name)).exists()
        observation = pool.submit(reader)
        try:
            assert reader_started.wait(timeout=5)
            assert not observation.done()
        finally:
            release.set()
        receipt = first.result(timeout=10)
        pending = observation.result(timeout=10)
    assert [item["receipt_id"] for item in pending] == [receipt["receipt_id"]]


def test_concurrent_writers_and_readers_serialize_history(tmp_path):
    def write(index):
        _fatal(tmp_path, run_id="run-%d" % index)
        return len(status.list_unresolved(tmp_path))
    with ThreadPoolExecutor(max_workers=4) as pool:
        counts = list(pool.map(write, range(16)))
    assert all(1 <= count <= 16 for count in counts)
    assert len(status.list_unresolved(tmp_path)) == 16
    receipts = [json.loads(path.read_text(encoding="utf-8")) for path in tmp_path.glob("receipt.*.json")]
    assert sorted(item["sequence"] for item in receipts) == list(range(1, 17))


def test_independent_processes_serialize_immutable_publication(tmp_path):
    worker = (
        "import sys; from pathlib import Path; "
        "from qbot.research.data.download_status import record_fatal,list_unresolved; "
        "root=Path(sys.argv[1]); "
        "[(record_fatal(root,symbol='600000.SH',provider_class='test.Provider',"
        "source_version='raw-hfq',start='2024-01-02',end='2024-01-12',"
        "run_id=sys.argv[2]+'-'+str(i),git_commit='a'*40,"
        "error_type='SchemaError',error_message='invalid bars'),"
        "list_unresolved(root)) for i in range(3)]"
    )
    processes = [subprocess.Popen([sys.executable, "-c", worker, str(tmp_path), str(index)],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  text=True) for index in range(4)]
    try:
        for process in processes:
            _, error = process.communicate(timeout=45)
            assert process.returncode == 0, error
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=10)
    assert len(status.list_unresolved(tmp_path)) == 12


def test_linked_pointer_is_rejected_even_if_contents_valid(tmp_path):
    _fatal(tmp_path)
    pointer = tmp_path / "current.json"
    outside = tmp_path.parent / (tmp_path.name + "-pointer.json")
    outside.write_bytes(pointer.read_bytes())
    pointer.unlink()
    try:
        pointer.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("host does not allow symlink creation")
    with pytest.raises(DataQualityError, match="link/reparse"):
        status.list_unresolved(tmp_path)
