"""Explicit adapters; existing research execution is unaffected."""
import json

from factorlab.dataset.identity import frame_hash
from factorlab.metrics.prediction import evaluate_predictions


def walk_forward_metadata(result, *, top_k, minimum_cross_section):
    """Extract small receipts, not prediction or training tables.

    Caller retains input dataset/label identities and splitter configuration in
    the experiment definition; this receipt binds the actual evaluated samples.
    """
    evaluation = evaluate_predictions(result.predictions, top_k=top_k,
                                     minimum_cross_section=minimum_cross_section)
    overall = evaluation.overall
    if len(overall) != 1:
        raise ValueError("Exactly one overall evaluation required")
    metrics = json.loads(overall.drop(columns=["scope", "period"]).to_json(orient="records"))[0]
    return {
        "prediction_metrics": metrics,
        "validation": {
            "sample_hash": frame_hash(result.predictions.drop(
                columns=["score", "rank", "model_name", "selected_alpha", "selected_parameters"],
                errors="ignore")),
            "evaluation": {"top_k": top_k, "minimum_cross_section": minimum_cross_section},
            "folds": json.loads(result.fold_summary.to_json(orient="records", date_format="iso")),
            "factor_selection": json.loads(result.factor_selection.to_json(orient="records", date_format="iso")),
        },
        "artifacts": {"model_%d" % i: {"path": str(path.resolve())}
                      for i, path in enumerate(result.model_paths)},
    }
