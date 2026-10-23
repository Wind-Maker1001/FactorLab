"""Small immutable evidence snapshots, shared by data qualification modules.

Hashes detect inconsistent local evidence; they are not external certification.
No provider calls or filesystem mutations occur at import time.
"""

from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import io
import json
import os
from pathlib import Path
import re
import uuid
from typing import Any, Dict

import pandas as pd

from factorlab.data.tdx import TdxError, read_snapshot, safe_path
from factorlab.errors import DataQualityError


def fail(detail: str) -> None:
    raise DataQualityError("MARKET DATA QUALIFICATION REQUIRED: " + detail)


def sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def require_hash(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        fail("invalid SHA-256")
    return value


def require_text(value: Any, label: str) -> str:
    if (not isinstance(value, str) or not value or value != value.strip()
            or not value.isprintable() or len(value) > 4096
            or value.upper() in {"UNKNOWN", "UNVERIFIED", "NONE", "NULL", "N/A"}):
        fail("explicit %s required" % label)
    return value


def date(value: Any) -> pd.Timestamp:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        fail("canonical YYYY-MM-DD required")
    try:
        result = pd.Timestamp(value)
    except (ValueError, TypeError) as exc:
        raise DataQualityError("invalid evidence date") from exc
    if pd.isna(result):
        fail("invalid evidence date")
    return result


def timestamp(value: Any) -> pd.Timestamp:
    """A date-only fact uses its Shanghai end-of-day upper bound."""
    if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return (date(value).tz_localize("Asia/Shanghai") + pd.Timedelta(days=1)
                - pd.Timedelta(nanoseconds=1)).tz_convert("UTC")
    try:
        result = pd.Timestamp(value)
    except (ValueError, TypeError) as exc:
        raise DataQualityError("invalid knowledge timestamp") from exc
    if pd.isna(result) or result.tzinfo is None:
        fail("explicit timezone-aware knowledge timestamp required")
    return result.tz_convert("UTC")


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if is_dataclass(value):
        return asdict(value)
    fail("unsupported evidence JSON value")


def json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"),
                       allow_nan=False, default=_json_default) + "\n").encode("ascii")


def table_sha256(frame: pd.DataFrame) -> str:
    """Stable table bytes within the pinned dependency contract; input is unmodified."""
    stream = io.BytesIO()
    frame.to_parquet(stream, index=False)
    return sha256(stream.getvalue())


def implementation_sha256(paths) -> str:
    """Bind actual implementation bytes without including checkout locations."""
    values = sorted((Path(p).name, sha256(Path(p).read_bytes())) for p in paths)
    return sha256(json_bytes(values))


@dataclass(frozen=True)
class SourceArtifact:
    path: str
    sha256: str
    bytes: int
    source_id: str
    acquired_at: str
    locator: str

    def read(self) -> bytes:
        require_hash(self.sha256)
        require_text(self.source_id, "source_id")
        timestamp(self.acquired_at)
        if isinstance(self.bytes, bool) or not isinstance(self.bytes, int) or self.bytes < 0:
            fail("invalid evidence byte count")
        p = Path(self.path)
        try:
            content, digest = read_snapshot(p, p.parent)
        except (TdxError, OSError) as exc:
            raise DataQualityError("unsafe or unreadable evidence") from exc
        if digest != self.sha256 or len(content) != self.bytes:
            fail("source artifact hash/length mismatch")
        return content


def snapshot_file(path: Path, source_id: str, acquired_at: str) -> SourceArtifact:
    require_text(source_id, "source_id")
    timestamp(acquired_at)
    p = Path(path)
    try:
        content, digest = read_snapshot(p, p.parent)
    except (TdxError, OSError) as exc:
        raise DataQualityError("unsafe or unreadable source") from exc
    return SourceArtifact(str(p.resolve()), digest, len(content), source_id,
                          acquired_at, str(p.resolve()))


def archive_bytes(content: bytes, root: Path, source_id: str, locator: str,
                  acquired_at: str) -> SourceArtifact:
    """Content addressed exclusive publication; never overwrite an old file."""
    require_text(source_id, "source_id")
    require_text(locator, "locator")
    timestamp(acquired_at)
    output = safe_path(root)
    output.mkdir(parents=True, exist_ok=True)
    digest = sha256(content)
    destination = safe_path(output / (digest + ".bin"), output)
    if destination.exists():
        old, actual = read_snapshot(destination, output)
        if actual != digest or old != content:
            fail("existing evidence is corrupt")
    else:
        temporary = safe_path(output / (".tmp-" + uuid.uuid4().hex), output)
        with temporary.open("xb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            # A hard-link atomically publishes without replacing a colliding writer.
            os.link(str(temporary), str(destination))
        except FileExistsError:
            if destination.read_bytes() != content:
                fail("evidence publication collision")
        finally:
            temporary.unlink()
    return SourceArtifact(str(destination), digest, len(content), source_id,
                          acquired_at, locator)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
