"""Bitemporal selection: knowledge revision first, business interval second."""

from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd

from .contracts import ContractError, digest, instant, real_number


class AsOfStore:
    def __init__(self, records):
        self.records = tuple(records)
        identities = [(r.fact_id, r.revision) for r in self.records]
        if len(set(identities)) != len(identities):
            raise ContractError("duplicate fact/revision")
        self._kinds = {}
        facts = {}
        for record in self.records:
            self._kinds.setdefault(record.kind, []).append(record)
            identity = (record.kind, record.source)
            if facts.setdefault(record.fact_id, identity) != identity:
                raise ContractError("fact identity changed across revisions")

    def snapshot(self, kind, observation_end, as_of, observation_start=None):
        end, cutoff = instant(observation_end), instant(as_of)
        start = instant(observation_start) if observation_start is not None else None
        latest = {}
        for record in self._kinds.get(kind, ()):
            if record.known_at <= cutoff:
                prior = latest.get(record.fact_id)
                if prior is None or record.revision > prior.revision:
                    latest[record.fact_id] = record
        selected = []
        for record in latest.values():
            if record.withdrawn:
                continue
            if kind == "minute":
                if (
                    record.observation_end is None
                    or record.observation_end > end
                    or (start is not None and record.valid_from < start)
                ):
                    continue
            elif kind == "benchmark":
                if record.observation_end is None or record.observation_end > min(
                    end, cutoff
                ):
                    continue
            elif not (
                record.valid_from <= end
                and (record.valid_to is None or end < record.valid_to)
            ):
                continue
            selected.append(record)
        return tuple(sorted(selected, key=lambda r: (r.fact_id, r.revision)))


@dataclass(frozen=True)
class Session:
    session_id: str
    segments: tuple

    def __post_init__(self):
        if not isinstance(self.session_id, str) or not self.session_id.strip():
            raise ContractError("explicit nonempty session ID string required")
        parsed = tuple((instant(a), instant(b)) for a, b in self.segments)
        if not parsed or any(
            b <= a or (b - a) % pd.Timedelta(minutes=1) for a, b in parsed
        ):
            raise ContractError("invalid explicit session segments")
        if any(parsed[i][1] > parsed[i + 1][0] for i in range(len(parsed) - 1)):
            raise ContractError("overlapping session segments")
        object.__setattr__(self, "segments", parsed)


def validate_calendar(sessions):
    sessions = tuple(sessions)
    if len({s.session_id for s in sessions}) != len(sessions):
        raise ContractError("duplicate session ID")
    intervals = sorted((a, b) for s in sessions for a, b in s.segments)
    if any(intervals[i][1] > intervals[i + 1][0] for i in range(len(intervals) - 1)):
        raise ContractError("overlapping calendar sessions")
    return sessions


def window_requests(
    sessions, universe, frequency, latency_seconds=2, scenario="historical_simulation"
):
    from .contracts import ComputeRequest

    if latency_seconds < 0:
        raise ContractError("negative decision latency")
    for session in validate_calendar(sessions):
        for start, stop in session.segments:
            cursor = start
            while cursor + pd.Timedelta(minutes=frequency) <= stop:
                end = cursor + pd.Timedelta(minutes=frequency)
                decision = end + pd.Timedelta(seconds=latency_seconds)
                yield ComputeRequest(
                    cursor,
                    end,
                    decision,
                    decision,
                    frequency,
                    tuple(universe),
                    session.session_id,
                    scenario,
                )
                cursor = end


def memberships(store, family, request):
    scopes = [
        r
        for r in store.snapshot("scope", request.observation_end, request.as_of)
        if r.payload.get("family") == family
    ]
    for scope in scopes:
        if any(
            not isinstance(scope.payload.get(key), str)
            or not scope.payload[key].strip()
            for key in ("namespace", "taxonomy_version")
        ):
            raise ContractError("explicit scope namespace/version strings required")
        groups = scope.payload.get("group_ids")
        if (
            not isinstance(groups, (tuple, list))
            or not groups
            or any(not isinstance(g, str) or not g.strip() for g in groups)
            or len(set(groups)) != len(groups)
        ):
            raise ContractError("unique nonempty taxonomy group IDs required")
    if not scopes or len({r.payload["namespace"] for r in scopes}) != len(scopes):
        raise ContractError("explicit unique source scopes required")
    complete = {
        r.payload["namespace"]: r.payload.get("complete") is True for r in scopes
    }
    rows, used = [], list(scopes)
    for record in store.snapshot("membership", request.observation_end, request.as_of):
        p = record.payload
        if p["family"] != family or p["security_id"] not in request.universe:
            continue
        change_date = p.get("change_date")
        try:
            valid_change_date = (
                isinstance(change_date, str)
                and date.fromisoformat(change_date).isoformat() == change_date
            )
        except ValueError:
            valid_change_date = False
        if not valid_change_date:
            raise ContractError(
                "membership change_date requires a valid YYYY-MM-DD string"
            )
        if not all(
            p.get(k)
            for k in ("namespace", "taxonomy_version", "change_date", "group_id")
        ):
            raise ContractError("membership taxonomy/history missing")
        matching_scope = next(
            (s for s in scopes if s.payload["namespace"] == p["namespace"]), None
        )
        if (
            matching_scope is None
            or matching_scope.payload.get("taxonomy_version") != p["taxonomy_version"]
        ):
            raise ContractError("membership taxonomy version/scope mismatch")
        rows.append(dict(p, record_sha256=record.sha256))
        used.append(record)
    columns = [
        "security_id",
        "namespace",
        "group_id",
        "weight",
        "taxonomy_version",
        "change_date",
        "record_sha256",
    ]
    frame = pd.DataFrame(rows, columns=columns)
    if frame.duplicated(["security_id", "namespace", "group_id"]).any():
        raise ContractError("duplicate membership mapping")
    frame["weight_origin"] = "EXPLICIT"
    for _, group in frame.groupby(["security_id", "namespace"]):
        missing = group["weight"].isna()
        if missing.all():
            frame.loc[group.index, "weight"] = 1.0 / len(group)
            frame.loc[group.index, "weight_origin"] = "GENERATED_EQUAL_SPLIT"
        elif missing.any():
            raise ContractError("mixed explicit/missing membership weights")
        else:
            weights = [
                real_number(w, "membership weight", True) for w in group["weight"]
            ]
            if not np.isclose(sum(weights), 1.0, atol=1e-12, rtol=0):
                raise ContractError("membership weights do not conserve one")
    return frame, complete, tuple(used)


def snapshot_digest(records):
    return digest(sorted({r.sha256 for r in records}))
