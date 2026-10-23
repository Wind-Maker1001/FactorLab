"""Measure the real research engines at scale using SYNTHETIC inputs only.

This offline engineering diagnostic does not download market data, publish a
universe, train models or produce research/Forward evidence. Business-day dates,
members, prices and security status are all explicitly artificial.
"""

import argparse
import cProfile
from contextlib import nullcontext
from dataclasses import asdict
import hashlib
import json
import logging
from pathlib import Path
import platform
import pstats
from time import perf_counter
from typing import Optional, Sequence

import numpy as np
import pandas as pd

from factorlab.config import load_config
from factorlab.dataset.builder import DatasetBuilder, DatasetBuildOptions
from factorlab.errors import ConfigurationError, SchemaError
from factorlab.factors.registry import get_factor
from factorlab.metrics.factor import evaluate_factors
from factorlab.reproducibility import RunManifest
from factorlab.universe.filters import UniverseFilterConfig


LOGGER = logging.getLogger(__name__)


def synthetic_inputs(stock_count, research_days, warmup_days, seed):
    """Vectorised random walks. These dates are NOT an A-share calendar."""
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2010-01-04", periods=research_days + warmup_days)
    symbols = np.array(["%06d.SH" % (600000 + i) for i in range(stock_count)])

    def bars_for(names):
        returns = rng.normal(0.0001, 0.012, (len(dates), len(names)))
        close = np.linspace(10, 50, len(names)) * np.exp(np.cumsum(returns, axis=0))
        previous = np.vstack([np.linspace(10, 50, len(names)), close[:-1]])
        opening = previous * np.exp(rng.normal(0, 0.003, close.shape))
        volume = rng.integers(100000, 10000000, close.shape).astype(float)
        frame = pd.DataFrame({"date": np.repeat(dates.to_numpy(), len(names)), "symbol": np.tile(names, len(dates)),
                              "open": opening.ravel(), "close": close.ravel(),
                              "high": (np.maximum(opening, close) * 1.003).ravel(),
                              "low": (np.minimum(opening, close) * 0.997).ravel(), "volume": volume.ravel(),
                              "amount": (volume * (opening + close) / 2).ravel(),
                              "turnover": rng.uniform(0.001, 0.08, close.shape).ravel(), "adj_factor": 1.0})
        for field in ("open", "high", "low", "close"):
            frame["raw_" + field] = frame[field]
            frame["adj_" + field] = frame[field]
        return frame

    bars, benchmark = bars_for(symbols), bars_for(np.array(["000905.SH"]))
    membership = bars.loc[bars["date"] >= dates[warmup_days], ["date", "symbol"]].copy()
    membership["is_member"] = True
    status = bars[["date", "symbol"]].copy()
    status["trade_status"], status["is_st"] = 1, False
    listings = pd.DataFrame({"symbol": symbols, "listing_date": dates[0]})
    return bars, benchmark, membership, status, listings, dates


def _options(config, stock_count):
    u = config.universe
    filters = UniverseFilterConfig(min_listed_days=u.min_listed_days, exclude_st=u.exclude_st,
                                   exclude_suspended=u.exclude_suspended, zero_volume_window=u.zero_volume_window_days,
                                   quality_window=u.data_quality_window_days, min_quality_ratio=u.min_valid_ohlcv_ratio)
    return DatasetBuildOptions(factor_names=config.factors, holding_days=config.label.holding_days,
                               entry_price=config.label.entry_price, expected_member_count=stock_count,
                               winsorization=config.preprocessing.winsorization,
                               mad_multiplier=config.preprocessing.mad_multiplier,
                               minimum_cross_section=config.evaluation.min_cross_section,
                               include_market_regime=config.market_regime.enabled, filter_config=filters)


def _validate_args(args, config):
    if not max(20, config.evaluation.min_cross_section) <= args.stocks <= 1000:
        raise ConfigurationError("synthetic stock count must be between the minimum cross-section and 1000")
    if not max(20, config.label.holding_days + 2) <= args.research_days <= 2000:
        raise ConfigurationError("synthetic research days must be between 20 and 2000 and exceed label horizon")
    minimum_warmup = max(config.universe.min_listed_days, config.universe.data_quality_window_days,
                         config.universe.zero_volume_window_days, max(get_factor(name).lookback for name in config.factors) + 1,
                         61 if config.market_regime.enabled else 1)
    if not minimum_warmup <= args.warmup_days <= 500:
        raise ConfigurationError("synthetic warmup must meet all actual filter/factor requirements and be at most 500")
    if args.stocks * (args.research_days + args.warmup_days) > 2000000:
        raise ConfigurationError("synthetic input grid is bounded at two million rows")
    if args.evaluation_scope == "regimes" and not config.market_regime.enabled:
        raise ConfigurationError("regime diagnostics require the frozen market-regime features")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="configs/research/csi500_5y.yaml")
    parser.add_argument("--stocks", type=int, default=500)
    parser.add_argument("--research-days", type=int, default=1200)
    parser.add_argument("--warmup-days", type=int, default=130)
    parser.add_argument("--evaluation-scope", choices=("all", "regimes"), default="all")
    parser.add_argument("--timing-detail", action="store_true")
    parser.add_argument("--measure-memory", action="store_true")
    parser.add_argument("--report", action="store_true", help="write the complete production report in this diagnostic run")
    parser.add_argument("--profile", action="store_true", help="profile evaluation; exclude this run from wall-time comparisons")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    config = load_config(args.config)
    manifest = RunManifest(config, "scale-factor-pipeline", vars(args))
    manifest.payload.update({"diagnostic_only": True, "synthetic_only": True, "network_requests": 0,
                             "no_live_money": True,
                             "point_in_time_universe_verified": False, "historical_constituents_generated": False,
                             "research_conclusions_permitted": False, "forward_evidence": False,
                             "input_source": "seeded artificial prices, members, status and business-day dates",
                             "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()})
    timings, measurements = {}, {}
    recorder = None
    started = perf_counter()
    try:
        _validate_args(args, config)
        output = config.paths.results_root / "diagnostics" / "scale-factor-pipeline" / manifest.run_id
        inputs = output / "synthetic-inputs"
        inputs.mkdir(parents=True, exist_ok=False)
        from examples.research.factor_performance import PerformanceRecorder, environment_details
        recorder = PerformanceRecorder(args.measure_memory)
        recorder.__enter__()
        recorder.current_phase = "generate_inputs"
        stage = perf_counter()
        bars, benchmark, membership, status, listings, dates = synthetic_inputs(
            args.stocks, args.research_days, args.warmup_days, config.seed)
        timings["generate_inputs"] = perf_counter() - stage
        options = _options(config, args.stocks)
        manifest.payload["synthetic_build_options"] = asdict(options)
        research_dates = dates[args.warmup_days:]
        manifest.payload["synthetic_date_range"] = {"calendar_start": dates[0].isoformat(),
            "research_start": research_dates[0].isoformat(), "research_end": dates[-1].isoformat(),
            "calendar_is_simulated_not_exchange_calendar": True}
        recorder.current_phase = "write_inputs"
        stage = perf_counter()
        for name, frame in (("bars", bars), ("benchmark", benchmark), ("membership", membership), ("status", status), ("listings", listings)):
            frame.to_parquet(inputs / (name + ".parquet"), index=False)
        timings["write_inputs"] = perf_counter() - stage
        LOGGER.info("SYNTHETIC build: %d raw rows, %d research rows; no actual market evidence.", len(bars), len(membership))
        recorder.current_phase = "dataset_build"
        stage = perf_counter()
        builder = DatasetBuilder(options)
        dataset = builder.build(bars, benchmark, membership, status, listings, dates)
        timings["dataset_build"] = perf_counter() - stage
        expected = args.stocks * args.research_days
        target = "EXCESS_RET%d" % config.label.holding_days
        if len(dataset) != expected or dataset[list(config.factors)].isna().any().any():
            raise SchemaError("synthetic full-grid/factor coverage invariant failed")
        mature = int(dataset[target].notna().sum())
        if mature != args.stocks * (args.research_days - config.label.holding_days - 1):
            raise SchemaError("synthetic next-open tail label count invariant failed")
        measurements.update({"input_rows": len(bars), "dataset_rows": len(dataset), "mature_label_rows": mature,
                             "dataset_dataframe_deep_bytes": int(dataset.memory_usage(index=True, deep=True).sum()),
                             "process_peak_rss_measured": False, "factor_count": len(config.factors)})
        LOGGER.info("Dataset built in %.3fs; testing future-price mutation.", timings["dataset_build"])
        cutoff = research_dates[len(research_dates) // 2]
        changed_bars, changed_benchmark = bars.copy(), benchmark.copy()
        for frame in (changed_bars, changed_benchmark):
            future = frame["date"] > cutoff
            columns = [p + f for p in ("", "raw_", "adj_") for f in ("open", "high", "low", "close")] + ["amount"]
            frame.loc[future, columns] *= 1000000.0
        recorder.current_phase = "future_mutation_rebuild_and_check"
        stage = perf_counter()
        changed = builder.build(changed_bars, changed_benchmark, membership, status, listings, dates)
        label_columns = {"entry_date", "exit_date", target, "STOCK_FWD_RET%d" % config.label.holding_days,
                         "BENCHMARK_FWD_RET%d" % config.label.holding_days}
        past_columns = [column for column in dataset if column not in label_columns]
        before = dataset.loc[dataset["date"] <= cutoff]
        after = changed.loc[changed["date"] <= cutoff]
        pd.testing.assert_frame_equal(before[past_columns], after[past_columns], check_exact=True)
        observed = before[target].notna() & after[target].notna()
        changed_labels = int((~np.isclose(before.loc[observed, target].to_numpy(),
                                         after.loc[observed, target].to_numpy(), rtol=1e-12, atol=1e-12)).sum())
        if changed_labels == 0:
            raise SchemaError("future mutation did not exercise forward-label dependence")
        timings["future_mutation_rebuild_and_check"] = perf_counter() - stage
        measurements.update({"lookahead_prefix_rows_checked": len(before), "lookahead_prefix_exact_match": True,
                             "future_mutation_cutoff": cutoff.isoformat(), "future_price_multiplier": 1000000,
                             "past_labels_affected_by_future_mutation": changed_labels,
                             "label_change_rtol": 1e-12, "label_change_atol": 1e-12,
                             "labels_excluded_from_past_feature_comparison": True})
        del changed, changed_bars, changed_benchmark, before, after
        LOGGER.info("Past features unchanged; evaluating %d factors on synthetic data.", len(config.factors))
        recorder.current_phase = "factor_evaluation"
        from factorlab.metrics import factor as factor_metrics
        profiler = cProfile.Profile() if args.profile else None
        stage = perf_counter()
        with recorder.instrument_evaluator(factor_metrics) if args.timing_detail else nullcontext():
            if profiler is not None:
                profiler.enable()
            try:
                evaluation = evaluate_factors(dataset, config.factors, target=target, quantiles=config.evaluation.quantiles,
                    minimum_cross_section=config.evaluation.min_cross_section, calendar=research_dates,
                    group_column="MARKET_REGIME" if args.evaluation_scope == "regimes" else None)
            finally:
                if profiler is not None:
                    profiler.disable()
        timings["factor_evaluation"] = perf_counter() - stage
        expected_ic = args.research_days * len(config.factors) * (2 if args.evaluation_scope == "regimes" else 1)
        if len(evaluation.daily_ic) != expected_ic:
            raise SchemaError("synthetic evaluation calendar coverage invariant failed")
        measurements["daily_ic_rows"] = len(evaluation.daily_ic)
        if profiler is not None:
            profiler.dump_stats(str(output / "factor-evaluation.prof"))
            with (output / "factor-evaluation-profile.txt").open("x", encoding="utf-8") as stream:
                pstats.Stats(profiler, stream=stream).strip_dirs().sort_stats("cumulative").print_stats(50)
        if args.report:
            from factorlab.reports.generator import write_factor_report
            stage = perf_counter()
            with recorder.phase("report_generation"), recorder.instrument_report():
                write_factor_report(evaluation, output / "factor-report")
            timings["report_generation"] = perf_counter() - stage
        recorder.current_phase = "write_results"
        stage = perf_counter()
        builder.write_artifacts(dataset, output / "synthetic-dataset.parquet", output / "synthetic-features")
        for name in ("daily_ic", "summary", "quantile_returns", "autocorrelation", "turnover", "correlation"):
            getattr(evaluation, name).to_parquet(output / ("synthetic-" + name + ".parquet"), index=name == "correlation")
        timings["write_results"] = perf_counter() - stage
        timings["total_before_output_hashing"] = perf_counter() - started
        details = recorder.payload()
        measurements["process_memory_measured"] = details["process_memory"]["measured"]
        # Keep the legacy RSS flag precise: Windows Working Set is reported
        # explicitly in performance_details, rather than relabelled as RSS.
        measurements["process_peak_rss_measured"] = (
            details["process_memory"]["measured"] and details["process_memory"]["metric"].startswith("Linux"))
        measurement_path = output / "measurements.json"
        with measurement_path.open("x", encoding="utf-8") as stream:
            json.dump({"synthetic_only": True, "research_conclusions_permitted": False,
                       "no_live_money": True,
                       "timings_seconds": timings, "measurements": measurements,
                       "platform": platform.platform(), "processor": platform.processor(),
                       "performance_details": details, "environment_details": environment_details(),
                       "profiled_not_normal_wall_time": args.profile}, stream, indent=2)
            stream.write("\n")
        manifest.add_output("measurements", measurement_path)
        manifest.add_output("synthetic_dataset", output / "synthetic-dataset.parquet")
        recorder.current_phase = "output_hashing"
        stage = perf_counter()
        artifacts = {str(p.relative_to(output)): {"path": str(p),
            "sha256": hashlib.sha256(p.read_bytes()).hexdigest()} for p in sorted(output.rglob("*")) if p.is_file()}
        timings["output_hashing"] = perf_counter() - stage
        manifest.payload.update({"timings_seconds": timings, "measurements": measurements,
                                 "output_artifacts": artifacts})
        recorder.current_phase = "success_receipt"
        stage = perf_counter()
        manifest.finish("success")
        timings["success_receipt"] = perf_counter() - stage
        # An append-only completion measurement can bind the now-final success
        # receipt without a circular self-hash or rewriting the old measurement.
        completed = perf_counter() - started
        with (output / "performance-completion.json").open("x", encoding="utf-8") as stream:
            json.dump({"synthetic_only": True, "research_conclusions_permitted": False,
                       "no_live_money": True,
                       "total_seconds_through_success_receipt": completed,
                       "timings_seconds_through_success_receipt": timings,
                       "success_manifest_sha256": hashlib.sha256(manifest.path.read_bytes()).hexdigest(),
                       "performance_details": recorder.payload()}, stream, indent=2)
            stream.write("\n")
        LOGGER.info("Manifest: %s; SYNTHETIC engineering measurement only.", manifest.path)
        return 0
    except Exception as exc:
        manifest.payload.update({"timings_seconds": timings, "measurements": measurements, "elapsed_seconds": perf_counter() - started})
        manifest.finish("failed", "%s: %s" % (type(exc).__name__, exc))
        LOGGER.error("Synthetic scale diagnostic failed: %s: %s", type(exc).__name__, exc)
        return 1
    finally:
        if recorder is not None:
            recorder.__exit__(None, None, None)


if __name__ == "__main__":
    raise SystemExit(main())
