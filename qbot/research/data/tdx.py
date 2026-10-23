"""Offline, read-only TDX DAY discovery and lossless candidate decoding.

This is deliberately not a MarketDataProvider. Layout plausibility, code
prefixes and amount/volume ratios are not independent financial verification.
No adjusted prices, listing dates, suspensions or PIT membership are inferred.
"""

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import stat
import time
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd


FORMAT_VERSION = "tdx-day-le32-raw-candidate-v1"
MARKETS = ("sh", "sz", "bj")
# Retain unsigned bit patterns: a high volume bit can be legitimate (ETF),
# and cannot by itself prove a negative volume or a signed corruption.
DAY_DTYPE = np.dtype([
    ("date", "<u4"), ("open", "<u4"), ("high", "<u4"),
    ("low", "<u4"), ("close", "<u4"), ("amount", "<f4"),
    ("volume", "<u4"), ("reserved", "<u4"),
])
assert DAY_DTYPE.itemsize == 32
NAME = re.compile(r"^(sh|sz|bj)([0-9]{6})\.day$", re.IGNORECASE)


class TdxError(ValueError):
    """Unsafe path, corrupt data or staging integrity error."""


def safe_path(path: Path, boundary: Optional[Path] = None) -> Path:
    """Reject lexical traversal, links and Windows junction/reparse ancestors."""
    path = Path(path).expanduser()
    if ".." in path.parts:
        raise TdxError("PATH_TRAVERSAL: %s" % path)
    absolute = Path(os.path.abspath(str(path)))
    for part in (absolute,) + tuple(absolute.parents):
        if part.exists() or part.is_symlink():
            info = part.lstat()
            if stat.S_ISLNK(info.st_mode) or (
                getattr(info, "st_file_attributes", 0) & 0x400
            ):
                raise TdxError("LINK_OR_REPARSE: %s" % part)
    resolved = absolute.resolve()
    if boundary is not None:
        try:
            resolved.relative_to(Path(boundary).resolve())
        except ValueError as exc:
            raise TdxError("PATH_OUTSIDE_ROOT: %s" % path) from exc
    return resolved


def read_snapshot(path: Path, boundary: Path) -> Tuple[bytes, str]:
    """Hash and parse the same bytes; detect replacement/changes during read."""
    target = safe_path(path, boundary)
    before = target.stat()
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise TdxError("NOT_SINGLE_LINK_REGULAR_FILE: %s" % target)
    with target.open("rb") as handle:
        opened = os.fstat(handle.fileno())
        content = handle.read()
        after = os.fstat(handle.fileno())
    end = safe_path(path, boundary).stat()
    identity = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
    if not (identity(before) == identity(opened) == identity(after) == identity(end)):
        raise TdxError("SOURCE_CHANGED_DURING_READ: %s" % target)
    return content, hashlib.sha256(content).hexdigest()


def classify(market: str, code: str) -> Dict[str, Any]:
    """Conservative versioned prefix hypotheses, never confirmed identities."""
    kind, rule, divisor = "UNKNOWN", "unmapped", None
    if market == "sh":
        if code.startswith(("600", "601", "603", "605", "688", "689")):
            kind, rule, divisor = "A_SHARE_CANDIDATE", "SH:600/601/603/605/688/689", 100
        elif code.startswith("900"):
            kind, rule, divisor = "B_SHARE_CANDIDATE", "SH:900;currency-unverified", 100
        elif code.startswith("000"):
            kind, rule, divisor = "INDEX_CANDIDATE", "SH:000", 100
        elif code.startswith(("51", "56", "58")):
            kind, rule, divisor = "FUND_CANDIDATE", "SH:51/56/58", 1000
        elif code.startswith(("110", "111", "113", "118")):
            kind, rule = "CONVERTIBLE_BOND_CANDIDATE", "SH:110/111/113/118;scale-unknown"
    elif market == "sz":
        if code.startswith(("000", "001", "002", "003", "300", "301")):
            kind, rule, divisor = "A_SHARE_CANDIDATE", "SZ:000/001/002/003/300/301", 100
        elif code.startswith("200"):
            kind, rule, divisor = "B_SHARE_CANDIDATE", "SZ:200;currency-unverified", 100
        elif code.startswith("399"):
            kind, rule, divisor = "INDEX_CANDIDATE", "SZ:399", 100
        elif code.startswith(("15", "16")):
            kind, rule, divisor = "FUND_CANDIDATE", "SZ:15/16", 1000
        elif code.startswith(("123", "127", "128")):
            kind, rule = "CONVERTIBLE_BOND_CANDIDATE", "SZ:123/127/128;scale-unknown"
    elif market == "bj":
        if code.startswith("920"):
            kind, rule, divisor = "A_SHARE_CANDIDATE", "BJ:920;historical-code-events-unknown", 100
        elif code.startswith(("43", "83", "87")):
            kind, rule, divisor = "AMBIGUOUS", "BJ:43/83/87;NEEQ-or-BJ-identity-unknown", 100
    return {
        "security_type": kind, "classification_rule": rule,
        "classification_version": "local-prefix-hypotheses-v1",
        "identity_status": "UNVERIFIED", "confirmed_a_share": False,
        "historical_identity_status": "UNKNOWN", "delisting_status": "UNKNOWN",
        "candidate_price_divisor": divisor,
    }


def discover(root: Path) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Scan only explicit sh/sz/bj lday trees, never following directory links."""
    root = safe_path(root)
    if not root.is_dir():
        raise TdxError("VIPDOC_NOT_ACCESSIBLE: %s" % root)
    entries, issues = [], []
    for market in MARKETS:
        folder = safe_path(root / market / "lday", root)
        if not folder.is_dir():
            raise TdxError("MARKET_LDAY_MISSING: %s" % folder)

        def walk(directory: Path) -> None:
            with os.scandir(directory) as iterator:
                children = sorted(iterator, key=lambda entry: entry.name.lower())
            for entry in children:
                path = Path(entry.path)
                try:
                    safe_path(path, root)
                    if entry.is_dir(follow_symlinks=False):
                        walk(path)
                        continue
                    if path.suffix.lower() != ".day":
                        issues.append({"source_file": str(path), "code": "NON_DAY_ENTRY", "severity": "INFO", "count": 1})
                        continue
                    entries.append({"path": path, "source_market": market, "discovery_error": ""})
                except (TdxError, OSError) as exc:
                    issues.append({"source_file": str(path), "code": "UNSAFE_OR_UNREADABLE_ENTRY", "severity": "ERROR", "count": 1, "detail": str(exc)})
                    if path.suffix.lower() == ".day":
                        entries.append({"path": path, "source_market": market, "discovery_error": str(exc)})
        walk(folder)
    return entries, issues


@dataclass
class DayAudit:
    inventory: Dict[str, Any]
    security: Dict[str, Any]
    coverage: Dict[str, Any]
    anomalies: List[Dict[str, Any]]
    records: Optional[np.ndarray]
    dates: Optional[pd.DatetimeIndex]

    @property
    def convertible(self) -> bool:
        return self.inventory["audit_status"] in {"PASS", "WARN"}


def audit_day(path: Path, market: str, root: Path, start: str, end: str,
              calendar: Optional[pd.DatetimeIndex] = None,
              research_start: Optional[str] = None, research_end: Optional[str] = None,
              warmup_days: int = 130, *, source_bytes: Optional[bytes] = None) -> DayAudit:
    """Audit every record; replay can supply already hash-checked source bytes.

    With source_bytes, path supplies original filename metadata only and is
    never opened. Neither decoding mode certifies financial units or identity.
    """
    info = {"source_file": str(path), "source_market": market, "filename": path.name,
            "symbol": "", "source_sha256": "", "size_bytes": None,
            "record_count": 0, "trailing_bytes": 0, "first_date": "", "last_date": ""}
    begin = time.perf_counter()
    read_end = begin
    anomalies: List[Dict[str, Any]] = []

    def issue(code, mask=None, severity="ERROR", detail=""):
        indices = [] if mask is None else np.flatnonzero(mask)
        count = 1 if mask is None else len(indices)
        if count:
            anomalies.append({"source_file": str(path), "symbol": info["symbol"],
                              "severity": severity, "code": code, "count": int(count),
                              "sample_record_indices": ";".join(str(int(i)) for i in indices[:8]),
                              "detail": detail})

    match = NAME.fullmatch(path.name)
    if not match or match.group(1).lower() != market:
        issue("INVALID_FILENAME_OR_MARKET_PREFIX")
        security = classify(market, "")
    else:
        info["symbol"] = "%s.%s" % (match.group(2), market.upper())
        security = classify(market, match.group(2))
    security.update({"source_file": str(path), "symbol": info["symbol"], "source_market": market})
    records, dates = None, None
    coverage = {"source_file": str(path), "symbol": info["symbol"],
                "calendar_status": "UNKNOWN" if calendar is None else "LOCAL_REFERENCE_NOT_INDEPENDENTLY_VERIFIED",
                "suspension_or_missing_status": "UNKNOWN", "research_coverage_ratio": None,
                "research_expected_dates": None, "research_observed_dates": None,
                "warmup_expected_dates": None, "warmup_observed_dates": None,
                "research_and_warmup_complete": "UNKNOWN"}
    try:
        if source_bytes is None:
            content, digest = read_snapshot(path, root)
        else:
            content = source_bytes
            digest = hashlib.sha256(content).hexdigest()
        read_end = time.perf_counter()
        info.update(source_sha256=digest, size_bytes=len(content),
                    record_count=len(content) // 32, trailing_bytes=len(content) % 32)
        if not content:
            issue("EMPTY_FILE")
        if len(content) % 32:
            issue("TRUNCATED_RECORD", detail="Trailing bytes retained only in original source")
        records = np.frombuffer(content[:len(content) // 32 * 32], dtype=DAY_DTYPE)
        if len(records):
            encoded_dates = records["date"]
            strings = pd.Series(encoded_dates).astype(str)
            parsed = pd.to_datetime(strings, format="%Y%m%d", errors="coerce")
            valid = (strings.str.fullmatch(r"[0-9]{8}").to_numpy() & parsed.notna().to_numpy())
            dates = pd.DatetimeIndex(parsed)
            issue("INVALID_DATE", ~valid)
            info["first_date"] = str(int(encoded_dates[0]))
            info["last_date"] = str(int(encoded_dates[-1]))
            issue("DUPLICATE_DATE", pd.Series(encoded_dates).duplicated().to_numpy())
            issue("NON_INCREASING_DATE", np.r_[False, encoded_dates[1:] <= encoded_dates[:-1]])
            issue("OUTSIDE_REQUESTED_RANGE", valid & ((dates < pd.Timestamp(start)) | (dates > pd.Timestamp(end))), "WARN", "Preserved, not clipped")
            issue("WEEKEND_OBSERVATION", valid & (dates.dayofweek >= 5), "WARN")
            prices = np.column_stack([records[field] for field in ("open", "high", "low", "close")])
            issue("NON_POSITIVE_OR_SIGNED_NEGATIVE_PRICE", (prices == 0).any(axis=1) | (prices >= 2 ** 31).any(axis=1))
            issue("INVALID_OHLC", (prices[:, 1] < prices[:, [0, 3]].max(axis=1)) |
                  (prices[:, 2] > prices[:, [0, 3]].min(axis=1)) | (prices[:, 1] < prices[:, 2]))
            issue("INVALID_AMOUNT", ~np.isfinite(records["amount"]) | (records["amount"] < 0))
            issue("VOLUME_HIGH_BIT_SIGN_OR_OVERFLOW_UNKNOWN", records["volume"] >= 2 ** 31, "WARN",
                  "Unsigned bit pattern retained; can be valid ETF volume, signed negative interpretation is unverified")
            issue("ZERO_VOLUME_STATUS_UNKNOWN", records["volume"] == 0, "WARN", "Does not establish suspension")
            issue("AMOUNT_VOLUME_ZERO_MISMATCH", ((records["volume"] == 0) != (records["amount"] == 0)), "WARN")
            # Candidate diagnostics, never enforce ST/board-specific limits
            # without verified security history and corporate actions.
            close = records["close"].astype(float)
            ratios = close[1:] / np.where(close[:-1] == 0, np.nan, close[:-1])
            issue("LARGE_RAW_PRICE_CHANGE_REQUIRES_ACTION_REVIEW", np.r_[False, (ratios > 1.35) | (ratios < 0.65)], "WARN")
            divisor = security["candidate_price_divisor"]
            if divisor is not None and security["security_type"] == "A_SHARE_CANDIDATE":
                # An internal plausibility check, not a unit certification.
                with np.errstate(divide="ignore", invalid="ignore"):
                    implied = records["amount"].astype(float) / records["volume"]
                positive = (records["volume"] > 0) & (records["amount"] > 0)
                issue("IMPLIED_PRICE_OUTSIDE_CANDIDATE_RANGE", positive &
                      ((implied < prices[:, 2] / divisor * .95) | (implied > prices[:, 1] / divisor * 1.05)),
                      "WARN", "Candidate CNY/shares interpretation only; units remain UNVERIFIED")
            observed = dates[valid].unique()
            for year in range(pd.Timestamp(start).year, pd.Timestamp(end).year + 1):
                scope_start, scope_end = max(pd.Timestamp(start), pd.Timestamp(year, 1, 1)), min(pd.Timestamp(end), pd.Timestamp(year, 12, 31))
                selected = observed[(observed >= scope_start) & (observed <= scope_end)]
                coverage["records_%s" % year] = len(selected)
                if calendar is not None:
                    expected = calendar[(calendar >= scope_start) & (calendar <= scope_end)]
                    coverage["reference_dates_%s" % year] = len(expected)
                    coverage["coverage_%s" % year] = None if len(expected) == 0 else len(selected.intersection(expected)) / len(expected)
            if calendar is not None and research_start and research_end:
                expected = calendar[(calendar >= pd.Timestamp(research_start)) & (calendar <= pd.Timestamp(research_end))]
                previous = calendar[calendar < pd.Timestamp(research_start)][-warmup_days:] if warmup_days else calendar[:0]
                obs = observed.intersection(expected)
                warm = observed.intersection(previous)
                complete_calendar = (pd.Timestamp(research_start) in calendar and pd.Timestamp(research_end) in calendar and len(previous) == warmup_days)
                coverage.update(research_expected_dates=len(expected), research_observed_dates=len(obs),
                                research_coverage_ratio=None if len(expected) == 0 else len(obs) / len(expected),
                                warmup_expected_dates=warmup_days, warmup_observed_dates=len(warm),
                                research_and_warmup_complete=bool(complete_calendar and len(obs) == len(expected) and len(warm) == warmup_days))
                missing = expected.difference(observed)
                coverage["missing_research_date_samples"] = ";".join(str(d.date()) for d in missing[:8])
                coverage["missing_research_dates"] = len(missing)
                if len(missing):
                    issue("REFERENCE_CALENDAR_GAPS_STATUS_UNKNOWN", severity="WARN", detail="%s research dates absent; listing/suspension/delisting unknown" % len(missing))
    except (OSError, TdxError) as exc:
        issue("READ_OR_PATH_FAILED", detail=str(exc))
    info["audit_status"] = "FAIL" if any(a["severity"] == "ERROR" for a in anomalies) else "WARN" if anomalies else "PASS"
    info["read_hash_seconds"] = read_end - begin
    info["decode_audit_seconds"] = time.perf_counter() - read_end
    return DayAudit(info, security, coverage, anomalies, records, dates)


def raw_frame(audit: DayAudit) -> pd.DataFrame:
    """Candidate display fields plus exact encoded integers; no adj_* fields."""
    if not audit.convertible or audit.records is None or audit.dates is None:
        raise TdxError("CORRUPT_SOURCE_CANNOT_CONVERT")
    records = audit.records
    frame = pd.DataFrame({"date": audit.dates, "symbol": audit.inventory["symbol"]})
    divisor = audit.security["candidate_price_divisor"]
    for field in ("open", "high", "low", "close"):
        frame["tdx_" + field + "_integer"] = records[field]
        frame[field] = records[field].astype(np.float64) / divisor if divisor else np.nan
    frame["volume"] = records["volume"]
    frame["amount"] = records["amount"].astype(np.float64)
    frame["tdx_date_integer"] = records["date"]
    frame["tdx_reserved"] = records["reserved"]
    for key in ("source_market", "source_file", "source_sha256"):
        frame[key] = audit.inventory[key]
    frame["original_record_count"] = len(records)
    frame["adjustment_status"] = "RAW_UNADJUSTED"
    frame["adjustment_evidence_status"] = "UNVERIFIED"
    frame["parse_status"] = audit.inventory["audit_status"]
    frame["candidate_price_divisor"] = np.nan if divisor is None else divisor
    frame["price_scale_status"] = "UNVERIFIED" if divisor else "UNKNOWN"
    frame["volume_unit"] = "UNKNOWN"
    frame["amount_unit"] = "UNKNOWN"
    frame["research_eligible"] = False
    return frame
