"""Fresh benchmark acquisition in an isolated cache, with a process deadline.

This is an input-repair tool, not a CSI500 experiment. It never reads, relabels,
overwrites or promotes the active benchmark cache, and never queries members.
Each invocation fetches the configured research range plus warm-up from the
current AKShare Provider into a new run-owned cache. Review the successful
manifest before explicitly archiving/replacing an incompatible active cache.

The installed index SDK has no native timeout argument. A child-process deadline
bounds that SDK call without changing global sockets, proxies or requests.
"""

import argparse
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import subprocess
import sys
from typing import Any, Dict, Optional, Sequence

import pandas as pd

from factorlab.cli import _benchmark_request_start, _select_benchmark_with_warmup
from factorlab.config import ResearchConfig, load_config
from factorlab.data.cache import ParquetBarCache
from factorlab.data.calendar import TradingCalendar
from factorlab.data.providers.akshare import AKShareProvider
from factorlab.data.providers.base import MarketDataProvider
from factorlab.data.schema import canonicalize_bars
from factorlab.errors import ConfigurationError, DataQualityError, FactorLabError
from factorlab.reproducibility import RunManifest, set_random_seed


LOGGER = logging.getLogger(__name__)
WARNING = (
    "BENCHMARK INPUT REPAIR ONLY: no verified PIT universe/security statuses, "
    "dataset, RankIC, model, backtest, Forward signal or live order. "
    "The active benchmark cache is unchanged."
)


def _write_json_exclusive(path: Path, payload: Dict[str, Any]) -> None:
    with path.open("x", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())


def _provider(config: ResearchConfig) -> AKShareProvider:
    if config.data.provider != "akshare":
        raise ConfigurationError("Fresh benchmark repair requires data.provider=akshare")
    return AKShareProvider(
        benchmark_symbol=config.data.benchmark,
        adjustment=config.data.adjustment,
        retry_count=0,
        request_interval=config.network.request_interval_seconds,
        timeout_seconds=config.network.timeout_seconds,
    )


def _fresh_cache(
    config: ResearchConfig, destination: Path,
    provider: Optional[MarketDataProvider] = None,
) -> Dict[str, Any]:
    """Fetch before publishing; old/partial caches can never mask missing edges."""

    root = Path(destination)
    expected_parent = config.paths.data_root / "diagnostics" / "benchmark-refetch"
    if root.resolve().parent != expected_parent.resolve() or root.is_symlink():
        raise ConfigurationError("Fresh cache must be a new benchmark-refetch diagnostic directory")
    # No read/merge/skip-existing path is permissible for this fresh acquisition.
    root.mkdir(parents=True, exist_ok=False)
    selected_provider = provider if provider is not None else _provider(config)
    start, end = _benchmark_request_start(config), pd.Timestamp(config.data.end)
    incoming = selected_provider.get_index_bars(config.data.benchmark, start, end)
    if not isinstance(incoming, pd.DataFrame) or incoming.empty:
        raise DataQualityError("Fresh benchmark endpoint returned no bars")
    bars = canonicalize_bars(incoming, symbol=config.data.benchmark)
    if not bars["date"].between(start, end).all():
        raise DataQualityError("Fresh benchmark bars fall outside requested dates")
    required = _select_benchmark_with_warmup(config, bars)
    TradingCalendar.from_benchmark_bars(required)
    # Missing benchmark opens make the next-open relative-return label invalid.
    if required[["adj_open", "adj_high", "adj_low", "adj_close"]].isna().any().any():
        raise DataQualityError("Fresh benchmark has missing required OHLC prices")
    source = selected_provider.__class__.__name__
    version = selected_provider.cache_source_version(config.data.benchmark, True)
    cache = ParquetBarCache(root, config.cache.lock_timeout_seconds)
    cache.update(config.data.benchmark, bars, start, end, source, version)
    snapshot = cache.load_snapshot(
        config.data.benchmark, expected_source=source, expected_source_version=version,
    )
    # Recheck the actual serialized generation, not only the incoming frame.
    _select_benchmark_with_warmup(config, snapshot.frame)
    research = required.loc[required["date"] >= pd.Timestamp(config.data.start)]
    unused_weekends = bars.loc[~bars["date"].isin(required["date"]) & bars["date"].dt.dayofweek.ge(5), "date"]
    result = {
        "cache_root": str(root.resolve()),
        "metadata": snapshot.metadata,
        "metadata_sha256": snapshot.metadata_sha256,
        "requested_start": start.date().isoformat(),
        "requested_end": end.date().isoformat(),
        "returned_rows": len(snapshot.frame),
        "research_sessions": len(research),
        "warmup_sessions": len(required) - len(research),
        "unused_weekend_observations": [value.date().isoformat() for value in unused_weekends],
        "required_weekend_observations": 0,
        "missing_required_ohlc_rows": int(required["adj_open"].isna().sum()),
        "provider_fetch_calls": 1,
        "sdk_retry_count": 0,
        "http_request_count": None,
        "http_request_count_observed": False,
        "historical_availability_proven": False,
        "point_in_time_universe_verified": False,
        "security_status_verified": False,
        "active_cache_changed": False,
    }
    _write_json_exclusive(root / "completion.json", result)
    return result


def _execute_child(command: Sequence[str], log_path: Path, deadline: float, cwd: Path):
    """subprocess.run kills and reaps its child before raising TimeoutExpired."""

    with log_path.open("xb") as log:
        return subprocess.run(
            list(command), cwd=str(cwd), stdout=log, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, timeout=deadline, check=False,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )


def run_refetch(config: ResearchConfig, *, deadline_seconds: float = 120.0) -> Dict[str, Any]:
    if (
        isinstance(deadline_seconds, bool)
        or not isinstance(deadline_seconds, (int, float))
        or not math.isfinite(float(deadline_seconds))
        or not 1.0 <= float(deadline_seconds) <= 600.0
    ):
        raise ConfigurationError("deadline_seconds must be finite and between 1 and 600")
    if config.data.provider != "akshare":
        raise ConfigurationError("Fresh benchmark repair requires data.provider=akshare")
    set_random_seed(config.seed)
    run = RunManifest(config, "refetch-benchmark", {"deadline_seconds": deadline_seconds})
    destination = config.paths.data_root / "diagnostics" / "benchmark-refetch" / run.run_id
    frozen_config = run.path.parent / "worker-config.json"
    log_path = run.path.parent / "worker.log"
    run.payload.update({
        "warning": WARNING, "diagnostic_only": True,
        "research_conclusions_permitted": False, "active_cache_changed": False,
        "point_in_time_universe_verified": False, "security_status_verified": False,
        "worker_terminal": False,
    })
    try:
        # JSON is also a strict YAML document. Freeze the expanded validated
        # configuration so editing the caller's YAML cannot change this fetch.
        _write_json_exclusive(frozen_config, config.to_dict())
        run.payload["worker_config_sha256"] = hashlib.sha256(frozen_config.read_bytes()).hexdigest()
        run.add_output("worker_config", frozen_config)
        run.add_output("worker_log", log_path)
        run.add_output("candidate_cache", destination)
        command = [
            sys.executable, "-m", "examples.research.refetch_benchmark", "--worker",
            "--config", str(frozen_config), "--destination", str(destination),
        ]
        completed = _execute_child(command, log_path, float(deadline_seconds), config.repo_root or Path.cwd())
        run.payload["worker_terminal"] = True
        run.payload["worker_returncode"] = completed.returncode
        if completed.returncode != 0:
            raise DataQualityError("Fresh benchmark worker failed; inspect worker.log")
        if hashlib.sha256(frozen_config.read_bytes()).hexdigest() != run.payload["worker_config_sha256"]:
            raise DataQualityError("Frozen worker configuration changed during acquisition")
        receipt_path = destination / "completion.json"
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        provider = _provider(config)
        cache = ParquetBarCache(destination, config.cache.lock_timeout_seconds)
        snapshot = cache.load_snapshot(
            config.data.benchmark, expected_source=provider.__class__.__name__,
            expected_source_version=provider.cache_source_version(config.data.benchmark, True),
        )
        if (
            snapshot.frame.empty
            or receipt.get("metadata_sha256") != snapshot.metadata_sha256
            or receipt.get("metadata") != snapshot.metadata
        ):
            raise DataQualityError("Fresh benchmark worker receipt does not match its cache")
        required = _select_benchmark_with_warmup(config, snapshot.frame)
        TradingCalendar.from_benchmark_bars(required)
        if required[["adj_open", "adj_high", "adj_low", "adj_close"]].isna().any().any():
            raise DataQualityError("Fresh benchmark generation has missing required OHLC prices")
        research = required.loc[required["date"] >= pd.Timestamp(config.data.start)]
        expected_statistics = {
            "returned_rows": len(snapshot.frame),
            "research_sessions": len(research),
            "warmup_sessions": len(required) - len(research),
            "missing_required_ohlc_rows": 0,
            "required_weekend_observations": 0,
            "unused_weekend_observations": [
                value.date().isoformat() for value in snapshot.frame.loc[
                    ~snapshot.frame["date"].isin(required["date"])
                    & snapshot.frame["date"].dt.dayofweek.ge(5), "date"
                ]
            ],
        }
        if any(receipt.get(key) != value for key, value in expected_statistics.items()):
            raise DataQualityError("Fresh benchmark worker statistics do not match its cache")
        run.payload["actual_data"] = receipt
        run.payload["completion_sha256"] = hashlib.sha256(receipt_path.read_bytes()).hexdigest()
        run.add_output("completion", receipt_path)
        run.finish()
        LOGGER.info("Fresh benchmark verified: %s; manifest=%s", destination, run.path)
    except subprocess.TimeoutExpired as exc:
        run.payload.update({"worker_terminal": True, "worker_timed_out": True,
                            "error_type": type(exc).__name__})
        run.finish("failed", "Benchmark SDK exceeded the child-process deadline; child killed and reaped")
        LOGGER.error("Benchmark deadline exceeded; active cache unchanged; manifest=%s", run.path)
    except Exception as exc:
        run.payload["error_type"] = type(exc).__name__
        run.finish("failed", str(exc))
        LOGGER.error("Benchmark repair failed: %s; manifest=%s", exc, run.path)
    return {"manifest_path": str(run.path), "manifest": run.payload}


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="configs/research/csi500_5y.yaml")
    parser.add_argument("--deadline-seconds", type=float, default=120.0)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--destination", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        config = load_config(args.config)
        if args.worker:
            if args.destination is None:
                raise ConfigurationError("Worker destination is required")
            _fresh_cache(config, Path(args.destination))
            return 0
        if args.destination is not None:
            raise ConfigurationError("Destination is run-owned, not user-selectable")
        return 0 if run_refetch(config, deadline_seconds=args.deadline_seconds)["manifest"]["status"] == "success" else 1
    except FactorLabError as exc:
        LOGGER.error("Fresh benchmark failed: %s", exc)
    except Exception:
        LOGGER.exception("Unexpected benchmark repair failure")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
