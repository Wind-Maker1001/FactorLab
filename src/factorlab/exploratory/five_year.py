"""Five-year feasibility pilot, isolated exploration / NO LIVE MONEY."""

from pathlib import Path
import argparse
from contextlib import contextmanager
import io
import json
import platform
import sys
import time
import uuid

import numpy as np
import pandas as pd

from factorlab.data.cache import ParquetBarCache
from factorlab.data.calendar import TradingCalendar, reindex_bars_to_calendar
from factorlab.data.evidence import implementation_sha256, sha256
from factorlab.data.providers.akshare import AKShareProvider
from factorlab.data.schema import (CANONICAL_BAR_COLUMNS, canonicalize_bars,
                                      empty_bars, normalize_symbol, validate_bars)
from factorlab.data.tdx import safe_path
from factorlab.data.tdx_staging import write_new
from factorlab.errors import ProviderError
from factorlab.exploratory.acquire import RecordingAKShare, utc_now
from factorlab.exploratory.contract import (assert_exploratory_only,
                                              manifest_version, sha256_file,
                                              validate_dataset_frames)
from factorlab.reproducibility import git_commit, package_versions


DEFAULT_SYMBOLS = (
    "600000.SH", "000001.SZ", "600519.SH", "000002.SZ", "300750.SZ",
    "688981.SH", "601059.SH", "001289.SZ", "600112.SH", "000961.SZ",
)
SELECTION_METHOD = (
    "Fixed engineering strata across Shanghai/Shenzhen, older and later listings, "
    "corporate-action and historical-exit coverage challenges; frozen before any "
    "target-window provider request. No return, RankIC, or current-survival screening, "
    "no replacement of unavailable securities. Stratum labels are hypotheses until "
    "independently evidenced; not a historical CSI500 constituent pool."
)
STRATA = {
    "600000.SH": "OLDER_LISTING_AND_CORPORATE_ACTION_CHALLENGE",
    "000001.SZ": "OLDER_LISTING_CHALLENGE",
    "600519.SH": "CORPORATE_ACTION_CHALLENGE",
    "000002.SZ": "HISTORICAL_SECURITY_STATE_CHALLENGE",
    "300750.SZ": "LATER_LISTING_2018_HYPOTHESIS",
    "688981.SH": "LATER_LISTING_2020_HYPOTHESIS",
    "601059.SH": "POST_WINDOW_START_LISTING_2021_HYPOTHESIS",
    "001289.SZ": "POST_WINDOW_START_LISTING_2022_HYPOTHESIS",
    "600112.SH": "HISTORICAL_EXIT_COVERAGE_CHALLENGE",
    "000961.SZ": "HISTORICAL_EXIT_COVERAGE_CHALLENGE",
}


def _json(value):
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True,
                       allow_nan=False) + "\n").encode("utf-8")


def freeze_pilot(root, symbols=DEFAULT_SYMBOLS, synthetic_only=False):
    """Freeze once, before HTTP; unavailable names remain in the pilot."""
    if type(synthetic_only) is not bool:
        raise ValueError("explicit synthetic boolean required")
    root = safe_path(Path(root))
    root.mkdir(parents=True, exist_ok=True)
    path = safe_path(root / "sample.json", root)
    canonical = [normalize_symbol(value) for value in symbols]
    if not canonical or len(canonical) != len(set(canonical)):
        raise ValueError("unique nonempty frozen symbols required")
    if path.exists():
        previous = json.loads(path.read_text(encoding="utf-8"))
        if (previous.get("symbols") != canonical
                or previous.get("selection_method") != SELECTION_METHOD
                or previous.get("synthetic_only") is not synthetic_only
                or previous.get("pit") is not False
                or previous.get("formal_admission") is not False):
            raise ValueError("cannot replace the pre-frozen pilot sample")
        return previous
    now = pd.Timestamp(utc_now())
    value = {
        "schema": "FIVE_YEAR_FROZEN_PILOT_V1",
        "symbols": canonical,
        "selection_method": SELECTION_METHOD,
        "selection_date": now.tz_convert("Asia/Shanghai").strftime("%Y-%m-%d"),
        "frozen_at": now.isoformat(),
        "pit": False,
        "classification": "NON_PIT_CONVENIENCE",
        "purpose": "EXPLORATORY",
        "qualification": "UNVERIFIED",
        "formal_admission": False,
        "synthetic_only": synthetic_only,
        "securities": [{"symbol": symbol,
                        "selection_stratum": STRATA.get(symbol, "CUSTOM_FROZEN_ENGINEERING_SAMPLE"),
                        "identity_status": "UNVERIFIED",
                        "listing_status": "UNVERIFIED",
                        "historical_membership": "UNVERIFIED"}
                       for symbol in canonical],
        "biases": ["manually selected engineering sample", "known-name convenience bias",
                   "not representative of historical CSI500 membership",
                   "status strata are unverified hypotheses"],
        "no_live_money": True,
    }
    write_new(path, _json(value))
    return value


VERSION = "five-year-minimal-ingestion-v3"
UNITS = {"price": "CNY", "volume": "SHARES", "amount": "CNY", "turnover": "RATIO"}
# Lock namespace only: no bars or metadata are published for this reserved key.
# It is distinct from every frozen pilot security and the CSI500 benchmark.
PILOT_LOCK_SYMBOL = "999999.SZ"
CSI_NATIVE_FIELDS = ("tradeDate", "indexCode", "indexNameCnAll", "indexNameCn", "indexNameEnAll", "indexNameEn",
                     "open", "high", "low", "close", "change", "changePct", "tradingVol", "tradingValue", "consNumber", "peg")
CSI_VENDOR_COLUMNS = ("日期", "指数代码", "指数中文全称", "指数中文简称", "指数英文全称", "指数英文简称",
                      "开盘", "最高", "最低", "收盘", "涨跌", "涨跌幅", "成交量", "成交金额", "样本数量", "滚动市盈率")
CALENDAR_SCOPE = "Observed native benchmark sessions; independent exchange-calendar completeness remains UNVERIFIED"


class _SupplierAccessLimit(ProviderError):
    """An explicit supplier denial ends this pilot's new HTTP requests."""


class _AccessLimitedRecorder(RecordingAKShare):
    """Preserve a denied response first, then stop without provider retries."""

    def __init__(self, module, root):
        self.access_limit = None
        super().__init__(module, root)
        # Prior denials remain immutable evidence, but only stop their own run.
        # A later explicit invocation may issue one ordinary request again.
        self.replay = {url: pair for url, pair in self.replay.items()
                       if pair[1].get("status_code") not in {401, 403, 429}}

    @contextmanager
    def capture(self, interface, kwargs):
        import requests

        with super().capture(interface, kwargs):
            recorded = requests.sessions.Session.request

            def guarded(session, method, url, **options):
                if self.access_limit is not None:
                    raise _SupplierAccessLimit("supplier access limit already recorded; no new HTTP permitted")
                response = recorded(session, method, url, **options)
                if response.status_code in {401, 403, 429}:
                    self.access_limit = {"status_code": response.status_code,
                                         "interface": interface, "interface_arguments": dict(kwargs),
                                         "url": response.url, "detected_at": utc_now(),
                                         "policy": "STOP_NEW_HTTP_THIS_RUN_NO_IMMEDIATE_RETRY_NO_PROXY"}
                    raise _SupplierAccessLimit("supplier HTTP %s; new requests stopped for this run" % response.status_code)
                return response

            requests.sessions.Session.request = guarded
            try:
                yield
            finally:
                requests.sessions.Session.request = recorded


def _day(value):
    import re

    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("canonical YYYY-MM-DD required")
    result = pd.Timestamp(value)
    if pd.isna(result):
        raise ValueError("valid date required")
    return result


def _item(root, path):
    path = safe_path(Path(path), root)
    return {"path": path.relative_to(root).as_posix(), "sha256": sha256_file(path),
            "bytes": path.stat().st_size}


def _parquet(path, frame):
    stream = io.BytesIO()
    frame.to_parquet(stream, index=False)
    write_new(path, stream.getvalue())


def _csv(path, frame):
    write_new(path, frame.to_csv(index=False, lineterminator="\n").encode("utf-8"))


def _copy_bound(path, target, expected_hash=None):
    """Copy one byte snapshot; concurrent upstream replacement fails explicitly."""
    content = safe_path(Path(path)).read_bytes()
    import hashlib

    if expected_hash is not None and hashlib.sha256(content).hexdigest() != expected_hash:
        raise ValueError("source changed while taking immutable evidence snapshot")
    target.parent.mkdir(parents=True, exist_ok=True)
    write_new(target, content)


def _copy_cache(snapshot, cache, symbol, run):
    target = run / "evidence" / "cache"
    target.mkdir(parents=True, exist_ok=True)
    metadata = snapshot.metadata
    if not metadata:
        return
    # A generation name comes from the validated snapshot, not a fresh pointer.
    data = cache.root / metadata.get("data_file", "%s.parquet" % symbol)
    _copy_bound(data, target / (symbol + ".parquet"), metadata["content_sha256"])
    _copy_bound(cache.metadata_path(symbol), target / (symbol + ".metadata.json"),
                snapshot.metadata_sha256)


def _intervals(sessions, missing):
    """Describe contiguous missing *actual sessions*, without causal inference."""
    output = []
    positions = np.flatnonzero(np.asarray(missing, dtype=bool))
    if not len(positions):
        return output
    groups = np.split(positions, np.flatnonzero(np.diff(positions) != 1) + 1)
    for group in groups:
        output.append({"start": sessions[group[0]].strftime("%Y-%m-%d"),
                       "end": sessions[group[-1]].strftime("%Y-%m-%d"),
                       "actual_sessions": len(group), "reason": "UNKNOWN_UNVERIFIED"})
    return output


def _coverage(frame, sessions, symbols, start, end):
    coverage, annual = [], []
    research = (sessions >= start) & (sessions <= end)
    for symbol in symbols:
        rows = frame.loc[frame["symbol"].eq(symbol)].set_index("date").reindex(sessions)
        raw = rows[["raw_" + f for f in ("open", "high", "low", "close")]].notna().all(axis=1)
        hfq = rows[["adj_" + f for f in ("open", "high", "low", "close")]].notna().all(axis=1)
        observed = raw & hfq
        valid_research = observed.to_numpy() & research
        warmup = int((observed.to_numpy() & (sessions < start)).sum())
        research_dates = sessions[research]
        missing = ~observed.to_numpy()[research]
        observed_dates = sessions[observed.to_numpy()]
        # Readiness only, without evaluating factors or manufacturing a label.
        ready = rows["adj_open"].shift(-1).notna() & rows["adj_open"].shift(-6).notna()
        coverage.append({"symbol": symbol, "actual_calendar_sessions": len(sessions),
                         "research_expected_sessions": int(research.sum()),
                         "raw_observed_research_sessions": int((raw.to_numpy() & research).sum()),
                         "hfq_observed_research_sessions": int((hfq.to_numpy() & research).sum()),
                         "paired_observed_research_sessions": int(valid_research.sum()),
                         "missing_research_sessions": int(missing.sum()),
                         "coverage_ratio": float(valid_research.sum() / research.sum()) if research.any() else None,
                         "observed_start": observed_dates.min().strftime("%Y-%m-%d") if len(observed_dates) else None,
                         "observed_end": observed_dates.max().strftime("%Y-%m-%d") if len(observed_dates) else None,
                         "warmup_observed_sessions": warmup,
                         "warmup_required_sessions": 130, "warmup_sufficient": warmup >= 130,
                         "t_plus_6_observed_price_ready_dates": int((ready.to_numpy() & research).sum()),
                         "missing_intervals": _intervals(research_dates, missing),
                         "identity": "UNVERIFIED", "security_state": "UNVERIFIED",
                         "pit": "PIT_UNVERIFIED", "hfq": "HFQ_UNVERIFIED"})
        for year in range(start.year, end.year + 1):
            selection = research & (sessions.year == year)
            expected = int(selection.sum())
            count = int((observed.to_numpy() & selection).sum())
            annual.append({"symbol": symbol, "year": year, "expected_sessions": expected,
                           "paired_observed_sessions": count, "missing_sessions": expected - count,
                           "coverage_ratio": count / expected if expected else None,
                           "missing_reason": "UNKNOWN_UNVERIFIED"})
    return coverage, annual


def _stdlib_memory():
    """Read process memory without adding a runtime dependency."""
    try:
        if sys.platform == "win32":
            import ctypes
            from ctypes import wintypes

            class Counters(ctypes.Structure):
                _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)] + [
                    (name, ctypes.c_size_t) for name in (
                        "PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage",
                        "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage",
                        "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage")]

            kernel, psapi = ctypes.WinDLL("kernel32"), ctypes.WinDLL("psapi")
            kernel.GetCurrentProcess.restype = wintypes.HANDLE
            psapi.GetProcessMemoryInfo.argtypes = (wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD)
            psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
            value = Counters()
            value.cb = ctypes.sizeof(value)
            if not psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(value), value.cb):
                raise OSError("GetProcessMemoryInfo failed")
            return {"process_rss_bytes": int(value.WorkingSetSize),
                    "process_peak_rss_bytes": int(value.PeakWorkingSetSize),
                    "memory_method": "Windows GetProcessMemoryInfo WorkingSet/PeakWorkingSet in bytes"}
        import resource

        scale = 1 if sys.platform == "darwin" else 1024
        return {"process_rss_bytes": None,
                "process_peak_rss_bytes": int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * scale),
                "memory_method": "resource.ru_maxrss converted from %s to bytes" % ("bytes" if scale == 1 else "KiB")}
    except (ImportError, AttributeError, OSError):
        return {"process_rss_bytes": None, "process_peak_rss_bytes": None,
                "memory_method": "UNAVAILABLE"}


def _benchmark_records(source_roots):
    """Read byte snapshots of CSI captures, retaining the latest exact URL."""
    latest = {}
    for root in source_roots:
        root = safe_path(Path(root))
        for path in sorted((root / "sources").rglob("*.request.json")):
            receipt_bytes = safe_path(path, root).read_bytes()
            entry = json.loads(receipt_bytes)
            if entry.get("interface") != "stock_zh_index_hist_csindex":
                continue
            relative = Path(entry["response_file"])
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError("benchmark native response escapes source root")
            body = safe_path(root / relative, root).read_bytes()
            if sha256(body) != entry["response_sha256"]:
                raise ValueError("benchmark native receipt/response SHA mismatch")
            record = {"entry": entry, "receipt_bytes": receipt_bytes, "body": body}
            prior = latest.get(entry["url"])
            if prior is None or _instant(entry["retrieved_at"]) > _instant(prior["entry"]["retrieved_at"]):
                latest[entry["url"]] = record
    return list(latest.values())


def _reconstruct_benchmark(records, start, end, retrieved_at, synthetic_only):
    """Replay an exact CSI response through the existing provider, without HTTP."""
    from urllib.parse import parse_qs, urlsplit
    from factorlab.data.providers.akshare import AKShareProvider as CanonicalAKShareProvider

    pieces = []
    for record in records:
        entry = record["entry"]
        if sha256(record["body"]) != entry.get("response_sha256"):
            raise ValueError("benchmark native receipt/response SHA mismatch")
        arguments = entry.get("interface_arguments", {})
        begin, finish = arguments.get("start_date"), arguments.get("end_date")
        if (entry.get("interface") != "stock_zh_index_hist_csindex" or arguments.get("symbol") != "000905"
                or set(arguments) != {"symbol", "start_date", "end_date"}
                or any(not isinstance(value, str) or len(value) != 8 or not value.isdigit() for value in (begin, finish))):
            raise ValueError("benchmark source must bind CSI500 and exact daily-history dates")
        begin_date, end_date = (pd.to_datetime(value, format="%Y%m%d", errors="raise") for value in (begin, finish))
        requested, acquired = _instant(entry.get("requested_at", entry["retrieved_at"])), _instant(entry["retrieved_at"])
        if (begin_date > end_date or not requested <= acquired <= _instant(retrieved_at)
                or end_date > acquired.tz_convert("Asia/Shanghai").normalize().tz_localize(None)):
            raise ValueError("benchmark source dates/acquisition timestamp inconsistent")
        parsed = urlsplit(entry["url"])
        query = parse_qs(parsed.query, keep_blank_values=True)
        if synthetic_only:
            expected = {key: [str(value)] for key, value in arguments.items()}
            valid_url = parsed.scheme == "https" and parsed.netloc == "example.invalid" and parsed.path == "/pilot-index" and query == expected
        else:
            expected = {"indexCode": ["000905"], "startDate": [begin], "endDate": [finish]}
            valid_url = (parsed.scheme == "https" and parsed.netloc == "www.csindex.com.cn"
                         and parsed.path == "/csindex-home/perf/index-perf" and query == expected)
        if (not valid_url or parsed.fragment or str(entry.get("method")).upper() != "GET"
                or entry.get("status_code") != 200 or isinstance(entry.get("status_code"), bool)
                or not entry.get("content_type", "").lower().startswith("application/json")
                or entry.get("upstream_vintage") != "UNKNOWN_UNVERIFIED"):
            raise ValueError("benchmark native supplier URL/parameters/status/content-type contract mismatch")
        payload = json.loads(record["body"])
        if synthetic_only:
            if payload.get("synthetic_only") is not True:
                raise ValueError("synthetic benchmark must remain explicitly classified")
            vendor = pd.DataFrame(payload["rows"])
        else:
            if payload.get("code") != "200" or payload.get("success") is not True or not isinstance(payload.get("data"), list) or not payload["data"]:
                raise ValueError("benchmark native supplier payload status or data unavailable")
            if any(not isinstance(row, dict) or tuple(row) != CSI_NATIVE_FIELDS or row["indexCode"] != "000905"
                   for row in payload["data"]):
                raise ValueError("benchmark native sixteen-field layout/index identity mismatch")
            vendor = pd.DataFrame(payload["data"], columns=CSI_NATIVE_FIELDS)
            vendor.columns = CSI_VENDOR_COLUMNS
        dates = pd.DatetimeIndex(pd.to_datetime(vendor["日期"], format="mixed", errors="raise"))
        if (dates.tz is not None or dates.hasnans or dates.has_duplicates or not dates.equals(dates.normalize())
                or not ((dates >= begin_date) & (dates <= end_date)).all()):
            raise ValueError("benchmark native dates must be unique daily sessions inside the declared request")

        class ArchivedCSI:
            __version__ = "ARCHIVED_NATIVE_RESPONSE_REPLAY"

            def stock_zh_index_hist_csindex(self, **kwargs):
                return vendor.copy()

        provider = CanonicalAKShareProvider(akshare_module=ArchivedCSI(), request_interval=0, retry_count=0)
        normalized = provider.get_index_bars("000905.SH", begin_date, end_date)
        pieces.append((acquired, normalized))
    if not pieces:
        raise ValueError("benchmark cache has no matching native CSI response evidence")
    rebuilt = pd.concat([frame for _, frame in sorted(pieces, key=lambda value: value[0])], ignore_index=True)
    rebuilt = rebuilt.drop_duplicates(["date", "symbol"], keep="last")
    rebuilt = canonicalize_bars(rebuilt.loc[rebuilt["date"].between(start, end)])
    TradingCalendar.from_benchmark_bars(rebuilt)
    return rebuilt


def _archive_benchmark_records(run, records):
    target = run / "evidence" / "benchmark-native"
    target.mkdir(parents=True, exist_ok=True)
    index = []
    for offset, record in enumerate(records, 1):
        receipt_path, body_path = target / ("%04d.request.json" % offset), target / ("%04d.bin" % offset)
        write_new(receipt_path, record["receipt_bytes"])
        write_new(body_path, record["body"])
        index.append({"receipt": _item(run, receipt_path), "response": _item(run, body_path)})
    write_new(run / "benchmark_sources.json", _json(index))


def _read_archived_benchmark_records(root, manifest):
    if "benchmark_sources.json" not in manifest["files"]:
        raise ValueError("V3 benchmark native evidence index is required")
    index = json.loads((root / "benchmark_sources.json").read_bytes())
    records = []
    for item in index:
        paths = {}
        for role in ("receipt", "response"):
            relative = Path(item[role]["path"])
            if relative.is_absolute() or ".." in relative.parts or relative.as_posix() not in manifest["files"]:
                raise ValueError("benchmark native evidence path/hash binding missing")
            paths[role] = safe_path(root / relative, root)
            if _item(root, paths[role]) != item[role] or manifest["files"][relative.as_posix()] != item[role]:
                raise ValueError("benchmark native evidence index hash mismatch")
        receipt_bytes = paths["receipt"].read_bytes()
        records.append({"entry": json.loads(receipt_bytes), "receipt_bytes": receipt_bytes, "body": paths["response"].read_bytes()})
    return records


def acquire_pilot(root, history_start="2021-03-08", research_start="2021-09-13",
                  research_end="2026-09-11", benchmark_cache_root=None,
                  akshare_module=None, progress=print, synthetic_only=False,
                  timeout_seconds=10.0, retry_count=0, lock_timeout_seconds=0.0,
                  benchmark_evidence_root=None):
    """Serialize a whole pilot acquisition before allocating evidence names.

    The existing ParquetBarCache OS writer lock protects RecordingAKShare's
    source sequence as well as cache snapshots and immutable run publication.
    A competing process fails explicitly by default, before constructing its
    recorder, creating a run or issuing HTTP. The permanent lock file is never
    removed; the OS releases ownership when this context or process exits.
    """
    root = safe_path(Path(root))
    lock_cache = ParquetBarCache(root / "cache", lock_timeout_seconds=lock_timeout_seconds)
    with lock_cache._writer_lock(PILOT_LOCK_SYMBOL):
        return _acquire_pilot_locked(
            root, history_start=history_start, research_start=research_start,
            research_end=research_end, benchmark_cache_root=benchmark_cache_root,
            akshare_module=akshare_module, progress=progress, synthetic_only=synthetic_only,
            timeout_seconds=timeout_seconds, retry_count=retry_count,
            benchmark_evidence_root=benchmark_evidence_root)


def _acquire_pilot_locked(root, history_start="2021-03-08", research_start="2021-09-13",
                          research_end="2026-09-11", benchmark_cache_root=None,
                          akshare_module=None, progress=print, synthetic_only=False,
                          timeout_seconds=10.0, retry_count=0, benchmark_evidence_root=None):
    """Bounded single-source RAW/HFQ pilot; failures stay in the frozen sample.

    Every invocation publishes a new manifest and independent receipt. Successful
    requests resume via the existing atomic cache; native supplier response bytes
    resume via RecordingAKShare. No factor search or formal/Forward calls occur.
    """
    began = time.perf_counter()
    history, start, end = (_day(value) for value in (history_start, research_start, research_end))
    today = pd.Timestamp(utc_now()).tz_convert("Asia/Shanghai").normalize().tz_localize(None)
    if not history <= start <= end <= today:
        raise ValueError("history/research date range is invalid or in the future")
    if type(retry_count) is not int or retry_count not in {0, 1}:
        raise ValueError("pilot permits zero or one retry")
    root = safe_path(Path(root))
    sample = freeze_pilot(root, synthetic_only=synthetic_only)
    run = safe_path(root / "runs" / (pd.Timestamp(utc_now()).strftime("%Y%m%dT%H%M%S%fZ") + "-" + uuid.uuid4().hex[:8]), root)
    run.mkdir(parents=True, exist_ok=False)
    _copy_bound(root / "sample.json", run / "sample.json")
    attempts, histories, cache_snapshots = [], [], []
    cache = ParquetBarCache(root / "cache")
    benchmark, calendar, recorder, source_version = empty_bars(), None, None, None
    benchmark_records = []
    fatal = None
    try:
        if akshare_module is None:
            import akshare as akshare_module
        recorder = _AccessLimitedRecorder(akshare_module, root)
        provider = AKShareProvider(adjustment="hfq", retry_count=retry_count,
                                   timeout_seconds=timeout_seconds, request_interval=0.5,
                                   akshare_module=recorder)
        source_version = provider.cache_source_version(sample["symbols"][0], False)
        for symbol in sample["symbols"]:
            progress("FIVE_YEAR_PILOT %s RAW+HFQ" % symbol)
            started, request_offset = time.perf_counter(), len(recorder.requests)
            was_access_limited = recorder.access_limit is not None
            try:
                if recorder.access_limit is not None:
                    raise _SupplierAccessLimit("not attempted: supplier access limit recorded earlier in this run")
                cache.update_from_provider(provider, symbol, history, end)
                snapshot = cache.load_snapshot(symbol, expected_source="AKShareProvider",
                                               expected_source_version=source_version)
                frame = snapshot.frame.loc[snapshot.frame["date"].between(history, end)].copy()
                # No stripping of unavailable names or short/new-listing histories.
                if not set(CANONICAL_BAR_COLUMNS).issubset(frame):
                    raise ValueError("explicit RAW and HFQ columns required")
                frame = canonicalize_bars(frame, symbol=symbol, allow_missing_ohlc=True)
                histories.append(frame)
                cache_snapshots.append((symbol, snapshot, cache))
                state = "ACQUIRED" if not frame.empty else "EMPTY_VENDOR_RESPONSE"
                attempt = {"symbol": symbol, "status": state, "observed_rows": len(frame),
                           "generation_id": snapshot.metadata.get("generation_id"),
                           "cache_data_sha256": snapshot.metadata.get("content_sha256"),
                           "cache_metadata_sha256": snapshot.metadata_sha256}
            except Exception as exc:
                attempt = {"symbol": symbol, "status": "NOT_ATTEMPTED_ACCESS_LIMIT" if was_access_limited else "DOWNLOAD_OR_VALIDATION_FAILED",
                           "reason": "%s: %s" % (type(exc).__name__, exc),
                           "observed_rows": 0}
                if recorder.access_limit is not None:
                    attempt["supplier_access_limit"] = dict(recorder.access_limit)
            attempt.update(requested_start=history_start, requested_end=research_end,
                           attempted_at=utc_now(), elapsed_seconds=time.perf_counter() - started,
                           native_http_responses=len(recorder.requests) - request_offset,
                           retained_in_frozen_sample=True)
            attempts.append(attempt)
            write_new(run / ("attempt-%02d.json" % len(attempts)), _json(attempt))
        progress("FIVE_YEAR_PILOT real CSI500 price-index calendar")
        try:
            index_version = provider.cache_source_version("000905.SH", True)
            index_cache = cache if benchmark_cache_root is None else ParquetBarCache(Path(benchmark_cache_root))
            if benchmark_cache_root is None:
                index_cache.update_from_provider(provider, "000905.SH", history, end, is_index=True)
            snapshot = index_cache.load_snapshot("000905.SH", expected_source="AKShareProvider",
                                                 expected_source_version=index_version)
            benchmark = snapshot.frame.loc[snapshot.frame["date"].between(history, end)].copy()
            roots = [root] + ([] if benchmark_evidence_root is None else [benchmark_evidence_root])
            benchmark_records = _benchmark_records(roots)
            rebuilt = _reconstruct_benchmark(benchmark_records, history, end, utc_now(), synthetic_only)
            pd.testing.assert_frame_equal(benchmark[list(CANONICAL_BAR_COLUMNS)].reset_index(drop=True),
                                          rebuilt[list(CANONICAL_BAR_COLUMNS)].reset_index(drop=True),
                                          check_dtype=False, check_exact=True)
            calendar = TradingCalendar.from_benchmark_bars(benchmark)
            if not len(calendar.sessions(start, end)):
                raise ValueError("benchmark has no actual research sessions")
            cache_snapshots.append(("000905.SH", snapshot, index_cache))
            attempts.append({"symbol": "000905.SH", "status": "BENCHMARK_ACQUIRED",
                             "observed_rows": len(benchmark), "source_version": index_version,
                             "upstream_retrieved_at": snapshot.metadata.get("updated_at"),
                             "cache_metadata_sha256": snapshot.metadata_sha256})
        except Exception as exc:
            benchmark, calendar = empty_bars(), None
            attempts.append({"symbol": "000905.SH", "status": "BENCHMARK_FAILED",
                             "reason": "%s: %s" % (type(exc).__name__, exc)})
    except Exception as exc:
        fatal = "%s: %s" % (type(exc).__name__, exc)
        for symbol in sample["symbols"]:
            if any(item["symbol"] == symbol for item in attempts):
                continue
            attempt = {"symbol": symbol, "status": "DOWNLOAD_OR_VALIDATION_FAILED",
                       "reason": "acquisition could not start: " + fatal,
                       "observed_rows": 0, "requested_start": history_start,
                       "requested_end": research_end, "attempted_at": utc_now(),
                       "elapsed_seconds": 0.0, "native_http_responses": 0,
                       "retained_in_frozen_sample": True}
            attempts.append(attempt)
            write_new(run / ("attempt-%02d.json" % len(attempts)), _json(attempt))

    observed = canonicalize_bars(pd.concat(histories, ignore_index=True), allow_missing_ohlc=True) if histories else empty_bars()
    coverage, annual = [], []
    frame = observed
    software_status = "SOFTWARE_SUCCESS" if fatal is None else "SOFTWARE_FAILED"
    calendar_status = "ACTUAL_BENCHMARK_CALENDAR_AVAILABLE" if calendar is not None else "CALENDAR_UNAVAILABLE"
    if calendar is not None:
        try:
            if not observed["date"].isin(calendar.dates).all():
                raise ValueError("stock observations outside declared actual calendar")
            frame = reindex_bars_to_calendar(observed, calendar, sample["symbols"])
            frame["observation_status"] = np.where(frame["raw_close"].notna(), "OBSERVED_VENDOR_BAR", "MISSING_REASON_UNKNOWN")
            frame["historical_security_state"] = "UNVERIFIED"
            frame["source_version"] = source_version
            frame["calculation_version"] = VERSION
            frame["hfq_qualification"] = "HFQ_UNVERIFIED"
            validation_manifest = {"research_start": research_start, "research_end": research_end,
                                   "retrieved_at": utc_now(), "warmup_min_sessions": 0}
            # Pilot validation checks fields/calendar/units independently from
            # factor eligibility. The required 130-session qualification is
            # reported below and never waived in the existing dataset contract.
            if not observed.empty:
                validate_dataset_frames(frame, benchmark, calendar.dates, validation_manifest, sample)
            coverage, annual = _coverage(frame, calendar.dates, sample["symbols"], start, end)
        except Exception as exc:
            software_status = "SOFTWARE_FAILED"
            fatal = "%s: %s" % (type(exc).__name__, exc)
            # Keep observed evidence but issue no admitted analysis-ready panel.
            frame = observed
    _parquet(run / "bars.parquet", frame)
    _parquet(run / "benchmark.parquet", benchmark)
    _csv(run / "calendar.csv", pd.DataFrame({"date": [] if calendar is None else calendar.dates.strftime("%Y-%m-%d")}))
    _csv(run / "annual_coverage.csv", pd.DataFrame(annual))
    write_new(run / "coverage.json", _json(coverage))
    write_new(run / "attempts.json", _json(attempts))
    _archive_benchmark_records(run, benchmark_records)
    for symbol, snapshot, snapshot_cache in cache_snapshots:
        _copy_cache(snapshot, snapshot_cache, symbol, run)
    if recorder is not None:
        for relative in sorted(set(recorder.artifacts)):
            source = safe_path(root / relative, root)
            _copy_bound(source, run / "evidence" / relative)
    available = int(sum(item.get("paired_observed_research_sessions", 0) > 0 for item in coverage))
    status = {"software": software_status,
              "data": "EXPLORATORY_DATA_AVAILABLE" if available else "EXPLORATORY_DATA_UNAVAILABLE",
              "pit": "PIT_UNVERIFIED", "hfq": "HFQ_UNVERIFIED",
              "formal": "FORMAL_ADMISSION_BLOCKED"}
    required_calendar_warmup = 0 if calendar is None else int((calendar.dates < start).sum())
    terminal = [] if calendar is None else [day.strftime("%Y-%m-%d") for day in calendar.sessions(start, end)[-6:]]
    files = {path.relative_to(run).as_posix(): _item(run, path)
             for path in sorted(run.rglob("*")) if path.is_file()}
    elapsed = time.perf_counter() - began
    metrics = {"elapsed_seconds": elapsed, "observed_rows": len(observed), "panel_rows": len(frame),
               "frame_deep_memory_bytes": int(frame.memory_usage(deep=True).sum()),
               "process_rss_bytes": None, "process_peak_rss_bytes": None,
               "successful_research_securities": available,
               "requested_securities": len(sample["symbols"]),
               "failed_security_requests": sum(item["status"] == "DOWNLOAD_OR_VALIDATION_FAILED" for item in attempts),
               "not_attempted_access_limit": sum(item["status"] == "NOT_ATTEMPTED_ACCESS_LIMIT" for item in attempts),
               "native_http_responses_this_run": 0 if recorder is None else len(recorder.requests),
               "snapshot_bytes": sum(item["bytes"] for item in files.values()),
               "estimated_500x1200_numeric_18_columns_bytes": 500 * 1200 * 18 * 8,
               "estimate_scope": "Numeric base only; excludes strings, copies, candidate matrices, sources, old generations and Python overhead"}
    try:
        import psutil

        memory = psutil.Process().memory_info()
        metrics["process_rss_bytes"] = memory.rss
        metrics["process_peak_rss_bytes"] = getattr(memory, "peak_wset", None)
        metrics["memory_method"] = "psutil.memory_info rss/Windows peak_wset in bytes"
    except ImportError:
        metrics.update(_stdlib_memory())
    manifest = {"schema": "FIVE_YEAR_PILOT_MANIFEST_V3", "version": VERSION,
                "purpose": "EXPLORATORY", "qualification": "UNVERIFIED",
                "formal_admission": False, "synthetic_only": synthetic_only,
                "historical_status": "HISTORICALLY_RECONSTRUCTED", "no_live_money": True,
                "statuses": status, "research_start": research_start, "research_end": research_end,
                "history_start": history_start, "retrieved_at": utc_now(),
                "sample_sha256": sha256_file(run / "sample.json"), "files": files,
                "source_version": source_version, "units": UNITS,
                "supplier_access_limit": None if recorder is None else recorder.access_limit,
                "price_basis": {"canonical": "RAW", "raw": "RAW", "adj": "HFQ"},
                "calendar_status": calendar_status, "upstream_vintage": "UNKNOWN_UNVERIFIED",
                "benchmark_provenance": {"status": "NATIVE_RESPONSE_RECONSTRUCTED" if calendar is not None else "UNAVAILABLE",
                                         "evidence_index": "benchmark_sources.json", "supplier": "CHINA_SECURITIES_INDEX_COMPANY",
                                         "interface": "stock_zh_index_hist_csindex", "source_contract": "CSI_INDEX_PERF_16_FIELDS_V1",
                                         "native_response_sha256": [sha256(record["body"]) for record in benchmark_records],
                                         "units_status": "PINNED_PROVIDER_CONVERSION_NOT_INDEPENDENT_FINANCIAL_CERTIFICATION",
                                         "calendar_scope": CALENDAR_SCOPE},
                "source_contract": {"stock_vendor": "EASTMONEY", "capture": "RecordingAKShare native HTTP response bytes",
                                    "vendor_units": {"price": "CNY", "volume": "HANDS_100_SHARES", "amount": "CNY", "turnover": "PERCENT"},
                                    "benchmark_basis": "CSI500_PRICE_INDEX_UNADJUSTED",
                                    "benchmark_volume": "Provider resolves scale then checks quantity plausibility; not independent certification",
                                    "adj_factor": "adj_close/raw_close schema ratio only; not an HFQ algorithm",
                                    "missing_reason": "UNKNOWN_UNVERIFIED; never inferred as suspension or pre/post listing",
                                    "revision_history": "UNKNOWN_UNVERIFIED",
                                    "request_policy": "401/403/429: preserve native bytes/receipt, stop new HTTP for this run, no immediate retry or alternate proxy",
                                    "acquisition_concurrency": "Whole pilot root serialized with existing ParquetBarCache OS writer lock before Recorder allocation; competing entry explicitly fails by default",
                                    "intended_use": "LOCAL_EXPLORATORY_ONLY_NO_REDISTRIBUTION",
                                    "authorization_evidence": "PUBLIC_ENDPOINT_ACCESS_ONLY; no commercial/redistribution grant established",
                                    "cache_metadata_representation": "ORIGINAL_BYTES_MATCH_SNAPSHOT_METADATA_SHA256"},
                "warmup": {"required_actual_sessions": 130, "available_calendar_sessions": required_calendar_warmup,
                           "calendar_sufficient": required_calendar_warmup >= 130,
                           "per_security": "coverage.json; short histories retained, no invented pre-listing prices"},
                "terminal_labels": {"entry_t_plus": 1, "exit_t_plus": 6, "prices_after_research_end_fetched": False,
                                    "unmatured_calendar_dates": terminal, "status": "TERMINAL_LABELS_UNAVAILABLE",
                                    "readiness_only_no_labels_evaluated": True},
                "verification_design": {"factor_search_performed": False, "factor_selection_performed": False,
                                        "factor_direction_changed": False, "existing_2025_2026_independent_test": False,
                                        "added_earlier_dates_are_forward_test": False,
                                        "future_train_validation_test_split": "MUST_FREEZE_BEFORE_SEARCH_OR_MODEL_SELECTION"},
                "blockers": ["FIVE_YEAR_ACTUAL_PIT_MEMBERSHIP", "HISTORICAL_IDENTITY_AND_SECURITY_STATE",
                             "COMPLETE_CORPORATE_ACTION_REVISIONS", "INDEPENDENT_HFQ_ALGORITHM_ANCHOR_ROUNDING", "CONTEMPORANEOUS_VINTAGES"],
                "metrics": metrics, "fatal_error": fatal,
                "implementation_sha256": implementation_sha256([__file__]),
                "git_commit": git_commit(Path(__file__).resolve().parents[3]),
                "environment": {"python": sys.version, "platform": platform.platform(),
                                "packages": package_versions(("numpy", "pandas", "pyarrow", "akshare"))}}
    manifest["data_version"] = manifest_version(manifest)
    manifest_path = run / "manifest.json"
    write_new(manifest_path, _json(manifest))
    receipt = {"schema": "FIVE_YEAR_PILOT_RECEIPT_V3", "purpose": "EXPLORATORY",
               "qualification": "UNVERIFIED", "formal_admission": False,
               "manifest_sha256": sha256_file(manifest_path), "data_version": manifest["data_version"],
               "artifact_hashes": {name: item["sha256"] for name, item in files.items()},
               "statuses": status, "created_at": utc_now(), "synthetic_only": synthetic_only}
    receipt_path = run / "receipt.json"
    write_new(receipt_path, _json(receipt))
    verify_pilot(manifest_path, allow_synthetic=synthetic_only)
    return {"manifest_path": str(manifest_path), "receipt_path": str(receipt_path),
            "statuses": status, "metrics": metrics, "coverage": coverage}


def _instant(value):
    result = pd.Timestamp(value)
    if pd.isna(result) or result.tzinfo is None:
        raise ValueError("timezone-aware evidence timestamp required")
    return result


def _verify_native_sources(root, manifest, sample):
    """Bind native response bytes to requests; no supplier algorithm certification."""
    from urllib.parse import parse_qs, urlsplit
    from factorlab.data.providers.akshare import AKShareProvider as CanonicalAKShareProvider

    quotes = {}
    frozen, retrieved = _instant(sample["frozen_at"]), _instant(manifest["retrieved_at"])
    attempts = json.loads((root / "attempts.json").read_text(encoding="utf-8"))
    rejected = {item["symbol"] for item in attempts if item["status"] == "DOWNLOAD_OR_VALIDATION_FAILED"}
    empty_responses = {item["symbol"] for item in attempts if item["status"] == "EMPTY_VENDOR_RESPONSE"}
    for name in sorted(manifest["files"]):
        if not name.startswith("evidence/sources/") or not name.endswith(".request.json"):
            continue
        entry = json.loads((root / name).read_text(encoding="utf-8"))
        relative = Path(entry["response_file"])
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("native response path escapes evidence snapshot")
        response_path = safe_path(root / "evidence" / relative, root)
        response_name = response_path.relative_to(root).as_posix()
        if response_name not in manifest["files"] or sha256_file(response_path) != entry["response_sha256"]:
            raise ValueError("native source receipt/response hash mismatch")
        acquired = _instant(entry["retrieved_at"])
        requested = _instant(entry.get("requested_at", entry["retrieved_at"]))
        stock_interface = entry.get("interface") == "stock_zh_a_hist"
        if not requested <= acquired <= retrieved or (stock_interface and requested < frozen):
            raise ValueError("native source acquisition timestamp predates freeze or exceeds snapshot")
        if entry.get("upstream_vintage") != "UNKNOWN_UNVERIFIED":
            raise ValueError("native receipt cannot assert an unproved upstream vintage")
        if not isinstance(entry.get("status_code"), int) or isinstance(entry["status_code"], bool):
            raise ValueError("explicit native HTTP response status required")
        if str(entry.get("method")).upper() not in {"GET", "POST"}:
            raise ValueError("explicit native request method required")
        arguments = entry.get("interface_arguments", {})
        if entry.get("interface") != "stock_zh_a_hist":
            if entry.get("interface") not in {"stock_zh_index_hist_csindex", "index_zh_a_hist", "stock_zh_index_daily"}:
                raise ValueError("unexpected source interface in isolated pilot")
            if arguments.get("symbol") not in {"000905", "sh000905"}:
                raise ValueError("benchmark source request must bind CSI500")
            continue
        code, adjustment = arguments.get("symbol"), arguments.get("adjust")
        if not isinstance(code, str) or len(code) != 6 or not code.isdigit():
            raise ValueError("native source request requires an explicit six-digit code")
        symbol = normalize_symbol(code)
        if symbol not in sample["symbols"] or adjustment not in {"", "hfq"} or arguments.get("period") != "daily":
            raise ValueError("native source request outside frozen sample or RAW/HFQ daily roles")
        begin, end = arguments.get("start_date"), arguments.get("end_date")
        if any(not isinstance(value, str) or len(value) != 8 or not value.isdigit() for value in (begin, end)):
            raise ValueError("native source request dates must be YYYYMMDD")
        begin_date, end_date = (pd.to_datetime(value, format="%Y%m%d", errors="raise") for value in (begin, end))
        if begin_date > end_date:
            raise ValueError("native source request dates reversed")
        parsed = urlsplit(entry["url"])
        query = parse_qs(parsed.query, keep_blank_values=True)
        synthetic = manifest["synthetic_only"]
        if synthetic:
            if parsed.scheme != "https" or parsed.netloc != "example.invalid" or parsed.path != "/pilot-stock":
                raise ValueError("synthetic native source must remain an explicit local test fixture")
            expected = {key: [str(value)] for key, value in arguments.items()}
            if query != expected:
                raise ValueError("synthetic native request URL/arguments mismatch")
        else:
            expected = {"secid": [("1." if symbol.endswith(".SH") else "0.") + code],
                        "fqt": ["2" if adjustment == "hfq" else "0"], "klt": ["101"],
                        "beg": [begin], "end": [end]}
            if (parsed.scheme != "https" or parsed.netloc != "push2his.eastmoney.com"
                    or parsed.path != "/api/qt/stock/kline/get" or parsed.fragment
                    or any(query.get(key) != values for key, values in expected.items())
                    or str(entry["method"]).upper() != "GET"):
                raise ValueError("native request URL must bind supplier/code/date/RAW-HFQ arguments")
        if entry["status_code"] != 200:
            continue  # Keep failed HTTP response bytes as failure evidence.
        payload = json.loads(response_path.read_bytes())
        if synthetic:
            if payload.get("synthetic_only") is not True:
                raise ValueError("synthetic HTTP body classification mismatch")
            vendor = pd.DataFrame(payload["rows"])
        else:
            data = payload.get("data")
            if data is None:
                if payload.get("rc") == 0 and symbol in empty_responses:
                    # The real SDK returns an empty table for HTTP 200 data:null.
                    # Bind the explicit empty RAW/HFQ response as evidence, then
                    # require the corresponding cached series to be empty below.
                    empty = CanonicalAKShareProvider._normalise_vendor_frame(
                        pd.DataFrame(), begin_date, end_date, require_turnover=adjustment == "",
                        require_amount=adjustment == "", symbol=symbol)
                    quotes.setdefault((symbol, adjustment), []).append((acquired, empty))
                    continue
                if symbol not in rejected:
                    raise ValueError("acquired native source has no supplier security data")
                continue
            if (payload.get("rc") != 0 or data.get("code") != code
                    or data.get("market") != (1 if symbol.endswith(".SH") else 0)):
                raise ValueError("native response code/market/status differs from requested security")
            rows = [row.split(",") for row in data.get("klines", [])]
            if any(len(row) != 11 for row in rows):
                raise ValueError("unexpected native Eastmoney eleven-field layout")
            vendor = pd.DataFrame(rows, columns=("date", "open", "close", "high", "low", "volume",
                                                 "amount", "amplitude", "pct_change", "price_change", "turnover"))
        try:
            normalized = CanonicalAKShareProvider._normalise_vendor_frame(
                vendor, begin_date, end_date, require_turnover=adjustment == "",
                require_amount=adjustment == "", symbol=symbol)
        except Exception:
            if symbol in rejected:
                continue  # Invalid supplier data was retained and explicitly rejected.
            raise ValueError("admitted cached rows reference invalid native source data")
        quotes.setdefault((symbol, adjustment), []).append((acquired, normalized))
    return quotes


def _verify_structured_snapshot(root, manifest, sample, legacy):
    """Recompute coverage and provenance relationships from this one snapshot."""
    frozen, retrieved = _instant(sample["frozen_at"]), _instant(manifest["retrieved_at"])
    selection = _day(sample["selection_date"])
    history, start, end = (_day(manifest[key]) for key in ("history_start", "research_start", "research_end"))
    if (not history <= start <= end <= retrieved.tz_convert("Asia/Shanghai").normalize().tz_localize(None)
            or frozen > retrieved or selection > frozen.tz_convert("Asia/Shanghai").normalize().tz_localize(None)):
        raise ValueError("frozen sample/date/acquisition ordering is inconsistent")
    if (sample.get("selection_method") != SELECTION_METHOD
            or sample.get("synthetic_only") is not manifest["synthetic_only"]
            or sample.get("classification") != "NON_PIT_CONVENIENCE"
            or len(sample["symbols"]) != len(set(sample["symbols"]))
            or any(normalize_symbol(symbol) != symbol for symbol in sample["symbols"])):
        raise ValueError("frozen sample classification or canonical identity inconsistent")
    if manifest.get("sample_sha256") != sha256_file(root / "sample.json"):
        raise ValueError("frozen sample hash mismatch")
    for item in sample["securities"]:
        if any(item.get(key) != "UNVERIFIED" for key in ("identity_status", "listing_status", "historical_membership")):
            raise ValueError("frozen engineering strata cannot certify historical identity or state")
    bars, benchmark = (pd.read_parquet(root / name) for name in ("bars.parquet", "benchmark.parquet"))
    validate_bars(bars, allow_missing_ohlc=True)
    validate_bars(benchmark, allow_missing_ohlc=True)
    if any(not frame["date"].equals(frame["date"].dt.normalize()) for frame in (bars, benchmark)):
        raise ValueError("snapshot bars require timezone-naive midnight trading dates")
    if not set(bars["symbol"]).issubset(sample["symbols"]) or not bars["date"].between(history, end).all():
        raise ValueError("snapshot bars outside frozen symbols or requested dates")
    attempts = json.loads((root / "attempts.json").read_text(encoding="utf-8"))
    securities = [item for item in attempts if item["symbol"] != "000905.SH"]
    if len(securities) != len(sample["symbols"]) or [item["symbol"] for item in securities] != sample["symbols"]:
        raise ValueError("download attempts must retain every frozen security in order")
    for offset, item in enumerate(securities, 1):
        if "attempt-%02d.json" % offset not in manifest["files"]:
            raise ValueError("independent attempt receipt missing from artifact binding")
        if (item.get("retained_in_frozen_sample") is not True
                or item.get("requested_start") != manifest["history_start"]
                or item.get("requested_end") != manifest["research_end"]
                or item != json.loads((root / ("attempt-%02d.json" % offset)).read_text(encoding="utf-8"))):
            raise ValueError("independent attempt receipt or retained sample semantics mismatch")
    for item in attempts:
        if item["status"] in {"ACQUIRED", "EMPTY_VENDOR_RESPONSE", "BENCHMARK_ACQUIRED"}:
            for suffix in (".parquet", ".metadata.json"):
                if "evidence/cache/" + item["symbol"] + suffix not in manifest["files"]:
                    raise ValueError("acquired security lacks independently bound cache evidence")
    limit = manifest.get("supplier_access_limit")
    limited = [item for item in securities if item["status"] == "NOT_ATTEMPTED_ACCESS_LIMIT"]
    if limit is None and limited:
        raise ValueError("unattempted securities require an explicit supplier access-limit receipt")
    if limit is not None:
        if limit.get("status_code") not in {401, 403, 429} or limit.get("policy") != "STOP_NEW_HTTP_THIS_RUN_NO_IMMEDIATE_RETRY_NO_PROXY":
            raise ValueError("supplier access-limit status or stop policy inconsistent")
        _instant(limit["detected_at"])
        if limit.get("interface") == "stock_zh_a_hist":
            symbol = normalize_symbol(limit["interface_arguments"]["symbol"])
            denied_offset = sample["symbols"].index(symbol)
            if (securities[denied_offset]["status"] != "DOWNLOAD_OR_VALIDATION_FAILED"
                    or securities[denied_offset].get("supplier_access_limit") != limit):
                raise ValueError("denied supplier response must retain its failed security receipt")
            for item in securities[denied_offset + 1:]:
                if (item["status"] != "NOT_ATTEMPTED_ACCESS_LIMIT" or item.get("native_http_responses") != 0
                        or item.get("supplier_access_limit") != limit):
                    raise ValueError("supplier denial must stop subsequent requests without replacing frozen securities")
        bound_denial = False
        for name in manifest["files"]:
            if name.startswith("evidence/sources/") and name.endswith(".request.json"):
                native = json.loads((root / name).read_text(encoding="utf-8"))
                bound_denial |= native.get("status_code") == limit["status_code"] and native.get("url") == limit["url"]
        if not bound_denial:
            raise ValueError("supplier stop policy lacks a bound native denied response")
    calendar_frame = pd.read_csv(root / "calendar.csv")
    if "date" not in calendar_frame:
        raise ValueError("actual calendar date column required")
    actual_dates = pd.DatetimeIndex(pd.to_datetime(calendar_frame["date"], errors="raise"))
    if actual_dates.tz is not None or not actual_dates.equals(actual_dates.normalize()):
        raise ValueError("actual calendar requires timezone-naive midnight dates")
    calendar = TradingCalendar(actual_dates)
    if manifest["schema"] == "FIVE_YEAR_PILOT_MANIFEST_V3":
        native_records = _read_archived_benchmark_records(root, manifest)
        provenance = manifest.get("benchmark_provenance", {})
        expected_status = "NATIVE_RESPONSE_RECONSTRUCTED" if len(calendar) else "UNAVAILABLE"
        if (provenance.get("status") != expected_status or provenance.get("evidence_index") != "benchmark_sources.json"
                or provenance.get("supplier") != "CHINA_SECURITIES_INDEX_COMPANY"
                or provenance.get("interface") != "stock_zh_index_hist_csindex"
                or provenance.get("source_contract") != "CSI_INDEX_PERF_16_FIELDS_V1"
                or provenance.get("units_status") != "PINNED_PROVIDER_CONVERSION_NOT_INDEPENDENT_FINANCIAL_CERTIFICATION"
                or provenance.get("calendar_scope") != CALENDAR_SCOPE
                or manifest.get("calendar_status") != ("ACTUAL_BENCHMARK_CALENDAR_AVAILABLE" if len(calendar) else "CALENDAR_UNAVAILABLE")
                or provenance.get("native_response_sha256") != [sha256(item["body"]) for item in native_records]):
            raise ValueError("benchmark native provenance/units/status binding mismatch")
        if len(calendar):
            rebuilt = _reconstruct_benchmark(native_records, history, end, manifest["retrieved_at"], manifest["synthetic_only"])
            pd.testing.assert_frame_equal(benchmark[list(CANONICAL_BAR_COLUMNS)].reset_index(drop=True),
                                          rebuilt[list(CANONICAL_BAR_COLUMNS)].reset_index(drop=True),
                                          check_dtype=False, check_exact=True)
    good = manifest["statuses"]["software"] == "SOFTWARE_SUCCESS"
    if manifest["statuses"]["software"] not in {"SOFTWARE_SUCCESS", "SOFTWARE_FAILED"}:
        raise ValueError("unknown software status")
    if good and manifest.get("fatal_error") is not None:
        raise ValueError("software success cannot hide a fatal validation error")
    coverage = json.loads((root / "coverage.json").read_text(encoding="utf-8"))
    if good and len(calendar):
        expected_calendar = TradingCalendar.from_benchmark_bars(benchmark)
        if not calendar.dates.equals(expected_calendar.dates):
            raise ValueError("snapshot calendar differs from actual benchmark sessions")
        if (manifest.get("calendar_status") != "ACTUAL_BENCHMARK_CALENDAR_AVAILABLE"
                or not set(bars["symbol"]) == set(sample["symbols"])):
            raise ValueError("calendar/panel must retain every frozen security")
        grid = pd.MultiIndex.from_product([calendar.dates, sorted(sample["symbols"])], names=["date", "symbol"])
        if not pd.MultiIndex.from_frame(bars[["date", "symbol"]]).equals(grid):
            raise ValueError("panel grid differs from actual calendar and frozen securities")
        observed = bars["raw_close"].notna()
        if not bars["observation_status"].equals(pd.Series(np.where(observed, "OBSERVED_VENDOR_BAR", "MISSING_REASON_UNKNOWN"), index=bars.index, name="observation_status")):
            raise ValueError("missing observations cannot be relabelled as known suspension or filled prices")
        for key, value in (("historical_security_state", "UNVERIFIED"), ("hfq_qualification", "HFQ_UNVERIFIED"),
                           ("source_version", manifest["source_version"]), ("calculation_version", manifest["version"])):
            if not bars[key].eq(value).all():
                raise ValueError("panel state/source/calculation semantics mismatch: " + key)
        if observed.any():
            validate_dataset_frames(bars, benchmark, calendar.dates,
                                    {"research_start": manifest["research_start"], "research_end": manifest["research_end"],
                                     "retrieved_at": manifest["retrieved_at"], "warmup_min_sessions": 0}, sample)
        expected_coverage, annual = _coverage(bars, calendar.dates, sample["symbols"], start, end)
        if coverage != expected_coverage:
            raise ValueError("reported coverage differs from observed RAW/HFQ sessions")
        pd.testing.assert_frame_equal(pd.read_csv(root / "annual_coverage.csv"), pd.DataFrame(annual), check_dtype=False, check_exact=False, rtol=1e-14)
    elif coverage:
        raise ValueError("failed/unavailable panel cannot assert verified coverage")
    available = sum(item["paired_observed_research_sessions"] > 0 for item in coverage)
    expected_data = "EXPLORATORY_DATA_AVAILABLE" if available else "EXPLORATORY_DATA_UNAVAILABLE"
    if manifest["statuses"]["data"] != expected_data:
        raise ValueError("exploratory availability status differs from measured coverage")
    metrics = manifest["metrics"]
    if (metrics.get("panel_rows") != len(bars)
            or metrics.get("observed_rows") != int(bars["raw_close"].notna().sum())
            or metrics.get("requested_securities") != len(sample["symbols"])
            or metrics.get("successful_research_securities") != available):
        raise ValueError("reported row/security metrics differ from immutable snapshot")
    pre_sessions = int((calendar.dates < start).sum())
    if (manifest["warmup"]["available_calendar_sessions"] != pre_sessions
            or manifest["warmup"]["required_actual_sessions"] != 130
            or manifest["warmup"]["calendar_sufficient"] is not (pre_sessions >= 130)):
        raise ValueError("warmup qualification differs from actual sessions")
    terminal = [day.strftime("%Y-%m-%d") for day in calendar.sessions(start, end)[-6:]] if len(calendar) else []
    labels = manifest["terminal_labels"]
    if (labels.get("unmatured_calendar_dates") != terminal or labels.get("entry_t_plus") != 1
            or labels.get("exit_t_plus") != 6 or labels.get("prices_after_research_end_fetched") is not False
            or labels.get("readiness_only_no_labels_evaluated") is not True):
        raise ValueError("terminal t+6 label maturity semantics mismatch")
    design = manifest["verification_design"]
    if any(design.get(key) is not False for key in ("factor_search_performed", "factor_selection_performed", "factor_direction_changed",
                                                  "existing_2025_2026_independent_test", "added_earlier_dates_are_forward_test")):
        raise ValueError("pilot cannot search/select factors or claim an independent forward test")
    if manifest.get("units") != UNITS or manifest.get("price_basis") != {"canonical": "RAW", "raw": "RAW", "adj": "HFQ"}:
        raise ValueError("RAW/HFQ price role or canonical units mismatch")
    quotes = _verify_native_sources(root, manifest, sample)
    for name in manifest["files"]:
        if not name.startswith("evidence/cache/") or not name.endswith(".metadata.json"):
            continue
        metadata = json.loads((root / name).read_text(encoding="utf-8"))
        symbol = metadata["symbol"]
        data_path = root / "evidence" / "cache" / (symbol + ".parquet")
        if sha256_file(data_path) != metadata["content_sha256"]:
            raise ValueError("cache metadata/content snapshot hash mismatch")
        cached = pd.read_parquet(data_path)
        validate_bars(cached, allow_missing_ohlc=True)
        if len(cached) != metadata["row_count"] or set(cached["symbol"]) - {symbol}:
            raise ValueError("cache snapshot row count or security identity mismatch")
        actual_bounds = (None, None) if cached.empty else (cached["date"].min().strftime("%Y-%m-%d"), cached["date"].max().strftime("%Y-%m-%d"))
        if actual_bounds != (metadata.get("data_start"), metadata.get("data_end")):
            raise ValueError("cache snapshot data bounds mismatch")
        attempt = next(item for item in attempts if item["symbol"] == symbol)
        if not legacy and sha256_file(root / name) != attempt["cache_metadata_sha256"]:
            raise ValueError("cache metadata original bytes do not match captured snapshot SHA")
        if symbol == "000905.SH":
            if metadata.get("source") != "AKShareProvider" or metadata.get("source_version") != attempt.get("source_version"):
                raise ValueError("benchmark cache source contract mismatch")
            expected = benchmark
        else:
            if metadata.get("source_version") != manifest["source_version"] or metadata.get("source") != "AKShareProvider":
                raise ValueError("cached rows cannot be relabelled from another source contract")
            if attempt["status"] not in {"ACQUIRED", "EMPTY_VENDOR_RESPONSE"}:
                raise ValueError("failed download cannot contribute admitted cached rows")
            expected = bars.loc[bars["symbol"].eq(symbol) & bars["raw_close"].notna(), list(CANONICAL_BAR_COLUMNS)]
            for adjustment in ("", "hfq"):
                pieces = quotes.get((symbol, adjustment), [])
                if not pieces:
                    raise ValueError("stock cache lacks a matching native RAW/HFQ source receipt")
                native = pd.concat([part for _, part in sorted(pieces, key=lambda value: value[0])], ignore_index=True)
                native = native.drop_duplicates("date", keep="last").set_index("date")
                selected = cached.loc[cached["date"].between(history, end)]
                if not selected["date"].isin(native.index).all():
                    raise ValueError("cached dates are absent from native source response")
                native = native.reindex(selected["date"])
                for field in ("open", "high", "low", "close"):
                    target = ("adj_" if adjustment else "raw_") + field
                    np.testing.assert_allclose(selected[target].to_numpy(), native[field].to_numpy(dtype=float), rtol=0, atol=0, equal_nan=True,
                                               err_msg="cached RAW/HFQ price differs from native source")
                if adjustment == "":
                    for field, scale in (("volume", 100.0), ("amount", 1.0), ("turnover", 0.01)):
                        np.testing.assert_allclose(selected[field].to_numpy(), native[field].to_numpy(dtype=float) * scale, rtol=1e-15, atol=0, equal_nan=True,
                                                   err_msg="cached quantity differs from native source units")
        selected = cached.loc[cached["date"].between(history, end), list(CANONICAL_BAR_COLUMNS)].reset_index(drop=True)
        pd.testing.assert_frame_equal(selected, expected[list(CANONICAL_BAR_COLUMNS)].reset_index(drop=True), check_dtype=False, check_exact=True)


def verify_pilot(manifest_path, allow_synthetic=False, with_verification=False):
    """Replay local hashes and safety classification; never certify market facts."""
    path = safe_path(Path(manifest_path))
    root = path.parent
    manifest = json.loads(path.read_text(encoding="utf-8"))
    receipt = json.loads((root / "receipt.json").read_text(encoding="utf-8"))
    assert_exploratory_only(manifest)
    assert_exploratory_only(receipt)
    versions = {"FIVE_YEAR_PILOT_MANIFEST_V1": "five-year-minimal-ingestion-v1",
                "FIVE_YEAR_PILOT_MANIFEST_V2": "five-year-minimal-ingestion-v2",
                "FIVE_YEAR_PILOT_MANIFEST_V3": VERSION}
    schema = manifest.get("schema")
    legacy = schema == "FIVE_YEAR_PILOT_MANIFEST_V1"
    expected_receipt = schema.replace("MANIFEST", "RECEIPT") if schema in versions else None
    if schema not in versions or receipt.get("schema") != expected_receipt:
        raise ValueError("unsupported pilot manifest or receipt")
    if manifest.get("version") != versions[schema]:
        raise ValueError("pilot schema and calculation version disagree")
    if schema != "FIVE_YEAR_PILOT_MANIFEST_V3" and "benchmark_provenance" in manifest:
        raise ValueError("legacy benchmark provenance is cache-only; native assertions require V3 source replay")
    if type(manifest.get("synthetic_only")) is not bool or receipt.get("synthetic_only") is not manifest["synthetic_only"]:
        raise ValueError("explicit consistent synthetic classification required")
    if manifest["synthetic_only"] and not allow_synthetic:
        raise ValueError("SYNTHETIC_TEST_ONLY cannot become real historical evidence")
    if (manifest.get("data_version") != manifest_version(manifest)
            or receipt.get("data_version") != manifest["data_version"]
            or receipt.get("manifest_sha256") != sha256_file(path)):
        raise ValueError("pilot manifest/receipt hash mismatch")
    expected = {name: item["sha256"] for name, item in manifest["files"].items()}
    if not {"bars.parquet", "benchmark.parquet", "calendar.csv", "annual_coverage.csv",
            "coverage.json", "attempts.json", "sample.json"}.issubset(manifest["files"]):
        raise ValueError("required pilot snapshot artifacts missing from hash binding")
    if receipt.get("artifact_hashes") != expected or receipt.get("statuses") != manifest["statuses"]:
        raise ValueError("receipt artifact/status binding mismatch")
    for name, item in manifest["files"].items():
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts or item.get("path") != name:
            raise ValueError("pilot artifact path escapes snapshot")
        artifact = safe_path(root / relative, root)
        if not artifact.is_file() or _item(root, artifact) != item:
            raise ValueError("pilot artifact hash or byte-length mismatch: " + name)
    if (manifest["statuses"].get("pit") != "PIT_UNVERIFIED"
            or manifest["statuses"].get("hfq") != "HFQ_UNVERIFIED"
            or manifest["statuses"].get("formal") != "FORMAL_ADMISSION_BLOCKED"
            or manifest.get("no_live_money") is not True):
        raise ValueError("pilot cannot upgrade financial qualification")
    sample = json.loads((root / "sample.json").read_text(encoding="utf-8"))
    if sample.get("pit") is not False or sample.get("formal_admission") is not False or sample.get("symbols") != [item["symbol"] for item in sample.get("securities", [])]:
        raise ValueError("sample cannot assert historical membership or replace identities")
    try:
        _verify_structured_snapshot(root, manifest, sample, legacy)
    except (AssertionError, KeyError, TypeError) as exc:
        raise ValueError("structured pilot snapshot mismatch: " + str(exc)) from exc
    verification = {"calendar_origin": "NATIVE_RESPONSE_RECONSTRUCTED" if schema == "FIVE_YEAR_PILOT_MANIFEST_V3" and manifest["benchmark_provenance"]["status"] == "NATIVE_RESPONSE_RECONSTRUCTED"
                    else "UNAVAILABLE" if schema == "FIVE_YEAR_PILOT_MANIFEST_V3" else "CACHE_ONLY_NOT_NATIVE_VERIFIED",
                    "independent_exchange_calendar_completeness": "UNVERIFIED", "formal_admission": False}
    return {"manifest": manifest, "verification": verification} if with_verification else manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description="Five-year source feasibility / EXPLORATORY / NO LIVE MONEY")
    commands = parser.add_subparsers(dest="command", required=True)
    freeze = commands.add_parser("freeze", help="freeze the engineering sample before HTTP")
    freeze.add_argument("--root", required=True, type=Path)
    acquire = commands.add_parser("acquire", help="fetch bounded RAW/HFQ pilot and immutable receipts")
    acquire.add_argument("--root", required=True, type=Path)
    acquire.add_argument("--benchmark-cache", type=Path)
    acquire.add_argument("--benchmark-evidence-root", type=Path)
    acquire.add_argument("--history-start", default="2021-03-08")
    acquire.add_argument("--research-start", default="2021-09-13")
    acquire.add_argument("--research-end", default="2026-09-11")
    acquire.add_argument("--timeout-seconds", default=10.0, type=float)
    acquire.add_argument("--retry-count", default=0, type=int)
    verify = commands.add_parser("verify", help="verify bytes and receipts without financial certification")
    verify.add_argument("--manifest", required=True, type=Path)
    args = parser.parse_args(argv)
    if args.command == "freeze":
        sample = freeze_pilot(args.root)
        print(json.dumps({"sample": str(args.root / "sample.json"), "symbols": sample["symbols"], "pit": False}))
        return 0
    if args.command == "verify":
        result = verify_pilot(args.manifest, with_verification=True)
        print(json.dumps({"verified": True, "statuses": result["manifest"]["statuses"],
                          "verification": result["verification"], "formal_admission": False}))
        return 0
    result = acquire_pilot(args.root, history_start=args.history_start, research_start=args.research_start,
                           research_end=args.research_end, benchmark_cache_root=args.benchmark_cache,
                           benchmark_evidence_root=args.benchmark_evidence_root,
                           timeout_seconds=args.timeout_seconds, retry_count=args.retry_count)
    print(json.dumps({key: result[key] for key in ("manifest_path", "receipt_path", "statuses", "metrics")}))
    return 0 if result["statuses"]["software"] == "SOFTWARE_SUCCESS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
