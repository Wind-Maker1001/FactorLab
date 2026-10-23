"""Independent reference checking with fixed, source-supported precision."""

from dataclasses import asdict
from decimal import Decimal

import numpy as np
import pandas as pd

from factorlab.data.evidence import fail, json_bytes, require_hash, require_text, sha256, table_sha256
from .contracts import AFFINE, RATIO, AdjustmentResult, SourceAdjustmentContract, ValidationReport


def validate_adjustment(result: AdjustmentResult, reference: pd.DataFrame,
                        source_contract: SourceAdjustmentContract) -> ValidationReport:
    contract = source_contract
    if not isinstance(contract.synthetic_only, bool):
        fail("reference synthetic boundary must be explicit")
    require_text(contract.source_id, "reference source_id")
    require_text(contract.source_version, "reference source_version")
    require_hash(contract.anchor_sha256)
    require_hash(contract.reference_sha256)
    if contract.method not in {AFFINE, RATIO} or result.method != contract.method:
        fail("reference adjustment method differs")
    if contract.source_id == result.evidence["source_id"]:
        fail("reference must name an independent upstream source")
    if contract.anchor_sha256 != result.evidence["anchor"]["source_sha256"]:
        fail("reference anchor differs")
    if table_sha256(reference) != contract.reference_sha256:
        fail("reference table hash mismatch")
    columns = ["date", "symbol", "adj_open", "adj_high", "adj_low", "adj_close"]
    if list(reference.columns) != columns or list(result.bars.columns) != columns:
        fail("explicit adjusted OHLC reference columns required")
    if (reference.duplicated(["date", "symbol"]).any()
            or not reference[columns[:2]].equals(result.bars[columns[:2]])):
        fail("reference keys/order must match every adjusted row")
    if contract.synthetic_only != result.synthetic_only:
        fail("synthetic reference boundary differs")
    if (not isinstance(contract.price_quantum, Decimal) or not contract.price_quantum.is_finite()
            or contract.price_quantum < 0):
        fail("invalid reference price quantum")
    if contract.rounding == "EXACT":
        if contract.price_quantum:
            fail("exact reference cannot have a rounding quantum")
        rounding_error = 0.0
    elif contract.rounding == "NEAREST_INDEPENDENTLY_CERTIFIED" and contract.price_quantum > 0:
        rounding_error = float(contract.price_quantum / 2)
    else:
        fail("reference rounding rule is unverified")
    left = result.bars[columns[2:]].to_numpy(dtype=float)
    right = reference[columns[2:]].to_numpy(dtype=float)
    if np.isinf(left).any() or np.isinf(right).any() or not np.array_equal(np.isnan(left), np.isnan(right)):
        fail("reference missing/finite positions differ")
    mask = np.isfinite(right)
    errors = np.abs(left[mask] - right[mask])
    allowed = rounding_error + 1e-12 + 1e-12 * np.abs(right[mask])
    issues = list(result.issues)
    if (errors > allowed).any():
        issues.append("INDEPENDENT_REFERENCE_MISMATCH")
    return ValidationReport("PASS" if not issues else "INCOMPLETE", result.method,
                            table_sha256(result.bars), contract.reference_sha256,
                            sha256(json_bytes(asdict(contract))), len(reference),
                            float(errors.max()) if len(errors) else 0.0,
                            tuple(sorted(set(issues))), result.synthetic_only)
