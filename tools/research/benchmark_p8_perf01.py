"""FactorLab P8-PERF-01: Reproducible Performance Audit & Benchmark Suite.

Covers:
- profile : cProfile + SQLite tx/commit + IPC pickle + per-phase breakdown for
            R09, R10, Pure MC, S02, and S01 workloads.
- bench_a : Pure Monte Carlo path generation (GBM / MBB, CPU / GPU probe) &
            vectorized risk statistics across 64..10000 paths and batch sizes.
- bench_b : Full Grid lifecycle simulation (R09 F11/F08 + R10 PaperStore WAL)
            across small, medium, and large workloads with financial semantics.
- bench_c : Multi-account parallel scaling across workers (1,4,8,12,16,20,24),
            measuring spawn overhead, pickle payload bytes, BLAS thread caps,
            memory, CPU utilization, speedup, and parallel efficiency.
- bench_d : Automatic strategy research (R10 Walk-Forward + funding sweep,
            S02 automatic.run, and S01 isolated protocol replay).

All benchmarks run 100% offline using checked-in repository fixtures/datasets.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor
import cProfile
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import io
import json
import math
from multiprocessing import get_context
import os
from pathlib import Path
import pickle
import platform
import pstats
import shutil
import statistics
import subprocess
import sys
import tempfile
from time import perf_counter, process_time
from typing import Any

import numpy as np
import pandas as pd
import psutil

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src"
for _p in (str(SRC_ROOT), str(REPO_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from factorlab.compute import ComputeConfig, ComputeUnavailableError
from factorlab.dataset.identity import file_hash
from factorlab.experiments.grid_f11_f08_offline import (
    DEFAULT_DEMO_WALLET,
    DEFAULT_STRATEGY_CAP,
    DemoGuardrails,
    FundingSchedule,
    default_demo_rules,
    execution_profile,
    run_offline_lifecycle,
)
from factorlab.experiments.grid_funding_adaptive import (
    DEMO_OBSERVED_HOURLY_FUNDING_BPS,
    NORMAL_PERP_HOURLY_FUNDING_BPS,
    FundingPolicy,
    VolatilityPolicy,
    load_funding_csv,
    run_paired_window,
    run_paired_window_multi_overlay,
)
from factorlab.experiments.grid_monte_carlo import close_bars
from factorlab.experiments.grid_r10_study import (
    HISTORICAL_REPLAY_CAPITAL,
    build_nonoverlapping_weekly_windows,
    load_strategy_configs,
    precompute_ac_multi_regime,
    run_expanding_walk_forward,
    run_funding_sensitivity_sweep,
)
from factorlab.experiments.provenance import capture_provenance
from factorlab.research.automatic import run as run_s02_automatic, write_json
from factorlab.research.monte_carlo import (
    MonteCarloConfig,
    MonteCarloInput,
    MonteCarloRunner,
    _gpu_probe,
)
from factorlab.research.monte_carlo.reliability import summarize_paths
from factorlab.research.protocol_strategy import run_protocol_research
from factorlab.strategies.loader import LoadedStrategy
from factorlab.trading.contracts import Market
from factorlab.trading.control import RiskLimits, TradingController
from factorlab.trading.paper import Instrument, PaperBroker, Quote
from factorlab.trading.storage import PaperStore
from factorlab.trading.strategy import ClosedBar

D = Decimal
BENCH_START_ISO = "2026-08-13T00:00:00+00:00"
BENCH_ANCHOR_PRICE = 132.89
BENCH_SEED = 909


# ---------------------------------------------------------------------------
# Environment & Helpers
# ---------------------------------------------------------------------------


def _set_single_thread_blas_env() -> None:
    for var in (
        "OMP_NUM_THREADS",
        "OPENBLAS_NUM_THREADS",
        "MKL_NUM_THREADS",
        "VECLIB_MAXIMUM_THREADS",
        "NUMEXPR_NUM_THREADS",
    ):
        os.environ[var] = "1"


def _worker_init_single_thread() -> None:
    _set_single_thread_blas_env()


def _worker_init_noop() -> None:
    pass


def _probe_gpu_hardware() -> dict[str, Any]:
    info: dict[str, Any] = {"nvidia_smi_available": False}
    try:
        proc = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,memory.total,memory.free,driver_version,utilization.gpu",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            parts = [p.strip() for p in proc.stdout.strip().splitlines()[0].split(",")]
            info.update(
                {
                    "nvidia_smi_available": True,
                    "name": parts[0] if len(parts) > 0 else None,
                    "memory_total_mb": int(parts[1]) if len(parts) > 1 else None,
                    "memory_free_mb": int(parts[2]) if len(parts) > 2 else None,
                    "driver_version": parts[3] if len(parts) > 3 else None,
                    "utilization_gpu_pct": int(parts[4]) if len(parts) > 4 else None,
                }
            )
    except Exception as exc:
        info["nvidia_smi_error"] = str(exc)

    cupy_status = _gpu_probe(ComputeConfig(device="gpu"))
    info["cupy_probe"] = asdict(cupy_status)
    return info


def collect_environment() -> dict[str, Any]:
    mem = psutil.virtual_memory()
    try:
        commit = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        ).stdout.strip()
        dirty = bool(
            subprocess.run(
                ["git", "-C", str(REPO_ROOT), "status", "--porcelain"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            ).stdout.strip()
        )
    except Exception:
        commit, dirty = None, None
    return {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "platform": platform.platform(),
        "python_version": platform.python_version(),
        "python_executable": sys.executable,
        "cpu_physical_cores": psutil.cpu_count(logical=False),
        "cpu_logical_processors": psutil.cpu_count(logical=True),
        "ram_total_gb": round(mem.total / (1024**3), 2),
        "ram_available_gb": round(mem.available / (1024**3), 2),
        "numpy_version": np.__version__,
        "pandas_version": pd.__version__,
        "gpu": _probe_gpu_hardware(),
        "git_commit": commit,
        "git_dirty": dirty,
        "blas_env": {
            k: os.environ.get(k)
            for k in (
                "OMP_NUM_THREADS",
                "OPENBLAS_NUM_THREADS",
                "MKL_NUM_THREADS",
                "NUMEXPR_NUM_THREADS",
            )
        },
    }


def _summarize_runs(times: list[float]) -> dict[str, Any]:
    warm = times[1:] if len(times) > 1 else times
    return {
        "all_runs_s": [round(t, 6) for t in times],
        "cold_s": round(times[0], 6),
        "warm_median_s": round(statistics.median(warm), 6),
        "median_s": round(statistics.median(times), 6),
        "min_s": round(min(times), 6),
        "max_s": round(max(times), 6),
    }


def _load_offline_r04b_r07_bars_and_funding() -> (
    tuple[list[dict[str, Any]], list[tuple[datetime, Decimal, Decimal, str]], np.ndarray]
):
    """Build deterministic continuous 1h bars from checked-in R04B + R07 CSV artifacts."""
    r04b_csv = REPO_ROOT / "docs" / "validation" / "p7-r09" / "R04B-base-baseline-hourly.csv"
    r07_csv = REPO_ROOT / "docs" / "validation" / "p7-r09" / "R07-base-baseline-hourly.csv"
    r04b_fund = REPO_ROOT / "docs" / "validation" / "p7-r09" / "R04B-base-baseline-funding-settlements.csv"
    r07_fund = REPO_ROOT / "docs" / "validation" / "p7-r09" / "R07-base-baseline-funding-settlements.csv"

    df1 = pd.read_csv(r04b_csv)
    df2 = pd.read_csv(r07_csv)
    df = pd.concat([df1[["date", "close"]], df2[["date", "close"]]], ignore_index=True)
    times = pd.to_datetime(df["date"], utc=True)
    closes = df["close"].astype(float).to_numpy()
    bars: list[dict[str, Any]] = []
    for idx, (t, c) in enumerate(zip(times, closes)):
        bars.append(
            {
                "id": f"hour-{idx}",
                "time": t.isoformat(),
                "price": float(c),
                "high": float(round(c * 1.003, 4)),
                "low": float(round(c * 0.997, 4)),
                "close": float(c),
                "volume": 5000,
                "completed_bar": True,
            }
        )
    f1 = load_funding_csv(r04b_fund)
    f2 = load_funding_csv(r07_fund)
    dedup: dict[tuple[datetime, str], tuple[datetime, Decimal, Decimal, str]] = {}
    for ev in f1 + f2:
        dedup[(ev[0], ev[3])] = ev
    funding = sorted(dedup.values(), key=lambda x: x[0])
    return bars, funding, closes


def _generate_fixed_price_paths(
    n_paths: int,
    n_steps: int = 168,
    seed: int = BENCH_SEED,
    *,
    anchor_price: float = BENCH_ANCHOR_PRICE,
    vol_1h: float = 0.0055,
    drift_1h: float = 0.0,
) -> np.ndarray:
    inp = MonteCarloInput(
        weights=[1.0],
        drift=[drift_1h],
        volatility=[vol_1h],
        assets=("NVDAUSDT",),
        initial_prices=[anchor_price],
    )
    cfg = MonteCarloConfig(
        model="gbm",
        n_paths=n_paths,
        n_steps=n_steps,
        seed=seed,
        dt=1.0,
        batch_size=min(256, n_paths),
        save_full_paths=True,
        compute=ComputeConfig(),
    )
    res = MonteCarloRunner(cfg).run(inp)
    return res.asset_paths[:, :, 0].copy()


# ---------------------------------------------------------------------------
# Process-Pool Workers (top-level picklable for Windows spawn)
# ---------------------------------------------------------------------------


def _bench_r09_worker(payload: tuple[Any, ...]) -> dict[str, Any]:
    prices, start_iso, guardrails, assumptions, funding, cap, tag = payload
    bars = close_bars(prices, start_iso)
    res = run_offline_lifecycle(
        bars,
        rules=default_demo_rules(),
        guardrails=guardrails,
        assumptions=assumptions,
        funding=funding,
        opening_wallet=DEFAULT_DEMO_WALLET,
        strategy_cap_usdt=cap,
        interval="1h",
        instrument_id="NVDAUSDT",
        account_id=f"perf-{tag}",
        family_prefix="fl-perf",
        store_path=":memory:",
    )
    return {
        "tag": tag,
        "net_pnl_usdt": res["net_pnl_usdt"],
        "net_return_pct": res["net_return_pct"],
        "loss_fraction": res["loss_fraction"],
        "max_drawdown": res["max_drawdown"],
        "fees_usdt": res["fees_usdt"],
        "slippage_usdt": res["slippage_usdt"],
        "realized_funding_usdt": res["realized_funding_usdt"],
        "fills_total": res["fills_total"],
        "orders_total": res["orders_total"],
        "grid_cycles": res["grid_cycles"],
        "rotations_completed": res["rotations_completed"],
        "final_equity_usdt": res["final_equity_usdt"],
        "residual_inventory": res["residual_inventory"],
        "reconciled_zero_residual": res["reconciled_zero_residual"],
        "final_state": res["final_state"],
    }


def _bench_r10_worker(payload: tuple[Any, ...]) -> dict[str, Any]:
    tmp_root, tag, w_bars, warmup_closes, strat_id, fp, vp, f_events, cap, prov = payload
    out_dir = Path(tmp_root) / tag
    res = run_paired_window(
        out_dir,
        strategy_id=strat_id,
        bars=w_bars,
        capital=cap,
        funding_events=f_events,
        funding_policy=fp,
        vol_policy=vp,
        warmup_closes=warmup_closes,
        funding_scale=1.0,
        provenance=prov,
        account_id=f"perf-r10-{tag}",
        cleanup_dir=True,
    )
    return {
        "tag": tag,
        "strategy_id": strat_id,
        "trade_net_pnl_usdt": res["trade_net_pnl_usdt"],
        "funding_pnl_usdt": res["funding_pnl_usdt"],
        "final_net_pnl_usdt": res["final_net_pnl_usdt"],
        "fees_usdt": res["fees_usdt"],
        "slippage_usdt": res["slippage_usdt"],
        "max_drawdown_with_funding_pct": res["max_drawdown_with_funding_pct"],
        "fills_count": res["fills_count"],
        "submitted_orders": res["submitted_orders"],
        "grid_cycles": res["grid_cycles"],
        "residual_inventory": res["residual_inventory"],
        "fifo_reconciliation_residual": res["fifo_reconciliation_residual"],
        "final_state": res["final_state"],
    }


def _noop_worker(x: int) -> int:
    return x


# ---------------------------------------------------------------------------
# Profiling Suite
# ---------------------------------------------------------------------------


def _extract_top_profile(pr: cProfile.Profile, limit: int = 20) -> dict[str, Any]:
    ps = pstats.Stats(pr)
    by_cum = []
    for func, (cc, nc, tt, ct, _) in sorted(ps.stats.items(), key=lambda kv: kv[1][3], reverse=True)[:limit]:
        by_cum.append(
            {
                "func": f"{Path(func[0]).name}:{func[1]}:{func[2]}",
                "calls": nc,
                "prim_calls": cc,
                "tottime_s": round(tt, 6),
                "cumtime_s": round(ct, 6),
            }
        )
    by_self = []
    for func, (cc, nc, tt, ct, _) in sorted(ps.stats.items(), key=lambda kv: kv[1][2], reverse=True)[:limit]:
        by_self.append(
            {
                "func": f"{Path(func[0]).name}:{func[1]}:{func[2]}",
                "calls": nc,
                "prim_calls": cc,
                "tottime_s": round(tt, 6),
                "cumtime_s": round(ct, 6),
            }
        )
    return {"top_cumtime": by_cum, "top_tottime": by_self}


def _instrument_paper_store_counts(fn) -> tuple[Any, dict[str, Any]]:
    """Wrap PaperStore methods temporarily to count SQLite transactions, commits, loads, saves."""
    orig_init = PaperStore.__init__
    orig_load = PaperStore.load
    orig_save = PaperStore.save
    orig_record_meta = PaperStore.record_metadata
    orig_order_events = PaperStore.order_events
    orig_funding_events = PaperStore.funding_events

    counters = {
        "store_opens": 0,
        "transactions": 0,
        "commits": 0,
        "commit_seconds": 0.0,
        "loads": 0,
        "load_seconds": 0.0,
        "saves": 0,
        "save_seconds": 0.0,
        "record_metadata_calls": 0,
        "order_events_scans": 0,
        "funding_events_scans": 0,
        "max_snapshot_bytes": 0,
    }
    stores: list[PaperStore] = []

    def wrapped_init(self, *a, **kw):
        counters["store_opens"] += 1
        orig_init(self, *a, **kw)
        stores.append(self)

    def wrapped_load(self):
        t0 = perf_counter()
        res = orig_load(self)
        counters["load_seconds"] += perf_counter() - t0
        counters["loads"] += 1
        return res

    def wrapped_save(self, state):
        t0 = perf_counter()
        res = orig_save(self, state)
        counters["save_seconds"] += perf_counter() - t0
        counters["saves"] += 1
        try:
            row = self.db.execute("SELECT length(payload) FROM snapshot WHERE id=1").fetchone()
            if row and row[0]:
                counters["max_snapshot_bytes"] = max(counters["max_snapshot_bytes"], int(row[0]))
        except Exception:
            pass
        return res

    def wrapped_record_meta(self, *a, **kw):
        counters["record_metadata_calls"] += 1
        return orig_record_meta(self, *a, **kw)

    def wrapped_order_events(self):
        counters["order_events_scans"] += 1
        return orig_order_events(self)

    def wrapped_funding_events(self):
        counters["funding_events_scans"] += 1
        return orig_funding_events(self)

    PaperStore.__init__ = wrapped_init
    PaperStore.load = wrapped_load
    PaperStore.save = wrapped_save
    PaperStore.record_metadata = wrapped_record_meta
    PaperStore.order_events = wrapped_order_events
    PaperStore.funding_events = wrapped_funding_events
    try:
        out = fn()
    finally:
        for s in stores:
            counters["transactions"] += s.transaction_count
            counters["commits"] += s.commit_count
            counters["commit_seconds"] += s.commit_seconds
        PaperStore.__init__ = orig_init
        PaperStore.load = orig_load
        PaperStore.save = orig_save
        PaperStore.record_metadata = orig_record_meta
        PaperStore.order_events = orig_order_events
        PaperStore.funding_events = orig_funding_events
    counters["commit_seconds"] = round(counters["commit_seconds"], 6)
    counters["load_seconds"] = round(counters["load_seconds"], 6)
    counters["save_seconds"] = round(counters["save_seconds"], 6)
    return out, counters


def _prepare_s01_grid_package(pkg_dir: Path) -> Path:
    pkg_dir.mkdir(parents=True, exist_ok=True)
    code = (
        "from factorlab.strategies.adapters import GridAdapter\n"
        "\n"
        "class BenchGridStrategy(GridAdapter):\n"
        "    pass\n"
    )
    (pkg_dir / "strategy.py").write_text(code, encoding="utf-8")
    manifest = {
        "file": "strategy.py",
        "class": "BenchGridStrategy",
        "version": "1",
        "parameters": {
            "config": {
                "mode": "arithmetic",
                "lower": 131.56,
                "upper": 134.22,
                "levels": 1,
                "initial_cash": 1000.0,
                "initial_inventory": 0,
                "initial_price": 132.89,
                "quantity": 1,
                "max_inventory": 1,
                "max_cash_committed": 10.0,
                "atr_window": 3,
                "atr_multiplier": 1.0,
                "rebuild_threshold": 2.0,
                "min_rebuild_interval": 3,
                "max_turnover": 10.0,
                "breakout": "pause",
                "max_drawdown": 0.2,
                "minimum_price": 0.01,
                "minimum_quantity": 1,
                "fee_bps": 3.0,
                "slippage_bps": 2.0,
                "order_ttl": 100,
                "center_rule": "last_close",
                "expiry_reseed_limit": 0,
                "expiry_reseed_interval": 100,
            },
            "rules": {
                "quantity_unit": "0.04",
                "step_size": "0.01",
                "tick_size": "0.01",
                "min_quantity": "0.01",
                "min_notional": "5",
            },
            "instrument_id": "NVDAUSDT",
            "initial_equity": "1000",
            "market": "BINANCE_USDM",
            "interval_seconds": 3600,
        },
    }
    (pkg_dir / "strategy.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return pkg_dir


def _run_s01_replay_once(
    n_bars: int = 8,
    *,
    persistent_worker: bool = False,
    verify_runtime_each_call: bool = True,
) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="perf-s01-") as tmp:
        root = Path(tmp)
        pkg = _prepare_s01_grid_package(root / "pkg")
        t_load = perf_counter()
        strategy = LoadedStrategy(
            pkg,
            trusted=True,
            persistent_worker=persistent_worker,
            verify_runtime_each_call=verify_runtime_each_call,
        )
        load_s = perf_counter() - t_load

        base = datetime(2026, 8, 13, 0, 0, tzinfo=timezone.utc)
        clock_now = [base]
        broker = PaperBroker(
            Market.BINANCE_USDM,
            "s01-perf",
            (Instrument("NVDAUSDT", "USDT"),),
            {"USDT": D("1000")},
            fee_rate=D("0.0003"),
        )
        store = PaperStore(root / "paper.sqlite", research_wal=True)
        try:
            controller = TradingController(
                broker,
                store,
                clock=lambda: clock_now[0],
                limits=RiskLimits(D("50"), D("50"), D("50"), orders_per_minute=20),
            )
            prices = [D("132.89"), D("132.20"), D("131.50"), D("132.40"), D("133.50"), D("134.30"), D("133.00"), D("132.50")]
            while len(prices) < n_bars:
                prices.append(prices[len(prices) % 8])
            events = []
            for i in range(n_bars):
                opened = base + timedelta(hours=i)
                closed = opened + timedelta(hours=1)
                px = prices[i]
                bar = ClosedBar(
                    "NVDAUSDT",
                    opened,
                    closed,
                    px,
                    px + D("0.20"),
                    px - D("0.20"),
                    px,
                    D("500"),
                    3600,
                    "SYNTHETIC",
                )
                events.append(("closed_bar", bar))
                events.append(("quote", Quote("NVDAUSDT", px, closed + timedelta(seconds=1))))

            # Advance clock dynamically per event
            t_run = perf_counter()
            from factorlab.strategies.decision_runtime import DecisionJournal, OrderStrategyRuntime

            journal = DecisionJournal(
                strategy,
                controller,
                deployment_id="s01perf",
                symbol="NVDAUSDT",
                interval_seconds=3600,
                data_types=["closed_bar", "quote", "order_event"],
            )
            runner = OrderStrategyRuntime(journal)
            state_sizes = []
            decide_calls = 0
            for kind, val in events:
                if kind == "closed_bar":
                    clock_now[0] = val.closed_at
                    runner.recover()
                    runner.closed_bar(val)
                    decide_calls += 1
                elif kind == "quote":
                    clock_now[0] = val.observed_at
                    runner.recover()
                    runner.quote(val)
                    decide_calls += 1
                latest = journal._latest()
                state_sizes.append(len(json.dumps(latest)))
            run_s = perf_counter() - t_run
            return {
                "n_bars": n_bars,
                "persistent_worker": persistent_worker,
                "verify_runtime_each_call": verify_runtime_each_call,
                "load_strategy_s": round(load_s, 6),
                "replay_s": round(run_s, 6),
                "total_s": round(load_s + run_s, 6),
                "decide_events": decide_calls,
                "ms_per_decide_event": round(run_s * 1000.0 / max(1, decide_calls), 2),
                "final_state_json_bytes": state_sizes[-1] if state_sizes else 0,
                "max_state_json_bytes": max(state_sizes) if state_sizes else 0,
                "sqlite_transactions": store.transaction_count,
                "orders_created": len(broker.state.orders),
            }
        finally:
            strategy.close()
            store.close()


def run_profile_suite() -> dict[str, Any]:
    print("[PROFILE] Running hotspot profiling across R09, R10, Pure MC, S02, S01...", flush=True)
    out: dict[str, Any] = {}
    all_bars, all_funding, _ = _load_offline_r04b_r07_bars_and_funding()
    _, b_policies, c_policies = load_strategy_configs(REPO_ROOT)
    windows = build_nonoverlapping_weekly_windows(
        all_bars,
        first_start=datetime(2026, 5, 15, 0, 0, tzinfo=timezone.utc),
        window_hours=168,
        warmup_hours=96,
    )
    prov = capture_provenance(REPO_ROOT)

    # 1. R09 profile (4 paths x 168h, Model B IDEAL + DEMO_STRESS_22.97BPS)
    paths_4 = _generate_fixed_price_paths(4, 168, seed=BENCH_SEED)
    gr_b = DemoGuardrails(enable_f11=True, enable_f08=True)
    prof_ideal = execution_profile("IDEAL_LIMIT_FILL")
    fs_stress = FundingSchedule("constant_hourly_bps", DEMO_OBSERVED_HOURLY_FUNDING_BPS, label="DEMO_STRESS_22.97BPS")

    def _run_r09_4():
        res_list = []
        for i in range(4):
            bars = close_bars(paths_4[i], BENCH_START_ISO)
            res_list.append(
                run_offline_lifecycle(
                    bars,
                    rules=default_demo_rules(),
                    guardrails=gr_b,
                    assumptions=prof_ideal,
                    funding=fs_stress,
                    opening_wallet=DEFAULT_DEMO_WALLET,
                    strategy_cap_usdt=DEFAULT_STRATEGY_CAP,
                    interval="1h",
                    instrument_id="NVDAUSDT",
                    account_id=f"prof-r09-{i}",
                    family_prefix="fl-prof",
                    store_path=":memory:",
                )
            )
        return res_list

    pr = cProfile.Profile()
    t0 = perf_counter()
    pr.enable()
    r09_res, r09_sql = _instrument_paper_store_counts(_run_r09_4)
    pr.disable()
    r09_s = perf_counter() - t0
    out["r09_4paths_168h_model_b"] = {
        "wall_s": round(r09_s, 6),
        "per_path_s": round(r09_s / 4, 6),
        "sqlite": r09_sql,
        "total_orders": sum(r["orders_total"] for r in r09_res),
        "total_fills": sum(r["fills_total"] for r in r09_res),
        "total_rotations": sum(r["rotations_completed"] for r in r09_res),
        "total_funding_events": sum(r["funding_events_count"] for r in r09_res),
        "profile": _extract_top_profile(pr, limit=18),
    }

    # 2. R10 profile (3 arms A, B5, C5 on 2 historical windows, file-backed WAL SQLite)
    b5 = next(p for p in b_policies if p.candidate_id == "B5")
    c5 = next(p for p in c_policies if p.candidate_id == "C5")

    def _run_r10_6():
        results = []
        with tempfile.TemporaryDirectory(prefix="prof-r10-") as tmp:
            for w in windows[:2]:
                for sid, fp, vp in (("A_FIXED", None, None), ("B5", b5, None), ("C5", None, c5)):
                    results.append(
                        run_paired_window(
                            Path(tmp) / f"{w['window_id']}_{sid}",
                            strategy_id=sid,
                            bars=w["bars"],
                            capital=HISTORICAL_REPLAY_CAPITAL,
                            funding_events=all_funding,
                            funding_policy=fp,
                            vol_policy=vp,
                            warmup_closes=w["warmup_closes"],
                            provenance=prov,
                            account_id=f"prof-r10-{w['window_id']}-{sid}",
                            cleanup_dir=True,
                        )
                    )
        return results

    pr = cProfile.Profile()
    t0 = perf_counter()
    pr.enable()
    r10_res, r10_sql = _instrument_paper_store_counts(_run_r10_6)
    pr.disable()
    r10_s = perf_counter() - t0
    out["r10_6accounts_168h_wal"] = {
        "wall_s": round(r10_s, 6),
        "per_account_s": round(r10_s / 6, 6),
        "sqlite": r10_sql,
        "total_fills": sum(r["fills_count"] for r in r10_res),
        "profile": _extract_top_profile(pr, limit=18),
    }

    # 3. Pure MC + reliability profile (10000 x 252 GBM + summarize_paths)
    inp = MonteCarloInput(weights=[1.0], drift=[0.08], volatility=[0.25], assets=("NVDAUSDT",), initial_prices=[132.89])
    cfg = MonteCarloConfig(model="gbm", n_paths=10000, n_steps=252, seed=42, batch_size=4096, save_full_paths=False)
    pr = cProfile.Profile()
    t0 = perf_counter()
    pr.enable()
    mc_res = MonteCarloRunner(cfg).run(inp)
    losses = 1.0 - mc_res.terminal_values / cfg.initial_value
    _ = summarize_paths(losses, mc_res.maximum_drawdowns, resamples=999, bootstrap_seed=42)
    pr.disable()
    mc_s = perf_counter() - t0
    out["pure_mc_10000x252_plus_summarize"] = {
        "wall_s": round(mc_s, 6),
        "profile": _extract_top_profile(pr, limit=15),
    }

    # 4. S02 automatic.run profile
    from examples.research.champion_demo import prepare as prepare_s02_demo

    with tempfile.TemporaryDirectory(prefix="prof-s02-") as tmp:
        root = Path(tmp) / "demo"
        cfg_path = prepare_s02_demo(root)
        pr = cProfile.Profile()
        t0 = perf_counter()
        pr.enable()
        s02_rep = run_s02_automatic(
            cfg_path,
            output=root / "run",
            store=root / "tracker",
            state_path=root / "champion.json",
            command=["benchmark_p8_perf01"],
        )
        pr.disable()
        s02_s = perf_counter() - t0
    out["s02_automatic_6candidates"] = {
        "wall_s": round(s02_s, 6),
        "reason": s02_rep["reason"],
        "selected": s02_rep["selected"]["candidate_id"],
        "profile": _extract_top_profile(pr, limit=15),
    }

    # 5. S01 isolated protocol replay profile (6 bars = 12 decide events)
    pr = cProfile.Profile()
    t0 = perf_counter()
    pr.enable()
    s01_metrics = _run_s01_replay_once(n_bars=6)
    pr.disable()
    out["s01_isolated_grid_6bars"] = {
        "metrics": s01_metrics,
        "profile": _extract_top_profile(pr, limit=15),
    }

    # 6. Measure IPC pickle sizes for representative R09 and R10 payloads
    r09_sample_payload = (
        paths_4[0],
        BENCH_START_ISO,
        gr_b,
        prof_ideal,
        fs_stress,
        DEFAULT_STRATEGY_CAP,
        "sample-0",
    )
    r10_sample_payload = (
        "/tmp/x",
        "W01_B5",
        windows[0]["bars"],
        windows[0]["warmup_closes"],
        "B5",
        b5,
        None,
        all_funding,
        HISTORICAL_REPLAY_CAPITAL,
        prov,
    )
    out["ipc_pickle_bytes"] = {
        "r09_path_payload_bytes": len(pickle.dumps(r09_sample_payload)),
        "r09_1000_tasks_mb": round(len(pickle.dumps(r09_sample_payload)) * 1000 / (1024**2), 3),
        "r10_window_payload_bytes_with_all_funding": len(pickle.dumps(r10_sample_payload)),
        "r10_1000_tasks_mb": round(len(pickle.dumps(r10_sample_payload)) * 1000 / (1024**2), 3),
        "all_funding_list_bytes": len(pickle.dumps(all_funding)),
        "window_bars_168_bytes": len(pickle.dumps(windows[0]["bars"])),
        "provenance_bytes": len(pickle.dumps(prov)),
    }
    return out


# ---------------------------------------------------------------------------
# BENCH-A: Pure Monte Carlo Path Generation & Risk Statistics
# ---------------------------------------------------------------------------


def run_bench_a(repeats: int = 3) -> dict[str, Any]:
    print(f"[BENCH-A] Running pure MC & risk statistics (repeats={repeats})...", flush=True)
    _, _, closes = _load_offline_r04b_r07_bars_and_funding()
    rets = (closes[1:] / closes[:-1] - 1.0)[:, None]
    logs = np.log1p(rets[:, 0])
    vol = float(logs.std(ddof=1))
    drift = float(logs.mean() + 0.5 * vol**2)
    ts = np.datetime64("2026-05-14T01:00:00", "ns") + np.arange(len(rets)) * np.timedelta64(1, "h")

    inp_1a = MonteCarloInput(
        weights=[1.0],
        drift=[drift],
        volatility=[vol],
        historical_returns=rets,
        timestamps=ts,
        available_at=ts,
        as_of=ts[-1],
        assets=("NVDAUSDT",),
        initial_prices=[BENCH_ANCHOR_PRICE],
    )
    corr5 = np.full((5, 5), 0.35)
    np.fill_diagonal(corr5, 1.0)
    inp_5a = MonteCarloInput(
        weights=[0.2, 0.2, 0.2, 0.2, 0.2],
        drift=[0.05, 0.06, 0.04, 0.07, 0.03],
        volatility=[0.20, 0.25, 0.18, 0.30, 0.22],
        correlation=corr5,
        assets=("A1", "A2", "A3", "A4", "A5"),
        initial_prices=[100.0, 120.0, 80.0, 150.0, 90.0],
    )

    scales = [64, 256, 1024, 4096, 10000]
    rows: list[dict[str, Any]] = []

    for model, n_steps, inp_label, inp in (
        ("gbm", 168, "1asset_168h", inp_1a),
        ("moving_block_bootstrap", 168, "1asset_168h", inp_1a),
        ("gbm", 252, "5asset_252d", inp_5a),
    ):
        for n_paths in scales:
            batch_candidates = [b for b in (64, 256, 1024, 4096) if b <= n_paths]
            if n_paths not in batch_candidates and n_paths < 4096:
                batch_candidates.append(n_paths)
            # Keep matrix focused: test default (None -> min(4096, n_paths)) plus 64 and 1024 when distinct
            test_batches: list[int | None] = [None]
            if n_paths >= 1024:
                test_batches.append(64)
            for batch_size in test_batches:
                for save_paths in (False, True) if (batch_size is None and inp_label == "1asset_168h") else (False,):
                    run_times = []
                    stat_times = []
                    last_res = None
                    for _ in range(repeats):
                        cfg = MonteCarloConfig(
                            model=model,
                            n_paths=n_paths,
                            n_steps=n_steps,
                            seed=42,
                            dt=1.0 if n_steps == 168 else 1 / 252,
                            batch_size=batch_size,
                            block_size=24,
                            save_full_paths=save_paths,
                            compute=ComputeConfig(device="cpu"),
                        )
                        t0 = perf_counter()
                        res = MonteCarloRunner(cfg).run(inp)
                        t_gen = perf_counter() - t0
                        run_times.append(t_gen)
                        last_res = res

                        if not save_paths and batch_size is None:
                            losses = 1.0 - res.terminal_values / cfg.initial_value
                            t1 = perf_counter()
                            _ = summarize_paths(losses, res.maximum_drawdowns, resamples=499, bootstrap_seed=42)
                            stat_times.append(perf_counter() - t1)

                    gen_summary = _summarize_runs(run_times)
                    med = gen_summary["median_s"]
                    row: dict[str, Any] = {
                        "backend": "numpy-cpu",
                        "model": model,
                        "scenario": inp_label,
                        "n_paths": n_paths,
                        "n_steps": n_steps,
                        "batch_size": batch_size if batch_size is not None else min(4096, n_paths),
                        "save_full_paths": save_paths,
                        "generation": gen_summary,
                        "paths_per_sec_median": round(n_paths / med, 1) if med > 0 else None,
                        "var_95": round(float(last_res.var), 8),
                        "cvar_95": round(float(last_res.cvar), 8),
                        "loss_prob": round(float(last_res.loss_probability), 6),
                        "mean_terminal": round(float(last_res.terminal_values.mean()), 8),
                    }
                    if stat_times:
                        row["summarize_paths_499boot"] = _summarize_runs(stat_times)
                    rows.append(row)

    # Probe GPU CUDA path explicitly
    gpu_result: dict[str, Any] = {"hardware": _probe_gpu_hardware()}
    try:
        gpu_cfg = MonteCarloConfig(
            model="gbm", n_paths=10000, n_steps=252, seed=42, compute=ComputeConfig(device="gpu")
        )
        t0 = perf_counter()
        gres = MonteCarloRunner(gpu_cfg).run(inp_5a)
        gpu_result["executed"] = True
        gpu_result["total_s"] = round(perf_counter() - t0, 6)
        gpu_result["metadata"] = gres.metadata["compute"]
    except ComputeUnavailableError as exc:
        gpu_result["executed"] = False
        gpu_result["unavailable_reason"] = str(exc)

    return {
        "suite": "BENCH-A",
        "repeats": repeats,
        "cpu_results": rows,
        "gpu_probe_and_execution": gpu_result,
    }


# ---------------------------------------------------------------------------
# BENCH-B: Full Grid Lifecycle Simulation (R09 + R10)
# ---------------------------------------------------------------------------


def _run_r09_batch(
    paths: np.ndarray,
    arms: list[tuple[str, DemoGuardrails, Any, FundingSchedule]],
    workers: int,
) -> tuple[float, list[dict[str, Any]]]:
    tasks = []
    for arm_name, gr, prof, fs in arms:
        for i in range(len(paths)):
            tasks.append((paths[i], BENCH_START_ISO, gr, prof, fs, DEFAULT_STRATEGY_CAP, f"{arm_name}-{i}"))
    t0 = perf_counter()
    if workers <= 1:
        res = [_bench_r09_worker(t) for t in tasks]
    else:
        with ProcessPoolExecutor(
            max_workers=workers,
            mp_context=get_context("spawn"),
            initializer=_worker_init_single_thread,
        ) as pool:
            res = list(pool.map(_bench_r09_worker, tasks))
    return perf_counter() - t0, res


def _summarize_r09_semantics(results: list[dict[str, Any]]) -> dict[str, Any]:
    losses = np.asarray([r["loss_fraction"] for r in results], dtype=float)
    dds = np.asarray([r["max_drawdown"] for r in results], dtype=float)
    rel = summarize_paths(losses, dds, threshold=0.05, confidences=(0.95,), resamples=199, bootstrap_seed=BENCH_SEED)
    return {
        "accounts": len(results),
        "all_zero_fifo_residual": all(r["reconciled_zero_residual"] for r in results),
        "sum_net_pnl_usdt": round(sum(r["net_pnl_usdt"] for r in results), 8),
        "sum_fees_usdt": round(sum(r["fees_usdt"] for r in results), 8),
        "sum_slippage_usdt": round(sum(r["slippage_usdt"] for r in results), 8),
        "sum_funding_usdt": round(sum(r["realized_funding_usdt"] for r in results), 8),
        "total_orders": sum(r["orders_total"] for r in results),
        "total_fills": sum(r["fills_total"] for r in results),
        "total_grid_cycles": sum(r["grid_cycles"] for r in results),
        "total_rotations": sum(r["rotations_completed"] for r in results),
        "mean_max_dd": round(float(np.mean(dds)), 8),
        "var_95": round(float(rel["tails"]["0.95"]["var"]), 8),
        "cvar_95": round(float(rel["tails"]["0.95"]["cvar"]), 8),
        "loss_prob": round(float(rel["loss_probability"]["estimate"]), 6),
    }


def _run_r10_batch(
    windows_subset: list[dict[str, Any]],
    all_funding: list[tuple[datetime, Decimal, Decimal, str]],
    b5: FundingPolicy,
    c5: VolatilityPolicy,
    prov: dict[str, Any],
    workers: int,
) -> tuple[float, list[dict[str, Any]]]:
    with tempfile.TemporaryDirectory(prefix="bench-b2-r10-") as tmp:
        tasks = []
        for w in windows_subset:
            for sid, fp, vp in (("A_FIXED", None, None), ("B5", b5, None), ("C5", None, c5)):
                tasks.append(
                    (
                        tmp,
                        f"{w['window_id']}_{sid}",
                        w["bars"],
                        w["warmup_closes"],
                        sid,
                        fp,
                        vp,
                        all_funding,
                        HISTORICAL_REPLAY_CAPITAL,
                        prov,
                    )
                )
        t0 = perf_counter()
        if workers <= 1:
            res = [_bench_r10_worker(t) for t in tasks]
        else:
            with ProcessPoolExecutor(
                max_workers=workers,
                mp_context=get_context("spawn"),
                initializer=_worker_init_single_thread,
            ) as pool:
                res = list(pool.map(_bench_r10_worker, tasks))
        elapsed = perf_counter() - t0
    return elapsed, res


def _summarize_r10_semantics(results: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "accounts": len(results),
        "all_zero_fifo_residual": all(abs(r["fifo_reconciliation_residual"]) < 1e-8 for r in results),
        "sum_trade_net_pnl_usdt": round(sum(r["trade_net_pnl_usdt"] for r in results), 8),
        "sum_funding_pnl_usdt": round(sum(r["funding_pnl_usdt"] for r in results), 8),
        "sum_final_net_pnl_usdt": round(sum(r["final_net_pnl_usdt"] for r in results), 8),
        "sum_fees_usdt": round(sum(r["fees_usdt"] for r in results), 8),
        "sum_slippage_usdt": round(sum(r["slippage_usdt"] for r in results), 8),
        "total_fills": sum(r["fills_count"] for r in results),
        "total_submitted_orders": sum(r["submitted_orders"] for r in results),
        "total_grid_cycles": sum(r["grid_cycles"] for r in results),
        "mean_max_dd_with_funding_pct": round(
            float(np.mean([r["max_drawdown_with_funding_pct"] for r in results])), 6
        ),
    }


def run_bench_b(repeats: int = 3, workers: int = 8) -> dict[str, Any]:
    print(f"[BENCH-B] Running full Grid lifecycle simulation (repeats={repeats}, workers={workers})...", flush=True)
    all_bars, all_funding, _ = _load_offline_r04b_r07_bars_and_funding()
    _, b_policies, c_policies = load_strategy_configs(REPO_ROOT)
    b5 = next(p for p in b_policies if p.candidate_id == "B5")
    c5 = next(p for p in c_policies if p.candidate_id == "C5")
    prov = capture_provenance(REPO_ROOT)
    windows = build_nonoverlapping_weekly_windows(
        all_bars,
        first_start=datetime(2026, 5, 15, 0, 0, tzinfo=timezone.utc),
        window_hours=168,
        warmup_hours=96,
    )

    fs_stress = FundingSchedule("constant_hourly_bps", DEMO_OBSERVED_HOURLY_FUNDING_BPS, label="DEMO_STRESS_22.97BPS")
    r09_arms = [
        ("A_BASELINE", DemoGuardrails(enable_f11=False, enable_f08=False), execution_profile("IDEAL_LIMIT_FILL"), fs_stress),
        ("B_F11_F08_IDEAL", DemoGuardrails(enable_f11=True, enable_f08=True), execution_profile("IDEAL_LIMIT_FILL"), fs_stress),
    ]

    # B1: R09 F11/F08 full lifecycle (small=8 paths/16 accts single-proc, medium=32 paths/64 accts, large=128 paths/256 accts)
    r09_tiers = [
        ("small_16accts_1w", 8, 1),
        ("medium_64accts_8w", 32, workers),
        ("large_256accts_8w", 128, workers),
    ]
    b1_rows = []
    for label, n_paths, w_cnt in r09_tiers:
        paths = _generate_fixed_price_paths(n_paths, 168, seed=BENCH_SEED)
        times = []
        last_res = None
        for _ in range(repeats):
            dt, res = _run_r09_batch(paths, r09_arms, w_cnt)
            times.append(dt)
            last_res = res
        summ = _summarize_runs(times)
        n_accts = n_paths * len(r09_arms)
        b1_rows.append(
            {
                "tier": label,
                "n_paths": n_paths,
                "n_accounts": n_accts,
                "workers": w_cnt,
                "timing": summ,
                "accounts_per_sec_median": round(n_accts / summ["median_s"], 2),
                "paths_per_sec_median": round(n_paths / summ["median_s"], 2),
                "semantics": _summarize_r09_semantics(last_res),
            }
        )

    # B2: R10 file-backed PaperStore WAL lifecycle (small=4w/12 accts 1w, medium=12w/36 accts, large=24w/72 accts)
    r10_tiers = [
        ("small_12accts_1w", 4, 1),
        ("medium_36accts_8w", 12, workers),
        ("large_72accts_8w", 24, workers),
    ]
    b2_rows = []
    for label, n_win, w_cnt in r10_tiers:
        w_sub = windows[:n_win]
        times = []
        last_res = None
        for _ in range(repeats):
            dt, res = _run_r10_batch(w_sub, all_funding, b5, c5, prov, w_cnt)
            times.append(dt)
            last_res = res
        summ = _summarize_runs(times)
        n_accts = n_win * 3
        b2_rows.append(
            {
                "tier": label,
                "n_windows": n_win,
                "n_accounts": n_accts,
                "workers": w_cnt,
                "timing": summ,
                "accounts_per_sec_median": round(n_accts / summ["median_s"], 2),
                "semantics": _summarize_r10_semantics(last_res),
            }
        )

    # Also measure disk-preserved R10 WAL mode (cleanup_dir=False) to separate
    # transaction/serialization speedup from temporary disk-elimination speedup.
    disk_times = []
    disk_last = None
    for _ in range(repeats):
        with tempfile.TemporaryDirectory(prefix="bench-b2-r10-disk-") as tmp:
            t0 = perf_counter()
            disk_res = []
            for w in windows[:4]:
                for sid, fp, vp in (("A_FIXED", None, None), ("B5", b5, None), ("C5", None, c5)):
                    disk_res.append(
                        run_paired_window(
                            Path(tmp) / f"{w['window_id']}_{sid}",
                            strategy_id=sid,
                            bars=w["bars"],
                            capital=HISTORICAL_REPLAY_CAPITAL,
                            funding_events=all_funding,
                            funding_policy=fp,
                            vol_policy=vp,
                            warmup_closes=w["warmup_closes"],
                            funding_scale=1.0,
                            provenance=prov,
                            account_id=f"perf-r10-disk-{w['window_id']}-{sid}",
                            cleanup_dir=False,
                        )
                    )
            disk_times.append(perf_counter() - t0)
            disk_last = disk_res
    disk_summ = _summarize_runs(disk_times)
    b2_disk_preserved = {
        "tier": "small_12accts_1w_disk_wal_preserved",
        "n_windows": 4,
        "n_accounts": 12,
        "workers": 1,
        "timing": disk_summ,
        "accounts_per_sec_median": round(12 / disk_summ["median_s"], 2),
        "semantics": _summarize_r10_semantics(disk_last),
    }

    return {
        "suite": "BENCH-B",
        "repeats": repeats,
        "r09_f11_f08_lifecycle": b1_rows,
        "r10_file_wal_lifecycle": b2_rows,
        "r10_disk_wal_preserved": b2_disk_preserved,
    }


# ---------------------------------------------------------------------------
# BENCH-C: Multi-Account Parallel Scaling
# ---------------------------------------------------------------------------


def run_bench_c(repeats: int = 3, worker_list: list[int] | None = None) -> dict[str, Any]:
    workers_to_test = worker_list or [1, 4, 8, 12, 16, 20, 24]
    print(f"[BENCH-C] Running multi-account scaling for workers={workers_to_test} (repeats={repeats})...", flush=True)

    # Measure spawn pool startup overhead
    spawn_overhead = []
    for w in workers_to_test:
        if w == 1:
            spawn_overhead.append({"workers": 1, "pool_startup_s": 0.0})
            continue
        ts = []
        for _ in range(2):
            t0 = perf_counter()
            with ProcessPoolExecutor(
                max_workers=w,
                mp_context=get_context("spawn"),
                initializer=_worker_init_single_thread,
            ) as pool:
                list(pool.map(_noop_worker, range(w)))
            ts.append(perf_counter() - t0)
        spawn_overhead.append({"workers": w, "pool_startup_s": round(statistics.median(ts), 4)})

    # C1: R09 64 accounts (32 paths x 2 arms)
    paths_32 = _generate_fixed_price_paths(32, 168, seed=BENCH_SEED)
    fs_stress = FundingSchedule("constant_hourly_bps", DEMO_OBSERVED_HOURLY_FUNDING_BPS, label="DEMO_STRESS_22.97BPS")
    r09_arms = [
        ("A_BASELINE", DemoGuardrails(enable_f11=False, enable_f08=False), execution_profile("IDEAL_LIMIT_FILL"), fs_stress),
        ("B_F11_F08_IDEAL", DemoGuardrails(enable_f11=True, enable_f08=True), execution_profile("IDEAL_LIMIT_FILL"), fs_stress),
    ]
    c1_rows = []
    t1_c1 = None
    ref_sem_c1 = None
    for w in workers_to_test:
        times = []
        peak_rss_mb = 0.0
        last_res = None
        for _ in range(repeats):
            proc = psutil.Process()
            dt, res = _run_r09_batch(paths_32, r09_arms, w)
            times.append(dt)
            last_res = res
            peak_rss_mb = max(peak_rss_mb, proc.memory_info().rss / (1024**2))
        summ = _summarize_runs(times)
        med = summ["median_s"]
        if w == 1:
            t1_c1 = med
            ref_sem_c1 = _summarize_r09_semantics(last_res)
        sem = _summarize_r09_semantics(last_res)
        speedup = (t1_c1 / med) if t1_c1 else 1.0
        c1_rows.append(
            {
                "workers": w,
                "n_accounts": 64,
                "timing": summ,
                "accounts_per_sec_median": round(64 / med, 2),
                "speedup": round(speedup, 3),
                "parallel_efficiency": round(speedup / w, 3),
                "parent_rss_mb": round(peak_rss_mb, 1),
                "semantics_match_1w": sem == ref_sem_c1,
            }
        )
        print(f"  [BENCH-C1 R09] workers={w} median={med:.2f}s speedup={speedup:.2f}x eff={speedup/w:.2f}", flush=True)

    # C2: R10 file-backed WAL 24 accounts (8 windows x 3 arms)
    all_bars, all_funding, _ = _load_offline_r04b_r07_bars_and_funding()
    _, b_policies, c_policies = load_strategy_configs(REPO_ROOT)
    b5 = next(p for p in b_policies if p.candidate_id == "B5")
    c5 = next(p for p in c_policies if p.candidate_id == "C5")
    prov = capture_provenance(REPO_ROOT)
    windows = build_nonoverlapping_weekly_windows(
        all_bars,
        first_start=datetime(2026, 5, 15, 0, 0, tzinfo=timezone.utc),
        window_hours=168,
        warmup_hours=96,
    )[:8]

    c2_rows = []
    t1_c2 = None
    ref_sem_c2 = None
    for w in workers_to_test:
        times = []
        last_res = None
        for _ in range(repeats):
            dt, res = _run_r10_batch(windows, all_funding, b5, c5, prov, w)
            times.append(dt)
            last_res = res
        summ = _summarize_runs(times)
        med = summ["median_s"]
        if w == 1:
            t1_c2 = med
            ref_sem_c2 = _summarize_r10_semantics(last_res)
        sem = _summarize_r10_semantics(last_res)
        speedup = (t1_c2 / med) if t1_c2 else 1.0
        c2_rows.append(
            {
                "workers": w,
                "n_accounts": 24,
                "timing": summ,
                "accounts_per_sec_median": round(24 / med, 2),
                "speedup": round(speedup, 3),
                "parallel_efficiency": round(speedup / w, 3),
                "semantics_match_1w": sem == ref_sem_c2,
            }
        )
        print(f"  [BENCH-C2 R10] workers={w} median={med:.2f}s speedup={speedup:.2f}x eff={speedup/w:.2f}", flush=True)

    return {
        "suite": "BENCH-C",
        "repeats": repeats,
        "spawn_overhead": spawn_overhead,
        "c1_r09_inmemory_64accts": c1_rows,
        "c2_r10_file_wal_24accts": c2_rows,
    }


# ---------------------------------------------------------------------------
# BENCH-D: Automatic Strategy Research & S01 Protocol Replay
# ---------------------------------------------------------------------------


def run_bench_d(repeats: int = 3, workers: int = 8) -> dict[str, Any]:
    print(f"[BENCH-D] Running automatic strategy research & S01 replay (repeats={repeats}, workers={workers})...", flush=True)
    all_bars, all_funding, _ = _load_offline_r04b_r07_bars_and_funding()
    _, b_policies, c_policies = load_strategy_configs(REPO_ROOT)
    prov = capture_provenance(REPO_ROOT)
    windows_12 = build_nonoverlapping_weekly_windows(
        all_bars,
        first_start=datetime(2026, 5, 15, 0, 0, tzinfo=timezone.utc),
        window_hours=168,
        warmup_hours=96,
    )[:12]

    # D1: R10 Walk-Forward (12 windows, 13 arms, 2 funding regimes + 8-regime funding sweep)
    d1_times = []
    d1_breakdown = {}
    d1_semantics = {}
    overlays = [
        ("mainnet_actual", 1.0, None, None),
        ("demo_stress", 1.0, DEMO_OBSERVED_HOURLY_FUNDING_BPS, None),
    ]
    for _ in range(repeats):
        t0 = perf_counter()
        from factorlab.compute import create_process_pool

        shared_pool = create_process_pool(workers) if workers > 1 else None
        try:
            t_ac = perf_counter()
            pre_ac = precompute_ac_multi_regime(
                all_bars,
                windows_12,
                all_funding,
                c_policies,
                overlays,
                initial_train_windows=6,
                step_windows=3,
                max_workers=workers,
                provenance=prov,
                executor=shared_pool,
            )
            ac_s = perf_counter() - t_ac
            t_wf1 = perf_counter()
            wf_m = run_expanding_walk_forward(
                all_bars,
                windows_12,
                all_funding,
                b_policies,
                c_policies,
                regime_label="mainnet_actual",
                funding_scale=1.0,
                initial_train_windows=6,
                step_windows=3,
                max_workers=workers,
                provenance=prov,
                precomputed_ac=pre_ac,
                executor=shared_pool,
            )
            wf1_s = perf_counter() - t_wf1
            t_wf2 = perf_counter()
            wf_d = run_expanding_walk_forward(
                all_bars,
                windows_12,
                all_funding,
                b_policies,
                c_policies,
                regime_label="demo_stress",
                constant_hourly_bps=DEMO_OBSERVED_HOURLY_FUNDING_BPS,
                initial_train_windows=6,
                step_windows=3,
                max_workers=workers,
                provenance=prov,
                precomputed_ac=pre_ac,
                executor=shared_pool,
            )
            wf2_s = perf_counter() - t_wf2
            t_sw = perf_counter()
            b2 = next(p for p in b_policies if p.candidate_id == "B2")
            b5 = next(p for p in b_policies if p.candidate_id == "B5")
            c5 = next(p for p in c_policies if p.candidate_id == "C5")
            sw_rows = run_funding_sensitivity_sweep(
                windows_12[:6],
                all_funding,
                b2,
                b5,
                c5,
                max_workers=workers,
                provenance=prov,
                executor=shared_pool,
            )
            sw_s = perf_counter() - t_sw
        finally:
            if shared_pool is not None:
                shared_pool.shutdown()
        total = perf_counter() - t0
        d1_times.append(total)
        d1_breakdown = {
            "precompute_ac_s": round(ac_s, 4),
            "wf_mainnet_s": round(wf1_s, 4),
            "wf_demo_stress_s": round(wf2_s, 4),
            "funding_sweep_8regimes_s": round(sw_s, 4),
        }
        d1_semantics = {
            "mainnet_oos_a_ret_pct": wf_m["oos_summary"]["A"]["mean_risk_budget_return_with_funding_pct"],
            "mainnet_oos_b_ret_pct": wf_m["oos_summary"]["B_wf"]["mean_risk_budget_return_with_funding_pct"],
            "mainnet_oos_c_ret_pct": wf_m["oos_summary"]["C_wf"]["mean_risk_budget_return_with_funding_pct"],
            "demo_oos_b_ret_pct": wf_d["oos_summary"]["B_wf"]["mean_risk_budget_return_with_funding_pct"],
            "sweep_rows": len(sw_rows),
        }

    # D2: S02 automatic.run (expanded 12 candidates)
    from examples.research.champion_demo import prepare as prepare_s02_demo

    d2_times = []
    d2_semantics = {}
    for _ in range(repeats):
        with tempfile.TemporaryDirectory(prefix="bench-d2-s02-") as tmp:
            root = Path(tmp) / "demo"
            cfg_path = prepare_s02_demo(root)
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            cfg["strategies"][0]["grid"]["window"] = [5, 8, 10, 15, 20, 30]
            cfg["strategies"][1]["grid"]["multiplier"] = [0.90, 0.95, 0.98, 1.0, 1.02, 1.05]
            write_json(cfg_path, cfg)
            t0 = perf_counter()
            rep = run_s02_automatic(
                cfg_path,
                output=root / "run",
                store=root / "tracker",
                state_path=root / "champion.json",
                command=["benchmark_p8_perf01"],
            )
            d2_times.append(perf_counter() - t0)
            d2_semantics = {
                "candidates": len(rep["candidates"]),
                "selected": rep["selected"]["candidate_id"],
                "promoted": rep["promoted"],
                "reason": rep["reason"],
            }

    # D3: S01 isolated protocol replay (8 bars = 16 decide events)
    d3_times = []
    d3_last = None
    for _ in range(repeats):
        res_s01 = _run_s01_replay_once(n_bars=8, persistent_worker=False)
        d3_times.append(res_s01["total_s"])
        d3_last = res_s01

    d3_pers_verify_times = []
    d3_pers_verify_last = None
    for _ in range(repeats):
        res_p1 = _run_s01_replay_once(n_bars=8, persistent_worker=True, verify_runtime_each_call=True)
        d3_pers_verify_times.append(res_p1["total_s"])
        d3_pers_verify_last = res_p1

    d3_pers_fast_times = []
    d3_pers_fast_last = None
    for _ in range(repeats):
        res_p2 = _run_s01_replay_once(n_bars=8, persistent_worker=True, verify_runtime_each_call=False)
        d3_pers_fast_times.append(res_p2["total_s"])
        d3_pers_fast_last = res_p2

    return {
        "suite": "BENCH-D",
        "repeats": repeats,
        "workers": workers,
        "d1_r10_walkforward_and_funding_sweep": {
            "timing": _summarize_runs(d1_times),
            "last_phase_breakdown": d1_breakdown,
            "semantics": d1_semantics,
        },
        "d2_s02_automatic_12candidates": {
            "timing": _summarize_runs(d2_times),
            "semantics": d2_semantics,
        },
        "d3_s01_isolated_grid_8bars": {
            "timing": _summarize_runs(d3_times),
            "last_run_detail": d3_last,
        },
        "d3_s01_persistent_worker_verify_each": {
            "timing": _summarize_runs(d3_pers_verify_times),
            "last_run_detail": d3_pers_verify_last,
        },
        "d3_s01_persistent_worker_verify_once": {
            "timing": _summarize_runs(d3_pers_fast_times),
            "last_run_detail": d3_pers_fast_last,
        },
    }


def run_scale_1024(workers: int = 16) -> dict[str, Any]:
    """Measure end-to-end throughput on 1024 full R09 F11/F08 lifecycle paths (168h)."""
    print(f"[SCALE-1024] Running 1024 full R09 F11/F08 lifecycle paths at workers={workers}...", flush=True)
    t_gen0 = perf_counter()
    paths_1024 = _generate_fixed_price_paths(1024, 168, seed=BENCH_SEED)
    gen_s = perf_counter() - t_gen0
    fs_stress = FundingSchedule("constant_hourly_bps", DEMO_OBSERVED_HOURLY_FUNDING_BPS, label="DEMO_STRESS_22.97BPS")
    arms = [
        ("B_F11_F08_IDEAL", DemoGuardrails(enable_f11=True, enable_f08=True), execution_profile("IDEAL_LIMIT_FILL"), fs_stress),
    ]
    from factorlab.compute import chunksize_for_tasks, create_process_pool

    tasks = [
        (paths_1024[i], BENCH_START_ISO, arms[0][1], arms[0][2], arms[0][3], DEFAULT_STRATEGY_CAP, f"s1024-{i}")
        for i in range(1024)
    ]
    t0 = perf_counter()
    with create_process_pool(workers) as pool:
        res = list(pool.map(_bench_r09_worker, tasks, chunksize=chunksize_for_tasks(len(tasks), workers)))
    exec_s = perf_counter() - t0
    t_stat0 = perf_counter()
    sem = _summarize_r09_semantics(res)
    stat_s = perf_counter() - t_stat0
    total_s = gen_s + exec_s + stat_s
    return {
        "suite": "SCALE-1024",
        "n_paths": 1024,
        "horizon_hours": 168,
        "workers": workers,
        "generation_s": round(gen_s, 6),
        "execution_s": round(exec_s, 4),
        "statistics_s": round(stat_s, 6),
        "total_s": round(total_s, 4),
        "paths_per_sec": round(1024 / total_s, 2),
        "projected_4096_paths_s": round(total_s * 4.0, 1),
        "projected_10000_paths_s": round(total_s * (10000.0 / 1024.0), 1),
        "semantics": sem,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--suite",
        choices=("all", "profile", "bench_a", "bench_b", "bench_c", "bench_d", "scale_1024"),
        default="all",
    )
    parser.add_argument("--output", type=Path, required=True, help="Output JSON file path.")
    parser.add_argument("--repeats", type=int, default=3, help="Number of repetitions (>=3 recommended).")
    parser.add_argument("--workers", type=int, default=8, help="Default worker count for BENCH-B/D.")
    parser.add_argument(
        "--c-workers",
        type=str,
        default="1,4,8,12,16,20,24",
        help="Comma-separated worker counts for BENCH-C.",
    )
    args = parser.parse_args(argv)
    _set_single_thread_blas_env()

    started = perf_counter()
    report: dict[str, Any] = {"environment": collect_environment(), "suite": args.suite}
    if args.suite in ("all", "profile"):
        report["profile"] = run_profile_suite()
    if args.suite in ("all", "bench_a"):
        report["bench_a"] = run_bench_a(repeats=args.repeats)
    if args.suite in ("all", "bench_b"):
        report["bench_b"] = run_bench_b(repeats=args.repeats, workers=args.workers)
    if args.suite in ("all", "bench_c"):
        wlist = [int(x.strip()) for x in args.c_workers.split(",") if x.strip()]
        report["bench_c"] = run_bench_c(repeats=args.repeats, worker_list=wlist)
    if args.suite in ("all", "bench_d"):
        report["bench_d"] = run_bench_d(repeats=args.repeats, workers=args.workers)
    if args.suite == "scale_1024":
        report["scale_1024"] = run_scale_1024(workers=args.workers)
    report["total_benchmark_wall_s"] = round(perf_counter() - started, 3)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8")
    print(f"[DONE] Saved benchmark results to {args.output} ({report['total_benchmark_wall_s']}s)", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
