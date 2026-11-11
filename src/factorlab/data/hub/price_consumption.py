"""Scoped research consumers of DataHub field semantics, not financial admission.

Contracts live under ``SourceMetadata.field_semantics[field]['consumption']``.
Evidence producers own semantic judgments; consumers only restrict their use.
Legacy DataFrame APIs remain available and do not imply this qualification.
"""
from decimal import Decimal, InvalidOperation

import pandas as pd

from factorlab.data.evidence import date, timestamp
from factorlab.errors import DataQualityError
from factorlab.universe.csi500 import CANONICAL_SYMBOL
from .contracts import QueryResult

SCHEMA = "PRICE_CONSUMPTION_V1"
BASES = frozenset({"OBSERVED_RAW", "ADJUSTED_CANDIDATE", "SUPPLIER_FILLED",
                   "MODEL_VALUATION_ASSUMPTION", "UNKNOWN", "CONFLICT", "NOT_PRICE"})
PURPOSES = {
    "RAW_LABEL": {"OBSERVED_RAW"},
    "RAW_FACTOR": {"OBSERVED_RAW"},
    "ADJUSTED_FACTOR": {"ADJUSTED_CANDIDATE"},
    "UNIT_FACTOR": {"NOT_PRICE"},
    "EXECUTION": {"OBSERVED_RAW"},
    "BOOK_VALUATION": {"OBSERVED_RAW", "MODEL_VALUATION_ASSUMPTION"},
}


class PriceConsumptionError(DataQualityError):
    """A field lacks evidence for its requested research use."""

    def __init__(self, gaps):
        self.gaps = tuple(gaps)
        super().__init__("price qualification gaps: " + "; ".join(self.gaps))


def decimal_text(value):
    """Parse finite numeric text without rounding or cross-source selection."""
    if isinstance(value, bool):
        raise PriceConsumptionError(["INVALID_NUMERIC_TEXT"])
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise PriceConsumptionError(["INVALID_NUMERIC_TEXT"]) from exc
    if not parsed.is_finite():
        raise PriceConsumptionError(["NONFINITE_NUMERIC_TEXT"])
    return parsed


def require_fields(result: QueryResult, requirements, *, identity, cutoff=None):
    """Check field -> (purpose, unit) against exact source/version and row scope.

    ``identity`` is the consumer's independently pinned (source_id, data_version).
    An absent cutoff requests retrospective use, never historical availability.
    Missing prices remain missing; every row still requires semantic coverage.
    """
    metadata, frame = result.metadata, result.data
    gaps = []
    if (any(not isinstance(v, str) or not v.strip() or v in ("UNKNOWN", "UNVERIFIED")
            for v in (metadata.source_id, metadata.data_version))
            or (metadata.source_id, metadata.data_version) != tuple(identity)):
        gaps.append("SOURCE_VERSION_IDENTITY_MISMATCH_OR_UNKNOWN")
    if not isinstance(frame, pd.DataFrame) or not {"date", "symbol"}.issubset(frame):
        raise PriceConsumptionError(gaps + ["SECURITY_DATE_KEYS_REQUIRED"])
    days = pd.to_datetime(frame.date, errors="raise")
    if (days.isna().any() or days.dt.tz is not None
            or not days.equals(days.dt.normalize())
            or not frame.symbol.map(lambda v: isinstance(v, str) and bool(CANONICAL_SYMBOL.fullmatch(v))).all()
            or frame.duplicated(["date", "symbol"]).any()):
        raise PriceConsumptionError(gaps + ["INVALID_OR_AMBIGUOUS_SECURITY_DATE_KEYS"])
    for field, (purpose, unit) in requirements.items():
        prefix = field + "/"
        if purpose not in PURPOSES:
            raise PriceConsumptionError([prefix + "UNKNOWN_PURPOSE"])
        if field not in frame:
            gaps.append(prefix + "FIELD_MISSING")
            continue
        contract = metadata.field_semantics.get(field, {}).get("consumption")
        if not isinstance(contract, dict) or contract.get("schema") != SCHEMA:
            gaps.append(prefix + "CONTRACT_MISSING_OR_UNKNOWN")
            continue
        if (contract.get("source_id"), contract.get("data_version")) != tuple(identity):
            gaps.append(prefix + "CONTRACT_IDENTITY_MISMATCH")
        if contract.get("field") != field:
            gaps.append(prefix + "FIELD_IDENTITY_MISMATCH")
        basis = contract.get("basis", "UNKNOWN")
        if basis not in BASES or basis not in PURPOSES[purpose]:
            gaps.append(prefix + "BASIS_NOT_ALLOWED:" + str(basis))
        if contract.get("qualification") != "RESEARCH_QUALIFIED":
            gaps.append(prefix + "UNQUALIFIED:" + str(contract.get("reasons", [])))
        if purpose not in contract.get("purposes", []):
            gaps.append(prefix + "PURPOSE_NOT_QUALIFIED:" + purpose)
        if not unit or unit in ("UNKNOWN", "UNVERIFIED") or contract.get("unit") != unit:
            gaps.append(prefix + "UNIT_UNKNOWN_OR_MISMATCH")
        policy = contract.get("adjustment_policy")
        if not policy or policy == "UNKNOWN" or (basis == "OBSERVED_RAW" and policy != "NONE"):
            gaps.append(prefix + "ADJUSTMENT_POLICY_UNKNOWN_OR_CONFLICT")
        if basis == "OBSERVED_RAW" and contract.get("adjustment_event_dates"):
            gaps.append(prefix + "RAW_HAS_ADJUSTMENT_EVENTS")
        if not contract.get("evidence"):
            gaps.append(prefix + "EVIDENCE_MISSING")
        coverage = pd.Series(0, index=frame.index, dtype=int)
        for scope in contract.get("scope", []):
            lo, hi = date(scope["start"]), date(scope["end"])
            if lo > hi:
                gaps.append(prefix + "INVALID_OBSERVATION_SCOPE")
            coverage += (frame.symbol.eq(scope["symbol"]) & days.between(lo, hi)).astype(int)
        if not coverage.eq(1).all():
            gaps.append(prefix + "OBSERVATION_SCOPE_MISSING_OR_AMBIGUOUS")
        if cutoff is not None:
            available = contract.get("available_at")
            if available is None:
                gaps.append(prefix + "INFORMATION_AVAILABILITY_UNKNOWN")
            elif timestamp(available) > timestamp(cutoff):
                gaps.append(prefix + "INFORMATION_NOT_YET_AVAILABLE")
            if any(timestamp(event) > timestamp(cutoff)
                   for event in contract.get("adjustment_event_dates", [])):
                gaps.append(prefix + "FUTURE_ADJUSTMENT_EVENT")
    if gaps:
        raise PriceConsumptionError(gaps)
    return frame


def raw_forward_returns(result, *, identity, calendar, field="raw_open", horizon=5,
                        entry_lag=1, unit="CNY/share", cutoff=None):
    """Raw endpoint returns on explicit sessions, with no fill across missing rows.

    These are retrospective labels, not a claim that future outcomes were known
    on the signal date. ``cutoff`` restricts the input vintage if requested.
    """
    frame = require_fields(result, {field: ("RAW_LABEL", unit)}, identity=identity, cutoff=cutoff)
    if type(horizon) is not int or horizon < 1 or type(entry_lag) is not int or entry_lag < 0:
        raise ValueError("positive horizon and nonnegative entry lag required")
    sessions = pd.DatetimeIndex(calendar)
    if (sessions.empty or sessions.has_duplicates or sessions.hasnans or sessions.tz is not None
            or not sessions.is_monotonic_increasing or not sessions.equals(sessions.normalize())
            or not pd.to_datetime(frame.date).isin(sessions).all()):
        raise ValueError("explicit unique daily session calendar required")
    values = frame[["date", "symbol", field]].copy()
    values["date"] = pd.to_datetime(values.date)
    values[field] = values[field].map(lambda v: None if pd.isna(v) else decimal_text(v))
    if values[field].dropna().map(lambda v: v <= 0).any():
        raise PriceConsumptionError([field + "/NONPOSITIVE_PRICE"])
    grid = pd.MultiIndex.from_product([sorted(values.symbol.unique()), sessions], names=["symbol", "date"])
    prices = values.set_index(["symbol", "date"])[field].reindex(grid)
    grouped = prices.groupby(level="symbol", sort=False)
    entry, exit_price = grouped.shift(-entry_lag), grouped.shift(-entry_lag-horizon)
    valid = prices.notna()
    path = valid.copy()
    for offset in range(entry_lag, entry_lag+horizon+1):
        path &= valid.groupby(level="symbol", sort=False).shift(-offset).eq(True)
    returns = pd.Series([float(b/a - 1) if ok else float("nan")
                         for a, b, ok in zip(entry, exit_price, path)], index=grid, name="raw_forward_return")
    output = values[["date", "symbol"]].merge(returns.reset_index(), on=["date", "symbol"], validate="one_to_one")
    output.attrs["price_consumption"] = {"source_id": identity[0], "data_version": identity[1],
                                         "field": field, "purpose": "RAW_LABEL", "pit": "UNVERIFIED"}
    return output


def compute_factors(result, snapshot, refs, *, identity, calendar, units, cutoff=None, **kwargs):
    """Apply resolved FactorDefinition semantics, then reuse its normal compute API."""
    refs = tuple(refs)
    requirements = {}
    for definition in snapshot.resolve(refs):
        for field, purpose in definition.consumption_requirements.items():
            if field not in units:
                raise PriceConsumptionError([field + "/REQUIRED_UNIT_UNDECLARED"])
            requirements[field] = (purpose, units[field])
    frame = require_fields(result, requirements, identity=identity, cutoff=cutoff)
    output = snapshot.compute(frame, refs, calendar=calendar, **kwargs)
    output.attrs["price_consumption"] = {"source_id": identity[0], "data_version": identity[1],
                                         "requirements": requirements, "cutoff": cutoff}
    return output


def execution_bars(result, *, identity, config, unit="CNY/share", cutoff=None):
    """Check real execution marks without consuming independent book valuations.

    Suspension/status and execution eligibility are still decided by the existing
    executor. Price qualification alone never supplies a tradable-state fact.
    """
    fields = {config.open_price_column, config.close_price_column}
    fields.update(f for f in result.data if f.startswith("raw_") or f == "execution_price")
    frame = require_fields(result, {f: ("EXECUTION", unit) for f in fields}, identity=identity, cutoff=cutoff)
    output = frame.drop(columns=[f for f in ("valuation_price", "valuation_mark", "book_value") if f in frame]).copy()
    output.attrs["price_consumption"] = {"source_id": identity[0], "data_version": identity[1],
                                         "fields": sorted(fields), "purpose": "EXECUTION", "cutoff": cutoff}
    return output
