"""Train diagnostics and descriptive comparisons for unequal-size candidate arms."""

import json
from pathlib import Path

import numpy as np
import pandas as pd

from factorlab.validation.ablation_statistics import _moments


def variation_diagnostics(train, names, *, minimum_unique_ratio=0.01):
    """Measure finite within-date variation; thresholds must be fixed before Test."""
    rows = []
    for name in names:
        values = train[name].replace([np.inf, -np.inf], np.nan)
        daily = train[["date"]].assign(value=values).groupby("date").value
        counts, unique = daily.count(), daily.nunique()
        supported = counts.ge(2)
        constant = unique.le(1) & supported
        low = unique.div(counts).le(minimum_unique_ratio) & supported
        rows.append({"factor_id": name, "finite_unique_values": int(values.nunique()),
                     "supported_dates": int(supported.sum()),
                     "constant_dates": int(constant.sum()), "low_variation_dates": int(low.sum()),
                     "status": "NOT_DETERMINED" if not supported.any() else
                     "CONSTANT" if constant[supported].all() else
                     "LOW_VARIATION" if low[supported].all() else "VARIABLE"})
    return pd.DataFrame(rows)


def summarize_candidates(daily):
    """Require identical samples, dates and labels; never silently intersect arms."""
    identity = ["rows", "paired_rows", "keys_sha256", "labels_sha256", "finite_pairs_sha256"]
    for _, part in daily.groupby(["fold", "date"]):
        if any(part[col].nunique() != 1 for col in identity):
            raise ValueError("Candidate comparisons require common samples and labels")
    expected = set(map(tuple, daily[["fold", "date"]].drop_duplicates().to_numpy()))
    rows = []
    for (group, model), part in daily.groupby(["group", "model"], sort=True):
        if part.duplicated(["fold", "date"]).any() or set(map(tuple, part[["fold", "date"]].to_numpy())) != expected:
            raise ValueError("Candidate comparisons require common scheduled dates")
        for fold, frame in [("ALL", part), *list(part.groupby("fold", sort=True))]:
            mean, std, icir, count = _moments(frame.rank_ic)
            rows.append({"group": group, "model": model, "fold": fold, "rank_ic": mean,
                         "rank_ic_std": std, "icir": icir, "valid_dates": count,
                         "scheduled_dates": len(frame),
                         "status": "DESCRIPTIVE_SYNTHETIC_ONLY" if count == len(frame) and count >= 20 and np.isfinite(icir)
                         else "NOT_DETERMINED"})
    return pd.DataFrame(rows)


def candidate_contrasts(summary):
    """Descriptive deltas only; no Test-based decision or significance claim."""
    rows = []
    for (model, fold), part in summary.groupby(["model", "fold"], sort=False):
        part = part.set_index("group")
        for group in part.index:
            control = "fixed64" if group.startswith("without_") else "baseline14"
            if group == control:
                continue
            current, baseline = part.loc[group], part.loc[control]
            delta = current.rank_ic - baseline.rank_ic
            rows.append({"model": model, "fold": fold, "group": group, "control": control,
                         "rank_ic_delta": delta, "icir_delta": current.icir - baseline.icir,
                         "degradation_observed": bool(delta < -0.05),
                         "interpretation": "DESCRIPTIVE_SYNTHETIC_ONLY; not a selection rule"})
    return pd.DataFrame(rows)


def recompute_output(output):
    """Recompute small receipts and audit feature consumption without retraining."""
    output = Path(output)
    daily = pd.read_csv(output / "daily.csv")
    recomputed = summarize_candidates(daily)
    stored = pd.read_csv(output / "summary.csv", dtype={"fold": str})
    recomputed["fold"] = recomputed.fold.astype(str)
    pd.testing.assert_frame_equal(stored, recomputed, check_exact=False, atol=1e-12)
    candidate_contrasts(recomputed).to_csv(output / "contrasts.csv", index=False)
    plans = json.loads((output / "plans.json").read_text(encoding="utf-8"))
    by_fold = {p["fold"]: p for p in plans}
    audit = pd.read_csv(output / "fit-audit.csv")
    selected = audit.loc[audit.group.eq("selected")]
    for row in selected.itertuples():
        if json.loads(row.feature_names) != by_fold[row.fold]["selected_features"]:
            raise AssertionError("Selected model features do not match frozen Train plan")
    expected_models = {"factor_score", "ridge", "lightgbm", "xgboost"}
    # Daily receipts name the actual runner/model pair, not a requested configuration.
    if set(daily.loc[daily.group.eq("selected"), "model"]) != expected_models:
        raise AssertionError("Four-model selected-arm evidence is incomplete")
    names = [r["factor_id"] for r in plans[0]["candidate_refs"]]
    pd.DataFrame([{"factor_id": n, "selected_folds": sum(n in p["selected_features"] for p in plans),
                   "total_folds": len(plans)} for n in names]).to_csv(output / "frequency.csv", index=False)
    return {"summary_recomputed": True, "common_samples_verified": True,
            "selected_four_models_verified": True, "audited_selected_stage_rows": len(selected)}
