"""Command-line interface for the isolated FactorLab research pipeline."""

import argparse
from contextlib import ExitStack
from dataclasses import asdict, replace
from datetime import date, datetime, timezone
import hashlib
import io
import json
import logging
import math
import os
from pathlib import Path
import re
import sys
from typing import Any, Dict, Iterable, List, Optional
from zoneinfo import ZoneInfo

import pandas as pd

from factorlab.backtest import (
    ExecutionBacktestConfig,
    FastBacktestConfig,
    FeeSchedule,
    PriceLimitSchedule,
    run_execution_backtest,
    run_fast_backtest,
)
from factorlab.config import ResearchConfig, load_config
from factorlab.data import AKShareProvider, ParquetBarCache, TradingCalendar
from factorlab.data.admission import assert_formal_market_admission
from factorlab.data.providers.base import MarketDataProvider
from factorlab.data.download_status import (
    assert_no_unresolved,
    list_unresolved,
    record_fatal,
    resolve_fatal,
)
from factorlab.data.providers.local import (
    LocalParquetProvider,
    publish_local_market_data,
)
from factorlab.data.provenance import (
    bar_data_version,
    build_bar_data_provenance,
    validate_bar_data_provenance,
)
from factorlab.dataset import DatasetBuildOptions, DatasetBuilder
from factorlab.errors import (
    DataQualityError,
    FactorLabError,
    PointInTimeUniverseRequiredError,
    ProviderNetworkError,
    SecurityStatusDataError,
)
from factorlab.forward import (
    activation_id,
    classify_forward_timing,
    fit_final_model,
    finalize_signal_record,
    forward_contract_sha256,
    load_forward_activation,
    load_model_bundle,
    record_signals,
    register_forward_activation,
    save_model_bundle,
    selected_forward_model_parameters,
)
from factorlab.forward.integrity import research_source_sha256, workflow_lock
from factorlab.metrics import (
    RobustnessError,
    RobustnessOptions,
    calculate_portfolio_metrics,
    evaluate_factors,
    evaluate_model_robustness,
    evaluate_predictions,
)
from factorlab.market import MARKET_FEATURE_COLUMNS, MARKET_REGIMES
from factorlab.portfolio import select_top_k
from factorlab.reports import (
    write_factor_report,
    write_model_comparison_report,
    write_model_report,
)
from factorlab.reproducibility import (
    PROVENANCE_FIELDS,
    RunManifest,
    git_commit,
    git_worktree_state,
    set_random_seed,
    validate_provenance,
)
from factorlab.universe.csi500 import (
    BAOSTOCK_PIT_UNVERIFIED_REASON,
    PIT_DAILY_ALIAS,
    PIT_GENERATION_SCHEMA_VERSION,
    PIT_INTERVALS_ALIAS,
    BaoStockPointInTimeProvider,
    collect_point_in_time_universe,
    compress_membership_intervals,
    dataframe_sha256,
    import_point_in_time_universe,
    validate_daily_membership,
)
from factorlab.universe.filters import UniverseFilterConfig
from factorlab.universe.status import (
    BAOSTOCK_SECURITY_SOURCE_VERSION,
    import_security_data,
    load_verified_security_data,
    publish_security_data,
)
from factorlab.validation import ExpandingWalkForwardSplitter, WalkForwardRunner
from factorlab.validation.walk_forward import (
    WalkForwardResult,
    diagnostic_frame_sha256,
)


LOGGER = logging.getLogger("factorlab")

_STAGE_TABLE_ATTRIBUTES = {
    "stage_predictions": "diagnostic_predictions",
    "stage_prediction_daily": "diagnostic_daily",
    "stage_prediction_summary": "diagnostic_summary",
    "stage_prediction_audit": "diagnostic_audit",
}
_STAGE_ARTIFACTS = {
    name + suffix for name in _STAGE_TABLE_ATTRIBUTES for suffix in (".csv", ".parquet")
} | {"stage_ic_comparison.png"}


def _stage_diagnostic_policy() -> Dict[str, Any]:
    return {
        "schema_version": 1,
        "fit_scope": "train_only",
        "train": "TRAIN_IN_SAMPLE:diagnostic_only",
        "validation": "VALIDATION_SELECTION:selection_and_diagnostic",
        "test": "HISTORICAL_TEST:evaluation_only",
        "validation_prediction": "reuse_selected_candidate",
        "test_predictions_per_fold": 1,
        "pooling": "evaluate_each_fold_and_stage_separately",
        "refit_train_plus_validation": False,
    }


def _manifest_arguments(arguments: argparse.Namespace) -> Dict[str, Any]:
    """Convert argparse values to stable JSON scalars for RunManifest."""

    result: Dict[str, Any] = {}
    for name, value in vars(arguments).items():
        if isinstance(value, (date, datetime)):
            result[name] = value.isoformat()
        elif isinstance(value, Path):
            result[name] = str(value)
        else:
            result[name] = value
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="factorlab",
        description="FactorLab research pipeline (NO LIVE MONEY)",
    )
    parser.add_argument("--log-level", default="INFO", choices=("DEBUG", "INFO", "WARNING", "ERROR"))
    subparsers = parser.add_subparsers(dest="command", required=True)
    demo = subparsers.add_parser("demo", help="Offline synthetic four-model software demonstration")
    demo.add_argument("--output", required=True, type=Path)
    demo.add_argument("--seed", default=42, type=int)
    for name in ("audit", "download-data", "build-dataset", "evaluate-factors"):
        command = subparsers.add_parser(name)
        command.add_argument("--config", required=True)
    importer = subparsers.add_parser("import-universe")
    importer.add_argument("--config", required=True)
    importer.add_argument(
        "--input",
        required=True,
        dest="input_path",
        help=(
            "CSV/Parquet with exact date,symbol snapshots or explicit "
            "inclusive symbol,entry_date,exit_date intervals"
        ),
    )
    security_importer = subparsers.add_parser("import-security-data")
    security_importer.add_argument("--config", required=True)
    security_importer.add_argument(
        "--status",
        required=True,
        dest="status_path",
        help="CSV/Parquet with date,symbol,trade_status,is_st",
    )
    security_importer.add_argument(
        "--listings",
        required=True,
        dest="listings_path",
        help="CSV/Parquet with symbol,listing_date",
    )
    market_importer = subparsers.add_parser("import-market-data")
    market_importer.add_argument("--config", required=True)
    market_importer.add_argument("--stocks", required=True, dest="stock_path")
    market_importer.add_argument("--benchmark", required=True, dest="benchmark_path")
    market_importer.add_argument(
        "--source", required=True,
        help="Verifiable original data source; not a claim of authority",
    )
    market_importer.add_argument("--source-version", required=True)
    market_importer.add_argument("--qualification-manifest", default=None,
                                 help="Replayable v2 evidence package; synthetic packages remain SOFTWARE_ONLY")
    walk_forward = subparsers.add_parser("walk-forward")
    walk_forward.add_argument("--config", required=True)
    walk_forward.add_argument(
        "--model",
        required=True,
        choices=("factor_score", "ridge", "lightgbm", "xgboost"),
    )
    comparison = subparsers.add_parser("compare-models")
    comparison.add_argument("--config", required=True)
    activation = subparsers.add_parser("activate-forward")
    activation.add_argument("--config", required=True)
    activation.add_argument("--date", required=True, type=date.fromisoformat)
    freeze_model = subparsers.add_parser("freeze-forward-model")
    freeze_model.add_argument("--config", required=True)
    forward_signal = subparsers.add_parser("forward-signal")
    forward_signal.add_argument("--config", required=True)
    forward_signal.add_argument("--date", required=True, type=date.fromisoformat)
    forward_signal.add_argument("--reissue-reason")
    forward_signal.add_argument("--version")
    forward_evaluation = subparsers.add_parser("evaluate-forward")
    forward_evaluation.add_argument("--config", required=True)
    forward_evaluation.add_argument("--as-of", required=True, type=date.fromisoformat)
    forward_evaluation.add_argument("--start", type=date.fromisoformat)
    forward_evaluation.add_argument("--end", type=date.fromisoformat)
    return parser


def _atomic_parquet(frame: pd.DataFrame, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(".%s.tmp" % destination.name)
    frame.to_parquet(temporary, index=False)
    os.replace(str(temporary), str(destination))


def _atomic_json(payload: Any, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(".%s.tmp" % destination.name)
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(str(temporary), str(destination))


def _atomic_csv(frame: pd.DataFrame, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(".%s.tmp" % destination.name)
    frame.to_csv(temporary, index=False)
    os.replace(str(temporary), str(destination))


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _security_manifest_sha256(manifest: Dict[str, Any]) -> str:
    content = json.dumps(
        manifest,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(content).hexdigest()


def _record_security_data_provenance(
    run: RunManifest, manifest: Dict[str, Any]
) -> None:
    """Bind the verified status/listing generation to a CLI run manifest."""

    run.payload.update(
        {
            "security_data_version": manifest["security_data_version"],
            "security_data_source": manifest["source"],
            "security_data_source_version": manifest["source_version"],
            "security_data_manifest_sha256": _security_manifest_sha256(manifest),
            "security_status_sha256": manifest["status_sha256"],
            "listing_dates_sha256": manifest["listings_sha256"],
            "security_data_input_sha256": manifest["input_sha256"],
            "security_data_input_files": manifest["input_files"],
        }
    )


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdefABCDEF" for character in value)
    )


def _artifact_sha256(directory: Path) -> Dict[str, str]:
    """Hash every regular report artifact using stable POSIX-relative names."""

    root = Path(directory)
    return {
        path.relative_to(root).as_posix(): _file_sha256(path)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _dataset_contract(config: ResearchConfig) -> Dict[str, Any]:
    """Return only configuration fields that determine dataset contents."""

    return _dataset_contract_from_snapshot(config.to_dict())


def _dataset_contract_from_snapshot(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    """Extract the dataset-relevant subset from a serialized full config."""

    if not isinstance(snapshot, dict):
        raise FactorLabError("serialized config is not a mapping")
    try:
        minimum_cross_section = snapshot["evaluation"]["min_cross_section"]
        factor_list = snapshot["factors"]
        data = snapshot["data"]
        universe = snapshot["universe"]
        label = snapshot["label"]
        preprocessing = snapshot["preprocessing"]
    except (KeyError, TypeError) as exc:
        raise FactorLabError(
            "serialized config is missing dataset-contract fields"
        ) from exc
    return {
        "data": data,
        "universe": universe,
        "label": label,
        "preprocessing": preprocessing,
        "market_regime": snapshot.get("market_regime", {"enabled": False}),
        "minimum_cross_section": minimum_cross_section,
        "factor_list": factor_list,
    }


def _provider(config: ResearchConfig) -> MarketDataProvider:
    if config.data.provider == "local_parquet":
        return LocalParquetProvider(
            config.paths.market_input_root,
            benchmark_symbol=config.data.benchmark,
        )
    if config.data.provider != "akshare":
        raise FactorLabError("Unsupported market-data provider")
    return AKShareProvider(
        benchmark_symbol=config.data.benchmark,
        adjustment=config.data.adjustment,
        retry_count=config.network.max_retries,
        backoff_seconds=config.network.backoff_seconds,
        request_interval=config.network.request_interval_seconds,
        timeout_seconds=config.network.timeout_seconds,
    )


def _benchmark_cache(config: ResearchConfig) -> ParquetBarCache:
    return ParquetBarCache(
        config.paths.benchmark_dir,
        lock_timeout_seconds=config.cache.lock_timeout_seconds,
    )


def _stock_cache(config: ResearchConfig) -> ParquetBarCache:
    return ParquetBarCache(
        config.paths.raw_dir / config.data.provider / "daily",
        lock_timeout_seconds=config.cache.lock_timeout_seconds,
    )


def _download_guard_root(config: ResearchConfig) -> Path:
    # Never partition fatal evidence by provider: switching source is not repair.
    return config.paths.raw_dir / "download_status"


def _record_fatal_download(
    config: ResearchConfig, run: RunManifest, provider: MarketDataProvider,
    symbol: str, start: Any, end: Any, is_index: bool, error: Exception,
) -> None:
    try:
        source_version = provider.cache_source_version(symbol, is_index)
    except Exception:
        source_version = None  # Explicitly unverifiable, never a fake identity.
    # Keep diagnostics in memory first.  Ledger publication must not depend on
    # a successful rewrite of the separate run manifest (for example on a full
    # disk); an interrupted first ledger write also remains fail-closed.
    run.payload["failures"]["fatal_market_data"] = {
        "symbol": symbol, "error_type": type(error).__name__,
        "error_message": str(error) or type(error).__name__,
    }
    root = _download_guard_root(config)
    run.payload["outputs"]["download_contract_ledger"] = str(root)
    receipt = record_fatal(
        root, symbol=symbol, provider_class=type(provider).__name__,
        source_version=source_version, start=start, end=end, run_id=run.run_id,
        git_commit=run.payload["git_commit"], error_type=type(error).__name__,
        error_message=str(error) or type(error).__name__,
    )
    run.payload.setdefault("fatal_download_receipts", []).append(receipt)
    run.add_output("download_contract_ledger", root)


def _update_guarded_bars(
    config: ResearchConfig, run: RunManifest, provider: MarketDataProvider,
    cache: ParquetBarCache, symbol: str, start: Any, end: Any,
    is_index: bool = False,
) -> pd.DataFrame:
    """Only a fresh, verified fetch can repair an observed fatal contract."""

    pending = [
        item for item in list_unresolved(_download_guard_root(config))
        if item["context"]["symbol"] == symbol
    ]
    first, last = pd.Timestamp(start), pd.Timestamp(end)
    if pending:
        first = min([first] + [pd.Timestamp(item["context"]["start"]) for item in pending])
        last = max([last] + [pd.Timestamp(item["context"]["end"]) for item in pending])
    try:
        if not pending:
            return cache.update_from_provider(provider, symbol, first, last, is_index=is_index)
        source = type(provider).__name__
        source_version = provider.cache_source_version(symbol, is_index)
        cache.assert_source_identity(symbol, source=source, source_version=source_version)
        fetch = provider.get_index_bars if is_index else provider.get_daily_bars
        incoming = fetch(symbol, first, last)
        if not isinstance(incoming, pd.DataFrame) or incoming.empty:
            raise DataQualityError("Fatal download repair requires a nonempty actual refetch for %s" % symbol)
        if is_index:
            # Validate the actual response, not a merged old cache, before a
            # benchmark repair can clear its receipt.  Old prices must not
            # hide a fresh response missing the required research boundaries.
            _select_benchmark_with_warmup(config, incoming)
        cache.update(symbol, incoming, first, last, source=source, source_version=source_version)
        snapshot = cache.load_snapshot(
            symbol, expected_source=source, expected_source_version=source_version,
        )
        resolved = resolve_fatal(
            _download_guard_root(config), symbol=symbol, provider_class=source,
            source_version=source_version, start=first, end=last,
            run_id=run.run_id, git_commit=run.payload["git_commit"],
            expected_fatal_receipt_ids=[item["receipt_id"] for item in pending],
            cache_metadata_sha256=snapshot.metadata_sha256,
            force_refetched=True, schema_verified=True, refetched_rows=len(incoming),
        )
        run.payload.setdefault("resolved_download_receipts", []).extend(resolved)
        run.add_output("download_contract_ledger", _download_guard_root(config))
        return snapshot.frame
    except ProviderNetworkError:
        raise  # Stock loop may skip this; never clear earlier fatal receipts.
    except Exception as exc:
        _record_fatal_download(config, run, provider, symbol, first, last, is_index, exc)
        raise


def _resolve_input_path(config: ResearchConfig, value: str) -> Path:
    """Resolve every imported file from the repository, not the process cwd."""

    try:
        path = Path(value).expanduser()
        root = config.repo_root or Path.cwd()
        return (path if path.is_absolute() else root / path).resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        raise FactorLabError("Cannot resolve imported input path %r" % value) from exc


def _benchmark_request_start(config: ResearchConfig) -> pd.Timestamp:
    """Return a conservative civil date used to discover exact warm-up sessions."""

    research_start = pd.Timestamp(config.data.start)
    warmup = int(config.data.warmup_trading_days)
    if warmup == 0:
        return research_start
    # Fetch more civil days than requested market sessions, then select the
    # exact final N sessions from the returned benchmark calendar.
    estimated_days = (warmup * 365 + 239) // 240 + 60
    return research_start - pd.Timedelta(days=max(366, estimated_days))


def _select_benchmark_with_warmup(
    config: ResearchConfig, bars: pd.DataFrame
) -> pd.DataFrame:
    """Select exactly N pre-start sessions plus the configured research range.

    Research boundaries are a strict data contract: both configured dates must
    be benchmark trading sessions present in the bars.  This deliberately
    rejects weekend/holiday boundaries instead of silently shortening the
    requested experiment, and also detects a truncated cache at either edge.
    """

    start = pd.Timestamp(config.data.start)
    end = pd.Timestamp(config.data.end)
    available = bars.loc[bars["date"] <= end].sort_values("date", kind="mergesort")
    available_dates = pd.DatetimeIndex(available["date"])
    if start not in available_dates:
        raise FactorLabError(
            "Benchmark bars do not contain configured data.start %s; "
            "research boundaries must be benchmark trading sessions and the "
            "cache must cover them" % start.date().isoformat()
        )
    if end not in available_dates:
        raise FactorLabError(
            "Benchmark bars do not contain configured data.end %s; "
            "research boundaries must be benchmark trading sessions and the "
            "cache must cover them" % end.date().isoformat()
        )
    research = available.loc[available["date"].between(start, end)]
    prior = available.loc[available["date"] < start]
    warmup = int(config.data.warmup_trading_days)
    if len(prior) < warmup:
        raise FactorLabError(
            "Benchmark cache has only %d/%d required warm-up trading sessions"
            % (len(prior), warmup)
        )
    selected_dates = pd.DatetimeIndex(prior["date"].tail(warmup)).append(
        pd.DatetimeIndex(research["date"])
    )
    return available.loc[available["date"].isin(selected_dates)].reset_index(drop=True)


def _download_benchmark_with_warmup(
    config: ResearchConfig, provider: MarketDataProvider
) -> pd.DataFrame:
    cache = _benchmark_cache(config)
    bars = cache.update_from_provider(
        provider,
        config.data.benchmark,
        _benchmark_request_start(config),
        config.data.end,
        is_index=True,
    )
    return _select_benchmark_with_warmup(config, bars)


def _load_benchmark_with_warmup(config: ResearchConfig) -> pd.DataFrame:
    return _select_benchmark_with_warmup(config, _load_benchmark_snapshot(config).frame)


def _load_benchmark_snapshot(config: ResearchConfig):
    """Pin benchmark bars and their evidence to a single cache generation."""

    assert_no_unresolved(_download_guard_root(config))
    provider = _provider(config)
    snapshot = _benchmark_cache(config).load_snapshot(
        config.data.benchmark,
        expected_source=provider.__class__.__name__,
        expected_source_version=provider.cache_source_version(
            config.data.benchmark, True
        ),
    )
    if snapshot.frame.empty:
        raise FactorLabError(
            "Benchmark cache is empty; run download-data before this command"
        )
    return snapshot


def _research_calendar(
    config: ResearchConfig, full_calendar: pd.DatetimeIndex
) -> pd.DatetimeIndex:
    start = pd.Timestamp(config.data.start)
    end = pd.Timestamp(config.data.end)
    result = full_calendar[(full_calendar >= start) & (full_calendar <= end)]
    if result.empty:
        raise FactorLabError("Benchmark research calendar is empty")
    return result


def _load_verified_membership(
    config: ResearchConfig, calendar: pd.DatetimeIndex
) -> pd.DataFrame:
    """Read one manifest snapshot and only its immutable PIT artifacts.

    Older schemas remain strictly verified, read-only inputs.  All current
    publishers create generation bundles; canonical aliases do not determine
    the active generation.
    """

    # A successful date echo, member count and content hash do not establish
    # the effective date of a vendor's weekly index snapshot. Reject even
    # previously published BaoStock generations until independently audited
    # effective-membership evidence has a new, explicit source contract.
    if config.universe.provider.lower() == "baostock":
        raise PointInTimeUniverseRequiredError(
            BAOSTOCK_PIT_UNVERIFIED_REASON.split(": ", 1)[1]
        )
    manifest_path = config.paths.universe_dir / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_bytes().decode("utf-8"))
    except (OSError, UnicodeError, ValueError, AttributeError) as exc:
        raise PointInTimeUniverseRequiredError("universe manifest is unreadable") from exc
    if not isinstance(manifest, dict):
        raise PointInTimeUniverseRequiredError("universe manifest must be a mapping")
    dates = pd.DatetimeIndex(pd.to_datetime(calendar, errors="raise")).normalize()
    source = config.universe.provider.lower()
    if source == "local":
        identity = (
            manifest.get("schema_version"),
            manifest.get("source"),
            manifest.get("source_version"),
        )
        supported_identities = {
            (1, "local-import", "user-pit-daily-import-v1"),
            (2, "local-import", "user-pit-import-v2"),
            (PIT_GENERATION_SCHEMA_VERSION, "local-import", "user-pit-import-v2"),
        }
        if identity not in supported_identities:
            raise PointInTimeUniverseRequiredError(
                "unsupported local universe manifest version; re-import the PIT universe"
            )
        expected_identity = {
            "schema_version": identity[0],
            "source": identity[1],
            "source_version": identity[2],
        }
        if identity[0] in {2, PIT_GENERATION_SCHEMA_VERSION}:
            layout = manifest.get("input_layout")
            expected_semantics = {
                "daily": "exact-trading-date-snapshot",
                "inclusive-intervals": "entry_date <= trading_date <= exit_date",
            }
            if layout not in expected_semantics:
                raise PointInTimeUniverseRequiredError(
                    "local universe manifest input_layout is invalid"
                )
            if manifest.get("membership_semantics") != expected_semantics[layout]:
                raise PointInTimeUniverseRequiredError(
                    "local universe manifest membership_semantics is invalid"
                )
            if manifest.get("daily_sha256") != manifest.get("sha256"):
                raise PointInTimeUniverseRequiredError(
                    "local universe manifest daily_sha256 is invalid"
                )
            intervals_digest = manifest.get("intervals_sha256")
            if not _is_sha256(intervals_digest):
                raise PointInTimeUniverseRequiredError(
                    "local universe manifest intervals_sha256 is invalid"
                )
    else:
        raise PointInTimeUniverseRequiredError(
            "universe.provider must be baostock or local"
        )
    expected = {
        **expected_identity,
        "index_symbol": config.universe.index_symbol,
        "start": dates.min().strftime("%Y-%m-%d"),
        "end": dates.max().strftime("%Y-%m-%d"),
        "trading_dates": int(dates.nunique()),
        "expected_member_count": int(config.universe.expected_member_count),
    }
    mismatches = [
        name for name, expected_value in expected.items()
        if manifest.get(name) != expected_value
    ]
    if mismatches:
        raise PointInTimeUniverseRequiredError(
            "universe manifest mismatch (%s); rebuild the PIT universe"
            % ", ".join(mismatches)
        )
    if source == "local":
        source_file = manifest.get("source_file")
        if not isinstance(source_file, str) or not source_file.strip():
            raise PointInTimeUniverseRequiredError(
                "local universe manifest source_file is missing"
            )
        if not _is_sha256(manifest.get("source_file_sha256")):
            raise PointInTimeUniverseRequiredError(
                "local universe manifest source_file_sha256 is invalid"
            )
    try:
        generated_at = pd.Timestamp(manifest.get("generated_at"))
    except (TypeError, ValueError) as exc:
        raise PointInTimeUniverseRequiredError(
            "universe manifest generated_at is invalid"
        ) from exc
    if pd.isna(generated_at):
        raise PointInTimeUniverseRequiredError(
            "universe manifest generated_at is invalid"
        )
    generation_fields = {
        "generation_id", "daily_file", "intervals_file", "daily_alias",
        "intervals_alias", "daily_file_sha256", "intervals_file_sha256",
    }
    is_generation = manifest.get("schema_version") == PIT_GENERATION_SCHEMA_VERSION
    if is_generation:
        generation_id = manifest.get("generation_id")
        if (
            not isinstance(generation_id, str)
            or re.fullmatch(r"[0-9a-f]{32}", generation_id) is None
            or manifest.get("daily_file") != "csi500_daily.%s.parquet" % generation_id
            or manifest.get("intervals_file") != "csi500_intervals.%s.parquet" % generation_id
            or manifest.get("daily_alias") != PIT_DAILY_ALIAS
            or manifest.get("intervals_alias") != PIT_INTERVALS_ALIAS
        ):
            raise PointInTimeUniverseRequiredError(
                "universe manifest contains unexpected generation artifact names"
            )
        if not all(
            _is_sha256(manifest.get(name))
            for name in ("daily_file_sha256", "intervals_file_sha256")
        ):
            raise PointInTimeUniverseRequiredError(
                "universe generation file SHA-256 is invalid"
            )
        daily_path = config.paths.universe_dir / manifest["daily_file"]
        intervals_path = config.paths.universe_dir / manifest["intervals_file"]
    else:
        if generation_fields.intersection(manifest):
            raise PointInTimeUniverseRequiredError(
                "legacy universe manifest contains unexpected generation fields"
            )
        daily_path = config.paths.universe_dir / PIT_DAILY_ALIAS
        intervals_path = config.paths.universe_dir / PIT_INTERVALS_ALIAS
    universe_root = config.paths.universe_dir.resolve()
    if any(path.is_symlink() or path.resolve().parent != universe_root for path in (daily_path, intervals_path)):
        raise PointInTimeUniverseRequiredError("universe artifact path escapes its root")
    try:
        daily_content = daily_path.read_bytes()
        intervals_content = intervals_path.read_bytes()
    except OSError as exc:
        raise PointInTimeUniverseRequiredError(
            "universe daily or intervals artifact is missing or unreadable"
        ) from exc
    if is_generation:
        for name, content in (
            ("daily_file_sha256", daily_content),
            ("intervals_file_sha256", intervals_content),
        ):
            if manifest[name].lower() != hashlib.sha256(content).hexdigest():
                raise PointInTimeUniverseRequiredError(
                    "universe %s mismatch" % name
                )
    try:
        raw_membership = pd.read_parquet(io.BytesIO(daily_content))
        intervals = pd.read_parquet(io.BytesIO(intervals_content))
    except Exception as exc:
        raise PointInTimeUniverseRequiredError(
            "universe daily or intervals artifact is unreadable"
        ) from exc
    membership = validate_daily_membership(
        raw_membership, calendar, config.universe.expected_member_count
    )
    digest = manifest.get("sha256")
    if not _is_sha256(digest) or digest.lower() != dataframe_sha256(membership):
        raise PointInTimeUniverseRequiredError("universe content hash mismatch")
    daily_digest = manifest.get("daily_sha256")
    if is_generation and not _is_sha256(daily_digest):
        raise PointInTimeUniverseRequiredError(
            "universe generation daily_sha256 is invalid"
        )
    if daily_digest is not None and (
        not _is_sha256(daily_digest) or daily_digest.lower() != digest.lower()
    ):
        raise PointInTimeUniverseRequiredError(
            "universe daily_sha256 does not match the canonical content hash"
        )
    intervals_digest = manifest.get("intervals_sha256")
    if intervals_digest is not None and not _is_sha256(intervals_digest):
        raise PointInTimeUniverseRequiredError(
            "universe intervals_sha256 is invalid"
        )
    if is_generation and not _is_sha256(intervals_digest):
        raise PointInTimeUniverseRequiredError(
            "universe generation intervals_sha256 is invalid"
        )
    expected_intervals = compress_membership_intervals(membership)
    actual_intervals_digest = dataframe_sha256(intervals)
    expected_intervals_digest = dataframe_sha256(expected_intervals)
    if intervals_digest is None:
        intervals_mismatch = (
            actual_intervals_digest != expected_intervals_digest
        )
    else:
        intervals_mismatch = (
            actual_intervals_digest != intervals_digest.lower()
            or expected_intervals_digest != intervals_digest.lower()
        )
    if intervals_mismatch:
        raise PointInTimeUniverseRequiredError(
            "universe intervals content hash mismatch"
        )
    membership.attrs["pit_manifest"] = manifest
    return membership


def command_audit(config: ResearchConfig, run: RunManifest) -> None:
    from factorlab._identity import software_identity
    identity = software_identity()
    run.payload["architecture_audit"] = identity
    run.payload["formal_admission"] = "FORMAL_ADMISSION_BLOCKED"
    LOGGER.info("FactorLab software identity audited: %d modules; formal admission BLOCKED",
                len(identity["source_sha256"]))


def command_download(config: ResearchConfig, run: RunManifest) -> None:
    provider = _provider(config)
    benchmark_cache = _benchmark_cache(config)
    pending = list_unresolved(_download_guard_root(config))
    if any(item["context"]["symbol"] == config.data.benchmark for item in pending):
        bars = _update_guarded_bars(
            config, run, provider, benchmark_cache, config.data.benchmark,
            _benchmark_request_start(config), config.data.end, is_index=True,
        )
        benchmark = _select_benchmark_with_warmup(config, bars)
    else:
        try:
            benchmark = _download_benchmark_with_warmup(config, provider)
        except ProviderNetworkError:
            raise  # Benchmark is mandatory even for a recoverable network error.
        except Exception as exc:
            _record_fatal_download(
                config, run, provider, config.data.benchmark,
                _benchmark_request_start(config), config.data.end, True, exc,
            )
            raise
    full_calendar = TradingCalendar.from_benchmark_bars(benchmark).dates
    calendar = _research_calendar(config, full_calendar)
    run.add_output("benchmark", benchmark_cache.data_path(config.data.benchmark))
    LOGGER.info(
        "Benchmark calendar contains %d research and %d warm-up sessions",
        len(calendar),
        len(full_calendar) - len(calendar),
    )

    universe_provider = config.universe.provider.lower()
    status_provider = config.universe.status_provider.lower()
    if universe_provider == "baostock":
        raise PointInTimeUniverseRequiredError(
            BAOSTOCK_PIT_UNVERIFIED_REASON.split(": ", 1)[1]
        )
    manifest_path = config.paths.universe_dir / "manifest.json"
    existing_manifest: Dict[str, Any] = {}
    if manifest_path.exists():
        try:
            existing_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            existing_manifest = {}
    published_pit_manifest = existing_manifest
    if universe_provider == "local" and existing_manifest.get("source") != "local-import":
        raise PointInTimeUniverseRequiredError(
            "universe.provider=local requires a validated import-universe artifact"
        )

    def open_baostock(stack: ExitStack) -> BaoStockPointInTimeProvider:
        return stack.enter_context(
            BaoStockPointInTimeProvider(
                retry_count=config.network.max_retries,
                backoff_seconds=config.network.backoff_seconds,
                request_interval=config.network.request_interval_seconds,
            )
        )

    status_error: Optional[SecurityStatusDataError] = None
    status_failures: List[str] = []
    status_failure_payload: Dict[str, Any] = {"symbols": [], "error": None}
    security_manifest: Optional[Dict[str, Any]] = None
    partial_status_path: Optional[Path] = None
    partial_listings_path: Optional[Path] = None
    with ExitStack() as stack:
        pit_provider: Optional[BaoStockPointInTimeProvider] = None
        if universe_provider == "local":
            membership = _load_verified_membership(config, calendar)
            LOGGER.info("Reusing validated local-import PIT universe")
        else:
            pit_provider = open_baostock(stack)
            published_pit_manifest = collect_point_in_time_universe(
                pit_provider,
                config.universe.index_symbol,
                calendar,
                config.paths.universe_dir,
                expected_member_count=config.universe.expected_member_count,
                # BaoStockPointInTimeProvider already owns the configured
                # network retry policy. Retrying the whole snapshot here
                # would multiply attempts (for example 4 x 4 calls).
                retries=0,
                backoff_seconds=config.network.backoff_seconds,
                request_interval_seconds=config.network.request_interval_seconds,
            )
            membership = _load_verified_membership(config, calendar)
        symbols = membership["symbol"].drop_duplicates().sort_values().tolist()
        published_pit_manifest = membership.attrs.get("pit_manifest", published_pit_manifest)
        universe_version = dataframe_sha256(membership)
        try:
            _, _, security_manifest = load_verified_security_data(
                config.paths.universe_dir,
                calendar=full_calendar,
                membership=membership,
                universe_sha256=universe_version,
                expected_provider=status_provider,
            )
            LOGGER.info(
                "Reusing verified %s security-data bundle",
                status_provider,
            )
        except SecurityStatusDataError as cache_error:
            if status_provider == "local":
                raise SecurityStatusDataError(
                    "status_provider=local requires import-security-data first; %s"
                    % cache_error
                ) from cache_error
            LOGGER.info("Refreshing BaoStock security data: %s", cache_error)
            if pit_provider is None:
                pit_provider = open_baostock(stack)
            status, listings, status_failures = pit_provider.get_security_history(
                symbols, full_calendar.min(), full_calendar.max()
            )
            if status_failures:
                status_error = SecurityStatusDataError(
                    "BaoStock status failed for %d/%d symbols; complete bundle not published"
                    % (len(status_failures), len(symbols))
                )
            else:
                try:
                    security_manifest = publish_security_data(
                        status,
                        listings,
                        output_dir=config.paths.universe_dir,
                        calendar=full_calendar,
                        membership=membership,
                        universe_sha256=universe_version,
                        source="BaoStockPointInTimeProvider",
                        source_version=BAOSTOCK_SECURITY_SOURCE_VERSION,
                    )
                except SecurityStatusDataError as exc:
                    status_error = exc
            if status_error is not None:
                partial_status_path = config.paths.universe_dir / (
                    "security_status.partial.%s.parquet" % run.run_id
                )
                partial_listings_path = config.paths.universe_dir / (
                    "listing_dates.partial.%s.parquet" % run.run_id
                )
                _atomic_parquet(
                    status,
                    partial_status_path,
                )
                _atomic_parquet(
                    listings,
                    partial_listings_path,
                )
                status_failure_payload = {
                    "symbols": status_failures,
                    "error": str(status_error),
                    "partial_status": str(partial_status_path),
                    "partial_listings": str(partial_listings_path),
                }
                run.add_failures(
                    "security_status",
                    status_failure_payload,
                )

    run.payload["universe_version"] = universe_version
    run.add_output("universe", config.paths.universe_dir / published_pit_manifest.get("daily_file", PIT_DAILY_ALIAS))
    run.add_output("universe_intervals", config.paths.universe_dir / published_pit_manifest.get("intervals_file", PIT_INTERVALS_ALIAS))
    status_failure_path = config.paths.universe_dir / "status_failures.json"
    if security_manifest is not None and status_error is None:
        status_failure_payload = {
            "symbols": [],
            "error": None,
            "security_data_version": security_manifest["security_data_version"],
        }
    _atomic_json(status_failure_payload, status_failure_path)
    run.add_output("status_failures", status_failure_path)
    if status_error is not None:
        if partial_status_path is not None:
            run.add_output("partial_security_status", partial_status_path)
        if partial_listings_path is not None:
            run.add_output("partial_listing_dates", partial_listings_path)
    if security_manifest is not None:
        _record_security_data_provenance(run, security_manifest)
        run.add_output(
            "security_status",
            config.paths.universe_dir / "security_status.parquet",
        )
        run.add_output(
            "listing_dates", config.paths.universe_dir / "listing_dates.parquet"
        )
        run.add_output(
            "security_data_manifest",
            config.paths.universe_dir / "security_data_manifest.json",
        )

    cache = _stock_cache(config)
    failures: Dict[str, str] = {}
    failure_path = config.paths.raw_dir / config.data.provider / "failed_symbols.json"

    def publish_failures() -> None:
        _atomic_json(failures, failure_path)
        run.add_failures("market_data", failures)
        run.add_output("failed_symbols", failure_path)

    for number, symbol in enumerate(symbols, start=1):
        try:
            _update_guarded_bars(
                config, run, provider, cache, symbol,
                full_calendar.min(), full_calendar.max(),
            )
        except ProviderNetworkError as exc:
            failures[symbol] = str(exc)
            LOGGER.warning("Market data failed for %s: %s", symbol, exc)
        except Exception as exc:
            failures[symbol] = str(exc)
            publish_failures()
            LOGGER.error("Fatal market-data contract for %s: %s", symbol, exc)
            raise
        if number % 25 == 0 or number == len(symbols):
            LOGGER.info("Downloaded/checked %d/%d securities", number, len(symbols))
    publish_failures()
    pending = list_unresolved(_download_guard_root(config))
    run.payload["unresolved_download_receipts"] = [item["receipt_id"] for item in pending]
    if pending:
        run.add_output("download_contract_ledger", _download_guard_root(config))
        LOGGER.warning(
            "Download scan finished with %d unresolved fatal receipt(s); "
            "research commands remain blocked even when old caches exist",
            len(pending),
        )
    LOGGER.info("Market data completed with %d failed symbols", len(failures))
    if status_error is not None:
        raise status_error


def command_import_universe(
    config: ResearchConfig, run: RunManifest, input_path: str
) -> None:
    if config.universe.provider.lower() != "local":
        raise PointInTimeUniverseRequiredError(
            "import-universe requires universe.provider=local"
        )
    try:
        benchmark = _load_benchmark_with_warmup(config)
    except FactorLabError:
        provider = _provider(config)
        benchmark = _download_benchmark_with_warmup(config, provider)
    full_calendar = TradingCalendar.from_benchmark_bars(benchmark).dates
    calendar = _research_calendar(config, full_calendar)
    manifest = import_point_in_time_universe(
        _resolve_input_path(config, input_path),
        config.paths.universe_dir,
        calendar,
        expected_member_count=config.universe.expected_member_count,
        index_symbol=config.universe.index_symbol,
    )
    run.payload["universe_version"] = manifest["sha256"]
    run.payload["universe_input_layout"] = manifest["input_layout"]
    run.payload["universe_source_file_sha256"] = manifest["source_file_sha256"]
    run.add_output("universe", config.paths.universe_dir / manifest.get("daily_file", PIT_DAILY_ALIAS))
    run.add_output(
        "universe_intervals",
        config.paths.universe_dir / manifest.get("intervals_file", PIT_INTERVALS_ALIAS),
    )
    run.add_output("universe_manifest", config.paths.universe_dir / "manifest.json")
    LOGGER.info("Imported and validated %d PIT sessions", manifest["trading_dates"])


def command_import_market_data(
    config: ResearchConfig,
    run: RunManifest,
    stock_path: str,
    benchmark_path: str,
    source: str,
    source_version: str,
    qualification_manifest: Optional[str] = None,
) -> None:
    """Publish a verified static local snapshot without contacting any vendor."""

    if config.data.provider != "local_parquet" or config.paths.market_input_root is None:
        raise FactorLabError(
            "import-market-data requires local_parquet and paths.market_input_root"
        )
    qualification_args = {}
    if qualification_manifest is not None:
        qualification_args["qualification_manifest"] = _resolve_input_path(config, qualification_manifest)
    manifest = publish_local_market_data(
        _resolve_input_path(config, stock_path),
        _resolve_input_path(config, benchmark_path),
        config.paths.market_input_root, source, source_version,
        benchmark_symbol=config.data.benchmark,
        **qualification_args,
    )
    run.payload["market_data_bundle_version"] = manifest["bundle_version"]
    run.payload["market_data_bundle_manifest"] = manifest
    run.add_output(
        "market_data_manifest", config.paths.market_input_root / "manifest.json"
    )
    for role in ("stocks", "benchmark"):
        run.add_output(
            "market_data_" + role,
            config.paths.market_input_root / manifest[role + "_file"],
        )
    if manifest["schema_version"] == 2:
        run.payload["market_data_qualification"] = manifest["qualification_report"]
        run.add_output("market_qualification_manifest",
                       config.paths.market_input_root / manifest["qualification_dir"] / "manifest.json")
    LOGGER.info("Local market-data snapshot published: %s", manifest["bundle_version"])


def command_import_security_data(
    config: ResearchConfig,
    run: RunManifest,
    status_path: str,
    listings_path: str,
) -> None:
    """Import one complete local status/listing bundle without BaoStock."""

    if config.universe.status_provider.lower() != "local":
        raise SecurityStatusDataError(
            "import-security-data requires universe.status_provider=local"
        )
    try:
        benchmark = _load_benchmark_with_warmup(config)
    except FactorLabError:
        provider = _provider(config)
        benchmark = _download_benchmark_with_warmup(config, provider)
    full_calendar = TradingCalendar.from_benchmark_bars(benchmark).dates
    research_calendar = _research_calendar(config, full_calendar)
    membership = _load_verified_membership(config, research_calendar)
    universe_version = dataframe_sha256(membership)
    manifest = import_security_data(
        _resolve_input_path(config, status_path),
        _resolve_input_path(config, listings_path),
        output_dir=config.paths.universe_dir,
        calendar=full_calendar,
        membership=membership,
        universe_sha256=universe_version,
    )
    run.payload["universe_version"] = universe_version
    _record_security_data_provenance(run, manifest)
    status_failure_path = config.paths.universe_dir / "status_failures.json"
    _atomic_json(
        {
            "symbols": [],
            "error": None,
            "security_data_version": manifest["security_data_version"],
        },
        status_failure_path,
    )
    run.add_output(
        "security_status", config.paths.universe_dir / "security_status.parquet"
    )
    run.add_output(
        "listing_dates", config.paths.universe_dir / "listing_dates.parquet"
    )
    run.add_output(
        "security_data_manifest",
        config.paths.universe_dir / "security_data_manifest.json",
    )
    run.add_output("status_failures", status_failure_path)
    LOGGER.info(
        "Imported verified security data: %d rows for %d symbols",
        manifest["status_rows"],
        manifest["symbols"],
    )


def _load_stock_bars(config: ResearchConfig, symbols: Iterable[str]) -> pd.DataFrame:
    assert_no_unresolved(_download_guard_root(config))
    cache = _stock_cache(config)
    provider = _provider(config)
    parts: List[pd.DataFrame] = []
    missing: List[str] = []
    for symbol in symbols:
        bars = cache.load(
            symbol,
            expected_source=provider.__class__.__name__,
            expected_source_version=provider.cache_source_version(symbol, False),
        )
        if bars.empty:
            missing.append(symbol)
        else:
            parts.append(bars)
    if missing:
        LOGGER.warning("No cached bars for %d member securities", len(missing))
    if not parts:
        raise FactorLabError("No stock bars are available; run download-data first")
    return pd.concat(parts, ignore_index=True)


class _PinnedBarInputs(tuple):
    """Keep the existing four-value interface plus the already pinned provider."""

    def __new__(cls, values, provider):
        result = super().__new__(cls, values)
        result.provider = provider
        return result


def _admit_bar_inputs(config, inputs, membership, calendar, *, status=None,
                      listings=None, require_execution=False):
    """Recheck even supplied snapshots; a tuple or receipt is not admission."""
    return assert_formal_market_admission(
        getattr(inputs, "provider", None), membership=membership, calendar=calendar,
        bars=inputs[1], benchmark=inputs[0], research_start=config.data.start,
        research_end=config.data.end, status=status, listings=listings,
        require_execution=require_execution,
    )


def _load_bar_inputs(config: ResearchConfig, membership: pd.DataFrame, benchmark_snapshot=None) -> tuple:
    """Pin complete bar inputs, including absent PIT members, before research.

    Full-cache hashes are deliberately conservative: even an append outside
    the research window invalidates prior datasets until they are rebuilt.
    """

    assert_no_unresolved(_download_guard_root(config))
    provider = _provider(config)
    source = provider.__class__.__name__
    if benchmark_snapshot is None:
        benchmark_snapshot = _load_benchmark_snapshot(config)
    benchmark = _select_benchmark_with_warmup(config, benchmark_snapshot.frame)
    symbols = membership["symbol"].drop_duplicates().sort_values().tolist()
    cache = _stock_cache(config)
    identities = {
        symbol: (source, provider.cache_source_version(symbol, False))
        for symbol in symbols
    }
    snapshots = {
        symbol: cache.load_snapshot(
            symbol, expected_source=identities[symbol][0],
            expected_source_version=identities[symbol][1],
        )
        for symbol in symbols
    }
    parts = [snapshot.frame for snapshot in snapshots.values() if not snapshot.frame.empty]
    if not parts:
        raise FactorLabError("No stock bars are available; run download-data first")
    missing = [symbol for symbol, snapshot in snapshots.items() if snapshot.frame.empty]
    if missing:
        LOGGER.warning("No cached bars for %d member securities", len(missing))
    bars = pd.concat(parts, ignore_index=True)
    provenance = build_bar_data_provenance(
        config.data.benchmark, benchmark_snapshot, snapshots, identities,
        (source, provider.cache_source_version(config.data.benchmark, True)),
    )
    pinned = _PinnedBarInputs((benchmark, bars, provenance, bar_data_version(provenance)), provider)
    _admit_bar_inputs(config, pinned, membership, TradingCalendar.from_benchmark_bars(benchmark).dates)
    return pinned


def command_build_dataset(config: ResearchConfig, run: RunManifest) -> None:
    benchmark_snapshot = _load_benchmark_snapshot(config)
    benchmark = _select_benchmark_with_warmup(config, benchmark_snapshot.frame)
    calendar = TradingCalendar.from_benchmark_bars(benchmark).dates
    research_calendar = _research_calendar(config, calendar)
    membership = _load_verified_membership(config, research_calendar)
    universe_version = dataframe_sha256(membership)
    symbols = membership["symbol"].drop_duplicates().sort_values().tolist()
    status, listings, security_manifest = load_verified_security_data(
        config.paths.universe_dir,
        calendar=calendar,
        membership=membership,
        universe_sha256=universe_version,
        expected_provider=config.universe.status_provider,
    )
    bar_inputs = _load_bar_inputs(
        config, membership, benchmark_snapshot
    )
    benchmark, bars, bar_provenance, bar_version = bar_inputs
    admission = _admit_bar_inputs(
        config, bar_inputs, membership, calendar, status=status, listings=listings,
    )
    run.payload["market_admission"] = admission
    filter_config = UniverseFilterConfig(
        min_listed_days=config.universe.min_listed_days,
        exclude_st=config.universe.exclude_st,
        exclude_suspended=config.universe.exclude_suspended,
        zero_volume_window=config.universe.zero_volume_window_days,
        quality_window=config.universe.data_quality_window_days,
        min_quality_ratio=config.universe.min_valid_ohlcv_ratio,
    )
    options = DatasetBuildOptions(
        factor_names=config.factors,
        holding_days=config.label.holding_days,
        entry_price=config.label.entry_price,
        expected_member_count=config.universe.expected_member_count,
        winsorization=config.preprocessing.winsorization,
        mad_multiplier=config.preprocessing.mad_multiplier,
        minimum_cross_section=config.evaluation.min_cross_section,
        include_market_regime=config.market_regime.enabled,
        filter_config=filter_config,
    )
    builder = DatasetBuilder(options)
    dataset = builder.build(bars, benchmark, membership, status, listings, calendar)
    destination = config.paths.processed_dir / "dataset.parquet"
    builder.write_artifacts(dataset, destination, config.paths.features_dir)
    dataset_manifest_path = config.paths.processed_dir / "dataset_manifest.json"
    dataset_sha256 = _file_sha256(destination)
    _atomic_json(
        {
            "schema_version": 4,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "dataset_path": str(destination),
            "dataset_sha256": dataset_sha256,
            "universe_version": universe_version,
            "bar_data_provenance": bar_provenance,
            "bar_data_version": bar_version,
            "security_data_version": security_manifest["security_data_version"],
            "security_data_source": security_manifest["source"],
            "security_data_source_version": security_manifest["source_version"],
            "security_data_manifest_sha256": _security_manifest_sha256(
                security_manifest
            ),
            "security_status_sha256": security_manifest["status_sha256"],
            "listing_dates_sha256": security_manifest["listings_sha256"],
            "security_data_input_sha256": security_manifest["input_sha256"],
            "factor_list": list(config.factors),
            "target": _target_column(config),
            "rows": int(len(dataset)),
            "dates": int(dataset["date"].nunique()),
            "data_start": config.data.start.isoformat(),
            "data_end": config.data.end.isoformat(),
            "dataset_contract": _dataset_contract(config),
            "market_admission": admission,
            "producer_run_id": run.run_id,
            "producer_provenance": validate_provenance(run.payload),
        },
        dataset_manifest_path,
    )
    run.add_output("dataset", destination)
    run.add_output("dataset_manifest", dataset_manifest_path)
    run.payload["dataset_rows"] = int(len(dataset))
    run.payload["dataset_dates"] = int(dataset["date"].nunique())
    run.payload["dataset_sha256"] = dataset_sha256
    run.payload["universe_version"] = universe_version
    run.payload["bar_data_provenance"] = bar_provenance
    run.payload["bar_data_version"] = bar_version
    _record_security_data_provenance(run, security_manifest)
    LOGGER.info("Dataset written: %d rows across %d dates", len(dataset), dataset["date"].nunique())


def command_evaluate(config: ResearchConfig, run: RunManifest) -> None:
    benchmark = _load_benchmark_with_warmup(config)
    full_calendar = TradingCalendar.from_benchmark_bars(benchmark).dates
    research_calendar = _research_calendar(config, full_calendar)
    dataset, dataset_sha256, universe_version = _load_verified_dataset(
        config, research_calendar, run, full_calendar=full_calendar
    )
    run.payload["dataset_sha256"] = dataset_sha256
    run.payload["universe_version"] = universe_version
    target = _target_column(config)
    evaluation = evaluate_factors(
        dataset,
        config.factors,
        target=target,
        quantiles=config.evaluation.quantiles,
        minimum_cross_section=config.evaluation.min_cross_section,
        group_column=("MARKET_REGIME" if config.market_regime.enabled else None),
        calendar=research_calendar,
    )
    summary = write_factor_report(evaluation, config.paths.factor_results_dir)
    run.add_output("factor_report", summary)
    valid_rank_ic = int(evaluation.daily_ic["RankIC"].notna().sum())
    run.payload["valid_rank_ic_observations"] = valid_rank_ic
    if valid_rank_ic == 0:
        raise FactorLabError("Factor evaluation produced no valid RankIC observations")
    LOGGER.info("Factor report written with %d valid daily RankIC values", valid_rank_ic)


def _target_column(config: ResearchConfig) -> str:
    return "EXCESS_RET%d" % config.label.holding_days


def _load_verified_dataset(
    config: ResearchConfig,
    research_calendar: pd.DatetimeIndex,
    run: Optional[RunManifest] = None,
    *,
    full_calendar: Optional[pd.DatetimeIndex] = None,
    bar_inputs: Optional[tuple] = None,
    require_execution: bool = False,
) -> tuple:
    assert_no_unresolved(_download_guard_root(config))
    membership = _load_verified_membership(config, research_calendar)
    universe_version = dataframe_sha256(membership)
    if bar_inputs is None:
        bar_inputs = _load_bar_inputs(config, membership)
    benchmark, _, current_bar_provenance, current_bar_version = bar_inputs
    validate_bar_data_provenance(
        current_bar_provenance, benchmark_symbol=config.data.benchmark,
        stock_symbols=membership["symbol"].drop_duplicates().tolist(),
    )
    if current_bar_version != bar_data_version(current_bar_provenance):
        raise FactorLabError("Pinned bar-data version is inconsistent")
    security_calendar = (
        full_calendar if full_calendar is not None else TradingCalendar.from_benchmark_bars(benchmark).dates
    )
    if not TradingCalendar.from_benchmark_bars(benchmark).dates.equals(security_calendar):
        raise FactorLabError("Pinned benchmark calendar differs from the dataset consumer calendar")
    security_status, security_listings, security_manifest = load_verified_security_data(
        config.paths.universe_dir,
        calendar=security_calendar,
        membership=membership,
        universe_sha256=universe_version,
        expected_provider=config.universe.status_provider,
    )
    admission = _admit_bar_inputs(
        config, bar_inputs, membership, security_calendar,
        status=security_status, listings=security_listings,
        require_execution=require_execution,
    )
    if run is not None:
        run.payload["market_admission"] = admission
    dataset_path = config.paths.processed_dir / "dataset.parquet"
    manifest_path = config.paths.processed_dir / "dataset_manifest.json"
    if not dataset_path.exists():
        raise FactorLabError("Dataset is missing; run build-dataset first")
    if not manifest_path.exists():
        raise FactorLabError(
            "Dataset manifest is missing; rebuild the dataset before evaluation"
        )
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, AttributeError) as exc:
        raise FactorLabError("Dataset manifest is unreadable") from exc
    # Read the exact bytes that will be parsed.  Hashing the path and opening
    # it again later creates a TOCTOU window when another process atomically
    # replaces dataset.parquet between those two operations.
    try:
        dataset_content = dataset_path.read_bytes()
    except OSError as exc:
        raise FactorLabError("Dataset is unreadable") from exc
    dataset_sha256 = hashlib.sha256(dataset_content).hexdigest()
    expected = {
        "schema_version": 4,
        "dataset_sha256": dataset_sha256,
        "universe_version": universe_version,
        "bar_data_version": current_bar_version,
        "security_data_version": security_manifest["security_data_version"],
        "security_data_source": security_manifest["source"],
        "security_data_source_version": security_manifest["source_version"],
        "security_data_manifest_sha256": _security_manifest_sha256(
            security_manifest
        ),
        "security_status_sha256": security_manifest["status_sha256"],
        "listing_dates_sha256": security_manifest["listings_sha256"],
        "security_data_input_sha256": security_manifest["input_sha256"],
        "factor_list": list(config.factors),
        "target": _target_column(config),
        "data_start": config.data.start.isoformat(),
        "data_end": config.data.end.isoformat(),
        "dataset_contract": _dataset_contract(config),
    }
    mismatches = [name for name, value in expected.items() if manifest.get(name) != value]
    if mismatches:
        raise FactorLabError(
            "Dataset manifest mismatch (%s); rebuild before evaluation"
            % ", ".join(mismatches)
        )
    recorded_bar_provenance = validate_bar_data_provenance(
        manifest.get("bar_data_provenance"),
        benchmark_symbol=config.data.benchmark,
        stock_symbols=membership["symbol"].drop_duplicates().tolist(),
    )
    if bar_data_version(recorded_bar_provenance) != current_bar_version:
        raise FactorLabError("Dataset bar-data evidence mismatch; rebuild before evaluation")
    try:
        producer_provenance = validate_provenance(
            manifest.get("producer_provenance", {})
        )
    except ValueError as exc:
        raise FactorLabError(
            "Dataset manifest has missing or unknown producer provenance: %s"
            % exc
        ) from exc
    producer_run_id = manifest.get("producer_run_id")
    if (
        not isinstance(producer_run_id, str)
        or not producer_run_id.strip()
        or Path(producer_run_id).name != producer_run_id
    ):
        raise FactorLabError("Dataset manifest producer_run_id is missing")
    producer_manifest_path = (
        config.paths.runs_dir / producer_run_id / "manifest.json"
    )
    try:
        producer_manifest = json.loads(
            producer_manifest_path.read_text(encoding="utf-8")
        )
    except (OSError, ValueError, AttributeError) as exc:
        raise FactorLabError(
            "Dataset producer run manifest is missing or unreadable"
        ) from exc
    producer_expected = {
        "run_id": producer_run_id,
        "command": "build-dataset",
        "status": "success",
        "dataset_sha256": dataset_sha256,
        "universe_version": universe_version,
        "bar_data_version": current_bar_version,
        "bar_data_provenance": manifest["bar_data_provenance"],
        "security_data_version": security_manifest["security_data_version"],
        "security_data_source": security_manifest["source"],
        "security_data_source_version": security_manifest["source_version"],
        "security_data_manifest_sha256": _security_manifest_sha256(
            security_manifest
        ),
        "security_status_sha256": security_manifest["status_sha256"],
        "listing_dates_sha256": security_manifest["listings_sha256"],
        "security_data_input_sha256": security_manifest["input_sha256"],
    }
    producer_mismatches = [
        name
        for name, expected_value in producer_expected.items()
        if producer_manifest.get(name) != expected_value
    ]
    if producer_mismatches:
        raise FactorLabError(
            "Dataset producer run manifest mismatch (%s)"
            % ", ".join(producer_mismatches)
        )
    try:
        producer_contract = _dataset_contract_from_snapshot(
            producer_manifest.get("config")
        )
    except FactorLabError as exc:
        raise FactorLabError(
            "Dataset producer run config is invalid: %s" % exc
        ) from exc
    if producer_contract != manifest.get("dataset_contract"):
        raise FactorLabError(
            "Dataset producer run config does not match the dataset contract"
        )
    try:
        recorded_producer_provenance = validate_provenance(producer_manifest)
    except ValueError as exc:
        raise FactorLabError(
            "Dataset producer run has missing or unknown provenance: %s" % exc
        ) from exc
    if recorded_producer_provenance != producer_provenance:
        raise FactorLabError(
            "Dataset manifest producer provenance differs from its build run"
        )
    if run is not None:
        current_provenance = _comparison_provenance(
            run.payload, "Current dataset consumer run"
        )
        mismatched_provenance = [
            field
            for field in PROVENANCE_FIELDS
            if producer_provenance[field] != current_provenance[field]
        ]
        if mismatched_provenance:
            raise FactorLabError(
                "Dataset producer provenance differs from the current environment (%s); "
                "rebuild the dataset"
                % ", ".join(mismatched_provenance)
            )
        run.payload["dataset_producer_run_id"] = producer_run_id
        run.payload["dataset_producer_provenance"] = producer_provenance
        run.payload["bar_data_provenance"] = current_bar_provenance
        run.payload["bar_data_version"] = current_bar_version
        _record_security_data_provenance(run, security_manifest)
    try:
        dataset = pd.read_parquet(io.BytesIO(dataset_content))
    except (OSError, ValueError, TypeError) as exc:
        raise FactorLabError("Dataset Parquet content is unreadable") from exc
    required = {
        "date",
        "symbol",
        "entry_date",
        "exit_date",
        _target_column(config),
    }.union(config.factors)
    if config.market_regime.enabled:
        required.update(MARKET_FEATURE_COLUMNS)
    missing = sorted(required.difference(dataset.columns))
    if missing:
        raise FactorLabError(
            "Dataset is missing model/audit columns: %s" % ", ".join(missing)
        )
    if dataset.empty:
        raise FactorLabError("Dataset is empty")
    result = dataset.copy()
    result["date"] = pd.to_datetime(result["date"], errors="raise").dt.normalize()
    result.sort_values(["date", "symbol"], kind="mergesort", inplace=True)
    result.reset_index(drop=True, inplace=True)
    if result.duplicated(["date", "symbol"]).any():
        raise FactorLabError("Dataset contains duplicate (date, symbol) rows")
    if config.market_regime.enabled:
        if result["MARKET_REGIME"].isna().any():
            raise FactorLabError("Dataset contains missing MARKET_REGIME values")
        invalid_regimes = sorted(
            set(result["MARKET_REGIME"].astype(str)).difference(MARKET_REGIMES)
        )
        if invalid_regimes:
            raise FactorLabError(
                "Dataset contains invalid MARKET_REGIME values: %s"
                % ", ".join(invalid_regimes)
            )
        regime_counts = result.groupby("date")["MARKET_REGIME"].nunique()
        if not regime_counts.eq(1).all():
            raise FactorLabError(
                "MARKET_REGIME must have exactly one value per trading date"
            )
    if manifest.get("rows") != len(result) or manifest.get("dates") != result["date"].nunique():
        raise FactorLabError("Dataset manifest row/date counts do not match the data")
    return result, dataset_sha256, universe_version


def _walk_forward_splitter(config: ResearchConfig) -> ExpandingWalkForwardSplitter:
    values = config.validation
    return ExpandingWalkForwardSplitter(
        initial_train_months=values.initial_train_months,
        validation_months=values.validation_months,
        test_months=values.test_months,
        step_months=values.step_months,
        purge_days=values.purge_days,
        embargo_days=values.embargo_days,
    )


def _model_hyperparameters(config: ResearchConfig, model_name: str) -> Dict[str, Any]:
    if model_name == "factor_score":
        return {
            "weights": dict(config.models.factor_score.weights),
            "normalize_by_absolute_weight": (
                config.models.factor_score.normalize_by_absolute_weight
            ),
        }
    if model_name == "ridge":
        return {
            "alphas": list(config.models.ridge.alphas),
            "fit_intercept": config.models.ridge.fit_intercept,
            "max_iter": config.models.ridge.max_iter,
            "tolerance": config.models.ridge.tolerance,
        }
    if model_name == "lightgbm":
        return {
            "candidates": [
                asdict(candidate) for candidate in config.models.lightgbm.candidates
            ],
            "n_jobs": config.models.lightgbm.n_jobs,
        }
    if model_name == "xgboost":
        return {
            "candidates": [
                asdict(candidate) for candidate in config.models.xgboost.candidates
            ],
            "n_jobs": config.models.xgboost.n_jobs,
        }
    raise FactorLabError("Unsupported model: %s" % model_name)


def _json_metrics(values: Dict[str, float]) -> Dict[str, Optional[float]]:
    result: Dict[str, Optional[float]] = {}
    for name, value in values.items():
        numeric = float(value)
        result[name] = numeric if math.isfinite(numeric) else None
    return result


def _backtest_window(
    predictions: pd.DataFrame,
    bars: pd.DataFrame,
    benchmark: pd.DataFrame,
) -> tuple:
    start = pd.Timestamp(predictions["date"].min()).normalize()
    if "exit_date" not in predictions or predictions["exit_date"].dropna().empty:
        raise FactorLabError(
            "Out-of-sample predictions require exit_date for backtest horizon audit"
        )
    end = pd.to_datetime(predictions["exit_date"], errors="raise").max().normalize()
    benchmark_dates = pd.to_datetime(benchmark["date"], errors="raise").dt.normalize()
    selected_benchmark = benchmark.loc[benchmark_dates.between(start, end)].copy()
    bar_dates = pd.to_datetime(bars["date"], errors="raise").dt.normalize()
    selected_bars = bars.loc[bar_dates.between(start, end)].copy()
    if selected_benchmark.empty or selected_bars.empty:
        raise FactorLabError("Backtest price window is empty")
    return selected_bars, selected_benchmark


def _attach_execution_status(
    config: ResearchConfig,
    bars: pd.DataFrame,
    full_calendar: pd.DatetimeIndex,
    research_calendar: pd.DatetimeIndex,
    *,
    expected_security_data_version: str,
    expected_security_manifest_sha256: str,
) -> pd.DataFrame:
    """Join one previously audited status generation to cached price rows.

    Status is mandatory for point-in-time membership keys, not for every
    historical price row.  Missing values can therefore remain on warm-up,
    pre-entry, or post-exit bars.  The execution engine fails fast if an
    actual order reaches one of those unaudited rows.
    """

    required_status = {"date", "symbol", "trade_status", "is_st"}
    membership = _load_verified_membership(config, research_calendar)
    universe_version = dataframe_sha256(membership)
    status, _, security_manifest = load_verified_security_data(
        config.paths.universe_dir,
        calendar=full_calendar,
        membership=membership,
        universe_sha256=universe_version,
        expected_provider=config.universe.status_provider,
    )
    actual_manifest_sha256 = _security_manifest_sha256(security_manifest)
    if (
        security_manifest["security_data_version"]
        != expected_security_data_version
        or actual_manifest_sha256 != expected_security_manifest_sha256
    ):
        raise FactorLabError(
            "Security data changed after dataset verification; restart the run"
        )
    status = status.loc[:, sorted(required_status)].copy()
    price_data = bars.drop(
        columns=[name for name in ("trade_status", "is_st") if name in bars],
        errors="ignore",
    ).copy()
    price_data["date"] = pd.to_datetime(
        price_data["date"], errors="raise"
    ).dt.normalize()
    joined = price_data.merge(
        status,
        on=["date", "symbol"],
        how="left",
        validate="one_to_one",
    )
    return (
        joined.sort_values(["date", "symbol"], kind="mergesort")
        .reset_index(drop=True)
    )


def _bind_diagnostic_sources(
    config: ResearchConfig,
    runner: WalkForwardRunner,
    result: WalkForwardResult,
    dataset: pd.DataFrame,
    calendar: pd.DatetimeIndex,
) -> None:
    """Rebuild audited stage cohorts from the same verified in-memory input.

    This only repeats deterministic validation/splitting and hashes features,
    labels and keys. No model is fitted, selected or predicted here.
    """
    from factorlab.reports.model import ModelReportError, validate_stage_diagnostics

    try:
        tables = validate_stage_diagnostics(result)
    except ModelReportError as exc:
        raise FactorLabError("Invalid stage diagnostics: %s" % exc) from exc
    if not tables:
        raise FactorLabError("Formal walk-forward requires complete stage diagnostics")
    prepared = runner._validate_data(dataset)
    folds = list(runner.splitter.split(prepared, calendar=calendar))
    audit = tables["stage_prediction_audit"]
    if set(audit["fold"]) != {fold.fold for fold in folds}:
        raise FactorLabError("Stage diagnostic folds differ from verified dataset splitting")
    for fold in folds:
        train = prepared.iloc[fold.train_indices]
        fitted = runner._diagnostic_source_frame(
            train.loc[train[_target_column(config)].notna()],
            entry_date_col="entry_date", exit_date_col="exit_date",
        )
        fitted_keys = diagnostic_frame_sha256(fitted[["date", "symbol"]])
        fitted_sha = diagnostic_frame_sha256(fitted)
        for stage, indices in (
            ("TRAIN_IN_SAMPLE", fold.train_indices),
            ("VALIDATION_SELECTION", fold.validation_indices),
            ("HISTORICAL_TEST", fold.test_indices),
        ):
            row = audit.loc[audit["fold"].eq(fold.fold) & audit["stage"].eq(stage)].iloc[0]
            source = runner._diagnostic_source_frame(
                prepared.iloc[indices], entry_date_col="entry_date", exit_date_col="exit_date",
            )
            expected = {
                "source_keys_sha256": diagnostic_frame_sha256(source[["date", "symbol"]]),
                "source_sha256": diagnostic_frame_sha256(source),
                "fit_source_keys_sha256": fitted_keys,
                "fit_source_sha256": fitted_sha,
                "rows": len(source), "model_name": runner.model_name,
                "target_column": _target_column(config),
                "top_k": config.portfolio.top_k,
                "minimum_cross_section": config.evaluation.min_cross_section,
                "random_seed": config.seed,
                "selection_metric": runner.selection_metric,
            }
            if any(row[name] != value for name, value in expected.items()):
                raise FactorLabError("Stage diagnostic source/config differs from verified dataset: fold %s/%s" % (fold.fold, stage))
            if json.loads(row["feature_names"]) != list(config.factors):
                raise FactorLabError("Stage diagnostic features differ from config")


def _stage_manifest_binding(
    result: WalkForwardResult,
    output_dir: Path,
    inventory: Dict[str, str],
    dataset_sha256: str,
    universe_version: str,
) -> Dict[str, Any]:
    """Bind complete stage artifacts and actual saved fold models to input IDs."""
    from factorlab.reports.model import ModelReportError, validate_stage_diagnostics
    from factorlab.forward.integrity import assert_no_link_components

    try:
        tables = validate_stage_diagnostics(result)
    except ModelReportError as exc:
        raise FactorLabError("Invalid stage diagnostics: %s" % exc) from exc
    if not tables or not _STAGE_ARTIFACTS.issubset(inventory):
        raise FactorLabError("Stage diagnostic report artifacts are incomplete")
    if not _is_sha256(dataset_sha256) or not _is_sha256(universe_version):
        raise FactorLabError("Stage diagnostics require verified dataset/universe identities")
    root = assert_no_link_components(Path(output_dir))
    models = {}
    for row in tables["stage_prediction_audit"].itertuples(index=False):
        if not isinstance(row.model_path, str) or not _is_sha256(row.saved_model_sha256):
            raise FactorLabError("Formal stage diagnostics require saved model evidence")
        model = assert_no_link_components(Path(row.model_path))
        try:
            relative = model.relative_to(root).as_posix()
        except ValueError as exc:
            raise FactorLabError("Stage diagnostic model path escapes its artifact root") from exc
        if not relative.startswith("fold_models/") or inventory.get(relative) != row.saved_model_sha256:
            raise FactorLabError("Stage diagnostic saved model SHA-256 differs from inventory")
        models[relative] = row.saved_model_sha256
    if len(models) != len(result.fold_summary):
        raise FactorLabError("Stage diagnostics must identify one saved model per fold")
    return {
        "schema_version": 1, "policy": _stage_diagnostic_policy(),
        "dataset_sha256": dataset_sha256, "universe_version": universe_version,
        "table_sha256": {name: diagnostic_frame_sha256(table) for name, table in tables.items()},
        "table_rows": {name: len(table) for name, table in tables.items()},
        "saved_model_sha256": models,
    }


def command_walk_forward(
    config: ResearchConfig,
    run: RunManifest,
    model_name: str,
) -> None:
    """Run one leakage-audited model and both out-of-sample backtests."""

    benchmark = _load_benchmark_with_warmup(config)
    full_calendar = TradingCalendar.from_benchmark_bars(benchmark).dates
    research_calendar = _research_calendar(config, full_calendar)
    membership = _load_verified_membership(config, research_calendar)
    bar_inputs = _load_bar_inputs(config, membership)
    benchmark, pinned_stock_bars, _, _ = bar_inputs
    dataset, dataset_sha256, universe_version = _load_verified_dataset(
        config, research_calendar, run, full_calendar=full_calendar,
        bar_inputs=bar_inputs,
        require_execution=True,
    )
    pinned_calendar = _research_calendar(
        config, TradingCalendar.from_benchmark_bars(benchmark).dates,
    )
    if not research_calendar.equals(pinned_calendar):
        raise FactorLabError("Benchmark calendar changed between verified input snapshots")
    output_dir = config.paths.model_results_dir / model_name / run.run_id
    model_dir = output_dir / "fold_models"

    runner_options: Dict[str, Any] = {
        "model_name": model_name,
        "feature_names": config.factors,
        "target_col": _target_column(config),
        "top_k": config.portfolio.top_k,
        "minimum_cross_section": config.evaluation.min_cross_section,
        "random_seed": config.seed,
        "model_directory": model_dir,
        "regime_col": (
            "MARKET_REGIME" if config.market_regime.enabled else None
        ),
    }
    if model_name == "factor_score":
        weights = config.models.factor_score.weights
        if not weights:
            raise FactorLabError(
                "models.factor_score.weights are required for factor_score"
            )
        runner_options.update(
            factor_weights=weights,
            normalize_by_absolute_weight=(
                config.models.factor_score.normalize_by_absolute_weight
            ),
        )
    elif model_name == "ridge":
        runner_options.update(
            ridge_alphas=config.models.ridge.alphas,
            ridge_fit_intercept=config.models.ridge.fit_intercept,
            ridge_max_iter=config.models.ridge.max_iter,
            ridge_tolerance=config.models.ridge.tolerance,
        )
    elif model_name == "lightgbm":
        runner_options.update(
            lightgbm_candidates=tuple(
                asdict(candidate)
                for candidate in config.models.lightgbm.candidates
            ),
            lightgbm_n_jobs=config.models.lightgbm.n_jobs,
        )
    elif model_name == "xgboost":
        runner_options.update(
            xgboost_candidates=tuple(
                asdict(candidate)
                for candidate in config.models.xgboost.candidates
            ),
            xgboost_n_jobs=config.models.xgboost.n_jobs,
        )
    else:
        raise FactorLabError("Unsupported model: %s" % model_name)

    runner = WalkForwardRunner(
        _walk_forward_splitter(config), **runner_options
    )
    walk_forward = runner.run(dataset, calendar=research_calendar)
    _bind_diagnostic_sources(config, runner, walk_forward, dataset, research_calendar)
    prediction = evaluate_predictions(
        walk_forward.predictions,
        target_col=_target_column(config),
        top_k=config.portfolio.top_k,
        minimum_cross_section=config.evaluation.min_cross_section,
        fold_col="fold",
        regime_col=("MARKET_REGIME" if config.market_regime.enabled else None),
    )
    overall = prediction.overall
    if overall.empty or int(overall.iloc[0]["rank_ic_valid_dates"]) == 0:
        raise FactorLabError("Walk-forward produced no valid out-of-sample RankIC")

    symbols = walk_forward.predictions["symbol"].drop_duplicates().sort_values()
    bars = pinned_stock_bars.loc[pinned_stock_bars["symbol"].isin(symbols)].copy()
    bars, backtest_benchmark = _backtest_window(
        walk_forward.predictions, bars, benchmark
    )
    backtest_stock_bars_sha256 = dataframe_sha256(bars)
    backtest_benchmark_sha256 = dataframe_sha256(backtest_benchmark)
    backtest = run_fast_backtest(
        walk_forward.predictions[["date", "symbol", "score"]],
        bars,
        backtest_benchmark,
        config=FastBacktestConfig(
            top_k=config.portfolio.top_k,
            rebalance_days=config.portfolio.rebalance_days,
            minimum_cross_section=config.evaluation.min_cross_section,
            initial_cash=config.backtest.initial_cash,
            price_column="adj_open",
            benchmark_price_column="adj_open",
        ),
        fee_schedule=FeeSchedule.from_config(config.fees),
    )
    portfolio_metrics = calculate_portfolio_metrics(
        backtest.nav,
        backtest.trades,
        backtest.positions,
        periods_per_year=config.backtest.annualization_days,
        risk_free_rate=config.backtest.risk_free_rate,
    )
    execution_bars = _attach_execution_status(
        config,
        bars,
        full_calendar,
        research_calendar,
        expected_security_data_version=run.payload["security_data_version"],
        expected_security_manifest_sha256=run.payload[
            "security_data_manifest_sha256"
        ],
    )
    execution_bars_sha256 = dataframe_sha256(execution_bars)
    execution = run_execution_backtest(
        walk_forward.predictions[["date", "symbol", "score"]],
        execution_bars,
        backtest_benchmark,
        config=ExecutionBacktestConfig(
            top_k=config.portfolio.top_k,
            rebalance_days=config.portfolio.rebalance_days,
            minimum_cross_section=config.evaluation.min_cross_section,
            initial_cash=config.backtest.initial_cash,
            board_lot=config.backtest.board_lot,
            t_plus_one=config.backtest.t_plus_one,
            reject_suspended=config.backtest.reject_suspended,
            price_limit_tolerance_bps=(
                config.backtest.price_limit_tolerance_bps
            ),
            open_price_column="raw_open",
            close_price_column="raw_close",
            benchmark_open_price_column="adj_open",
            benchmark_close_price_column="adj_close",
        ),
        fee_schedule=FeeSchedule.from_config(config.fees),
        price_limit_schedule=PriceLimitSchedule.from_config(
            config.backtest.price_limits
        ),
    )
    execution_metrics = calculate_portfolio_metrics(
        execution.nav,
        execution.trades,
        execution.positions,
        periods_per_year=config.backtest.annualization_days,
        risk_free_rate=config.backtest.risk_free_rate,
    )
    summary_path = write_model_report(
        model_name,
        walk_forward,
        prediction,
        backtest,
        portfolio_metrics,
        output_dir,
        execution_backtest=execution,
        execution_metrics=execution_metrics,
    )
    calendar_table = pd.DataFrame({"date": pinned_calendar})
    _atomic_csv(calendar_table, output_dir / "evaluation_calendar.csv")
    _atomic_parquet(calendar_table, output_dir / "evaluation_calendar.parquet")
    run.payload["inference_input_version"] = 1
    run.payload["inference_calendar_sha256"] = diagnostic_frame_sha256(calendar_table)

    run.payload["model"] = model_name
    run.payload["dataset_sha256"] = dataset_sha256
    run.payload["universe_version"] = universe_version
    run.payload["backtest_stock_bars_sha256"] = backtest_stock_bars_sha256
    run.payload["backtest_benchmark_sha256"] = backtest_benchmark_sha256
    run.payload["execution_bars_sha256"] = execution_bars_sha256
    run.payload["hyperparameters"] = _model_hyperparameters(config, model_name)
    run.payload["walk_forward_folds"] = int(len(walk_forward.fold_summary))
    run.payload["out_of_sample_prediction_rows"] = int(
        len(walk_forward.predictions)
    )
    run.payload["prediction_metrics"] = {
        key: (None if pd.isna(value) else float(value))
        for key, value in overall.iloc[0].items()
        if key not in {"scope", "period"}
    }
    run.payload["portfolio_metrics"] = _json_metrics(portfolio_metrics)
    run.payload["execution_portfolio_metrics"] = _json_metrics(
        execution_metrics
    )
    run.payload["execution_rejections"] = int(len(execution.rejections))
    prediction_path = output_dir / "predictions.parquet"
    run.payload["predictions_sha256"] = _file_sha256(prediction_path)
    inventory = _artifact_sha256(output_dir)
    run.payload["model_artifact_sha256"] = inventory
    run.payload["stage_diagnostics_version"] = 1
    run.payload["stage_diagnostic_provenance"] = _stage_manifest_binding(
        walk_forward, output_dir, inventory, dataset_sha256, universe_version,
    )
    run.add_output("model_report", summary_path)
    run.add_output("predictions", prediction_path)
    run.add_output("walk_forward_results", output_dir / "walk_forward_results.csv")
    for name in _STAGE_TABLE_ATTRIBUTES:
        run.add_output(name, output_dir / (name + ".parquet"))
    run.add_output("evaluation_calendar", output_dir / "evaluation_calendar.parquet")
    run.add_output("nav", output_dir / "nav.csv")
    run.add_output("execution_nav", output_dir / "execution_nav.csv")
    run.add_output(
        "execution_rejections", output_dir / "execution_rejections.csv"
    )
    LOGGER.info(
        "%s completed: %d folds, %d OOS predictions",
        model_name,
        len(walk_forward.fold_summary),
        len(walk_forward.predictions),
    )


def _latest_model_output(config: ResearchConfig, model_name: str) -> Path:
    root = config.paths.model_results_dir / model_name
    required = (
        "predictions.parquet",
        "prediction_summary.csv",
        "portfolio_metrics.csv",
        "execution_nav.csv",
        "execution_portfolio_metrics.csv",
        "execution_rejections.csv",
        "walk_forward_results.csv",
        "summary.md",
    )
    candidates = []
    if root.exists():
        for path in root.iterdir():
            if not path.is_dir() or not all(
                (path / name).is_file() for name in required
            ):
                continue
            manifest_path = config.paths.runs_dir / path.name / "manifest.json"
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, ValueError, AttributeError):
                continue
            if manifest.get("status") != "success":
                continue
            if manifest.get("run_id") != path.name:
                continue
            if manifest.get("command") != "walk-forward":
                continue
            if manifest.get("model") != model_name:
                continue
            candidates.append(path)
    if not candidates:
        raise FactorLabError(
            "No completed %s report; run walk-forward --model %s first"
            % (model_name, model_name)
        )
    return sorted(candidates, key=lambda path: path.name)[-1]


def _comparison_provenance(
    payload: Dict[str, Any], description: str
) -> Dict[str, Any]:
    try:
        return validate_provenance(payload)
    except ValueError as exc:
        raise FactorLabError(
            "%s has missing or unknown provenance: %s" % (description, exc)
        ) from exc


def _prediction_oos_keys(
    model_name: str,
    output_dir: Path,
    manifest: Dict[str, Any],
    target_col: str,
    *,
    content: Optional[bytes] = None,
) -> pd.DataFrame:
    prediction_path = output_dir / "predictions.parquet"
    expected_sha256 = manifest.get("predictions_sha256")
    if (
        not isinstance(expected_sha256, str)
        or len(expected_sha256) != 64
        or any(character not in "0123456789abcdefABCDEF" for character in expected_sha256)
    ):
        raise FactorLabError(
            "%s run has missing or unknown predictions_sha256 provenance"
            % model_name
        )
    if content is None:
        try:
            content = prediction_path.read_bytes()
        except OSError as exc:
            raise FactorLabError("%s predictions.parquet is unreadable" % model_name) from exc
    actual_sha256 = hashlib.sha256(content).hexdigest()
    if expected_sha256.lower() != actual_sha256:
        raise FactorLabError(
            "%s predictions.parquet SHA-256 mismatch" % model_name
        )
    try:
        predictions = pd.read_parquet(io.BytesIO(content))
    except Exception as exc:
        raise FactorLabError(
            "%s predictions.parquet is unreadable" % model_name
        ) from exc
    key_columns = ["date", "symbol", "fold"]
    audit_columns = ["entry_date", "exit_date", target_col]
    comparison_columns = key_columns + audit_columns
    missing = [name for name in comparison_columns if name not in predictions]
    if missing:
        raise FactorLabError(
            "%s predictions are missing OOS identity/audit columns: %s"
            % (model_name, ", ".join(missing))
        )
    keys = predictions.loc[:, comparison_columns].copy()
    required_non_null = key_columns + ["entry_date", "exit_date"]
    if keys.empty or keys[required_non_null].isna().any().any():
        raise FactorLabError(
            "%s predictions contain empty or null OOS keys" % model_name
        )
    if keys["symbol"].map(lambda value: not str(value).strip()).any():
        raise FactorLabError("%s predictions contain blank symbols" % model_name)
    try:
        keys["date"] = pd.to_datetime(keys["date"], errors="raise").dt.normalize()
        keys["entry_date"] = pd.to_datetime(
            keys["entry_date"], errors="raise"
        ).dt.normalize()
        keys["exit_date"] = pd.to_datetime(
            keys["exit_date"], errors="raise"
        ).dt.normalize()
        keys[target_col] = pd.to_numeric(keys[target_col], errors="raise")
        keys.sort_values(key_columns, kind="mergesort", inplace=True)
    except (TypeError, ValueError) as exc:
        raise FactorLabError(
            "%s predictions contain invalid OOS keys" % model_name
        ) from exc
    if keys.duplicated(key_columns).any():
        raise FactorLabError(
            "%s predictions contain duplicate (date, symbol, fold) OOS keys"
            % model_name
        )
    invalid_windows = ~(
        (keys["date"] < keys["entry_date"])
        & (keys["entry_date"] < keys["exit_date"])
    )
    if invalid_windows.any():
        raise FactorLabError(
            "%s predictions contain invalid label audit windows" % model_name
        )
    return keys.reset_index(drop=True)


def _verify_model_artifacts(
    model_name: str,
    output_dir: Path,
    manifest: Dict[str, Any],
) -> Dict[str, bytes]:
    """Validate artifacts and pin report bytes for the comparison consumers.

    Large model binaries are hashed as streams, not retained in memory.
    CSV/Parquet reports are parsed from the exact verified byte snapshots.
    """

    expected = manifest.get("model_artifact_sha256")
    if not isinstance(expected, dict) or not expected:
        raise FactorLabError(
            "%s run has missing model artifact hash inventory" % model_name
        )
    required = {
        "predictions.parquet",
        "prediction_summary.csv",
        "portfolio_metrics.csv",
        "execution_nav.csv",
        "execution_portfolio_metrics.csv",
        "execution_rejections.csv",
        "walk_forward_results.csv",
        "summary.md",
    }
    if "stage_diagnostics_version" in manifest:
        version = manifest["stage_diagnostics_version"]
        evidence = manifest.get("stage_diagnostic_provenance")
        if (type(version) is not int or version != 1 or not isinstance(evidence, dict)
                or evidence.get("policy") != _stage_diagnostic_policy()):
            raise FactorLabError("%s has unknown stage diagnostic policy/version" % model_name)
        required |= _STAGE_ARTIFACTS | {"walk_forward_results.parquet"}
    elif _STAGE_ARTIFACTS.intersection(expected):
        raise FactorLabError("%s stage diagnostic artifacts lack producer provenance" % model_name)
    calendar_artifacts = {"evaluation_calendar.csv", "evaluation_calendar.parquet"}
    if "inference_input_version" in manifest:
        version = manifest["inference_input_version"]
        if type(version) is not int or version != 1 or not _is_sha256(manifest.get("inference_calendar_sha256")):
            raise FactorLabError("%s has unknown inference calendar policy/version" % model_name)
        required |= calendar_artifacts
    elif calendar_artifacts.intersection(expected) or "inference_calendar_sha256" in manifest:
        raise FactorLabError("%s inference calendar artifacts lack producer provenance" % model_name)
    if not required.issubset(expected):
        missing = sorted(required.difference(expected))
        raise FactorLabError(
            "%s artifact hash inventory is missing: %s"
            % (model_name, ", ".join(missing))
        )
    actual = {}
    snapshots = {}
    for path in sorted(output_dir.rglob("*")):
        if not path.is_file():
            continue
        if path.is_symlink() or output_dir.resolve() not in path.resolve().parents:
            raise FactorLabError("%s model artifact path escapes its root" % model_name)
        name = path.relative_to(output_dir).as_posix()
        # Train cohorts expand and overlap. The human-readable raw CSV can
        # be very large and is never parsed by comparison; hash it as a
        # stream. Its lossless Parquet is pinned only until stage validation.
        if path.suffix in {".csv", ".parquet"} and name != "stage_predictions.csv":
            content = path.read_bytes()
            snapshots[name] = content
            actual[name] = hashlib.sha256(content).hexdigest()
        else:
            actual[name] = _file_sha256(path)
    if set(actual) != set(expected):
        raise FactorLabError(
            "%s model artifact inventory differs from its manifest" % model_name
        )
    modified = [
        name
        for name in sorted(actual)
        if not _is_sha256(expected.get(name))
        or actual[name].lower() != str(expected[name]).lower()
    ]
    if modified:
        raise FactorLabError(
            "%s model artifact SHA-256 mismatch (%s)"
            % (model_name, ", ".join(modified))
        )
    return snapshots


def _verified_inference_calendar(
    model_name: str, manifest: Dict[str, Any], artifacts: Dict[str, bytes],
) -> Optional[pd.DatetimeIndex]:
    """Use only pinned report bytes. Never infer sessions or reopen market caches."""
    if "inference_input_version" not in manifest:
        return None
    try:
        if type(manifest["inference_input_version"]) is not int or manifest["inference_input_version"] != 1:
            raise ValueError("unknown version")
        table = pd.read_parquet(io.BytesIO(artifacts["evaluation_calendar.parquet"]))
        if list(table.columns) != ["date"] or table.empty:
            raise ValueError("calendar must contain exactly the date column")
        if not pd.api.types.is_datetime64_ns_dtype(table["date"].dtype):
            raise ValueError("calendar dates must be datetime64[ns]")
        dates = pd.DatetimeIndex(table["date"])
        if (dates.hasnans or dates.tz is not None or dates.has_duplicates
                or not dates.is_monotonic_increasing or not dates.equals(dates.normalize())):
            raise ValueError("calendar must contain unique increasing tz-naive trading dates")
        data_config = manifest.get("config", {}).get("data", {})
        if (dates.min() < pd.Timestamp(data_config["start"])
                or dates.max() > pd.Timestamp(data_config["end"])):
            raise ValueError("calendar lies outside the producer research window")
        if diagnostic_frame_sha256(table) != manifest.get("inference_calendar_sha256"):
            raise ValueError("calendar semantic SHA-256 differs from producer evidence")
        stored = pd.read_csv(io.BytesIO(artifacts["evaluation_calendar.csv"]), dtype=str, keep_default_na=False)
        canonical = pd.read_csv(io.BytesIO(table.to_csv(index=False).encode("utf-8")), dtype=str, keep_default_na=False)
        pd.testing.assert_frame_equal(stored, canonical, check_exact=True)
        return dates
    except Exception as exc:
        raise FactorLabError("%s inference calendar evidence is invalid: %s" % (model_name, exc)) from exc


def _inference_options(config: ResearchConfig) -> RobustnessOptions:
    return RobustnessOptions(
        **{key: value for key, value in asdict(config.inference).items() if key != "enabled"},
        holding_days=config.label.holding_days, random_seed=config.seed,
    )


def _verified_inference_daily(
    config: ResearchConfig, model_name: str, artifacts: Dict[str, bytes],
) -> pd.DataFrame:
    """Recompute metrics from fixed Test scores; do not refit/rescore/select."""
    try:
        predictions = pd.read_parquet(io.BytesIO(artifacts["predictions.parquet"]))
        evaluation = evaluate_predictions(
            predictions, target_col=_target_column(config),
            top_k=config.portfolio.top_k, minimum_cross_section=config.evaluation.min_cross_section,
            regime_col="MARKET_REGIME" if config.market_regime.enabled else None,
        )
        stages = pd.read_parquet(io.BytesIO(artifacts["stage_prediction_daily.parquet"]))
        stages = stages.loc[stages["stage"].eq("HISTORICAL_TEST")]
        columns = list(evaluation.daily.columns)
        actual = stages[columns].sort_values(["fold", "date"]).reset_index(drop=True)
        expected = evaluation.daily.sort_values(["fold", "date"]).reset_index(drop=True)
        pd.testing.assert_frame_equal(
            actual.astype(object).where(actual.notna(), None),
            expected.astype(object).where(expected.notna(), None), check_dtype=False, check_exact=True,
        )
        daily = evaluation.daily.copy()
        if not config.market_regime.enabled:
            daily = daily.drop(columns="regime")
        daily["model_name"] = model_name
        daily["stage"] = "HISTORICAL_TEST"
        return daily
    except Exception as exc:
        raise FactorLabError("%s inference Test metrics differ from verified scores: %s" % (model_name, exc)) from exc


def _verified_stage_cohort(
    model_name: str,
    output_dir: Path,
    manifest: Dict[str, Any],
    artifacts: Dict[str, bytes],
) -> Optional[pd.DataFrame]:
    """Validate stage diagnostics from already-verified bytes, not paths."""
    if "stage_diagnostics_version" not in manifest:
        return None
    try:
        tables = {
            name: pd.read_parquet(io.BytesIO(artifacts[name + ".parquet"]))
            for name in _STAGE_TABLE_ATTRIBUTES
        }
        result = WalkForwardResult(
            predictions=pd.read_parquet(io.BytesIO(artifacts["predictions.parquet"])),
            fold_summary=pd.read_parquet(io.BytesIO(artifacts["walk_forward_results.parquet"])),
            validation_metrics=pd.DataFrame(), coefficients=pd.DataFrame(), model_paths=(),
            **{attribute: tables[name] for name, attribute in _STAGE_TABLE_ATTRIBUTES.items()},
        )
        binding = _stage_manifest_binding(
            result, output_dir, manifest["model_artifact_sha256"],
            manifest.get("dataset_sha256"), manifest.get("universe_version"),
        )
        if binding != manifest.get("stage_diagnostic_provenance"):
            raise FactorLabError("%s stage diagnostics differ from producer provenance" % model_name)
        # These small CSVs are the human-readable views of the canonical
        # Parquets, not independent sources of metrics. Check their cells
        # even if somebody reseals the byte inventory after changing a CSV.
        # Raw expanding-Train CSV is streamed only and never parsed here.
        for name, table in tables.items():
            if name == "stage_predictions":
                continue
            stored_csv = pd.read_csv(
                io.BytesIO(artifacts[name + ".csv"]),
                dtype=str, keep_default_na=False,
            )
            canonical_csv = pd.read_csv(
                io.BytesIO(table.to_csv(index=False).encode("utf-8")),
                dtype=str, keep_default_na=False,
            )
            try:
                pd.testing.assert_frame_equal(stored_csv, canonical_csv, check_exact=True)
            except AssertionError as exc:
                raise FactorLabError(
                    "%s stage diagnostic CSV/Parquet values differ (%s)" % (model_name, name)
                ) from exc
        audit = tables["stage_prediction_audit"]
        if not audit["model_name"].eq(model_name).all():
            raise FactorLabError("Stage diagnostic model identity differs from its report")
        config = manifest.get("config", {})
        factors = config.get("factors")
        target = "EXCESS_RET%d" % config.get("label", {}).get("holding_days", 5)
        expected = {
            "random_seed": config.get("seed"),
            "top_k": config.get("portfolio", {}).get("top_k"),
            "minimum_cross_section": config.get("evaluation", {}).get("min_cross_section"),
            "target_column": target,
            # CLI does not offer a mutable selection metric. The fixed
            # default is part of its experiment protocol, not Test feedback.
            "selection_metric": "prediction_rank_ic_mean",
        }
        if any(value is None or not audit[name].eq(value).all() for name, value in expected.items()):
            raise FactorLabError("Stage diagnostic protocol differs from producer config")
        if any(json.loads(value) != factors for value in audit["feature_names"]):
            raise FactorLabError("Stage diagnostic features differ from producer config")
        # Scores and selected parameters legitimately differ by model; only
        # actual cohort/feature/label/fit-input identities must match.
        columns = [
            "fold", "stage", "source_keys_sha256", "source_sha256",
            "fit_source_keys_sha256", "fit_source_sha256",
            "signal_start", "signal_end", "label_min_entry", "label_max_entry",
            "label_min_exit", "label_max_exit", "maturity_boundary", "rows", "labeled_rows",
        ]
        return audit[columns].sort_values(["fold", "stage"], kind="mergesort").reset_index(drop=True)
    except FactorLabError:
        raise
    except Exception as exc:
        raise FactorLabError("%s stage diagnostic evidence is invalid: %s" % (model_name, exc)) from exc


def _comparison_record(model_name: str, output_dir: Path, *, artifacts: Optional[Dict[str, bytes]] = None) -> Dict[str, Any]:
    def read_csv(filename):
        return pd.read_csv(output_dir / filename if artifacts is None else io.BytesIO(artifacts[filename]))

    prediction = read_csv("prediction_summary.csv")
    overall = prediction.loc[
        prediction["scope"].astype(str).str.upper() == "OVERALL"
    ]
    if len(overall) != 1:
        raise FactorLabError("%s report has no unique OVERALL row" % model_name)
    metrics = read_csv("portfolio_metrics.csv")
    if not {"metric", "value"}.issubset(metrics.columns):
        raise FactorLabError("%s portfolio metrics are malformed" % model_name)
    record: Dict[str, Any] = {
        "model_name": model_name,
        "run_id": output_dir.name,
        "stage_diagnostics_available": (
            "stage_prediction_summary.parquet" in artifacts if artifacts is not None
            else (output_dir / "stage_prediction_summary.parquet").is_file()
        ),
    }
    record.update(
        {
            name: value
            for name, value in overall.iloc[0].items()
            if name not in {"scope", "period"}
        }
    )
    record.update(dict(zip(metrics["metric"], metrics["value"])))
    execution_metrics_path = output_dir / "execution_portfolio_metrics.csv"
    if (artifacts is None and not execution_metrics_path.is_file()) or (artifacts is not None and "execution_portfolio_metrics.csv" not in artifacts):
        raise FactorLabError(
            "%s execution portfolio metrics are missing" % model_name
        )
    execution_metrics = read_csv("execution_portfolio_metrics.csv")
    if not {"metric", "value"}.issubset(execution_metrics.columns):
        raise FactorLabError(
            "%s execution portfolio metrics are malformed" % model_name
        )
    record.update(
        {
            "execution_%s" % metric: value
            for metric, value in zip(
                execution_metrics["metric"], execution_metrics["value"]
            )
        }
    )
    return record


def _combine_model_tables(
    outputs: Dict[str, Path],
    filename: str,
    *,
    required: bool,
    artifact_snapshots: Optional[Dict[str, Dict[str, bytes]]] = None,
) -> pd.DataFrame:
    parts: List[pd.DataFrame] = []
    for model_name, output_dir in outputs.items():
        path = output_dir / filename
        snapshot = None if artifact_snapshots is None else artifact_snapshots[model_name]
        if (snapshot is None and not path.is_file()) or (snapshot is not None and filename not in snapshot):
            if required:
                raise FactorLabError(
                    "%s report is missing %s" % (model_name, filename)
                )
            continue
        try:
            reader = pd.read_parquet if path.suffix == ".parquet" else pd.read_csv
            table = reader(path if snapshot is None else io.BytesIO(snapshot[filename]))
        except Exception as exc:
            raise FactorLabError(
                "%s report has unreadable %s" % (model_name, filename)
            ) from exc
        if filename == "stage_prediction_audit.parquet":
            # Fixed-weight/tree models have an entirely missing alpha;
            # Ridge has floats. Give this optional numeric field the same
            # explicit type before concat, rather than relying on pandas'
            # deprecated all-NA inference. Never drop the audit column.
            table["selected_alpha"] = pd.to_numeric(
                table["selected_alpha"], errors="raise",
            ).astype("float64")
        table.insert(0, "source_run_id", output_dir.name)
        if "model_name" not in table:
            table.insert(1, "model_name", model_name)
        parts.append(table)
    if not parts:
        return pd.DataFrame(columns=["source_run_id", "model_name"])
    return pd.concat(parts, ignore_index=True, sort=False)


def _shared_model_security_provenance(
    manifests: Dict[str, Dict[str, Any]]
) -> Dict[str, Any]:
    """Return one exact security-data identity shared by all model runs."""

    sha_fields = (
        "security_data_version",
        "security_data_manifest_sha256",
        "security_status_sha256",
        "listing_dates_sha256",
    )
    text_fields = (
        "security_data_source",
        "security_data_source_version",
    )
    mapping_fields = (
        "security_data_input_sha256",
        "security_data_input_files",
    )
    result: Dict[str, Any] = {}
    for field in sha_fields:
        values = [payload.get(field) for payload in manifests.values()]
        if not values or any(value != values[0] for value in values):
            raise FactorLabError(
                "Model reports use different security-data identity (%s)" % field
            )
        if not _is_sha256(values[0]):
            raise FactorLabError(
                "Model reports have unknown security-data identity (%s)" % field
            )
        result[field] = values[0]
    for field in text_fields:
        values = [payload.get(field) for payload in manifests.values()]
        if (
            not values
            or any(value != values[0] for value in values)
            or not isinstance(values[0], str)
            or not values[0].strip()
        ):
            raise FactorLabError(
                "Model reports use different or unknown security-data source (%s)"
                % field
            )
        result[field] = values[0]
    for field in mapping_fields:
        values = [payload.get(field) for payload in manifests.values()]
        if (
            not values
            or any(value != values[0] for value in values)
            or not isinstance(values[0], dict)
        ):
            raise FactorLabError(
                "Model reports use different or unknown security-data inputs (%s)"
                % field
            )
        result[field] = dict(values[0])
    if any(
        not _is_sha256(value)
        for value in result["security_data_input_sha256"].values()
    ):
        raise FactorLabError("Model reports have invalid security-data input hashes")
    return result


def command_compare_models(config: ResearchConfig, run: RunManifest) -> None:
    """Compare all four pre-registered models without selecting a winner."""

    assert_no_unresolved(_download_guard_root(config))
    model_names = ("factor_score", "ridge", "lightgbm", "xgboost")
    outputs = {name: _latest_model_output(config, name) for name in model_names}
    manifests = {
        name: json.loads(
            (config.paths.runs_dir / path.name / "manifest.json").read_text(
                encoding="utf-8"
            )
        )
        for name, path in outputs.items()
    }
    current_provenance = _comparison_provenance(
        run.payload, "Current compare-models run"
    )
    for name, payload in manifests.items():
        source_provenance = _comparison_provenance(
            payload, "%s source run" % name
        )
        provenance_mismatches = [
            field
            for field in PROVENANCE_FIELDS
            if source_provenance[field] != current_provenance[field]
        ]
        if provenance_mismatches:
            raise FactorLabError(
                "%s source run provenance differs from the current environment (%s)"
                % (name, ", ".join(provenance_mismatches))
            )
    current_config = config.to_dict()
    if any(payload.get("config") != current_config for payload in manifests.values()):
        raise FactorLabError(
            "Model reports were not produced from the current identical config"
        )
    dataset_versions = {
        payload.get("dataset_sha256") for payload in manifests.values()
    }
    universe_versions = {
        payload.get("universe_version") for payload in manifests.values()
    }
    if (
        len(dataset_versions) != 1
        or not _is_sha256(next(iter(dataset_versions), None))
    ):
        raise FactorLabError("Model reports use different or unknown dataset versions")
    if (
        len(universe_versions) != 1
        or not _is_sha256(next(iter(universe_versions), None))
    ):
        raise FactorLabError("Model reports use different or unknown PIT universe versions")
    # Check shared input identities before interpreting the new stage
    # evidence. An unknown dataset must retain its original fail-fast error,
    # even when its report also includes diagnostic tables.
    artifact_snapshots = {}
    stage_cohorts = {}
    inference_calendars = {}
    oos_keys = {}
    for name, payload in manifests.items():
        artifact_snapshots[name] = _verify_model_artifacts(name, outputs[name], payload)
        oos_keys[name] = _prediction_oos_keys(
            name, outputs[name], payload, _target_column(config),
            content=artifact_snapshots[name]["predictions.parquet"],
        )
        if name != model_names[0] and not oos_keys[model_names[0]].equals(oos_keys[name]):
            raise FactorLabError(
                "Model reports do not use identical out-of-sample keys, label "
                "windows, and realised targets"
            )
        stage_cohorts[name] = _verified_stage_cohort(
            name, outputs[name], payload, artifact_snapshots[name],
        )
        inference_calendars[name] = _verified_inference_calendar(name, payload, artifact_snapshots[name])
        # Raw Train/Validation predictions stay in each source report. Do
        # not retain or concatenate millions of expanding-training rows.
        artifact_snapshots[name].pop("stage_predictions.parquet", None)
    stage_available = all(cohort is not None for cohort in stage_cohorts.values())
    if any(cohort is not None for cohort in stage_cohorts.values()) != stage_available:
        raise FactorLabError("Cannot mix legacy reports and complete stage diagnostics")
    if stage_available:
        reference_stage = stage_cohorts[model_names[0]]
        if any(not reference_stage.equals(stage_cohorts[name]) for name in model_names[1:]):
            raise FactorLabError("Model reports use different stage cohorts or fitted Train inputs")
    inference_calendar = None
    if config.inference.enabled:
        if not stage_available or any(value is None for value in inference_calendars.values()):
            raise FactorLabError("Inference requires complete stage diagnostics and verified benchmark calendars; rebuild model reports")
        inference_calendar = inference_calendars[model_names[0]]
        if any(not inference_calendar.equals(inference_calendars[name]) for name in model_names[1:]):
            raise FactorLabError("Model reports use different inference benchmark calendars")
    bar_versions = {payload.get("bar_data_version") for payload in manifests.values()}
    if len(bar_versions) != 1 or not _is_sha256(next(iter(bar_versions), None)):
        raise FactorLabError("Model reports use different or unknown raw bar-data versions")
    for name, payload in manifests.items():
        if bar_data_version(payload.get("bar_data_provenance")) != payload["bar_data_version"]:
            raise FactorLabError("%s model report has inconsistent bar-data evidence" % name)
    security_provenance = _shared_model_security_provenance(manifests)
    backtest_stock_versions = {
        payload.get("backtest_stock_bars_sha256") for payload in manifests.values()
    }
    backtest_benchmark_versions = {
        payload.get("backtest_benchmark_sha256") for payload in manifests.values()
    }
    if (
        len(backtest_stock_versions) != 1
        or not _is_sha256(next(iter(backtest_stock_versions), None))
    ):
        raise FactorLabError(
            "Model reports use different or unknown backtest stock-bar inputs"
        )
    if (
        len(backtest_benchmark_versions) != 1
        or not _is_sha256(next(iter(backtest_benchmark_versions), None))
    ):
        raise FactorLabError(
            "Model reports use different or unknown backtest benchmark inputs"
        )
    execution_bar_versions = {
        payload.get("execution_bars_sha256") for payload in manifests.values()
    }
    if (
        len(execution_bar_versions) != 1
        or not _is_sha256(next(iter(execution_bar_versions), None))
    ):
        raise FactorLabError(
            "Model reports use different or unknown execution-bar inputs"
        )
    # Historical successful manifests cannot authorize unqualified prices.
    benchmark = _load_benchmark_with_warmup(config)
    calendar = TradingCalendar.from_benchmark_bars(benchmark).dates
    _, admitted_dataset_sha, _ = _load_verified_dataset(
        config, _research_calendar(config, calendar), run,
        full_calendar=calendar, require_execution=True,
    )
    if dataset_versions != {admitted_dataset_sha}:
        raise FactorLabError("Model reports differ from the currently admitted dataset")
    comparison = pd.DataFrame(
        [_comparison_record(name, outputs[name], artifacts=artifact_snapshots[name]) for name in model_names]
    )
    comparison_dir = config.paths.report_dir / run.run_id
    combined_folds = _combine_model_tables(
        outputs, "walk_forward_results.csv", required=True,
        artifact_snapshots=artifact_snapshots,
    )
    detail_files = {
        "prediction_summary": ("prediction_summary.csv", True),
        "validation_results": ("validation_results.csv", True),
        "nav": ("nav.csv", True),
        "trades": ("trades.csv", True),
        "positions": ("positions.csv", True),
        "signals": ("signals.csv", True),
        "execution_nav": ("execution_nav.csv", True),
        "execution_trades": ("execution_trades.csv", True),
        "execution_positions": ("execution_positions.csv", True),
        "execution_rejections": ("execution_rejections.csv", True),
        "feature_importance": ("feature_importance.csv", False),
        "ridge_coefficients": ("ridge_coefficients.csv", False),
    }
    if stage_available:
        detail_files.update({
            name: (name + ".parquet", True)
            for name in _STAGE_TABLE_ATTRIBUTES if name != "stage_predictions"
        })
    details = {
        "walk_forward_results": combined_folds,
        **{
            output_name: _combine_model_tables(
                outputs, filename, required=required,
                artifact_snapshots=artifact_snapshots,
            )
            for output_name, (filename, required) in detail_files.items()
        },
    }
    robustness = None
    if config.inference.enabled:
        inference_daily = pd.concat([
            _verified_inference_daily(config, name, artifact_snapshots[name]) for name in model_names
        ], ignore_index=True)
        try:
            robustness = evaluate_model_robustness(inference_daily, inference_calendar, _inference_options(config))
        except RobustnessError as exc:
            raise FactorLabError("Historical Test inference: %s" % exc) from exc
    summary_path = write_model_comparison_report(
        comparison,
        comparison_dir,
        details=details,
        robustness=robustness,
    )
    fold_path = comparison_dir / "walk_forward_results.csv"
    run.payload["compared_models"] = list(model_names)
    run.payload["source_model_runs"] = {
        name: path.name for name, path in outputs.items()
    }
    run.payload["stage_diagnostics_available"] = stage_available
    run.payload["inference_available"] = robustness is not None
    if robustness is not None:
        run.payload["inference_protocol"] = robustness.protocol
        run.payload["inference_evidence"] = robustness.evidence
        run.payload["inference_calendar_sha256"] = manifests[model_names[0]]["inference_calendar_sha256"]
        run.payload["inference_table_sha256"] = {
            name: diagnostic_frame_sha256(table) for name, table in (
                ("inference_daily", robustness.daily), ("model_inference", robustness.summary),
                ("inference_audit", robustness.audit),
            )
        }
        for name in ("inference_daily", "model_inference", "inference_audit"):
            run.add_output(name, comparison_dir / (name + ".parquet"))
    if stage_available:
        run.payload["stage_diagnostics_version"] = 1
        run.payload["stage_diagnostic_policy"] = _stage_diagnostic_policy()
        run.payload["source_stage_diagnostic_provenance"] = {
            name: manifests[name]["stage_diagnostic_provenance"] for name in model_names
        }
    run.payload["source_predictions_sha256"] = {
        name: manifests[name]["predictions_sha256"] for name in model_names
    }
    run.payload["source_model_artifact_sha256"] = {
        name: manifests[name]["model_artifact_sha256"] for name in model_names
    }
    run.payload["dataset_sha256"] = next(iter(dataset_versions))
    run.payload["universe_version"] = next(iter(universe_versions))
    run.payload["bar_data_version"] = next(iter(bar_versions))
    run.payload["bar_data_provenance"] = manifests[model_names[0]]["bar_data_provenance"]
    run.payload["source_bar_data_provenance"] = {
        name: payload["bar_data_provenance"] for name, payload in manifests.items()
    }
    run.payload.update(security_provenance)
    run.payload["backtest_stock_bars_sha256"] = next(
        iter(backtest_stock_versions)
    )
    run.payload["backtest_benchmark_sha256"] = next(
        iter(backtest_benchmark_versions)
    )
    run.payload["execution_bars_sha256"] = next(iter(execution_bar_versions))
    run.payload["comparison_artifact_sha256"] = _artifact_sha256(comparison_dir)
    run.add_output(
        "model_comparison", comparison_dir / "model_comparison.csv"
    )
    run.add_output("walk_forward_results", fold_path)
    run.add_output("summary", summary_path)
    LOGGER.info(
        "Compared FactorScore, Ridge, LightGBM, and XGBoost without "
        "automatic model selection"
    )


def _forward_runtime_config(
    config: ResearchConfig, signal_date: date
) -> ResearchConfig:
    """Pin a daily forward run to exactly its requested information date."""

    if signal_date < config.data.start:
        raise FactorLabError("forward signal date is before data.start")
    runtime = replace(config, data=replace(config.data, end=signal_date))
    object.__setattr__(runtime, "config_path", config.config_path)
    object.__setattr__(runtime, "repo_root", config.repo_root)
    runtime.validate()
    return runtime


def _forward_now() -> datetime:
    """Internal clock seam for pre-activation freeze enforcement."""

    return datetime.now(timezone.utc)


def _require_pre_activation_freeze(config: ResearchConfig) -> None:
    activation = config.forward.activation_date
    if activation is None:
        raise FactorLabError("forward.activation_date is required")
    now = _forward_now()
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise FactorLabError("internal forward clock must be timezone-aware")
    if now.astimezone(ZoneInfo("Asia/Shanghai")).date() >= activation:
        raise FactorLabError(
            "Forward model must be frozen strictly before its activation date"
        )


def _forward_identity(
    config: ResearchConfig, run: RunManifest
) -> tuple:
    if not config.forward.enabled:
        raise FactorLabError(
            "Forward Test is disabled; pre-register a future activation in config"
        )
    if config.live_trading:
        raise FactorLabError("Forward Test is paper-only; live_trading must be false")
    try:
        provenance = validate_provenance(run.payload)
    except ValueError as exc:
        raise FactorLabError("Forward run provenance is incomplete: %s" % exc) from exc
    if config.forward.require_clean_worktree and provenance["git_dirty"]:
        raise FactorLabError(
            "Forward Test requires a clean committed worktree before publication"
        )
    _assert_current_forward_worktree(config, provenance)
    if config.forward.activation_date is None:
        raise FactorLabError("forward.activation_date is required")
    contract = forward_contract_sha256(config)
    identifier = activation_id(config.forward.activation_date, contract)
    from factorlab._identity import package_root
    context = config.repo_root or Path.cwd()
    if (context / "src/factorlab").resolve() != package_root():
        raise FactorLabError("Forward source root does not match the executing FactorLab package")
    source = research_source_sha256(context)
    return provenance, contract, identifier, source


def _assert_current_forward_worktree(
    config: ResearchConfig, expected_provenance: Dict[str, Any]
) -> None:
    """Recheck code identity immediately before publishing forward evidence."""

    root = config.repo_root or Path.cwd()
    current_commit = git_commit(root)
    current_state = git_worktree_state(root)
    if current_commit != expected_provenance["git_commit"]:
        raise FactorLabError(
            "Git commit changed during the forward run; no evidence was published"
        )
    if current_state.get("git_dirty") is not False:
        raise FactorLabError(
            "Git worktree changed during the forward run; no evidence was published"
        )
    if current_state.get("git_diff_sha256") is not None:
        raise FactorLabError("Clean forward worktree must not have a diff hash")


def _registered_forward_context(
    config: ResearchConfig, run: RunManifest
) -> tuple:
    provenance, contract, identifier, source = _forward_identity(config, run)
    activation = load_forward_activation(
        config.paths.forward_dir,
        identifier,
        expected_contract_sha256=contract,
        expected_source_sha256=source,
    )
    if activation.activation_date != config.forward.activation_date:
        raise FactorLabError("Forward activation date differs from current config")
    return provenance, contract, activation, source


def _load_registered_forward_bundle(config, activation, contract, source):
    """Verify the immutable pre-activation model, never fit or rescore here."""

    model_version = config.forward.model_version
    if model_version is None:
        raise FactorLabError("forward.model_version is required")
    bundle = load_model_bundle(
        config.paths.forward_models_dir / config.forward.model_name / model_version,
        expected_model_name=config.forward.model_name,
        expected_feature_names=config.factors,
        expected_source_sha256=source,
    )
    expected = {
        "model_version": model_version,
        "target": _target_column(config),
        "training_asof": config.forward.training_asof.isoformat()
        if config.forward.training_asof is not None else None,
        "research_contract_sha256": contract,
        "source_sha256": source,
        "seed": config.seed,
        "parameters": selected_forward_model_parameters(config),
    }
    mismatches = [key for key, value in expected.items() if bundle.metadata.get(key) != value]
    if mismatches:
        raise FactorLabError(
            "Forward model bundle differs from the registered contract (%s)"
            % ", ".join(mismatches)
        )
    try:
        frozen_at = datetime.strptime(
            bundle.metadata["frozen_at"], "%Y-%m-%dT%H:%M:%S.%fZ"
        ).replace(tzinfo=timezone.utc)
    except (KeyError, TypeError, ValueError) as exc:
        raise FactorLabError("Forward model bundle has an invalid frozen_at") from exc
    if frozen_at.astimezone(ZoneInfo(config.forward.timezone)).date() >= activation.activation_date:
        raise FactorLabError("Forward model bundle was not frozen before the activation date")
    return bundle


def command_activate_forward(
    config: ResearchConfig,
    run: RunManifest,
    requested_date: date,
) -> None:
    """Create an immutable receipt before a genuine future experiment starts."""

    provenance, contract, identifier, source = _forward_identity(config, run)
    if requested_date != config.forward.activation_date:
        raise FactorLabError(
            "--date must exactly match forward.activation_date in the reviewed config"
        )
    _assert_current_forward_worktree(config, provenance)
    receipt = register_forward_activation(
        config.paths.forward_dir,
        activation_date=requested_date,
        contract_sha256=contract,
        source_sha256=source,
        git_commit=provenance["git_commit"],
        config_path=config.config_path or "<in-memory-config>",
        timezone_name=config.forward.timezone,
    )
    if receipt.activation_id != identifier:
        raise FactorLabError("Forward activation receipt identity is inconsistent")
    run.payload["activation_id"] = receipt.activation_id
    run.payload["activation_manifest_sha256"] = receipt.manifest_sha256
    run.payload["research_contract_sha256"] = contract
    run.payload["source_sha256"] = source
    run.add_output("forward_activation", receipt.path / "manifest.json")
    LOGGER.info(
        "Registered paper-only Forward Test activation %s for %s",
        receipt.activation_id,
        requested_date,
    )


def command_freeze_forward_model(
    config: ResearchConfig,
    run: RunManifest,
) -> None:
    """Fit one pre-specified model using only labels mature by training_asof."""

    provenance, contract, activation, source = _registered_forward_context(config, run)
    _require_pre_activation_freeze(config)
    training_asof = config.forward.training_asof
    model_version = config.forward.model_version
    if training_asof is None or model_version is None:
        raise FactorLabError("Forward model freeze settings are incomplete")
    if config.data.end != training_asof:
        raise FactorLabError(
            "freeze-forward-model requires data.end to equal forward.training_asof"
        )

    benchmark = _load_benchmark_with_warmup(config)
    full_calendar = TradingCalendar.from_benchmark_bars(benchmark).dates
    research_calendar = _research_calendar(config, full_calendar)
    dataset, dataset_sha256, universe_version = _load_verified_dataset(
        config, research_calendar, run, full_calendar=full_calendar, require_execution=True
    )
    parameters = selected_forward_model_parameters(config)
    fitted = fit_final_model(
        dataset,
        model_name=config.forward.model_name,
        params=parameters,
        feature_names=config.factors,
        target=_target_column(config),
        training_asof=training_asof,
        seed=config.seed,
    )
    # Fitting can take minutes for tree models.  Recheck time, Git state, and
    # executable source immediately before the immutable directory appears.
    _require_pre_activation_freeze(config)
    _assert_current_forward_worktree(config, provenance)
    if research_source_sha256(config.repo_root or Path.cwd()) != source:
        raise FactorLabError(
            "FactorLab source changed during model fitting; no bundle was published"
        )
    bundle = save_model_bundle(
        fitted,
        config.paths.forward_models_dir,
        source_dataset_sha256=dataset_sha256,
        source_sha256=source,
        universe_version=universe_version,
        research_contract_sha256=contract,
        git_commit=provenance["git_commit"],
        model_version=model_version,
    )
    run.payload.update(
        {
            "activation_id": activation.activation_id,
            "activation_manifest_sha256": activation.manifest_sha256,
            "research_contract_sha256": contract,
            "source_sha256": source,
            "model": config.forward.model_name,
            "model_version": model_version,
            "model_parameters": parameters,
            "model_bundle_sha256": bundle.metadata["content_sha256"],
            "dataset_sha256": dataset_sha256,
            "universe_version": universe_version,
            "training_rows": bundle.metadata["training_rows"],
            "max_label_exit_date": bundle.metadata["max_label_exit_date"],
            "frozen_at": bundle.metadata["frozen_at"],
        }
    )
    run.add_output("forward_model_bundle", bundle.path)
    LOGGER.info(
        "Frozen %s model %s on %d mature observations",
        bundle.model_name,
        bundle.model_version,
        bundle.metadata["training_rows"],
    )


def _run_forward_child(
    config: ResearchConfig,
    parent: RunManifest,
    command: str,
    operation: Any,
    *,
    require_no_failures: bool = False,
) -> RunManifest:
    """Run one existing pipeline stage with its own truthful producer manifest."""

    child = RunManifest(
        config,
        command,
        {"parent_run_id": parent.run_id, "forward_refresh": True},
    )
    try:
        operation(config, child)
        if require_no_failures:
            failures = child.payload.get("failures", {})
            nonempty = sorted(
                name
                for name, values in failures.items()
                if values not in (None, {}, [], ())
            )
            if nonempty:
                raise FactorLabError(
                    "Forward refresh is incomplete (%s); no signal may be published"
                    % ", ".join(nonempty)
                )
        child.finish("success")
    except Exception as exc:
        try:
            child.finish("failed", "%s: %s" % (type(exc).__name__, exc))
        except Exception:
            LOGGER.exception("Cannot finalise failed %s child manifest", command)
        raise
    parent.payload.setdefault("child_run_ids", {})[command] = child.run_id
    return child


def command_forward_signal(
    config: ResearchConfig,
    run: RunManifest,
    signal_date: date,
    *,
    reissue_reason: Optional[str] = None,
    version: Optional[str] = None,
    generated_at: Optional[datetime] = None,
    refresh_data: bool = True,
) -> None:
    """Serialize the full refresh/build/score/publish transaction."""

    lock_path = config.paths.data_root / ".locks" / "forward-workflow.lock"
    with workflow_lock(lock_path, timeout_seconds=0.0):
        _command_forward_signal_locked(
            config,
            run,
            signal_date,
            reissue_reason=reissue_reason,
            version=version,
            generated_at=generated_at,
            refresh_data=refresh_data,
        )


def _command_forward_signal_locked(
    config: ResearchConfig,
    run: RunManifest,
    signal_date: date,
    *,
    reissue_reason: Optional[str] = None,
    version: Optional[str] = None,
    generated_at: Optional[datetime] = None,
    refresh_data: bool = True,
) -> None:
    """Refresh data, score one close-known cross-section, and append a signal."""

    provenance, contract, activation, source = _registered_forward_context(config, run)
    if config.data.end != signal_date:
        raise FactorLabError("Forward runtime data.end must equal --date")
    preflight_timing = classify_forward_timing(
        signal_date,
        activation_date=activation.activation_date,
        generated_at=generated_at,
        timezone_name=config.forward.timezone,
        market_close_hour=config.forward.market_close_hour,
        market_close_minute=config.forward.market_close_minute,
        reissue_reason=reissue_reason,
    )

    model_version = config.forward.model_version
    if model_version is None:
        raise FactorLabError("forward.model_version is required")
    bundle = _load_registered_forward_bundle(config, activation, contract, source)

    if refresh_data:
        _run_forward_child(
            config,
            run,
            "download-data",
            command_download,
            require_no_failures=True,
        )
        _run_forward_child(config, run, "build-dataset", command_build_dataset)

    benchmark = _load_benchmark_with_warmup(config)
    full_calendar = TradingCalendar.from_benchmark_bars(benchmark).dates
    research_calendar = _research_calendar(config, full_calendar)
    normalized_day = pd.Timestamp(signal_date)
    if normalized_day not in research_calendar:
        raise FactorLabError("--date is not a CSI500 benchmark trading session")
    if research_calendar.max() != normalized_day:
        raise FactorLabError(
            "Benchmark data_asof does not reach exactly the requested signal date"
        )
    dataset, dataset_sha256, universe_version = _load_verified_dataset(
        config, research_calendar, run, full_calendar=full_calendar, require_execution=True
    )
    cross_section = dataset.loc[dataset["date"].eq(normalized_day)].copy()
    if cross_section.empty:
        raise FactorLabError("Dataset contains no eligible rows on the signal date")
    required_coverage = max(
        config.evaluation.min_cross_section,
        int(
            math.ceil(
                config.universe.expected_member_count
                * config.universe.min_valid_ohlcv_ratio
            )
        ),
    )
    if len(cross_section) < required_coverage:
        raise FactorLabError(
            "Signal-day eligible cross-section has only %d rows; at least %d are "
            "required by the PIT/data-quality coverage contract"
            % (len(cross_section), required_coverage)
        )
    finite_features = cross_section.loc[:, list(config.factors)].apply(
        pd.to_numeric, errors="coerce"
    )
    invalid_rows = ~finite_features.apply(lambda column: column.map(math.isfinite)).all(axis=1)
    if invalid_rows.any():
        invalid_symbols = cross_section.loc[invalid_rows, "symbol"].astype(str).tolist()
        raise FactorLabError(
            "Signal-day factors are incomplete for %d eligible securities; "
            "no model-side imputation is allowed (%s)"
            % (len(invalid_symbols), ", ".join(invalid_symbols[:10]))
        )
    scores = bundle.predict(finite_features)
    predictions = pd.DataFrame(
        {
            "date": normalized_day,
            "symbol": cross_section["symbol"].to_numpy(),
            "score": scores.to_numpy(dtype=float),
        }
    )
    if not predictions["score"].map(math.isfinite).all():
        raise FactorLabError(
            "Signal-day model predictions are non-finite; the complete eligible "
            "cross-section must be recorded without silently dropping securities"
        )
    predictions = predictions.sort_values(
        ["score", "symbol"], ascending=[False, True], kind="mergesort"
    ).reset_index(drop=True)
    predictions["rank"] = range(1, len(predictions) + 1)
    ranked = select_top_k(
        predictions,
        top_k=config.portfolio.top_k,
        rebalance_every=1,
        trading_dates=[normalized_day],
        minimum_cross_section=config.evaluation.min_cross_section,
    )
    signal_values = ranked[["symbol", "score", "rank", "target_weight"]]
    # When the CLI uses the real clock, record publication time after the
    # potentially long network/build stages.  If midnight was crossed the
    # run must be reclassified (and cannot silently remain ON_TIME).
    timing = (
        preflight_timing
        if generated_at is not None
        else classify_forward_timing(
            signal_date,
            activation_date=activation.activation_date,
            timezone_name=config.forward.timezone,
            market_close_hour=config.forward.market_close_hour,
            market_close_minute=config.forward.market_close_minute,
            reissue_reason=reissue_reason,
        )
    )
    _assert_current_forward_worktree(config, provenance)
    if research_source_sha256(config.repo_root or Path.cwd()) != source:
        raise FactorLabError(
            "FactorLab source changed during the forward workflow; no signal was published"
        )
    record = record_signals(
        signal_values,
        config.paths.forward_dir / "signals",
        predictions=predictions[["symbol", "score", "rank"]],
        cross_section_size=len(cross_section),
        activation_date=activation.activation_date,
        signal_date=signal_date,
        data_asof=signal_date,
        generated_at=timing.generated_at,
        model_name=config.forward.model_name,
        model_version=model_version,
        model_bundle_sha256=bundle.metadata["content_sha256"],
        dataset_sha256=dataset_sha256,
        git_commit=provenance["git_commit"],
        run_id=run.run_id,
        universe_version=universe_version,
        activation_id=activation.activation_id,
        activation_manifest_sha256=activation.manifest_sha256,
        research_contract_sha256=contract,
        source_sha256=source,
        timeliness=timing.timeliness,
        reissue_reason=timing.reissue_reason,
        version=version,
    )
    run.payload.update(
        {
            "activation_id": activation.activation_id,
            "activation_manifest_sha256": activation.manifest_sha256,
            "research_contract_sha256": contract,
            "source_sha256": source,
            "model": config.forward.model_name,
            "model_version": model_version,
            "model_bundle_sha256": bundle.metadata["content_sha256"],
            "model_training_dataset_sha256": bundle.metadata[
                "source_dataset_sha256"
            ],
            "dataset_sha256": dataset_sha256,
            "universe_version": universe_version,
            "signal_date": signal_date.isoformat(),
            "data_asof": signal_date.isoformat(),
            "timeliness": timing.timeliness,
            "signal_version": record.version,
            "signal_sha256": record.sha256,
            "signal_manifest_sha256": record.manifest_sha256,
            "signal_rows": record.row_count,
            "prediction_sha256": record.predictions_sha256,
            "prediction_rows": record.prediction_row_count,
        }
    )
    run.add_output("forward_signals", record.signals_path)
    run.add_output("forward_signal_manifest", record.manifest_path)
    run.add_output("forward_predictions", record.predictions_path)
    LOGGER.info(
        "Published paper-only %s signal %s/%s with %d holdings and %d predictions",
        timing.timeliness,
        signal_date,
        record.version,
        record.row_count,
        record.prediction_row_count,
    )


def _snapshot_covers(snapshot, start: date, end: date, symbol: str) -> None:
    """Check requested coverage from the *same* verified cache snapshot."""

    if not snapshot.metadata:
        raise FactorLabError("Forward evaluation has no verified cache for %s" % symbol)
    cursor = pd.Timestamp(start)
    stop = pd.Timestamp(end)
    intervals = sorted(
        (pd.Timestamp(item["start"]), pd.Timestamp(item["end"]))
        for item in snapshot.metadata.get("covered_ranges", [])
    )
    for left, right in intervals:
        if right < cursor:
            continue
        if left > cursor:
            break
        cursor = max(cursor, right + pd.Timedelta(days=1))
        if cursor > stop:
            return
    raise FactorLabError(
        "Forward evaluation cache coverage is incomplete for %s (%s through %s); "
        "update data explicitly before evaluation, no automatic refill is allowed"
        % (symbol, start.isoformat(), end.isoformat())
    )


def _admit_forward_evaluation(config, provider, benchmark, bars, calendar, first, as_of, symbols):
    """Realised labels need financial qualification as well as cache coverage."""
    dates = _research_calendar(config, calendar)
    membership = _load_verified_membership(config, dates)
    status, listings, _ = load_verified_security_data(
        config.paths.universe_dir, calendar=calendar, membership=membership,
        universe_sha256=dataframe_sha256(membership),
        expected_provider=config.universe.status_provider,
    )
    return assert_formal_market_admission(
        provider, membership=membership, calendar=calendar, bars=bars, benchmark=benchmark,
        research_start=config.data.start, research_end=as_of, status=status, listings=listings,
        require_execution=True, required_symbols=symbols,
    )


def command_evaluate_forward(
    config: ResearchConfig,
    run: RunManifest,
    as_of: date,
    *,
    start: Optional[date] = None,
    end: Optional[date] = None,
) -> None:
    """Append mature-label diagnostics for original complete predictions only."""

    from factorlab.forward.evidence import select_forward_prediction_records
    from factorlab.forward.evaluation import evaluate_forward_predictions
    from factorlab.reports.forward import write_forward_evaluation_report

    if type(as_of) is not date:
        raise FactorLabError("Forward evaluation --as-of must be a canonical date")
    now = _forward_now()
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise FactorLabError("internal forward clock must be timezone-aware")
    now = now.astimezone(ZoneInfo(config.forward.timezone))
    if as_of > now.date():
        raise FactorLabError("Forward evaluation --as-of cannot be in the future")
    if as_of == now.date() and (now.hour, now.minute) < (
        config.forward.market_close_hour, config.forward.market_close_minute
    ):
        raise FactorLabError("Today's forward evaluation must wait until 15:30 Asia/Shanghai")
    lock_path = config.paths.data_root / ".locks" / "forward-workflow.lock"
    with workflow_lock(lock_path, timeout_seconds=0.0):
        provenance, contract, activation, source = _registered_forward_context(config, run)
        bundle = _load_registered_forward_bundle(config, activation, contract, source)
        first = activation.activation_date if start is None else start
        last = as_of if end is None else end
        if type(first) is not date or type(last) is not date:
            raise FactorLabError("Forward evaluation range must contain canonical dates")
        if first < activation.activation_date or first > last or last > as_of:
            raise FactorLabError("Forward evaluation range must lie between activation_date and --as-of")
        benchmark_snapshot = _load_benchmark_snapshot(config)
        _snapshot_covers(benchmark_snapshot, first, as_of, config.data.benchmark)
        benchmark = benchmark_snapshot.frame.loc[
            benchmark_snapshot.frame["date"].le(pd.Timestamp(as_of))
        ].copy()
        calendar = TradingCalendar.from_benchmark_bars(benchmark).dates
        if pd.Timestamp(as_of) not in calendar:
            raise FactorLabError("Forward evaluation --as-of is not an observed benchmark session")
        evidence = select_forward_prediction_records(
            config.paths.forward_dir / "signals",
            start=first,
            end=last,
            activation_id=activation.activation_id,
            activation_manifest_sha256=activation.manifest_sha256,
            research_contract_sha256=contract,
            source_sha256=source,
            model_name=config.forward.model_name,
            model_version=config.forward.model_version,
            model_bundle_sha256=bundle.metadata["content_sha256"],
            market_close_hour=config.forward.market_close_hour,
            market_close_minute=config.forward.market_close_minute,
            version_policy=config.forward.evaluation_version_policy,
        )
        predictions = evidence.predictions
        signal_dates = pd.DatetimeIndex(pd.to_datetime(predictions["signal_date"].unique()))
        if not signal_dates.isin(calendar).all():
            raise FactorLabError("A recorded signal date is not an observed benchmark session")
        symbols = sorted(predictions["symbol"].unique())
        provider = _provider(config)
        provider_name = provider.__class__.__name__
        cache = _stock_cache(config)
        identities = {
            symbol: (provider_name, provider.cache_source_version(symbol, False))
            for symbol in symbols
        }
        snapshots = {
            symbol: cache.load_snapshot(
                symbol, expected_source=identities[symbol][0],
                expected_source_version=identities[symbol][1],
            )
            for symbol in symbols
        }
        for symbol, snapshot in snapshots.items():
            symbol_start = pd.Timestamp(
                predictions.loc[predictions["symbol"].eq(symbol), "signal_date"].min()
            ).date()
            _snapshot_covers(snapshot, symbol_start, as_of, symbol)
        bar_provenance = build_bar_data_provenance(
            config.data.benchmark, benchmark_snapshot, snapshots, identities,
            (provider_name, provider.cache_source_version(config.data.benchmark, True)),
        )
        evaluation_bars = pd.concat([snapshot.frame for snapshot in snapshots.values()], ignore_index=True)
        admission = _admit_forward_evaluation(
            config, provider, benchmark, evaluation_bars, calendar, first, as_of, symbols,
        )
        evaluation = evaluate_forward_predictions(
            predictions,
            evaluation_bars,
            benchmark,
            as_of=as_of,
            holding_days=config.label.holding_days,
            top_k=config.portfolio.top_k,
            minimum_cross_section=config.evaluation.min_cross_section,
            benchmark_symbol=config.data.benchmark,
            include_regime=config.market_regime.enabled,
        )
        expected_dates = calendar[(calendar >= pd.Timestamp(first)) & (calendar <= pd.Timestamp(last))]
        missing_dates = expected_dates.difference(signal_dates)
        report_provenance = {
            **provenance,
            "market_admission": admission,
            "source_sha256": source,
            "research_contract_sha256": contract,
            "activation_id": activation.activation_id,
            "activation_manifest_sha256": activation.manifest_sha256,
            "model_name": config.forward.model_name,
            "model_version": config.forward.model_version,
            "model_bundle_sha256": bundle.metadata["content_sha256"],
            "config": config.to_dict(),
            "bar_data_version": bar_data_version(bar_provenance),
            "bar_data_provenance": bar_provenance,
            "signal_start": first.isoformat(),
            "signal_end": last.isoformat(),
            "as_of": as_of.isoformat(),
            "expected_signal_dates": expected_dates.strftime("%Y-%m-%d").tolist(),
            "missing_signal_dates": missing_dates.strftime("%Y-%m-%d").tolist(),
            "selected_rows": len(predictions),
            "version_policy": config.forward.evaluation_version_policy,
        }
        _assert_current_forward_worktree(config, provenance)
        if research_source_sha256(config.repo_root or Path.cwd()) != source:
            raise FactorLabError("FactorLab source changed during forward evaluation")
        report = write_forward_evaluation_report(
            evaluation, evidence,
            config.paths.forward_dir / "evaluations" / run.run_id,
            run_id=run.run_id, provenance=report_provenance,
        )
        # Plotting/publication can take time. A changed worktree leaves an
        # unfinalized report, never evidence authorized by a successful run.
        _assert_current_forward_worktree(config, provenance)
        if research_source_sha256(config.repo_root or Path.cwd()) != source:
            raise FactorLabError("FactorLab source changed during forward report publication")
        run.payload.update({
            "forward_evaluation_manifest_sha256": report.manifest_sha256,
            "forward_evaluation_artifact_sha256": dict(report.artifact_sha256),
            "forward_evaluation": report_provenance,
        })
        run.add_output("forward_evaluation_report", report.directory)
        run.add_output("forward_evaluation_manifest", report.manifest_path)
        LOGGER.info(
            "Recorded forward evaluation: %d original predictions, %d missing signal dates; %s",
            len(predictions), len(missing_dates), report.directory,
        )


def main(argv: Optional[List[str]] = None) -> int:
    parser = _parser()
    arguments = parser.parse_args(argv)
    if arguments.command == "demo":
        from factorlab.demo import run
        try:
            result = run(arguments.output, seed=arguments.seed)
        except (OSError, ValueError, FactorLabError) as exc:
            print(str(exc), file=sys.stderr)
            return 1
        print(result["warning"])
        print(Path(arguments.output) / "manifest.json")
        return 0
    logging.basicConfig(
        level=getattr(logging, arguments.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        config = load_config(arguments.config)
        if arguments.command == "forward-signal":
            config = _forward_runtime_config(config, arguments.date)
        elif arguments.command == "evaluate-forward":
            config = _forward_runtime_config(config, arguments.as_of)
    except FactorLabError as exc:
        LOGGER.error("%s", exc)
        return 2
    try:
        set_random_seed(config.seed)
        run = RunManifest(config, arguments.command, _manifest_arguments(arguments))
    except Exception as exc:
        LOGGER.error(
            "Cannot initialise reproducible run manifest: %s: %s",
            type(exc).__name__,
            exc,
        )
        return 2
    LOGGER.info("Run ID: %s", run.run_id)
    try:
        if arguments.command in {
            "build-dataset", "evaluate-factors", "walk-forward", "compare-models",
            "activate-forward", "freeze-forward-model", "forward-signal",
            "evaluate-forward",
        }:
            assert_no_unresolved(_download_guard_root(config))
        if arguments.command == "audit":
            command_audit(config, run)
        elif arguments.command == "download-data":
            command_download(config, run)
        elif arguments.command == "import-universe":
            command_import_universe(config, run, arguments.input_path)
        elif arguments.command == "import-security-data":
            command_import_security_data(
                config,
                run,
                arguments.status_path,
                arguments.listings_path,
            )
        elif arguments.command == "import-market-data":
            import_args = {}
            if arguments.qualification_manifest is not None:
                import_args["qualification_manifest"] = arguments.qualification_manifest
            command_import_market_data(config, run, arguments.stock_path, arguments.benchmark_path,
                                       arguments.source, arguments.source_version, **import_args)
        elif arguments.command == "build-dataset":
            command_build_dataset(config, run)
        elif arguments.command == "evaluate-factors":
            command_evaluate(config, run)
        elif arguments.command == "walk-forward":
            command_walk_forward(config, run, arguments.model)
        elif arguments.command == "compare-models":
            command_compare_models(config, run)
        elif arguments.command == "activate-forward":
            command_activate_forward(config, run, arguments.date)
        elif arguments.command == "freeze-forward-model":
            command_freeze_forward_model(config, run)
        elif arguments.command == "forward-signal":
            command_forward_signal(
                config,
                run,
                arguments.date,
                reissue_reason=arguments.reissue_reason,
                version=arguments.version,
            )
        elif arguments.command == "evaluate-forward":
            command_evaluate_forward(
                config, run, arguments.as_of, start=arguments.start, end=arguments.end
            )
        else:  # argparse prevents this branch
            raise FactorLabError("Unsupported command %s" % arguments.command)
    except (FactorLabError, OSError, ValueError) as exc:
        try:
            run.finish("failed", str(exc))
        except Exception as manifest_exc:
            LOGGER.error(
                "Cannot finalise failed run manifest (original error: %s): %s: %s",
                exc,
                type(manifest_exc).__name__,
                manifest_exc,
            )
        LOGGER.error("%s", exc)
        return 1
    except Exception as exc:
        try:
            run.finish("failed", "%s: %s" % (type(exc).__name__, exc))
        except Exception as manifest_exc:
            LOGGER.error(
                "Cannot finalise failed run manifest (original error: %s: %s): %s: %s",
                type(exc).__name__,
                exc,
                type(manifest_exc).__name__,
                manifest_exc,
            )
        LOGGER.exception("Unexpected failure")
        return 1
    try:
        run.finish("success")
        if arguments.command == "forward-signal":
            signals_path = run.payload.get("outputs", {}).get("forward_signals")
            if not isinstance(signals_path, str) or not signals_path:
                raise FactorLabError(
                    "Forward signal run has no published signal output to finalize"
                )
            finalized = finalize_signal_record(Path(signals_path).parent, run.path)
            LOGGER.info(
                "Finalized append-only forward signal receipt: %s",
                finalized.completion_path,
            )
        elif arguments.command == "evaluate-forward":
            from factorlab.reports.forward import finalize_forward_evaluation_report

            directory = run.payload.get("outputs", {}).get("forward_evaluation_report")
            if not isinstance(directory, str) or not directory:
                raise FactorLabError("Forward evaluation run has no published report to finalize")
            finalize_forward_evaluation_report(Path(directory), run.path)
    except Exception as exc:
        try:
            run.finish("failed", "signal/run finalization failed: %s" % exc)
        except Exception:
            LOGGER.exception("Cannot mark failed signal finalization in run manifest")
        LOGGER.error(
            "Command completed but its success evidence could not be finalised: %s: %s",
            type(exc).__name__,
            exc,
        )
        return 1
    LOGGER.info("Manifest: %s", run.path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
