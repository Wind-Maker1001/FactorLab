"""Immutable RAW-only publication and offline TDX diagnostic run orchestration."""

from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import re
import sys
import time
import uuid
from typing import Any, Dict, Optional

import numpy as np
import pandas as pd

from factorlab.data.tdx import (
    DAY_DTYPE, FORMAT_VERSION, TdxError, DayAudit, audit_day, discover, raw_frame,
    NAME, read_snapshot, safe_path,
)
from factorlab.reproducibility import git_commit, git_worktree_state, package_versions


CONTRACT = {
    "schema": "TDX_RAW_STAGING_V1", "format": FORMAT_VERSION,
    "usage": "ENGINEERING_DIAGNOSTIC_ONLY_NO_LIVE_MONEY",
    "adjustment_status": "RAW_UNADJUSTED", "adjustment_evidence_status": "UNVERIFIED",
    "price_scale_status": "UNVERIFIED_OR_UNKNOWN", "volume_unit": "UNKNOWN",
    "amount_unit": "UNKNOWN", "financial_research_eligible": False,
    "format_verification": "LOCAL_STRUCTURE_PLAUSIBILITY_NOT_INDEPENDENT_REFERENCE",
    "production_schema": "NOT_FACTORLAB_CANONICAL_BARS",
}


def json_bytes(payload: Any) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False).encode("utf-8")


def sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def write_new(path: Path, content: bytes) -> None:
    safe_path(path)
    with path.open("xb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


def disjoint(source: Path, output: Path) -> None:
    source, output = safe_path(source), safe_path(output)
    if source == output or source in output.parents or output in source.parents:
        raise TdxError("SOURCE_OUTPUT_OVERLAP: %s" % output)


def staging_root(path: Path) -> Path:
    root = safe_path(path)
    marker = root / ".tdx-raw-staging.json"
    if root.exists() and not marker.exists() and any(root.iterdir()):
        raise TdxError("REFUSE_NONEMPTY_UNMARKED_STAGING_ROOT")
    root.mkdir(parents=True, exist_ok=True)
    marker = safe_path(marker, root)
    if marker.exists():
        content, _ = read_snapshot(marker, root)
        if content != json_bytes({"usage": CONTRACT["usage"], "schema": CONTRACT["schema"]}):
            raise TdxError("STAGING_MARKER_MISMATCH")
    else:
        write_new(marker, json_bytes({"usage": CONTRACT["usage"], "schema": CONTRACT["schema"]}))
    return root


def frame_source_sha256(frame: pd.DataFrame) -> str:
    """Reconstruct every 32-byte record, including reserved bits and float32."""
    encoded = np.empty(len(frame), dtype=DAY_DTYPE)
    encoded["date"] = frame["tdx_date_integer"].to_numpy()
    for field in ("open", "high", "low", "close"):
        encoded[field] = frame["tdx_" + field + "_integer"].to_numpy()
    encoded["volume"] = frame["volume"].to_numpy()
    encoded["amount"] = frame["amount"].to_numpy().astype(np.float32)
    encoded["reserved"] = frame["tdx_reserved"].to_numpy()
    return sha256(encoded.tobytes())


def convert_day(audit: DayAudit, root: Path) -> Dict[str, Any]:
    """One-file memory, exact round trip, exclusive atomic directory publish."""
    begin = time.perf_counter()
    root = staging_root(root)
    frame = raw_frame(audit)
    contract_digest = sha256(json_bytes(CONTRACT))
    # Bind implementation too: old generations cannot be reused across a
    # decoder change merely because someone forgot to change its version.
    implementation = sha256(Path(__file__).read_bytes() + Path(audit_day.__code__.co_filename).read_bytes())
    identity = sha256(json_bytes({"contract": contract_digest, "implementation": implementation,
                                 "source": audit.inventory["source_file"],
                                 "sha256": audit.inventory["source_sha256"],
                                 "parse_status": audit.inventory["audit_status"],
                                 "security": audit.security}))
    parent = safe_path(root / audit.inventory["source_market"] / audit.inventory["symbol"], root)
    parent.mkdir(parents=True, exist_ok=True)
    destination = safe_path(parent / identity, root)
    expected = {"schema": CONTRACT["schema"], "identity": identity, "contract": CONTRACT,
                "implementation_sha256": implementation,
                "source_file": audit.inventory["source_file"],
                "source_sha256": audit.inventory["source_sha256"], "rows": len(frame),
                "symbol": audit.inventory["symbol"], "security": audit.security}

    def validate(folder):
        receipt_content, _ = read_snapshot(folder / "receipt.json", root)
        receipt = json.loads(receipt_content)
        if set(receipt) != set(expected) | {"parquet_sha256", "parquet_bytes"}:
            raise TdxError("STAGING_RECEIPT_FIELDS_MISMATCH")
        if any(receipt.get(key) != value for key, value in expected.items()):
            raise TdxError("STAGING_RECEIPT_IDENTITY_MISMATCH")
        content, digest = read_snapshot(folder / "data.parquet", root)
        if digest != receipt["parquet_sha256"] or len(content) != receipt["parquet_bytes"]:
            raise TdxError("STAGING_PARQUET_HASH_MISMATCH")
        restored = pd.read_parquet(io.BytesIO(content))
        pd.testing.assert_frame_equal(frame, restored, check_exact=True)
        if frame_source_sha256(restored) != expected["source_sha256"]:
            raise TdxError("SOURCE_BINARY_ROUNDTRIP_MISMATCH")
        return receipt

    if destination.exists():
        receipt = validate(destination)
        status = "REUSED_VERIFIED"
    else:
        temporary = safe_path(parent / (".tmp-" + uuid.uuid4().hex), root)
        temporary.mkdir(exist_ok=False)
        parquet = temporary / "data.parquet"
        with parquet.open("xb") as handle:
            frame.to_parquet(handle, index=False, engine="pyarrow", compression="zstd")
            handle.flush()
            os.fsync(handle.fileno())
        content, digest = read_snapshot(parquet, root)
        receipt = dict(expected, parquet_sha256=digest, parquet_bytes=len(content))
        write_new(temporary / "receipt.json", json_bytes(receipt))
        validate(temporary)
        # Nonempty destination generations are never overwritten. Concurrent
        # writers may leave an unreferenced temporary; it is not a publication.
        os.rename(str(temporary), str(destination))
        status = "CONVERTED_RAW_ONLY"
    return {"source_file": audit.inventory["source_file"], "symbol": audit.inventory["symbol"],
            "source_sha256": expected["source_sha256"], "status": status,
            "parquet_file": str(destination / "data.parquet"),
            "parquet_sha256": receipt["parquet_sha256"], "parquet_bytes": receipt["parquet_bytes"],
            "rows": len(frame), "seconds": time.perf_counter() - begin,
            "financial_research_eligible": False}


def peak_memory() -> Dict[str, Any]:
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        class Counters(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)] + [
                (field, ctypes.c_size_t) for field in (
                    "PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage",
                    "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage", "QuotaNonPagedPoolUsage",
                    "PagefileUsage", "PeakPagefileUsage")]
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
        counters = Counters()
        counters.cb = ctypes.sizeof(counters)
        if psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
            return {"peak_rss_bytes": counters.PeakWorkingSetSize, "current_rss_bytes": counters.WorkingSetSize,
                    "memory_method": "Windows GetProcessMemoryInfo process-lifetime peak working set"}
    else:
        import resource
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return {"peak_rss_bytes": int(peak * (1 if sys.platform == "darwin" else 1024)),
                "memory_method": "getrusage process-lifetime peak RSS"}
    return {"peak_rss_bytes": None, "memory_method": "UNKNOWN"}


def write_csv(path: Path, rows, columns=None) -> None:
    frame = pd.DataFrame(rows)
    if columns is not None:
        frame = frame.reindex(columns=columns)
    content = frame.to_csv(index=False, lineterminator="\n").encode("utf-8-sig")
    write_new(path, content)


def load_calendar(path: Path):
    content, digest = read_snapshot(path, safe_path(path).parent)
    frame = pd.read_parquet(io.BytesIO(content)) if path.suffix.lower() == ".parquet" else pd.read_csv(io.BytesIO(content))
    if "date" not in frame or frame.empty:
        raise TdxError("REFERENCE_CALENDAR_DATE_COLUMN_REQUIRED")
    dates = pd.DatetimeIndex(pd.to_datetime(frame["date"], errors="raise"))
    if dates.tz is not None or dates.hasnans or dates.has_duplicates or not dates.is_monotonic_increasing or not dates.equals(dates.normalize()) or (dates.dayofweek >= 5).any():
        raise TdxError("INVALID_REFERENCE_CALENDAR")
    return dates, {"path": str(safe_path(path)), "sha256": digest, "rows": len(dates),
                   "independent_calendar_verification": "UNVERIFIED"}


def run_audit(root: Path, report_root: Path, staging: Optional[Path] = None,
              start="2020-01-02", end="2026-10-09", research_start="2021-09-13",
              research_end="2026-09-11", warmup_days=130, calendar_path: Optional[Path] = None,
              run_id: Optional[str] = None, progress=None) -> Dict[str, Any]:
    """Write exclusive diagnostic artifacts; never register production inputs."""
    begin = time.perf_counter()
    for date in (start, end, research_start, research_end):
        if not isinstance(date, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", date):
            raise TdxError("DATES_REQUIRE_YYYY_MM_DD")
        pd.Timestamp(date)
    if start > end or research_start > research_end or not start <= research_start <= research_end <= end:
        raise TdxError("INVALID_DATE_RANGES")
    if isinstance(warmup_days, bool) or not isinstance(warmup_days, int) or not 0 <= warmup_days <= 2000:
        raise TdxError("INVALID_WARMUP_DAYS")
    root, report_root = safe_path(root), safe_path(report_root)
    disjoint(root, report_root)
    if staging is not None:
        staging = safe_path(staging)
        disjoint(root, staging)
        disjoint(report_root, staging)
    run_id = run_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ") + "-tdx-" + uuid.uuid4().hex[:8]
    if not re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9_.-]{0,120}", run_id) or ".." in run_id:
        raise TdxError("INVALID_RUN_ID")
    destination = safe_path(report_root / run_id, report_root)
    destination.mkdir(parents=True, exist_ok=False)
    repo = Path(__file__).resolve().parents[3]
    manifest = {"schema_version": 1, "run_id": run_id, "created_at": datetime.now(timezone.utc).isoformat(),
                "source": "user-local-TDX-vipdoc", "source_root": str(root), "contracts": CONTRACT,
                "configuration": {"start": start, "end": end, "research_start": research_start,
                                  "research_end": research_end, "warmup_days": warmup_days,
                                  "report_root": str(report_root), "staging_root": None if staging is None else str(staging)},
                "code": {"git_commit": git_commit(repo), **git_worktree_state(repo),
                         "module_sha256": {str(p): sha256(p.read_bytes()) for p in (Path(__file__), Path(audit_day.__code__.co_filename))}},
                "environment": {"python": sys.version, "platform": platform.platform(),
                                "packages": package_versions(("numpy", "pandas", "pyarrow"))},
                "network_requests": 0, "financial_research_eligible": False}
    inventories, securities, coverages, anomalies, conversions, samples = [], [], [], [], [], []
    try:
        calendar, calendar_info = (None, {"independent_calendar_verification": "UNKNOWN"}) if calendar_path is None else load_calendar(Path(calendar_path))
        manifest["calendar_reference"] = calendar_info
        scan_begin = time.perf_counter()
        entries, discovery_issues = discover(root)
        scan_seconds = time.perf_counter() - scan_begin
        anomalies.extend(discovery_issues)
        if not entries:
            anomalies.append({"code": "NO_DAY_FILES", "severity": "ERROR", "count": 1})
        if staging is not None:
            staging = staging_root(staging)
        symbol_counts = {}
        for entry in entries:
            match = NAME.fullmatch(entry["path"].name)
            if match and match.group(1).lower() == entry["source_market"]:
                key = (entry["source_market"], match.group(2))
                symbol_counts[key] = symbol_counts.get(key, 0) + 1
        union_dates = {market: set() for market in ("sh", "sz", "bj")}
        for index, entry in enumerate(entries):
            item = audit_day(entry["path"], entry["source_market"], root, start, end,
                             calendar, research_start, research_end, warmup_days)
            if item.inventory["symbol"]:
                key = (entry["source_market"], item.inventory["symbol"].split(".")[0])
                if symbol_counts[key] > 1:
                    item.inventory["audit_status"] = "FAIL"
                    item.anomalies.append({"source_file": str(entry["path"]), "symbol": item.inventory["symbol"], "code": "DUPLICATE_SOURCE_SYMBOL", "severity": "ERROR", "count": symbol_counts[key], "detail": "Conflicting copies are retained; no automatic choice"})
            inventories.append(item.inventory)
            securities.append(item.security)
            coverages.append(item.coverage)
            anomalies.extend(item.anomalies)
            if item.dates is not None:
                union_dates[entry["source_market"]].update(int(d) for d in item.records["date"][~item.dates.isna()])
            if item.convertible:
                if staging is not None:
                    try:
                        conversions.append(convert_day(item, staging))
                    except Exception as exc:
                        conversions.append({"source_file": str(entry["path"]), "symbol": item.inventory["symbol"], "status": "FAILED", "error": "%s: %s" % (type(exc).__name__, exc)})
                else:
                    conversions.append({"source_file": str(entry["path"]), "symbol": item.inventory["symbol"], "status": "NOT_REQUESTED"})
                # A small reproducible selection across market/type/history.
                if item.inventory["symbol"] in {"600000.SH", "000001.SZ", "000905.SH", "510300.SH", "159915.SZ", "920001.BJ", "110075.SH", "900901.SH"}:
                    frame = raw_frame(item)
                    for pos in sorted({0, len(frame) // 2, len(frame) - 1}):
                        row = frame.iloc[pos]
                        samples.append({"symbol": row.symbol, "date": str(row.date.date()), "source_file": row.source_file,
                                        "source_sha256": row.source_sha256, "security_type": item.security["security_type"],
                                        "candidate_price_divisor": row.candidate_price_divisor,
                                        **{f: row[f] for f in ("open", "high", "low", "close", "volume", "amount")},
                                        "tdx_reserved": row.tdx_reserved, "independent_verification": "UNKNOWN"})
            else:
                conversions.append({"source_file": str(entry["path"]), "symbol": item.inventory["symbol"], "status": "REJECTED_AUDIT_FAILURE"})
            if progress and ((index + 1) % 500 == 0 or index + 1 == len(entries)):
                progress(index + 1, len(entries))
        markets = {}
        coverage_by_path = {row["source_file"]: row for row in coverages}
        for market in union_dates:
            rows = [r for r in inventories if r["source_market"] == market]
            dates = sorted(union_dates[market])
            market_coverage = [coverage_by_path[row["source_file"]] for row in rows]
            date_text = lambda d: "%04d-%02d-%02d" % (d // 10000, d // 100 % 100, d % 100)
            markets[market] = {"files": len(rows), "source_bytes": sum(r["size_bytes"] or 0 for r in rows),
                               "records": sum(r["record_count"] for r in rows), "unique_observed_dates": len(dates),
                               "observed_start": date_text(dates[0]) if dates else None, "observed_end": date_text(dates[-1]) if dates else None,
                               "year_records": {str(y): sum(c.get("records_%s" % y, 0) for c in market_coverage) for y in range(pd.Timestamp(start).year, pd.Timestamp(end).year + 1)}}
        valid = [r for r in inventories if r["audit_status"] != "FAIL"]
        converted = [r for r in conversions if r["status"] in {"CONVERTED_RAW_ONLY", "REUSED_VERIFIED"}]
        failed = [r for r in inventories if r["audit_status"] == "FAIL"]
        original_bytes = sum(r["size_bytes"] or 0 for r in inventories)
        parquet_bytes = sum(r["parquet_bytes"] for r in converted)
        read_seconds = sum(r["read_hash_seconds"] for r in inventories)
        decode_seconds = sum(r["decode_audit_seconds"] for r in inventories)
        conversion_seconds = sum(r.get("seconds", 0) for r in converted)
        metrics = {"markets": markets, "discovered_day_files": len(inventories), "valid_day_files": len(valid),
                   "failed_day_files": len(failed), "warning_day_files": sum(r["audit_status"] == "WARN" for r in inventories),
                   "confirmed_a_shares": 0, "a_share_candidates": sum(r["security_type"] == "A_SHARE_CANDIDATE" for r in securities),
                   "classification_counts": pd.Series([r["security_type"] for r in securities], dtype=object).value_counts().to_dict(),
                   "records": sum(r["record_count"] for r in inventories), "convertible_records": sum(r["record_count"] for r in valid),
                   "source_bytes": original_bytes, "parquet_bytes": parquet_bytes, "converted_files": len(converted),
                   "converted_records": sum(r["rows"] for r in converted),
                   "parquet_compression_ratio": None if not parquet_bytes else sum(r["size_bytes"] or 0 for r in valid) / parquet_bytes,
                   "audit_failure_ratio": None if not inventories else len(failed) / len(inventories),
                   "anomaly_file_ratio": None if not inventories else sum(r["audit_status"] != "PASS" for r in inventories) / len(inventories),
                   "scan_seconds": scan_seconds, "read_hash_seconds": read_seconds, "decode_audit_seconds": decode_seconds,
                   "decode_audit_records_per_second": None if decode_seconds == 0 else sum(r["record_count"] for r in inventories) / decode_seconds,
                   "conversion_seconds": conversion_seconds, "conversion_records_per_second": None if not conversion_seconds else sum(r["rows"] for r in converted) / conversion_seconds,
                   **peak_memory()}
        a_share_paths = {s["source_file"] for s in securities if s["security_type"] == "A_SHARE_CANDIDATE"}
        for scope, paths in (("all_files", set(coverage_by_path)), ("a_share_candidates", a_share_paths)):
            selected = [coverage_by_path[p] for p in paths]
            ratios = [r["research_coverage_ratio"] for r in selected if r["research_coverage_ratio"] is not None]
            metrics["coverage_" + scope] = {
                "reference_calendar_status": "UNVERIFIED" if calendar is not None else "UNKNOWN",
                "research_mean_ratio": float(np.mean(ratios)) if ratios else None,
                "full_research_and_warmup_files": sum(r["research_and_warmup_complete"] is True for r in selected),
                "expected_research_dates": None if not selected else selected[0]["research_expected_dates"],
                "no_inference_of_listing_or_suspension": True}
        manifest["metrics"] = metrics
        manifest["status"] = "partial" if failed or any(a.get("severity") == "ERROR" for a in anomalies) or any(r["status"] == "FAILED" for r in conversions) else "engineering_complete"
        write_csv(destination / "inventory.csv", inventories)
        write_csv(destination / "securities.csv", securities)
        write_csv(destination / "coverage.csv", coverages)
        write_csv(destination / "anomalies.csv", anomalies, ["source_file", "symbol", "severity", "code", "count", "sample_record_indices", "detail"])
        write_csv(destination / "conversion_status.csv", conversions)
        write_csv(destination / "verification_samples.csv", samples)
        # Archive anomaly identities/reasons only; originals are never moved.
        write_new(destination / "quarantine_manifest.json", json_bytes({"usage": "LOGICAL_QUARANTINE_NO_SOURCE_MOVES", "failed_sources": failed, "anomalies_csv": "anomalies.csv"}))
        write_new(destination / "summary.md", summary_text(manifest).encode("utf-8"))
        manifest["artifacts"] = {p.name: {"sha256": sha256(p.read_bytes()), "bytes": p.stat().st_size} for p in destination.iterdir() if p.is_file()}
        manifest["elapsed_seconds"] = time.perf_counter() - begin
        write_new(destination / "manifest.json", json_bytes(manifest))
    except Exception as exc:
        manifest.update(status="failed", error="%s: %s" % (type(exc).__name__, exc), elapsed_seconds=time.perf_counter() - begin)
        if not (destination / "manifest.json").exists():
            write_new(destination / "manifest.json", json_bytes(manifest))
        raise
    return {"report_dir": str(destination), **manifest}


def summary_text(manifest: Dict[str, Any]) -> str:
    m = manifest["metrics"]
    lines = ["# 通达信本地 DAY 工程审计", "", "**仅 RAW staging / NO LIVE MONEY；未通过金融研究验收，不是正式五年中证500实证。**", "",
             "运行：`%s`；状态：`%s`。" % (manifest["run_id"], manifest["status"]),
             "来源：`%s`。全程本地只读，0网络请求。" % manifest["source_root"], "",
             "| 市场 | DAY文件 | 32字节完整记录 | 原文件字节 | 首日 | 末日 |", "| --- | ---: | ---: | ---: | --- | --- |"]
    for market, values in m["markets"].items():
        lines.append("| %s | %s | %s | %s | %s | %s |" % (market, values["files"], values["records"], values["source_bytes"], values["observed_start"], values["observed_end"]))
    lines += ["", "发现%s个DAY；结构/数值可转换%s个，失败%s个，有警告%s个。代码规则A股候选%s个，独立主数据确认A股 **0个（UNKNOWN）**；不能按文件数推断股票数。" % (m["discovered_day_files"], m["valid_day_files"], m["failed_day_files"], m["warning_day_files"], m["a_share_candidates"]),
              "证券分类：`%s`。历史退市、代码更换、上市日均UNKNOWN，未按当前列表删文件。" % json.dumps(m["classification_counts"], ensure_ascii=False), "",
              "| 年份 | SH有效日期记录数 | SZ有效日期记录数 | BJ有效日期记录数 |", "| --- | ---: | ---: | ---: |"]
    for year in m["markets"]["sh"]["year_records"]:
        lines.append("| %s | %s | %s | %s |" % (year, *[m["markets"][market]["year_records"][year] for market in ("sh", "sz", "bj")]))
    lines += ["", "年度数为请求范围内合法唯一日期观测；损坏文件的可解析部分只用于审计，不发布。2026只统计至配置末日。",
              "参考日历：`%s`。coverage.csv逐证券列出研究/130日预热覆盖及缺口；缺日原因UNKNOWN，不推断停牌或自动填补。缺独立2020—2026完整日历时，不宣称全区间交易日覆盖已核验。" % manifest["calendar_reference"], "",
              "原始%s字节；本次引用Parquet %s字节；转换%s文件/%s记录，压缩比%s。Parquet含额外原整数、来源与语义列，因此压缩比可以小于1；目录、收据和旧代另计。" % (m["source_bytes"], m["parquet_bytes"], m["converted_files"], m["converted_records"], m["parquet_compression_ratio"]),
              "扫描%.3fs；读取+SHA %.3fs；解析及质量/覆盖检查%.3fs（%s记录/s）；转换、重读及逐字节回验%.3fs（%s记录/s）。" % (m["scan_seconds"], m["read_hash_seconds"], m["decode_audit_seconds"], m["decode_audit_records_per_second"], m["conversion_seconds"], m["conversion_records_per_second"]),
              "进程峰值RSS：%s字节；口径：%s。失败比例%s；含警告/失败文件比例%s。" % (m["peak_rss_bytes"], m["memory_method"], m["audit_failure_ratio"], m["anomaly_file_ratio"]), "",
              "32字节小端布局通过本地日期/OHLC结构检查；价格缩放、量/额单位和原始调整语义为UNVERIFIED/UNKNOWN。候选股票/指数÷100、基金÷1000仅供显示，未知类型显示价为空；原整数完整保存。量按无符号位模式保留，高位不等于负成交量。amount保留原float32精度，不补造更高精度。",
              "本次没有通达信界面独立OHLCV参考，**无字段被本次自动认证**。verification_samples.csv提供跨市场、类型、历史长度的实际样本；请在未复权模式逐条记录界面OHLC、成交量及股/手单位、成交额及元/万元/亿元单位、截图来源和日期。指数成交量不能套用股票单位。",
              "异常见anomalies.csv及quarantine_manifest.json（仅逻辑隔离，不移动原文件）。零量、量额不匹配、周末、大幅价格变化及日历缺口需核对状态/公司行为；没有涨跌停或除权事件证明，不能自行修正。", "",
              "接入仍须独立单位/身份核验、后复权OHLC与公司行为、换手率、历史上市退市、每日ST/停牌、完整起点500成员及实际生效调样链、独立交易日历。完成后另行组装精确生产Schema，经原import-market-data和PIT/状态门禁进入DatasetBuilder；本工具不生成adj_*，不注册Provider，不写研究缓存或正式数据集。", "",
              "manifest绑定输入SHA、配置、代码/工作区、环境与输出SHA；转换收据绑定每份Parquet，重读后精确重建源32字节记录。再次运行新建报告，完整校验后复用同一不可变RAW代；不覆盖失败或历史审计。测试工程证据另见TDX_LOCAL_DATA.md，人工单测不作为真实市场验收。", ""]
    return "\n".join(lines)
