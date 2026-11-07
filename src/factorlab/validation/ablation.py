"""Strict 2x2 selection/direction orchestration around the existing runner.

The splitter, selector, model factory, fitting and Validation tuning algorithms
are reused unchanged. Each fold's Train decision is computed once, before any
arm is fitted. The fixed specification must be committed before studying Test.
"""

from dataclasses import asdict, dataclass, replace
import json
from pathlib import Path
import time
import tracemalloc

import numpy as np
import pandas as pd

from factorlab.dataset.identity import IDENTITY_ATTR, check_identity, frame_hash, json_hash
from factorlab.metrics._factor_arrays import _PreparedFactors
from factorlab.metrics.factor import _daily_ic, _summarise_ic
from factorlab.models.base import ResearchModel
from factorlab.validation.factor_selection import TrainFactorSelector
from factorlab.validation.splitter import ExpandingWalkForwardSplitter
from factorlab.validation.walk_forward import WalkForwardRunner
from factorlab.validation.ablation_statistics import daily_receipts, summarize_ablation, selection_stability
from factorlab.validation.ablation_resources import ResidentMemorySample


@dataclass(frozen=True)
class AblationConfig:
    fixed_features: tuple[str, ...]
    fixed_specification: str
    legacy_features: tuple[str, ...] = ()

    def __post_init__(self):
        for name in ("fixed_features", "legacy_features"):
            names = tuple(getattr(self, name))
            if len(set(names)) != len(names) or any(not isinstance(n, str) or not n for n in names):
                raise ValueError("Unique feature names required")
            object.__setattr__(self, name, names)
        if not self.fixed_features or not self.fixed_specification.strip():
            raise ValueError("Pre-Test fixed features and specification required")


class _OneFold(ExpandingWalkForwardSplitter):
    """Replay an already audited fold; do not implement another splitter."""

    def __init__(self, source, fold):
        self.__dict__.update(source.__dict__)
        self.fold = fold

    def split(self, data, **kwargs):
        yield self.fold


class _TimedRunner(WalkForwardRunner):
    def _fit_and_select(self, *args, **kwargs):
        started = time.perf_counter()
        result = super()._fit_and_select(*args, **kwargs)
        self.fit_validation_seconds = time.perf_counter() - started
        return result


@dataclass
class AblationResult:
    daily: pd.DataFrame
    metrics: pd.DataFrame
    contrasts: pd.DataFrame
    volatility: pd.DataFrame
    plans: pd.DataFrame
    frequency: pd.DataFrame
    jaccard: pd.DataFrame
    performance: pd.DataFrame
    audit: pd.DataFrame
    validation: pd.DataFrame
    quality: pd.DataFrame
    redundancy: pd.DataFrame
    manifest: dict

    def write(self, output):
        output = Path(output)
        output.mkdir(parents=True, exist_ok=True)
        for name in ("daily", "metrics", "contrasts", "volatility", "frequency", "jaccard",
                     "performance", "audit", "validation", "quality", "redundancy"):
            getattr(self, name).to_csv(output / (name + ".csv"), index=False)
        self.performance.groupby(["group", "model"], sort=True).agg(
            runner_seconds=("runner_seconds", "sum"), cpu_seconds=("cpu_seconds", "sum"),
            fit_validation_seconds=("fit_validation_seconds", "sum"),
            python_peak_bytes=("python_peak_bytes", "max"), sampled_peak_rss_bytes=("sampled_peak_rss_bytes", "max"),
            candidate_fits=("candidate_fits", "sum")).reset_index().to_csv(output / "performance-summary.csv", index=False)
        (output / "plans.json").write_text(self.plans.to_json(orient="records", indent=2) + "\n", encoding="utf-8")
        (output / "manifest.json").write_text(json.dumps(self.manifest, indent=2, allow_nan=False) + "\n", encoding="utf-8")


class FactorAblationRunner:
    def __init__(self, splitter, selector, config, *, model_options=None,
                 minimum_cross_section=2, top_k=20, random_seed=42):
        if type(selector) is not TrainFactorSelector or not isinstance(config, AblationConfig):
            raise ValueError("Pinned TrainFactorSelector and AblationConfig required")
        if len(config.fixed_features) != selector.config.k:
            raise ValueError("Fixed K must equal dynamic K")
        if not set(config.fixed_features + config.legacy_features) <= set(selector.feature_names):
            raise ValueError("All arms must use the same pinned candidate source")
        self.splitter, self.config = splitter, config
        # A strict K is necessary for factorial attribution; never pad a shortfall.
        self.selector = replace(selector, config=replace(selector.config,
            shortfall_policy="error", minimum_selected=selector.config.k, orient_by_train_ic=True))
        self.model_options = model_options or {
            "factor_score": {}, "ridge": {}, "lightgbm": {}, "xgboost": {}}
        reserved = {"model_name", "feature_names", "factor_weights", "factor_selector",
            "dynamic_factor_score_weighting", "normalize_by_absolute_weight", "model_directory",
            "date_col", "symbol_col", "target_col", "regime_col", "top_k", "minimum_cross_section", "random_seed"}
        for model, options in self.model_options.items():
            if model not in ("factor_score", "ridge", "lightgbm", "xgboost") or reserved.intersection(options):
                raise ValueError("Model options must not override ablation controls")
        self.minimum_cross_section, self.top_k, self.random_seed = minimum_cross_section, top_k, random_seed

    def prepare(self, data, *, calendar=None):
        """Freeze Train decisions; no Validation/Test targets enter this method."""
        validator = WalkForwardRunner(self.splitter, model_name="ridge", feature_names=self.selector.feature_names,
                                      factor_selector=self.selector)
        prepared = validator._validate_data(data)
        if IDENTITY_ATTR in data.attrs:
            identity = check_identity(data.attrs[IDENTITY_ATTR])
            if frame_hash(data) != identity.get("dataset_content_hash"):
                raise ValueError("Dataset identity content mismatch")
            if (identity.get("snapshot_hash") != self.selector.snapshot.snapshot_hash or
                {json_hash(r) for r in identity.get("factors", [])} !=
                    {json_hash(asdict(r)) for r in self.selector.factor_refs}):
                raise ValueError("Dataset identity candidate pins mismatch")
        folds = list(self.splitter.split(prepared, calendar=calendar))
        validator._assert_unique_test_observations(prepared, folds)
        if not folds:
            raise ValueError("At least one audited fold required")
        plans, quality, redundancy = [], [], []
        for fold in folds:
            train = prepared.iloc[fold.train_indices]
            if train.exit_date.max() >= fold.audit.validation_first_date:
                raise ValueError("Unmatured Train labels")
            selected = self.selector.select(train)
            quality.append(selected.quality.assign(fold=fold.fold))
            redundancy.append(selected.redundancy.assign(fold=fold.fold))
            # Fixed arms retain their predeclared features even if a dynamic
            # quality/redundancy gate would reject them. Only signs are learned.
            names = self.config.fixed_features
            workspace = _PreparedFactors.create(train, list(names), "EXCESS_RET5")
            fixed_daily = _daily_ic(workspace if workspace is not None else train,
                list(names), "EXCESS_RET5", self.selector.config.minimum_cross_section, "ALL")
            stats = _summarise_ic(fixed_daily).set_index("factor")
            fixed_signs = {}
            for name in names:
                mean = stats.loc[name, "RankIC Mean"]
                valid_dates = fixed_daily.loc[fixed_daily.factor.eq(name), "RankIC"].notna().sum()
                if not np.isfinite(mean) or valid_dates < self.selector.config.minimum_dates:
                    raise ValueError("Insufficient Train direction evidence: " + name)
                fixed_signs[name] = -1 if mean < 0 else 1
            arm_names = {"A": names, "B": names, "C": selected.feature_names, "D": selected.feature_names}
            if self.config.legacy_features:
                arm_names["E"] = self.config.legacy_features
            for group, features in arm_names.items():
                signs = fixed_signs if group == "B" else dict(zip(selected.feature_names, selected.directions)) if group == "D" else dict.fromkeys(features, 1)
                plan = {"group": group, "fold": fold.fold, "selected_features": list(features),
                    "directions": signs, "fixed_specification": self.config.fixed_specification,
                    "fixed_specification_sha256": json_hash(asdict(self.config)),
                    "dynamic_selection_hash": selected.receipt["selection_hash"] if group in ("C", "D") else None,
                    "train_sha256": selected.receipt["selection_train_sha256"],
                    "train_max_exit": train.exit_date.max().isoformat(),
                    "train_window": [d.isoformat() for d in fold.train_window],
                    "validation_window": [d.isoformat() for d in fold.validation_window],
                    "test_window": [d.isoformat() for d in fold.test_window],
                    "purge_days": self.splitter.purge_days, "embargo_days": self.splitter.embargo_days,
                    "direction_scope": "mature_train_only" if group in ("B", "D") else "disabled"}
                plan["plan_sha256"] = json_hash(plan)
                plans.append(plan)
        return prepared, folds, pd.DataFrame(plans), pd.concat(quality, ignore_index=True), pd.concat(redundancy, ignore_index=True)

    def run(self, data, *, calendar=None, model_directory):
        if tracemalloc.is_tracing():
            raise ValueError("Ablation owns tracemalloc for comparable fold measurements")
        calendar = None if calendar is None else tuple(calendar)
        started = time.perf_counter()
        tracemalloc.start()
        try:
            prepared, folds, plans, quality, redundancy = self.prepare(data, calendar=calendar)
            selection_peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        selection_seconds = time.perf_counter() - started
        daily, audit, performance, validation = [], [], [], []
        directory = Path(model_directory)
        for group, group_plans in plans.groupby("group", sort=True):
            for model, options in self.model_options.items():
                for fold, plan in zip(folds, group_plans.to_dict("records")):
                    features = tuple(plan["selected_features"])
                    transformed = prepared.copy()
                    transformed.attrs.clear()  # original identity is bound in manifest
                    for name, sign in plan["directions"].items():
                        transformed[name] = transformed[name].replace([np.inf, -np.inf], np.nan) * sign
                    weights = {"factor_weights": dict.fromkeys(features, 1 / len(features)),
                               "normalize_by_absolute_weight": False} if model == "factor_score" else {}
                    runner = _TimedRunner(_OneFold(self.splitter, fold), model_name=model,
                        feature_names=features, minimum_cross_section=self.minimum_cross_section,
                        top_k=self.top_k, random_seed=self.random_seed,
                        model_directory=directory / group / model, **weights, **options)
                    wall, cpu = time.perf_counter(), time.process_time()
                    tracemalloc.start()
                    try:
                        with ResidentMemorySample() as memory:
                            result = runner.run(transformed, calendar=calendar)
                        peak = tracemalloc.get_traced_memory()[1]
                    finally:
                        tracemalloc.stop()
                    performance.append({"group": group, "model": model, "fold": fold.fold,
                        "runner_seconds": time.perf_counter() - wall, "cpu_seconds": time.process_time() - cpu,
                        "fit_validation_seconds": runner.fit_validation_seconds, "python_peak_bytes": peak,
                        "sampled_peak_rss_bytes": memory.peak, "baseline_rss_bytes": memory.baseline,
                        "candidate_fits": len(result.validation_metrics), "features": len(features)})
                    # A saved model must reproduce the actual selected model's
                    # Test output exactly on the same transformed observations.
                    fitted = ResearchModel.load(result.model_paths[0])
                    if tuple(fitted.feature_names) != features:
                        raise AssertionError("Saved feature order mismatch")
                    test = transformed.iloc[fold.test_indices]
                    expected = pd.Series(fitted.predict(test[list(features)]).to_numpy(),
                        index=pd.MultiIndex.from_frame(test[["date", "symbol"]])).sort_index()
                    actual = result.predictions.set_index(["date", "symbol"]).score.sort_index()
                    np.testing.assert_array_equal(expected, actual)
                    bound = result.diagnostic_audit.copy()
                    bound["group"], bound["plan_sha256"] = group, plan["plan_sha256"]
                    bound["restored_test_predictions_exact"] = True
                    bound["model_prediction_identity"] = [json_hash({
                        "plan": plan["plan_sha256"], "model": r.saved_model_sha256,
                        "fit": r.fit_source_sha256, "input": r.source_sha256,
                        "predictions": r.prediction_sha256, "stage": r.stage})
                        for r in bound.itertuples()]
                    result.model_paths[0].with_suffix(".ablation.json").write_text(json.dumps({
                        "plan": plan, "model_options": options,
                        "audit": json.loads(bound.to_json(orient="records", date_format="iso"))},
                        indent=2, allow_nan=False) + "\n", encoding="utf-8")
                    audit.append(bound)
                    validation.append(result.validation_metrics.assign(group=group))
                    daily.append(daily_receipts(result.predictions, group=group, model=model,
                                               minimum_cross_section=self.minimum_cross_section))
        daily = pd.concat(daily, ignore_index=True)
        metrics, contrasts, volatility = summarize_ablation(daily)
        frequency, jaccard = selection_stability(plans, self.selector.feature_names)
        manifest = {"schema": "FACTORLAB_ABLATION_V1", "config": asdict(self.config),
            "snapshot_hash": self.selector.snapshot.snapshot_hash,
            "candidate_refs": [asdict(r) for r in self.selector.factor_refs],
            "selection_config": asdict(self.selector.config), "model_options": self.model_options,
            "top_k": self.top_k, "minimum_cross_section": self.minimum_cross_section, "random_seed": self.random_seed,
            "dataset_sha256": frame_hash(prepared), "dataset_identity": data.attrs.get(IDENTITY_ATTR),
            "fold_count": len(folds), "shared_selection_seconds": selection_seconds,
            "shared_selection_python_peak_bytes": selection_peak,
            "factor_score": "positive equal 1/K on signed X; sum(abs(weights))=1; no second sign or scale",
            "statistics": "daily equal weight; std ddof=1; ICIR unannualized, undefined at std <= 1e-12; strict full-date paired support",
            "performance_scope": "runner includes candidate fitting, validation selection, stage diagnostics, save; excludes transforms and reload proof; shared selection counted once",
            "memory_scope": "per-fold incremental tracemalloc peak; process RSS sampled every 20ms (lower bound on peak, includes native libraries and retained shared state); arm summary takes max fold peak",
            "interpretation": "descriptive synthetic results only; no significance or market efficacy claim"}
        return AblationResult(daily, metrics, contrasts, volatility, plans, frequency, jaccard,
            pd.DataFrame(performance), pd.concat(audit, ignore_index=True), pd.concat(validation, ignore_index=True),
            quality, redundancy, manifest)
