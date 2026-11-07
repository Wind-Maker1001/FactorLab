"""Recompute synthetic account evidence; optionally diagnose private CSMAR input.

Run with the project's dev dependencies from any cwd. Private source rows and
prices never appear in the output. No real-data return backtest is executed.
"""
import argparse
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT / "src"), str(ROOT)]

import pandas as pd

from tests.research.test_dividend_execution import execute, inputs, plan_for


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=True, default=str)+"\n", encoding="utf-8")


def synthetic(output):
    dates, bars, targets = inputs()
    result = execute(bars, targets, plan_for(output / "manual-input", dates))
    for key in ("nav", "trades", "entitlements", "recognitions", "payments"):
        result[key].to_csv(output / ("manual-"+key+".csv"), index=False)
    assert result["nav"].portfolio_value.tolist() == [10000]*7
    funding = []
    for label, pay, rate in [("paid_friday", "2024-01-05", "0"),
                              ("paid_monday", "2024-01-08", "0"),
                              ("paid_saturday", "2024-01-06", "0"),
                              ("unknown_tax", "2024-01-05", None)]:
        dates, bars, targets = inputs()
        bars.loc[:, ["raw_open", "raw_close", "raw_high", "raw_low"]] = 2.0
        targets.target_weight = [1, 0, float("nan"), 1, float("nan"), float("nan"), float("nan")]
        result = execute(bars, targets, plan_for(output / (label+"-input"), dates, pay=pay, rate=rate),
                         cash=200, first=False)
        for key in ("nav", "trades", "payments"):
            result[key].to_csv(output / (label+"-"+key+".csv"), index=False)
        buy = result["trades"].loc[result["trades"].side == "buy"].iloc[-1]
        funding.append(dict(case=label, actual_pay_date=pay, tax_rate=rate,
                            next_buy_date=str(buy.execution_date.date()), shares=int(buy.executed_shares)))
    write_json(output / "funding-summary.json", funding)
    # Also exercise a causal MA entry before record close, so both the strategy
    # and benchmark independently earn the dividend through the same adapter.
    from factorlab.strategies.dual_ma import DualMAConfig, run_integrated_software_backtest
    from tests.research.test_dividend_execution import SYMBOL, fees
    dates, bars, _ = inputs()
    warmup = pd.bdate_range("2023-12-26", "2024-01-01")
    prefix = pd.concat([bars.iloc[[0]].assign(date=day) for day in warmup], ignore_index=True)
    bars = pd.concat([prefix, bars.iloc[1:]], ignore_index=True)
    bars["adj_close"] = [100, 100, 100, 100, 90, 110, 110, 110, 90, 90, 110, 110]
    config = DualMAConfig(symbol=SYMBOL, start=str(dates[0].date()), end=str(dates[-1].date()),
                          short_window=1, long_window=2, initial_cash=10000)
    result = run_integrated_software_backtest(bars, bars.date, plan_for(output / "dual-input", dates),
                                             config, fee_schedule=fees())
    result["signals"].to_csv(output / "dual-signals.csv", index=False)
    for leg in ("strategy", "benchmark"):
        assert result[leg]["entitlements"].eligible_shares.tolist() == [100]
        assert result[leg]["payments"].gross.tolist() == [200]
        for key in ("nav", "trades", "entitlements", "payments"):
            result[leg][key].to_csv(output / ("dual-"+leg+"-"+key+".csv"), index=False)


def real_diagnostics(output, candidate, source_root):
    from pypdf import PdfReader
    from factorlab.data.csmar.binding import replay
    from factorlab.data.csmar.prepare import action_candidates
    from factorlab.data.corporate_actions.validation import event_terms
    from factorlab.errors import DataQualityError, SchemaError
    from factorlab.strategies.dual_ma import DualMAConfig, moving_average_signals, run_software_backtest

    prepared = candidate / "prepared-v4"
    manifest = json.loads((prepared / "manifest.json").read_text())
    for filename, expected in manifest["files"].items():
        assert hashlib.sha256((prepared / filename).read_bytes()).hexdigest() == expected
    refs = json.loads((prepared / "provenance/references.json").read_text())
    ref = next(r for r in refs if r["family"] == "TRDNEW_Cptl")
    scratch = output / "private-replay-scratch"
    scratch.mkdir(exist_ok=True)
    projection = replay(ref, source_root=source_root, scratch=scratch)
    originals = action_candidates(projection, "601857.SH", manifest["observed_at"])
    originals = {a["event"]["effective_date"]: a for a in originals}
    archived = json.loads((candidate / "evidence/corroborated-events.json").read_text())
    summaries = []
    for bound in archived:
        notice = bound["official_evidence"]
        original = originals[notice["effective_date"]]
        payload = original["event"]
        assert payload["event_id"] == bound["event"]["event_id"]
        assert original["origin"] == bound["origin"]
        assert payload["record_date"] is None and payload["status"] == "PROPOSED"
        assert Decimal(payload["cash"]) == Decimal(notice["cash_per_share"])
        pdf = candidate / "evidence" / ("official-"+notice["name"]+".pdf")
        digest = hashlib.sha256(pdf.read_bytes()).hexdigest()
        assert digest == notice["sha256"]
        text = "".join("".join(page.extract_text().split()) for page in PdfReader(pdf).pages)
        assert notice["cash_per_share"] in text
        for field in ("record_date", "effective_date", "payment_date"):
            day = pd.Timestamp(notice[field])
            variants = [f"{day.year}/{day.month}/{day.day}", f"{day.year}年{day.month}月{day.day}日"]
            assert any(v in text for v in variants), (pdf.name, field)
        try:
            from factorlab.data.corporate_actions.contracts import CorporateAction
            event_terms(CorporateAction(**payload))
        except DataQualityError as exc:
            refusal = str(exc)
        else:
            raise AssertionError("PROPOSED candidate unexpectedly executable")
        summaries.append(dict(event_id=payload["event_id"], revision=payload["revision"],
            source_version=original["source_version"], source_sha256=payload["source_sha256"],
            member_sha256=original["origin"]["member_sha256"], row_number=original["origin"]["row_number"],
            official_sha256=digest, official_url=notice["url"],
            public_terms={k:notice[k] for k in ("record_date", "effective_date", "payment_date", "cash_per_share")},
            record_date_source="official implementation notice; original CSMAR candidate remains missing",
            announced_native=notice["native_announcement_date"], announced_document=notice["notice_document_date"],
            date_conflict=notice["date_discrepancy"], retrieved_at=notice["retrieved_at"],
            available_at=payload["available_at"], status=payload["status"], refusal=refusal))
    bars = pd.read_parquet(prepared / "market/bars.parquet")
    signals = moving_average_signals(bars, bars.date, DualMAConfig())
    try:
        run_software_backtest(bars, bars.date, DualMAConfig())
    except SchemaError as exc:
        legacy_refusal = str(exc)
    else:
        raise AssertionError("old runner unexpectedly accepted variable adjustment ratio")
    report = dict(schema="P2_DATA_02_PRIVATE_BINDING_DIAGNOSTIC", real_return_backtest="BLOCKED / NOT_RUN",
        source_root_read_only=True, manifest_sha256=hashlib.sha256((prepared / "manifest.json").read_bytes()).hexdigest(),
        candidate_file_hashes_verified=len(manifest["files"]), original_replay="PASS", events=summaries,
        warmup_records=int((bars.date < "2018-01-01").sum()), research_records=len(signals),
        gold_crosses=int(signals.signal.eq("buy").sum()), death_crosses=int(signals.signal.eq("sell").sum()),
        action_coverage="PARTIAL; no complete range evidence", old_runner_refusal=legacy_refusal,
        tax_status="UNKNOWN", hfq_status="HFQ_UNVERIFIED", pit_status="PIT_UNVERIFIED",
        formal_admission="FORMAL_ADMISSION_BLOCKED", no_live_money=True)
    write_json(output / "real-2018-diagnostic.json", report)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--candidate", type=Path)
    parser.add_argument("--source-root", type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    if args.candidate is not None:
        if args.source_root is None:
            parser.error("--candidate requires --source-root")
        paths = [args.output.resolve(), args.candidate.resolve(), args.source_root.resolve()]
        if any(paths[0] == p or p in paths[0].parents for p in paths[1:]):
            parser.error("output must be outside private candidate and source roots")
    synthetic(args.output)
    if args.candidate is not None:
        real_diagnostics(args.output, args.candidate, args.source_root)
    print("SOFTWARE_ONLY evidence saved:", args.output.resolve())


if __name__ == "__main__":
    main()
