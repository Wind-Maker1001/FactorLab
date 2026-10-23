"""Read-only market context and explicit future research view interfaces."""

from dataclasses import dataclass

import numpy as np
import pandas as pd

from factorlab.market.regime import build_market_regime_features

from .contracts import ContractError, MARKERS, digest, instant
from .store import validate_calendar


def market_context(store, sessions, request):
    sessions = validate_calendar(sessions)
    records = store.snapshot("benchmark", request.as_of, request.as_of)
    complete = {s.session_id: s for s in sessions if s.segments[-1][1] <= request.as_of}
    selected = {}
    for record in records:
        p = record.payload
        day = p["session_id"]
        price = p.get("adj_close")
        if (
            day in complete
            and record.observation_end == complete[day].segments[-1][1]
            and record.observation_end <= request.as_of
            and p.get("complete") is True
            and isinstance(price, (int, float))
            and not isinstance(price, bool)
            and np.isfinite(price)
            and price > 0
        ):
            if day in selected:
                raise ContractError("duplicate benchmark session")
            selected[day] = record
    if not selected:
        return (
            dict(
                status="MARKET_CONTEXT_UNAVAILABLE",
                calendar_sha256=calendar_digest(complete.values()),
            ),
            (),
        )
    last = max(selected, key=lambda day: complete[day].segments[-1][1])
    ordered = sorted(
        (
            s
            for s in complete.values()
            if s.segments[-1][1] <= complete[last].segments[-1][1]
        ),
        key=lambda s: s.segments[-1][1],
    )
    rows = []
    for session in ordered:
        rec = selected.get(session.session_id)
        rows.append(
            dict(
                date=regime_date(session),
                symbol="000905.SH",
                adj_close=rec.payload.get("adj_close") if rec else None,
            )
        )
    frame = pd.DataFrame(rows)
    if frame["date"].duplicated().any():
        raise ContractError("market context requires distinct UTC regime dates")
    frame["adj_close"] = pd.to_numeric(frame["adj_close"], errors="raise")
    computed = build_market_regime_features(frame)
    rec = selected[last]
    result = dict(
        status="OK",
        benchmark_session=last,
        revision=rec.revision,
        published_at=rec.published_at.isoformat(),
        available_at=rec.known_at.isoformat(),
        benchmark_observation_end=rec.observation_end.isoformat(),
        source_sha256=rec.sha256,
        age_seconds=(request.as_of - rec.observation_end).total_seconds(),
        missing_session_count=int(frame["adj_close"].isna().sum()),
        calendar_sha256=calendar_digest(ordered),
    )
    result.update(computed.iloc[-1].drop(labels=["date"]).to_dict())
    return result, tuple(selected.values())


def regime_date(session):
    """UTC date of the final observed instant; opaque IDs are identifiers only."""
    return (
        (session.segments[-1][1] - pd.Timedelta(nanoseconds=1))
        .tz_localize(None)
        .normalize()
    )


def calendar_digest(sessions):
    """Bind the applicable UTC calendar, independent of caller input ordering."""
    return digest(
        [
            dict(
                session_id=s.session_id,
                segments=[(a.isoformat(), b.isoformat()) for a, b in s.segments],
            )
            for s in sorted(
                sessions, key=lambda item: (item.segments[-1][1], item.session_id)
            )
        ]
    )


@dataclass(frozen=True)
class FutureModelInterface:
    model: str
    input_kind: str = "explicit numeric pandas.DataFrame; exact key alignment"
    feature_selection: str = "explicit opt-in only; separate from production registry"
    training: str = "NOT_IMPLEMENTED; no fit/predict calls in this PR"
    missing_values: str = (
        "existing model accepts NaN at validation; future training-only imputation must retain model policy"
    )
    labels: str = "supplied separately, training period only; no labels in this MVP"


class ResearchFeatureAdapter:
    """An explicit security view, never production DatasetBuilder admission."""

    model_interfaces = tuple(
        FutureModelInterface(m) for m in ("FactorScore", "Ridge", "LightGBM", "XGBoost")
    )

    snapshot_keys = (
        "scenario",
        "frequency",
        "session_id",
        "observation_start",
        "observation_end",
        "as_of",
        "decision_time",
    )

    @classmethod
    def _validated_index(cls, frame, *, unique=True):
        required = list(cls.snapshot_keys) + ["entity_id"]
        if not isinstance(frame, pd.DataFrame) or not set(required).issubset(frame):
            raise ContractError(
                "explicit target index requires securities and all snapshot keys"
            )
        local = frame.copy(deep=True)
        if local.empty or local[required].isna().any().any():
            raise ContractError(
                "nonempty target index with complete snapshot keys required"
            )
        for key in ("scenario", "session_id", "entity_id"):
            if any(not isinstance(x, str) or not x.strip() for x in local[key]):
                raise ContractError(
                    "nonempty security/scenario/session identifiers required"
                )
        try:
            for key in (
                "observation_start",
                "observation_end",
                "as_of",
                "decision_time",
            ):
                local[key] = local[key].map(instant)
        except (TypeError, ValueError) as exc:
            raise ContractError(
                "target snapshot requires timezone-aware times"
            ) from exc
        if any(
            isinstance(x, (bool, np.bool_))
            or not isinstance(x, (int, np.integer))
            or x not in (5, 15, 30)
            for x in local.frequency
        ):
            raise ContractError("target frequency must be 5/15/30 minutes")
        duration = pd.to_timedelta(local.frequency, unit="min")
        if (
            (local.observation_end - local.observation_start != duration).any()
            or (local.observation_end > local.as_of).any()
            or (local.as_of > local.decision_time).any()
        ):
            raise ContractError("target snapshot window/as_of/decision_time mismatch")
        if unique and local.duplicated(required).any():
            raise ContractError("duplicate target security/snapshot keys")
        return local

    def build_view(
        self,
        values,
        feature_ids,
        *,
        broadcast_market=False,
        target_index=None,
        feature_namespaces=None,
    ):
        if not feature_ids or len(set(feature_ids)) != len(feature_ids):
            raise ContractError("explicit unique features required")
        if type(broadcast_market) is not bool:
            raise ContractError(
                "market broadcast requires explicit boolean authorization"
            )
        selected = values[values.feature_id.isin(feature_ids)].copy()
        if set(selected.feature_id) != set(feature_ids):
            raise ContractError("missing selected feature")
        if selected.grain.isin(["industry", "concept"]).any():
            raise ContractError(
                "group projection requires future explicit historical membership adapter"
            )
        if (selected.grain == "market").any() and not broadcast_market:
            raise ContractError("market broadcast requires explicit opt-in")
        if not selected.grain.isin(["market", "security"]).all():
            raise ContractError("unknown native feature grain")
        keys = list(self.snapshot_keys)
        if target_index is None:
            if selected.grain.eq("market").any():
                raise ContractError(
                    "market broadcast requires explicit nonempty target index"
                )
            target_index = selected[keys + ["entity_id"]].drop_duplicates()
        if isinstance(target_index, pd.DataFrame) and "namespace" in target_index:
            raise ContractError(
                "target namespace must use explicit feature_namespaces selector"
            )
        pieces = self._validated_index(target_index)[keys + ["entity_id"]]
        namespaces = {} if feature_namespaces is None else dict(feature_namespaces)
        if set(namespaces) - set(feature_ids):
            raise ContractError("namespace selector contains unrequested feature")
        selected = self._validated_index(selected, unique=False)
        broadcasted, selected_namespaces = [], {}
        for name in feature_ids:
            f = selected[selected.feature_id == name]
            if name in namespaces:
                f = f[f.namespace == namespaces[name]]
            if f.empty or f.namespace.isna().any() or f.namespace.nunique() != 1:
                raise ContractError(
                    "explicit unique feature namespace required: " + name
                )
            if f.grain.nunique() != 1:
                raise ContractError("mixed native feature grain: " + name)
            selected_namespaces[name] = f.namespace.iloc[0]
            market = f.grain.eq("market").all()
            join = keys if market else keys + ["entity_id"]
            if f.duplicated(join).any():
                raise ContractError("ambiguous feature view keys")
            pieces = pieces.merge(
                f[join + ["value"]].rename(columns={"value": name}),
                on=join,
                how="left",
                validate="many_to_one",
                indicator="_feature_present",
            )
            if not pieces._feature_present.eq("both").all():
                raise ContractError(
                    "target snapshot unavailable for selected feature: " + name
                )
            pieces = pieces.drop(columns="_feature_present")
            if market:
                broadcasted.append(name)
                pieces[name + "__broadcast"] = True
        pieces.attrs.update(
            MARKERS,
            production_dataset_adapter="NOT_CONNECTED",
            group_projection="NOT_IMPLEMENTED",
            market_broadcast=bool(broadcasted),
            broadcast_features=tuple(broadcasted),
            feature_namespaces=selected_namespaces,
            target_snapshot_keys=tuple(keys),
        )
        return pieces
