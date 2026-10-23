"""Publish a small independent receipt-bound allowlist of aggregate artifacts."""

from collections import OrderedDict
import hashlib
from pathlib import Path

import pandas as pd

from .contracts import DiscoveryError, ErrorCode
from .receipts import verify_receipt, write_receipt

PUBLIC_FILES = frozenset(
    (
        "candidates.csv",
        "config.json",
        "factor_quality.csv",
        "evaluation_summary.csv",
        "daily_ic.csv",
        "quantile_returns.csv",
        "label_sensitivity.csv",
        "redundancy_summary.csv",
        "summary.md",
    )
)
FORBIDDEN_COLUMNS = frozenset(
    (
        "symbol",
        "entry_date",
        "exit_date",
        "EXCESS_RET5",
        "STOCK_FWD_RET5",
        "BENCHMARK_FWD_RET5",
        "members",
        "membership_sha256",
    )
)

PUBLIC_COLUMNS = {
    "candidates.csv": {
        "candidate_id",
        "generation_sequence",
        "family",
        "meaning",
        "original_expression",
        "status",
        "rejection_reason",
        "duplicate_of",
        "normalized_ast",
        "expression_sha256",
        "dimension",
        "complexity",
        "lookback",
        "compute_seconds",
        "valid_ratio",
        "valid_count",
        "estimated_cell_operations",
        "reason_counts",
        "original_structure_matches",
    },
    "factor_quality.csv": {
        "candidate_id",
        "valid_count",
        "total_count",
        "valid_ratio",
        "missing_ratio",
        "nonfinite_ratio",
        "outlier_ratio",
        "evaluation_status",
        "processed_valid_ratio",
    },
    "evaluation_summary.csv": {
        "scope",
        "factor",
        "IC Mean",
        "IC Std",
        "ICIR",
        "RankIC Mean",
        "RankIC Std",
        "RankICIR",
        "IC > 0 Ratio",
        "RankIC > 0 Ratio",
        "Valid Dates",
        "RankIC Valid Dates",
        "Coverage",
    },
    "daily_ic.csv": {
        "scope",
        "date",
        "factor",
        "observations",
        "coverage",
        "IC",
        "RankIC",
    },
    "quantile_returns.csv": {
        "scope",
        "date",
        "factor",
        "quantile",
        "future_return",
        "observations",
        "labeled_observations",
        "label_coverage",
    },
    "label_sensitivity.csv": {
        "scope",
        "date",
        "factor",
        "quantile",
        "future_return",
        "observations",
        "labeled_observations",
        "label_coverage",
        "diagnostic",
    },
    "redundancy_summary.csv": {
        "left",
        "right",
        "comparison",
        "status",
        "valid_dates",
        "development_dates",
        "mean_pair_coverage",
        "mean_pearson",
        "mean_abs_pearson",
        "paired_observations",
        "exact_values_and_missing_mask",
    },
}

# In-process producer provenance, separate from caller-writable integrity hashes.
# Cold receipts can be verified, but publication requires a freshly completed run.
# Arbitrary Python execution/module monkeypatching is outside the internal AST API.
_COMPLETED_RUNS = OrderedDict()


def _register_completed_run(receipt):
    _COMPLETED_RUNS[receipt["receipt_sha256"]] = dict(receipt["artifacts"])
    _COMPLETED_RUNS.move_to_end(receipt["receipt_sha256"])
    while len(_COMPLETED_RUNS) > 256:
        _COMPLETED_RUNS.popitem(last=False)


def publish_aggregates(receipt_path, destination):
    source = Path(receipt_path)
    receipt = verify_receipt(source)
    if receipt["status"] != "SOFTWARE_MVP_SUCCESS":
        raise DiscoveryError(
            ErrorCode.INVALID_INPUT,
            "completed software run required for aggregate publication",
        )
    for name in PUBLIC_FILES:
        if name not in receipt["artifacts"]:
            raise DiscoveryError(
                ErrorCode.INVALID_INPUT, "complete public artifact allowlist required"
            )
        if name.endswith(".csv"):
            columns = set(pd.read_csv(source.parent / name, nrows=0).columns)
            if columns & FORBIDDEN_COLUMNS or columns != PUBLIC_COLUMNS[name]:
                raise DiscoveryError(
                    ErrorCode.INVALID_INPUT,
                    "security-level columns cannot be published",
                )
    trusted = _COMPLETED_RUNS.get(receipt["receipt_sha256"])
    if trusted is None or trusted != receipt["artifacts"]:
        raise DiscoveryError(
            ErrorCode.INVALID_INPUT,
            "trusted in-process completed runner provenance required for publication",
        )
    # Freeze bytes before copying and check each against the producer record,
    # so a concurrent source change cannot acquire a fresh publication hash.
    contents = {name: (source.parent / name).read_bytes() for name in PUBLIC_FILES}
    if any(
        hashlib.sha256(value).hexdigest() != trusted[name]
        for name, value in contents.items()
    ):
        raise DiscoveryError(ErrorCode.INVALID_INPUT, "producer artifact bytes changed")
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=False)
    for name in sorted(PUBLIC_FILES):
        (destination / name).write_bytes(contents[name])
    payload = {
        key: value
        for key, value in receipt.items()
        if key not in ("artifacts", "receipt_sha256")
    }
    payload["publication_source_receipt_sha256"] = receipt["receipt_sha256"]
    payload["publication_policy"] = (
        "AGGREGATE_ALLOWLIST_V1_NO_RAW_OR_SECURITY_LEVEL_ARTIFACTS"
    )
    write_receipt(destination, payload)
    return verify_receipt(destination / "receipt.json")
