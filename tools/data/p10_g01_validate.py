"""Bounded native-case validation of the scoped price-consumption adapter."""
import argparse
from dataclasses import replace
from decimal import Decimal, ROUND_HALF_UP
import json
from pathlib import Path
import sys
import time
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np
import pandas as pd
import pyarrow as pa

from factorlab.backtest.execution import ExecutionBacktestConfig, AShareExecutionBacktester
from factorlab.data.hub.contracts import QueryResult, SourceMetadata
from factorlab.data.hub.price_consumption import (
    SCHEMA, PriceConsumptionError, compute_factors, execution_bars, raw_forward_returns, require_fields,
)
from factorlab.dataset.identity import json_hash
from factorlab.exploratory.factor_discovery.contracts import FIELDS, Node
from factorlab.exploratory.factor_discovery.engine import compile_expression
from factorlab.factors.registry_v2 import FactorDefinition, FactorRegistrySnapshot
from factorlab.research.price_inputs import run_execution
from p10_d02_price_basis_cases import rows
from tri_source_market_audit import memory

SAFETY = ["NO LIVE MONEY", "PIT_UNVERIFIED", "HFQ_UNVERIFIED", "FORMAL_ADMISSION_BLOCKED"]


def dump(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def wrap(native, source, code, months, receipts, basis, *, qualification="UNKNOWN", events=()):
    frame = rows(native, source, code, months, receipts).reset_index()
    frame["symbol"] = code + (".SH" if code.startswith("6") else ".SZ")
    frame["date"] = pd.to_datetime(frame.date)
    pins = sorted({frame_receipt for frame_receipt in frame.receipt})
    version = json_hash([{key: receipts[name][key] for key in ("source", "member", "sha256", "dictionary_sha256")}
                         for name in pins])
    semantics = {}
    for field in ("open", "high", "low", "close"):
        frame["raw_"+field] = frame[field]
        semantics["raw_"+field] = {"consumption": dict(
            schema=SCHEMA, field="raw_"+field, source_id=source, data_version=version,
            basis=basis, qualification=qualification, unit="CNY/share",
            adjustment_policy="NONE" if basis == "OBSERVED_RAW" else "NEXT_CASH_CANDIDATE" if events else "UNKNOWN",
            adjustment_event_dates=list(events), available_at=None,
            purposes=["RAW_LABEL", "RAW_FACTOR", "EXECUTION"] if qualification == "RESEARCH_QUALIFIED" else [],
            reasons=[] if qualification == "RESEARCH_QUALIFIED" else ["SUPPLIER_PRICE_POLICY_UNCLOSED"],
            evidence=[receipts[name]["sha256"] for name in pins],
            scope=[dict(symbol=frame.symbol.iloc[0], start=str(frame.date.min().date()), end=str(frame.date.max().date()))])}
    metadata = SourceMetadata(source, version, field_semantics=semantics,
                              provenance={"native_receipts": pins, "qualification_scope": "RETROSPECTIVE_RESEARCH_ONLY"})
    return QueryResult(frame, metadata), (source, version)


def rejected(operation):
    try:
        operation()
    except PriceConsumptionError as exc:
        return list(exc.gaps)
    raise AssertionError("expected price-consumption rejection")


def run(args):
    started = time.perf_counter()
    pa.set_cpu_count(1)
    pa.set_io_thread_count(1)
    receipts = {p.stem: json.loads(p.read_text(encoding="utf-8"))
                for p in args.native.glob("*-*.json") if p.name.startswith(("CSMAR-", "CNRDS-"))}
    cases = []
    events = json.loads((args.work / "targeted-distribution-native-rows.json").read_text(encoding="utf-8"))
    windows = {"600565": "2019-05", "600578": "2019-07", "300682": "2018-12"}
    for event in events:
        record = event["record"]
        code, exdate = record["Stkcd"], record["Exdistdt"]
        window = windows[code]
        months = sorted({window, exdate[:7]})
        cs = rows(args.native, "CSMAR", code, months, receipts)
        cn = rows(args.native, "CNRDS", code, [window], receipts)
        before = cs.loc[cs.index < exdate].iloc[-1]
        factor = 1 - Decimal(record["Amount"]) / Decimal(before.close)
        sample = cs.loc[cs.index.str.startswith(window)]
        matches = differences = tested = 0
        for day in sample.index.intersection(cn.index):
            for field in ("open", "high", "low", "close"):
                left, right = Decimal(sample.at[day, field]), Decimal(cn.at[day, field])
                tested += 1
                differences += left != right
                matches += (left*factor).quantize(Decimal(".01"), rounding=ROUND_HALF_UP) == right
        candidate, identity = wrap(args.native, "CNRDS", code, [window], receipts,
                                   "ADJUSTED_CANDIDATE", events=[exdate])
        gaps = rejected(lambda: raw_forward_returns(candidate, identity=identity, calendar=candidate.data.date))
        future = rejected(lambda: require_fields(candidate, {"raw_open": ("RAW_LABEL", "CNY/share")},
                                               identity=identity, cutoff=window+"-28"))
        cases.append(dict(symbol=code, window=window, mode="TARGETED_NATIVE_CHECKPOINT_RECOMPUTED",
                          tested_fields=tested, raw_differences=differences, matched_cash_formula=matches,
                          formula_residual=tested-matches, event_date=exdate, raw_label_rejection=gaps,
                          historical_cutoff_rejection=future, candidate_status="UNKNOWN",
                          source_version=identity[1]))
    # The known six filled days are case selection, not production qualification rules.
    filled_dates = ["2020-10-30", "2020-11-02", "2020-11-03", "2020-11-04", "2020-11-05", "2020-11-06"]
    cs, identity = wrap(args.native, "CSMAR", "688189", ["2020-10", "2020-11"], receipts, "SUPPLIER_FILLED")
    cn, _ = wrap(args.native, "CNRDS", "688189", ["2020-10", "2020-11"], receipts, "UNKNOWN")
    cs = replace(cs, data=cs.data.loc[cs.data.date.isin(pd.to_datetime(filled_dates))].copy())
    cn = replace(cn, data=cn.data.loc[cn.data.date.isin(pd.to_datetime(filled_dates))].copy())
    assert len(cs.data) == len(cn.data) == 6
    assert cs.data.filling.eq("1").all()
    assert cs.data.volume.map(Decimal).eq(0).all() and cn.data.volume.map(Decimal).eq(0).all()
    merged = cs.data.merge(cn.data, on="date", suffixes=("_cs", "_cn"), validate="one_to_one")
    assert all(Decimal(a) == Decimal(b) for a, b in zip(merged.close_cs, merged.close_cn))
    shape_differences = sum(Decimal(a) != Decimal(b) for f in ("open", "high", "low")
                            for a, b in zip(merged[f+"_cs"], merged[f+"_cn"]))
    assert shape_differences == 18
    cases.append(dict(symbol="688189.SH", dates=filled_dates, shape_differences=shape_differences,
                      mode="SIX_NATIVE_DAY_PAIRS_RECOMPUTED", source_version=identity[1],
                      execution_rejection=rejected(lambda: execution_bars(cs, identity=identity, config=ExecutionBacktestConfig()))))
    months = [f"{y}-{m:02}" for y in (2017, 2018) for m in range(1, 13)]
    pilot, identity = wrap(args.native, "CSMAR", "601857", months, receipts,
                           "OBSERVED_RAW", qualification="RESEARCH_QUALIFIED")
    other, _ = wrap(args.native, "CNRDS", "601857", months, receipts, "UNKNOWN")
    frame = pilot.data.sort_values("date").reset_index(drop=True)
    other_frame = other.data.set_index("date")
    exact = sum(Decimal(row[field]) == Decimal(other_frame.at[row["date"], field])
                for row in frame.to_dict("records") for field in ("open", "high", "low", "close", "volume", "amount"))
    assert len(frame) == 487 and exact == 2922 and frame.filling.eq("0").all()
    for f in ("raw_open", "raw_high", "raw_low", "raw_close", "volume", "amount"):
        frame[f] = pd.to_numeric(frame[f], errors="raise")
    pilot = replace(pilot, data=frame)
    dates = pd.DatetimeIndex(frame.date)
    labels = raw_forward_returns(pilot, identity=identity, calendar=dates)
    old = frame.raw_open.shift(-6)/frame.raw_open.shift(-1)-1
    np.testing.assert_allclose(labels.raw_forward_return, old, atol=1e-15, rtol=0, equal_nan=True)
    # Same engines and identities, with and without the scoped entry check.
    for f in FIELDS:
        if f != "RET1":
            frame[f] = pd.to_numeric(frame[f].replace("", np.nan), errors="raise") if f in frame else np.nan
    close = Node("Field", value="raw_close")
    definition = FactorDefinition.from_expression("RAW_RETURN1", "1", compile_expression(
        Node("SafeDiv", (close, Node("Lag", (close,), param=1)))))
    snapshot = FactorRegistrySnapshot((definition,))
    guarded = compute_factors(pilot, snapshot, (definition.ref,), identity=identity,
                               calendar=dates, units={"raw_close": "CNY/share"})
    baseline = snapshot.compute(frame, (definition.ref,), calendar=dates)
    pd.testing.assert_frame_equal(guarded, baseline, check_exact=True)
    # Use the unmodified observed raw fields; no synthetic fills or carry patches.
    bars = frame[["date", "symbol", "raw_open", "raw_close", "volume"]].copy()
    bars["trade_status"] = frame.filling.eq("0") & frame.volume.gt(0)
    bars["is_st"] = pd.to_numeric(frame.trade_code, errors="raise").isin([2, 3, 5, 6, 8, 9, 11, 12, 14, 15])
    market = replace(pilot, data=bars)
    config = ExecutionBacktestConfig(top_k=1, minimum_cross_section=1,
                                    benchmark_open_price_column="raw_open", benchmark_close_price_column="raw_close")
    benchmark = bars[["date", "raw_open", "raw_close"]].copy()
    predictions = bars[["date", "symbol"]].copy()
    predictions["score"] = 1.
    engine = AShareExecutionBacktester(config)
    unguarded_result = engine.run(predictions, bars, benchmark)
    guarded_result = run_execution(engine, predictions, market, benchmark, identity=identity)
    for name in ("nav", "trades", "positions", "rejections", "signals"):
        pd.testing.assert_frame_equal(getattr(guarded_result, name), getattr(unguarded_result, name), check_exact=True)
    drift = replace(pilot, metadata=replace(pilot.metadata, data_version="different-vintage"))
    unknown = replace(pilot, metadata=SourceMetadata("BigQuant", "unclosed-units"))
    cases.append(dict(symbol="601857.SH", window="2017-01-01/2018-12-31",
                      mode="NATIVE_CHECKPOINT_RECOMPUTED", keys=len(frame), exact_six_fields=exact,
                      labels_compared=int(labels.raw_forward_return.notna().sum()),
                      raw_label_compatibility="SAME_WITH_DECIMAL_ARITHMETIC_FLOAT_OUTPUT",
                      raw_factor_compatibility="EXACT", execution_compatibility="EXACT",
                      source_version=identity[1],
                      drift_rejection=rejected(lambda: require_fields(drift, {"raw_open": ("RAW_LABEL", "CNY/share")}, identity=identity)),
                      unknown_rejection=rejected(lambda: require_fields(unknown, {"raw_open": ("RAW_LABEL", "CNY/share")}, identity=("BigQuant", "unclosed-units")))))
    assert [c["matched_cash_formula"] for c in cases[:3]] == [74, 80, 92]
    args.output.mkdir(parents=True, exist_ok=True)
    dump(args.output / "validation-cases.json", {"safety": SAFETY, "cases": cases})
    dump(args.output / "compatibility-results.json", dict(
        raw_labels=dict(rows=len(frame), evaluable=int(labels.raw_forward_return.notna().sum()), maximum_absolute_tolerance=1e-15),
        factor=dict(definition_hash=definition.definition_hash, comparison="EXACT"),
        backtest=dict(comparison="EXACT", sessions=len(guarded_result.nav), trades=len(guarded_result.trades)),
        qualification="SCOPED_RETROSPECTIVE_RESEARCH_ONLY", legacy_dataframe_apis="UNCHANGED_UNGUARDED",
        safety=SAFETY))
    tests = None
    if args.tests_xml is not None:
        suites = ET.parse(args.tests_xml).getroot().findall("testsuite")
        tests = {name: sum(int(s.attrib[name]) for s in suites)
                 for name in ("tests", "failures", "errors", "skipped")}
        assert tests["tests"] > 0 and tests["failures"] == tests["errors"] == tests["skipped"] == 0
    dump(args.output / "verification.json", dict(
        schema="P10_G01_VERIFICATION_V1", base_commit="0cf5404510b497cf2ad0d974d21a04119219a0df",
        native_validation="PASS", seconds=time.perf_counter()-started, processes=1, arrow_threads=1,
        peak_working_set_bytes=memory(), targeted_regressions=tests,
        scope="five securities; fixed months plus 601857 2017-2018; no full-market scan", safety=SAFETY))
    print(json.dumps({"native_validation": "PASS", "seconds": time.perf_counter()-started, "cases": len(cases)}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--native", type=Path, required=True)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--tests-xml", type=Path)
    run(parser.parse_args())
