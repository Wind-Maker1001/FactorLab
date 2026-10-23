"""Verify frozen private inputs and v2 lineage without acquisition calls."""

from pathlib import Path

import numpy as np
import pandas as pd

from ..contract import ExploratoryContractError, load_dataset, sha256_file
from ..publication import verify_publication
from ..runner import FACTOR_NAMES, TARGET, analyze_frames, verify_receipt


DATA_VERSION = "2f145d173f5ddc79d303f580bc4485241023ae3971d3b40a2e8fa6e3de2e3f5f"
BASELINE = "64c72ab0aee49bba59dbf7449f2c8235c61fca2c"
ROOT = Path(__file__).resolve().parents[4]


def require_equal(actual, expected, name, exact=False):
    try:
        pd.testing.assert_frame_equal(
            actual.reset_index(drop=True),
            expected.reset_index(drop=True),
            check_dtype=False,
            check_exact=exact,
            atol=1e-12,
            rtol=1e-12,
        )
    except AssertionError as exc:
        raise ExploratoryContractError("frozen replay mismatch: " + name) from exc


def load_frozen(manifest_path, private_run, publication):
    """Source hashes, code bytes, exact security labels, and aggregate replay."""
    manifest_path, private_run, publication = map(
        Path, (manifest_path, private_run, publication)
    )
    bars, benchmark, calendar, manifest = load_dataset(
        manifest_path, allow_synthetic=False
    )
    if manifest["data_version"] != DATA_VERSION:
        raise ExploratoryContractError("frozen data semantic version mismatch")
    private = verify_receipt(private_run / "receipt.json")
    public = verify_publication(publication / "receipt.json")
    if any(
        r["status"] != "SUCCESS" or r["data_version"] != DATA_VERSION
        for r in (private, public)
    ):
        raise ExploratoryContractError("successful frozen real v2 receipts required")
    if public["private_run_receipt_sha256"] != sha256_file(
        private_run / "receipt.json"
    ):
        raise ExploratoryContractError("private receipt does not bind public v2")
    if public["code_and_environment"] != private["code_and_environment"]:
        raise ExploratoryContractError("private/public frozen code version mismatch")
    if private["data_files"] != manifest["files"]:
        raise ExploratoryContractError(
            "frozen receipt/manifest data file binding mismatch"
        )
    for relative, digest in private["code_and_environment"]["source_sha256"].items():
        if not (ROOT / relative).is_file() or sha256_file(ROOT / relative) != digest:
            raise ExploratoryContractError(
                "frozen calculation source hash mismatch: " + relative
            )
    if tuple(private["contract"]["factors"]) != FACTOR_NAMES:
        raise ExploratoryContractError("frozen 14-factor order mismatch")
    if (
        len(manifest["sample"]["symbols"]) != 50
        or manifest["research_start"] != "2025-09-12"
        or manifest["research_end"] != "2026-09-11"
    ):
        raise ExploratoryContractError("frozen sample/window mismatch")
    analysis = analyze_frames(bars, benchmark, calendar, manifest)
    stored = pd.read_parquet(private_run / "derived_factor_labels.parquet")
    require_equal(
        analysis.frame[stored.columns],
        stored,
        "security factors/labels/regimes",
        exact=True,
    )
    for name, actual in (
        ("daily_ic.csv", analysis.evaluation.daily_ic),
        ("daily_coverage.csv", analysis.daily_coverage),
        ("regime_counts.csv", analysis.regime_counts),
        ("security_coverage.csv", analysis.security_coverage),
        ("quantile_returns.csv", analysis.evaluation.quantile_returns),
    ):
        expected = pd.read_csv(private_run / name)
        actual = actual.copy()
        for column in actual:
            if pd.api.types.is_datetime64_any_dtype(actual[column]):
                expected[column] = pd.to_datetime(expected[column])
            elif actual[column].dtype == object:
                expected[column] = expected[column].fillna("")
                actual[column] = actual[column].fillna("")
        require_equal(actual, expected, name)
    dates = pd.DatetimeIndex(analysis.daily_coverage.date)
    if len(dates) != 242 or dates.has_duplicates or not dates.is_monotonic_increasing:
        raise ExploratoryContractError("242 complete frozen signal sessions required")
    ic = analysis.evaluation.daily_ic
    panel = (
        ic.loc[ic.scope.eq("ALL")]
        .pivot(index="date", columns="factor", values="RankIC")
        .reindex(dates)
    )
    panel = panel[list(FACTOR_NAMES)]
    if not panel.count().eq(236).all():
        raise ExploratoryContractError(
            "236 valid frozen RankIC dates per factor required"
        )
    regimes = analysis.daily_coverage.set_index("date").market_regime.reindex(dates)
    date_rows = []
    all_ic = ic.loc[ic.scope.eq("ALL")].set_index(["date", "factor"])
    for date, cross in stored.groupby("date", sort=True):
        for factor in FACTOR_NAMES:
            observed = np.isfinite(cross[factor]) & np.isfinite(cross[TARGET])
            valid = np.isfinite(panel.at[date, factor])
            reason = "VALID"
            if not valid:
                reason = (
                    "UNMATURED_T_PLUS_6_LABEL"
                    if cross.exit_date.isna().all()
                    else (
                        "LESS_THAN_20_FACTOR_LABEL_PAIRS"
                        if observed.sum() < 20
                        else "CONSTANT_FACTOR_OR_LABEL"
                    )
                )
            date_rows.append(
                dict(
                    date=date,
                    factor=factor,
                    market_regime=regimes.at[date],
                    RankIC=panel.at[date, factor],
                    IC=all_ic.at[(date, factor), "IC"],
                    factor_available=int(np.isfinite(cross[factor]).sum()),
                    observable_labels=int(np.isfinite(cross[TARGET]).sum()),
                    pair_count=int(observed.sum()),
                    valid_rank_ic=valid,
                    missing_reason=reason,
                )
            )
    evidence = dict(
        verification_complete=True,
        data_version=DATA_VERSION,
        baseline_commit=BASELINE,
        manifest_sha256=sha256_file(manifest_path),
        private_run_receipt_sha256=sha256_file(private_run / "receipt.json"),
        public_v2_receipt_sha256=sha256_file(publication / "receipt.json"),
        frozen_code_and_environment=private["code_and_environment"],
        source_artifact_count=len(manifest["sources"]),
        source_hashes_verified=True,
        security_level_replay_exact=True,
        aggregate_replay_tolerance="atol=rtol=1e-12",
        input_files={name: item["sha256"] for name, item in manifest["files"].items()},
        frozen_derived_sha256=private["artifacts"],
        signal_sessions=len(dates),
        valid_rank_ic_dates=236,
        securities=50,
        label_missing_rows=int(stored[TARGET].isna().sum()),
        baseline_ci={
            "research": "https://github.com/Wind-Maker1001/quant-tv/actions/runs/38530507226",
            "static": "https://github.com/Wind-Maker1001/quant-tv/actions/runs/38530507234",
        },
    )
    return (
        stored,
        panel,
        regimes,
        pd.DataFrame(date_rows),
        evidence,
        analysis.evaluation.quantile_returns,
    )
