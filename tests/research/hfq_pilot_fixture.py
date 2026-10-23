"""Generate fictional parser fixtures; no vendor bytes or historical prices.

The symbol/date/notice shapes exercise the bounded pilot contract only.
Every artifact is locally generated and must never count as market evidence.
"""

from decimal import Decimal, localcontext
import io
import json

import numpy as np
import pandas as pd

from factorlab.data.evidence import sha256
from factorlab.data.tdx import DAY_DTYPE


FIXTURE_DATES = ("2025-07-09", "2025-07-10", "2025-07-11", "2025-07-14",
                 "2025-07-15", "2025-07-16", "2025-07-17", "2025-07-18")
REAL_REQUESTS = {
    "tencent-raw": ("TENCENT", "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=sh600000,day,2025-07-09,2025-07-18,8,"),
    "tencent-hfq": ("TENCENT", "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=sh600000,day,2025-07-09,2025-07-18,8,hfq"),
    "sohu-raw": ("SOHU", "https://q.stock.sohu.com/hisHq?code=cn_600000&start=20250709&end=20250718&stat=1&order=D&period=d&callback=historySearchHandler&rt=jsonp"),
    "sina-factors": ("SINA", "https://finance.sina.com.cn/realstock/company/sh600000/hfq.js"),
}


def make_evidence(root):
    root.mkdir()
    sources = root / "sources"
    sources.mkdir()
    copied = {}
    acquisitions = []

    def put(name, content, source_id=None):
        (sources / name).write_bytes(content)
        info = {"sha256": sha256(content), "bytes": len(content), "synthetic_only": True}
        if source_id is None:
            copied[name] = info
        else:
            locator = "synthetic://" + source_id + "/sh600000"
            acquisitions.append(dict(info, id=source_id, path="sources\\" + name,
                                     http_status=200, locator=locator, upstream_origin="LOCAL_GENERATED"))
        return info["sha256"]

    def encoded(value):
        return json.dumps(value, ensure_ascii=True).encode("ascii")

    prices = []
    records = np.zeros(8, dtype=DAY_DTYPE)
    for i, day in enumerate(FIXTURE_DATES):
        row = {"open": Decimal(100), "high": Decimal(102),
               "low": Decimal(98), "close": Decimal(100)}
        prices.append(row)
        records[i]["date"] = int(day.replace("-", ""))
        for field, value in row.items():
            records[i][field] = int(value * 100)
        records[i]["amount"] = 100000
        records[i]["volume"] = 1000
    put("tdx-600000.day", records.tobytes())
    staged = pd.DataFrame({"tdx_date_integer": records["date"], "volume": records["volume"],
                           "amount": records["amount"], "tdx_reserved": records["reserved"]})
    for field in ("open", "high", "low", "close"):
        staged["tdx_" + field + "_integer"] = records[field]
    stream = io.BytesIO()
    staged.to_parquet(stream, index=False)
    put("tdx-staging.parquet.bin", stream.getvalue())
    stream = io.BytesIO()
    pd.DataFrame({"synthetic_only": [True] * 9}).to_parquet(stream, index=False)
    put("cached-nine.parquet.bin", stream.getvalue())
    pdf_hash = put("official-cninfo.pdf", b"SYNTHETIC TEST NOTICE. NOT AN ISSUER DOCUMENT.")
    em_raw, em_hfq, qq_raw, qq_hfq, sohu = [], [], [], [], []
    for day, row in zip(FIXTURE_DATES, prices):
        ordered = [row[f] for f in ("open", "close", "high", "low")]
        shift = Decimal("0.41") if day >= "2025-07-16" else Decimal(0)
        em_raw.append(",".join([day] + [str(p) for p in ordered] + ["0"] * 6))
        em_hfq.append(",".join([day] + [str(2 * (p + shift) + 3) for p in ordered] + ["0"] * 6))
        qq_raw.append([day] + [str(p) for p in ordered] + ["0"])
        qq_hfq.append([day] + [str(3 * (p + shift) + 7) for p in ordered] + ["0"])
        sohu.append([day, str(row["open"]), str(row["close"]), "0", "0%",
                     str(row["low"]), str(row["high"]), "0", "0", "0%"])
    def em(rows):
        return encoded({"rc": 0, "data": {"code": "600000", "market": 1, "decimal": 2,
                                           "klines": rows}, "synthetic_only": True})
    put("eastmoney-raw.bin", em(em_raw))
    put("eastmoney-hfq.bin", em(em_hfq))
    for mode, rows in (("raw", qq_raw), ("hfq", qq_hfq)):
        key = "day" if mode == "raw" else "hfqday"
        put("tencent-" + mode + ".bin", encoded({"code": 0, "synthetic_only": True,
                                                "data": {"sh600000": {key: rows}}}), "tencent-" + mode)
    put("sohu-raw.bin", b"historySearchHandler(" + encoded([
        {"status": 0, "code": "cn_600000", "hq": sohu, "synthetic_only": True}]) + b")", "sohu-raw")
    with localcontext() as context:
        context.prec = 50
        after = str(Decimal(2) * Decimal(100) / Decimal("99.59"))
    put("sina-factors.bin", b"var sh600000hfq=" + encoded({"total": 3, "data": [
        {"d": "2024-07-18", "f": "2"}, {"d": "2025-07-16", "f": after},
        {"d": "1900-01-01", "f": "1"}]}) + b";", "sina-factors")
    (root / "initial-audit.json").write_bytes(encoded({"synthetic_only": True, "copied_sources": copied}))
    (root / "acquisition.json").write_bytes(encoded(acquisitions))
    (root / "official-review.json").write_bytes(encoded({
        "synthetic_only": True, "source_sha256": pdf_hash, "status": "LOCAL_REVIEWED_SINGLE_NOTICE",
        "cash_cny_per_old_share": "0.41", "ex_date": "2025-07-16"}))
    return root


def malicious_real_metadata(root, receipts):
    """Deliberate relabeling for negative identity tests; never a real fixture."""
    for name in ("initial-audit.json", "official-review.json"):
        path = root / name
        payload = json.loads(path.read_bytes())
        payload["synthetic_only"] = False
        for source in payload.get("copied_sources", {}).values():
            source["synthetic_only"] = False
        path.write_text(json.dumps(payload), encoding="utf-8")
    for entry in receipts:
        entry["synthetic_only"] = False
        entry["upstream_origin"], entry["url"] = REAL_REQUESTS[entry["id"]]
