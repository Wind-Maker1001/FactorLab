"""Causal affine/ratio HFQ from explicit event vintages; no price filling."""

from dataclasses import asdict
from decimal import Decimal, localcontext
import math
from typing import Mapping

import numpy as np
import pandas as pd

from factorlab.data.corporate_actions.contracts import EventView
from factorlab.data.corporate_actions.validation import decimal, event_terms, known_versions, validate_snapshot
from factorlab.data.evidence import date, fail, json_bytes, require_hash, sha256, table_sha256, timestamp
from factorlab.data.schema import normalize_symbol
from .contracts import AFFINE, RATIO, AdjustmentAnchor, AdjustmentResult


def build_hfq(raw_bars: pd.DataFrame, event_view: EventView, *, method: str,
              anchor: AdjustmentAnchor, signal_as_of: Mapping) -> AdjustmentResult:
    """One resolved security per call; each date requires its own knowledge cutoff.

    Coefficients are rebuilt from the fixed anchor for that row's vintage. A
    late correction never changes previously emitted rows. Such a stitched
    vintage is flagged for a separate feature-view protocol, not admitted.
    """
    if method not in {AFFINE, RATIO} or anchor.method != method:
        fail("explicit matching source adjustment method required")
    validate_snapshot(event_view.snapshot)
    require_hash(anchor.source_sha256)
    if anchor.source_sha256 not in {a.sha256 for a in event_view.snapshot.artifacts}:
        fail("anchor lacks a bound source artifact")
    if anchor.synthetic_only and not event_view.snapshot.synthetic_only:
        fail("synthetic anchor cannot be relabelled")
    a0 = decimal(anchor.multiplier, "anchor multiplier", positive=True)
    if not isinstance(anchor.offset, Decimal) or not anchor.offset.is_finite():
        fail("explicit finite Decimal anchor offset required")
    if method == RATIO and anchor.offset:
        fail("ratio anchor cannot contain an affine offset")
    required = {"date", "symbol", "open", "high", "low", "close"}
    if (not isinstance(raw_bars, pd.DataFrame) or raw_bars.empty
            or raw_bars.columns.duplicated().any() or not required.issubset(raw_bars)):
        fail("explicit nonempty raw OHLC required")
    bars = raw_bars.copy()
    if not pd.api.types.is_datetime64_ns_dtype(bars.date.dtype) or bars.date.dt.tz is not None:
        fail("raw dates must be naive datetime64[ns]")
    if bars.date.isna().any() or not bars.date.eq(bars.date.dt.normalize()).all():
        fail("invalid raw date")
    if bars.date.duplicated().any() or not bars.date.is_monotonic_increasing:
        fail("one resolved security, strictly ordered dates required")
    if "security_id" in bars and not bars.security_id.eq(anchor.security_id).all():
        fail("raw security identity differs from anchor")
    if "security_id" not in bars and bars.symbol.nunique() != 1:
        fail("multiple codes require explicit resolved security_id")
    if not bars.symbol.map(normalize_symbol).eq(bars.symbol).all():
        fail("production adjustment requires canonical SH/SZ symbols")
    prices = bars[["open", "high", "low", "close"]]
    for c in prices:
        if not pd.api.types.is_numeric_dtype(prices[c]) or pd.api.types.is_bool_dtype(prices[c]):
            fail("real numeric raw prices required")
    values = prices.to_numpy(dtype=float)
    missing = prices.isna().sum(axis=1)
    complete = missing.eq(0)
    if ((missing != 0) & (missing != 4)).any() or np.isinf(values).any():
        fail("raw OHLC must be all present or all missing and finite")
    valid = prices.loc[complete]
    if ((valid <= 0).any(axis=1) | (valid.high < valid[["open", "close"]].max(axis=1))
            | (valid.low > valid[["open", "close"]].min(axis=1)) | (valid.high < valid.low)).any():
        fail("invalid raw OHLC relationships")
    if date(anchor.date) > bars.date.min():
        fail("anchor lies after raw history begins")
    if not isinstance(signal_as_of, Mapping):
        fail("per-date signal_as_of mapping required; end-of-history cutoff is forbidden")
    cutoffs = {pd.Timestamp(k): timestamp(v) for k, v in signal_as_of.items()}
    if len(cutoffs) != len(signal_as_of):
        fail("duplicate normalized signal cutoff date")
    if set(cutoffs) != set(bars.date):
        fail("signal_as_of must cover exactly the raw dates")
    for day, cutoff in cutoffs.items():
        local = cutoff.tz_convert("Asia/Shanghai")
        if local.tz_localize(None).normalize() != day or local.hour < 15:
            fail("signal cutoff must be on its own trading date after close")
        if timestamp(anchor.available_at) > cutoff:
            fail("anchor was unavailable at signal time")
    if date(event_view.effective_through) < bars.date.max():
        fail("event view ends before raw prices")
    if timestamp(event_view.knowledge_as_of) < max(cutoffs.values()):
        fail("event view knowledge horizon does not cover signals")
    # Validate the view instead of allowing a manually constructed view to inject revisions.
    expected = known_versions(event_view.snapshot, date(event_view.effective_through),
                              timestamp(event_view.knowledge_as_of))
    if event_view.events != expected:
        fail("event view does not match its snapshot/time boundaries")
    issues = set(event_view.snapshot.issues)
    coefficient_rows = []
    with localcontext() as context:
        context.prec = 50
        for row in bars.itertuples(index=False):
            day, cutoff = row.date, cutoffs[row.date]
            coverage = [c for c in event_view.snapshot.coverage
                        if c.security_id == anchor.security_id and c.status == "COMPLETE"
                        and date(c.start) <= date(anchor.date) and date(c.end) >= day]
            if not coverage:
                fail("INCOMPLETE event coverage; empty events are not proof of no actions")
            if not any(timestamp(c.available_at) <= cutoff for c in coverage):
                issues.add("HISTORICAL_COVERAGE_NOT_CONTEMPORANEOUS")
            groups = {}
            for event in event_view.events:
                if event.security_id != anchor.security_id:
                    continue
                if timestamp(event.available_at) <= cutoff:
                    groups.setdefault(event.event_id, []).append(event)
            selected = []
            for versions in groups.values():
                event = max(versions, key=lambda e: e.revision)
                earlier_effective = [date(e.effective_date) for e in versions if e.revision < event.revision
                                     and date(anchor.date) < date(e.effective_date) <= day]
                if earlier_effective and timestamp(event.available_at).tz_convert("Asia/Shanghai").tz_localize(None).normalize() > min(earlier_effective):
                    issues.add("LATE_EVENT_REVISION_REQUIRES_VINTAGE_FEATURE_VIEW")
                if date(anchor.date) < date(event.effective_date) <= day:
                    if timestamp(event.available_at).tz_convert("Asia/Shanghai").tz_localize(None).normalize() > date(event.effective_date):
                        issues.add("LATE_EVENT_REVISION_REQUIRES_VINTAGE_FEATURE_VIEW")
                    if event.status == "PROPOSED":
                        fail("INCOMPLETE implemented terms for an already effective event")
                    if event.status == "CANCELLED":
                        continue
                    selected.append(event)
            # A known event whose initial revision was unavailable cannot silently vanish.
            for event in event_view.snapshot.events:
                if (event.security_id == anchor.security_id and date(anchor.date) < date(event.effective_date) <= day
                        and event.event_id not in groups):
                    fail("EVENT_INFORMATION_UNAVAILABLE_AT_SIGNAL")
            selected.sort(key=lambda e: (e.effective_date, e.sequence or 0, e.event_id))
            for effective in {e.effective_date for e in selected}:
                same = [e for e in selected if e.effective_date == effective]
                if len(same) > 1 and [e.sequence for e in same] != list(range(1, len(same) + 1)):
                    fail("same-day events require explicit contiguous unique sequence")
            a, b = a0, anchor.offset
            previous_date, previous_reference = None, None
            used = []
            for event in selected:
                terms = event_terms(event)
                same_day = previous_date == event.effective_date
                if same_day:
                    if event.sequence is None or event.basis != "SEQUENTIAL_AFTER_PREVIOUS_EVENT":
                        fail("same-day events require independently proved sequential bases")
                    if terms["close"] != previous_reference:
                        fail("same-day reference does not match preceding event")
                else:
                    if event.basis != "PER_OLD_SHARE":
                        fail("first event requires old-share basis")
                    previous = bars.loc[bars.date.lt(date(event.effective_date)) & bars.close.notna(), "close"]
                    if not previous.empty and not math.isclose(float(terms["close"]), float(previous.iloc[-1]), rel_tol=1e-12, abs_tol=1e-12):
                        fail("event reference_close does not match pre-event raw price")
                ref = (terms["close"] - terms["cash"] + terms["rights_price"] * terms["rights"]) / terms["multiplier"]
                if not ref.is_finite() or ref <= 0:
                    fail("nonpositive ex-rights reference price")
                if "published_reference" in terms and terms["published_reference"] != ref:
                    fail("special/rounded published reference needs an explicit source rule")
                if method == AFFINE:
                    old_a = a
                    a = a * terms["multiplier"]
                    b = b + old_a * (terms["cash"] - terms["rights_price"] * terms["rights"])
                else:
                    a = a * terms["close"] / ref
                previous_date, previous_reference = event.effective_date, ref
                used.append("%s:%d" % (event.event_id, event.revision))
            fa, fb = float(a), float(b)
            if not math.isfinite(fa) or not math.isfinite(fb) or fa <= 0:
                fail("adjustment coefficient overflow")
            coefficient_rows.append({"date": day, "symbol": row.symbol, "multiplier": fa,
                                     "offset": fb, "event_revisions": ";".join(used),
                                     "signal_as_of": cutoff.isoformat()})
    coefficients = pd.DataFrame(coefficient_rows)
    adjusted = bars[["date", "symbol"]].copy()
    for field in ("open", "high", "low", "close"):
        adjusted["adj_" + field] = bars[field].to_numpy(dtype=float) * coefficients.multiplier.to_numpy() + coefficients.offset.to_numpy()
    present = adjusted.filter(like="adj_").to_numpy(dtype=float)
    if np.isinf(present).any() or (present[np.isfinite(present)] <= 0).any():
        fail("adjusted price overflow/nonpositive value")
    if event_view.snapshot.history_kind != "CONTEMPORANEOUS":
        issues.add("HISTORICALLY_RECONSTRUCTED")
    evidence = {"anchor": asdict(anchor), "source_id": event_view.snapshot.source_id,
                "source_version": event_view.snapshot.source_version,
                "raw_table_sha256": table_sha256(raw_bars),
                "event_versions_sha256": sha256(json_bytes([asdict(e) for e in event_view.snapshot.events])),
                "coverage_sha256": sha256(json_bytes([asdict(c) for c in event_view.snapshot.coverage])),
                "coefficients_sha256": table_sha256(coefficients),
                "price_representation": "HFQ_NOT_INVESTOR_TOTAL_RETURN", "decimal_precision": 50,
                "history_kind": event_view.snapshot.history_kind}
    return AdjustmentResult(adjusted, coefficients, method, evidence, tuple(sorted(issues)),
                            event_view.snapshot.synthetic_only or anchor.synthetic_only)
