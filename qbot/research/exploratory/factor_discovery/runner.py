"""Run a bounded software loop, writing only expressions and aggregate results."""

from dataclasses import dataclass
import json
from pathlib import Path
import time

import numpy as np
import pandas as pd

from qbot.research.factors.registry import FACTOR_REGISTRY, compute_factors
from qbot.research.labels.forward_return import build_forward_excess_return_labels

from .contracts import DiscoveryConfig, DiscoveryError, ErrorCode
from .engine import ExpressionEngine
from .evaluation import evaluate_discovery
from .generator import TEMPLATE_MANIFEST, generate_candidates
from .private_inputs import verify_private_frames
from .publication import _register_completed_run
from .receipts import code_evidence, digest, verify_receipt, write_receipt
from .redundancy import diagnose_redundancy, original_structure_hashes
from .report import write_report
from .synthetic import frame_sha256, make_synthetic_data


@dataclass
class DiscoveryRun:
    destination: Path
    receipt: dict
    candidates: pd.DataFrame
    factors: pd.DataFrame
    evaluation: object
    redundancy: pd.DataFrame


def _destination(root, version):
    base = Path(root).resolve() / version[:16]
    base.mkdir(parents=True, exist_ok=True)
    for attempt in range(1, 10001):
        destination = base / ("attempt-%04d" % attempt)
        try:
            destination.mkdir()
            return destination
        except FileExistsError:
            continue
    raise DiscoveryError(ErrorCode.BUDGET_EXCEEDED, "output attempt budget exhausted")


def run_frames(
    bars,
    benchmark,
    calendar,
    config=DiscoveryConfig(),
    *,
    evidence,
    labels=None,
    universe=None,
    output_root="results/exploratory/factor-discovery",
    real_data_status="NOT_RUN",
    real_data_reason="private real input not requested",
    private_input_paths=None,
):
    """Only prices <= development end enter the engine; labels stay external."""
    started = time.perf_counter()
    if evidence.get("classification") not in (
        "SYNTHETIC_TEST_ONLY",
        "EXPLORATORY / UNVERIFIED / NON_PIT_CONVENIENCE",
    ):
        raise DiscoveryError(
            ErrorCode.INVALID_INPUT,
            "explicit synthetic/private classification required",
        )
    if evidence.get("synthetic_only") is not (
        evidence["classification"] == "SYNTHETIC_TEST_ONLY"
    ) or real_data_status not in ("SUCCESS", "NOT_RUN", "BLOCKED"):
        raise DiscoveryError(ErrorCode.INVALID_INPUT, "source/real status contract")
    if evidence["synthetic_only"] and real_data_status == "SUCCESS":
        raise DiscoveryError(
            ErrorCode.INVALID_INPUT,
            "synthetic inputs cannot assert real evaluation success",
        )
    if not evidence["synthetic_only"]:
        approved, real_data_reason = verify_private_frames(
            bars, benchmark, calendar, labels, universe, evidence, private_input_paths
        )
        # Execute the newly loaded anchored snapshots, so callers cannot mutate
        # their frames after the binding check and before label evaluation.
        bars, benchmark, calendar, labels, evidence, universe = approved
        if real_data_status != "SUCCESS":
            raise DiscoveryError(
                ErrorCode.INVALID_INPUT, "verified private run status required"
            )
    else:
        # Caller metadata is not a channel for identifiers or arbitrary report text.
        evidence = {
            "classification": "SYNTHETIC_TEST_ONLY",
            "synthetic_only": True,
            "generator": (
                "make_synthetic_data-v1"
                if evidence.get("generator") == "make_synthetic_data-v1"
                else "EXPLICIT_SYNTHETIC_FRAMES"
            ),
            "input_sha256": {
                "bars": frame_sha256(bars),
                "benchmark": frame_sha256(benchmark),
                "calendar": frame_sha256(pd.DataFrame({"date": calendar})),
            },
            "calendar": "SYNTHETIC_BUSINESS_DAYS_NOT_EXCHANGE_SESSIONS",
        }
        real_data_reason = "private real input not requested"
    generated = generate_candidates(config)
    code = code_evidence()
    sessions = pd.DatetimeIndex(calendar)
    end = pd.Timestamp(config.development_end)
    allowed_sessions = sessions[sessions <= end]
    local_bars = bars.loc[pd.to_datetime(bars["date"]) <= end].copy()
    engine = ExpressionEngine(local_bars, allowed_sessions, config, universe=universe)
    factors = engine.panel[["date", "symbol"]].copy()
    original_hashes = original_structure_hashes(config)
    rows = []
    compute_elapsed = 0.0
    cache_hits = peak_cache_bytes = 0
    for record in generated.records:
        compiled = record["compiled"]
        row = {
            key: record[key]
            for key in (
                "candidate_id",
                "generation_sequence",
                "family",
                "meaning",
                "original_expression",
                "status",
                "rejection_reason",
                "duplicate_of",
            )
        }
        row.update(
            normalized_ast=compiled.canonical_json if compiled else "",
            expression_sha256=compiled.expression_sha256 if compiled else "",
            dimension=(
                json.dumps(compiled.dimension.as_dict(), sort_keys=True)
                if compiled
                else ""
            ),
            complexity=(
                json.dumps(compiled.complexity, sort_keys=True) if compiled else ""
            ),
            lookback=compiled.lookback if compiled else None,
            compute_seconds=0.0,
            valid_ratio=None,
            valid_count=0,
            estimated_cell_operations=0,
            reason_counts="{}",
            original_structure_matches="",
        )
        if compiled:
            row["original_structure_matches"] = ",".join(
                name
                for name, hash_ in original_hashes.items()
                if hash_ == compiled.expression_sha256
            )
        if record["status"] == "GENERATED":
            if time.perf_counter() - started >= config.max_total_seconds:
                row.update(
                    status="BUDGET_EXCEEDED",
                    rejection_reason="total run time budget",
                    reason_counts='{"BUDGET_EXCEEDED":1}',
                )
            else:
                execution = engine.compute(compiled)
                row.update(
                    status=execution.status,
                    rejection_reason=(
                        "" if execution.status == "SUCCESS" else execution.status
                    ),
                    compute_seconds=execution.elapsed_seconds,
                    estimated_cell_operations=execution.estimated_cell_operations,
                    reason_counts=json.dumps(execution.reason_counts, sort_keys=True),
                    valid_count=execution.valid_count,
                )
                compute_elapsed += execution.elapsed_seconds
                cache_hits += execution.cache_hits
                peak_cache_bytes = max(peak_cache_bytes, execution.peak_cache_bytes)
                if execution.status == "SUCCESS":
                    values = local_bars[["date", "symbol"]].copy()
                    values[record["candidate_id"]] = execution.values
                    factors = factors.merge(
                        values, on=["date", "symbol"], how="left", validate="one_to_one"
                    )
                    mask = factors["date"].between(
                        pd.Timestamp(config.development_start), end
                    )
                    row["valid_ratio"] = float(
                        factors.loc[mask, record["candidate_id"]].notna().mean()
                    )
        rows.append(row)
    candidates = pd.DataFrame(rows).fillna({"rejection_reason": "", "duplicate_of": ""})
    successful = candidates.loc[
        candidates["status"].eq("SUCCESS"), "candidate_id"
    ].tolist()
    evaluation = None
    redundancy = pd.DataFrame()
    daily_redundancy = pd.DataFrame()
    diagnostic_failure = None
    if successful and time.perf_counter() - started < config.max_total_seconds:
        if labels is None:
            if evidence["synthetic_only"] is not True:
                raise DiscoveryError(
                    ErrorCode.INVALID_INPUT,
                    "real data requires frozen historical labels",
                )
            labels = build_forward_excess_return_labels(
                bars, benchmark, holding_days=config.holding_days, calendar=sessions
            )
        try:
            deadline = started + config.max_total_seconds
            evaluation = evaluate_discovery(
                factors, labels, sessions, config, deadline=deadline
            )
            old = compute_factors(engine.panel, tuple(FACTOR_REGISTRY))
            compared = factors.merge(
                old[["date", "symbol"] + list(FACTOR_REGISTRY)],
                on=["date", "symbol"],
                validate="one_to_one",
            )
            compared = compared.replace([np.inf, -np.inf], np.nan)
            redundancy, daily_redundancy = diagnose_redundancy(
                compared, successful, list(FACTOR_REGISTRY), config, deadline=deadline
            )
        except DiscoveryError as exc:
            if exc.code != ErrorCode.BUDGET_EXCEEDED:
                raise
            diagnostic_failure = str(exc)
    counts = dict(
        generated.stats,
        successful=len(successful),
        failed=int(candidates["status"].eq("BUDGET_EXCEEDED").sum()),
    )
    enough_evaluation = (
        evaluation is not None
        and len(evaluation.summary) >= 20
        and not evaluation.daily_ic.empty
        and not evaluation.quantile_returns.empty
        and evaluation.summary["RankIC Valid Dates"]
        .ge(config.minimum_evaluation_dates)
        .sum()
        >= 20
    )
    elapsed = time.perf_counter() - started
    success = (
        20 <= len(candidates) <= 50
        and len(successful) >= 20
        and counts["family_count"] >= 3
        and enough_evaluation
        and not redundancy.empty
        and elapsed < config.max_total_seconds
    )
    if real_data_status == "SUCCESS" and not success:
        real_data_status = "BLOCKED"
        real_data_reason = diagnostic_failure or "real evaluation acceptance not met"
    expression_hashes = [row["expression_sha256"] for row in rows]
    identity = {
        "input": evidence,
        "config": config.as_dict(),
        "expressions": expression_hashes,
        "source_digest": code["source_digest"],
    }
    version = digest(identity)
    destination = _destination(output_root, version)
    candidates.to_csv(destination / "candidates.csv", index=False)
    for name, table in (
        ("factor_quality", evaluation.quality if evaluation else pd.DataFrame()),
        ("evaluation_summary", evaluation.summary if evaluation else pd.DataFrame()),
        ("daily_ic", evaluation.daily_ic if evaluation else pd.DataFrame()),
        (
            "quantile_returns",
            evaluation.quantile_returns if evaluation else pd.DataFrame(),
        ),
        (
            "label_sensitivity",
            evaluation.label_sensitivity if evaluation else pd.DataFrame(),
        ),
        ("redundancy_summary", redundancy),
        ("redundancy_daily", daily_redundancy),
    ):
        table.to_csv(destination / (name + ".csv"), index=False)
    factor_dev = factors.loc[
        factors["date"].between(pd.Timestamp(config.development_start), end)
    ].sort_values(["date", "symbol"])
    payload = {
        "status": "SOFTWARE_MVP_SUCCESS" if success else "FAILED",
        "completion_reason": (
            "bounded actual compute and descriptive evaluation completed"
            if success
            else diagnostic_failure
            or "candidate/evaluation/resource acceptance not met"
        ),
        "real_data_status": real_data_status,
        "real_data_reason": real_data_reason,
        "input": evidence,
        "config": config.as_dict(),
        "generator_rules": list(TEMPLATE_MANIFEST),
        "counts": counts,
        "run_version": version,
        "normalized_expression_set_sha256": digest(expression_hashes),
        "development_factor_values_sha256": frame_sha256(factor_dev),
        "panel_rows": len(engine.panel),
        "compute_elapsed_seconds": compute_elapsed,
        "total_elapsed_seconds": elapsed,
        "peak_cache_bytes": peak_cache_bytes,
        "cache_hits": cache_hits,
        "code_and_environment": code,
        "evaluation_summary_sha256": (
            frame_sha256(evaluation.summary) if evaluation is not None else None
        ),
        "daily_ic_sha256": (
            frame_sha256(evaluation.daily_ic) if evaluation is not None else None
        ),
        "quantile_returns_sha256": (
            frame_sha256(evaluation.quantile_returns)
            if evaluation is not None
            else None
        ),
        "public_boundary": "EXPRESSIONS_AND_AGGREGATES_ONLY_NO_SECURITY_ROWS_OR_MEMBERSHIP_COMMITMENTS",
    }
    (destination / "config.json").write_text(
        json.dumps(config.as_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    write_report(
        destination / "summary.md", payload, candidates, evaluation, redundancy
    )
    payload["total_elapsed_seconds"] = time.perf_counter() - started
    if payload["total_elapsed_seconds"] >= config.max_total_seconds:
        payload.update(
            status="FAILED",
            completion_reason="total runtime budget exceeded while writing artifacts",
        )
        if payload["real_data_status"] == "SUCCESS":
            payload.update(
                real_data_status="BLOCKED",
                real_data_reason=payload["completion_reason"],
            )
        write_report(
            destination / "summary.md", payload, candidates, evaluation, redundancy
        )
    receipt = write_receipt(destination, payload)
    verify_receipt(destination / "receipt.json")
    if receipt["status"] == "SOFTWARE_MVP_SUCCESS":
        _register_completed_run(receipt)
    factors.attrs.update(
        schema=receipt["schema"],
        purpose="EXPLORATORY",
        qualification="UNVERIFIED",
        formal_admission=False,
        forward_allowed=False,
        live_allowed=False,
    )
    return DiscoveryRun(
        destination, receipt, candidates, factors, evaluation, redundancy
    )


def run_synthetic(
    config=DiscoveryConfig(),
    output_root="results/exploratory/factor-discovery/synthetic",
):
    bars, benchmark, calendar = make_synthetic_data(seed=config.seed)
    evidence = {
        "classification": "SYNTHETIC_TEST_ONLY",
        "synthetic_only": True,
        "generator": "make_synthetic_data-v1",
        "input_sha256": {
            "bars": frame_sha256(bars),
            "benchmark": frame_sha256(benchmark),
            "calendar": frame_sha256(pd.DataFrame({"date": calendar})),
        },
        "calendar": "SYNTHETIC_BUSINESS_DAYS_NOT_EXCHANGE_SESSIONS",
    }
    return run_frames(
        bars, benchmark, calendar, config, evidence=evidence, output_root=output_root
    )
