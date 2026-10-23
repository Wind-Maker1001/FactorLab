"""Isolated descriptive research over explicitly unverified vendor HFQ bars.

No formal dataset builder, financial admission, model training, Forward
activation, or trading interface is called by this module.
"""

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import platform
import subprocess
from typing import Dict, List

import numpy as np
import pandas as pd

from qbot.research.errors import SchemaError
from qbot.research.factors.preprocess import preprocess_cross_sections
from qbot.research.factors.registry import FACTOR_REGISTRY, compute_factors
from qbot.research.labels.forward_return import build_forward_excess_return_labels
from qbot.research.market.regime import build_market_regime_features
from qbot.research.metrics.factor import FactorEvaluation, evaluate_factors
from qbot.research.reproducibility import package_versions

from .contract import (
    ExploratoryContractError,
    REQUIRED_MARKERS,
    assert_exploratory_only,
    load_dataset,
    sha256_file,
)


FACTOR_NAMES = tuple(FACTOR_REGISTRY)
MINIMUM_CROSS_SECTION = 20
TARGET = "EXCESS_RET5"
SCOPES = ("ALL", "BULL", "BEAR", "SIDEWAYS")
INPUT_FIELDS = (
    tuple("raw_" + field for field in ("open", "high", "low", "close"))
    + tuple("adj_" + field for field in ("open", "high", "low", "close"))
    + ("volume", "amount", "turnover")
)


@dataclass
class ExploratoryAnalysis:
    frame: pd.DataFrame
    evaluation: FactorEvaluation
    quality: Dict
    daily_coverage: pd.DataFrame
    factor_quality: pd.DataFrame
    regime_counts: pd.DataFrame
    security_coverage: pd.DataFrame


@dataclass
class RunResult:
    status: str
    output_dir: Path
    reasons: List[str]


def build_blocked_quality(bars, benchmark, calendar, manifest):
    """Preserve real partial acquisition facts without producing factor claims."""
    if manifest.get("synthetic_only") is not False:
        raise ExploratoryContractError(
            "synthetic fixtures cannot supply a real-data prescreen"
        )
    dates = pd.DatetimeIndex(calendar)
    start, end = pd.Timestamp(manifest["research_start"]), pd.Timestamp(
        manifest["research_end"]
    )
    signal_dates = dates[(dates >= start) & (dates <= end)]
    frozen = list(manifest["sample"]["symbols"])
    acquired = sorted(bars["symbol"].unique())
    pre = bars.loc[bars["date"] < start]
    warmup = (
        pre.loc[pre[list(INPUT_FIELDS)].notna().all(axis=1)]
        .groupby("symbol")["date"]
        .nunique()
        .reindex(frozen, fill_value=0)
    )
    history = (
        benchmark.sort_values("date")
        .set_index("date")
        .reindex(dates)
        .rename_axis("date")
        .reset_index()
    )
    history["symbol"] = "000905.SH"
    market = build_market_regime_features(history, price_column="adj_close")
    counts = []
    for scope in SCOPES:
        selected = (
            signal_dates
            if scope == "ALL"
            else market.loc[
                market["date"].isin(signal_dates) & market["MARKET_REGIME"].eq(scope),
                "date",
            ]
        )
        counts.append(
            {
                "scope": scope,
                "observed_real_signal_dates": len(selected),
                "valid_evaluated_dates": 0,
                "status": "REAL_BENCHMARK_ONLY_NO_FACTOR_EVALUATION",
            }
        )
    panel = (
        bars.set_index(["date", "symbol"])
        .reindex(
            pd.MultiIndex.from_product(
                [signal_dates, acquired], names=["date", "symbol"]
            )
        )
        .reset_index()
    )
    daily = (
        panel.groupby("date", sort=True)
        .agg(
            acquired_securities=("symbol", "size"),
            available_adj_close=("adj_close", "count"),
            available_raw_close=("raw_close", "count"),
            available_volume=("volume", "count"),
            available_amount=("amount", "count"),
            available_turnover=("turnover", "count"),
        )
        .reset_index()
    )
    daily = daily.merge(
        market[["date", "MARKET_REGIME"]], on="date", how="left", validate="one_to_one"
    )
    rows = []
    for symbol in frozen:
        observed = bars.loc[bars["symbol"] == symbol]
        signal = observed.loc[observed["date"].isin(signal_dates)]
        rows.append(
            {
                "symbol": symbol,
                "warmup_complete_sessions": int(warmup[symbol]),
                "eligible_by_pre_window_only": int(warmup[symbol])
                >= manifest["warmup_min_sessions"],
                "observed_research_sessions": signal["date"].nunique(),
                "expected_research_sessions": len(signal_dates),
                "research_row_missing_rate": 1
                - signal["date"].nunique() / float(len(signal_dates)),
                "first_observation": (
                    str(observed["date"].min().date()) if len(observed) else None
                ),
                "last_observation": (
                    str(observed["date"].max().date()) if len(observed) else None
                ),
            }
        )
    quality = {
        "markers": list(REQUIRED_MARKERS),
        "qualification": "UNVERIFIED",
        "historical_status": "HISTORICALLY_RECONSTRUCTED",
        "status": "BLOCKED",
        "prescreen_only": True,
        "empirical_results_computed": False,
        "frozen_security_count": len(frozen),
        "acquired_security_count": len(acquired),
        "warmup_eligible_security_count": int(
            (warmup >= manifest["warmup_min_sessions"]).sum()
        ),
        "analyzed_security_count": 0,
        "minimum_cross_section": MINIMUM_CROSS_SECTION,
        "requested_start": str(start.date()),
        "requested_end": str(end.date()),
        "actual_signal_start": str(signal_dates.min().date()),
        "actual_signal_end": str(signal_dates.max().date()),
        "signal_sessions": len(signal_dates),
        "calendar_warmup_sessions": int((dates < start).sum()),
        "full_calendar_start": str(dates.min().date()),
        "full_calendar_end": str(dates.max().date()),
        "minimum_acquired_complete_warmup": (
            int(warmup.loc[acquired].min()) if acquired else 0
        ),
        "expected_acquired_signal_rows": len(panel),
        "field_missing_rates": {
            field: float(panel[field].isna().mean()) for field in INPUT_FIELDS
        },
        "benchmark_research_missing_rates": {
            field: float(
                history.loc[history["date"].isin(signal_dates), field].isna().mean()
            )
            for field in ("raw_open", "raw_close", "adj_open", "adj_close")
        },
        "duplicate_stock_keys": int(bars.duplicated(["date", "symbol"]).sum()),
        "zero_volume_rows": int(panel["volume"].eq(0).sum()),
        "turnover_above_100_percent_rows": int(panel["turnover"].gt(1).sum()),
        "no_price_imputation": True,
        "synthetic_replacement_used": False,
        "sample_classification": "NON_PIT_CONVENIENCE",
        "pit": False,
        "formal_admission": False,
        "forward_allowed": False,
        "live_allowed": False,
    }
    return {
        "quality": quality,
        "daily_coverage": daily,
        "security_coverage": pd.DataFrame(rows),
        "regime_counts": pd.DataFrame(counts),
    }


def evaluate_exploratory_factors(frame, calendar):
    """Reuse all frozen statistics; observe the exploration-only label contract.

    Core metrics deliberately form quantiles without future-label filtering.
    Here the user requested descriptive buckets formed only among observable
    labels. This isolated adapter changes the evaluation input, not the frozen
    financial mathematics or the factor/temporal diagnostics input.
    """
    evaluation = evaluate_factors(
        frame,
        FACTOR_NAMES,
        target=TARGET,
        quantiles=5,
        minimum_cross_section=MINIMUM_CROSS_SECTION,
        group_column="MARKET_REGIME",
        calendar=calendar,
    )
    mature = frame.loc[
        np.isfinite(pd.to_numeric(frame[TARGET], errors="coerce"))
    ].copy()
    if not mature.empty:
        observed = evaluate_factors(
            mature,
            FACTOR_NAMES,
            target=TARGET,
            quantiles=5,
            minimum_cross_section=MINIMUM_CROSS_SECTION,
            group_column="MARKET_REGIME",
            calendar=calendar,
        )
        evaluation.quantile_returns = observed.quantile_returns
    else:
        evaluation.quantile_returns = evaluation.quantile_returns.iloc[:0].copy()
    # Every scope/factor is explicit, including an entirely absent regime.
    key = pd.MultiIndex.from_product([SCOPES, FACTOR_NAMES], names=["scope", "factor"])
    evaluation.summary = (
        evaluation.summary.set_index(["scope", "factor"]).reindex(key).reset_index()
    )
    for column in ("Valid Dates", "RankIC Valid Dates"):
        evaluation.summary[column] = evaluation.summary[column].fillna(0).astype(int)
    return evaluation


def _scope_counts(frame, evaluation, dates):
    regimes = frame[["date", "MARKET_REGIME"]].drop_duplicates("date")
    rows = []
    for scope in SCOPES:
        selected = (
            dates
            if scope == "ALL"
            else pd.DatetimeIndex(
                regimes.loc[regimes["MARKET_REGIME"] == scope, "date"]
            )
        )
        daily = evaluation.daily_ic.loc[evaluation.daily_ic["scope"] == scope]
        valid = daily.loc[daily["RankIC"].notna()]
        minimum_valid = int(
            evaluation.summary.loc[
                evaluation.summary["scope"] == scope, "RankIC Valid Dates"
            ].min()
        )
        rows.append(
            {
                "scope": scope,
                "signal_dates": len(selected),
                "dates_with_any_valid_rank_ic": valid["date"].nunique(),
                "factor_date_observations": int(valid["observations"].sum()),
                "minimum_factor_valid_dates": minimum_valid,
                "sample_insufficient": len(selected) < 60 or minimum_valid < 60,
            }
        )
    return pd.DataFrame(rows)


def _factor_quality(frame):
    rows = []
    for factor in FACTOR_NAMES:
        values = pd.to_numeric(frame["raw_" + factor], errors="coerce")
        finite = values.replace([np.inf, -np.inf], np.nan)
        medians = finite.groupby(frame["date"]).transform("median")
        deviations = (finite - medians).abs()
        mad = deviations.groupby(frame["date"]).transform("median")
        unusual = ((mad > 0) & (deviations > 5 * 1.4826 * mad)).sum()
        rows.append(
            {
                "factor": factor,
                "expected_observations": len(values),
                "finite_raw_observations": int(finite.notna().sum()),
                "raw_completeness": float(finite.notna().mean()),
                "processed_completeness": float(frame[factor].notna().mean()),
                "infinite_raw_observations": int(
                    np.isinf(values.to_numpy(dtype=float)).sum()
                ),
                "missing_raw_observations": int(values.isna().sum()),
                "zero_raw_observations": int(finite.eq(0).sum()),
                "mad_outliers_before_preprocessing": int(unusual),
                "raw_min": finite.min(),
                "raw_p01": finite.quantile(0.01),
                "raw_median": finite.median(),
                "raw_p99": finite.quantile(0.99),
                "raw_max": finite.max(),
            }
        )
    return pd.DataFrame(rows)


def analyze_frames(bars, benchmark, calendar, manifest):
    """Analyze already validated frames; every time-series uses actual sessions.

    The real report entrypoint calls strict load_dataset before this function.
    Synthetic frames are supported only in explicitly labelled offline tests.
    """
    dates = pd.DatetimeIndex(calendar)
    start, end = pd.Timestamp(manifest["research_start"]), pd.Timestamp(
        manifest["research_end"]
    )
    research_dates = dates[(dates >= start) & (dates <= end)]
    frozen_symbols = list(manifest["sample"]["symbols"])
    stock = bars.copy()
    stock["date"] = pd.to_datetime(stock["date"])
    before = stock.loc[stock["date"] < start]
    complete = before.loc[before[list(INPUT_FIELDS)].notna().all(axis=1)]
    warmup_counts = (
        complete.groupby("symbol")["date"]
        .nunique()
        .reindex(frozen_symbols, fill_value=0)
    )
    eligible = warmup_counts.index[
        warmup_counts >= manifest["warmup_min_sessions"]
    ].tolist()
    if len(eligible) < MINIMUM_CROSS_SECTION:
        raise ExploratoryContractError(
            "only %d frozen securities have %d actual complete pre-window sessions; minimum is %d"
            % (len(eligible), manifest["warmup_min_sessions"], MINIMUM_CROSS_SECTION)
        )
    benchmark = benchmark.sort_values("date", kind="mergesort").copy()
    benchmark = (
        benchmark.set_index("date").reindex(dates).rename_axis("date").reset_index()
    )
    benchmark["symbol"] = "000905.SH"  # fixed identity, never a filled price
    if (
        benchmark.loc[benchmark["date"] < start, ["adj_open", "adj_close"]]
        .dropna()
        .shape[0]
        < manifest["warmup_min_sessions"]
    ):
        raise ExploratoryContractError(
            "CSI500 benchmark has insufficient actual pre-window prices"
        )
    panel_index = pd.MultiIndex.from_product(
        [dates, eligible], names=["date", "symbol"]
    )
    panel = stock.set_index(["date", "symbol"]).reindex(panel_index).reset_index()
    factored = compute_factors(panel, FACTOR_NAMES)
    processed = preprocess_cross_sections(
        factored,
        FACTOR_NAMES,
        method="mad",
        mad_multiplier=5.0,
        minimum_count=MINIMUM_CROSS_SECTION,
    )
    labels = build_forward_excess_return_labels(
        panel, benchmark, holding_days=5, calendar=dates
    )
    regimes = build_market_regime_features(benchmark, price_column="adj_close")
    frame = processed.merge(
        labels, on=["date", "symbol"], how="left", validate="one_to_one"
    )
    frame = frame.merge(regimes, on="date", how="left", validate="many_to_one")
    frame = frame.loc[frame["date"].isin(research_dates)].reset_index(drop=True)
    evaluation = evaluate_exploratory_factors(frame, research_dates)
    all_summary = evaluation.summary.loc[evaluation.summary["scope"] == "ALL"]
    unusable = all_summary.loc[
        all_summary["RankIC Valid Dates"] == 0, "factor"
    ].tolist()
    if unusable:
        raise ExploratoryContractError(
            "no valid >=20-security RankIC days for: %s" % ", ".join(unusable)
        )
    daily = (
        frame.groupby("date", sort=True)
        .agg(
            frozen_eligible_securities=("symbol", "size"),
            available_adj_close=("adj_close", "count"),
            available_turnover=("turnover", "count"),
            available_labels=(TARGET, "count"),
            market_regime=("MARKET_REGIME", "first"),
        )
        .reset_index()
    )
    for factor in FACTOR_NAMES:
        daily[factor + "_factor_available"] = (
            frame.groupby("date")[factor].count().reindex(research_dates).to_numpy()
        )
        daily[factor + "_labeled_available"] = (
            frame.loc[frame[TARGET].notna()]
            .groupby("date")[factor]
            .count()
            .reindex(research_dates, fill_value=0)
            .to_numpy()
        )
    coverage_rows = []
    for symbol in frozen_symbols:
        observations = stock.loc[stock["symbol"] == symbol]
        research = observations.loc[observations["date"].isin(research_dates)]
        coverage_rows.append(
            {
                "symbol": symbol,
                "warmup_complete_sessions": int(warmup_counts.loc[symbol]),
                "eligible_by_pre_window_only": symbol in eligible,
                "exclusion_reason": (
                    ""
                    if symbol in eligible
                    else "insufficient_pre_window_complete_history"
                ),
                "observed_research_sessions": research["date"].nunique(),
                "expected_research_sessions": len(research_dates),
                "research_row_missing_rate": 1
                - research["date"].nunique() / float(len(research_dates)),
                "first_observation": (
                    str(observations["date"].min().date())
                    if len(observations)
                    else None
                ),
                "last_observation": (
                    str(observations["date"].max().date())
                    if len(observations)
                    else None
                ),
            }
        )
    missing_rates = {field: float(frame[field].isna().mean()) for field in INPUT_FIELDS}
    active = frame["volume"].gt(0) & frame["amount"].notna()
    vwap = frame.loc[active, "amount"] / frame.loc[active, "volume"]
    return_pct = panel.groupby("symbol")["adj_close"].pct_change(fill_method=None)
    quality = {
        "status": "SUCCESS",
        "markers": list(REQUIRED_MARKERS),
        "qualification": "UNVERIFIED",
        "historical_status": "HISTORICALLY_RECONSTRUCTED",
        "frozen_security_count": len(frozen_symbols),
        "acquired_security_count": int(stock["symbol"].nunique()),
        "analyzed_security_count": len(eligible),
        "excluded_by_pre_window_count": len(frozen_symbols) - len(eligible),
        "requested_start": str(start.date()),
        "requested_end": str(end.date()),
        "actual_signal_start": str(research_dates.min().date()),
        "actual_signal_end": str(research_dates.max().date()),
        "signal_sessions": len(research_dates),
        "calendar_warmup_sessions": int((dates < start).sum()),
        "minimum_security_complete_warmup": int(warmup_counts.loc[eligible].min()),
        "future_calendar_sessions": int((dates > end).sum()),
        "full_calendar_start": str(dates.min().date()),
        "full_calendar_end": str(dates.max().date()),
        "expected_signal_rows": len(frame),
        "duplicate_stock_keys": int(stock.duplicated(["date", "symbol"]).sum()),
        "field_missing_rates": missing_rates,
        "label_missing_rows": int(frame[TARGET].isna().sum()),
        "benchmark_research_missing_rates": {
            field: float(
                benchmark.loc[benchmark["date"].isin(research_dates), field]
                .isna()
                .mean()
            )
            for field in ("raw_open", "raw_close", "adj_open", "adj_close")
        },
        "market_regime_incomplete_trailing_feature_rows": int(
            regimes.loc[regimes["date"].isin(research_dates), "INDEX_RET60"]
            .isna()
            .sum()
        ),
        "label_missing_rate": float(frame[TARGET].isna().mean()),
        "no_price_imputation": True,
        "zero_volume_rows": int(frame["volume"].eq(0).sum()),
        "turnover_above_100_percent_rows": int(frame["turnover"].gt(1).sum()),
        "adj_close_absolute_daily_return_above_30_percent_rows": int(
            return_pct.abs().gt(0.30).sum()
        ),
        "raw_vwap_min": float(vwap.min()) if len(vwap) else None,
        "raw_vwap_max": float(vwap.max()) if len(vwap) else None,
        "minimum_cross_section": MINIMUM_CROSS_SECTION,
        "sample_classification": manifest["sample"]["classification"],
        "synthetic_only": manifest.get("synthetic_only") is True,
        "pit": False,
        "formal_admission": False,
        "for_forward_or_live": False,
        "quantile_boundary_policy": "OBSERVED_LABELS_ONLY_DESCRIPTIVE",
    }
    return ExploratoryAnalysis(
        frame,
        evaluation,
        quality,
        daily,
        _factor_quality(frame),
        _scope_counts(frame, evaluation, research_dates),
        pd.DataFrame(coverage_rows),
    )


def _code_evidence():
    root = Path(__file__).resolve().parents[3]
    paths = sorted(Path(__file__).parent.glob("*.py"))
    research = Path(__file__).resolve().parents[1]
    for directory in ("factors", "labels", "market", "metrics"):
        paths.extend(sorted((research / directory).glob("*.py")))
    paths.extend(
        research / relative
        for relative in (
            "data/schema.py",
            "data/calendar.py",
            "data/units.py",
            "reports/generator.py",
            "reproducibility.py",
        )
    )
    paths.append(root / "dev/requirements-research.txt")
    hashes = {
        str(path.relative_to(root)).replace("\\", "/"): sha256_file(path)
        for path in paths
    }
    try:
        revision = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=str(root), text=True
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        revision = "unavailable"
    return {
        "git_revision_at_run": revision,
        "source_sha256": hashes,
        "source_hash_policy": "EXACT_SOURCE_BYTES_INCLUDING_LINE_ENDINGS",
        "python": platform.python_version(),
        "platform": platform.platform(),
        "pandas": pd.__version__,
        "numpy": np.__version__,
        "package_versions": package_versions(
            ("numpy", "pandas", "scipy", "pyarrow", "matplotlib", "akshare", "baostock")
        ),
    }


def _safe_output_root(output_root):
    root = Path(output_root).expanduser().resolve()
    parts = {part.lower() for part in root.parts}
    if "exploratory" not in parts or parts.intersection(
        {"forward", "models", "factors", "report"}
    ):
        raise ExploratoryContractError(
            "output must use an independent exploratory directory, outside formal/Forward results"
        )
    root.mkdir(parents=True, exist_ok=True)
    return root


def _write_receipt(destination, status, manifest, code, reasons, validated_input=False):
    artifacts = {}
    for path in sorted(destination.rglob("*")):
        if path.is_file() and path.name != "receipt.json":
            artifacts[path.relative_to(destination).as_posix()] = sha256_file(path)
    receipt = {
        "schema": "EXPLORATORY_RUN_V1",
        "status": status,
        "purpose": "EXPLORATORY",
        "qualification": "UNVERIFIED",
        "historical_status": "HISTORICALLY_RECONSTRUCTED",
        "formal_admission": False,
        "forward_allowed": False,
        "live_allowed": False,
        "markers": list(REQUIRED_MARKERS),
        "synthetic_only": manifest.get("synthetic_only") is True,
        "input_metadata_verified": validated_input,
        "data_version": manifest.get("data_version") if validated_input else None,
        "source_version": manifest.get("source_version") if validated_input else None,
        "data_files": manifest.get("files", {}) if validated_input else {},
        "code_and_environment": code,
        "contract": {
            "factors": list(FACTOR_NAMES),
            "min_cross_section": MINIMUM_CROSS_SECTION,
            "signal": "after close t",
            "entry": "adj_open t+1",
            "exit": "adj_open t+6",
            "target": TARGET,
            "quantile_boundary_policy": "OBSERVED_LABELS_ONLY_DESCRIPTIVE",
        },
        "reasons": reasons,
        "artifacts": artifacts,
    }
    digest = hashlib.sha256(
        json.dumps(receipt, ensure_ascii=False, sort_keys=True, allow_nan=False).encode(
            "utf-8"
        )
    ).hexdigest()
    receipt["receipt_sha256"] = digest
    (destination / "receipt.json").write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def run_research(manifest_path, output_root=Path("results/exploratory")):
    """Produce SUCCESS real-data artifacts or an explicit BLOCKED report.

    There is intentionally no option to admit synthetic evidence or override
    the frozen minimum cross section in this real empirical entrypoint.
    """
    from .report import write_blocked_report, write_exploratory_report

    root = _safe_output_root(output_root)
    code = _code_evidence()
    manifest = {}
    validated_input = False
    try:
        bars, benchmark, calendar, manifest = load_dataset(
            manifest_path, allow_synthetic=False
        )
        validated_input = True
        analysis = analyze_frames(bars, benchmark, calendar, manifest)
    except (
        ExploratoryContractError,
        SchemaError,
        FileNotFoundError,
        json.JSONDecodeError,
    ) as exc:
        reasons = [str(exc)]
        manifest_path = Path(manifest_path)
        if not manifest and manifest_path.is_file():
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                if not isinstance(manifest, dict):
                    manifest = {}
            except (OSError, UnicodeError, json.JSONDecodeError):
                pass
        key = hashlib.sha256(
            json.dumps(
                {
                    "manifest": str(manifest_path),
                    "reason": reasons,
                    "manifest_sha256": (
                        sha256_file(manifest_path) if manifest_path.is_file() else None
                    ),
                    "source_sha256": code["source_sha256"],
                    "package_versions": code["package_versions"],
                },
                ensure_ascii=False,
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()[:16]
        destination = root / ("blocked-" + key)
        if destination.exists():
            previous = verify_receipt(destination / "receipt.json")
            if previous["status"] != "BLOCKED" or previous["reasons"] != reasons:
                raise ExploratoryContractError(
                    "existing immutable blocked run is inconsistent"
                )
            return RunResult("BLOCKED", destination, reasons)
        destination.mkdir(parents=True, exist_ok=True)
        prescreen = (
            build_blocked_quality(bars, benchmark, calendar, manifest)
            if validated_input
            else None
        )
        write_blocked_report(destination, manifest, reasons, prescreen=prescreen)
        _write_receipt(destination, "BLOCKED", manifest, code, reasons, validated_input)
        return RunResult("BLOCKED", destination, reasons)
    version_payload = {
        "data_version": manifest["data_version"],
        "source_sha256": code["source_sha256"],
        "python": code["python"],
        "package_versions": code["package_versions"],
    }
    run_version = hashlib.sha256(
        json.dumps(version_payload, sort_keys=True).encode("utf-8")
    ).hexdigest()[:16]
    destination = root / manifest["data_version"][:16] / run_version
    if destination.exists():
        previous = verify_receipt(destination / "receipt.json")
        if (
            previous["status"] != "SUCCESS"
            or previous["data_version"] != manifest["data_version"]
        ):
            raise ExploratoryContractError(
                "existing immutable exploratory run is inconsistent"
            )
        return RunResult("SUCCESS", destination, [])
    destination.mkdir(parents=True, exist_ok=True)
    write_exploratory_report(destination, manifest, analysis)
    _write_receipt(destination, "SUCCESS", manifest, code, [], validated_input=True)
    return RunResult("SUCCESS", destination, [])


def verify_receipt(receipt_path):
    """Verify the receipt semantic hash and every recorded derived artifact."""
    receipt_path = Path(receipt_path)
    payload = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert_exploratory_only(payload)
    if payload.get("schema") != "EXPLORATORY_RUN_V1" or payload.get("status") not in {
        "SUCCESS",
        "BLOCKED",
    }:
        raise ExploratoryContractError("unsupported exploratory receipt schema/status")
    if (
        payload.get("forward_allowed") is not False
        or payload.get("live_allowed") is not False
    ):
        raise ExploratoryContractError(
            "exploratory receipt cannot authorize Forward or live inputs"
        )
    digest = payload.pop("receipt_sha256", None)
    if (
        digest
        != hashlib.sha256(
            json.dumps(
                payload, ensure_ascii=False, sort_keys=True, allow_nan=False
            ).encode("utf-8")
        ).hexdigest()
    ):
        raise ExploratoryContractError("run receipt semantic hash mismatch")
    recorded = payload.get("artifacts")
    if not isinstance(recorded, dict) or not recorded:
        raise ExploratoryContractError("complete derived artifact set required")
    actual = {
        path.relative_to(receipt_path.parent).as_posix()
        for path in receipt_path.parent.rglob("*")
        if path.is_file() and path != receipt_path
    }
    if actual != set(recorded):
        raise ExploratoryContractError("derived artifact collection mismatch")
    for relative, expected in recorded.items():
        if Path(relative).is_absolute() or ".." in Path(relative).parts:
            raise ExploratoryContractError(
                "derived artifact path escapes output directory"
            )
        path = (receipt_path.parent / relative).resolve()
        if (
            receipt_path.parent.resolve() not in path.parents
            or not path.is_file()
            or sha256_file(path) != expected
        ):
            raise ExploratoryContractError(
                "derived artifact hash mismatch: %s" % relative
            )
    return payload
