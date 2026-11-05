"""Isolated CSMAR research candidates, using the existing original-byte replay."""
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path
import subprocess

import pandas as pd

from factorlab.data.corporate_actions.contracts import CorporateAction
from factorlab.data.evidence import date, fail, json_bytes, sha256, table_sha256, utc_now
from factorlab.data.schema import normalize_symbol
from factorlab.data.tdx import safe_path
from factorlab.errors import DataQualityError
from . import profiles
from .binding import number, replay, runtime
from .catalog import operator_paths, references, write_json


def market_candidate(projection, symbol):
    """Comparable reinvestment close is a signal candidate, never a fill price.

    This is CSMAR Adjprcwd's reviewed definition, not a newly certified HFQ
    formula. Keep its native lexemes/units in the accompanying tables.
    """
    raw, records = projection.raw.copy(), projection.native
    if raw.empty or not raw.symbol.eq(symbol).all():
        fail("exact native market identity differs from requested symbol")
    if "Adjprcwd" not in records:
        fail("missing native comparable close; no raw-close substitute")
    for schema in projection.metrics["schemas"].values():
        fields, units = schema["headers"], schema["units"]
        if "Adjprcwd" not in fields or units[fields.index("Adjprcwd")] != "元/股":
            fail("unreviewed comparable-close units")
    output = raw.rename(columns={c: "raw_" + c for c in ("open", "high", "low", "close")})
    output["adj_close"] = [number(v, positive=True) for v in records.Adjprcwd]
    states = [int(number(v, integral=True, positive=True)) for v in records.Trdsta]
    if any(v not in range(1, 16) for v in states):
        fail("unsupported native trade status")
    output["trade_status"] = (raw.volume.gt(0) & records.Filling.eq("0")).astype(int)
    output["is_st"] = [int(v in {2, 3, 5, 6, 8, 9, 11, 12, 14, 15}) for v in states]
    # Supplier holiday fills are not observed trading sessions. Suspended
    # session placeholders remain missing RAW and block downstream fills.
    return output.loc[~records.Filling.eq("2")].reset_index(drop=True)


def action_candidates(projection, symbol, observed_at):
    """No record date in Cptl: retain a PROPOSED event, never invent entitlement.

    Each fact keeps full physical provenance and raw terms. Unsupported types
    remain explicit OTHER candidates; no capital-stock total becomes a split.
    """
    events = []
    for record, origin in zip(projection.native.to_dict("records"), projection.lineage.to_dict("records")):
        market = str(int(number(record.get("Markettype"), integral=True)))
        if profiles.MARKETS.get(market) != symbol.split(".")[1]:
            fail("corporate action market identity differs")
        cash = record.get("Disttyp") == "CA"
        event = CorporateAction(
            security_id=symbol, event_id="csmar:" + sha256(json_bytes([record, origin])), revision=1,
            event_type="CASH_DIVIDEND" if cash else "OTHER", effective_date=record["Exdistdt"],
            announced_at=record.get("Annodt"), available_at=observed_at,
            source_id="CSMAR_TRDNEW_Cptl_CANDIDATE", source_sha256=origin["source_sha256"],
            cash=Decimal(record["Amount"]) if cash else None,
            pay_date=record.get("Paydt"), currency="CNY", record_date=None,
            original_terms=json_bytes(record).decode("ascii").strip())
        events.append(dict(event=asdict(event), origin=origin,
                           execution_status="BLOCKED_MISSING_RECORD_DATE_AND_REVIEWED_TERMS",
                           tax_status="UNKNOWN", source_version="sha256:" + origin["source_sha256"]))
    return events


def prepare(catalog, *, source_root, scratch, output, symbol, start, end, warmup_start):
    symbol = normalize_symbol(symbol)
    if not date(warmup_start) < date(start) <= date(end):
        fail("explicit preceding warmup and ordered research range required")
    root, scratch, destination = operator_paths(source_root, scratch, output)
    destination.mkdir(parents=True, exist_ok=False)
    for name in ("market", "corporate_actions", "calendar", "provenance", "diagnostics"):
        (destination / name).mkdir()
    files, projections, diagnostics, pinned = {}, {}, [], []
    def table(name, frame):
        with (destination / name).open("xb") as handle:
            frame.to_parquet(handle, index=False)
        files[name] = table_sha256(frame)
    observed = utc_now()
    for family in ("TRDNEW_Dalyr", "TRDNEW_Cptl", "TRDNEW_Capchg", "TRDNEW_Co", "TRD_AdjustFactor"):
        lo, hi = ("1900-01-01", end) if family == "TRDNEW_Co" else (warmup_start, end)
        refs = references(catalog, family=family, codes=[symbol[:6]], start=lo, end=hi)
        found = []
        for i, ref in enumerate(refs):
            pinned.append(ref)
            try:
                projection = replay(ref, source_root=root, scratch=scratch)
            except DataQualityError as exc:
                if "requested native scope has no records" not in str(exc):
                    raise
                diagnostics.append(dict(family=family, member=ref["member"], status="NO_TARGET_ROWS", reason=str(exc)))
                continue
            found.append(projection)
            for role in ("native", "lineage"):
                table("provenance/%s-%d-%s.parquet" % (family, i, role), getattr(projection, role))
            write_json(destination / "provenance" / ("%s-%d-metrics.json" % (family, i)), projection.metrics)
        if found:
            combined = pd.concat([p.native for p in found], ignore_index=True)
            if combined.duplicated(list(profiles.PRIMARY_KEYS[family])).any():
                fail("overlapping source shards/duplicate native keys: " + family)
        projections[family] = found
    markets = [market_candidate(p, symbol) for p in projections["TRDNEW_Dalyr"]]
    if not markets:
        fail("BLOCKED: no target RAW market records")
    market = pd.concat(markets, ignore_index=True).sort_values("date").reset_index(drop=True)
    if market.duplicated(["date", "symbol"]).any():
        fail("overlapping source shards/duplicate market keys; no silent source priority")
    if (market.date < date(start)).sum() < 20:
        fail("BLOCKED: fewer than 20 real warmup sessions")
    table("market/bars.parquet", market)
    calendar = market[["date"]].copy()
    table("calendar/observed_sessions.parquet", calendar)
    actions = [a for p in projections["TRDNEW_Cptl"] for a in action_candidates(p, symbol, observed)]
    ids = [json_bytes([a["event"]["effective_date"], a["event"]["event_type"], a["event"]["original_terms"]]) for a in actions]
    if len(ids) != len(set(ids)):
        fail("overlapping corporate action source records")
    write_json(destination / "corporate_actions/events.json", actions)
    write_json(destination / "provenance/references.json", pinned)
    research = market.loc[market.date.between(date(start), date(end))]
    if research.empty:
        fail("BLOCKED: empty requested research interval")
    coverage = dict(symbol=symbol, start=start, end=end, warmup_start=warmup_start,
                    raw_records=len(market), research_records=len(research),
                    warmup_records=int((market.date < date(start)).sum()),
                    first_observed=str(market.date.min().date()), last_observed=str(market.date.max().date()),
                    corporate_actions=len(actions), raw_conversion="PASS", comparable_close="CSMAR_Adjprcwd_CANDIDATE",
                    calendar="OBSERVED_SECURITY_SESSIONS_ONLY; OFFICIAL_CALENDAR_UNKNOWN",
                    factors="NATIVE_OBSERVATIONS_ONLY" if projections["TRD_AdjustFactor"] else "UNKNOWN",
                    identity="CURRENT_NATIVE_COMPANY_RECORD; HISTORICAL_IDENTITY_UNVERIFIED" if projections["TRDNEW_Co"] else "UNKNOWN",
                    action_coverage="PARTIAL", event_factor_association="UNVERIFIED",
                    execution="BLOCKED", formal_return_backtest="BLOCKED / NOT_RUN")
    write_json(destination / "diagnostics/coverage.json", coverage)
    write_json(destination / "diagnostics/replay.json", diagnostics)
    for path in sorted(destination.rglob("*")):
        if path.is_file():
            files[path.relative_to(destination).as_posix()] = sha256(path.read_bytes())
    try:
        code_commit = subprocess.check_output(["git", "rev-parse", "HEAD"],
                                              cwd=Path(__file__).parents[4], text=True).strip()
    except (OSError, subprocess.CalledProcessError):
        code_commit = "UNKNOWN"
    source_files = {p.relative_to(Path(__file__).parents[2]).as_posix(): sha256(p.read_bytes())
                    for p in Path(__file__).parent.glob("*.py")}
    manifest = dict(schema="CSMAR_LOCAL_RESEARCH_CANDIDATE_V1", files=files, coverage=coverage,
                    code_commit=code_commit, implementation_files=source_files, runtime=runtime(),
                    input_catalog_sha256=sha256(json_bytes(catalog)), source="CSMAR_ONLY",
                    no_live_money=True, pit_status="PIT_UNVERIFIED", hfq_status="HFQ_UNVERIFIED",
                    formal_status="FORMAL_ADMISSION_BLOCKED", observed_at=observed)
    write_json(destination / "manifest.json", manifest)
    return destination / "manifest.json"
