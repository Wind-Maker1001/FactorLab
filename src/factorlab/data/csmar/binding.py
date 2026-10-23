"""Original-byte replay and isolated partial evidence using existing artifacts.

References contain only ZIP basenames. The external trust root is supplied by
the operator (FACTORLAB_CSMAR_ROOT for qualification replay), not a package.
No complete financial qualification or historical knowledge time is invented.
"""

from dataclasses import dataclass
from decimal import Context, Decimal, DecimalException, ROUND_HALF_EVEN, localcontext
import importlib.metadata
import json
import math
import os
from pathlib import Path
import platform
import re
import time

import pandas as pd

from factorlab.data.evidence import (
    SourceArtifact, archive_bytes, date, fail,
    json_bytes, sha256, table_sha256, utc_now,
)
from factorlab.data.tdx import read_snapshot, safe_path
from factorlab.errors import DataQualityError
from . import native, profiles

UNBOUND = ("PIT", "STATUS", "CALENDAR", "IDENTITY", "UNITS", "ADJUSTMENT",
           "REFERENCE", "BENCHMARK", "TURNOVER", "LISTINGS", "ANCHOR")
MAX_SELECTED = 100000
MAX_SELECTED_ESTIMATED_BYTES = 64 * 1024 ** 2
RAW_COLUMNS = ["date", "symbol", "open", "high", "low", "close", "volume", "amount"]


def parser_sha256():
    from .fingerprint import implementation_manifest
    return sha256(json_bytes(implementation_manifest("parser")))


def runtime():
    return {"python": platform.python_version(), "pandas": pd.__version__,
            "pyarrow": importlib.metadata.version("pyarrow"),
            "numpy": importlib.metadata.version("numpy")}


def scope(codes, start, end):
    if (not isinstance(codes, (list, tuple)) or not 1 <= len(codes) <= 1000
            or len(set(codes)) != len(codes)
            or any(not isinstance(c, str) or not re.fullmatch(r"[0-9]{6}", c) for c in codes)):
        fail("explicit six-digit native codes required; no leading-zero repair")
    if date(start) > date(end):
        fail("scope start exceeds end")
    return {"codes": sorted(codes), "start": start, "end": end}


def reference(*, source_name, source_sha256, source_bytes, family, member,
              member_sha256, sheet_records, codes, start, end, synthetic_only=False):
    """Build a replay request; supplied counts are expectations, not certificates."""
    if family not in profiles.PROFILES or type(synthetic_only) is not bool:
        fail("unsupported CSMAR table or synthetic boundary")
    if (not isinstance(sheet_records, dict) or not sheet_records
            or any(not isinstance(k, str) or not k or type(v) is not int or v < 1
                   for k, v in sheet_records.items())):
        fail("explicit complete sheet record expectations required")
    if not re.fullmatch(re.escape(family) + r"[0-9]*\.xlsx", member):
        fail("native workbook name differs from table contract")
    return {"schema": profiles.SOURCE_ID, "source_name": source_name,
            "source_sha256": source_sha256, "source_bytes": source_bytes,
            "family": family, "member": member, "member_sha256": member_sha256,
            "sheet_records": sheet_records, "scope": scope(codes, start, end),
            "synthetic_only": synthetic_only, "parser_sha256": parser_sha256(),
            "profile_sha256": profiles.profile_sha256(), "runtime": runtime(),
            "supplier_version": "UNKNOWN", "supplier_acquired_at": None,
            "license_scope": "SYNTHETIC_ONLY" if synthetic_only else "RESTRICTED_LOCAL_AUTHORIZED_USE_ONLY",
            "local_snapshot_version": "sha256:" + source_sha256}


def number(value, multiplier="1", *, integral=False, positive=False):
    """Exact decimal unit conversion followed by binary64 nearest rounding.

    At most 128 coefficient digits, lexical length 512, exponents +/-9999.
    All decimal arithmetic signals trap. No decimal rounding is permitted.
    Integral values must be exact in their canonical unit and in binary64;
    native shares/enumerations must already be integral. Fractional wan-shares
    are permitted only when the exact x10000 result is integral. Non-integral
    prices/amounts allow ordinary binary64 rounding, including nonzero
    subnormals, but never overflow or loss of a nonzero value to zero.
    """
    if not isinstance(value, str) or len(value) > 512 or not re.fullmatch(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[Ee][+-]?[0-9]+)?", value):
        fail("missing or ambiguous native numeric value")
    if multiplier not in {"1", "10000"}:
        fail("unreviewed native numeric unit multiplier")
    context = Context(prec=128, rounding=ROUND_HALF_EVEN, Emin=-9999,
                      Emax=9999, capitals=1, clamp=0)
    for signal in context.traps:
        context.traps[signal] = True
    try:
        with localcontext(context):
            original = Decimal(value)
            if (not original.is_finite() or original < 0 or (positive and original <= 0)
                    or len(original.as_tuple().digits) > 128
                    or not -9999 <= original.as_tuple().exponent <= 9999):
                fail("invalid native numeric domain/range")
            if integral and multiplier == "1" and original != original.to_integral_value():
                fail("invalid native integer before unit conversion")
            result = original * Decimal(multiplier)
            if integral and result != result.to_integral_value():
                fail("invalid native integer in canonical unit")
            converted = float(result)
            if (not math.isfinite(converted) or (result != 0 and converted == 0)
                    or (positive and converted <= 0)
                    or (integral and Decimal.from_float(converted) != result)):
                fail("native value exceeds canonical numeric precision/range (overflow or underflow)")
            return converted
    except (DecimalException, OverflowError, ValueError) as exc:
        fail("invalid native numeric value: " + str(exc))


def raw_projection(record, family, coordinate=""):
    def reject(detail, *fields):
        # The caller supplies the original ZIP/member/sheet/XLSX row. Field
        # names locate cells within that row, including every OHLC operand.
        # Direct calls without provenance retain no invented source location.
        location = " ".join([coordinate] + ["field=" + field for field in fields]).strip()
        fail("%s: %s" % (location, detail))

    def numeric(field, multiplier="1", **kwargs):
        try:
            return number(record.get(field), multiplier, **kwargs)
        except DataQualityError as exc:
            reject(str(exc), field)

    _, code_field, date_field = profiles.PROFILES[family]
    code = record[code_field]
    try:
        day = date(record[date_field])
    except DataQualityError as exc:
        reject(str(exc), date_field)
    if family == "TRDNEW_Dalyr":
        # Native numeric cells serialize enumerations as e.g. 4.0. Decimal
        # integral conversion is explicit; never infer an exchange from code.
        market = str(int(numeric("Markettype", integral=True)))
        exchange = profiles.MARKETS.get(market)
        if exchange is None:
            reject("unsupported/ambiguous market currency or exchange", "Markettype")
        if record.get("Filling") not in {"0", "1", "2"}:
            reject("unknown supplier Filling semantics", "Filling")
        if int(numeric("Trdsta", integral=True)) not in range(1, 17):
            reject("unknown supplier Trdsta observation", "Trdsta")
    elif family == "IDX_Idxtrd":
        if code != "000905" or record.get("Idxtrd09") != "中证500":
            reject("benchmark must be native 000905 / 中证500", code_field, "Idxtrd09")
        exchange = "SH"  # Explicit target-index contract, never ticker inference.
    else:
        reject("table has no canonical RAW projection", code_field, date_field)
    output = {"date": day, "symbol": code + "." + exchange}
    filled = family == "TRDNEW_Dalyr" and record["Filling"] != "0"
    for source, (target, _, _, multiplier) in profiles.MAPPINGS[family].items():
        # Preserve filled values in the native sidecar; never expose as trades.
        output[target] = (float("nan") if filled and target in {"open", "high", "low", "close"}
                          else numeric(source, multiplier,
                                      integral=target == "volume", positive=target in {"open", "high", "low", "close"}))
    if not filled and not (output["low"] <= min(output["open"], output["close"])
                           <= max(output["open"], output["close"]) <= output["high"]):
        reject("invalid native OHLC geometry", *(source for source, (target, _, _, _) in
               profiles.MAPPINGS[family].items() if target in {"open", "high", "low", "close"}))
    if not filled and family == "TRDNEW_Dalyr" and output["volume"] <= 0:
        reject("unfilled trade requires positive native volume", "Dnshrtrd")
    return output


@dataclass(frozen=True)
class Projection:
    raw: pd.DataFrame
    native: pd.DataFrame
    lineage: pd.DataFrame
    metrics: dict


def replay(ref, *, source_root, scratch) -> Projection:
    """Scan complete selected sheets, select a bounded range, compare originals.

    Caller-selected workbooks/sheets define scope. Counts do not assert that all
    supplier shards exist. All selected-sheet rows are scanned, even after the
    requested date range, and count/CRC/hash checks cannot be short-circuited.
    """
    started = time.perf_counter()
    if not isinstance(ref, dict):
        fail("CSMAR reference must be an object")
    try:
        expected = reference(**{k: ref[k] for k in (
            "source_name", "source_sha256", "source_bytes", "family", "member",
            "member_sha256", "sheet_records", "synthetic_only")}, **ref["scope"])
    except (TypeError, KeyError) as exc:
        fail("malformed CSMAR reference: " + str(exc))
    if json_bytes(expected) != json_bytes(ref):
        fail("CSMAR reference parser/profile/runtime/schema mismatch; rebuild explicitly")
    family = ref["family"]
    _, code_field, date_field = profiles.PROFILES[family]
    selected, provenance, projected, schemas = [], [], [], {}
    scanned = 0
    selected_bytes = 0
    excluded_index_variants = 0
    codes = set(ref["scope"]["codes"])
    with native.source_zip(source_root, ref["source_name"], ref["source_sha256"], ref["source_bytes"]) as (archive, path):
        dictionary_member = family + "[DES][xlsx].txt"
        if archive.getinfo(dictionary_member).file_size > 1024 ** 2:
            fail("dictionary byte limit")
        dictionary = archive.read(dictionary_member)
        dictionary_sha = sha256(dictionary)
        required_sha = sha256(profiles.SYNTHETIC_DICTIONARY) if ref["synthetic_only"] else profiles.PROFILES[family][0]
        if dictionary_sha != required_sha:
            fail("unreviewed/missing native field dictionary version")
        with native.workbook(archive, ref["member"], ref["member_sha256"], scratch) as book:
            available = native.sheets(book)
            if set(available) != set(ref["sheet_records"]):
                fail("complete workbook sheet inventory differs from reference")
            strings = native.shared_strings(book)
            for sheet, xml_member in available.items():
                iterator = iter(native.rows(book, xml_member, strings))
                metadata = [next(iterator, None) for _ in range(3)]
                if any(row is None or row[0] != i + 1 for i, row in enumerate(metadata)):
                    fail("three explicit native header/name/unit rows required")
                headers, names, units = [row[1] for row in metadata]
                if (len(headers) != len(set(headers)) or not headers
                        or any(not isinstance(h, str) or not h for h in headers)
                        or code_field not in headers or date_field not in headers):
                    fail("native schema fields missing or duplicated")
                width = len(headers)
                if len(names) > width or len(units) > width:
                    fail("native metadata extends beyond fields")
                names += [None] * (width - len(names))
                units += [None] * (width - len(units))
                for field, (_, unit, _, _) in profiles.MAPPINGS.get(family, {}).items():
                    if field not in headers or units[headers.index(field)] != unit:
                        fail("native field unit differs from reviewed mapping")
                if family == "TRDNEW_Dalyr" and not {"Filling", "Markettype", "Trdsta"}.issubset(headers):
                    fail("daily native state/filling/market fields required")
                if family == "IDX_Idxtrd" and "Idxtrd09" not in headers:
                    fail("native index identity field required")
                schemas[sheet] = {"headers": headers, "names": names, "units": units}
                count = 0
                code_index, day_index = headers.index(code_field), headers.index(date_field)
                for row_number, values in iterator:
                    if not any(v is not None for v in values):
                        fail("unexpected blank native data row")
                    if len(values) > width:
                        fail("native row exceeds schema width")
                    values += [None] * (width - len(values))
                    count += 1
                    code = values[code_index]
                    # No numeric-code coercion, zero-padding or guessed date.
                    # Native index exports also carry explicit currency variants
                    # such as 000500HKD00. They are out of the six-digit target
                    # contract: count/exclude them without stripping the suffix.
                    if (family.startswith("IDX_") and isinstance(code, str)
                            and re.fullmatch(r"[0-9]{6}(?:HKD|USD)00", code)):
                        excluded_index_variants += 1
                        continue
                    if not isinstance(code, str) or not re.fullmatch(r"[0-9]{6}", code):
                        fail("native code is ambiguous or lost leading zeros")
                    if code not in codes:
                        continue
                    day = values[day_index]
                    try:
                        date(day)
                    except DataQualityError as exc:
                        fail("ZIP=%s member=%s sheet=%s row=%s field=%s: %s" %
                             (ref["source_name"], ref["member"], sheet, row_number, date_field, exc))
                    if not ref["scope"]["start"] <= day <= ref["scope"]["end"]:
                        continue
                    if len(selected) >= MAX_SELECTED:
                        fail("selected range memory limit; use smaller bounded partitions")
                    selected_bytes += 2048 + width * 128 + sum(len(v.encode("utf8")) for v in values if v is not None)
                    if selected_bytes > MAX_SELECTED_ESTIMATED_BYTES:
                        fail("selected native byte budget; use smaller bounded partitions")
                    record = dict(zip(headers, values))
                    selected.append(record)
                    provenance.append({"source_sha256": ref["source_sha256"], "member": ref["member"],
                                       "member_sha256": ref["member_sha256"], "sheet": sheet,
                                       "row_number": row_number, "native_code": code, "native_date": day,
                                       "native_values_sha256": sha256(json_bytes(record)),
                                       "mapping_version": profiles.VERSION})
                    if family in profiles.MAPPINGS:
                        projected.append(raw_projection(record, family,
                            "ZIP=%s member=%s sheet=%s row=%s" % (ref["source_name"], ref["member"], sheet, row_number)))
                if count != ref["sheet_records"][sheet]:
                    fail("native sheet truncated or row count differs from pinned expectation")
                scanned += count
    if not selected:
        fail("requested native scope has no records (no substitute index/code)")
    raw = pd.DataFrame(projected, columns=RAW_COLUMNS)
    if projected:
        raw["date"] = pd.to_datetime(raw.date).astype("datetime64[ns]")
        if raw.duplicated(["date", "symbol"]).any():
            fail("duplicate native primary key/overlapping sheets; no silent deduplication")
    native_frame = pd.DataFrame(selected)
    key = list(profiles.PRIMARY_KEYS[family])
    if not set(key).issubset(native_frame) or native_frame[key].isna().any().any() or native_frame.duplicated(key).any():
        fail("missing or duplicate native table primary key; no silent shard deduplication")
    lineage = pd.DataFrame(provenance)
    if family == "TRD_AdjustFactor":
        required = {"FwardFactor", "BwardFactor", "CumulateFwardFactor", "CumulateBwardFactor"}
        if not required.issubset(native_frame):
            fail("native adjustment factor fields missing")
        for column in required:
            for value, origin in zip(native_frame[column], provenance):
                try:
                    number(value, positive=True)
                except DataQualityError as exc:
                    fail("ZIP=%s member=%s sheet=%s row=%s field=%s: %s" %
                         (ref["source_name"], ref["member"], origin["sheet"], origin["row_number"], column, exc))
        if native_frame.duplicated([code_field, date_field]).any():
            fail("duplicate native factor primary key")
    return Projection(raw, native_frame, lineage, {
        "source_original_path": str(path), "source_sha256": ref["source_sha256"],
        "member_sha256": ref["member_sha256"], "dictionary_sha256": dictionary_sha,
        "dictionary_member": dictionary_member, "schemas": schemas,
        "scanned_records": scanned, "selected_records": len(selected),
        "excluded_native_currency_index_variant_records": excluded_index_variants,
        "elapsed_seconds": time.perf_counter() - started,
        "process_lifetime_peak_memory_bytes": native.peak_memory_bytes(),
        "raw_sha256": table_sha256(raw), "native_sha256": table_sha256(native_frame),
        "lineage_sha256": table_sha256(lineage), "parser_sha256": parser_sha256(),
        "runtime": runtime(), "unbound_financial_roles": list(UNBOUND),
        "formal_status": "FORMAL_ADMISSION_BLOCKED",
    })


def replay_artifact(artifact: SourceArtifact, *, synthetic: bool):
    if artifact.source_id != profiles.SOURCE_ID:
        fail("unsupported CSMAR artifact source_id")
    if artifact.bytes > 1024 ** 2:
        fail("CSMAR reference receipt size limit")
    ref = json.loads(artifact.read())
    if ref.get("family") != "TRDNEW_Dalyr" or (ref.get("synthetic_only") and not synthetic):
        fail("CSMAR RAW role/synthetic boundary mismatch")
    root = os.environ.get("FACTORLAB_CSMAR_ROOT")
    scratch = os.environ.get("FACTORLAB_CSMAR_SCRATCH")
    if not root or not scratch:
        fail("operator CSMAR root and bounded scratch must be configured")
    result = replay(ref, source_root=Path(root), scratch=Path(scratch))
    artifact.read()  # Detect a changing reference during original-byte replay.
    return result.raw


def write_candidate(ref, *, source_root, scratch, output_root):
    """Isolated partial material, deliberately not a v2 qualification package."""
    result = replay(ref, source_root=source_root, scratch=scratch)
    output = safe_path(Path(output_root))
    output.mkdir(exist_ok=False, parents=True)
    artifact = archive_bytes(json_bytes(ref), output / "references", profiles.SOURCE_ID,
                             "external-original-reference-not-an-acquisition-receipt", utc_now())
    files = {}
    for name, frame in [("raw", result.raw), ("native", result.native), ("lineage", result.lineage)]:
        with (output / (name + ".parquet")).open("xb") as handle:
            frame.to_parquet(handle, index=False)
        files[name + ".parquet"] = table_sha256(frame)
    payload = {"schema": "FACTORLAB_CSMAR_PARTIAL_CANDIDATE_V1",
               "reference": Path(artifact.path).name, "reference_sha256": artifact.sha256,
               "files": files, "metrics": result.metrics,
               "financial_evidence": "UNKNOWN", "software_binding": "RAW" if len(result.raw) else "NATIVE_OBSERVATIONS_ONLY",
               "formal_status": "FORMAL_ADMISSION_BLOCKED", "no_live_money": True}
    with (output / "candidate.json").open("xb") as handle:
        handle.write(json_bytes(payload))
    return output / "candidate.json", artifact, result


def verify_candidate(path, *, source_root, scratch):
    path = safe_path(Path(path))
    if path.stat().st_size > 1024 ** 2:
        fail("candidate manifest byte limit")
    content, digest = read_snapshot(path, path.parent)
    value = json.loads(content)
    if (set(value) != {"schema", "reference", "reference_sha256", "files", "metrics", "financial_evidence",
                      "software_binding", "formal_status", "no_live_money"}
            or value.get("schema") != "FACTORLAB_CSMAR_PARTIAL_CANDIDATE_V1"
            or value["financial_evidence"] != "UNKNOWN"
            or value["formal_status"] != "FORMAL_ADMISSION_BLOCKED" or value["no_live_money"] is not True):
        fail("not a CSMAR partial candidate")
    name = value["reference"]
    expected = value["reference_sha256"]
    if name != expected + ".bin":
        fail("unsafe candidate reference name")
    ref_path = safe_path(path.parent / "references" / name, path.parent)
    body, actual = read_snapshot(ref_path, path.parent)
    if actual != expected:
        fail("candidate reference hash mismatch")
    result = replay(json.loads(body), source_root=source_root, scratch=scratch)
    volatile = {"elapsed_seconds", "process_lifetime_peak_memory_bytes"}
    if (value["software_binding"] != ("RAW" if len(result.raw) else "NATIVE_OBSERVATIONS_ONLY")
            or {k: v for k, v in value["metrics"].items() if k not in volatile}
            != {k: v for k, v in result.metrics.items() if k not in volatile}):
        fail("candidate declared metrics differ from original-byte replay")
    frames = {"raw.parquet": result.raw, "native.parquet": result.native, "lineage.parquet": result.lineage}
    if set(value["files"]) != set(frames):
        fail("candidate table inventory mismatch")
    for name, frame in frames.items():
        if (path.parent / name).stat().st_size > 64 * 1024 ** 2:
            fail("candidate table byte limit")
        data, actual = read_snapshot(path.parent / name, path.parent)
        if actual != value["files"][name] or actual != table_sha256(frame):
            fail("candidate Parquet differs from original-byte replay")
    if read_snapshot(path, path.parent)[1] != digest or read_snapshot(ref_path, path.parent)[1] != expected:
        fail("candidate/reference changed during verification")
    return result


def publish_diagnostic(raw_candidate, benchmark_candidate, stocks_path, *, source_root, scratch, output_root):
    """Reuse LocalParquetProvider only with explicitly supplied adjusted columns.

    RAW/factors do not prove HFQ. Caller must provide the existing 18 columns;
    adjusted observations are declared/unqualified. No factor/turnover defaults.
    """
    from factorlab.data.providers.local import publish_local_market_data
    raw = verify_candidate(raw_candidate, source_root=source_root, scratch=scratch)
    benchmark = verify_candidate(benchmark_candidate, source_root=source_root, scratch=scratch)
    stocks_path = safe_path(Path(stocks_path))
    if stocks_path.stat().st_size > 64 * 1024 ** 2:
        fail("diagnostic input byte limit; partition the scope")
    stocks = pd.read_parquet(stocks_path)
    try:
        pd.testing.assert_frame_equal(stocks[["date", "symbol"] + ["raw_" + f for f in ("open", "high", "low", "close")]
                                               + ["volume", "amount"]].rename(columns={"raw_" + f: f for f in ("open", "high", "low", "close")}),
                                      raw.raw, check_exact=True, check_dtype=False)
    except (AssertionError, KeyError) as exc:
        fail("diagnostic stock RAW differs from CSMAR original projection: " + str(exc))
    # Index adjustment=none is explicit in the existing provider contract.
    bars = benchmark.raw.copy()
    for field in ("open", "high", "low", "close"):
        bars["raw_" + field] = bars[field]
        bars["adj_" + field] = bars[field]
    bars["adj_factor"] = 1.0
    bars["turnover"] = float("nan")  # Not supplied by this index table.
    from factorlab.data.schema import CANONICAL_BAR_COLUMNS
    output = safe_path(Path(output_root))
    output.mkdir(parents=True, exist_ok=False)
    bp = output / "benchmark-input.parquet"
    sp = output / "stocks-input.parquet"
    # Publish exactly the adjusted/RAW observations already compared above.
    # Reopening the caller's mutable file would introduce a second input read.
    with sp.open("xb") as handle:
        stocks.to_parquet(handle, index=False)
    bars[list(CANONICAL_BAR_COLUMNS)].to_parquet(bp, index=False)
    version = sha256(json_bytes([raw.metrics["source_sha256"], benchmark.metrics["source_sha256"],
                                parser_sha256(), table_sha256(stocks)]))
    return publish_local_market_data(sp, bp, output / "local", "CSMAR-CANDIDATE-DIAGNOSTIC",
                                     "csmar-local-snapshot:" + version)
