"""Recomputable descriptive ablation statistics from small daily receipts.

No significance tests: folds of an expanding walk-forward are not independent.
Comparisons fail closed on differing observation/label identities or undefined
daily metrics; no intersection of a poorly performing arm is silently taken.
"""

from itertools import combinations

import numpy as np
import pandas as pd

from factorlab.dataset.identity import frame_hash
from factorlab.metrics.prediction import evaluate_predictions


CONTRASTS = {
    "B-A": {"B": 1, "A": -1},
    "C-A": {"C": 1, "A": -1},
    "D-A": {"D": 1, "A": -1},
    "C-B": {"C": 1, "B": -1},
    "D-B": {"D": 1, "B": -1},
    "D-C": {"D": 1, "C": -1},
    "D-C-B+A": {"D": 1, "C": -1, "B": -1, "A": 1},
}


def daily_receipts(predictions, *, group, model, minimum_cross_section=2):
    """Retain daily ICs plus the identities needed for strict paired analysis."""
    metrics = evaluate_predictions(predictions, top_k=1,
        minimum_cross_section=minimum_cross_section, fold_col="fold").daily
    rows = []
    for (fold, date), part in predictions.groupby(["fold", "date"], sort=True):
        part = part.sort_values("symbol").reset_index(drop=True)
        finite = np.isfinite(part["score"]) & np.isfinite(part["EXCESS_RET5"])
        stat = metrics.loc[metrics.fold.eq(fold) & metrics.date.eq(date)].iloc[0]
        rows.append({"group": group, "model": model, "fold": int(fold),
            "date": pd.Timestamp(date).isoformat(), "rows": len(part),
            "paired_rows": int(finite.sum()),
            "keys_sha256": frame_hash(part[["date", "symbol"]]),
            "labels_sha256": frame_hash(part[["date", "symbol", "EXCESS_RET5"]]),
            "finite_pairs_sha256": frame_hash(part.loc[finite, ["date", "symbol"]]),
            "ic": stat.prediction_ic, "rank_ic": stat.prediction_rank_ic})
    return pd.DataFrame(rows)


def _moments(values):
    values = pd.Series(values, dtype=float)
    valid = values[np.isfinite(values)]
    mean = float(valid.mean()) if len(valid) else np.nan
    std = float(valid.std(ddof=1)) if len(valid) > 1 else np.nan
    # IC is bounded in [-1, 1]. Machine-scale dispersion in a nearly perfect
    # synthetic correlation is not evidence for an enormous finite ICIR.
    return mean, std, mean / std if std > 1e-12 else np.nan, len(valid)


def summarize_ablation(daily):
    """Return fold/overall metrics, all six paired contrasts and interaction.

    ICIR = daily mean / daily sample std, unannualized; std <= 1e-12 is
    numerically zero and yields an undefined ICIR. A fold contrast is
    available only if *every* scheduled date has the same observations/labels
    and finite metrics in all required arms. Standalone means retain explicit
    valid/total date counts. E is excluded from every factorial contrast.
    """
    keys = ["model", "group", "fold", "date"]
    if daily.empty or daily.duplicated(keys).any():
        raise ValueError("Nonempty unique daily ablation receipts required")
    metrics, paired = [], []
    for (model, group), arm in daily.groupby(["model", "group"], sort=True):
        for fold, part in [("ALL", arm), *list(arm.groupby("fold", sort=True))]:
            row = {"model": model, "group": group, "fold": fold, "total_dates": len(part)}
            for metric in ("ic", "rank_ic"):
                mean, std, ratio, count = _moments(part[metric])
                row.update({metric: mean, metric + "_std": std,
                            metric + "ir": ratio, metric + "_valid_dates": count})
            metrics.append(row)
    for (model, fold), part in daily.groupby(["model", "fold"], sort=True):
        for contrast, coefficients in CONTRASTS.items():
            arms = {g: part.loc[part.group.eq(g)].set_index("date").sort_index()
                    for g in coefficients}
            dates = sorted(set().union(*(set(a.index) for a in arms.values())))
            identity_ok = []
            for date in dates:
                entries = [a.loc[date] for a in arms.values() if date in a.index]
                identity_ok.append(len(entries) == len(arms) and all(
                    len({r[c] for r in entries}) == 1 for c in
                    ("keys_sha256", "labels_sha256", "finite_pairs_sha256")))
            row = {"model": model, "fold": int(fold), "contrast": contrast,
                   "total_dates": len(dates), "identity_mismatch_dates": identity_ok.count(False)}
            for metric in ("ic", "rank_ic"):
                valid = sum(ok and all(np.isfinite(a.loc[d, metric]) for a in arms.values())
                            for d, ok in zip(dates, identity_ok))
                status = ("OK" if dates and valid == len(dates) else
                          "OBSERVATION_MISMATCH" if not all(identity_ok) else "UNDEFINED_DAILY_METRIC")
                row[metric + "_status"] = status
                row[metric + "_paired_dates"] = valid
                for suffix, position in (("", 0), ("ir", 2)):
                    values = {g: _moments(a[metric])[position] for g, a in arms.items()}
                    value = sum(coefficients[g] * v for g, v in values.items())
                    row[metric + suffix + "_delta"] = value if status == "OK" else np.nan
                    if suffix:
                        row[metric + suffix + "_status"] = (status if status != "OK" else
                            "OK" if all(np.isfinite(v) for v in values.values()) else "UNDEFINED_ICIR")
            paired.append(row)
    metrics, paired = pd.DataFrame(metrics), pd.DataFrame(paired)
    volatility = []
    for (model, group), part in metrics.loc[metrics.fold.ne("ALL")].groupby(["model", "group"]):
        row = {"model": model, "group": group, "folds": len(part)}
        for metric in ("ic", "rank_ic", "icir", "rank_icir"):
            row[metric + "_fold_std"] = _moments(part[metric])[1]
            row[metric + "_valid_folds"] = int(np.isfinite(part[metric]).sum())
        volatility.append(row)
    return metrics, paired, pd.DataFrame(volatility)


def selection_stability(plans, candidate_names):
    """Frequency includes zero-selection candidates; Jaccard covers all pairs."""
    frequencies, stability = [], []
    for group, part in plans.groupby("group", sort=True):
        part = part.sort_values("fold")
        for name in candidate_names:
            count = sum(name in ns for ns in part.selected_features)
            frequencies.append({"group": group, "factor": name, "fold_count": count,
                                "folds": len(part), "frequency": count / len(part)})
        for left, right in combinations(part.to_dict("records"), 2):
            a, b = set(left["selected_features"]), set(right["selected_features"])
            stability.append({"group": group, "left_fold": left["fold"], "right_fold": right["fold"],
                "intersection": len(a & b), "union": len(a | b),
                "jaccard": len(a & b) / len(a | b) if a | b else np.nan})
    return pd.DataFrame(frequencies), pd.DataFrame(stability)


if __name__ == "__main__":
    import argparse
    import json
    from pathlib import Path

    parser = argparse.ArgumentParser(description="Recompute P2-08 statistics without predictions or models")
    parser.add_argument("--summary", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    daily = pd.read_csv(args.summary / "daily.csv")
    for name, table in zip(("metrics", "contrasts", "volatility"), summarize_ablation(daily)):
        table.to_csv(args.output / (name + ".csv"), index=False)
    plans = pd.DataFrame(json.loads((args.summary / "plans.json").read_text(encoding="utf-8")))
    manifest = json.loads((args.summary / "manifest.json").read_text(encoding="utf-8"))
    names = [r["factor_id"] for r in manifest["candidate_refs"]]
    for name, table in zip(("frequency", "jaccard"), selection_stability(plans, names)):
        table.to_csv(args.output / (name + ".csv"), index=False)
