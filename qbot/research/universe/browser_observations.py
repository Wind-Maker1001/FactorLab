"""Bind saved public-browser observations without relabelling them as HTTP cache.

The container preserves a decoded response.text() string, NOT compressed wire
bytes. Its transport label and capture time are provenance supplied by the
observation, not independently authenticated timestamp or origin evidence.
This reader has no networking and never supplies point-in-time constituents.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Dict

from qbot.research.errors import ProviderError
from qbot.research.universe.official_sources import DETAIL_URL, OfficialNoticeSource


TRANSPORT = "firecrawl-public-interactive-browser-fetch"
REPRESENTATION = ("exact response.text() string preserved; not compressed wire bytes or a "
                  "historical availability timestamp")
_FIELDS = {"schema_version", "source_url", "http_status", "transport", "observed_at",
           "response_text", "representation", "point_in_time_universe_verified"}


def _unique_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ProviderError("duplicate key in browser observation JSON")
        result[key] = value
    return result


def _json(content):
    try:
        if isinstance(content, bytes):
            content = content.decode("utf-8")
        value = json.loads(content, object_pairs_hook=_unique_keys)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ProviderError("browser observation is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise ProviderError("browser observation JSON must be an object")
    return value


def _observed_at(value: Any) -> str:
    if not isinstance(value, str):
        raise ProviderError("browser observation requires its actual capture timestamp")
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} UTC", value):
            parsed = datetime.strptime(value, "%Y-%m-%d %H:%M:%S UTC").replace(tzinfo=timezone.utc)
        else:
            # Python 3.9 does not yet accept the standard UTC 'Z' spelling.
            # Preserve the original supplied timestamp while parsing it as UTC.
            parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError as exc:
        raise ProviderError("invalid browser observation timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ProviderError("browser observation timestamp must be explicitly UTC")
    if parsed > datetime.now(timezone.utc):
        raise ProviderError("browser observation timestamp is in the future")
    return parsed.isoformat()


@dataclass(frozen=True)
class BrowserNoticeObservation:
    input_path: Path
    container_bytes: bytes
    container_sha256: str
    response_text_sha256: str
    source_url: str
    observed_at: str
    observed_at_utc: str
    notice: Dict[str, Any]

    def evidence(self) -> Dict[str, Any]:
        return {"input_path": str(self.input_path), "observation_sha256": self.container_sha256,
                "response_text_utf8_sha256": self.response_text_sha256, "source_url": self.source_url,
                "capture_time_supplied_by_observation": self.observed_at,
                "capture_time_utc": self.observed_at_utc, "transport_label_supplied_by_observation": TRANSPORT,
                "http_status_supplied_by_observation": 200, "representation": REPRESENTATION,
                "external_origin_authentication_proof": False, "external_timestamp_proof": False,
                "point_in_time_universe_verified": False}


def read_browser_notice(path: Path) -> BrowserNoticeObservation:
    """Read one exact local container, reject ambiguous identity, never request data."""
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ProviderError("browser observation must be a regular non-symlink file")
    if path.stat().st_size > 4 * 1024 * 1024:
        raise ProviderError("browser observation exceeds bounded container size")
    raw = path.read_bytes()
    if len(raw) > 4 * 1024 * 1024:
        raise ProviderError("browser observation exceeds bounded container size")
    payload = _json(raw)
    if set(payload) != _FIELDS or type(payload["schema_version"]) is not int or payload["schema_version"] != 1:
        raise ProviderError("unrecognized browser observation container schema")
    if (type(payload["http_status"]) is not int or payload["http_status"] != 200 or
            payload["transport"] != TRANSPORT or payload["representation"] != REPRESENTATION or
            payload["point_in_time_universe_verified"] is not False):
        raise ProviderError("browser observation provenance/HTTP/representation is invalid")
    url = payload["source_url"]
    if not isinstance(url, str) or not re.fullmatch(re.escape(DETAIL_URL) + r"\?id=[1-9]\d*", url):
        raise ProviderError("browser observation requires the exact public notice URL")
    notice_id = int(url.rsplit("=", 1)[1])
    text = payload["response_text"]
    if not isinstance(text, str) or not text or len(text.encode("utf-8")) > 2 * 1024 * 1024:
        raise ProviderError("browser observation response_text is invalid or oversized")
    notice = OfficialNoticeSource.parse_notice_payload(_json(text), notice_id)
    timestamp = _observed_at(payload["observed_at"])
    if notice["publishDate"] > timestamp[:10]:
        raise ProviderError("notice publication follows browser capture date")
    return BrowserNoticeObservation(path.resolve(), raw, hashlib.sha256(raw).hexdigest(),
                                   hashlib.sha256(text.encode("utf-8")).hexdigest(), url,
                                   payload["observed_at"], timestamp, notice)
