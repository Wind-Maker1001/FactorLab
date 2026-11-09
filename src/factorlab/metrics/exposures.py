"""NAV-weighted concentration and optional, caller-supplied historical exposures."""

from __future__ import annotations

import numpy as np
import pandas as pd


_COLUMNS = ["date", "metric", "value", "status", "reason"]
_EVIDENCE_COLUMNS = {
    "symbol", "effective_date", "known_at", "source", "kind", "value", "unit", "source_kind",
}
_KINDS = ("industry", "market_cap", "momentum", "volatility")


def _timestamp(value):
    """Interpret naive evidence timestamps in the exchange's Asia/Shanghai zone."""
    try:
        stamp = pd.Timestamp(value)
        if pd.isna(stamp):
            return pd.NaT
        return (stamp.tz_localize("Asia/Shanghai") if stamp.tzinfo is None
                else stamp.tz_convert("Asia/Shanghai"))
    except (ValueError, TypeError):
        return pd.NaT


def diagnose_exposures(positions: pd.DataFrame, evidence: pd.DataFrame | None = None,
                       *, calendar=None) -> pd.DataFrame:
    """Return tidy daily exposure diagnostics without fitting a risk model.

    Positions contain unique ``date, symbol`` rows and finite signed NAV weights.
    Concentration is the sum of squared NAV weights (not normalized invested
    weights); maximum security weight is absolute. Industry weights are signed.
    Style exposures are sums of weight times the supplied value, with cash
    contributing zero, and are not Barra estimates or inferred style scores.

    Evidence uses ``symbol, effective_date, known_at, source, kind, value, unit,
    source_kind``. For each held symbol and kind, select the latest effective
    date and then latest knowledge timestamp, both strictly before 15:00
    Asia/Shanghai on the session date. Date-only effective dates apply from
    midnight. No future backfill is performed. Duplicate winning observations
    are ambiguous, including competing sources. All nonzero holdings must have
    valid observations in a common unit and source_kind; otherwise the entire
    kind is NOT_COMPUTABLE. Missing/invalid history is not a zero exposure.
    Malformed table schemas and malformed positions raise ValueError.
    """
    if not isinstance(positions, pd.DataFrame) or not {"date", "symbol", "weight"}.issubset(positions.columns):
        raise ValueError("positions require date, symbol, weight columns")
    if not positions.columns.is_unique:
        raise ValueError("positions columns must be unique")
    frame = positions.loc[:, ["date", "symbol", "weight"]].copy()
    frame["date"] = pd.to_datetime(frame["date"], errors="raise")
    if (frame.date.isna().any() or frame.date.dt.tz is not None
            or not frame.date.eq(frame.date.dt.normalize()).all()):
        raise ValueError("positions date must be timezone-naive session dates")
    if not frame.symbol.map(lambda x: isinstance(x, str) and bool(x.strip())).all():
        raise ValueError("positions symbol must be nonempty strings")
    frame["weight"] = pd.to_numeric(frame.weight, errors="raise")
    if not np.isfinite(frame.weight.to_numpy(dtype=float)).all():
        raise ValueError("positions weight must be finite")
    if frame.duplicated(["date", "symbol"]).any():
        raise ValueError("positions require unique date, symbol rows")

    history = None
    if evidence is not None:
        if (not isinstance(evidence, pd.DataFrame) or not evidence.columns.is_unique
                or not _EVIDENCE_COLUMNS.issubset(evidence.columns)):
            raise ValueError("evidence requires " + ", ".join(sorted(_EVIDENCE_COLUMNS)))
        history = evidence.copy()
        for column in ("effective_date", "known_at"):
            history[column] = pd.to_datetime(history[column].map(_timestamp), utc=True)

    rows = []

    def emit(date, metric, value=None, reason=""):
        rows.append((date, metric, value, "NOT_COMPUTABLE" if reason else "OK", reason))

    groups = dict(tuple(frame.groupby("date", sort=True)))
    dates = pd.DatetimeIndex(sorted(groups)) if calendar is None else pd.DatetimeIndex(calendar)
    if (dates.hasnans or dates.has_duplicates or dates.tz is not None
            or not dates.is_monotonic_increasing or not dates.equals(dates.normalize())
            or not frame.date.isin(dates).all()):
        raise ValueError("calendar must cover positions with unique ordered local session dates")
    for date in dates:
        day = groups.get(date, frame.iloc[:0])
        held = day.loc[day.weight.ne(0)].set_index("symbol")
        emit(date, "security_hhi", float(day.weight.pow(2).sum()))
        emit(date, "max_security_weight", float(day.weight.abs().max()) if len(day) else 0.0)
        cutoff = (date.tz_localize("Asia/Shanghai") + pd.Timedelta(hours=15)).tz_convert("UTC")
        for kind in _KINDS:
            reason = ""
            selected = None
            if history is None or history.empty:
                reason = "missing historical " + kind + " evidence"
            else:
                candidates = history.loc[
                    history.kind.eq(kind) & history.symbol.isin(held.index)
                    & history.effective_date.lt(cutoff) & history.known_at.lt(cutoff)
                ]
                # Invalid dates cannot establish eligibility, and are not filled
                # from later observations. Eligible invalid values stay selected.
                latest = candidates.sort_values(["effective_date", "known_at"]).groupby("symbol", sort=False).tail(1)
                selected = latest.set_index("symbol").reindex(held.index)
                if selected.effective_date.isna().any() or held.empty:
                    reason = "incomplete historical " + kind + " coverage"
                elif candidates.duplicated(["symbol", "effective_date", "known_at"], keep=False).any():
                    winners = candidates.merge(latest[["symbol", "effective_date", "known_at"]],
                                                on=["symbol", "effective_date", "known_at"])
                    if winners.symbol.duplicated().any():
                        reason = "ambiguous historical " + kind + " evidence"
                if not reason:
                    for column in ("source", "unit", "source_kind"):
                        if not selected[column].map(lambda x: isinstance(x, str) and bool(x.strip())).all():
                            reason = "invalid historical " + kind + " provenance"
                    if not selected.source_kind.isin(['SYNTHETIC', 'HISTORICAL']).all():
                        reason = "unqualified historical " + kind + " provenance"
                    if selected.unit.nunique() != 1 or selected.source_kind.nunique() != 1:
                        reason = "incompatible historical " + kind + " evidence"
            metric = "industry_hhi" if kind == "industry" else kind
            if reason:
                emit(date, metric, reason=reason)
                continue
            if kind == "industry":
                if not selected.value.map(lambda x: isinstance(x, str) and bool(x.strip())).all():
                    emit(date, metric, reason="invalid historical industry values")
                    continue
                weights = held.weight.groupby(selected.value).sum()
                emit(date, metric, float(weights.pow(2).sum()))
                for industry, weight in weights.sort_index().items():
                    emit(date, "industry_weight:" + industry, float(weight))
            else:
                values = pd.to_numeric(selected.value, errors="coerce")
                valid = np.isfinite(values.to_numpy(dtype=float)).all()
                if kind == "market_cap":
                    valid = valid and values.gt(0).all()
                elif kind == "volatility":
                    valid = valid and values.ge(0).all()
                if not valid:
                    emit(date, metric, reason="invalid historical " + kind + " values")
                else:
                    emit(date, metric, float((held.weight * values).sum()))
    return pd.DataFrame(rows, columns=_COLUMNS)


__all__ = ["diagnose_exposures"]
