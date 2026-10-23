"""Read-only CSI/SSE public announcement evidence, NOT a PIT universe provider.

The endpoints below were observed on the publisher's public website. Their
undocumented JSON contract may change. Complete pagination of this catalogue
does not establish complete effective index membership or historical availability.
"""

from dataclasses import dataclass
from datetime import date, datetime, timezone
import hashlib
import http.client
import json
import logging
import math
import os
from pathlib import Path
import re
import socket
import time
from typing import Any, Callable, Dict, List, Optional, Tuple
import urllib.error
import urllib.parse
import urllib.request
import uuid

from qbot.research.errors import CacheError, ProviderError, ProviderNetworkError


LOGGER = logging.getLogger(__name__)
CATALOGUE_URL = "https://www.csindex.com.cn/csindex-home/announcement/queryAnnouncementByVo"
DETAIL_URL = "https://www.csindex.com.cn/csindex-home/announcement/queryAnnouncementById"
CONTRACT_VERSION = "csindex-public-announcements-observed-20261010-v1"
EXCHANGE_CONTRACT_VERSION = "sse-public-index-notices-observed-20261010-v1"
LEGACY_ATTACHMENT_CONTRACT_VERSION = "csindex-legacy-static-notice-attachments-observed-20261010-v1"
_SUFFIXES = {".xlsx": b"PK", ".zip": b"PK", ".pdf": b"%PDF-", ".xls": b"\xd0\xcf\x11\xe0"}


class OfficialAccessDeniedError(ProviderError):
    """Publisher denied this batch; do not change identities or continue requests."""

    def __init__(self, message: str, request_performed: bool):
        super().__init__(message)
        self.request_performed = request_performed


def _date(value: Any) -> str:
    if not isinstance(value, str):
        raise ProviderError("official publishDate must be a YYYY-MM-DD string")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ProviderError("invalid official publishDate") from exc
    if parsed.isoformat() != value:
        raise ProviderError("invalid official publishDate")
    return value


def _positive_integer(value: Any, name: str) -> int:
    if type(value) is not int or value < 1:
        raise ProviderError("%s must be a positive integer" % name)
    return value


def _approved_url(url: str) -> None:
    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise ProviderError("invalid official evidence URL") from exc
    if (parsed.scheme != "https" or parsed.username or parsed.password or
            port not in (None, 443) or parsed.fragment):
        raise ProviderError("only public HTTPS CSI evidence URLs are allowed")
    if parsed.hostname == "www.csindex.com.cn":
        if parsed.path not in (
                "/csindex-home/announcement/queryAnnouncementByVo",
                "/csindex-home/announcement/queryAnnouncementById"):
            raise ProviderError("unobserved official announcement endpoint")
    elif parsed.hostname == "www.sse.com.cn":
        if (not re.fullmatch(r"/market/sseindex/diclosure/c/c_\d{8}_\d+\.shtml", parsed.path)
                or parsed.query):
            raise ProviderError("unobserved Shanghai exchange index notice URL")
    elif parsed.hostname == "oss-ch.csindex.com.cn":
        # Two observed historical notices encode the slash after 'notice'.
        # Decode only to validate scope; retain the publisher URL/wire encoding
        # in the request identity. Reject traversal and double-encoding rather
        # than resolving them into a different resource.
        try:
            decoded_path = urllib.parse.unquote(parsed.path, errors="strict")
        except UnicodeDecodeError as exc:
            raise ProviderError("invalid attachment path encoding") from exc
        legacy = re.fullmatch(r"/static/html/csindex/public/uploads/indices/info/files/\d{8}/\d{16}\.xlsx", decoded_path)
        if ((not decoded_path.startswith("/notice/") and not legacy) or
                (legacy and parsed.query) or "\\" in decoded_path or "%" in decoded_path or
                any(part in (".", "..") for part in decoded_path.split("/")) or
                any(ord(character) < 32 for character in decoded_path)):
            raise ProviderError("attachment must be in an observed publisher notice archive directory")
    else:
        raise ProviderError("unapproved official evidence host")


class _PublicRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _approved_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


@dataclass(frozen=True)
class SourceArtifact:
    """Exactly the bytes read and their receipt; retrieval time is not an as-of claim."""

    content: bytes
    receipt: Dict[str, Any]
    path: Path

    def json(self) -> Dict[str, Any]:
        try:
            value = json.loads(self.content.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise ProviderError("official JSON response is not valid UTF-8 JSON") from exc
        if not isinstance(value, dict):
            raise ProviderError("official JSON response must be an object")
        return value


class OfficialNoticeSource:
    """Bounded HTTP reads with immutable byte objects and verified request receipts.

    Cache reuse means a previously retrieved snapshot, never a fresh observation.
    A caller wanting a fresh catalogue uses a new root (old evidence is preserved).
    No method generates members, assigns effective dates, or bypasses PIT gates.
    """

    def __init__(self, root: Path, timeout_seconds: float = 30.0,
                 request_interval_seconds: float = 0.25, max_retries: int = 3,
                 opener: Optional[Callable] = None, sleeper: Callable = time.sleep,
                 cache_only: bool = False):
        for name, value in (("timeout", timeout_seconds), ("interval", request_interval_seconds)):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ProviderError("%s must be finite" % name)
        if not 0 < timeout_seconds <= 60 or not 0 <= request_interval_seconds <= 60:
            raise ProviderError("timeout/interval is out of bounds")
        if type(max_retries) is not int or not 0 <= max_retries <= 3:
            raise ProviderError("max_retries must be an integer between 0 and 3")
        if type(cache_only) is not bool:
            raise ProviderError("cache_only must be a boolean")
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.timeout_seconds = float(timeout_seconds)
        self.interval = float(request_interval_seconds)
        self.max_retries = max_retries
        self._open = opener or urllib.request.build_opener(_PublicRedirects()).open
        self._sleep = sleeper
        self._last_request: Optional[float] = None
        self.cache_only = cache_only
        self._site_denied = False
        self.network_requests = 0
        self.cache_reads = 0

    def _request(self, url: str, body: Optional[Dict[str, Any]] = None) -> SourceArtifact:
        _approved_url(url)
        host = urllib.parse.urlsplit(url).hostname
        suffix = ".json" if host == "www.csindex.com.cn" else (
            ".html" if host == "www.sse.com.cn" else Path(
                urllib.parse.unquote(urllib.parse.urlsplit(url).path)).suffix.lower())
        if suffix not in (".json", ".html") and suffix not in _SUFFIXES:
            raise ProviderError("unsupported official attachment format: %s" % suffix)
        method = "GET" if body is None else "POST"
        parsed_url = urllib.parse.urlsplit(url)
        wire_url = urllib.parse.urlunsplit((parsed_url.scheme, parsed_url.netloc,
            urllib.parse.quote(parsed_url.path, safe="/%:@"),
            urllib.parse.quote(parsed_url.query, safe="=&%+,:"), ""))
        identity = {"url": url, "method": method, "body": body,
                    "contract": EXCHANGE_CONTRACT_VERSION if host == "www.sse.com.cn" else (
                        LEGACY_ATTACHMENT_CONTRACT_VERSION if host == "oss-ch.csindex.com.cn" and
                        urllib.parse.unquote(parsed_url.path).startswith("/static/") else CONTRACT_VERSION)}
        identity_bytes = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
        key = hashlib.sha256(identity_bytes).hexdigest()
        receipt_path = self.root / (key + ".receipt.json")
        if receipt_path.exists():
            try:
                if receipt_path.is_symlink():
                    raise CacheError("official evidence receipt cannot be a symlink")
                receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
                digest = receipt["content_sha256"]
                if (receipt["schema_version"] != 1 or type(receipt["schema_version"]) is not int or
                        receipt["point_in_time_universe_verified"] is not False or
                        type(receipt["bytes"]) is not int or receipt["bytes"] < 1 or
                        receipt["http_status"] != 200 or type(receipt["http_status"]) is not int or
                        receipt["request"] != identity or not isinstance(digest, str) or len(digest) != 64 or
                        any(c not in "0123456789abcdef" for c in digest) or
                        receipt["file"] != digest + suffix):
                    raise CacheError("official evidence receipt identity is invalid")
                path = self.root / receipt["file"]
                if path.is_symlink():
                    raise CacheError("official evidence object cannot be a symlink")
                content = path.read_bytes()
                if hashlib.sha256(content).hexdigest() != digest or len(content) != receipt["bytes"]:
                    raise CacheError("official evidence content hash mismatch")
                self.cache_reads += 1
                return SourceArtifact(content, receipt, path)
            except (KeyError, TypeError, ValueError, OSError) as exc:
                raise CacheError("invalid official evidence cache") from exc
        if self.cache_only:
            raise ProviderError("official evidence is absent from the verified cache: %s" % url)
        if self._site_denied:
            raise OfficialAccessDeniedError("further HTTP reads skipped after publisher access denial", False)
        limit = 2 * 1024 * 1024 if suffix in (".json", ".html") else 20 * 1024 * 1024
        headers = {"User-Agent": "Qbot-FactorLab/1.0 (public research evidence)"}
        encoded = None
        if body is not None:
            encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        for attempt in range(self.max_retries + 1):
            if self._last_request is not None:
                self._sleep(max(0.0, self.interval - (time.monotonic() - self._last_request)))
            self._last_request = time.monotonic()
            try:
                self.network_requests += 1
                with self._open(urllib.request.Request(wire_url, encoded, headers, method=method),
                                timeout=self.timeout_seconds) as response:
                    _approved_url(response.geturl())
                    if response.status != 200:
                        if response.status in (401, 403):
                            self._site_denied = True
                            raise OfficialAccessDeniedError(
                                "official HTTP status %s; batch HTTP reads stopped" % response.status, True)
                        raise ProviderError("unexpected official HTTP status %s" % response.status)
                    content = response.read(limit + 1)
                    if not content or len(content) > limit:
                        raise ProviderError("empty or oversized official source response")
                    observed_headers = {name: response.headers.get(name) for name in
                                        ("Content-Type", "ETag", "Last-Modified")}
                    resolved_url = response.geturl()
                break
            except urllib.error.HTTPError as exc:
                if exc.code in (401, 403):
                    self._site_denied = True
                    raise OfficialAccessDeniedError("official HTTP status %s; batch HTTP reads stopped" % exc.code, True) from exc
                if exc.code not in (408, 429) and not 500 <= exc.code <= 599:
                    raise ProviderError("official HTTP status %s" % exc.code) from exc
                failure = exc
            except (urllib.error.URLError, TimeoutError, socket.timeout, ConnectionError,
                    http.client.IncompleteRead, http.client.BadStatusLine) as exc:
                failure = exc
            if attempt == self.max_retries:
                raise ProviderNetworkError("official evidence request failed: %s" % url) from failure
            LOGGER.warning("Official evidence transport retry %d: %s", attempt + 1, url)
            self._sleep(2 ** attempt)
        if suffix == ".json":
            SourceArtifact(content, {}, self.root).json()
        elif suffix == ".html":
            if not content.lstrip().lower().startswith((b"<!doctype html", b"<html")):
                raise ProviderError("exchange notice is not an HTML document")
            try:
                content.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ProviderError("exchange notice is not UTF-8 HTML") from exc
        elif not content.startswith(_SUFFIXES[suffix]):
            raise ProviderError("official attachment signature does not match extension")
        digest = hashlib.sha256(content).hexdigest()
        path = self.root / (digest + suffix)
        if path.exists():
            if path.is_symlink() or path.read_bytes() != content:
                raise CacheError("conflicting official evidence object")
        else:
            temporary_object = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
            try:
                with temporary_object.open("xb") as stream:
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(str(temporary_object), str(path))
            finally:
                if temporary_object.exists():
                    temporary_object.unlink()
        receipt = {"schema_version": 1, "request": identity,
                   "retrieved_at": datetime.now(timezone.utc).isoformat(),
                   "resolved_url": resolved_url, "http_status": 200,
                   "response_headers": observed_headers, "file": path.name,
                   "content_sha256": digest, "bytes": len(content),
                   "point_in_time_universe_verified": False}
        temporary = receipt_path.with_name(receipt_path.name + ".tmp-" + uuid.uuid4().hex)
        try:
            with temporary.open("x", encoding="utf-8") as stream:
                json.dump(receipt, stream, ensure_ascii=False, indent=2, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(str(temporary), str(receipt_path))
        finally:
            if temporary.exists():
                temporary.unlink()
        return SourceArtifact(content, receipt, path)

    @staticmethod
    def _response(artifact: SourceArtifact) -> Dict[str, Any]:
        payload = artifact.json()
        if payload.get("success") is not True or payload.get("code") != "200":
            raise ProviderError("official announcement API reported failure")
        return payload

    def catalogue(self, page_size: int = 100) -> Tuple[List[Dict[str, Any]], List[SourceArtifact]]:
        """All pages of the observed CSI-series/rebalance query (not all possible sources)."""
        _positive_integer(page_size, "page_size")
        if page_size > 100:
            raise ProviderError("official page_size must not exceed 100")
        rows: List[Dict[str, Any]] = []
        artifacts: List[SourceArtifact] = []
        total = None
        page = 1
        while total is None or len(rows) < total:
            query = {"lang": "cn", "searchInput": "", "page": {"key": "", "page": page, "rows": page_size},
                     "classList": [], "indexList": ["csi_index"],
                     "relatedTopics": ["index_rebalance"], "typeList": ["announcement"]}
            artifact = self._request(CATALOGUE_URL, query)
            payload = self._response(artifact)
            current_total = _positive_integer(payload.get("total"), "catalogue total")
            if current_total > 10000:
                raise ProviderError("official catalogue exceeds bounded collection size")
            if total is None:
                total = current_total
            if (current_total != total or type(payload.get("currentPage")) is not int or
                    payload["currentPage"] != page or type(payload.get("pageSize")) is not int or
                    payload["pageSize"] != page_size):
                raise ProviderError("official pagination changed; use a fresh archive root")
            entries = payload.get("data")
            if not isinstance(entries, list) or len(entries) != min(page_size, total - len(rows)):
                raise ProviderError("official catalogue has a missing/truncated page")
            for row in entries:
                self._entry(row)
            rows.extend(entries)
            if len({row["id"] for row in rows}) != len(rows):
                raise ProviderError("official catalogue contains duplicate notice IDs")
            artifacts.append(artifact)
            page += 1
        return rows, artifacts

    @staticmethod
    def _entry(row: Any) -> None:
        if not isinstance(row, dict) or not isinstance(row.get("title"), str) or not row["title"].strip():
            raise ProviderError("official notice has no title")
        _positive_integer(row.get("id"), "notice ID")
        _date(row.get("publishDate"))

    def notice(self, notice_id: int) -> Tuple[Dict[str, Any], SourceArtifact]:
        _positive_integer(notice_id, "notice ID")
        artifact = self._request(DETAIL_URL + "?id=%d" % notice_id)
        return self.parse_notice_payload(artifact.json(), notice_id), artifact

    @staticmethod
    def parse_notice_payload(payload: Dict[str, Any], notice_id: int) -> Dict[str, Any]:
        """Validate an already observed public JSON payload; no HTTP or PIT claims."""
        _positive_integer(notice_id, "notice ID")
        if not isinstance(payload, dict) or payload.get("success") is not True or payload.get("code") != "200":
            raise ProviderError("official announcement API reported failure")
        row = payload.get("data")
        OfficialNoticeSource._entry(row)
        if row["id"] != notice_id or not isinstance(row.get("content"), str):
            raise ProviderError("official notice identity/content mismatch")
        enclosures = row.get("enclosureList")
        if not isinstance(enclosures, list):
            raise ProviderError("official notice enclosureList is missing")
        for enclosure in enclosures:
            if (not isinstance(enclosure, dict) or type(enclosure.get("noticeId")) is not int or enclosure["noticeId"] != notice_id or
                    not isinstance(enclosure.get("fileName"), str) or not enclosure["fileName"].strip() or
                    not isinstance(enclosure.get("fileUrl"), str)):
                raise ProviderError("official notice enclosure identity is invalid")
            _approved_url(enclosure["fileUrl"])
            if urllib.parse.urlsplit(enclosure["fileUrl"]).hostname != "oss-ch.csindex.com.cn":
                raise ProviderError("notice enclosure must reference the observed publisher attachment host")
        return row

    def attachment(self, url: str) -> SourceArtifact:
        """Archive a link read from an official notice; this does not interpret it."""
        if urllib.parse.urlsplit(url).hostname != "oss-ch.csindex.com.cn":
            raise ProviderError("attachment requires the observed publisher attachment host")
        return self._request(url)

    def exchange_article(self, url: str) -> SourceArtifact:
        """Archive an observed public SSE index article, never infer members from it."""
        if urllib.parse.urlsplit(url).hostname != "www.sse.com.cn":
            raise ProviderError("exchange article requires the observed Shanghai exchange host")
        return self._request(url)
