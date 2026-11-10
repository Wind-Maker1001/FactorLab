"""Read only the two P9-D01 securities; never fit models or execute orders."""
import argparse
import io
import json
from pathlib import Path
import sys
import zipfile

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from factorlab.data.evidence import fail, json_bytes, sha256
from factorlab.data.market_evidence_sidecar import ALLOWED, SAFETY, SUSPENSION_DATES, load_sidecar
from factorlab.backtest.rules import PriceLimitSchedule


def inspect_workbook(path, vendor):
    """Read one code-sorted target shard, stopping after the last target code."""
    from openpyxl import load_workbook
    member = "TRDNEW_Dalyr.xlsx" if vendor == "CSMAR" else "2023/个股日回报率-2023_1.xlsx"
    with zipfile.ZipFile(path) as archive:
        content = archive.read(member)
    workbook = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    sheet = workbook.active
    # CSMAR's declared dimension ends at A although rows contain A:AA.
    width = 27 if vendor == "CSMAR" else 19
    selected, scanned = [], 0
    for row in sheet.iter_rows(min_row=4 if vendor == "CSMAR" else 3, max_col=width, values_only=True):
        scanned += 1
        code = str(row[0]).zfill(6)
        if code > "002408":
            break
        day = str(row[1 if vendor == "CSMAR" else 2])[:10]
        if ((code == "002408" and "2023-04-25" <= day <= "2023-05-05")
                or (code == "000540" and day in ("2023-05-05", "2023-05-08"))):
            selected.append((day, row))
    workbook.close()
    summary = {"source": vendor, "member": member, "sha256": sha256(content),
               "scanned_prefix_rows": scanned, "target_rows": len(selected)}
    if vendor == "CSMAR":
        suspension = [r for d, r in selected if str(r[0]) == "002408" and d in SUSPENSION_DATES]
        if len(suspension) != 4 or any(str(r[4]) != "1" or float(r[9]) != 0 for r in suspension):
            fail("CSMAR suspension fill evidence differs")
        target = [r for d, r in selected if str(r[0]) == "000540" and d == "2023-05-08"]
        if (len(target) != 1 or float(target[0][3]) != 3 or str(target[0][4]) != "0"
                or any(float(target[0][i]) != .6 for i in (5, 6, 7, 8))):
            fail("CSMAR target ST/OHLC evidence differs")
        summary.update(suspension_filled_rows=4, target_st_status=3,
                       may05_st_conflict="CSMAR_1_VERSUS_ISSUER_EFFECTIVE_STAR_ST")
    else:
        suspension = [r for d, r in selected if str(r[0]) == "002408" and d in SUSPENSION_DATES]
        if (len(suspension) != 4 or any(r[12] not in (None, "") for r in suspension)
                or any(float(r[i]) != 7.12 for r in suspension for i in (3, 4, 5, 6))):
            fail("CNRDS repeated-price/no-volume evidence differs")
        summary.update(target_000540_rows=sum(str(r[0]) == "000540" for _, r in selected),
                       suspension_volume="EMPTY_NOT_ZERO; NOT_OBSERVED_TRADES")
    return summary


def verify(args):
    config = json.loads(args.sidecar.read_bytes())
    manifest = args.manifest.read_bytes()
    if sha256(manifest) != config["source_manifest_sha256"]:
        fail("source manifest identity differs")
    entries = {e["member"]: e for e in json.loads(manifest)["entries"]}
    frames, documents, identities = {}, {}, []
    with zipfile.ZipFile(args.stock) as archive:
        for table, columns in {
            "cn_stock_real_bar1d": ["date", "instrument", "open", "high", "low", "close", "pre_close", "volume", "lower_limit"],
            "cn_stock_status": ["date", "instrument", "suspended", "st_status", "is_risk_warning"],
        }.items():
            parts = []
            for month, end in (("04", "30"), ("05", "31")):
                member = f"stock/{table}/2023-{month}-01_2023-{month}-{end}.parquet"
                content = archive.read(member)
                digest = sha256(content)
                receipt = json.loads(archive.read(member + ".receipt.json"))
                if digest != entries[member]["actual_sha256"] or digest != receipt["sha256"]:
                    fail("target shard differs from frozen manifest/receipt")
                identities.append({"member": member, "sha256": digest})
                documents[digest] = content
                frame = pd.read_parquet(io.BytesIO(content), columns=columns,
                    filters=[("instrument", "in", ["002408.SZ", "000540.SZ"]),
                             ("date", ">=", pd.Timestamp("2023-04-25")),
                             ("date", "<=", pd.Timestamp("2023-05-09"))])
                frame["source_sha256"] = digest
                parts.append(frame)
            frames[table] = pd.concat(parts).set_index(["instrument", "date"])
    prices, states = frames["cn_stock_real_bar1d"], frames["cn_stock_status"]
    sources = json.loads(args.sources.read_bytes())
    for source in sources["documents"]:
        content = (args.source_root / source["local_name"]).read_bytes()
        if sha256(content) != source["sha256"]:
            fail("public document identity differs")
        documents[source["sha256"]] = content
    old = prices.loc[("002408.SZ", pd.Timestamp("2023-04-25"))]
    observation = {"security": "002408.SZ", "date": "2023-04-25", "field": "raw_close",
        "status": "OBSERVED_RAW_CLOSE", "price": str(old["close"]),
        "volume": int(old["volume"]), "source_sha256": old.source_sha256}
    documents[sha256(json_bytes(observation))] = json_bytes(observation)
    originals = {key: None for key in ALLOWED}
    # Preserve the adapter's UNKNOWN rather than interpreting enum 2 generically.
    native_st = states.loc[("000540.SZ", pd.Timestamp("2023-05-08")), "st_status"]
    for field in ("st_status", "suspended", "is_risk_warning"):
        if states.loc[("000540.SZ", pd.Timestamp("2023-05-08")), field] != config["original_supplier_state"][field]:
            fail("original supplier state differs: " + field)
    evidence = load_sidecar(args.sidecar, source_manifest=manifest, originals=originals, documents=documents)
    sensitivity = []
    frozen_bytes = args.frozen_result.read_bytes().replace(b"\r\n", b"\n")
    if sha256(frozen_bytes) != config["frozen_result_lf_sha256"]:
        fail("frozen P9 account identity report differs")
    frozen = json.loads(frozen_bytes)["private_artifact_sha256"]
    for model in ("B_factor_score", "B_ridge"):
        account = model + "-fold-0"
        position_path = args.study / account / "positions.parquet"
        nav_path = args.study / account / "nav.parquet"
        for name, path in (("positions.parquet", position_path), ("nav.parquet", nav_path)):
            if sha256(path.read_bytes()) != frozen[account + "/" + name]:
                fail("saved account identity differs")
        positions = pd.read_parquet(position_path, filters=[("symbol", "==", "002408.SZ"),
            ("date", ">=", pd.Timestamp("2023-04-26")), ("date", "<=", pd.Timestamp("2023-05-04"))]).set_index("date")
        nav = pd.read_parquet(nav_path, columns=["date", "portfolio_value"], filters=[
            ("date", ">=", pd.Timestamp("2023-04-26")), ("date", "<=", pd.Timestamp("2023-05-04"))]).set_index("date")
        for day in SUSPENSION_DATES:
            key = ("002408.SZ", pd.Timestamp(day))
            price, state = prices.loc[key], states.loc[key]
            if not all(pd.isna(price[c]) for c in ("open", "high", "low", "close")) or price.volume != 0 or state.suspended != 1:
                fail("suspension price/status conflicts")
            holding = positions.loc[pd.Timestamp(day)]
            mark = evidence.carry("002408.SZ", day, shares=int(holding.shares), raw_close=None,
                                  previous_observation=observation)
            equity = float(nav.loc[pd.Timestamp(day), "portfolio_value"])
            sensitivity.append({"model": model, "date": day, "shares": mark.shares,
                "valuation_price": str(mark.valuation_price), "book_value": str(mark.book_value),
                "affected_equity_fraction": float(mark.book_value) / equity,
                "old_mark_equal": float(mark.valuation_price) == float(holding.mark_price),
                "equity_basis": "SAVED_DIAGNOSTIC_EQUITY_WITH_CARRY_ASSUMPTION",
                "valuation_status": mark.valuation_status})
    resumed = prices.loc[("002408.SZ", pd.Timestamp("2023-05-05"))]
    if resumed.volume <= 0 or pd.isna(resumed.close):
        fail("resumption observed price missing")
    target = prices.loc[("000540.SZ", pd.Timestamp("2023-05-08"))]
    previous = prices.loc[("000540.SZ", pd.Timestamp("2023-05-05"))]
    evidence.fact("000540.SZ", "2023-05-08", "is_st", purpose="SIGNAL_STATE", cutoff="2023-05-05T15:00:00+08:00")
    if float(previous.close) != float(target.pre_close):
        fail("previous close conflict")
    blocked, bounds = PriceLimitSchedule.a_share_default().blocks("000540.SZ", "2023-05-08", "sell",
        float(target.open), float(previous.close), is_st=True, tolerance_bps=5)
    if bounds.lower != float(target.lower_limit):
        fail("issuer-rule lower bound conflicts with source")
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(inspect_workbook, path, vendor)
                   for vendor, path in (("CSMAR", args.csmar), ("CNRDS", args.cnrds)) if path is not None]
        ancillary = [future.result() for future in futures]
    for item in ancillary:
        if item["sha256"] != config["ancillary_sha256"][item["source"]]:
            fail("ancillary source shard identity differs")
    result = {"ancillary": ancillary, "schema": "P9_D01_VERIFICATION_V1", "status": "P9_D01_PARTIAL", "safety": list(SAFETY),
        "source_manifest_sha256": evidence.source_manifest_sha256, "shards": identities,
        "suspension_sessions": len(SUSPENSION_DATES), "sensitivity": sensitivity,
        "resumption": {"date": "2023-05-05", "status": "OBSERVED_RAW_CLOSE", "close": float(resumed.close)},
        "000540": {"original_st_status": int(native_st), "is_st": "SIGNAL_TIME_STATE_KNOWN",
            "trading_observed": "EX_POST_TRADING_CONFIRMED", "price": "EXECUTION_PRICE_AVAILABLE",
            "intraday_interruption": "UNKNOWN", "signal_time_trading_state": "UNKNOWN",
            "order_executability": "ORDER_EXECUTABLE_UNVERIFIED", "existing_rule_sell_blocked": bool(blocked),
            "rule_lower_bound": bounds.lower, "execution_permit": False}, "formal_admission": False}
    args.output.mkdir(parents=True, exist_ok=False)
    (args.output / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": result["status"], "sensitivity_rows": len(sensitivity), "sell_blocked": bool(blocked)}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("sidecar", "sources", "source-root", "manifest", "stock", "study", "frozen-result", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--csmar", type=Path, help="Existing 2023 daily-return ZIP only")
    parser.add_argument("--cnrds", type=Path, help="Existing CNSP ZIP; only first 2023 shard is read")
    verify(parser.parse_args())


if __name__ == "__main__":
    main()
