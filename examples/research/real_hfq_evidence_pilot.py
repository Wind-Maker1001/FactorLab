"""Offline, bounded 600000.SH evidence replay; never grants market admission.

No HTTP, SDK eval, anchor estimation, cache writes or Forward dependencies.
Vendor precision below is a diagnostic hypothesis, never a certified contract.
"""

import argparse
from decimal import Decimal, localcontext
import io
import itertools
import json
from pathlib import Path
import re
from urllib.parse import parse_qs, urlsplit

import numpy as np
import pandas as pd

from qbot.research.data.corporate_actions.sources.akshare import parse_sina_factors
from qbot.research.data.evidence import date, implementation_sha256, sha256
from qbot.research.data.tdx import DAY_DTYPE, FORMAT_VERSION, safe_path
from qbot.research.data.tdx_staging import frame_source_sha256, write_new


VERSION = "600000-hfq-diagnostic-v6-origin-contracts"
FIELDS = ("open", "high", "low", "close")
DATES = ("2025-07-09", "2025-07-10", "2025-07-11", "2025-07-14",
         "2025-07-15", "2025-07-16", "2025-07-17", "2025-07-18")
ORIGINS = {"TDX": "TDX", "EASTMONEY": "EASTMONEY",
           "AKSHARE_EASTMONEY": "EASTMONEY", "TENCENT": "TENCENT",
           "SOHU": "SOHU", "SINA": "SINA", "LOCAL_GENERATED": None}
REQUEST_CONTRACTS = {
    "tencent-raw": ("TENCENT", "web.ifzq.gtimg.cn", "/appstock/app/fqkline/get",
                    {"param": ["sh600000,day,2025-07-09,2025-07-18,8,"]}),
    "tencent-hfq": ("TENCENT", "web.ifzq.gtimg.cn", "/appstock/app/fqkline/get",
                    {"param": ["sh600000,day,2025-07-09,2025-07-18,8,hfq"]}),
    "sohu-raw": ("SOHU", "q.stock.sohu.com", "/hisHq",
                 {"code": ["cn_600000"], "start": ["20250709"], "end": ["20250718"],
                  "stat": ["1"], "order": ["D"], "period": ["d"],
                  "callback": ["historySearchHandler"], "rt": ["jsonp"]}),
    "sina-factors": ("SINA", "finance.sina.com.cn", "/realstock/company/sh600000/hfq.js", {}),
}


def distinct_vendor(left, right):
    """Distinct vendor candidates only; backend independence needs external proof."""
    return (left in ORIGINS and right in ORIGINS
            and ORIGINS[left] is not None and ORIGINS[right] is not None
            and ORIGINS[left] != ORIGINS[right])


def number(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d+(?:\.\d+)?", value):
        raise ValueError("explicit positive decimal price required")
    result = Decimal(value)
    if not result.is_finite() or result <= 0:
        raise ValueError("positive finite price required")
    return result


def strict_json(content):
    def reject(_):
        raise ValueError("nonfinite JSON")
    return json.loads(content, parse_constant=reject)


def checked_rows(rows):
    if not rows or len(rows) != len(DATES):
        raise ValueError("frozen eight-day scope required")
    result = {}
    for row in rows:
        day = row["date"]
        date(day)
        if day in result or day not in DATES:
            raise ValueError("duplicate or unexpected date")
        prices = {field: number(row[field]) for field in FIELDS}
        if (prices["high"] < max(prices.values())
                or prices["low"] > min(prices.values())):
            raise ValueError("invalid OHLC relationships")
        result[day] = prices
    if set(result) != set(DATES):
        raise ValueError("missing frozen date")
    return {day: result[day] for day in DATES}


def parse_eastmoney(content):
    payload = strict_json(content)
    data = payload["data"]
    if (payload["rc"] != 0 or data["code"] != "600000"
            or data["market"] != 1 or data["decimal"] != 2):
        raise ValueError("unexpected Eastmoney identity or price precision")
    rows = []
    for line in data["klines"]:
        fields = line.split(",")
        if len(fields) != 11:
            raise ValueError("unexpected Eastmoney kline layout")
        rows.append(dict(zip(("date", "open", "close", "high", "low"), fields[:5])))
    return checked_rows(rows)


def parse_tencent(content, adjustment):
    if adjustment not in {"raw", "hfq"}:
        raise ValueError("explicit raw/hfq required")
    payload = strict_json(content)
    if payload["code"] != 0 or "sh600000" not in payload["data"]:
        raise ValueError("unexpected Tencent identity/status")
    key = "day" if adjustment == "raw" else "hfqday"
    rows = []
    for values in payload["data"]["sh600000"][key]:
        if not isinstance(values, list) or len(values) not in {6, 7}:
            raise ValueError("unexpected Tencent kline layout")
        rows.append(dict(zip(("date", "open", "close", "high", "low"), values[:5])))
    return checked_rows(rows)


def parse_sohu(content):
    # The archived response is GBK JSONP. Only this fixed wrapper is accepted.
    text = content.decode("gbk")
    match = re.fullmatch(r"\s*historySearchHandler\((.*)\);?\s*", text, re.DOTALL)
    if not match:
        raise ValueError("unexpected Sohu JSONP wrapper")
    payload = strict_json(match.group(1))
    if (len(payload) != 1 or payload[0]["status"] != 0
            or payload[0]["code"] != "cn_600000"):
        raise ValueError("unexpected Sohu identity/status")
    rows = []
    for fields in payload[0]["hq"]:
        if len(fields) != 10:
            raise ValueError("unexpected Sohu kline layout")
        rows.append({"date": fields[0], "open": fields[1], "close": fields[2],
                     "low": fields[5], "high": fields[6]})
    return checked_rows(rows)


def ratio_error_bound(raw, adjusted):
    """Exact Chebyshev lower bound for H=F*P, without estimating F/any anchor.

    If |H_i-F*P_i| <= e, pairwise intervals must intersect, giving
    e >= |H_i*P_j-H_j*P_i|/(P_i+P_j). No fitted factor is returned.
    """
    with localcontext() as context:
        context.prec = 50
        return max(abs(adjusted[i] * raw[j] - adjusted[j] * raw[i])
                   / (raw[i] + raw[j]) for i, j in itertools.combinations(FIELDS, 2))


def affine_error_bound(raw, adjusted, cash, effective_date):
    """Necessary collinearity bound for this one cash event, with no a/b fit.

    Under the conditional single-event cash-only model, x=P+D after ex-date,
    H=a*x+b. Every triple determinant must be zero before output rounding.
    This necessary check does not prove the model, anchor or event coverage.
    """
    points = [(raw[d][f] + (cash if d >= effective_date else Decimal(0)), adjusted[d][f])
              for d in DATES for f in FIELDS]
    with localcontext() as context:
        context.prec = 50
        bounds = [abs(h1 - h2) / 2 for (x1, h1), (x2, h2)
                  in itertools.combinations(points, 2) if x1 == x2]
        for (x1, h1), (x2, h2), (x3, h3) in itertools.combinations(points, 3):
            divisor = abs(x3 - x2) + abs(x1 - x3) + abs(x2 - x1)
            if divisor:
                determinant = h1 * (x3 - x2) + h2 * (x1 - x3) + h3 * (x2 - x1)
                bounds.append(abs(determinant) / divisor)
        return max(bounds, default=Decimal(0))


def read_bound(root, relative, digest):
    # Acquisition receipts preserve native Windows paths; replay also runs on Linux CI.
    path = safe_path(root / relative.replace("\\", "/"), root)
    content = path.read_bytes()
    if sha256(content) != digest:
        raise ValueError("evidence hash mismatch: " + relative)
    return content


def validate_acquisition(receipt, synthetic):
    """Bind the declared request, not external provenance or historical identity."""
    identity = receipt["id"]
    if synthetic:
        if (receipt.get("upstream_origin") != "LOCAL_GENERATED"
                or receipt.get("locator") != "synthetic://" + identity + "/sh600000"):
            raise ValueError("synthetic acquisition request must bind sh600000 locally: " + identity)
        return
    origin, host, path, query = REQUEST_CONTRACTS[identity]
    url = receipt.get("url")
    if not isinstance(url, str):
        raise ValueError("real acquisition request URL required: " + identity)
    parsed = urlsplit(url)
    parameters = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True) if parsed.query else {}
    if (receipt.get("upstream_origin") != origin or parsed.scheme != "https"
            or parsed.netloc != host or parsed.path != path or parsed.fragment
            or parameters != query):
        raise ValueError("acquisition request must match vendor, sh600000, dates and mode: " + identity)


def replay(root):
    root = safe_path(root)
    audit = strict_json((root / "initial-audit.json").read_bytes())
    synthetic = audit.get("synthetic_only")
    if not isinstance(synthetic, bool):
        raise ValueError("synthetic boundary must be explicit boolean")
    acquired = strict_json((root / "acquisition.json").read_bytes())
    review = strict_json((root / "official-review.json").read_bytes())
    old_sources = audit["copied_sources"]
    # Metadata classification is explicit at every level. It is not an attestation.
    for entry in [review] + list(old_sources.values()) + list(acquired):
        if not isinstance(entry, dict) or not isinstance(entry.get("synthetic_only"), bool):
            raise ValueError("every evidence record requires an explicit synthetic boolean")
        if entry["synthetic_only"] is not synthetic:
            raise ValueError("inconsistent synthetic evidence classification")
    expected_ids = {"tencent-raw", "tencent-hfq", "sohu-raw", "sina-factors"}
    acquisitions_by_id = {}
    for entry in acquired:
        identity = entry.get("id")
        if not isinstance(identity, str) or identity not in expected_ids:
            raise ValueError("unexpected acquisition ID")
        if identity in acquisitions_by_id:
            raise ValueError("duplicate acquisition ID: " + identity)
        validate_acquisition(entry, synthetic)
        acquisitions_by_id[identity] = entry
    if set(acquisitions_by_id) != expected_ids:
        raise ValueError("missing acquisition ID")
    def old(name):
        return read_bound(root, "sources/" + name, old_sources[name]["sha256"])
    def new(name):
        receipt = acquisitions_by_id[name]
        if receipt.get("http_status") != 200:
            raise ValueError("missing successful source: " + name)
        content = read_bound(root, receipt["path"], receipt["sha256"])
        if name == "sina-factors" and not re.match(
                r"\s*var\s+sh600000hfq\s*=", content.decode("utf-8-sig")):
            raise ValueError("Sina factor wrapper must bind sh600000")
        return content
    pdf = old("official-cninfo.pdf")
    if review["source_sha256"] != sha256(pdf) or review["status"] != "LOCAL_REVIEWED_SINGLE_NOTICE":
        raise ValueError("notice review binding differs")
    cash = number(review["cash_cny_per_old_share"])
    effective = review["ex_date"]
    if effective != "2025-07-16" or cash != Decimal("0.41"):
        raise ValueError("reviewed event differs from frozen pilot")
    source_day = old("tdx-600000.day")
    staged = pd.read_parquet(io.BytesIO(old("tdx-staging.parquet.bin")))
    if frame_source_sha256(staged) != sha256(source_day):
        raise ValueError("DAY/staging lossless roundtrip differs")
    encoded = np.frombuffer(source_day, dtype=DAY_DTYPE)
    rows = []
    for record in encoded:
        digits = str(int(record["date"]))
        day = digits[:4] + "-" + digits[4:6] + "-" + digits[6:]
        if day in DATES:
            rows.append(dict(date=day, **{f: str(Decimal(int(record[f])) / 100) for f in FIELDS}))
    raw = checked_rows(rows)
    refs = {"EASTMONEY": parse_eastmoney(old("eastmoney-raw.bin")),
            "TENCENT": parse_tencent(new("tencent-raw"), "raw"),
            "SOHU": parse_sohu(new("sohu-raw"))}
    quotes = {"EASTMONEY": parse_eastmoney(old("eastmoney-hfq.bin")),
              "TENCENT": parse_tencent(new("tencent-hfq"), "hfq")}
    raw_comparison = [{"date": d, "field": f, "tdx_candidate": str(raw[d][f]),
                       "vendor_values": {s: str(refs[s][d][f]) for s in refs},
                       "max_absolute_difference": str(max(abs(refs[s][d][f] - raw[d][f]) for s in refs)),
                       "status": "CROSS_VENDOR_MATCH" if all(refs[s][d][f] == raw[d][f] for s in refs)
                       else "CROSS_VENDOR_MISMATCH", "financial_admission": "UNVERIFIED"}
                      for d in DATES for f in FIELDS]
    factors = parse_sina_factors(new("sina-factors"))
    before = max((r for r in factors if r["date"] < effective and r["kind"] != "SENTINEL"),
                 key=lambda r: r["date"])
    after = next(r for r in factors if r["date"] == effective)
    with localcontext() as context:
        context.prec = 50
        close = raw["2025-07-15"]["close"]
        reference = close - cash
        theory = close / reference
        observed = Decimal(after["factor"]) / Decimal(before["factor"])
        step = {"raw_reference_close": str(close), "cash": str(cash),
                "theoretical_unrounded_ex_price": str(reference), "ratio_step_theory": str(theory),
                "sina_before": before, "sina_after": after, "sina_step_observed": str(observed),
                "absolute_step_difference": str(abs(theory - observed)),
                "affine_step": "a_new=a_old; b_new=b_old+0.41*a_old",
                "status": "OBSERVED_STEP_DIFFERENCE_ONLY_NOT_HFQ_LEVEL_CERTIFICATION"}
    diagnostics = []
    for vendor, adjusted in quotes.items():
        # These widths describe the response strings, NOT certified rounding rules.
        assumed_quantum = Decimal("0.01") if vendor == "EASTMONEY" else Decimal("0.001")
        ratio_bounds = {d: str(ratio_error_bound(raw[d], adjusted[d])) for d in DATES}
        affine_bound = affine_error_bound(raw, adjusted, cash, effective)
        diagnostics.append({"vendor": vendor, "assumed_nearest_quantum": str(assumed_quantum),
                            "rounding_contract": "UNVERIFIED", "ratio_error_lower_bounds": ratio_bounds,
                            "ratio_shape_under_hypothesis": "INCOMPATIBLE" if any(
                                Decimal(v) > assumed_quantum / 2 for v in ratio_bounds.values()) else "NOT_REJECTED",
                            "affine_error_lower_bound": str(affine_bound),
                            "affine_shape_under_hypothesis": "INCOMPATIBLE" if affine_bound > assumed_quantum / 2
                            else "NECESSARY_CONDITION_NOT_REJECTED",
                            "absolute_hfq_error": None, "relative_hfq_error": None,
                            "certification": "UNVERIFIED", "anchor_estimated": False})
    cached = pd.read_parquet(io.BytesIO(old("cached-nine.parquet.bin")))
    return {"version": VERSION, "implementation_sha256": implementation_sha256([__file__]),
            "tdx_parser_version": FORMAT_VERSION, "dates": list(DATES), "day_rows": len(encoded),
            "cache_rows": len(cached), "cache_upstream": "EASTMONEY", "cache_kind": "SDK_GENERATED_PARQUET",
            "original_sdk_response_bytes": "NOT_PRESENT_FOR_NINE_ROW_CACHE",
            "sources": {name: info["sha256"] for name, info in old_sources.items()},
            "new_sources": {identity: receipt["sha256"]
                            for identity, receipt in acquisitions_by_id.items()},
            "official_review_sha256": sha256((root / "official-review.json").read_bytes()),
            "raw_comparison": raw_comparison, "event_step": step, "algorithm_diagnostics": diagnostics,
            "computed_supplier_hfq": None, "certified_hfq_dates": [], "status": "BLOCKED",
            "financial_admission": "UNVERIFIED", "synthetic_only": synthetic,
            "history_kind": "SYNTHETIC_TEST_ONLY" if synthetic else "HISTORICALLY_RECONSTRUCTED",
            "blockers": ["PROVED_HISTORICAL_ANCHORS", "COMPLETE_EVENT_AND_REVISION_HISTORY",
                         "INDEPENDENT_SUPPLIER_ALGORITHM_AND_ROUNDING_CONTRACTS",
                         "BACKEND_SOURCE_LINEAGE", "CONTEMPORANEOUS_VINTAGES"],
            "production_gate_modified": False, "forward_enabled": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-root", required=True, type=Path)
    parser.add_argument("--output-root", required=True, type=Path)
    args = parser.parse_args()
    # Output is a new, separate directory; refuses any existing path.
    evidence, output = safe_path(args.evidence_root), safe_path(args.output_root)
    if output == evidence or output in evidence.parents or evidence in output.parents:
        raise ValueError("source/output overlap")
    result = replay(evidence)
    output.mkdir(parents=True, exist_ok=False)
    write_new(output / "review.json", (json.dumps(result, ensure_ascii=True, indent=2) + "\n").encode("ascii"))
    print(json.dumps({"status": result["status"], "certified_hfq_dates": [],
                      "raw_fields_checked": len(result["raw_comparison"]), "output": str(output)}))
    return 1  # A complete diagnostic report is not financial certification success.


if __name__ == "__main__":
    raise SystemExit(main())
