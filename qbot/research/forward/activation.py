"""Pre-registration receipts for genuine FactorLab forward experiments.

An activation receipt must be created *before* its activation date.  Signals
can later point to that immutable receipt, making it impossible to register a
missed historical date after seeing its returns and call it a genuine start.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
from typing import Optional
import uuid
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
import re

from qbot.research.errors import FactorLabError
from qbot.research.forward.integrity import (
    ForwardIntegrityError,
    assert_no_link_components,
)


class ForwardActivationError(FactorLabError):
    """Raised when forward activation evidence is unsafe or inconsistent."""


def _sha256_text(value: object, name: str, lengths=(64,)) -> str:
    text = str(value)
    if len(text) not in lengths or any(
        character not in "0123456789abcdefABCDEF" for character in text
    ):
        raise ForwardActivationError("%s must be a SHA-256 digest" % name)
    return text.lower()


def _utc_now() -> datetime:
    """Return the registration clock reading (kept patchable for tests)."""

    return datetime.now(timezone.utc)


def _aware_datetime(value: datetime) -> datetime:
    result = value
    if not isinstance(result, datetime) or result.tzinfo is None:
        raise ForwardActivationError("registered_at must be timezone-aware")
    if result.utcoffset() is None:
        raise ForwardActivationError("registered_at must be timezone-aware")
    return result.astimezone(timezone.utc)


def _safe_evidence_path(path: Path) -> Path:
    try:
        return assert_no_link_components(path)
    except ForwardIntegrityError as exc:
        raise ForwardActivationError(str(exc)) from exc


def _parse_date(value: object, name: str) -> date:
    if isinstance(value, datetime):
        raise ForwardActivationError("%s must be a date without a time" % name)
    elif isinstance(value, date):
        result = value
    elif isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        try:
            result = date.fromisoformat(value)
        except (TypeError, ValueError) as exc:
            raise ForwardActivationError("%s must be an ISO date" % name) from exc
    else:
        raise ForwardActivationError("%s must be an ISO date" % name)
    return result


def _timezone(value: str) -> ZoneInfo:
    try:
        return ZoneInfo(value)
    except (TypeError, ZoneInfoNotFoundError) as exc:
        raise ForwardActivationError("unknown activation timezone: %s" % value) from exc


def _safe_config_path(value: object) -> str:
    text = str(value)
    if not text.strip():
        raise ForwardActivationError("config_path must not be empty")
    return text


def _canonical_json(payload: dict) -> bytes:
    return (
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


@dataclass(frozen=True)
class ForwardActivation:
    activation_id: str
    activation_date: date
    registered_at: datetime
    timezone: str
    contract_sha256: str
    source_sha256: str
    git_commit: str
    config_path: str
    path: Path
    manifest_sha256: str


def activation_id(activation_date: object, contract_sha256: object) -> str:
    day = _parse_date(activation_date, "activation_date")
    digest = _sha256_text(contract_sha256, "contract_sha256")
    return "%s-%s" % (day.isoformat(), digest[:16])


def register_forward_activation(
    root: Path,
    *,
    activation_date: object,
    contract_sha256: object,
    source_sha256: object,
    git_commit: object,
    config_path: object,
    timezone_name: str = "Asia/Shanghai",
) -> ForwardActivation:
    """Atomically create one immutable future activation receipt.

    Re-registering the same date/contract is an error, not an idempotent
    rewrite.  A caller can therefore prove that the receipt existed before
    the first permissible signal date.
    """

    day = _parse_date(activation_date, "activation_date")
    contract = _sha256_text(contract_sha256, "contract_sha256")
    source = _sha256_text(source_sha256, "source_sha256")
    commit = _sha256_text(git_commit, "git_commit", lengths=(40, 64))
    timestamp = _aware_datetime(_utc_now())
    market_timezone = _timezone(timezone_name)
    if day <= timestamp.astimezone(market_timezone).date():
        raise ForwardActivationError(
            "activation_date must be strictly after the registration date"
        )
    identifier = activation_id(day, contract)
    activation_root = _safe_evidence_path(Path(root)) / "activations"
    activation_root.mkdir(parents=True, exist_ok=True)
    activation_root = _safe_evidence_path(activation_root)
    destination = activation_root / identifier
    lock_path = activation_root / (".%s.lock" % identifier)
    try:
        descriptor = os.open(
            str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY
        )
    except FileExistsError as exc:
        raise ForwardActivationError(
            "activation registration is already in progress: %s" % identifier
        ) from exc
    os.close(descriptor)
    staging = activation_root / (".%s.tmp-%s" % (identifier, uuid.uuid4().hex))
    try:
        if os.path.lexists(str(destination)):
            raise ForwardActivationError(
                "activation receipt already exists: %s" % identifier
            )
        staging.mkdir()
        payload = {
            "schema_version": 2,
            "activation_id": identifier,
            "activation_date": day.isoformat(),
            "registered_at": timestamp.isoformat(),
            "timezone": timezone_name,
            "contract_sha256": contract,
            "source_sha256": source,
            "git_commit": commit,
            "config_path": _safe_config_path(config_path),
            "live_trading": False,
        }
        content = _canonical_json(payload)
        digest = hashlib.sha256(content).hexdigest()
        (staging / "manifest.json").write_bytes(content)
        (staging / "manifest.sha256").write_text(digest + "\n", encoding="ascii")
        os.replace(str(staging), str(destination))
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        raise
    finally:
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass
    return load_forward_activation(
        root,
        identifier,
        expected_contract_sha256=contract,
        expected_source_sha256=source,
    )


def load_forward_activation(
    root: Path,
    activation: str,
    *,
    expected_contract_sha256: Optional[str] = None,
    expected_source_sha256: Optional[str] = None,
) -> ForwardActivation:
    """Load and cryptographically verify an immutable activation receipt."""

    identifier = str(activation)
    if not identifier or Path(identifier).name != identifier:
        raise ForwardActivationError("activation_id is unsafe")
    activation_root = _safe_evidence_path(Path(root)) / "activations"
    destination = _safe_evidence_path(activation_root / identifier)
    if not destination.is_dir():
        raise ForwardActivationError("activation receipt does not exist: %s" % identifier)
    for child in destination.iterdir():
        _safe_evidence_path(child)
    inventory = {path.name for path in destination.iterdir()}
    if inventory != {"manifest.json", "manifest.sha256"}:
        raise ForwardActivationError("activation receipt inventory is invalid")
    content = (destination / "manifest.json").read_bytes()
    recorded = (destination / "manifest.sha256").read_text(encoding="ascii").strip()
    actual = hashlib.sha256(content).hexdigest()
    if recorded != actual:
        raise ForwardActivationError("activation manifest SHA-256 mismatch")
    try:
        payload = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ForwardActivationError("activation manifest is unreadable") from exc
    required = {
        "schema_version",
        "activation_id",
        "activation_date",
        "registered_at",
        "timezone",
        "contract_sha256",
        "source_sha256",
        "git_commit",
        "config_path",
        "live_trading",
    }
    if set(payload) != required or payload.get("schema_version") != 2:
        raise ForwardActivationError("activation manifest schema is invalid")
    if payload.get("activation_id") != identifier:
        raise ForwardActivationError("activation_id does not match its directory")
    day = _parse_date(payload.get("activation_date"), "activation_date")
    contract = _sha256_text(payload.get("contract_sha256"), "contract_sha256")
    source = _sha256_text(payload.get("source_sha256"), "source_sha256")
    if identifier != activation_id(day, contract):
        raise ForwardActivationError("activation_id does not match receipt content")
    if expected_contract_sha256 is not None and contract != _sha256_text(
        expected_contract_sha256, "expected_contract_sha256"
    ):
        raise ForwardActivationError("activation contract does not match current config")
    if expected_source_sha256 is not None and source != _sha256_text(
        expected_source_sha256, "expected_source_sha256"
    ):
        raise ForwardActivationError("activation source does not match current code")
    try:
        registered = datetime.fromisoformat(str(payload.get("registered_at")))
    except (TypeError, ValueError) as exc:
        raise ForwardActivationError("registered_at is invalid") from exc
    registered = _aware_datetime(registered)
    timezone_name = str(payload.get("timezone"))
    market_timezone = _timezone(timezone_name)
    if day <= registered.astimezone(market_timezone).date():
        raise ForwardActivationError("activation receipt was not registered in advance")
    if payload.get("live_trading") is not False:
        raise ForwardActivationError("activation receipt must keep live_trading=false")
    return ForwardActivation(
        activation_id=identifier,
        activation_date=day,
        registered_at=registered,
        timezone=timezone_name,
        contract_sha256=contract,
        source_sha256=source,
        git_commit=_sha256_text(payload.get("git_commit"), "git_commit", (40, 64)),
        config_path=_safe_config_path(payload.get("config_path")),
        path=destination,
        manifest_sha256=actual,
    )


__all__ = [
    "ForwardActivation",
    "ForwardActivationError",
    "activation_id",
    "load_forward_activation",
    "register_forward_activation",
]
