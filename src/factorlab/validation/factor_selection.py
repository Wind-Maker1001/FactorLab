"""Pinned, deterministic factor selection on one already-purged Train window.

This module does not split data, fit models, or inspect Validation/Test. RankIC
and daily pair correlations use the existing FactorLab evaluation kernels.
"""

from dataclasses import asdict, dataclass
import math

import numpy as np
import pandas as pd

from factorlab.dataset.identity import frame_hash, json_hash
from factorlab.exploratory.factor_discovery.contracts import DiscoveryConfig
from factorlab.exploratory.factor_discovery.redundancy import diagnose_redundancy
from factorlab.factors.registry_v2 import FactorRef, FactorRegistrySnapshot
from factorlab.metrics.factor import _daily_ic, _summarise_ic
from factorlab.metrics._factor_arrays import _PreparedFactors


class FactorSelectionError(ValueError):
    """Selection cannot produce the explicitly required minimum evidence."""

    def __init__(self, message, *, quality=None, redundancy=None):
        super().__init__(message)
        self.quality = quality
        self.redundancy = redundancy


@dataclass(frozen=True)
class FactorSelectionConfig:
    k: int = 10
    minimum_selected: int = 1
    shortfall_policy: str = "use_available"
    minimum_coverage: float = 0.8
    maximum_missing_ratio: float = 0.2
    maximum_nonfinite_ratio: float = 0.0
    minimum_observations: int = 100
    minimum_cross_section: int = 20
    minimum_dates: int = 20
    minimum_abs_rank_ic: float = 0.01
    minimum_direction_consistency: float = 0.5
    minimum_pair_coverage: float = 0.8
    redundancy_threshold: float = 0.95
    orient_by_train_ic: bool = True

    def __post_init__(self):
        for name in ("k", "minimum_selected", "minimum_observations",
                     "minimum_cross_section", "minimum_dates"):
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise FactorSelectionError("Positive integer required: " + name)
        if self.minimum_cross_section < 2 or self.minimum_dates < 2:
            raise FactorSelectionError("At least two cross-section rows and dates required")
        if self.minimum_selected > self.k:
            raise FactorSelectionError("minimum_selected exceeds k")
        for name in ("minimum_coverage", "maximum_missing_ratio", "maximum_nonfinite_ratio",
                     "minimum_abs_rank_ic", "minimum_direction_consistency",
                     "minimum_pair_coverage", "redundancy_threshold"):
            value = getattr(self, name)
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value) or not 0 <= value <= 1):
                raise FactorSelectionError("Finite ratio in [0, 1] required: " + name)
        if self.minimum_pair_coverage == 0 or self.redundancy_threshold == 0:
            raise FactorSelectionError("Positive pair coverage and redundancy threshold required")
        if self.shortfall_policy not in ("use_available", "error"):
            raise FactorSelectionError("Unknown shortfall_policy")
        if type(self.orient_by_train_ic) is not bool:
            raise FactorSelectionError("orient_by_train_ic must be boolean")


@dataclass(frozen=True)
class FactorSelectionResult:
    feature_names: tuple[str, ...]
    directions: tuple[int, ...]
    quality: pd.DataFrame
    redundancy: pd.DataFrame
    receipt: dict

    def transform(self, frame):
        """Apply Train-frozen signs; models retain their own missing-value rules.

        Nonfinite candidate values are measured before selection. If a caller
        explicitly permits them, selected infinities become missing model inputs.
        """
        result = frame.copy()
        for name, direction in zip(self.feature_names, self.directions):
            result[name] = result[name].replace([np.inf, -np.inf], np.nan) * direction
        return result


@dataclass(frozen=True)
class TrainFactorSelector:
    snapshot: FactorRegistrySnapshot
    factor_refs: tuple[FactorRef, ...]
    config: FactorSelectionConfig = FactorSelectionConfig()

    RULE_VERSION = "TRAIN_FACTOR_SELECTION_V1"

    def __post_init__(self):
        if type(self.snapshot) is not FactorRegistrySnapshot or type(self.config) is not FactorSelectionConfig:
            raise FactorSelectionError("Frozen snapshot and selection config required")
        refs = tuple(self.factor_refs)
        self.snapshot.resolve(refs)
        if not refs:
            raise FactorSelectionError("Nonempty pinned candidate list required")
        # Input list order is not a quality signal. Identity breaks all ties.
        object.__setattr__(self, "factor_refs", tuple(sorted(
            refs, key=lambda r: (r.factor_id, r.version, r.definition_hash))))

    @property
    def feature_names(self):
        return tuple(ref.factor_id for ref in self.factor_refs)

    def select(self, train, *, target_col="EXCESS_RET5", date_col="date", symbol_col="symbol"):
        cfg, names = self.config, self.feature_names
        if len({date_col, symbol_col, target_col, *names}) != len(names) + 3:
            raise FactorSelectionError("Candidate names collide with semantic columns")
        local = train[[date_col, symbol_col, target_col, *names]].rename(
            columns={date_col: "date", symbol_col: "symbol", target_col: "__target"}).copy()
        local = local.sort_values(["date", "symbol"]).reset_index(drop=True)
        if local.empty or local.duplicated(["date", "symbol"]).any():
            raise FactorSelectionError("Nonempty unique Train keys required")
        input_hash = frame_hash(local)
        rows, representatives, equivalents = [], [], {}

        def root_hash(ref):
            definition = self.snapshot.get(ref.factor_id, ref.version)
            return (root_hash(definition.dependencies[0]) if definition.kind == "alias"
                    else definition.definition_hash)

        for ref in self.factor_refs:
            name = ref.factor_id
            raw = local[name].to_numpy(dtype=float, na_value=np.nan)
            finite = np.isfinite(raw)
            row = {**asdict(ref), "coverage": float(finite.mean()),
                   "missing_ratio": float(np.isnan(raw).mean()),
                   "nonfinite_ratio": float(np.isinf(raw).mean()),
                   "valid_observations": int(finite.sum()), "reason": "",
                   "selected_order": None, "direction": None}
            equivalent = equivalents.get(root_hash(ref))
            if equivalent is not None:
                same = np.array_equal(raw, local[equivalent].to_numpy(dtype=float), equal_nan=True)
                row.update(reason="duplicate_definition_or_alias" if same else "identity_value_conflict",
                           compared_with=equivalent)
            else:
                equivalents[root_hash(ref)] = name
                representatives.append(name)
            rows.append(row)

        # Evaluate each distinct pinned definition once. Invalid values remain
        # missing evidence and never become zero RankIC observations.
        local[list(names)] = local[list(names)].replace([np.inf, -np.inf], np.nan)
        workspace = _PreparedFactors.create(local, representatives, "__target")
        daily = _daily_ic(workspace if workspace is not None else local, representatives,
                          "__target", cfg.minimum_cross_section, "ALL")
        summary = _summarise_ic(daily).set_index("factor")
        for row in rows:
            if row["reason"] == "identity_value_conflict":
                row.update(rank_ic_mean=np.nan, rank_ic_std=np.nan, rank_icir=np.nan,
                           valid_dates=0, valid_cross_sections=0, direction_consistency=np.nan,
                           evaluated_as=None, paired_label_observations=0)
                continue
            evaluated = row.get("compared_with", row["factor_id"])
            stats = summary.loc[evaluated]
            valid = daily.loc[daily.factor.eq(evaluated), "RankIC"].dropna()
            mean = float(stats["RankIC Mean"])
            sign = -1 if mean < 0 else 1
            consistency = float((valid * sign > 0).mean()) if len(valid) else np.nan
            row.update(rank_ic_mean=mean, rank_ic_std=float(stats["RankIC Std"]),
                       rank_icir=float(stats["RankICIR"]), valid_dates=int(len(valid)),
                       valid_cross_sections=int(len(valid)),
                       direction_consistency=consistency, evaluated_as=evaluated)
            row["paired_label_observations"] = int(daily.loc[
                daily.factor.eq(evaluated), "observations"].sum())
            failures = []
            if row["reason"]:
                continue
            for failed, reason in (
                (row["coverage"] < cfg.minimum_coverage, "coverage"),
                (row["missing_ratio"] > cfg.maximum_missing_ratio, "missing_ratio"),
                (row["nonfinite_ratio"] > cfg.maximum_nonfinite_ratio, "nonfinite_ratio"),
                (row["valid_observations"] < cfg.minimum_observations, "observations"),
                (row["paired_label_observations"] < cfg.minimum_observations, "paired_label_observations"),
                (len(valid) < cfg.minimum_dates, "insufficient_valid_dates"),
                (not np.isfinite(mean) or not np.isfinite(row["rank_ic_std"]), "undefined_rank_ic"),
                (np.isfinite(mean) and abs(mean) < cfg.minimum_abs_rank_ic, "weak_rank_ic"),
                (not np.isfinite(consistency) or consistency < cfg.minimum_direction_consistency,
                 "unstable_direction"),
            ):
                if failed:
                    failures.append(reason)
            row["reason"] = ";".join(failures)

        qualified = sorted((r for r in rows if not r["reason"]), key=lambda r: (
            -abs(r["rank_ic_mean"]), -r["direction_consistency"], -r["coverage"],
            r["factor_id"], r["version"], r["definition_hash"]))
        qualified_names = [r["factor_id"] for r in qualified]
        pairs = pd.DataFrame()
        if len(qualified) > 1:
            diagnostic_config = DiscoveryConfig(
                development_start=local.date.min().date().isoformat(),
                development_end=local.date.max().date().isoformat(),
                minimum_cross_section=cfg.minimum_cross_section,
                minimum_evaluation_dates=cfg.minimum_dates,
                minimum_pair_coverage=cfg.minimum_pair_coverage,
                redundancy_threshold=cfg.redundancy_threshold)
            pairs, _ = diagnose_redundancy(local, qualified_names, [], diagnostic_config,
                                           summary_only=True)
        lookup = {frozenset((r["left"], r["right"])): r for r in pairs.to_dict("records")}
        retained = []
        # Determine an independent qualified pool before Top-K. Insufficient
        # correlation evidence rejects the lower-ranked member conservatively.
        for row in qualified:
            name = row["factor_id"]
            for other in retained:
                pair = lookup[frozenset((name, other["factor_id"]))]
                if pair["status"] != "NON_REDUNDANT":
                    row.update(reason=pair["status"].lower(), compared_with=other["factor_id"])
                    break
            if not row["reason"]:
                retained.append(row)
        selected = retained[:cfg.k]
        for row in retained[cfg.k:]:
            row["reason"] = "outside_top_k"
        for order, row in enumerate(selected):
            row.update(reason="selected", selected_order=order,
                       direction=(-1 if row["rank_ic_mean"] < 0 else 1)
                       if cfg.orient_by_train_ic else 1)
        required = cfg.k if cfg.shortfall_policy == "error" else cfg.minimum_selected
        if len(selected) < required:
            reasons = {r["factor_id"]: r["reason"] for r in rows}
            raise FactorSelectionError("Only %d eligible factors; require %d: %s" % (
                len(selected), required, reasons), quality=pd.DataFrame(rows), redundancy=pairs)
        chosen = tuple(r["factor_id"] for r in selected)
        directions = tuple(r["direction"] for r in selected)
        quality = pd.DataFrame(rows)
        receipt = {
            "rule_version": self.RULE_VERSION, "parameters": asdict(cfg),
            "snapshot_hash": self.snapshot.snapshot_hash,
            "candidate_refs": [asdict(r) for r in self.factor_refs],
            "selected_refs": [asdict(next(r for r in self.factor_refs if r.factor_id == n)) for n in chosen],
            "selected_features": list(chosen), "directions": dict(zip(chosen, directions)),
            "selection_train_sha256": input_hash,
            "quality_sha256": frame_hash(quality), "redundancy_sha256": frame_hash(pairs),
            "eligible_count": len(retained), "selected_count": len(chosen),
            "shortfall": len(chosen) < cfg.k,
        }
        receipt["selection_hash"] = json_hash(receipt)
        return FactorSelectionResult(chosen, directions, quality, pairs, receipt)
