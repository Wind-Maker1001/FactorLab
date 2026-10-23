"""Bounded native-byte supplier audit; no formal admission or price publication.

Only the pre-frozen engineering sample is read. Requests use public endpoints,
one attempt and a ten-second transport timeout; a refusal is retained, never
bypassed. All native responses and TDX bytes stay in the ignored research tree.
"""

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import time
import uuid

import pandas as pd
import requests

from qbot.research.data.schema import normalize_symbol
from qbot.research.data.tdx import audit_day, read_snapshot, safe_path
from qbot.research.data.tdx_staging import json_bytes, write_new


SUBSET = ("600000.SH", "688981.SH", "000961.SZ")
PARSER_VERSION = "five-year-supplier-coverage-v2"


def now():
    return datetime.now(timezone.utc).isoformat()


def response_rows(content, vendor, symbol, basis):
    """Parse fixed JSON/JSONP shapes, retaining only coverage diagnostics."""
    def reject(value):
        raise ValueError("nonfinite JSON: " + value)

    text = content.decode("gbk" if vendor == "SOHU" else "utf-8")
    if vendor == "SOHU":
        match = re.fullmatch(r"\s*historySearchHandler\((.*)\);?\s*", text, re.DOTALL)
        if match:
            text = match.group(1)
        payload = json.loads(text, parse_constant=reject)
        if not payload:
            return []
        if (not isinstance(payload, list) or len(payload) != 1
                or payload[0].get("status") != 0
                or payload[0].get("code") != "cn_" + symbol[:6]):
            raise ValueError("unexpected Sohu identity/status")
        values = payload[0]["hq"]
        if any(len(row) != 10 for row in values):
            raise ValueError("unexpected Sohu layout")
        rows = [(row[0], row[1], row[6], row[5], row[2]) for row in values]
    else:
        payload = json.loads(text, parse_constant=reject)
        code = symbol.split(".")[1].lower() + symbol[:6]
        if payload.get("code") != 0:
            raise ValueError("Tencent supplier returned nonzero code")
        if code not in payload.get("data", {}):
            raise ValueError("requested Tencent identity absent")
        data = payload["data"][code]
        key = "day" if basis == "RAW" else "hfqday"
        # Absence of hfqday must never be relabelled as HFQ using day/qfqday.
        if not data:
            return []
        values = data.get(key)
        if values is None:
            raise ValueError("requested price basis absent from Tencent response")
        if any(not isinstance(row, list) or len(row) < 6 for row in values):
            raise ValueError("unexpected Tencent layout")
        rows = [(row[0], row[1], row[3], row[4], row[2]) for row in values]
    if not rows:
        return []
    if any(not isinstance(row[0], str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", row[0]) for row in rows):
        raise ValueError("canonical daily session date required")
    dates = pd.to_datetime([row[0] for row in rows], errors="raise")
    if dates.has_duplicates or dates.hasnans:
        raise ValueError("duplicate or missing supplier dates")
    if not dates.equals(dates.normalize()) or (dates.dayofweek >= 5).any():
        raise ValueError("invalid supplier session dates")
    for row in rows:
        prices = [float(value) for value in row[1:]]
        if any(not pd.notna(value) or not 0 < value < float("inf") for value in prices):
            raise ValueError("invalid supplier OHLC")
        if prices[1] < max(prices) or prices[2] > min(prices):
            raise ValueError("invalid supplier OHLC relationship")
    return sorted(str(day.date()) for day in dates)


def probe(root, pilot_root, tdx_root=None, calendar_path=None):
    sample_path = safe_path(Path(pilot_root) / "sample.json")
    sample_bytes = sample_path.read_bytes()
    sample = json.loads(sample_bytes.decode("utf-8"))
    if calendar_path is None:
        raise ValueError("explicit actual benchmark calendar required")
    calendar_path = safe_path(Path(calendar_path))
    calendar_bytes = calendar_path.read_bytes()
    import io

    sessions = pd.DatetimeIndex(pd.to_datetime(pd.read_csv(io.BytesIO(calendar_bytes))["date"], errors="raise"))
    if sessions.empty or sessions.has_duplicates or sessions.hasnans or not sessions.is_monotonic_increasing:
        raise ValueError("unique nonempty actual calendar required")
    actual_dates = set(sessions.strftime("%Y-%m-%d"))
    expected = sum("2021-09-13" <= day <= "2026-09-11" for day in actual_dates)
    if (sample.get("pit") is not False or sample.get("formal_admission") is not False
            or sample.get("synthetic_only") is not False):
        raise ValueError("explicit real exploratory frozen sample required")
    if any(symbol not in sample["symbols"] for symbol in SUBSET):
        raise ValueError("supplier subset is outside the frozen sample")
    frozen_at = pd.Timestamp(sample["frozen_at"])
    if frozen_at.tzinfo is None or frozen_at > pd.Timestamp(now()):
        raise ValueError("sample must be frozen before requests")
    root = safe_path(Path(root))
    # Bind privacy to this checkout's ignored tree, not arbitrary path names.
    private = safe_path(Path(__file__).resolve().parents[2] / "data" / "research")
    if private not in root.parents:
        raise ValueError("native supplier bytes require a private research root")
    pilot = sample_path.parent
    if root == pilot or root in pilot.parents or pilot in root.parents:
        raise ValueError("supplier output must be disjoint from pilot inputs")
    run = root / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + "-" + uuid.uuid4().hex[:8])
    run.mkdir(parents=True, exist_ok=False)
    sources = run / "sources"
    sources.mkdir()
    write_new(run / "sample.json", sample_bytes)
    write_new(run / "calendar.csv", calendar_bytes)
    attempts = []
    blocked_vendors = set()
    started_at = now()
    started = time.perf_counter()
    for vendor in ("TENCENT", "SOHU"):
        for symbol in SUBSET:
            for basis in (("RAW", "HFQ") if vendor == "TENCENT" else ("RAW",)):
                if vendor in blocked_vendors:
                    item = {"vendor": vendor, "symbol": symbol, "basis": basis,
                            "status": "NOT_ATTEMPTED_ACCESS_LIMIT", "attempts": 0,
                            "reason": "Earlier 401/403/429 stopped this supplier for this run",
                            "parser_version": PARSER_VERSION, "financial_qualification": "UNVERIFIED"}
                    attempts.append(item)
                    write_new(run / ("attempt-%02d.json" % len(attempts)), json_bytes(item))
                    continue
                time.sleep(1.0)
                item = {"vendor": vendor, "symbol": symbol, "basis": basis,
                        "requested_start": "2021-03-08", "requested_end": "2026-09-11",
                        "requested_at": now(), "parser_version": PARSER_VERSION,
                        "upstream_vintage": "UNKNOWN_UNVERIFIED", "attempts": 1,
                        "financial_qualification": "UNVERIFIED"}
                if vendor == "TENCENT":
                    url = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
                    code = symbol.split(".")[1].lower() + symbol[:6]
                    params = {"param": "%s,day,2021-03-08,2026-09-11,2000,%s"
                              % (code, "hfq" if basis == "HFQ" else "")}
                else:
                    url = "https://q.stock.sohu.com/hisHq"
                    params = {"code": "cn_" + symbol[:6], "start": "20210308",
                              "end": "20260911", "stat": "1", "order": "D",
                              "period": "d", "callback": "historySearchHandler", "rt": "jsonp"}
                begin = time.perf_counter()
                try:
                    response = requests.get(url, params=params, timeout=10)
                    body = response.content
                    native = sources / ("%s-%s-%s.bin" % (vendor, symbol, basis))
                    write_new(native, body)
                    item.update(url=response.url, http_status=response.status_code,
                                response_sha256=hashlib.sha256(body).hexdigest(),
                                response_bytes=len(body), native_file=native.relative_to(run).as_posix(),
                                retrieved_at=now(), representation="NATIVE_HTTP_RESPONSE_CONTENT_BYTES")
                    if response.status_code in {401, 403, 429}:
                        blocked_vendors.add(vendor)
                    response.raise_for_status()
                    dates = response_rows(body, vendor, symbol, basis)
                    # Some endpoints ignore boundaries. Keep all source bytes and disclose extra rows.
                    used = [day for day in dates if "2021-03-08" <= day <= "2026-09-11"]
                    target = [day for day in used if day >= "2021-09-13"]
                    outside_calendar = [day for day in used if day not in actual_dates]
                    if outside_calendar:
                        raise ValueError("supplier observations outside frozen benchmark calendar")
                    item.update(status="OBSERVED" if used else "EMPTY", source_rows=len(dates),
                                rows=len(used), research_rows=len(target),
                                research_expected_sessions=expected, missing_research_sessions=expected - len(target),
                                first_date=used[0] if used else None, last_date=used[-1] if used else None,
                                outside_request_rows=len(dates) - len(used),
                                validated_fields=["date", "open", "high", "low", "close"],
                                other_fields="native volume retained but not validated; Sohu amount/turnover retained but not validated",
                                units={"price": "CNY_VENDOR_DECLARATION_NOT_INDEPENDENTLY_CERTIFIED",
                                       "volume": "UNKNOWN_UNVERIFIED", "amount": "UNKNOWN_UNVERIFIED",
                                       "turnover": "UNKNOWN_UNVERIFIED"})
                except Exception as exc:
                    item.update(status="FAILED", error_type=type(exc).__name__,
                                reason="TRANSPORT_OR_SUPPLIER_SHAPE_FAILURE; inspect private native response")
                    if vendor in blocked_vendors:
                        item.update(status="ACCESS_DENIED_OR_RATE_LIMIT", reason="SUPPLIER_STOPPED_NO_RETRY_OR_BYPASS")
                item["elapsed_seconds"] = time.perf_counter() - begin
                attempts.append(item)
                write_new(run / ("attempt-%02d.json" % len(attempts)), json_bytes(item))
                print(vendor, symbol, basis, item["status"], item.get("research_rows"), flush=True)
    if tdx_root is not None:
        boundary = safe_path(Path(tdx_root))
        for symbol in sample["symbols"]:
            normalize_symbol(symbol)
            market = symbol.split(".")[1].lower()
            path = boundary / market / "lday" / (market + symbol[:6] + ".day")
            item = {"vendor": "TDX", "symbol": symbol, "basis": "RAW_CANDIDATE",
                    "requested_at": now(), "parser_version": PARSER_VERSION,
                    "financial_qualification": "UNVERIFIED", "units": "UNKNOWN_UNVERIFIED"}
            try:
                body, digest = read_snapshot(path, boundary)
                native = sources / ("TDX-" + symbol + ".bin")
                write_new(native, body)
                item.update(response_sha256=digest, response_bytes=len(body),
                            native_file=native.relative_to(run).as_posix(), retrieved_at=now(),
                            representation="LOCAL_DAY_ORIGINAL_BYTES")
                audit = audit_day(path, market, boundary, "2021-03-08", "2026-09-11", source_bytes=body)
                dates = audit.dates
                if not audit.convertible or dates is None:
                    raise ValueError("TDX candidate audit failed")
                used = dates[(dates >= "2021-03-08") & (dates <= "2026-09-11")]
                target = used[used >= "2021-09-13"]
                item.update(status="OBSERVED", response_sha256=digest, response_bytes=len(body),
                            native_file=native.relative_to(run).as_posix(), retrieved_at=now(),
                            source_rows=len(dates), rows=len(used), research_rows=len(target),
                            first_date=str(used.min().date()) if len(used) else None,
                            last_date=str(used.max().date()) if len(used) else None,
                            representation="LOCAL_DAY_ORIGINAL_BYTES", audit_status=audit.inventory["audit_status"],
                            anomalies=[{k: a[k] for k in ("code", "severity", "count")} for a in audit.anomalies])
            except Exception as exc:
                item.update(status="FAILED", error_type=type(exc).__name__, reason="LOCAL_INPUT_ABSENT_OR_INVALID")
            attempts.append(item)
            write_new(run / ("attempt-%02d.json" % len(attempts)), json_bytes(item))
    receipt = {"schema": "FIVE_YEAR_SUPPLIER_PROBE_V1", "purpose": "EXPLORATORY",
               "formal_admission": False, "no_live_money": True, "synthetic_only": False,
               "sample_sha256": hashlib.sha256(sample_bytes).hexdigest(), "frozen_at": sample["frozen_at"],
               "calendar_sha256": hashlib.sha256(calendar_bytes).hexdigest(),
               "started_at": started_at, "finished_at": now(), "elapsed_seconds": time.perf_counter() - started,
               "parser_version": PARSER_VERSION, "http_attempts": sum(item.get("attempts", 0) for item in attempts),
               "implementation_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
               "rate_policy": "serial; 1s spacing; no retries or access-limit bypass",
               "distribution_permission": "NOT_ESTABLISHED_NATIVE_BYTES_PRIVATE_ONLY", "attempts": attempts}
    write_new(run / "receipt.json", json_bytes(receipt))
    print("receipt:", run / "receipt.json", flush=True)
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--pilot-root", type=Path, required=True)
    parser.add_argument("--tdx-root", type=Path)
    parser.add_argument("--calendar", type=Path, required=True)
    args = parser.parse_args()
    probe(args.root, args.pilot_root, args.tdx_root, args.calendar)


if __name__ == "__main__":
    main()
