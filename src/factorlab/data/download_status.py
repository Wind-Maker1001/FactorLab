"""Fail-closed, append-only receipts for unresolved market-data contracts.

Ordinary network failures are deliberately not recorded here.  An observed
schema/contract failure stays blocking until a real, nonempty, verified fetch
covers *all* failed windows for that stock.  Merely switching providers, reading
an older cache, or starting another attempt cannot remove that evidence.

This is accidental-corruption detection, not an external tamper-proof log: a
party able to delete the entire ledger and every external reference can erase
its existence.  Partial deletion, interrupted publication, unknown files, and
malformed history fail closed.  No automatic orphan/stale-file deletion is
performed; inspect the failed publication before explicitly repairing it.
"""

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import time
from typing import Any, Dict, Iterator, List, Optional, Tuple
import uuid

from factorlab.data.schema import normalize_symbol
from factorlab.errors import DataQualityError


SCHEMA_VERSION = 1
_ID = re.compile(r"^[0-9a-f]{32}$")
_SHA = re.compile(r"^[0-9a-f]{64}$")
_GIT = re.compile(r"^[0-9a-f]{40}$")
_RECEIPT_FILE = re.compile(r"^receipt\.([0-9a-f]{32})\.json$")
_LOCK_FILE = ".writer.lock"
_POINTER_FILE = "current.json"
_LOCK_TIMEOUT_SECONDS = 10.0


def _fail(detail: str) -> DataQualityError:
    return DataQualityError("UNRESOLVED DOWNLOAD CONTRACT: " + detail)


def _safe_path(path: Path) -> Path:
    """Check every existing component before following any link/junction."""
    candidate = Path(os.path.abspath(os.fspath(path)))
    for component in reversed([candidate] + list(candidate.parents)):
        if not os.path.lexists(str(component)):
            continue
        try:
            info = os.lstat(str(component))
        except OSError as exc:
            raise _fail("cannot inspect ledger path: %s" % component) from exc
        if stat.S_ISLNK(info.st_mode) or (
            getattr(info, "st_file_attributes", 0)
            & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400)
        ):
            raise _fail("ledger path contains a link/reparse point: %s" % component)
    return candidate


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _fail("%s must be a nonempty string" % label)
    return value


def _date(value: Any, label: str) -> str:
    if isinstance(value, str):
        text = value
    elif hasattr(value, "strftime"):
        if getattr(value, "tzinfo", None) is not None:
            raise _fail("%s must be a timezone-naive trading date" % label)
        try:
            if any(getattr(value, part, 0) != 0 for part in ("hour", "minute", "second", "microsecond", "nanosecond")):
                raise _fail("%s must be midnight on a trading date" % label)
            text = value.strftime("%Y-%m-%d")
        except (TypeError, ValueError) as exc:
            raise _fail("%s must be a valid trading date" % label) from exc
    else:
        raise _fail("%s must be an ISO trading date" % label)
    try:
        parsed = datetime.strptime(text, "%Y-%m-%d")
    except (TypeError, ValueError) as exc:
        raise _fail("%s must be an ISO trading date" % label) from exc
    if parsed.strftime("%Y-%m-%d") != text:
        raise _fail("%s must use YYYY-MM-DD" % label)
    return text


def _context(
    symbol: str, provider_class: str, source_version: Optional[str],
    start: Any, end: Any, run_id: str, git_commit: str,
) -> Dict[str, Any]:
    try:
        canonical = normalize_symbol(symbol)
    except Exception as exc:
        raise _fail("invalid receipt symbol") from exc
    if canonical != symbol:
        raise _fail("receipt symbol must be canonical")
    _text(provider_class, "provider_class")
    if source_version is not None:
        _text(source_version, "source_version")
    _text(run_id, "run_id")
    if not isinstance(git_commit, str) or not _GIT.fullmatch(git_commit):
        raise _fail("git_commit must be a full lowercase Git SHA")
    first, last = _date(start, "start"), _date(end, "end")
    if first > last:
        raise _fail("start must not follow end")
    return {
        "symbol": symbol, "provider_class": provider_class,
        "source_version": source_version, "start": first, "end": last,
        "run_id": run_id, "git_commit": git_commit,
    }


def _keys(value: Any, expected: set, label: str) -> None:
    if not isinstance(value, dict) or set(value) != expected:
        raise _fail("invalid or unknown fields in %s" % label)


def _json(content: bytes, label: str) -> Dict[str, Any]:
    def no_duplicates(pairs: Any) -> Dict[str, Any]:
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON field")
            result[key] = value
        return result
    try:
        result = json.loads(content.decode("utf-8"), object_pairs_hook=no_duplicates)
    except (UnicodeError, ValueError, TypeError) as exc:
        raise _fail("invalid JSON in %s" % label) from exc
    if not isinstance(result, dict):
        raise _fail("%s must contain an object" % label)
    return result


def _read(path: Path) -> bytes:
    _safe_path(path)
    try:
        before = os.lstat(str(path))
        if not stat.S_ISREG(before.st_mode):
            raise _fail("receipt is not a regular file: %s" % path.name)
        content = path.read_bytes()
        after = os.lstat(str(path))
    except OSError as exc:
        raise _fail("missing/unreadable ledger artifact: %s" % path.name) from exc
    identity = lambda info: (
        info.st_dev, info.st_ino, info.st_size, getattr(info, "st_mtime_ns", None)
    )
    if identity(before) != identity(after):
        raise _fail("ledger artifact changed while reading: %s" % path.name)
    _safe_path(path)
    return content


@contextmanager
def _lock(root: Path) -> Iterator[bool]:
    """Permanent sibling OS lock also serializes first-ledger initialization.

    A lock inside a not-yet-created ledger would leave a race between creating
    the directory and obtaining the lock.  The sibling can be acquired before
    that first directory creation; readers never create a missing ledger.
    """
    _safe_path(root)
    try:
        root.parent.mkdir(parents=True, exist_ok=True)
        _safe_path(root.parent)
        path = _safe_path(root.parent / (".%s.writer.lock" % root.name))
        if os.path.lexists(str(path)) and not stat.S_ISREG(os.lstat(str(path)).st_mode):
            raise _fail("ledger writer lock is not a regular file")
        try:
            handle = path.open("x+b", buffering=0)
            created_lock = True
        except FileExistsError:
            handle = path.open("r+b", buffering=0)
            created_lock = False
    except OSError as exc:
        raise _fail("cannot open ledger writer lock") from exc
    with handle:
        deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
        # File creation and native locking are separate OS operations.  A
        # contender must not initialize a newly created zero-byte lock before
        # its creator acquires the OS lock.  An abandoned zero-byte marker is
        # retained as incomplete initialization, never treated as clean.
        while not created_lock and not root.exists() and os.fstat(handle.fileno()).st_size == 0:
            if time.monotonic() >= deadline:
                raise _fail("writer lock has no ledger history; incomplete initialization")
            time.sleep(min(0.02, max(0.0, deadline - time.monotonic())))
        while True:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:  # pragma: no cover - POSIX CI
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if time.monotonic() >= deadline:
                    raise _fail("ledger writer lock timeout") from exc
                time.sleep(min(0.02, max(0.0, deadline - time.monotonic())))
        primary = None
        try:
            try:
                _safe_path(path)
                on_disk, acquired = os.lstat(str(path)), os.fstat(handle.fileno())
                if (on_disk.st_dev, on_disk.st_ino) != (acquired.st_dev, acquired.st_ino):
                    raise _fail("ledger writer lock identity changed")
                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b"\0")
                    handle.flush()
            except OSError as exc:
                raise _fail("cannot initialize ledger writer lock") from exc
            _safe_path(root)
            yield created_lock
        except BaseException as exc:
            primary = exc
            raise
        finally:
            try:
                handle.seek(0)
                if os.name == "nt":
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:  # pragma: no cover - POSIX CI
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError as exc:
                if primary is None:
                    raise _fail("cannot release ledger writer lock") from exc


def _verify_receipt(value: Dict[str, Any], filename: str) -> None:
    common = {"schema_version", "receipt_id", "sequence", "previous_receipt_id",
              "previous_receipt_sha256", "kind", "timestamp", "context"}
    kind = value.get("kind")
    _keys(value, common | ({"fatal"} if kind == "fatal" else {"resolution"}), "receipt")
    if kind not in {"fatal", "resolution"} or value["schema_version"] != SCHEMA_VERSION:
        raise _fail("unsupported receipt schema/kind")
    if type(value["schema_version"]) is not int:
        raise _fail("invalid receipt schema version")
    identity = value["receipt_id"]
    if not isinstance(identity, str) or not _ID.fullmatch(identity):
        raise _fail("invalid receipt ID")
    if filename != "receipt.%s.json" % identity:
        raise _fail("receipt ID does not match filename")
    if type(value["sequence"]) is not int or value["sequence"] < 1:
        raise _fail("invalid receipt sequence")
    context = value["context"]
    _keys(context, {"symbol", "provider_class", "source_version", "start", "end", "run_id", "git_commit"}, "context")
    if _context(**context) != context:
        raise _fail("noncanonical receipt context")
    timestamp = value["timestamp"]
    try:
        parsed = datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%S.%fZ")
    except (ValueError, TypeError) as exc:
        raise _fail("invalid receipt timestamp") from exc
    if parsed.strftime("%Y-%m-%dT%H:%M:%S.%fZ") != timestamp:
        raise _fail("noncanonical receipt timestamp")
    if kind == "fatal":
        _keys(value["fatal"], {"error_type", "error_message"}, "fatal")
        _text(value["fatal"]["error_type"], "error_type")
        _text(value["fatal"]["error_message"], "error_message")
    else:
        result = value["resolution"]
        _keys(result, {"fatal_receipt_ids", "cache_metadata_sha256", "force_refetched", "schema_verified", "refetched_rows"}, "resolution")
        ids = result["fatal_receipt_ids"]
        if (not isinstance(ids, list) or not ids
                or any(not isinstance(item, str) or not _ID.fullmatch(item) for item in ids)
                or len(ids) != len(set(ids))):
            raise _fail("invalid resolved receipt IDs")
        _validate_success(context["source_version"], result["cache_metadata_sha256"],
                          result["force_refetched"], result["schema_verified"], result["refetched_rows"])


def _load(root: Path, *, allow_initial_empty: bool = False) -> Tuple[List[Dict[str, Any]], Dict[str, str]]:
    try:
        names = sorted(path.name for path in root.iterdir())
    except OSError as exc:
        raise _fail("cannot inspect receipt directory") from exc
    unknown = [name for name in names if name not in {_LOCK_FILE, _POINTER_FILE}
               and not _RECEIPT_FILE.fullmatch(name)]
    if unknown:
        raise _fail("unknown/orphan ledger artifact: %s" % unknown[0])
    if _LOCK_FILE in names:
        _read(root / _LOCK_FILE)  # Legacy permanent lock must still be safe.
    receipt_names = [name for name in names if _RECEIPT_FILE.fullmatch(name)]
    if not receipt_names and _POINTER_FILE not in names:
        if allow_initial_empty:
            return [], {}
        raise _fail("existing ledger has no committed receipt history; incomplete initialization")
    if _POINTER_FILE not in names:
        raise _fail("current receipt pointer is missing")
    pointer = _json(_read(root / _POINTER_FILE), _POINTER_FILE)
    _keys(pointer, {"schema_version", "receipt_count", "latest_receipt_id", "latest_receipt_sha256"}, "pointer")
    if type(pointer["schema_version"]) is not int or pointer["schema_version"] != SCHEMA_VERSION:
        raise _fail("unsupported pointer schema")
    if type(pointer["receipt_count"]) is not int or pointer["receipt_count"] != len(receipt_names) or not receipt_names:
        raise _fail("receipt count mismatch or missing immutable receipt")
    receipts = []
    hashes = {}
    for name in receipt_names:
        content = _read(root / name)
        value = _json(content, name)
        _verify_receipt(value, name)
        hashes[value["receipt_id"]] = hashlib.sha256(content).hexdigest()
        receipts.append(value)
    receipts.sort(key=lambda value: value["sequence"])
    previous_id, previous_hash = None, None
    for sequence, value in enumerate(receipts, start=1):
        if (value["sequence"] != sequence or value["previous_receipt_id"] != previous_id
                or value["previous_receipt_sha256"] != previous_hash):
            raise _fail("receipt history/hash chain is broken")
        previous_id = value["receipt_id"]
        previous_hash = hashes[previous_id]
    if pointer["latest_receipt_id"] != previous_id or pointer["latest_receipt_sha256"] != previous_hash:
        raise _fail("current receipt pointer hash does not match history")
    _pending(receipts)  # Validate resolution semantics as well as byte history.
    return receipts, hashes


def _pending(receipts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    pending: Dict[str, Dict[str, Any]] = {}
    for receipt in receipts:
        if receipt["kind"] == "fatal":
            pending[receipt["receipt_id"]] = receipt
            continue
        context = receipt["context"]
        expected = [item for item in pending.values() if item["context"]["symbol"] == context["symbol"]]
        ids = receipt["resolution"]["fatal_receipt_ids"]
        if set(ids) != {item["receipt_id"] for item in expected}:
            raise _fail("resolution must cover all current fatal receipts for its symbol")
        if any(context["start"] > item["context"]["start"] or context["end"] < item["context"]["end"] for item in expected):
            raise _fail("resolution range does not fully cover fatal request windows")
        for identity in ids:
            del pending[identity]
    return list(pending.values())


def _publish(root: Path, receipts: List[Dict[str, Any]], hashes: Dict[str, str], context: Dict[str, Any],
             kind: str, detail: Dict[str, Any]) -> Dict[str, Any]:
    # Do not absorb changes made by a non-cooperating writer into a new chain.
    # The hashes were obtained from the exact bytes parsed in _load, not from
    # another file read that might contain a later history generation.
    for prior in receipts:
        prior_id = prior["receipt_id"]
        if hashlib.sha256(_read(root / ("receipt.%s.json" % prior_id))).hexdigest() != hashes[prior_id]:
            raise _fail("immutable receipt changed before publication")
    identity = uuid.uuid4().hex
    receipt = {
        "schema_version": SCHEMA_VERSION, "receipt_id": identity,
        "sequence": len(receipts) + 1,
        "previous_receipt_id": receipts[-1]["receipt_id"] if receipts else None,
        "previous_receipt_sha256": hashes[receipts[-1]["receipt_id"]] if receipts else None,
        "kind": kind,
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "context": context, kind: detail,
    }
    content = (json.dumps(receipt, sort_keys=True, ensure_ascii=False, allow_nan=False, indent=2) + "\n").encode("utf-8")
    path = _safe_path(root / ("receipt.%s.json" % identity))
    _verify_receipt(receipt, path.name)
    try:
        with path.open("xb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        raise _fail("cannot exclusively publish immutable receipt") from exc
    pointer = {
        "schema_version": SCHEMA_VERSION, "receipt_count": len(receipts) + 1,
        "latest_receipt_id": identity,
        "latest_receipt_sha256": hashlib.sha256(content).hexdigest(),
    }
    temporary = _safe_path(root / (".current.%s.tmp" % uuid.uuid4().hex))
    owned = False
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            owned = True
            json.dump(pointer, handle, sort_keys=True, allow_nan=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        _safe_path(root / _POINTER_FILE)
        os.replace(str(temporary), str(root / _POINTER_FILE))
    except OSError as exc:
        raise _fail("receipt published but current pointer failed; inspect orphan evidence") from exc
    finally:
        if owned and temporary.exists():
            try:
                temporary.unlink()
            except OSError:
                pass  # A leftover temporary is rejected by the next ledger read.
    return receipt


def record_fatal(
    root: Path, *, symbol: str, provider_class: str, source_version: Optional[str],
    start: Any, end: Any, run_id: str, git_commit: str,
    error_type: str, error_message: str,
) -> Dict[str, Any]:
    """Append a fatal contract event without clearing any earlier event."""
    context = _context(symbol, provider_class, source_version, start, end, run_id, git_commit)
    detail = {"error_type": _text(error_type, "error_type"),
              "error_message": _text(error_message, "error_message")}
    candidate = _safe_path(Path(root))
    with _lock(candidate) as created_lock:
        # Only this exact call's creation of a previously missing directory is
        # bootstrap.  An existing empty/lock-only root may be evidence of an
        # earlier first-receipt publication failure and must not be erased.
        if not candidate.exists() and not created_lock:
            raise _fail("existing writer lock has no ledger root; incomplete initialization")
        try:
            candidate.mkdir(exist_ok=False)
            created = True
        except FileExistsError:
            created = False
        except OSError as exc:
            raise _fail("cannot create ledger root") from exc
        receipts, hashes = _load(candidate, allow_initial_empty=created)
        return _publish(candidate, receipts, hashes, context, "fatal", detail)


def list_unresolved(root: Path) -> List[Dict[str, Any]]:
    """Read every provider's unresolved events; corrupt/missing history fails."""
    candidate = _safe_path(Path(root))
    sibling = _safe_path(candidate.parent / (".%s.writer.lock" % candidate.name))
    if not candidate.exists() and not sibling.exists():
        return []  # Legacy clean cache: no ledger and no attempt evidence.
    if candidate.exists() and not candidate.is_dir():
        raise _fail("receipt root is not a directory")
    with _lock(candidate):
        if not candidate.exists():
            raise _fail("writer lock has no ledger root; incomplete initialization")
        receipts, _ = _load(candidate)
        return _pending(receipts)


def _validate_success(source_version: Optional[str], cache_metadata_sha256: str,
                      force_refetched: bool, schema_verified: bool, refetched_rows: int) -> None:
    if source_version is None:
        raise _fail("repair source contract must be verified, not null")
    _text(source_version, "source_version")
    if not isinstance(cache_metadata_sha256, str) or not _SHA.fullmatch(cache_metadata_sha256):
        raise _fail("repair must bind the verified cache metadata SHA-256")
    if force_refetched is not True or schema_verified is not True:
        raise _fail("repair requires an actual forced fetch and schema verification")
    if type(refetched_rows) is not int or refetched_rows <= 0:
        raise _fail("empty/unverified fetch cannot resolve a fatal download")


def resolve_fatal(
    root: Path, *, symbol: str, provider_class: str, source_version: str,
    start: Any, end: Any, run_id: str, git_commit: str,
    cache_metadata_sha256: str, force_refetched: bool, schema_verified: bool,
    refetched_rows: int, expected_fatal_receipt_ids: List[str],
) -> List[str]:
    """Append explicit successful repair evidence; cache hits are not repairs.

    The caller is responsible for passing these flags only after making the
    real request, validating the returned bars, and publishing that cache.
    Repair may use a newly verified provider, but its requested date interval
    must encompass *every* unresolved interval for the specified symbol.
    The caller must pin the fatal receipt IDs before fetching.  A later fatal
    for the same symbol invalidates that repair even when its dates happen to
    lie inside the fetched interval.  Another repair may have already cleared
    the observed set; that is an idempotent no-op, not a new resolution.
    """
    context = _context(symbol, provider_class, source_version, start, end, run_id, git_commit)
    _validate_success(source_version, cache_metadata_sha256, force_refetched, schema_verified, refetched_rows)
    if (not isinstance(expected_fatal_receipt_ids, list) or not expected_fatal_receipt_ids
            or any(not isinstance(item, str) or not _ID.fullmatch(item) for item in expected_fatal_receipt_ids)
            or len(expected_fatal_receipt_ids) != len(set(expected_fatal_receipt_ids))):
        raise _fail("expected fatal receipt IDs must be a nonempty list of unique canonical IDs")
    observed = set(expected_fatal_receipt_ids)
    candidate = _safe_path(Path(root))
    sibling = _safe_path(candidate.parent / (".%s.writer.lock" % candidate.name))
    if not candidate.exists() and not sibling.exists():
        return []
    with _lock(candidate):
        if not candidate.exists():
            raise _fail("writer lock has no ledger root; incomplete initialization")
        receipts, hashes = _load(candidate)
        pending = [item for item in _pending(receipts) if item["context"]["symbol"] == symbol]
        if not pending:
            return []
        current = {item["receipt_id"] for item in pending}
        if current - observed:
            raise _fail("new fatal receipts for %s were not observed before refetch" % symbol)
        # Resolution is all-or-nothing per symbol.  A cooperating repair can
        # clear the whole observed set, never only some of it.  A nonempty
        # subset is therefore an inconsistent/stale observation, not evidence
        # that this fetch may safely resolve a different receipt generation.
        if current != observed:
            raise _fail("current fatal receipt set for %s differs from the pre-refetch observation" % symbol)
        if any(context["start"] > item["context"]["start"] or context["end"] < item["context"]["end"] for item in pending):
            raise _fail("repair must refetch every unresolved window for %s" % symbol)
        identities = [item["receipt_id"] for item in pending]
        detail = {"fatal_receipt_ids": identities, "cache_metadata_sha256": cache_metadata_sha256,
                  "force_refetched": True, "schema_verified": True,
                  "refetched_rows": refetched_rows}
        _publish(candidate, receipts, hashes, context, "resolution", detail)
        return identities


def assert_no_unresolved(root: Path) -> None:
    """Gate all downstream research until every fatal contract is repaired."""
    pending = list_unresolved(root)
    if pending:
        symbols = sorted({item["context"]["symbol"] for item in pending})
        raise _fail("verified refetch required for %s (%d fatal receipt(s))" % (", ".join(symbols), len(pending)))


__all__ = ["assert_no_unresolved", "list_unresolved", "record_fatal", "resolve_fatal"]
