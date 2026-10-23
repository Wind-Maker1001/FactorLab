"""Offline CSI500 historical diagnostic; never a formal model/Forward run."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import platform
import sys
from time import perf_counter
from typing import Optional, Sequence
import uuid

import numpy as np
import pandas as pd

from qbot.research.config import load_config
from qbot.research.data.cache import ParquetBarCache
from qbot.research.diagnostics.market_regime import DISCLAIMER, diagnose_market_regime
from qbot.research.errors import ConfigurationError, DataQualityError
from qbot.research.reports.market_regime import write_market_regime_report
from qbot.research.reproducibility import _atomic_json, git_commit, git_worktree_state, package_versions


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ") + "-market-regime-" + uuid.uuid4().hex[:8]


def synthetic_benchmark(start, end, seed):
    """Explicit artificial business dates and prices for software checks only."""
    dates = pd.bdate_range(pd.Timestamp(start) - pd.offsets.BDay(130), end)
    rng = np.random.default_rng(seed)
    close = 5000.0 * np.exp(np.cumsum(rng.normal(0.0002, 0.01, len(dates))))
    return pd.DataFrame({"date": dates, "symbol": "000905.SH", "adj_close": close})


def run_diagnostic(config, synthetic: bool = False) -> Path:
    """Create a new run, bind a single cache snapshot, report or persist failure."""
    started = perf_counter()
    directory = config.paths.results_root / "diagnostics" / "csi500-market-regime" / _run_id()
    # Collision is an error; no existing file or receipt can be rewritten.
    directory.mkdir(parents=True, exist_ok=False)
    manifest_path = directory / "manifest.json"
    payload = {
        "schema_version": 1, "artifact_type": "CSI500_HISTORICAL_REGIME_DIAGNOSTIC",
        "run_id": directory.name, "status": "running", "started_at": datetime.now(timezone.utc).isoformat(),
        "completed_at": None, "elapsed_seconds": None, "failures": [],
        "config": config.to_dict(), "config_path": str(config.config_path) if config.config_path else None,
        "diagnostic_config": {"price_column": "adj_close", "ma_short": 20, "ma_long": 60,
                              "return_semantics": "SAME_DAY_CLOSE_TO_CLOSE_CONDITIONED_ON_STATE_T",
                              "volatility_ddof": 1, "annualization_sessions": 252,
                              "calendar": "observed_cache_dates", "unknown_overlay_only": True},
        "synthetic_only": synthetic, "research_conclusions_permitted": False,
        "forward_evidence": False, "live_trading": False, "network_requests": 0,
        "warning": DISCLAIMER, "data": {"state": "not_loaded"},
        "outputs": {}, "measurements": {},
    }
    _atomic_json(payload, manifest_path)
    try:
        if config.data.benchmark != "000905.SH" or config.live_trading:
            raise ConfigurationError("CSI500 diagnostic requires 000905.SH and NO LIVE MONEY")
        root = config.repo_root or Path(__file__).resolve().parents[2]
        payload.update({"git_commit": git_commit(root), **git_worktree_state(root),
                        "python_version": platform.python_version(), "python_executable": sys.executable,
                        "package_versions": package_versions()})
        sources = sorted((root / "qbot" / "research").rglob("*.py")) + [Path(__file__).resolve(), root / "dev" / "requirements-research.txt"]
        payload["source_sha256"] = {str(path.relative_to(root)): _sha256(path) for path in sources}
        if config.config_path:
            payload["config_sha256"] = _sha256(config.config_path)
        mark = perf_counter()
        if synthetic:
            benchmark = synthetic_benchmark(config.data.start, config.data.end, config.seed)
            input_path = directory / "synthetic_input.csv"
            with input_path.open("x", encoding="utf-8", newline="") as stream:
                benchmark.assign(dataset_kind="SYNTHETIC_SOFTWARE_CHECK").to_csv(stream, index=False, date_format="%Y-%m-%d")
            payload["data"] = {"state": "synthetic", "source": "ARTIFICIAL_BUSINESS_DATES_AND_PRICES",
                               "seed": config.seed, "path": str(input_path), "sha256": _sha256(input_path)}
            payload["outputs"][input_path.name] = {"sha256": _sha256(input_path)}
            payload["diagnostic_config"]["calendar"] = "synthetic_business_dates_not_exchange_calendar"
        else:
            cache_root = config.paths.benchmark_dir
            if not cache_root.is_dir():
                payload["data"] = {"state": "missing", "cache_root": str(cache_root)}
                raise DataQualityError("REAL CSI500 CACHE REQUIRED: local benchmark cache is absent; no network/synthetic fallback")
            snapshot = ParquetBarCache(cache_root, config.cache.lock_timeout_seconds).load_snapshot(config.data.benchmark, verify_hash=True)
            if snapshot.frame.empty:
                payload["data"] = {"state": "missing", "cache_root": str(cache_root)}
                raise DataQualityError("REAL CSI500 CACHE REQUIRED: no committed benchmark observations; no network/synthetic fallback")
            benchmark = snapshot.frame
            payload["data"] = {"state": "verified_cache", "cache_root": str(cache_root),
                               "sha256": snapshot.metadata["content_sha256"],
                               "metadata_sha256": snapshot.metadata_sha256, "metadata": snapshot.metadata}
        payload["measurements"]["input_seconds"] = perf_counter() - mark
        _atomic_json(payload, manifest_path)
        mark = perf_counter()
        diagnosis = diagnose_market_regime(benchmark, config.data.start, config.data.end)
        payload["measurements"]["diagnosis_seconds"] = perf_counter() - mark
        payload["coverage"] = diagnosis.coverage
        payload["measurements"].update({"input_rows": len(benchmark), "report_rows": len(diagnosis.daily),
                                        "daily_dataframe_bytes": int(diagnosis.daily.memory_usage(index=True, deep=True).sum()),
                                        "peak_rss_measured": False})
        mark = perf_counter()
        outputs = write_market_regime_report(diagnosis, directory, synthetic=synthetic)
        payload["outputs"].update({name: {"sha256": _sha256(path), "bytes": path.stat().st_size}
                                   for name, path in outputs.items()})
        payload["measurements"]["report_seconds"] = perf_counter() - mark
        payload["status"] = "success"
    except BaseException as exc:
        payload["status"] = "failed"
        payload["failures"].append({"type": type(exc).__name__, "message": str(exc)})
        # Keep partial files for audit, identified by hashes, never certify them.
        payload["outputs"].update({path.name: {"sha256": _sha256(path), "bytes": path.stat().st_size}
                                   for path in directory.iterdir() if path.is_file() and path != manifest_path})
        raise
    finally:
        payload["completed_at"] = datetime.now(timezone.utc).isoformat()
        payload["elapsed_seconds"] = perf_counter() - started
        _atomic_json(payload, manifest_path)
    return directory


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=DISCLAIMER)
    parser.add_argument("--config", default="configs/research/csi500_5y.yaml")
    parser.add_argument("--synthetic", action="store_true", help="Explicit artificial software check; never real CSI500 results")
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        directory = run_diagnostic(config, args.synthetic)
    except Exception as exc:
        print("CSI500 diagnostic FAILED: %s: %s" % (type(exc).__name__, exc), file=sys.stderr)
        return 1
    print("%s\n%s\n%s" % (DISCLAIMER, "SYNTHETIC SOFTWARE CHECK" if args.synthetic else "VERIFIED LOCAL CACHE", directory))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
