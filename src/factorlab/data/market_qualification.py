"""Replayable evidence qualification, separate from local source declarations.

No arbitrary PASS input or verified boolean promotes a data source. Byte
hashes and normalized table hashes alone do not bind one to the other. TDX
DAY and scoped CSMAR ZIP candidate RAW are reparsed; remaining real contracts
fail closed, even with complete local review declarations. Synthetic evidence
is never research eligible.
"""

from dataclasses import asdict, dataclass, fields, is_dataclass
from decimal import Decimal
import io
import json
import os
from pathlib import Path
import re
import uuid
from typing import Any, Dict, Mapping, Optional, Tuple

import numpy as np
import pandas as pd

from factorlab.data.adjustment.contracts import AdjustmentAnchor, SourceAdjustmentContract
from factorlab.data.adjustment.engine import build_hfq
from factorlab.data.adjustment.validation import validate_adjustment
from factorlab.data.corporate_actions.contracts import CorporateAction, EventCoverage, EventView, SourceSnapshot
from factorlab.data.evidence import SourceArtifact, date, fail, json_bytes, require_text, sha256, table_sha256, timestamp
from factorlab.data.csmar.fingerprint import implementation_sha256
from factorlab.data.tdx import read_snapshot, safe_path
from factorlab.data.units import UnitContract
from factorlab.errors import DataQualityError
from factorlab.universe.csi500 import validate_daily_membership
from factorlab.universe.identity import IdentitySnapshot, SecurityIdentity
from factorlab.universe.status import validate_security_data


@dataclass(frozen=True)
class AdjustmentInput:
    raw: pd.DataFrame
    view: EventView
    method: str
    anchor: AdjustmentAnchor
    signal_as_of: Mapping
    reference: pd.DataFrame
    contract: SourceAdjustmentContract
    reference_source: Optional[SourceArtifact] = None


@dataclass(frozen=True)
class QualificationInputs:
    raw: pd.DataFrame
    turnover: pd.DataFrame
    identity: IdentitySnapshot
    units: Tuple[UnitContract, ...]
    adjustments: Mapping[str, AdjustmentInput]
    signal_as_of: Mapping
    source_version: str
    status: pd.DataFrame
    listings: pd.DataFrame
    membership: pd.DataFrame
    raw_sources: Tuple[SourceArtifact, ...]
    calendar_reference: pd.DataFrame
    calendar_source: SourceArtifact
    status_source: SourceArtifact
    pit_source: SourceArtifact
    # Independent source review receipts, not flags supplied to the engine.
    source_reviews: Tuple[SourceArtifact, ...] = ()
    expected_member_count: int = 500
    synthetic_only: bool = False
    benchmark_source: Optional[SourceArtifact] = None
    # Independent frozen research range, never inferred from membership.
    # The supplied calendar can also contain earlier RAW/status warmup days.
    research_start: str = ""
    research_end: str = ""


@dataclass(frozen=True)
class QualificationReport:
    schema: str
    stages: Dict[str, str]
    issues: Tuple[str, ...]
    stocks_sha256: str
    benchmark_sha256: str
    required_keys_sha256: str
    source_version: str
    synthetic_only: bool
    formal_admission: bool
    execution_admission: bool
    implementation_sha256: str


def _reparse_raw(inputs: QualificationInputs, synthetic: bool) -> bool:
    """One existing decoder, not a supplier registry or financial certificate.

    The source_id selects a fixed candidate byte layout. The original locator
    supplies its filename/code hypothesis; historical identity/units remain
    separately unbound. Compare every supplied column/row, without repairing,
    sorting, clipping or accepting a caller's table hash as a parse proof.
    """
    from factorlab.data.tdx import NAME, audit_day, raw_frame
    parsed = []
    supported = True
    for artifact in inputs.raw_sources:
        content = artifact.read()
        if artifact.source_id == "CSMAR_ZIP_REFERENCE_V1":
            from factorlab.data.csmar.binding import replay_artifact
            frame = replay_artifact(artifact, synthetic=synthetic)
            if not set(inputs.raw.columns).issubset(frame.columns):
                fail("CSMAR RAW columns lack a reparsed source contract")
            parsed.append(frame[list(inputs.raw.columns)])
            continue
        if artifact.source_id != "TDX_DAY_BYTES_V1":
            supported = False
            continue
        original = Path(artifact.locator)
        match = NAME.fullmatch(original.name)
        if not match:
            fail("TDX RAW original filename required for reparsed source")
        audit = audit_day(original, match.group(1).lower(), original.parent,
                          str(inputs.raw.date.min().date()), str(inputs.raw.date.max().date()),
                          source_bytes=content)
        frame = raw_frame(audit)
        if not set(inputs.raw.columns).issubset(frame.columns):
            fail("RAW columns lack a reparsed source contract")
        parsed.append(frame[list(inputs.raw.columns)])
    if parsed and not supported:
        fail("mixed RAW sources lack a complete reparsed source contract")
    if not supported or not parsed:
        if not synthetic:
            fail("RAW lacks a deterministic reparsed source contract")
        return False
    try:
        pd.testing.assert_frame_equal(pd.concat(parsed, ignore_index=True), inputs.raw.reset_index(drop=True),
                                      check_exact=True, check_dtype=False)
    except AssertionError:
        fail("RAW differs from reparsed source bytes")
    return True


def _unbound_real_roles(raw_bound: bool) -> Tuple[str, ...]:
    """Only code-owned parsers can remove a role from this fail-closed list.

    A normalized JSON/Parquet generation, manual declaration, COMPLETE event
    coverage, review receipt or self-recomputed hash is not an upstream parser.
    Sina factors are observations, not event terms or independent OHLC. There
    is currently no complete real admission path; do not invent one here.
    """
    roles = ("PIT", "STATUS", "CALENDAR", "IDENTITY", "UNITS", "ADJUSTMENT", "REFERENCE",
             "BENCHMARK", "TURNOVER", "LISTINGS", "ANCHOR")
    return roles if raw_bound else ("RAW",) + roles


def _reviews(inputs: QualificationInputs) -> None:
    """Require scoped, inspectable independent judgments for real evidence.

    This does not authenticate a reviewer or prove that a claimed timestamp
    existed historically. That limitation is explicit in the source contract.
    """
    expected = {
        "RAW": {a.sha256 for a in inputs.raw_sources},
        "CALENDAR": {inputs.calendar_source.sha256},
        "STATUS": {inputs.status_source.sha256},
        "PIT": {inputs.pit_source.sha256},
        "IDENTITY": {a.sha256 for a in inputs.identity.artifacts},
        "UNITS": {c.artifact.sha256 for c in inputs.units},
        "ADJUSTMENT": {a.sha256 for spec in inputs.adjustments.values() for a in spec.view.snapshot.artifacts},
        "REFERENCE": {spec.reference_source.sha256 for spec in inputs.adjustments.values() if spec.reference_source is not None},
        "BENCHMARK": {inputs.benchmark_source.sha256} if inputs.benchmark_source is not None else set(),
    }
    covered = {role: set() for role in expected}
    for artifact in inputs.source_reviews:
        try:
            value = json.loads(artifact.read())
        except (ValueError, UnicodeError) as exc:
            fail("unreadable source review")
        required = {"schema", "role", "reviewer", "reviewed_at", "bound_sha256", "upstream_origin",
                    "time_basis", "coverage_basis", "effective_date_basis", "history_kind", "scope_start", "scope_end"}
        if not isinstance(value, dict) or set(value) != required or value["schema"] != "FACTORLAB_INDEPENDENT_SOURCE_REVIEW_V1":
            fail("unsupported independent source review contract")
        role = value["role"]
        if role not in expected:
            fail("unknown review role")
        for field in ("reviewer", "upstream_origin", "time_basis", "coverage_basis", "effective_date_basis"):
            require_text(value[field], "review " + field)
        if timestamp(value["reviewed_at"]) > timestamp(artifact.acquired_at):
            fail("review acquisition predates its review")
        if value["history_kind"] != "CONTEMPORANEOUS":
            fail("strict qualification requires historical source vintages")
        if date(value["scope_start"]) > inputs.raw.date.min() or date(value["scope_end"]) < inputs.raw.date.max():
            fail("independent review scope does not cover prices")
        hashes = value["bound_sha256"]
        if not isinstance(hashes, list) or not hashes or len(set(hashes)) != len(hashes) or not set(hashes).issubset(expected[role]):
            fail("review source hashes are incomplete or unrelated")
        covered[role].update(hashes)
    if any(not hashes or covered[role] != hashes for role, hashes in expected.items()):
        fail("independent source time/coverage reviews are incomplete")


def qualify_market_data(inputs: QualificationInputs, required_keys: pd.DataFrame,
                        calendar: pd.DatetimeIndex, *, stocks: pd.DataFrame,
                        benchmark: pd.DataFrame) -> QualificationReport:
    from factorlab.data.assemble import assemble_market_data
    implementation = implementation_sha256("qualification")
    stages = {name: "INCOMPLETE" for name in ("RAW", "ADJUSTMENT", "STATUS", "PIT", "SOURCE_BINDING", "FORMAL")}
    issues = []
    if not isinstance(inputs.synthetic_only, bool):
        fail("qualification synthetic boundary must be explicit")
    synthetic = (inputs.synthetic_only or inputs.identity.synthetic_only
                 or any(u.synthetic_only for u in inputs.units)
                 or any(s.view.snapshot.synthetic_only or s.anchor.synthetic_only or s.contract.synthetic_only
                        for s in inputs.adjustments.values()))
    calendar = pd.DatetimeIndex(calendar)
    if (calendar.empty or calendar.tz is not None or calendar.has_duplicates
            or not calendar.is_monotonic_increasing or not calendar.equals(calendar.normalize())):
        fail("explicit unique normalized calendar required")
    if set(required_keys.columns) != {"date", "symbol"} or required_keys.empty or required_keys.duplicated().any():
        fail("explicit nonempty required market keys required")
    if (isinstance(inputs.expected_member_count, bool) or not isinstance(inputs.expected_member_count, int)
            or inputs.expected_member_count < 1):
        fail("explicit expected PIT member count required")
    if not required_keys.date.isin(calendar).all():
        fail("required market keys outside calendar")

    def run_stage(name, operation):
        try:
            operation()
            stages[name] = "PASS"
        except (DataQualityError, ValueError, TypeError, KeyError) as exc:
            issues.append(name + ": " + str(exc))

    raw_bound = False
    def raw_check():
        nonlocal raw_bound
        if not inputs.raw_sources:
            fail("RAW source bytes required")
        for artifact in inputs.raw_sources:
            artifact.read()
        if (not {"date", "symbol", "open", "high", "low", "close", "volume", "amount"}.issubset(inputs.raw)
                or inputs.raw.empty or inputs.raw.duplicated(["date", "symbol"]).any()):
            fail("RAW engineering keys/fields incomplete")
        raw_bound = _reparse_raw(inputs, synthetic)
    run_stage("RAW", raw_check)

    if synthetic:
        stages["SOURCE_BINDING"] = "SOFTWARE_ONLY"
    else:
        for role in _unbound_real_roles(raw_bound):
            issues.append("SOURCE_BINDING/" + role + ": deterministic upstream-to-normalized parser contract incomplete")

    results = {}
    def adjustment_check():
        if not inputs.adjustments:
            fail("adjustment recipes required")
        if not inputs.units:
            fail("unit evidence required")
        inputs.identity.resolve(required_keys, inputs.signal_as_of)
        from factorlab.data.units import convert_units
        converted = convert_units(inputs.raw, inputs.units, source_version=inputs.source_version,
                                  signal_as_of=inputs.signal_as_of)
        resolved = inputs.identity.resolve(converted[["date", "symbol"]], inputs.signal_as_of)
        normalized = converted.merge(resolved, on=["date", "symbol"], validate="one_to_one")
        for security_id, spec in sorted(inputs.adjustments.items()):
            if spec.anchor.security_id != security_id:
                fail("adjustment recipe identity mismatch")
            listing_dates = [date(item.listing_date) for item in inputs.identity.intervals if item.security_id == security_id]
            if not listing_dates or date(spec.anchor.date) < min(listing_dates):
                fail("anchor predates security listing; a sentinel is not a proved historical anchor")
            source_rows = normalized.loc[normalized.security_id.eq(security_id), list(converted.columns)].reset_index(drop=True)
            if not set(converted.columns).issubset(spec.raw):
                fail("adjustment raw recipe lacks normalized source fields")
            try:
                pd.testing.assert_frame_equal(source_rows, spec.raw[list(converted.columns)].reset_index(drop=True),
                                              check_exact=True, check_dtype=False)
            except AssertionError:
                fail("adjustment recipe raw prices differ from unit-normalized source")
            if spec.reference_source is not None:
                spec.reference_source.read()
                if spec.reference_source.source_id == spec.view.snapshot.source_id:
                    fail("reference source is not independent")
            elif not synthetic:
                fail("real Golden requires bound independent original reference bytes")
            result = build_hfq(spec.raw, spec.view, method=spec.method, anchor=spec.anchor,
                               signal_as_of=spec.signal_as_of)
            report = validate_adjustment(result, spec.reference, spec.contract)
            if report.status != "PASS":
                fail("independent adjustment validation incomplete: " + ";".join(report.issues))
            results[security_id] = result
        assembled = assemble_market_data(inputs.raw, results, inputs.turnover, inputs.identity,
                                         inputs.units, source_version=inputs.source_version,
                                         signal_as_of=inputs.signal_as_of, benchmark=benchmark,
                                         benchmark_symbol=benchmark.symbol.iloc[0])
        if table_sha256(assembled.stocks) != table_sha256(stocks):
            fail("canonical stocks differ from replayed source assembly")
    run_stage("ADJUSTMENT", adjustment_check)

    def pit_check():
        if not inputs.research_start or not inputs.research_end:
            fail("independent research window start/end required; membership cannot define it")
        start, end = date(inputs.research_start), date(inputs.research_end)
        if start > end or start not in calendar or end not in calendar:
            fail("research window must have ordered boundaries in the independent trading calendar")
        research = calendar[(calendar >= start) & (calendar <= end)]
        if not synthetic and inputs.expected_member_count != 500:
            fail("formal CSI500 requires exactly 500 PIT members per research trading day")
        inputs.pit_source.read()
        inputs.calendar_source.read()
        if inputs.benchmark_source is not None:
            inputs.benchmark_source.read()
        elif not synthetic:
            fail("real benchmark original source bytes required")
        if set(inputs.calendar_reference.columns) != {"date"}:
            fail("independent calendar requires exact date column")
        if not pd.DatetimeIndex(inputs.calendar_reference.date).equals(calendar):
            fail("independent calendar differs from benchmark")
        if not pd.DatetimeIndex(benchmark.date).equals(calendar):
            fail("benchmark calendar differs")
        if inputs.membership.empty:
            fail("PIT membership required")
        validate_daily_membership(inputs.membership, requested_dates=research,
                                  expected_member_count=inputs.expected_member_count)
        index = pd.MultiIndex.from_frame(required_keys)
        pit = pd.MultiIndex.from_frame(inputs.membership[["date", "symbol"]])
        if len(pit.difference(index)):
            fail("required key scope excludes PIT members")
    run_stage("PIT", pit_check)

    def status_check():
        inputs.status_source.read()
        status, _ = validate_security_data(inputs.status, inputs.listings, calendar=calendar,
                                           membership=inputs.membership)
        status_keys = pd.MultiIndex.from_frame(status[["date", "symbol"]])
        raw_keys = pd.MultiIndex.from_frame(inputs.raw[["date", "symbol"]])
        if len(raw_keys.difference(pd.MultiIndex.from_frame(required_keys))):
            fail("qualification scope must include all claimed RAW rows, including warmup")
        if len(pd.MultiIndex.from_frame(required_keys).difference(status_keys)):
            fail("required key status coverage incomplete")
        prices = required_keys.merge(stocks, on=["date", "symbol"], how="left", validate="one_to_one")
        prices = prices.merge(status, on=["date", "symbol"], how="left", validate="one_to_one")
        trading = prices.trade_status.eq(1)
        necessary = ["raw_open", "raw_high", "raw_low", "raw_close", "adj_open", "adj_high", "adj_low", "adj_close", "volume", "amount", "turnover", "adj_factor"]
        if prices.loc[trading, necessary].isna().any().any():
            fail("unexplained missing trading bars/turnover")
        if (prices.loc[trading, "volume"] <= 0).any():
            fail("normal trading rows require positive share volume")
        halted = ~trading
        if prices.loc[halted, ["raw_open", "raw_high", "raw_low", "raw_close", "adj_open", "adj_high", "adj_low", "adj_close"]].notna().any().any():
            fail("suspended rows cannot carry vendor-filled prices into research")
    run_stage("STATUS", status_check)

    if not synthetic:
        try:
            _reviews(inputs)
        except DataQualityError as exc:
            issues.append("FORMAL: " + str(exc))
    base_passed = all(stages[k] == "PASS" for k in ("RAW", "ADJUSTMENT", "STATUS", "PIT"))
    formal = base_passed and not issues and not synthetic
    stages["FORMAL"] = "PASS" if formal else "SOFTWARE_ONLY" if base_passed and synthetic else "INCOMPLETE"
    # Current execution code has no corporate-action ledger, even if prices qualify.
    has_actions = any(any(date(e.effective_date) > date(spec.anchor.date)
                          and date(e.effective_date) <= inputs.raw.date.max()
                          and e.status == "IMPLEMENTED" for e in spec.view.events)
                      for spec in inputs.adjustments.values())
    return QualificationReport("FACTORLAB_MARKET_QUALIFICATION_V1", stages, tuple(issues),
                               table_sha256(stocks), table_sha256(benchmark), table_sha256(required_keys),
                               inputs.source_version, synthetic, formal, formal and not has_actions, implementation)


_TYPES = {cls.__name__: cls for cls in (SourceArtifact, CorporateAction, EventCoverage, SourceSnapshot,
          EventView, SecurityIdentity, IdentitySnapshot, UnitContract, AdjustmentAnchor,
          SourceAdjustmentContract, AdjustmentInput, QualificationInputs)}


def _write(path: Path, content: bytes) -> None:
    with path.open("xb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def _encode(value, root, artifacts):
    if isinstance(value, SourceArtifact):
        content = value.read()
        name = "source-" + value.sha256 + ".bin"
        if name not in artifacts:
            _write(root / name, content)
            artifacts[name] = sha256(content)
        return {"type": "SourceArtifact", "fields": dict(asdict(value), path=name)}
    if isinstance(value, pd.DataFrame):
        stream = io.BytesIO()
        value.to_parquet(stream, index=False)
        content = stream.getvalue()
        digest = sha256(content)
        name = "table-" + digest + ".parquet"
        if name not in artifacts:
            _write(root / name, content)
            artifacts[name] = digest
        return {"type": "DataFrame", "file": name, "sha256": digest}
    if isinstance(value, pd.Timestamp):
        return {"type": "Timestamp", "value": value.isoformat()}
    if isinstance(value, Decimal):
        return {"type": "Decimal", "value": str(value)}
    if is_dataclass(value):
        return {"type": type(value).__name__, "fields": {f.name: _encode(getattr(value, f.name), root, artifacts) for f in fields(value)}}
    if isinstance(value, Mapping):
        return {"type": "Mapping", "items": [[_encode(k, root, artifacts), _encode(v, root, artifacts)] for k, v in value.items()]}
    if isinstance(value, tuple):
        return {"type": "Tuple", "items": [_encode(v, root, artifacts) for v in value]}
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    fail("unsupported qualification recipe value")


def _decode(value, root, artifacts):
    if not isinstance(value, dict):
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        fail("unsupported recipe value")
    kind = value.get("type")
    if kind in {"Decimal", "Timestamp"} and set(value) == {"type", "value"}:
        return Decimal(value["value"]) if kind == "Decimal" else pd.Timestamp(value["value"])
    if kind in {"Tuple", "Mapping"} and set(value) == {"type", "items"}:
        if kind == "Tuple":
            return tuple(_decode(v, root, artifacts) for v in value["items"])
        items = [(_decode(k, root, artifacts), _decode(v, root, artifacts)) for k, v in value["items"]]
        if len(dict(items)) != len(items):
            fail("duplicate recipe mapping key")
        return dict(items)
    if kind == "DataFrame" and set(value) == {"type", "file", "sha256"}:
        name = value["file"]
        if name != "table-" + value["sha256"] + ".parquet" or artifacts.get(name) != value["sha256"]:
            fail("unbound qualification table")
        content, digest = read_snapshot(root / name, root)
        if digest != value["sha256"]:
            fail("qualification table hash mismatch")
        return pd.read_parquet(io.BytesIO(content))
    if kind in _TYPES and set(value) == {"type", "fields"}:
        cls = _TYPES[kind]
        payload = value["fields"]
        if not isinstance(payload, dict) or set(payload) != set(cls.__dataclass_fields__):
            fail("qualification recipe fields mismatch")
        if kind == "SourceArtifact":
            name = payload["path"]
            if name != "source-" + payload["sha256"] + ".bin" or artifacts.get(name) != payload["sha256"]:
                fail("unbound qualification source")
            return SourceArtifact(**dict(payload, path=str(safe_path(root / name, root))))
        return cls(**{k: _decode(v, root, artifacts) for k, v in payload.items()})
    fail("unknown qualification recipe type")


def publish_qualification(inputs: QualificationInputs, required_keys: pd.DataFrame,
                          calendar: pd.DatetimeIndex, *, stocks: pd.DataFrame,
                          benchmark: pd.DataFrame, output_root: Path) -> Path:
    report = qualify_market_data(inputs, required_keys, calendar, stocks=stocks, benchmark=benchmark)
    root = safe_path(output_root)
    root.mkdir(parents=True, exist_ok=True)
    temporary = safe_path(root / (".tmp-" + uuid.uuid4().hex), root)
    temporary.mkdir()
    artifacts = {}
    recipe = {"inputs": _encode(inputs, temporary, artifacts),
              "required_keys": _encode(required_keys, temporary, artifacts),
              "calendar": _encode(pd.DataFrame({"date": calendar}), temporary, artifacts)}
    content = json_bytes(recipe)
    _write(temporary / "recipe.json", content)
    artifacts["recipe.json"] = sha256(content)
    payload = {"schema": "FACTORLAB_MARKET_QUALIFICATION_PACKAGE_V1",
               "report": asdict(report), "artifacts": artifacts}
    identity = sha256(json_bytes(payload))
    destination = safe_path(root / identity, root)
    _write(temporary / "manifest.json", json_bytes(dict(payload, generation=identity)))
    restored, _, _, _, _ = load_qualification(temporary / "manifest.json", stocks=stocks, benchmark=benchmark)
    if asdict(restored) != asdict(report):
        fail("qualification replay differs")
    if destination.exists():
        old, _, _, _, _ = load_qualification(destination / "manifest.json", stocks=stocks, benchmark=benchmark)
        if asdict(old) != asdict(report):
            fail("qualification generation collision")
    else:
        try:
            os.rename(str(temporary), str(destination))
        except FileExistsError:
            old, _, _, _, _ = load_qualification(destination / "manifest.json", stocks=stocks, benchmark=benchmark)
            if asdict(old) != asdict(report):
                fail("concurrent qualification generation collision")
    return destination / "manifest.json"


def load_qualification(path: Path, *, stocks: pd.DataFrame, benchmark: pd.DataFrame):
    path = safe_path(path)
    root = path.parent
    content, digest = read_snapshot(path, root)
    try:
        manifest = json.loads(content)
        if (set(manifest) != {"schema", "generation", "report", "artifacts"}
                or manifest["schema"] != "FACTORLAB_MARKET_QUALIFICATION_PACKAGE_V1"):
            fail("qualification manifest fields/schema mismatch")
        identity = sha256(json_bytes({k: v for k, v in manifest.items() if k != "generation"}))
        if manifest["generation"] != identity:
            fail("qualification manifest semantic hash mismatch")
        artifacts = manifest["artifacts"]
        if not isinstance(artifacts, dict) or "recipe.json" not in artifacts:
            fail("qualification recipe absent")
        for name, expected in artifacts.items():
            if name != "recipe.json" and not re.fullmatch(r"(?:source-[0-9a-f]{64}\.bin|table-[0-9a-f]{64}\.parquet)", name):
                fail("unsafe qualification artifact name")
            _, actual = read_snapshot(root / name, root)
            if actual != expected:
                fail("qualification artifact hash mismatch")
        recipe = json.loads(read_snapshot(root / "recipe.json", root)[0])
        if set(recipe) != {"inputs", "required_keys", "calendar"}:
            fail("qualification replay recipe fields mismatch")
        inputs = _decode(recipe["inputs"], root, artifacts)
        keys = _decode(recipe["required_keys"], root, artifacts)
        calendar_frame = _decode(recipe["calendar"], root, artifacts)
        if not isinstance(inputs, QualificationInputs) or list(calendar_frame.columns) != ["date"]:
            fail("qualification replay input types mismatch")
        calendar = pd.DatetimeIndex(calendar_frame.date)
        report = qualify_market_data(inputs, keys, calendar, stocks=stocks, benchmark=benchmark)
        if json_bytes(asdict(report)) != json_bytes(manifest["report"]):
            fail("qualification report disagrees with replay")
        return report, inputs, keys, calendar, digest
    except (ValueError, TypeError, KeyError, OSError) as exc:
        fail("malformed qualification package: " + str(exc))
