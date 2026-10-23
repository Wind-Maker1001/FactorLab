"""Explicit intraday/grain adapter around the frozen redundancy kernel."""

from dataclasses import dataclass
from itertools import combinations

import numpy as np
import pandas as pd

from qbot.research.exploratory.factor_discovery.redundancy import diagnose_redundancy

from .contracts import ContractError


@dataclass(frozen=True)
class RedundancyPolicy:
    minimum_security_entities: int = 20
    minimum_group_entities: int = 3
    minimum_pair_coverage: float = 0.8
    minimum_observation_windows: int = 20
    minimum_trading_sessions: int = 3
    minimum_window_coverage: float = 0.8
    correlation_threshold: float = 0.98
    max_diagnostic_records: int = 500000

    def __post_init__(self):
        for name in (
            "minimum_security_entities",
            "minimum_group_entities",
            "minimum_observation_windows",
            "minimum_trading_sessions",
            "max_diagnostic_records",
        ):
            if type(getattr(self, name)) is not int or getattr(self, name) < 1:
                raise ContractError("invalid explicit diagnostic sample threshold")
        for name in (
            "minimum_pair_coverage",
            "minimum_window_coverage",
            "correlation_threshold",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not np.isfinite(value) or not 0 < value <= 1:
                raise ContractError("invalid explicit diagnostic coverage threshold")


@dataclass(frozen=True)
class _KernelOptions:
    # These legacy names stay private: dates here are observation WINDOWS.
    development_start: object
    development_end: object
    minimum_cross_section: int
    minimum_evaluation_dates: int
    minimum_pair_coverage: float
    redundancy_threshold: float
    max_diagnostic_records: int


def _status(enough, identical, pearson, spearman, threshold):
    if not enough:
        return "UNDETERMINED"
    if identical:
        return "NUMERIC_DUPLICATE"
    if not (np.isfinite(pearson) and np.isfinite(spearman)):
        return "UNDETERMINED"
    if max(abs(pearson), abs(spearman)) >= threshold:
        return "HIGHLY_REDUNDANT"
    return "NON_REDUNDANT"


def _table(rows, columns):
    """Preserve the evidence interface even when no candidate is rejected."""
    extras = sorted({key for row in rows for key in row} - set(columns))
    return pd.DataFrame(rows, columns=list(columns) + extras)


def diagnose_features(values, policy=None):
    policy = policy if policy is not None else RedundancyPolicy()
    required = [
        "scenario",
        "frequency",
        "grain",
        "namespace",
        "feature_id",
        "entity_id",
        "observation_end",
        "as_of",
        "decision_time",
        "session_id",
        "value",
        "reason",
        "input_coverage",
    ]
    if not set(required).issubset(values):
        raise ContractError("diagnostic evidence fields missing")
    keys = [
        "scenario",
        "frequency",
        "grain",
        "namespace",
        "observation_end",
        "entity_id",
        "feature_id",
    ]
    if values.duplicated(keys).any():
        raise ContractError("duplicate keys or mixed as_of snapshots: split scenarios")
    for _, g in values.groupby(["scenario", "frequency", "observation_end"]):
        if g[["as_of", "decision_time", "session_id"]].drop_duplicates().shape[0] != 1:
            raise ContractError("mixed decision schedule/as_of versions")
    quality, pairs, daily, rejected = [], [], [], []
    scopes = ["scenario", "frequency", "grain", "namespace"]
    for scope, group in values.groupby(scopes, sort=True):
        metadata = dict(zip(scopes, scope))
        for name, f in group.groupby("feature_id"):
            numeric = f.value.to_numpy(dtype=float)
            finite = np.isfinite(numeric)
            per_window = f.groupby("observation_end").value.agg(
                lambda s: s[np.isfinite(s)].nunique()
            )
            constant = bool(finite.any() and np.unique(numeric[finite]).size == 1)
            completeness = float(finite.mean())
            reason = (
                "CONSTANT"
                if constant
                else "INSUFFICIENT_COMPLETENESS" if completeness < 0.8 else "OK"
            )
            quality.append(
                dict(
                    metadata,
                    feature_id=name,
                    expected_cells=len(f),
                    finite_cells=int(finite.sum()),
                    completeness=completeness,
                    mean_input_coverage=float(f.input_coverage.mean()),
                    constant=constant,
                    duplicate_value_ratio=(
                        1 - len(np.unique(numeric[finite])) / finite.sum()
                        if finite.any()
                        else np.nan
                    ),
                    cross_section_constant_windows=(
                        int((per_window == 1).sum()) if scope[2] != "market" else 0
                    ),
                    cross_section_status=(
                        "NOT_APPLICABLE" if scope[2] == "market" else "APPLICABLE"
                    ),
                    quality_status=(
                        "QUALITY_REJECTED" if reason != "OK" else "QUALITY_DESCRIBED"
                    ),
                    reason_counts=f.reason.value_counts().to_json(),
                    rejection_reason=reason if reason != "OK" else "",
                )
            )
            if reason != "OK":
                rejected.append(
                    dict(
                        metadata,
                        feature_id=name,
                        reason=reason,
                        observed=completeness,
                        threshold=0.8,
                        evidence="native-grain quality",
                    )
                )
        names = sorted(group.feature_id.unique())
        if len(names) < 2:
            continue
        if (
            group.observation_end.nunique() * len(names) * (len(names) - 1) // 2
            > policy.max_diagnostic_records
        ):
            raise ContractError("diagnostic record budget exceeded")
        local = group.copy()
        local["date"] = pd.to_datetime(local.observation_end, utc=True).dt.tz_localize(
            None
        )
        local["symbol"] = local.entity_id
        wide = local.pivot(
            index=["date", "symbol"], columns="feature_id", values="value"
        ).reset_index()
        sessions = local.groupby("date").session_id.first()
        if scope[2] == "market":
            # Market scalars have a time-series diagnostic, never a one-entity cross-section.
            temporal = wide.set_index("date")[names]
            for left, right in combinations(names, 2):
                valid = np.isfinite(temporal[left]) & np.isfinite(temporal[right])
                paired = temporal.loc[valid, [left, right]]
                n = len(paired)
                session_count = sessions.loc[paired.index].nunique()
                coverage = n / len(temporal)
                enough = (
                    n >= policy.minimum_observation_windows
                    and session_count >= policy.minimum_trading_sessions
                    and coverage >= policy.minimum_window_coverage
                )
                pearson = float(paired.corr().iloc[0, 1]) if n > 1 else np.nan
                spearman = float(paired.rank().corr().iloc[0, 1]) if n > 1 else np.nan
                identical = bool(
                    np.array_equal(
                        np.isfinite(temporal[left]), np.isfinite(temporal[right])
                    )
                    and np.array_equal(paired[left], paired[right])
                )
                status = _status(
                    enough, identical, pearson, spearman, policy.correlation_threshold
                )
                pairs.append(
                    dict(
                        metadata,
                        left=left,
                        right=right,
                        diagnostic_kind="TIME_SERIES_DESCRIPTIVE",
                        valid_observation_windows=n,
                        total_observation_windows=len(temporal),
                        distinct_trading_sessions=int(session_count),
                        mean_pair_coverage=coverage,
                        mean_abs_pearson=abs(pearson),
                        mean_abs_spearman=abs(spearman),
                        status=status,
                    )
                )
        else:
            minimum = (
                policy.minimum_security_entities
                if scope[2] == "security"
                else policy.minimum_group_entities
            )
            options = _KernelOptions(
                wide.date.min(),
                wide.date.max(),
                minimum,
                policy.minimum_observation_windows,
                policy.minimum_pair_coverage,
                policy.correlation_threshold,
                policy.max_diagnostic_records,
            )
            _, raw = diagnose_redundancy(wide, names, [], options)
            rank_rows = []
            for left, right in combinations(names, 2):
                ranked = wide[["date", "symbol", left, right]].copy()
                paired = np.isfinite(ranked[left]) & np.isfinite(ranked[right])
                # Keep every original entity/window for coverage; rank only the
                # intersection for THIS pair. Raw masks/counts remain in `raw`.
                ranked[[left, right]] = ranked[[left, right]].where(paired, axis=0)
                ranked[[left, right]] = ranked.groupby("date")[[left, right]].rank(
                    method="average"
                )
                _, ranks = diagnose_redundancy(ranked, [left, right], [], options)
                rank_rows.append(ranks.rename(columns={"pearson": "spearman"}))
            ranks = pd.concat(rank_rows, ignore_index=True)
            evidence = raw.merge(
                ranks[["date", "left", "right", "spearman"]],
                on=["date", "left", "right"],
                validate="one_to_one",
            )
            evidence["session_id"] = evidence.date.map(sessions)
            for (left, right), f in evidence.groupby(["left", "right"]):
                good = f[
                    (f.observations >= minimum)
                    & (f.pair_coverage >= policy.minimum_pair_coverage)
                    & np.isfinite(f.pearson)
                    & np.isfinite(f.spearman)
                ]
                n = len(good)
                session_count = good.session_id.nunique()
                enough = (
                    n >= policy.minimum_observation_windows
                    and session_count >= policy.minimum_trading_sessions
                    and n / len(f) >= policy.minimum_window_coverage
                )
                pearson, spearman = (
                    good.pearson.abs().mean(),
                    good.spearman.abs().mean(),
                )
                status = _status(
                    enough,
                    f.same_values_and_missing_mask.all(),
                    pearson,
                    spearman,
                    policy.correlation_threshold,
                )
                pairs.append(
                    dict(
                        metadata,
                        left=left,
                        right=right,
                        diagnostic_kind="INTRADAY_CROSS_SECTION",
                        valid_observation_windows=n,
                        total_observation_windows=len(f),
                        distinct_trading_sessions=int(session_count),
                        minimum_entities=minimum,
                        mean_pair_coverage=float(f.pair_coverage.mean()),
                        mean_abs_pearson=pearson,
                        mean_abs_spearman=spearman,
                        status=status,
                    )
                )
            for row in evidence.rename(
                columns={"date": "observation_end", "observations": "paired_entities"}
            ).to_dict("records"):
                row["observation_end"] = (
                    pd.Timestamp(row["observation_end"]).tz_localize("UTC").isoformat()
                )
                daily.append(dict(metadata, **row))
    for row in pairs:
        if row["status"] in ("NUMERIC_DUPLICATE", "HIGHLY_REDUNDANT"):
            rejected.append(
                dict(
                    row,
                    feature_id=row["right"],
                    reason=row["status"],
                    observed=row["mean_abs_pearson"],
                    threshold=policy.correlation_threshold,
                    evidence="pairwise evidence; registry contract retained",
                )
            )
    return dict(
        quality=_table(
            quality,
            scopes
            + [
                "feature_id",
                "expected_cells",
                "finite_cells",
                "completeness",
                "mean_input_coverage",
                "constant",
                "duplicate_value_ratio",
                "cross_section_constant_windows",
                "cross_section_status",
                "quality_status",
                "reason_counts",
                "rejection_reason",
            ],
        ),
        redundancy=_table(
            pairs,
            scopes
            + [
                "left",
                "right",
                "diagnostic_kind",
                "valid_observation_windows",
                "total_observation_windows",
                "distinct_trading_sessions",
                "minimum_entities",
                "mean_pair_coverage",
                "mean_abs_pearson",
                "mean_abs_spearman",
                "status",
            ],
        ),
        redundancy_windows=_table(
            daily,
            scopes
            + [
                "observation_end",
                "session_id",
                "left",
                "right",
                "paired_entities",
                "pair_coverage",
                "pearson",
                "spearman",
                "same_values_and_missing_mask",
            ],
        ),
        rejections=_table(
            rejected,
            scopes + ["feature_id", "reason", "observed", "threshold", "evidence"],
        ),
    )


def structural_diagnostics(registry):
    seen, rows = {}, []
    for spec in registry.specs:
        signature = spec.as_dict()["structural_signature"]
        duplicate = seen.get(signature, "")
        rows.append(
            dict(
                feature_id=spec.feature_id,
                signature=signature,
                duplicate_of=duplicate,
                status="STRUCTURAL_DUPLICATE" if duplicate else "DISTINCT_CONTRACT",
            )
        )
        seen.setdefault(signature, spec.feature_id)
    return pd.DataFrame(rows)
