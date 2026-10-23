"""Single-stock provider/factor/label diagnostic, never a CSI500 experiment.

Run from an importable repository (the YAML path is repository-root-relative)::

    python -m examples.research.real_factor_label_smoke \
        --config configs/research/csi500_smoke.yaml \
        --start 2023-06-01 --end 2024-01-12 --symbol 600000.SH

``--start`` and ``--end`` delimit the actual diagnostic download window. There
is no hidden pre-start download: initial rolling-factor warm-up values remain
missing, and the final ``holding_days + 1`` labels remain immature. Both edges
must be observed benchmark sessions. This example does not establish that the
stock was a historical CSI500 member, filter ST/suspensions, evaluate RankIC,
train a model, or send orders. It never changes proxy or socket settings.

For a verified pre-existing cache, ``--read-only-cache-root PATH`` reads the
existing ``PATH/daily`` and ``PATH/benchmark`` generations in place. This mode
requires full declared request coverage, never contacts a provider endpoint,
never copies/updates those caches, and never silently fetches missing data.
"""

import argparse
from dataclasses import replace
from datetime import date, datetime
import hashlib
import io
import logging
import math
import os
from pathlib import Path
from typing import Any, Dict, Optional, Sequence, Union

import numpy as np
import pandas as pd

from qbot.research.config import ResearchConfig, discover_repository_root, load_config
from qbot.research.data.cache import BarCacheSnapshot, ParquetBarCache
from qbot.research.data.calendar import TradingCalendar, reindex_bars_to_calendar
from qbot.research.data.providers.akshare import AKShareProvider
from qbot.research.data.providers.base import MarketDataProvider
from qbot.research.data.providers.local import LocalParquetProvider
from qbot.research.data.schema import normalize_symbol
from qbot.research.errors import ConfigurationError, DataQualityError, FactorLabError
from qbot.research.factors.registry import compute_factors, get_factor
from qbot.research.labels.forward_return import build_forward_excess_return_labels
from qbot.research.reproducibility import RunManifest, set_random_seed


LOGGER = logging.getLogger(__name__)
WARNING = (
    "DIAGNOSTIC ONLY: no point-in-time CSI500 membership or security-status "
    "verification; no RankIC, research conclusion, model, or order. "
    "600000.SH is a connectivity example, not a CSI500 membership claim."
)


def _iso_date(value: Union[str, date], name: str) -> date:
    if isinstance(value, date) and not isinstance(value, (datetime, pd.Timestamp)):
        return value
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ConfigurationError("%s must be a YYYY-MM-DD date" % name) from exc
    if parsed.isoformat() != value:
        raise ConfigurationError("%s must be a YYYY-MM-DD date" % name)
    return parsed


def _provider(
    config: ResearchConfig, retry_count: int, timeout_seconds: float
) -> MarketDataProvider:
    if config.data.provider == "local_parquet":
        return LocalParquetProvider(
            config.paths.market_input_root, benchmark_symbol=config.data.benchmark
        )
    return AKShareProvider(
        benchmark_symbol=config.data.benchmark,
        adjustment=config.data.adjustment,
        retry_count=retry_count,
        backoff_seconds=config.network.backoff_seconds,
        request_interval=config.network.request_interval_seconds,
        timeout_seconds=timeout_seconds,
    )


def _snapshot(
    cache: ParquetBarCache,
    provider: MarketDataProvider,
    symbol: str,
    start: date,
    end: date,
    is_index: bool,
    force_refresh: bool,
) -> BarCacheSnapshot:
    source = provider.__class__.__name__
    source_version = provider.cache_source_version(symbol, is_index)
    if force_refresh:
        cache.assert_source_identity(symbol, source=source, source_version=source_version)
        fetch = provider.get_index_bars if is_index else provider.get_daily_bars
        bars = fetch(symbol, start, end)
        if not isinstance(bars, pd.DataFrame) or bars.empty:
            raise DataQualityError("Forced diagnostic refresh returned no bars: %s" % symbol)
        cache.update(symbol, bars, start, end, source, source_version)
    else:
        cache.update_from_provider(provider, symbol, start, end, is_index=is_index)
    # Rows and both hashes come from the same verified committed generation;
    # do not separately re-read the live metadata pointer for the manifest.
    snapshot = cache.load_snapshot(
        symbol, expected_source=source, expected_source_version=source_version
    )
    if snapshot.frame.empty or not snapshot.metadata_sha256:
        raise DataQualityError("Diagnostic cache has no verified bars: %s" % symbol)
    return snapshot


def _evidence(snapshot: BarCacheSnapshot, cache: ParquetBarCache) -> Dict[str, Any]:
    return {
        "cache_root": str(cache.root),
        "metadata": snapshot.metadata,
        "metadata_sha256": snapshot.metadata_sha256,
    }


def _read_only_snapshot(
    cache: ParquetBarCache,
    provider: MarketDataProvider,
    symbol: str,
    start: date,
    end: date,
    is_index: bool,
) -> BarCacheSnapshot:
    """Read one generation and check its own coverage, without any writes."""

    snapshot = cache.load_snapshot(
        symbol,
        expected_source=provider.__class__.__name__,
        expected_source_version=provider.cache_source_version(symbol, is_index),
    )
    if snapshot.frame.empty or not snapshot.metadata_sha256:
        raise DataQualityError("Read-only diagnostic cache has no verified bars: %s" % symbol)
    # Inspect the pinned snapshot, not missing_ranges/read_metadata, which
    # could observe a newer pointer than the bars and hashes just consumed.
    first, last = pd.Timestamp(start), pd.Timestamp(end)
    cursor = first
    ranges = sorted(
        (pd.Timestamp(item["start"]), pd.Timestamp(item["end"]))
        for item in snapshot.metadata["covered_ranges"]
    )
    for covered_start, covered_end in ranges:
        if covered_end < cursor:
            continue
        if covered_start > cursor:
            break
        if covered_end >= last:
            return snapshot
        cursor = covered_end + pd.Timedelta(days=1)
    raise DataQualityError(
        "Read-only diagnostic cache does not cover the complete requested window for %s: %s to %s"
        % (symbol, first.date().isoformat(), last.date().isoformat())
    )


def _write_parquet(frame: pd.DataFrame, path: Path) -> Dict[str, Any]:
    """Publish only a new run-owned file, hash the actual serialized bytes."""

    buffer = io.BytesIO()
    frame.to_parquet(buffer, index=False)
    content = buffer.getvalue()
    with path.open("xb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    return {"path": str(path), "sha256": hashlib.sha256(content).hexdigest(),
            "row_count": int(len(frame))}


def run_smoke(
    config: ResearchConfig,
    *,
    start: Union[str, date],
    end: Union[str, date],
    symbol: str = "600000.SH",
    retry_count: Optional[int] = None,
    timeout_seconds: Optional[float] = None,
    force_refresh: bool = False,
    read_only_cache_root: Optional[Union[str, Path]] = None,
    provider: Optional[MarketDataProvider] = None,
) -> Dict[str, Any]:
    """Execute the real pipeline; provider injection is for offline tests only.

    Domain/provider errors are preserved, while the production RunManifest
    records failure. The configured formal cache, dataset, PIT and reports are
    never written by this diagnostic.
    """

    first, last = _iso_date(start, "start"), _iso_date(end, "end")
    if first > last:
        raise ConfigurationError("start must be on or before end")
    if normalize_symbol(symbol) != symbol:
        raise ConfigurationError("symbol must be canonical, e.g. 600000.SH")
    retries = config.network.max_retries if retry_count is None else retry_count
    timeout = config.network.timeout_seconds if timeout_seconds is None else timeout_seconds
    if isinstance(retries, bool) or not isinstance(retries, int) or retries < 0:
        raise ConfigurationError("retry_count must be a non-negative integer")
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        raise ConfigurationError("timeout_seconds must be finite and positive")
    if not math.isfinite(timeout) or timeout <= 0:
        raise ConfigurationError("timeout_seconds must be finite and positive")
    if type(force_refresh) is not bool:
        raise ConfigurationError("force_refresh must be boolean")

    effective = replace(config, data=replace(config.data, start=first, end=last))
    # dataclasses.replace resets init=False provenance fields; keep the loader's
    # authoritative repository/config location rather than falling back to cwd.
    object.__setattr__(effective, "repo_root", config.repo_root)
    object.__setattr__(effective, "config_path", config.config_path)
    effective.validate()
    set_random_seed(effective.seed)
    run = RunManifest(effective, "real-factor-label-smoke", {
        "symbol": symbol, "start": first.isoformat(), "end": last.isoformat(),
        "retry_count": retries, "timeout_seconds": float(timeout),
        "force_refresh": force_refresh,
        "read_only_cache_root": None if read_only_cache_root is None else str(read_only_cache_root),
    })
    run.payload.update({
        "diagnostic_only": True,
        "research_conclusions_permitted": False,
        "point_in_time_universe_verified": False,
        "security_status_verified": False,
        "rank_ic_evaluated": False,
        "cross_sectional_preprocessing_applied": False,
        "provider_injected_for_testing": provider is not None,
        "data_access": (
            "existing-verified-cache-only" if read_only_cache_root is not None
            else "provider-with-incremental-cache"
        ),
        "input_root": None,
        "warning": WARNING,
        "rolling_warmup_policy": "within_requested_window_only_no_fill",
        "configured_formal_warmup_applied": False,
        "network_controls": {
            "retry_count": retries, "stock_connection_read_timeout_seconds": float(timeout),
            "benchmark_native_timeout_supported": False,
            "whole_task_wall_clock_deadline_supported": False,
            "proxy_settings_modified": False,
            "process_no_proxy_all": (os.environ.get("no_proxy") or os.environ.get("NO_PROXY")) == "*",
        },
    })
    try:
        if read_only_cache_root is not None:
            candidate = Path(read_only_cache_root).expanduser()
            repository = effective.repo_root or discover_repository_root()
            cache_root = (candidate if candidate.is_absolute() else repository / candidate).resolve()
            run.payload["input_root"] = str(cache_root)
        else:
            cache_root = effective.paths.data_root / "diagnostics" / "market-smoke"
        # Persist the diagnostic safeguards before a potentially long vendor
        # request; an interrupted run remains visibly diagnostic-only.
        run.add_output("diagnostic_cache_root", cache_root)
        LOGGER.warning(WARNING)
        if read_only_cache_root is not None:
            if force_refresh:
                raise ConfigurationError("--force-refresh cannot be combined with --read-only-cache-root")
            if not cache_root.is_dir():
                raise DataQualityError("Read-only diagnostic cache root is not an existing directory: %s" % cache_root)
            for name in ("daily", "benchmark"):
                if not (cache_root / name).is_dir():
                    raise DataQualityError("Read-only diagnostic cache directory is missing: %s" % (cache_root / name))
            LOGGER.info("Reading verified existing cache in place; no provider endpoint calls: %s", cache_root)
        elif effective.data.provider == "akshare":
            LOGGER.warning(
                "AKShare benchmark SDK has no native timeout parameter; "
                "--timeout-seconds applies to stock connection/read operations, "
                "not the benchmark endpoint or a whole-task deadline."
            )
        selected_provider = provider if provider is not None else _provider(effective, retries, timeout)
        run.payload["provider_class"] = selected_provider.__class__.__name__
        cache_namespace = cache_root if read_only_cache_root is not None else cache_root / effective.data.provider
        stock_cache = ParquetBarCache(
            cache_namespace / "daily",
            lock_timeout_seconds=effective.cache.lock_timeout_seconds,
        )
        benchmark_cache = ParquetBarCache(
            cache_namespace / "benchmark",
            lock_timeout_seconds=effective.cache.lock_timeout_seconds,
        )
        if read_only_cache_root is not None:
            benchmark_snapshot = _read_only_snapshot(
                benchmark_cache, selected_provider, effective.data.benchmark, first, last, True
            )
        else:
            benchmark_snapshot = _snapshot(
                benchmark_cache, selected_provider, effective.data.benchmark,
                first, last, True, force_refresh,
            )
        run.payload["cache_evidence"] = {
            "benchmark": _evidence(benchmark_snapshot, benchmark_cache)
        }
        benchmark = benchmark_snapshot.frame.loc[
            benchmark_snapshot.frame["date"].between(pd.Timestamp(first), pd.Timestamp(last))
        ].copy().reset_index(drop=True)
        calendar = TradingCalendar.from_benchmark_bars(benchmark)
        if not calendar.contains(first) or not calendar.contains(last):
            raise DataQualityError(
                "Diagnostic boundaries must be observed benchmark trading sessions: %s to %s"
                % (first.isoformat(), last.isoformat())
            )
        minimum_factor_rows = max(get_factor(name).lookback for name in effective.factors)
        if len(calendar) < minimum_factor_rows:
            raise DataQualityError(
                "Insufficient within-window factor warm-up: %d/%d benchmark sessions"
                % (len(calendar), minimum_factor_rows)
            )
        if read_only_cache_root is not None:
            stock_snapshot = _read_only_snapshot(stock_cache, selected_provider, symbol, first, last, False)
        else:
            stock_snapshot = _snapshot(
                stock_cache, selected_provider, symbol, first, last, False, force_refresh
            )
        run.payload["cache_evidence"]["stock"] = _evidence(stock_snapshot, stock_cache)
        observed = stock_snapshot.frame.loc[
            stock_snapshot.frame["date"].between(pd.Timestamp(first), pd.Timestamp(last))
        ].copy().reset_index(drop=True)
        bars = reindex_bars_to_calendar(observed, calendar, symbols=[symbol])
        factors_with_bars = compute_factors(bars, effective.factors)
        factors = factors_with_bars[["date", "symbol"] + list(effective.factors)].copy()
        valid_factor_rows = {
            name: int(np.isfinite(factors[name].to_numpy(dtype=float)).sum())
            for name in effective.factors
        }
        missing_factors = [name for name, count in valid_factor_rows.items() if count == 0]
        if missing_factors:
            raise DataQualityError("Diagnostic has no valid observations for factors: %s" % missing_factors)
        labels = build_forward_excess_return_labels(
            bars, benchmark, holding_days=effective.label.holding_days,
            entry_price=effective.label.entry_price, calendar=calendar.dates,
        )
        target = "EXCESS_RET%d" % effective.label.holding_days
        mature_labels = int(labels[target].notna().sum())
        if mature_labels == 0:
            raise DataQualityError("Diagnostic has no matured %s labels" % target)
        run.payload["actual_data"] = {
            "symbol": symbol, "benchmark": effective.data.benchmark,
            "start": calendar.dates[0].date().isoformat(),
            "end": calendar.dates[-1].date().isoformat(),
            "benchmark_sessions": len(calendar),
            "observed_stock_rows": int(len(observed)),
            "stock_rows_outside_benchmark_calendar": int((~observed["date"].isin(calendar.dates)).sum()),
            "aligned_stock_rows": int(len(bars)),
            "missing_stock_open_rows": int(bars["adj_open"].isna().sum()),
            "valid_factor_rows": valid_factor_rows,
            "mature_labels": mature_labels,
            "holding_days": effective.label.holding_days,
            "immature_tail_sessions": effective.label.holding_days + 1,
        }
        destination = effective.paths.results_root / "diagnostics" / "market-smoke" / run.run_id
        destination.mkdir(parents=True, exist_ok=False)
        run.payload["output_artifacts"] = {}
        for name, frame in (("stock_bars", bars), ("benchmark_bars", benchmark),
                            ("factors", factors), ("labels", labels)):
            artifact = _write_parquet(frame, destination / (name + ".parquet"))
            run.payload["output_artifacts"][name] = artifact
            run.add_output(name, artifact["path"])
        run.finish()
        LOGGER.info(
            "Diagnostic completed: %d sessions, %d configured factors, %d mature labels; manifest=%s",
            len(calendar), len(effective.factors), mature_labels, run.path,
        )
    except Exception as exc:
        run.payload["error_type"] = type(exc).__name__
        run.finish("failed", str(exc))
        raise
    return {"manifest_path": str(run.path), "manifest": run.payload}


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="configs/research/csi500_smoke.yaml")
    parser.add_argument("--symbol", default="600000.SH", help="Canonical symbol; not a CSI500 membership claim")
    parser.add_argument("--start", required=True, help="Actual diagnostic window start, YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="Actual diagnostic window end, YYYY-MM-DD")
    parser.add_argument("--retry-count", type=int, default=None, help="Retries after initial request (0 permitted)")
    parser.add_argument("--timeout-seconds", type=float, default=None, help="Native stock connection/read timeout, not benchmark/task deadline")
    parser.add_argument("--force-refresh", action="store_true", help="Fetch the entire window even if diagnostic caches cover it")
    parser.add_argument("--read-only-cache-root", default=None, help="Existing root with daily/ and benchmark/; repository-relative or absolute; no provider calls or cache writes")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        config = load_config(args.config)
        run_smoke(
            config, start=args.start, end=args.end, symbol=args.symbol,
            retry_count=args.retry_count, timeout_seconds=args.timeout_seconds,
            force_refresh=args.force_refresh,
            read_only_cache_root=args.read_only_cache_root,
        )
    except FactorLabError as exc:
        LOGGER.error("Diagnostic failed: %s", exc)
        return 1
    except Exception:
        LOGGER.exception("Unexpected diagnostic failure")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
