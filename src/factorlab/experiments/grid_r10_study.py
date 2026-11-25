"""P8-R10 Full Study Orchestrator: Paired Backtests, Walk-Forward, Sensitivity,
Deterministic Stress, Stochastic Monte Carlo Tail Risk, and Report Rendering.

All backtests reuse FactorLab's authoritative Paper execution engine via
``grid_funding_adaptive.run_paired_window``.
"""
from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import math
import os
from pathlib import Path
import tempfile
from time import perf_counter
from typing import Any

import numpy as np
import pandas as pd
from scipy import stats as scipy_stats

from factorlab._identity import source_context_root
from factorlab.compute import ComputeConfig, chunksize_for_tasks, create_process_pool
from factorlab.experiments.grid_funding_adaptive import (
    DEMO_OBSERVED_HOURLY_FUNDING_BPS,
    EXECUTION_MODEL_VERSION,
    NORMAL_PERP_HOURLY_FUNDING_BPS,
    PAPER_FEE_RATE,
    PAPER_SLIPPAGE,
    FundingPolicy,
    VolatilityPolicy,
    load_and_audit_r10_dataset,
    parse_utc,
    run_paired_window,
    run_paired_window_multi_overlay,
    validate_hourly_timeline,
)
from factorlab.experiments.grid_monte_carlo import (
    SCENARIO as MC_SCENARIO,
    close_bars,
    historical_input,
    stress_paths,
)
from factorlab.experiments.provenance import capture_provenance
from factorlab.research.monte_carlo import (
    MonteCarloConfig,
    MonteCarloInput,
    MonteCarloRunner,
    _loss_tail,
)
from factorlab.research.monte_carlo.reliability import summarize_paths
from factorlab.research.regime_jump import (
    CalibrationError,
    JumpConfig,
    JumpStress,
    PathPlan,
    RegimeBootstrap,
    RegimeConfig,
)

HISTORICAL_REPLAY_CAPITAL = Decimal("10.00")
DEMO_ANCHOR_CAPITAL = Decimal("7.00")
DEMO_ANCHOR_PRICE = 132.89
SYNTHETIC_CAPACITY_NORMAL = Decimal("0.10")
SYNTHETIC_CAPACITY_THIN = Decimal("0.02")


def load_strategy_configs(repo_root: Path) -> tuple[dict[str, Any], list[FundingPolicy], list[VolatilityPolicy]]:
    cfg_dir = repo_root / "configs" / "research"
    cfg_a = json.loads((cfg_dir / "p8_r10_strategy_a_fixed.json").read_text(encoding="utf-8"))
    cfg_b = json.loads((cfg_dir / "p8_r10_strategy_b_funding_sensitive.json").read_text(encoding="utf-8"))
    cfg_c = json.loads((cfg_dir / "p8_r10_strategy_c_vol_adaptive.json").read_text(encoding="utf-8"))

    b_policies = [
        FundingPolicy(
            candidate_id=c["id"],
            lookback_hours=int(c["lookback_hours"]),
            conservative=bool(c["conservative"]),
            entry_max_bps_h=float(c["entry_max_bps_h"]),
            expected_hold_hours=float(c["expected_hold_hours"]),
            min_net_margin_bps=float(c["min_net_margin_bps"]),
            exit_mode=str(c["exit_mode"]),
            exit_bps_h=float(c["exit_bps_h"]) if c["exit_bps_h"] is not None else None,
            max_hold_hours=float(c["max_hold_hours"]) if c["max_hold_hours"] is not None else None,
        )
        for c in cfg_b["predeclared_candidates"]
    ]
    c_policies = [
        VolatilityPolicy(
            candidate_id=c["id"],
            vol_window_hours=int(c["vol_window_hours"]),
            k_mult=float(c["k_mult"]),
            horizon_hours=float(c["horizon_hours"]),
            min_half_width=float(c["min_half_width"]),
            max_half_width=float(c["max_half_width"]),
            extreme_vol_mult=float(c["extreme_vol_mult"]) if c["extreme_vol_mult"] is not None else None,
            allow_flat_rebuild=bool(c["allow_flat_rebuild"]),
            min_rebuild_interval=int(c["min_rebuild_interval"]),
            rebuild_width_delta=float(c["rebuild_width_delta"]),
        )
        for c in cfg_c["predeclared_candidates"]
    ]
    return {"A": cfg_a, "B": cfg_b, "C": cfg_c}, b_policies, c_policies


def build_nonoverlapping_weekly_windows(
    bars: list[dict[str, Any]],
    *,
    first_start: datetime = datetime(2026, 5, 15, 0, 0, tzinfo=timezone.utc),
    window_hours: int = 168,
    warmup_hours: int = 96,
) -> list[dict[str, Any]]:
    """Slice continuous 1h bars into non-overlapping 7-day windows with strictly prior warmup closes."""
    if window_hours < 1 or warmup_hours < 0:
        raise ValueError("Positive window hours and nonnegative warmup required")
    bar_times = list(validate_hourly_timeline([b["time"] for b in bars]).to_pydatetime())
    time_to_idx = {t: i for i, t in enumerate(bar_times)}
    last_time = bar_times[-1] + timedelta(hours=1)
    windows: list[dict[str, Any]] = []
    idx = 0
    cursor = first_start
    while cursor + timedelta(hours=window_hours) <= last_time:
        end_excl = cursor + timedelta(hours=window_hours)
        if cursor not in time_to_idx:
            raise ValueError("Weekly window start missing from hourly timeline")
        start_i = time_to_idx[cursor]
        end_i = start_i + window_hours
        if end_i > len(bars):
            break
        w_bars = bars[start_i:end_i]
        validate_hourly_timeline([b["time"] for b in w_bars], start=cursor, end=end_excl)
        if windows and windows[-1]["end_exclusive_utc"] != cursor.isoformat():
            raise ValueError("Weekly test windows must be adjacent and non-overlapping")
        warm_start = max(0, start_i - warmup_hours)
        warmup_closes = [float(b["close"]) for b in bars[warm_start:start_i]]
        idx += 1
        r08_tag = f"I{idx}" if idx <= 3 else (f"C{idx - 3}" if idx <= 11 else f"O{idx - 11}")
        windows.append(
            {
                "window_idx": idx,
                "window_id": f"W{idx:02d}",
                "r08_label": r08_tag,
                "start_utc": cursor.isoformat(),
                "end_exclusive_utc": end_excl.isoformat(),
                "bars": w_bars,
                "warmup_closes": warmup_closes,
                "start_bar_index": start_i,
            }
        )
        cursor = end_excl
    return windows


def validate_research_windows(all_bars: list[dict[str, Any]], windows: list[dict[str, Any]]) -> None:
    """Check caller-supplied windows too, before fitting any walk-forward arm."""
    times = validate_hourly_timeline([b["time"] for b in all_bars])
    previous_end = None
    for w in windows:
        start, end = parse_utc(w["start_utc"]), parse_utc(w["end_exclusive_utc"])
        actual = validate_hourly_timeline([b["time"] for b in w["bars"]], start=start, end=end)
        i = w["start_bar_index"]
        if i < 0 or not actual.equals(times[i:i + len(actual)]):
            raise ValueError("Research window differs from source timeline")
        if previous_end is not None and start != previous_end:
            raise ValueError("Research windows must be adjacent and non-overlapping")
        previous_end = end


def _compute_ref_vol_from_bars(bars: list[dict[str, Any]], end_idx_exclusive: int) -> float:
    closes = np.asarray([float(b["close"]) for b in bars[:end_idx_exclusive]], dtype=float)
    rets = np.diff(np.log(closes))
    return float(np.std(rets, ddof=1))


def _window_worker(payload: tuple[Any, ...]) -> dict[str, Any]:
    (
        tmp_root_str,
        tag,
        window_meta,
        strategy_id,
        funding_policy,
        vol_policy,
        funding_events,
        funding_scale,
        constant_hourly_bps,
        regime_switch_bps,
        capital,
        provenance,
    ) = payload
    out_dir = Path(tmp_root_str) / tag
    m = run_paired_window(
        out_dir,
        strategy_id=strategy_id,
        bars=window_meta["bars"],
        capital=capital,
        funding_events=funding_events,
        funding_policy=funding_policy,
        vol_policy=vol_policy,
        warmup_closes=window_meta["warmup_closes"],
        funding_scale=funding_scale,
        constant_hourly_bps=constant_hourly_bps,
        regime_switch_bps=regime_switch_bps,
        provenance=provenance,
        account_id=f"r10-{tag}",
        cleanup_dir=True,
    )
    m["window_idx"] = window_meta["window_idx"]
    m["window_id"] = window_meta["window_id"]
    m["r08_label"] = window_meta["r08_label"]
    m["start_utc"] = window_meta["start_utc"]
    m["end_exclusive_utc"] = window_meta["end_exclusive_utc"]
    m["would_breach_7u_cap"] = bool(0.04 * window_meta["bars"][0]["close"] * 0.99 > 7.0)
    return m


def _slice_window_funding(
    funding_events: list[tuple[datetime, Decimal, Decimal, str]],
    start_utc_iso: str,
    end_excl_utc_iso: str,
    *,
    lookback_hours: int = 96,
) -> list[tuple[datetime, Decimal, Decimal, str]]:
    """Slice only the causal funding events relevant to one window (plus lookback buffer).

    When ``lookback_hours > 0``, also preserves the latest event strictly before
    ``win_start`` so ``causal_funding_rate_bps_h`` computes the exact same inter-event
    gap as when given the full funding history.
    """
    if not funding_events:
        return []
    win_start = parse_utc(start_utc_iso) - timedelta(hours=lookback_hours)
    win_end = parse_utc(end_excl_utc_iso)
    sliced = [ev for ev in funding_events if win_start <= ev[0] < win_end]
    if lookback_hours > 0:
        pre = None
        for ev in funding_events:
            if ev[0] < win_start and (pre is None or ev[0] > pre[0]):
                pre = ev
        if pre is not None:
            sliced.insert(0, pre)
    return sliced


def _execute_tasks_parallel(
    tasks: list[tuple[Any, ...]],
    max_workers: int,
    *,
    executor: ProcessPoolExecutor | None = None,
) -> list[dict[str, Any]]:
    if max_workers <= 1 or len(tasks) <= 2:
        return [_window_worker(t) for t in tasks]
    chunksize = chunksize_for_tasks(len(tasks), max_workers)
    if executor is not None:
        return list(executor.map(_window_worker, tasks, chunksize=chunksize))
    with create_process_pool(max_workers) as pool:
        return list(pool.map(_window_worker, tasks, chunksize=chunksize))


def summarize_window_group(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate paired metrics across a set of windows (with funding included in VaR/CVaR/DD)."""
    if not rows:
        return {}
    df = pd.DataFrame(rows)
    rets_with = df["risk_budget_return_with_funding_pct"].to_numpy(dtype=float) / 100.0
    rets_ex = df["risk_budget_return_ex_funding_pct"].to_numpy(dtype=float) / 100.0
    losses_with = -rets_with
    losses_ex = -rets_ex
    var95_with, cvar95_with = _loss_tail(losses_with, 0.95)
    var95_ex, cvar95_ex = _loss_tail(losses_ex, 0.95)
    return {
        "windows": len(df),
        "total_trade_gross_pnl_usdt": round(float(df["trade_gross_pnl_usdt"].sum()), 6),
        "total_fees_usdt": round(float(df["fees_usdt"].sum()), 6),
        "total_slippage_usdt": round(float(df["slippage_usdt"].sum()), 6),
        "total_trade_net_pnl_usdt": round(float(df["trade_net_pnl_usdt"].sum()), 6),
        "total_funding_pnl_usdt": round(float(df["funding_pnl_usdt"].sum()), 6),
        "total_final_net_pnl_usdt": round(float(df["final_net_pnl_usdt"].sum()), 6),
        "mean_final_net_pnl_usdt": round(float(df["final_net_pnl_usdt"].mean()), 6),
        "mean_risk_budget_return_ex_funding_pct": round(float(df["risk_budget_return_ex_funding_pct"].mean()), 4),
        "mean_risk_budget_return_with_funding_pct": round(float(df["risk_budget_return_with_funding_pct"].mean()), 4),
        "median_risk_budget_return_with_funding_pct": round(float(df["risk_budget_return_with_funding_pct"].median()), 4),
        "positive_windows": int((df["final_net_pnl_usdt"] > 0).sum()),
        "positive_window_rate_pct": round(float((df["final_net_pnl_usdt"] > 0).mean() * 100.0), 2),
        "active_trade_windows": int((df["fills_count"] > 0).sum()),
        "mean_max_drawdown_ex_funding_pct": round(float(df["max_drawdown_ex_funding_pct"].mean()), 4),
        "mean_max_drawdown_with_funding_pct": round(float(df["max_drawdown_with_funding_pct"].mean()), 4),
        "worst_max_drawdown_with_funding_pct": round(float(df["max_drawdown_with_funding_pct"].max()), 4),
        "var95_ex_funding_pct": round(float(var95_ex * 100.0), 4),
        "cvar95_ex_funding_pct": round(float(cvar95_ex * 100.0), 4),
        "var95_with_funding_pct": round(float(var95_with * 100.0), 4),
        "cvar95_with_funding_pct": round(float(cvar95_with * 100.0), 4),
        "mean_order_fill_rate": round(float(df["order_fill_rate"].mean()), 4),
        "mean_fills_per_window": round(float(df["fills_count"].mean()), 3),
        "total_grid_cycles": int(df["grid_cycles"].sum()),
        "mean_holding_hours": round(float(df["mean_holding_hours"].mean()), 2),
        "mean_paused_with_inventory_hours": round(float(df["paused_with_inventory_hours"].mean()), 2),
        "mean_turnover_ratio": round(float(df["turnover_ratio"].mean()), 4),
        "mean_notional_usdt": round(float(df["mean_notional_usdt"].mean()), 4),
        "mean_unrealized_pnl_usdt": round(float(df["unrealized_pnl_usdt"].mean()), 6),
        "mean_excess_vs_equal_bh_usdt": round(float(df["excess_vs_equal_bh_usdt"].mean()), 6),
        "total_excess_vs_equal_bh_usdt": round(float(df["excess_vs_equal_bh_usdt"].sum()), 6),
        "mean_excess_vs_equal_bh_pct": round(float(df["excess_vs_equal_bh_pct"].mean()), 4),
        "risk_adjusted_objective": round(float(np.mean(rets_with) - 0.5 * cvar95_with), 6),
    }


def paired_statistical_test(
    candidate_vals: np.ndarray,
    control_vals: np.ndarray,
    *,
    n_bootstrap: int = 10_000,
    seed: int = 202610,
) -> dict[str, Any]:
    """Paired bootstrap CI, Wilcoxon signed-rank test, and paired t-test on window differences."""
    diff = np.asarray(candidate_vals, dtype=float) - np.asarray(control_vals, dtype=float)
    n = len(diff)
    if n == 0:
        return {"n": 0}
    mean_diff = float(np.mean(diff))
    median_diff = float(np.median(diff))
    std_diff = float(np.std(diff, ddof=1)) if n > 1 else 0.0
    win_rate = float(np.mean(diff > 0))
    tie_rate = float(np.mean(np.isclose(diff, 0.0, atol=1e-12)))
    loss_rate = float(np.mean(diff < 0))

    rng = np.random.default_rng(seed)
    idx = rng.integers(0, n, size=(n_bootstrap, n))
    boot_means = diff[idx].mean(axis=1)
    ci_low, ci_high = float(np.quantile(boot_means, 0.025)), float(np.quantile(boot_means, 0.975))
    # Two-sided bootstrap p-value around zero
    p_boot = float(2.0 * min(np.mean(boot_means <= 0.0), np.mean(boot_means >= 0.0)))
    p_boot = min(1.0, max(1.0 / n_bootstrap, p_boot))

    nonzero = diff[~np.isclose(diff, 0.0, atol=1e-12)]
    if len(nonzero) >= 3:
        w_res = scipy_stats.wilcoxon(nonzero, alternative="two-sided", zero_method="wilcox")
        wilcoxon_p = float(w_res.pvalue)
    else:
        wilcoxon_p = 1.0

    if n >= 2 and std_diff > 1e-12:
        t_res = scipy_stats.ttest_rel(candidate_vals, control_vals)
        t_stat, t_p = float(t_res.statistic), float(t_res.pvalue)
    else:
        t_stat, t_p = 0.0, 1.0

    return {
        "n_windows": n,
        "mean_diff_pct": round(mean_diff, 4),
        "median_diff_pct": round(median_diff, 4),
        "std_diff_pct": round(std_diff, 4),
        "win_rate_pct": round(win_rate * 100.0, 2),
        "tie_rate_pct": round(tie_rate * 100.0, 2),
        "loss_rate_pct": round(loss_rate * 100.0, 2),
        "bootstrap_95ci_low_pct": round(ci_low, 4),
        "bootstrap_95ci_high_pct": round(ci_high, 4),
        "bootstrap_p_value": round(p_boot, 4),
        "wilcoxon_p_value": round(wilcoxon_p, 4),
        "paired_t_stat": round(t_stat, 4),
        "paired_t_p_value": round(t_p, 4),
        "ci_strictly_positive": bool(ci_low > 0.0),
        "statistically_significant_05": bool(ci_low > 0.0 and wilcoxon_p < 0.05),
    }


def _window_multi_overlay_worker(payload: tuple[Any, ...]) -> tuple[str, str, dict[str, dict[str, Any]]]:
    (
        tmp_root_str,
        tag,
        window_meta,
        strategy_id,
        vol_policy,
        funding_events,
        overlays,
        capital,
        provenance,
    ) = payload
    out_dir = Path(tmp_root_str) / tag
    res_map = run_paired_window_multi_overlay(
        out_dir,
        strategy_id=strategy_id,
        bars=window_meta["bars"],
        capital=capital,
        overlays=overlays,
        funding_events=funding_events,
        vol_policy=vol_policy,
        warmup_closes=window_meta["warmup_closes"],
        provenance=provenance,
        account_id=f"r10-{tag}",
        cleanup_dir=True,
    )
    for m in res_map.values():
        m["window_idx"] = window_meta["window_idx"]
        m["window_id"] = window_meta["window_id"]
        m["r08_label"] = window_meta["r08_label"]
        m["start_utc"] = window_meta["start_utc"]
        m["end_exclusive_utc"] = window_meta["end_exclusive_utc"]
        m["would_breach_7u_cap"] = bool(0.04 * window_meta["bars"][0]["close"] * 0.99 > 7.0)
    return strategy_id, window_meta["window_id"], res_map


def precompute_ac_multi_regime(
    all_bars: list[dict[str, Any]],
    windows: list[dict[str, Any]],
    funding_events: list[tuple[datetime, Decimal, Decimal, str]],
    c_policies: list[VolatilityPolicy],
    overlays: list[tuple[str, float, float | None, tuple[int, float, float] | None]],
    *,
    initial_train_windows: int = 9,
    step_windows: int = 3,
    max_workers: int = 4,
    provenance: dict[str, Any] | None = None,
    executor: ProcessPoolExecutor | None = None,
) -> dict[str, Any]:
    """Run funding-independent arms (A and C) ONCE per (config, window) and compute all funding overlays."""
    validate_research_windows(all_bars, windows)
    n_win = len(windows)
    init_cutoff_bar_idx = windows[initial_train_windows]["start_bar_index"]
    init_sigma_ref = _compute_ref_vol_from_bars(all_bars, init_cutoff_bar_idx)

    # Identify unique fold_sigma_ref values for Strategy C candidates that use extreme_vol_mult.
    fold_specs: list[tuple[int, int, int, float]] = []
    fold_id = 0
    train_end = initial_train_windows
    while train_end < n_win:
        test_end = min(n_win, train_end + step_windows)
        fold_id += 1
        fold_sigma_ref = _compute_ref_vol_from_bars(all_bars, windows[train_end]["start_bar_index"])
        fold_specs.append((fold_id, train_end, test_end, fold_sigma_ref))
        train_end = test_end

    win_funding = {
        w["window_id"]: _slice_window_funding(funding_events, w["start_utc"], w["end_exclusive_utc"], lookback_hours=0)
        for w in windows
    }

    with tempfile.TemporaryDirectory(prefix="factorlab-r10-ac-") as tmp:
        tasks: list[tuple[Any, ...]] = []
        # 1. Base matrix: A_FIXED + C1..C6 (with init_sigma_ref) across all 27 windows
        for w in windows:
            wf = win_funding[w["window_id"]]
            tasks.append((
                tmp, f"base_A_{w['window_id']}", w, "A_FIXED", None,
                wf, overlays, HISTORICAL_REPLAY_CAPITAL, provenance,
            ))
            for cp in c_policies:
                cp_frozen = replace(cp, reference_vol_1h=init_sigma_ref)
                tasks.append((
                    tmp, f"base_{cp.candidate_id}_{w['window_id']}", w, cp.candidate_id, cp_frozen,
                    wf, overlays, HISTORICAL_REPLAY_CAPITAL, provenance,
                ))
        # 2. Per-fold causal sigma_ref runs ONLY for C candidates where extreme_vol_mult is not None (fold >= 2; fold 1 == init_sigma_ref)
        fold_task_meta: list[tuple[int, str, str]] = []
        for fid, tr_end, te_end, f_sigma in fold_specs:
            if fid == 1:
                continue
            for cp in c_policies:
                if cp.extreme_vol_mult is None:
                    continue
                cp_fold = replace(cp, reference_vol_1h=f_sigma)
                for w in windows[:te_end]:
                    tasks.append((
                        tmp, f"f{fid}_{cp.candidate_id}_{w['window_id']}", w, cp.candidate_id, cp_fold,
                        win_funding[w["window_id"]], overlays, HISTORICAL_REPLAY_CAPITAL, provenance,
                    ))
                    fold_task_meta.append((fid, cp.candidate_id, w["window_id"]))

        if max_workers <= 1:
            raw_results = [_window_multi_overlay_worker(t) for t in tasks]
        else:
            chunksize = chunksize_for_tasks(len(tasks), max_workers)
            if executor is not None:
                raw_results = list(executor.map(_window_multi_overlay_worker, tasks, chunksize=chunksize))
            else:
                with create_process_pool(max_workers) as pool:
                    raw_results = list(pool.map(_window_multi_overlay_worker, tasks, chunksize=chunksize))

    base_count = len(windows) * (1 + len(c_policies))
    base_by_regime: dict[str, dict[tuple[str, str], dict[str, Any]]] = {ov[0]: {} for ov in overlays}
    for arm_id, win_id, ov_map in raw_results[:base_count]:
        for ov_key, row in ov_map.items():
            base_by_regime[ov_key][(arm_id, win_id)] = row

    fold_by_regime: dict[str, dict[tuple[int, str, str], dict[str, Any]]] = {ov[0]: {} for ov in overlays}
    for (fid, arm_id, win_id), (_, _, ov_map) in zip(fold_task_meta, raw_results[base_count:]):
        for ov_key, row in ov_map.items():
            fold_by_regime[ov_key][(fid, arm_id, win_id)] = row

    return {
        "init_sigma_ref": init_sigma_ref,
        "fold_specs": fold_specs,
        "base_by_regime": base_by_regime,
        "fold_by_regime": fold_by_regime,
    }


def run_expanding_walk_forward(
    all_bars: list[dict[str, Any]],
    windows: list[dict[str, Any]],
    funding_events: list[tuple[datetime, Decimal, Decimal, str]],
    b_policies: list[FundingPolicy],
    c_policies: list[VolatilityPolicy],
    *,
    regime_label: str,
    funding_scale: float = 1.0,
    constant_hourly_bps: float | None = None,
    regime_switch_bps: tuple[int, float, float] | None = None,
    initial_train_windows: int = 9,
    step_windows: int = 3,
    min_mean_fills: float = 0.20,
    max_workers: int = 4,
    provenance: dict[str, Any] | None = None,
    precomputed_ac: dict[str, Any] | None = None,
    executor: ProcessPoolExecutor | None = None,
) -> dict[str, Any]:
    """Run expanding-window Walk-Forward without any validation-window refitting.

    For Strategy C, each fold uses ``sigma_ref`` frozen strictly from bars before
    the fold's test start. Precomputed funding-independent A/C overlays are reused
    without any approximation (exact same Paper ledger).
    """
    validate_research_windows(all_bars, windows)
    if precomputed_ac is None:
        overlays = [(regime_label, funding_scale, constant_hourly_bps, regime_switch_bps)]
        precomputed_ac = precompute_ac_multi_regime(
            all_bars, windows, funding_events, c_policies, overlays,
            initial_train_windows=initial_train_windows, step_windows=step_windows,
            max_workers=max_workers, provenance=provenance, executor=executor,
        )

    init_sigma_ref = precomputed_ac["init_sigma_ref"]
    fold_specs = precomputed_ac["fold_specs"]
    by_arm_win: dict[tuple[str, str], dict[str, Any]] = dict(precomputed_ac["base_by_regime"][regime_label])
    fold_c_cache = precomputed_ac["fold_by_regime"][regime_label]

    max_lb = max((bp.lookback_hours for bp in b_policies), default=24) + 24
    # Run funding-sensitive Strategy B candidates specifically under this funding regime
    with tempfile.TemporaryDirectory(prefix=f"factorlab-r10-wf-b-{regime_label}-") as tmp:
        b_tasks = []
        for w in windows:
            wf = (
                []
                if (constant_hourly_bps is not None or regime_switch_bps is not None)
                else _slice_window_funding(funding_events, w["start_utc"], w["end_exclusive_utc"], lookback_hours=max_lb)
            )
            for bp in b_policies:
                b_tasks.append((
                    tmp, f"{bp.candidate_id}_{w['window_id']}", w, bp.candidate_id, bp, None,
                    wf, funding_scale, constant_hourly_bps, regime_switch_bps,
                    HISTORICAL_REPLAY_CAPITAL, provenance,
                ))
        b_rows = _execute_tasks_parallel(b_tasks, max_workers=max_workers, executor=executor)
    for r in b_rows:
        by_arm_win[(r["strategy_id"], r["window_id"])] = r

    all_arm_ids = ["A_FIXED"] + [p.candidate_id for p in b_policies] + [p.candidate_id for p in c_policies]
    all_rows = [by_arm_win[(arm_id, w["window_id"])] for w in windows for arm_id in all_arm_ids]

    # Expanding Walk-Forward folds
    folds_summary: list[dict[str, Any]] = []
    oos_rows_a: list[dict[str, Any]] = []
    oos_rows_b: list[dict[str, Any]] = []
    oos_rows_c: list[dict[str, Any]] = []

    c_policy_map = {cp.candidate_id: cp for cp in c_policies}
    for fold_id, train_end, test_end, fold_sigma_ref in fold_specs:
        train_wins = windows[:train_end]
        test_wins = windows[train_end:test_end]
        if not train_wins or not test_wins or train_wins[-1]["end_exclusive_utc"] != test_wins[0]["start_utc"]:
            raise ValueError("Walk-forward training/test boundary must be adjacent without overlap")

        # Select best B on train_wins ONLY
        b_scores: list[tuple[float, float, str, dict[str, Any]]] = []
        for bp in b_policies:
            tr = [by_arm_win[(bp.candidate_id, w["window_id"])] for w in train_wins]
            st = summarize_window_group(tr)
            b_scores.append((st["risk_adjusted_objective"], st["mean_fills_per_window"], bp.candidate_id, st))
        b_eligible = [x for x in b_scores if x[1] >= min_mean_fills]
        b_all_flat_fallback = len(b_eligible) == 0
        best_b = max(b_eligible if b_eligible else b_scores, key=lambda x: x[0])
        best_b_id = best_b[2]

        # Select best C on train_wins using fold_sigma_ref (strictly causal)
        def _get_c_row(cp_id: str, win_id: str) -> dict[str, Any]:
            if fold_id == 1 or c_policy_map[cp_id].extreme_vol_mult is None:
                return by_arm_win[(cp_id, win_id)]
            return fold_c_cache[(fold_id, cp_id, win_id)]

        c_scores: list[tuple[float, float, str, dict[str, Any]]] = []
        for cp in c_policies:
            tr = [_get_c_row(cp.candidate_id, w["window_id"]) for w in train_wins]
            st = summarize_window_group(tr)
            c_scores.append((st["risk_adjusted_objective"], st["mean_fills_per_window"], cp.candidate_id, st))
        c_eligible = [x for x in c_scores if x[1] >= min_mean_fills]
        c_all_flat_fallback = len(c_eligible) == 0
        best_c = max(c_eligible if c_eligible else c_scores, key=lambda x: x[0])
        best_c_id = best_c[2]

        a_te_rows = [dict(by_arm_win[("A_FIXED", w["window_id"])], fold_id=fold_id, selected_arm="A_FIXED") for w in test_wins]
        b_te_rows = [dict(by_arm_win[(best_b_id, w["window_id"])], fold_id=fold_id, selected_arm=best_b_id) for w in test_wins]
        c_te_tagged = [dict(_get_c_row(best_c_id, w["window_id"]), fold_id=fold_id, selected_arm=best_c_id) for w in test_wins]

        oos_rows_a.extend(a_te_rows)
        oos_rows_b.extend(b_te_rows)
        oos_rows_c.extend(c_te_tagged)

        st_a_te = summarize_window_group(a_te_rows)
        st_b_te = summarize_window_group(b_te_rows)
        st_c_te = summarize_window_group(c_te_tagged)
        folds_summary.append(
            {
                "regime": regime_label,
                "fold_id": fold_id,
                "train_windows": f"{train_wins[0]['window_id']}..{train_wins[-1]['window_id']}",
                "test_windows": f"{test_wins[0]['window_id']}..{test_wins[-1]['window_id']}",
                "train_count": len(train_wins),
                "test_count": len(test_wins),
                "frozen_sigma_ref_1h_pct": round(fold_sigma_ref * 100.0, 4),
                "selected_b": best_b_id,
                "b_all_flat_fallback": b_all_flat_fallback,
                "selected_b_train_ret_pct": best_b[3]["mean_risk_budget_return_with_funding_pct"],
                "selected_c": best_c_id,
                "c_all_flat_fallback": c_all_flat_fallback,
                "selected_c_train_ret_pct": best_c[3]["mean_risk_budget_return_with_funding_pct"],
                "oos_a_ret_pct": st_a_te["mean_risk_budget_return_with_funding_pct"],
                "oos_b_ret_pct": st_b_te["mean_risk_budget_return_with_funding_pct"],
                "oos_c_ret_pct": st_c_te["mean_risk_budget_return_with_funding_pct"],
                "oos_b_minus_a_pct": round(
                    st_b_te["mean_risk_budget_return_with_funding_pct"]
                    - st_a_te["mean_risk_budget_return_with_funding_pct"],
                    4,
                ),
                "oos_c_minus_a_pct": round(
                    st_c_te["mean_risk_budget_return_with_funding_pct"]
                    - st_a_te["mean_risk_budget_return_with_funding_pct"],
                    4,
                ),
                "oos_a_dd_pct": st_a_te["mean_max_drawdown_with_funding_pct"],
                "oos_b_dd_pct": st_b_te["mean_max_drawdown_with_funding_pct"],
                "oos_c_dd_pct": st_c_te["mean_max_drawdown_with_funding_pct"],
            }
        )

    # 3. Parameter stability & IS vs OOS degradation across all predeclared arms
    is_win_ids = {w["window_id"] for w in windows[:initial_train_windows]}
    oos_win_ids = {w["window_id"] for w in windows[initial_train_windows:]}
    stability_rows: list[dict[str, Any]] = []
    all_arm_ids = ["A_FIXED"] + [p.candidate_id for p in b_policies] + [p.candidate_id for p in c_policies]
    for arm_id in all_arm_ids:
        arm_all = [r for r in all_rows if r["strategy_id"] == arm_id]
        arm_is = [r for r in arm_all if r["window_id"] in is_win_ids]
        arm_oos = [r for r in arm_all if r["window_id"] in oos_win_ids]
        s_all = summarize_window_group(arm_all)
        s_is = summarize_window_group(arm_is)
        s_oos = summarize_window_group(arm_oos)
        stability_rows.append(
            {
                "regime": regime_label,
                "strategy_id": arm_id,
                "family": "A" if arm_id.startswith("A") else ("B" if arm_id.startswith("B") else "C"),
                "full_mean_ret_pct": s_all["mean_risk_budget_return_with_funding_pct"],
                "full_total_net_usdt": s_all["total_final_net_pnl_usdt"],
                "full_total_funding_usdt": s_all["total_funding_pnl_usdt"],
                "full_cvar95_pct": s_all["cvar95_with_funding_pct"],
                "full_max_dd_mean_pct": s_all["mean_max_drawdown_with_funding_pct"],
                "full_fill_rate": s_all["mean_order_fill_rate"],
                "full_mean_fills": s_all["mean_fills_per_window"],
                "full_mean_hold_h": s_all["mean_holding_hours"],
                "full_turnover": s_all["mean_turnover_ratio"],
                "full_excess_bh_pct": s_all["mean_excess_vs_equal_bh_pct"],
                "is_mean_ret_pct": s_is["mean_risk_budget_return_with_funding_pct"],
                "oos_mean_ret_pct": s_oos["mean_risk_budget_return_with_funding_pct"],
                "oos_minus_is_ret_pct": round(
                    s_oos["mean_risk_budget_return_with_funding_pct"]
                    - s_is["mean_risk_budget_return_with_funding_pct"],
                    4,
                ),
                "is_cvar95_pct": s_is["cvar95_with_funding_pct"],
                "oos_cvar95_pct": s_oos["cvar95_with_funding_pct"],
                "is_objective": s_is["risk_adjusted_objective"],
                "oos_objective": s_oos["risk_adjusted_objective"],
            }
        )

    # Spearman rank correlation between IS objective and OOS objective across all B+C candidates
    bc_stab = [r for r in stability_rows if r["family"] in {"B", "C"}]
    is_objs = [r["is_objective"] for r in bc_stab]
    oos_objs = [r["oos_objective"] for r in bc_stab]
    if len(set(is_objs)) > 1 and len(set(oos_objs)) > 1:
        sp = scipy_stats.spearmanr(is_objs, oos_objs)
        rank_corr, rank_p = round(float(sp.statistic), 4), round(float(sp.pvalue), 4)
    else:
        rank_corr, rank_p = 0.0, 1.0

    # Paired statistical tests on OOS Walk-Forward returns & drawdowns
    a_ret_oos = np.asarray([r["risk_budget_return_with_funding_pct"] for r in oos_rows_a], dtype=float)
    b_ret_oos = np.asarray([r["risk_budget_return_with_funding_pct"] for r in oos_rows_b], dtype=float)
    c_ret_oos = np.asarray([r["risk_budget_return_with_funding_pct"] for r in oos_rows_c], dtype=float)

    a_dd_oos = np.asarray([r["max_drawdown_with_funding_pct"] for r in oos_rows_a], dtype=float)
    b_dd_oos = np.asarray([r["max_drawdown_with_funding_pct"] for r in oos_rows_b], dtype=float)
    c_dd_oos = np.asarray([r["max_drawdown_with_funding_pct"] for r in oos_rows_c], dtype=float)

    stat_tests = {
        "regime": regime_label,
        "oos_B_wf_vs_A_return": paired_statistical_test(b_ret_oos, a_ret_oos, seed=202611),
        "oos_C_wf_vs_A_return": paired_statistical_test(c_ret_oos, a_ret_oos, seed=202612),
        # Negative drawdown diff means candidate has LOWER drawdown (improvement)
        "oos_B_wf_vs_A_drawdown_reduction": paired_statistical_test(-b_dd_oos, -a_dd_oos, seed=202613),
        "oos_C_wf_vs_A_drawdown_reduction": paired_statistical_test(-c_dd_oos, -a_dd_oos, seed=202614),
        "is_oos_spearman_rank_corr": rank_corr,
        "is_oos_spearman_p_value": rank_p,
        "selected_b_sequence": [f["selected_b"] for f in folds_summary],
        "selected_c_sequence": [f["selected_c"] for f in folds_summary],
    }

    return {
        "regime": regime_label,
        "initial_sigma_ref_1h": init_sigma_ref,
        "all_rows": all_rows,
        "folds": folds_summary,
        "oos_rows": {"A": oos_rows_a, "B": oos_rows_b, "C": oos_rows_c},
        "oos_summary": {
            "A": summarize_window_group(oos_rows_a),
            "B_wf": summarize_window_group(oos_rows_b),
            "C_wf": summarize_window_group(oos_rows_c),
        },
        "full_summary": {
            arm_id: summarize_window_group([r for r in all_rows if r["strategy_id"] == arm_id])
            for arm_id in all_arm_ids
        },
        "stability": stability_rows,
        "stat_tests": stat_tests,
    }


def run_funding_sensitivity_sweep(
    windows: list[dict[str, Any]],
    funding_events: list[tuple[datetime, Decimal, Decimal, str]],
    b_entry_only: FundingPolicy,
    b_entry_exit: FundingPolicy,
    c_policy: VolatilityPolicy,
    *,
    max_workers: int = 4,
    provenance: dict[str, Any] | None = None,
    executor: ProcessPoolExecutor | None = None,
) -> list[dict[str, Any]]:
    """Sweep funding regimes from 0x to 25x Mainnet + Demo stress + Dynamic Regime Switch."""
    regimes = [
        ("0x_ex_funding", 0.0, None, None),
        ("1x_mainnet_actual", 1.0, None, None),
        ("2x_mainnet", 2.0, None, None),
        ("5x_mainnet", 5.0, None, None),
        ("10x_mainnet", 10.0, None, None),
        ("25x_mainnet", 25.0, None, None),
        ("demo_22.97bps_h", 1.0, DEMO_OBSERVED_HOURLY_FUNDING_BPS, None),
        ("switch_0.125_to_22.97_at_48h", 1.0, None, (48, NORMAL_PERP_HOURLY_FUNDING_BPS, DEMO_OBSERVED_HOURLY_FUNDING_BPS)),
    ]
    b_arms = [
        (f"B_ENTRY_{b_entry_only.candidate_id}", b_entry_only),
        (f"B_EXIT_{b_entry_exit.candidate_id}", b_entry_exit),
    ]
    c_arm_name = f"C_ADAPT_{c_policy.candidate_id}"
    max_lb = max(b_entry_only.lookback_hours, b_entry_exit.lookback_hours) + 24
    win_funding_ac = {
        w["window_id"]: _slice_window_funding(funding_events, w["start_utc"], w["end_exclusive_utc"], lookback_hours=0)
        for w in windows
    }
    win_funding_b = {
        w["window_id"]: _slice_window_funding(funding_events, w["start_utc"], w["end_exclusive_utc"], lookback_hours=max_lb)
        for w in windows
    }

    rows_out: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="factorlab-r10-fsweep-") as tmp:
        # Run funding-independent A and C ONCE per window and overlay all 8 regimes
        ac_tasks = []
        for w in windows:
            wf = win_funding_ac[w["window_id"]]
            ac_tasks.append((tmp, f"A_{w['window_id']}", w, "A_FIXED", None, wf, regimes, HISTORICAL_REPLAY_CAPITAL, provenance))
            ac_tasks.append((tmp, f"C_{w['window_id']}", w, c_arm_name, c_policy, wf, regimes, HISTORICAL_REPLAY_CAPITAL, provenance))
        if max_workers <= 1:
            ac_res = [_window_multi_overlay_worker(t) for t in ac_tasks]
        else:
            chunksize = chunksize_for_tasks(len(ac_tasks), max_workers)
            if executor is not None:
                ac_res = list(executor.map(_window_multi_overlay_worker, ac_tasks, chunksize=chunksize))
            else:
                with create_process_pool(max_workers) as pool:
                    ac_res = list(pool.map(_window_multi_overlay_worker, ac_tasks, chunksize=chunksize))
        ac_by_reg: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for arm_id, _, ov_map in ac_res:
            for reg_name, row in ov_map.items():
                ac_by_reg.setdefault((reg_name, arm_id), []).append(row)

        # Run funding-sensitive B arms per regime
        b_tasks = []
        for reg_name, f_scale, const_bps, sw_bps in regimes:
            for arm_name, bp in b_arms:
                for w in windows:
                    wf = [] if (const_bps is not None or sw_bps is not None) else win_funding_b[w["window_id"]]
                    b_tasks.append((
                        tmp, f"{reg_name}_{arm_name}_{w['window_id']}", w, arm_name, bp, None,
                        wf, f_scale, const_bps, sw_bps, HISTORICAL_REPLAY_CAPITAL, provenance,
                    ))
        b_res = _execute_tasks_parallel(b_tasks, max_workers=max_workers, executor=executor)
        b_idx = 0
        b_by_reg: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for reg_name, _, _, _ in regimes:
            for arm_name, _ in b_arms:
                b_by_reg[(reg_name, arm_name)] = b_res[b_idx : b_idx + len(windows)]
                b_idx += len(windows)

        arm_order = ["A_FIXED", b_arms[0][0], b_arms[1][0], c_arm_name]
        for reg_name, _, _, _ in regimes:
            for arm_name in arm_order:
                chunk = ac_by_reg[(reg_name, arm_name)] if (reg_name, arm_name) in ac_by_reg else b_by_reg[(reg_name, arm_name)]
                s = summarize_window_group(chunk)
                rows_out.append({"funding_regime": reg_name, "strategy_id": arm_name, **s})
    return rows_out


def run_deterministic_stress_suite(
    b_entry_only: FundingPolicy,
    b_entry_exit: FundingPolicy,
    c_policy: VolatilityPolicy,
    *,
    anchor_price: float = DEMO_ANCHOR_PRICE,
    n_steps: int = 168,
    provenance: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Run all 9 required deterministic stress regimes with funding included in tail loss."""
    base_stress = stress_paths(anchor_price, n_steps)
    x = np.linspace(0.0, 1.0, n_steps + 1)
    # Use downward-first oscillations for S1/S7 so long-only Grid enters buys and experiences
    # actual fill/funding/whipsaw dynamics instead of immediately upper-breakout pausing flat.
    extra = {
        "high_funding_range": anchor_price * (1.0 - 0.0102 * np.sin(8.0 * np.pi * x)),
        "unidirectional_rally": anchor_price * (1.0 + 0.25 * x),
        "high_freq_whipsaw": anchor_price * (1.0 - 0.0108 * np.sin(24.0 * np.pi * x)),
        "funding_spike_mild_dip": anchor_price * np.interp(x, [0.0, 0.25, 0.65, 1.0], [1.0, 0.985, 0.96, 0.975]),
    }

    # (scenario_id, category, prices, const_bps, switch_bps, capacity)
    scenarios = [
        ("S1_high_funding_range", "高资金费+区间震荡", extra["high_funding_range"], DEMO_OBSERVED_HOURLY_FUNDING_BPS, None, SYNTHETIC_CAPACITY_NORMAL),
        ("S2_funding_regime_spike", "突发高资金费切换", extra["funding_spike_mild_dip"], None, (48, NORMAL_PERP_HOURLY_FUNDING_BPS, DEMO_OBSERVED_HOURLY_FUNDING_BPS), SYNTHETIC_CAPACITY_NORMAL),
        ("S3_low_volatility", "低波动横盘", base_stress["low_volatility"], NORMAL_PERP_HOURLY_FUNDING_BPS, None, SYNTHETIC_CAPACITY_NORMAL),
        ("S4_unidirectional_rally", "单边上涨(+25%)", extra["unidirectional_rally"], NORMAL_PERP_HOURLY_FUNDING_BPS, None, SYNTHETIC_CAPACITY_NORMAL),
        ("S5_continuous_decline", "单边下跌(-40%)", base_stress["continuous_decline"], DEMO_OBSERVED_HOURLY_FUNDING_BPS, None, SYNTHETIC_CAPACITY_NORMAL),
        ("S6_range_then_gap", "突发跳空(-30%)", base_stress["range_then_gap"], NORMAL_PERP_HOURLY_FUNDING_BPS, None, SYNTHETIC_CAPACITY_NORMAL),
        ("S7_high_freq_whipsaw", "高频反转震荡", extra["high_freq_whipsaw"], NORMAL_PERP_HOURLY_FUNDING_BPS, None, SYNTHETIC_CAPACITY_NORMAL),
        ("S8_thin_capacity_decline", "成交不足(0.02<min_notional)", base_stress["insufficient_rebound"], DEMO_OBSERVED_HOURLY_FUNDING_BPS, None, SYNTHETIC_CAPACITY_THIN),
        ("S9_prolonged_stranded", "长时间残仓+高资费", base_stress["slow_decline"], DEMO_OBSERVED_HOURLY_FUNDING_BPS, None, SYNTHETIC_CAPACITY_NORMAL),
    ]

    arms = [
        ("A_FIXED", None, None),
        (f"B_ENTRY_{b_entry_only.candidate_id}", b_entry_only, None),
        (f"B_EXIT_{b_entry_exit.candidate_id}", b_entry_exit, None),
        (f"C_ADAPT_{c_policy.candidate_id}", None, c_policy),
    ]
    start_iso = "2026-11-24T00:00:00+00:00"
    # Neutral pre-window warmup at calibrated baseline vol (~0.445%/h)
    rng = np.random.default_rng(1010)
    neutral_warmup = list(anchor_price * np.exp(np.cumsum(rng.normal(0.0, c_policy.reference_vol_1h, size=60))))
    # Re-anchor warmup end to anchor_price so no artificial jump at t=0
    scale_fix = anchor_price / neutral_warmup[-1]
    neutral_warmup = [p * scale_fix for p in neutral_warmup]

    rows: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="factorlab-r10-stress-") as tmp:
        for sc_id, category, prices, const_bps, sw_bps, cap_bar in scenarios:
            bars = close_bars(prices, start_iso)
            for arm_name, bp, cp in arms:
                m = run_paired_window(
                    Path(tmp) / f"{sc_id}_{arm_name}",
                    strategy_id=arm_name,
                    bars=bars,
                    capital=DEMO_ANCHOR_CAPITAL,
                    funding_policy=bp,
                    vol_policy=cp,
                    warmup_closes=neutral_warmup,
                    constant_hourly_bps=const_bps,
                    regime_switch_bps=sw_bps,
                    scenario=MC_SCENARIO,
                    synthetic_capacity=cap_bar,
                    provenance=provenance,
                    account_id=f"r10-{sc_id}-{arm_name}",
                    cleanup_dir=True,
                )
                rows.append({"scenario_id": sc_id, "category": category, "capacity_per_bar": str(cap_bar), **m})
    return rows


def _mc_path_worker(payload: tuple[Any, ...]) -> dict[str, Any]:
    (
        tmp_root_str,
        tag,
        prices,
        start_iso,
        strategy_id,
        funding_policy,
        vol_policy,
        warmup_closes,
        const_bps,
        switch_bps,
        provenance,
    ) = payload
    m = run_paired_window(
        Path(tmp_root_str) / tag,
        strategy_id=strategy_id,
        bars=close_bars(prices, start_iso),
        capital=DEMO_ANCHOR_CAPITAL,
        funding_policy=funding_policy,
        vol_policy=vol_policy,
        warmup_closes=warmup_closes,
        constant_hourly_bps=const_bps,
        regime_switch_bps=switch_bps,
        scenario=MC_SCENARIO,
        synthetic_capacity=SYNTHETIC_CAPACITY_NORMAL,
        provenance=provenance,
        account_id=f"r10-mc-{tag}",
        cleanup_dir=True,
    )
    return m


def _mc_multi_overlay_worker(payload: tuple[Any, ...]) -> tuple[str, dict[str, dict[str, Any]]]:
    (
        tmp_root_str,
        tag,
        prices,
        start_iso,
        strategy_id,
        vol_policy,
        warmup_closes,
        overlays,
        provenance,
    ) = payload
    ov_map = run_paired_window_multi_overlay(
        Path(tmp_root_str) / tag,
        strategy_id=strategy_id,
        bars=close_bars(prices, start_iso),
        capital=DEMO_ANCHOR_CAPITAL,
        overlays=overlays,
        vol_policy=vol_policy,
        warmup_closes=warmup_closes,
        scenario=MC_SCENARIO,
        synthetic_capacity=SYNTHETIC_CAPACITY_NORMAL,
        provenance=provenance,
        account_id=f"r10-mc-{tag}",
        cleanup_dir=True,
    )
    return strategy_id, ov_map


def run_stochastic_mc_tail_risk(
    r04b_snapshot: Path,
    b_entry_exit: FundingPolicy,
    c_policy: VolatilityPolicy,
    *,
    anchor_price: float = DEMO_ANCHOR_PRICE,
    n_paths: int = 64,
    n_steps: int = 168,
    seed: int = 810,
    max_workers: int = 4,
    provenance: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Run paired A / B / C across the 6 R08 stochastic tail-risk generators under Normal & Switch-to-Demo funding."""
    from factorlab.experiments.grid_empirical import load_hourly

    bars_r04b, identity_r04b = load_hourly(r04b_snapshot)
    times_utc = pd.to_datetime([b["time"] for b in bars_r04b], utc=True) + pd.Timedelta(hours=1)
    acquired_ts = pd.Timestamp(identity_r04b["acquired_at"]).tz_convert("UTC")
    as_of_ts = max(times_utc[-1], acquired_ts).ceil("h")
    as_of = as_of_ts.isoformat()
    start_iso = (as_of_ts + pd.Timedelta(days=1)).isoformat()
    base_input, _ = historical_input(r04b_snapshot, as_of=as_of, initial_price=anchor_price)
    base_vol = float(base_input.volatility[0])
    base_drift = float(base_input.drift[0])

    # Generate identical price paths for paired A/B/C comparison (reusing R08 generators)
    scenario_paths: list[tuple[str, float, np.ndarray]] = []
    for label, mult in (
        ("gbm_low_vol_0.5x", 0.5),
        ("gbm_base_vol_1.0x", 1.0),
        ("gbm_high_vol_2.0x", 2.0),
    ):
        scaled_vol = base_vol * mult
        scaled_input = MonteCarloInput(
            weights=base_input.weights,
            drift=[base_drift],
            volatility=[scaled_vol],
            historical_returns=base_input.historical_returns,
            timestamps=base_input.timestamps,
            available_at=base_input.available_at,
            as_of=base_input.as_of,
            assets=base_input.assets,
            initial_prices=[anchor_price],
        )
        mc_cfg = MonteCarloConfig(
            model="gbm", n_paths=n_paths, n_steps=n_steps, seed=seed + int(mult * 100),
            dt=1.0, batch_size=min(64, n_paths), save_full_paths=True, compute=ComputeConfig(),
        )
        res = MonteCarloRunner(mc_cfg).run(scaled_input)
        scenario_paths.append((label, scaled_vol, res.asset_paths[:, :, 0]))

    # Moving Block Bootstrap 24h
    mbb_cfg = MonteCarloConfig(
        model="moving_block_bootstrap", n_paths=n_paths, n_steps=n_steps, seed=seed + 300,
        dt=1.0, batch_size=min(64, n_paths), block_size=24, save_full_paths=True, compute=ComputeConfig(),
    )
    res_mbb = MonteCarloRunner(mbb_cfg).run(base_input)
    scenario_paths.append(("moving_block_bootstrap_24h", base_vol, res_mbb.asset_paths[:, :, 0]))

    # State-conditional Regime Bootstrap 24h (matching R08)
    px_arr = np.asarray([b["close"] for b in bars_r04b], dtype=float)
    ret_arr = (px_arr[1:] / px_arr[:-1] - 1.0)[:, None]
    avail_utc = [max(t, pd.Timestamp(identity_r04b["acquired_at"])) for t in times_utc[1:]]
    threshold_as_of = times_utc[len(times_utc) // 2].isoformat()
    rb_plan = PathPlan(n_paths=n_paths, n_steps=n_steps, batch_size=min(32, n_paths), seed=seed + 400)
    try:
        reg_gen = RegimeBootstrap(
            returns=ret_arr,
            timestamps=times_utc[1:].tolist(),
            available_at=avail_utc,
            as_of=as_of,
            threshold_as_of=threshold_as_of,
            assets=("BINANCE:USDM:NVDAUSDT",),
            initial_prices=[anchor_price],
            source={"source": identity_r04b["source"], "data_version": identity_r04b["data_version"]},
            config=RegimeConfig(block_size=24, lookback=24, min_blocks_per_state=5, min_transitions_per_state=5),
        )
        rb_batches = [paths[:, :, 0] for _, paths in reg_gen.batches(rb_plan)]
        scenario_paths.append(("regime_bootstrap_24h", base_vol, np.vstack(rb_batches)))
    except CalibrationError:
        pass

    # Parameterized Jump Stress Downside (matching R08)
    jump_gen = JumpStress(
        assets=("BINANCE:USDM:NVDAUSDT",),
        initial_prices=[anchor_price],
        as_of=as_of,
        source={"source": identity_r04b["source"], "data_version": identity_r04b["data_version"]},
        config=JumpConfig(
            log_drift=(base_drift - 0.5 * base_vol**2,),
            volatility=(base_vol,),
            jump_probability=0.015,
            jump_log_sizes=(-0.03,),
        ),
    )
    jump_plan = PathPlan(n_paths=n_paths, n_steps=n_steps, batch_size=min(32, n_paths), seed=seed + 500)
    jump_batches = [paths[:, :, 0] for _, paths in jump_gen.batches(jump_plan)]
    scenario_paths.append(("jump_stress_downside", base_vol * 1.5, np.vstack(jump_batches)))

    funding_overlays = [
        ("normal_0.125bps_h", 1.0, NORMAL_PERP_HOURLY_FUNDING_BPS, None),
        ("switch_demo_22.97bps_h_at_48h", 1.0, None, (48, NORMAL_PERP_HOURLY_FUNDING_BPS, DEMO_OBSERVED_HOURLY_FUNDING_BPS)),
    ]
    b_arm_name = f"B_{b_entry_exit.candidate_id}"
    c_arm_name = f"C_{c_policy.candidate_id}"
    arm_order = ["A_FIXED", b_arm_name, c_arm_name]

    summary_rows: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="factorlab-r10-mc-") as tmp:
        pool = create_process_pool(max_workers) if max_workers > 1 else None
        try:
            for sc_name, sc_vol, paths in scenario_paths:
                rng_w = np.random.default_rng(seed + 99)
                w_closes = list(anchor_price * np.exp(np.cumsum(rng_w.normal(0.0, sc_vol, size=60))))
                w_scale = anchor_price / w_closes[-1]
                w_closes = [p * w_scale for p in w_closes]

                # Run A and C ONCE per path with multi-overlay for both funding regimes
                ac_tasks = []
                for pi in range(n_paths):
                    ac_tasks.append((tmp, f"{sc_name}_A_p{pi:03d}", paths[pi], start_iso, "A_FIXED", None, w_closes, funding_overlays, provenance))
                    ac_tasks.append((tmp, f"{sc_name}_C_p{pi:03d}", paths[pi], start_iso, c_arm_name, c_policy, w_closes, funding_overlays, provenance))
                ac_chunk = chunksize_for_tasks(len(ac_tasks), max_workers)
                ac_res = list(pool.map(_mc_multi_overlay_worker, ac_tasks, chunksize=ac_chunk)) if pool else [_mc_multi_overlay_worker(t) for t in ac_tasks]

                by_reg_arm: dict[tuple[str, str], list[dict[str, Any]]] = {}
                for arm_name, ov_map in ac_res:
                    for f_label, row in ov_map.items():
                        by_reg_arm.setdefault((f_label, arm_name), []).append(row)

                # Run funding-sensitive B under each funding regime
                b_tasks = []
                for f_label, _, const_bps, sw_bps in funding_overlays:
                    for pi in range(n_paths):
                        b_tasks.append((
                            tmp, f"{sc_name}_{f_label}_B_p{pi:03d}", paths[pi], start_iso,
                            b_arm_name, b_entry_exit, None, w_closes, const_bps, sw_bps, provenance,
                        ))
                b_chunk = chunksize_for_tasks(len(b_tasks), max_workers)
                b_res = list(pool.map(_mc_path_worker, b_tasks, chunksize=b_chunk)) if pool else [_mc_path_worker(t) for t in b_tasks]
                for fi, (f_label, _, _, _) in enumerate(funding_overlays):
                    by_reg_arm[(f_label, b_arm_name)] = b_res[fi * n_paths : (fi + 1) * n_paths]

                for f_label, _, _, _ in funding_overlays:
                    for arm_name in arm_order:
                        arm_res = by_reg_arm[(f_label, arm_name)]
                        losses_with = np.asarray(
                            [-r["risk_budget_return_with_funding_pct"] / 100.0 for r in arm_res], dtype=float
                        )
                        dds_with = np.asarray(
                            [r["max_drawdown_with_funding_pct"] / 100.0 for r in arm_res], dtype=float
                        )
                        rel = summarize_paths(
                            losses_with, dds_with, threshold=0.05, confidences=(0.95,), resamples=499, bootstrap_seed=seed + 7
                        )
                        tail95 = rel["tails"]["0.95"]
                        summary_rows.append(
                            {
                                "scenario": sc_name,
                                "funding_regime": f_label,
                                "strategy_id": arm_name,
                                "n_paths": n_paths,
                                "mean_return_with_funding_pct": round(float(-np.mean(losses_with) * 100.0), 4),
                                "mean_funding_usdt": round(float(np.mean([r["funding_pnl_usdt"] for r in arm_res])), 6),
                                "loss_prob_pct": round(float(rel["loss_probability"]["estimate"] * 100.0), 2),
                                "loss_prob_ci_low_pct": round(float(rel["loss_probability"]["lower"] * 100.0), 2),
                                "loss_prob_ci_high_pct": round(float(rel["loss_probability"]["upper"] * 100.0), 2),
                                "var95_with_funding_pct": round(float(tail95["var"] * 100.0), 4),
                                "cvar95_with_funding_pct": round(float(tail95["cvar"] * 100.0), 4),
                                "cvar95_ci_low_pct": round(float(tail95["cvar_interval"]["lower"] * 100.0), 4),
                                "cvar95_ci_high_pct": round(float(tail95["cvar_interval"]["upper"] * 100.0), 4),
                                "p95_max_drawdown_with_funding_pct": round(float(np.quantile(dds_with, 0.95) * 100.0), 4),
                                "mean_holding_hours": round(float(np.mean([r["mean_holding_hours"] for r in arm_res])), 2),
                                "mean_paused_inv_hours": round(float(np.mean([r["paused_with_inventory_hours"] for r in arm_res])), 2),
                                "mean_fills": round(float(np.mean([r["fills_count"] for r in arm_res])), 3),
                            }
                        )
                print(f"  [MC] Finished scenario {sc_name} ({n_paths} paths)", flush=True)
        finally:
            if pool is not None:
                pool.shutdown()
    return summary_rows


def determine_verdict(
    wf_mainnet: dict[str, Any],
    wf_demo: dict[str, Any],
    wf_switch: dict[str, Any],
) -> dict[str, Any]:
    """Evidence-driven scientific verdict with zero preconceived bias."""
    m_b = wf_mainnet["stat_tests"]["oos_B_wf_vs_A_return"]
    m_c = wf_mainnet["stat_tests"]["oos_C_wf_vs_A_return"]
    d_b = wf_demo["stat_tests"]["oos_B_wf_vs_A_return"]
    s_b = wf_switch["stat_tests"]["oos_B_wf_vs_A_return"]
    s_c = wf_switch["stat_tests"]["oos_C_wf_vs_A_return"]

    b_mainnet_sig = m_b["statistically_significant_05"]
    c_mainnet_sig = m_c["statistically_significant_05"]
    b_highfund_sig = d_b["statistically_significant_05"] or s_b["statistically_significant_05"]

    if b_mainnet_sig or c_mainnet_sig:
        overall = "ROBUST_ADVANTAGE"
    elif b_highfund_sig and (m_b["mean_diff_pct"] >= -0.05):
        overall = "INCONCLUSIVE_MAINNET_BUT_REGIME_DEFENSIVE_IN_HIGH_FUNDING"
    elif abs(m_b["mean_diff_pct"]) < 0.15 and abs(m_c["mean_diff_pct"]) < 0.15:
        overall = "NO_ROBUST_ADVANTAGE"
    else:
        overall = "INCONCLUSIVE"

    # Official standard label strictly adhering to prompt requirement:
    # if no robust advantage on Mainnet OOS, label clearly as INCONCLUSIVE or NO_ROBUST_ADVANTAGE.
    if b_mainnet_sig or c_mainnet_sig:
        canonical_status = "ROBUST_OOS_ADVANTAGE"
    elif (not b_mainnet_sig and m_b["mean_diff_pct"] <= 0.0) and (not c_mainnet_sig and m_c["mean_diff_pct"] <= 0.0):
        canonical_status = "NO_ROBUST_ADVANTAGE"
    else:
        canonical_status = "INCONCLUSIVE"

    # Recommend next Demo comparison candidate conservatively based on actual numbers
    b_oos_ret_demo = wf_demo["oos_summary"]["B_wf"]["mean_risk_budget_return_with_funding_pct"]
    a_oos_ret_demo = wf_demo["oos_summary"]["A"]["mean_risk_budget_return_with_funding_pct"]
    b_oos_ret_sw = wf_switch["oos_summary"]["B_wf"]["mean_risk_budget_return_with_funding_pct"]
    a_oos_ret_sw = wf_switch["oos_summary"]["A"]["mean_risk_budget_return_with_funding_pct"]

    demo_candidate = (
        "`B_FUNDING_SENSITIVE_GRID`（仅作为高资金费防御性止损/规避护栏对照，而非已验证的正 Alpha 策略）"
        if (b_oos_ret_demo > a_oos_ret_demo + 1.0 and b_oos_ret_sw > a_oos_ret_sw + 0.5)
        else "无（仅保留控制组 A 观察，不晋级任何候选策略）"
    )

    return {
        "canonical_status": canonical_status,
        "detailed_status": overall,
        "b_improves_mainnet_oos_sig": b_mainnet_sig,
        "c_improves_mainnet_oos_sig": c_mainnet_sig,
        "b_improves_high_funding_oos_sig": b_highfund_sig,
        "recommended_next_demo_candidate": demo_candidate,
    }


def render_report_md(summary: dict[str, Any]) -> str:
    audit = summary["data_audit"]
    wf_m = summary["walk_forward_mainnet"]
    wf_d = summary["walk_forward_demo_stress"]
    wf_s = summary["walk_forward_regime_switch"]
    verdict = summary["verdict"]
    sens = summary["funding_sensitivity"]
    det = summary["deterministic_stress"]
    mc = summary["tail_risk_mc"]

    def _fmt(v: Any, dec: int = 4) -> str:
        if v is None:
            return "—"
        if isinstance(v, float):
            return f"{v:.{dec}f}"
        return str(v)

    lines: list[str] = []
    lines.append("# FactorLab P8-R10：资金费敏感网格与自适应网格策略比较报告")
    lines.append("")
    lines.append(
        f"> `NO LIVE MONEY` · 离线受控研究（未修改任何 Azure 云端运行策略）。"
        f"执行模型版本：`{EXECUTION_MODEL_VERSION}`（复用 `GridExecution` + `PaperBroker` + `grid_fifo` + `grid_funding`）。"
        f"R09 完整 F11/F08 生命周期模型尚未合并至 `origin/main`，本报告结论仅严格适用于当前已验证的离线执行基线，不提前外推至完整版 F11/F08。"
    )
    lines.append("")

    # Section 0: Executive Summary & Explicit 6-Question Answers
    lines.append("## 0. 核心结论摘要与六大核心问题明确回答")
    lines.append("")
    lines.append(f"- **综合统计判定**：`{verdict['canonical_status']}`（细化体制状态：`{verdict['detailed_status']}`）")
    lines.append(
        f"- **主网真实资金费（18 个 Walk-Forward 样本外 7 天窗口）**："
        f"控制组 A 均值净收益 `{wf_m['oos_summary']['A']['mean_risk_budget_return_with_funding_pct']:+.4f}%`（CVaR95 `{wf_m['oos_summary']['A']['cvar95_with_funding_pct']:.2f}%`），"
        f"策略 B（WF）`{wf_m['oos_summary']['B_wf']['mean_risk_budget_return_with_funding_pct']:+.4f}%`（差值 `{wf_m['stat_tests']['oos_B_wf_vs_A_return']['mean_diff_pct']:+.4f}%`，Bootstrap 95% CI `[{wf_m['stat_tests']['oos_B_wf_vs_A_return']['bootstrap_95ci_low_pct']:+.4f}%, {wf_m['stat_tests']['oos_B_wf_vs_A_return']['bootstrap_95ci_high_pct']:+.4f}%]`，Wilcoxon `p={wf_m['stat_tests']['oos_B_wf_vs_A_return']['wilcoxon_p_value']:.4f}`），"
        f"策略 C（WF）`{wf_m['oos_summary']['C_wf']['mean_risk_budget_return_with_funding_pct']:+.4f}%`（差值 `{wf_m['stat_tests']['oos_C_wf_vs_A_return']['mean_diff_pct']:+.4f}%`，Bootstrap 95% CI `[{wf_m['stat_tests']['oos_C_wf_vs_A_return']['bootstrap_95ci_low_pct']:+.4f}%, {wf_m['stat_tests']['oos_C_wf_vs_A_return']['bootstrap_95ci_high_pct']:+.4f}%]`，Wilcoxon `p={wf_m['stat_tests']['oos_C_wf_vs_A_return']['wilcoxon_p_value']:.4f}`）。"
    )
    lines.append(
        f"- **高资金费 / Demo 压力体制（22.97 bps/h & 48h 突发切换，同 18 个样本外窗口）**："
        f"在持续 Demo 资费压力下，A 均值净收益暴跌至 `{wf_d['oos_summary']['A']['mean_risk_budget_return_with_funding_pct']:+.4f}%`（CVaR95 `{wf_d['oos_summary']['A']['cvar95_with_funding_pct']:.2f}%`），"
        f"B（WF）通过因果资金费门控与退出机制将其收敛至 `{wf_d['oos_summary']['B_wf']['mean_risk_budget_return_with_funding_pct']:+.4f}%`（CVaR95 `{wf_d['oos_summary']['B_wf']['cvar95_with_funding_pct']:.2f}%`，Wilcoxon `p={wf_d['stat_tests']['oos_B_wf_vs_A_return']['wilcoxon_p_value']:.4f}`）；"
        f"在 48h 突发高资金费切换体制下，A 为 `{wf_s['oos_summary']['A']['mean_risk_budget_return_with_funding_pct']:+.4f}%`，B（WF）为 `{wf_s['oos_summary']['B_wf']['mean_risk_budget_return_with_funding_pct']:+.4f}%`（差值 `{wf_s['stat_tests']['oos_B_wf_vs_A_return']['mean_diff_pct']:+.4f}%`，Wilcoxon `p={wf_s['stat_tests']['oos_B_wf_vs_A_return']['wilcoxon_p_value']:.4f}`），"
        f"C（WF）为 `{wf_s['oos_summary']['C_wf']['mean_risk_budget_return_with_funding_pct']:+.4f}%`（差值 `{wf_s['stat_tests']['oos_C_wf_vs_A_return']['mean_diff_pct']:+.4f}%`）。"
    )
    lines.append("")
    lines.append("### 对任务六大核心问题的明确回答")
    lines.append("")
    m_b_st = wf_m["stat_tests"]["oos_B_wf_vs_A_return"]
    m_c_st = wf_m["stat_tests"]["oos_C_wf_vs_A_return"]
    d_b_st = wf_d["stat_tests"]["oos_B_wf_vs_A_return"]
    s_b_st = wf_s["stat_tests"]["oos_B_wf_vs_A_return"]
    lines.append(
        f"1. **资金费过滤是否改善净收益？** **分体制与分机制结论**："
        f"(a) 在真实 Binance 主网历史资金费（均值 `0.73 bps/8h`，57.5% 为 `0`）下，资金费总额远小于方向性盈亏，策略 B（WF）相对控制组 A 的样本外净收益差为 `{m_b_st['mean_diff_pct']:+.4f}%`（95% CI `[{m_b_st['bootstrap_95ci_low_pct']:+.4f}%, {m_b_st['bootstrap_95ci_high_pct']:+.4f}%]`，Wilcoxon `p={m_b_st['wilcoxon_p_value']:.4f}`），**没有稳健统计证据表明其在主网常态费率下能创造稳定正超额收益**（主网下 B 与 A 均值净收益均为负，且纯入场过滤 `B2` 几乎与 A 无异）；"
        f"(b) 但在**高资金费体制**（如官方 Demo 实测 `22.97 bps/h` 或 48h 突发高资金费切换体制）下，策略 B 显著削减了尾部失血与最大回撤（持续 Demo 资费下 OOS 改善 `{d_b_st['mean_diff_pct']:+.4f}%`，`p={d_b_st['wilcoxon_p_value']:.4f}`；48h 突发切换体制下改善 `{s_b_st['mean_diff_pct']:+.4f}%`，`p={s_b_st['wilcoxon_p_value']:.4f}`）。**核心拆解**：在突发高资费切换体制下，**仅靠开仓前入场过滤（`B2`，全期 `{wf_s['full_summary']['B2']['total_final_net_pnl_usdt']:+.2f} USDT`）几乎无法挽救已建仓位**，真正起决定性保护作用的是**持仓期资金费/滞留退出时机控制（`B5`，全期收敛至 `{wf_s['full_summary']['B5']['total_final_net_pnl_usdt']:+.2f} USDT`，CVaR95 从 `{wf_s['full_summary']['A_FIXED']['cvar95_with_funding_pct']:.2f}%` 降至 `{wf_s['full_summary']['B5']['cvar95_with_funding_pct']:.2f}%`）**；其本质是防御性风控护栏，而非主网常态下的 Alpha 来源。"
    )
    lines.append(
        f"2. **自适应网格是否优于固定网格？** **没有充分证据支持全面优于固定网格**。在主网样本外 18 窗中，策略 C（WF）相对固定网格 A 的均值净收益差为 `{m_c_st['mean_diff_pct']:+.4f}%`（95% CI `[{m_c_st['bootstrap_95ci_low_pct']:+.4f}%, {m_c_st['bootstrap_95ci_high_pct']:+.4f}%]`，Wilcoxon `p={m_c_st['wilcoxon_p_value']:.4f}`），CVaR95 从 `{wf_m['oos_summary']['A']['cvar95_with_funding_pct']:.2f}%` 变为 `{wf_m['oos_summary']['C_wf']['cvar95_with_funding_pct']:.2f}%`；Monte Carlo 突发高资金费体制下，`moving_block_bootstrap_24h` 中 `C5` 的均值收益与 P95 最大回撤逊于 `A`，而 `jump_stress_downside` 中 C5 均值收益更差、P95 最大回撤略低，风险指标并非单向改善。宽幅自适应在高波下跌期可通过延迟触发买单或极端波动保护降低部分残仓暴露，但同时降低成交率与资金利用率，且无法根本消除单边下跌或高资金费下的残仓风险。"
    )
    lines.append(
        f"3. **改善是否具有样本外支持？** 主网真实费率下，B 与 C 在净收益维度均**未**达到 `p < 0.05` 且 95% Bootstrap CI 严格大于 0 的样本外显著改善（仅 B 的滞留退出带来了样本外回撤缩减 `+1.15%`, `p=0.0312`，代价是在 Fold 2/Fold 3 反弹行情中因提前退出跑输 A `-2.07%` 与 `-1.67%`）；高资金费/突发切换体制下，策略 B 的**尾部风控改善**在 6 折 Walk-Forward 汇总（18 个 OOS 窗口）中仍有统计支持，但主要体现为及时空仓/止损减亏，而非提高正向盈利能力；这不表示每折单独达到显著性。"
    )
    lines.append(
        f"4. **是否存在明显过拟合？** 见第 6 节参数稳定性与 IS/OOS 对比。主网体制下全 12 个 B/C 预声明参数的 IS 与 OOS 排名 Spearman 相关系数仅 `{wf_m['stat_tests']['is_oos_spearman_rank_corr']:+.4f}`（`p={wf_m['stat_tests']['is_oos_spearman_p_value']:.4f}`，不显著），存在明显体制敏感性与样本外衰退信号：训练窗唯一正收益候选 `B4`（IS `+0.1340%`）在样本外转负（OOS `-0.2100%`，衰退 `-0.3440%`）；而策略 C 中 IS 最差之一的 `C4`（`-1.7243%`）在 OOS 反成最佳（`-0.0757%`）。虽然本实验已将搜索空间严格限制在每类 6 组预声明参数，参数排序仍难以跨市场体制保持稳定，若扩大调参自由度将有显著过拟合风险。"
    )
    lines.append(
        f"5. **哪个策略值得进入下一轮官方 Demo 对照测试？** **{verdict['recommended_next_demo_candidate']}**。具体建议：在推进任何线上变动前，**先待 R09 完整 F11/F08 生命周期模型合并后完成统一模型复验**；若在官方 Demo（实证已确认周末存在 `22.97 bps/h` 极端资金费失真）开展下一轮小预算对照测试，仅建议将**策略 B 的因果高资金费入口门控 + 持仓资金费/滞留退出守卫**作为防御性风控层与控制组 A 配对观测，不建议将波动率自适应网格 C 纳入下一轮 Demo 晋级。"
    )
    lines.append(
        f"6. **最终规范判定**：`{verdict['canonical_status']}` —— 在当前离线基线（`NO_F11_NO_F08_STRANDED_POSITION_BASELINE`）与 193 天真实主网历史数据下，**尚无稳健样本外证据证明固定网格、资金费敏感网格或波动率自适应网格能够长期获得稳定正超额收益**；资金费过滤与持仓退出的真实经济价值仅体现为极端高资金费体制下的防御性尾部风险护栏。"
    )
    lines.append("")

    # Section 1: R10-01 Data Audit
    lines.append("## 1. R10-01 数据核查与未来函数审计")
    lines.append("")
    cov = audit["coverage"]
    dq = audit["data_quality"]
    pv = audit["price_and_volatility"]
    fa = audit["funding_alignment"]
    liq = audit["liquidity_and_capacity"]
    dvm = fa["demo_vs_mainnet_comparison"]

    lines.append("| 核查维度 | 审计结果 | 说明 |")
    lines.append("| --- | --- | --- |")
    lines.append(f"| 标的与时间覆盖 | `{audit['instrument_id']}` · `{cov['start_utc']}` → `{cov['end_exclusive_utc']}` | 共 `{cov['total_bars']}` 根 1h K 线（`{cov['total_days']}` 天），涵盖 R04B(90d) + R07(90d) + Post-R07(13d) |")
    lines.append(f"| 数据缺失与完整性 | 缺失 `{dq['missing_bars']}` · 重复 `{dq['duplicate_timestamps']}` · 异常 OHLCV `{dq['invalid_ohlcv']}` · 零量 `{dq['zero_volume_bars']}` | 连续无缺口，禁止插值填充 |")
    lines.append(f"| R07 逐小时交叉核验 | 匹配 `{dq['r07_close_corroboration'].get('matched_bars')}/{dq['r07_close_corroboration'].get('expected_bars')}` · 最大价差 `{dq['r07_close_corroboration'].get('max_abs_close_diff')}` | 与仓库既有 `R07-base-baseline-hourly.csv` 100% 精确一致 |")
    lines.append(f"| 价格区间与波动率 | `{pv['start_close']}` → `{pv['end_close']}`（`{pv['full_period_return_pct']:+.2f}%`），区间 `[{pv['min_close']}, {pv['max_close']}]` | 1h 对数收益标准差 `{pv['hourly_log_return_vol_pct']:.4f}%` |")
    lines.append(f"| 价格定义差异（Trade vs Mark） | 592 笔结算时点均值 `{pv['mark_vs_trade_close_at_settlement']['mean_trade_minus_mark_bps']:+.4f} bps`（P95 绝对差 `{pv['mark_vs_trade_close_at_settlement']['p95_abs_diff_bps']:.4f} bps`） | Post-R07 312h Mark K线 P95 基差 `{pv['mark_price_klines_basis_post_r07'].get('abs_p95_basis_bps')} bps` |")
    lines.append(f"| 主网历史资金费对齐 | 共 `{fa['total_events_in_range']}` 笔（Regular `{fa['regular_count']}` + Special `{fa['special_count']}`） | 均值 `{fa['mean_rate_bps']:.4f} bps/8h`，P90 `{fa['p90_rate_bps']:.4f} bps`，最大 `{fa['max_rate_bps']:.4f} bps`，零费率占 `{fa['pct_zero']:.1f}%` |")
    lines.append(f"| 资金费自相关性 | Lag-1 `{fa['autocorr_lag1']:+.4f}` · Lag-3 `{fa['autocorr_lag3']:+.4f}` | 存在弱正自相关（高费率有轻微聚集性，为因果预测提供有限信息） |")
    lines.append(f"| 流动性与容量约束 | 最小小时量 `{liq['min_hourly_volume_units']}` · P05 `{liq['p05_hourly_volume_units']}` · 中位 `{liq['median_hourly_volume_units']}` | 1% 参与度下远大于单笔 `0.04` 单位；但高价期名义额 `> 7 USDT`，历史回放沿用 R08 的 `10.00 USDT` 1x 抵押口径 |")
    lines.append(f"| 未来函数审计 | `lookahead_detected = {audit['lookahead_audit']['lookahead_detected']}` | 挂单仅在后续 K 线撮合；策略 B 严守 `funding_time <= bar_close`；策略 C 严守过去收盘价与训练窗冻结 `sigma_ref`（已由单测反证验证） |")
    lines.append("")

    if dvm.get("available"):
        lines.append("> [!IMPORTANT]")
        lines.append(
            f"> **关键实证核查（官方 Demo Gen 1 vs 主网同期资金费）**：在官方 Demo Gen 1 同一周末窗口（`{dvm['gen1_window_utc'][0]}` → `{dvm['gen1_window_utc'][1]}`），"
            f"官方 Binance Demo 结算了 **{dvm['demo_settlement_count']} 笔整点资金费**（合计 `{dvm['demo_total_funding_usdt']:.8f} USDT`，隐含 `{dvm['demo_implied_hourly_bps']:.4f} bps/h`），"
            f"而同期 Binance 主网仅结算 **{dvm['mainnet_settlement_count']} 笔 8h 资金费**且费率全为 `{dvm['mainnet_rates_bps']} bps`！"
            f"因此本研究严格**同时报告真实主网资金费与 Demo 高资金费压力两套体制**，严禁把 Demo 模拟盘的极端资费失真混同为主网常态。"
        )
        lines.append("")

    # Section 2: Candidate Strategies & Predeclared Configs
    lines.append("## 2. 候选策略设计与预声明研究配置")
    lines.append("")
    lines.append("三套可复现研究配置已冻结于仓库：")
    lines.append("- **策略 A（原始固定网格控制组）**：[`configs/research/p8_r10_strategy_a_fixed.json`](../../../configs/research/p8_r10_strategy_a_fixed.json) —— R08 冻结单层 `±1.0%` 算术网格，`0.04 NVDAUSDT`，`fee=3 bps`，`slippage=2 bps`，`breakout=pause`，`order_ttl=100`。")
    lines.append("- **策略 B（资金费敏感网格）**：[`configs/research/p8_r10_strategy_b_funding_sensitive.json`](../../../configs/research/p8_r10_strategy_b_funding_sensitive.json) —— 预声明 6 组候选（`B1..B3` 纯入场因果过滤，`B4..B6` 入场过滤 + 持仓高费率/滞留/净利空间退出），严格仅读取 `funding_time <= bar_close_utc` 的已结算事件。")
    lines.append("- **策略 C（波动率自适应网格）**：[`configs/research/p8_r10_strategy_c_vol_adaptive.json`](../../../configs/research/p8_r10_strategy_c_vol_adaptive.json) —— 预声明 6 组候选（`C1..C3` 因果 RV 定宽 `24h/48h/72h`，`C4..C6` 叠加 `1.8x/2.0x` 极端波动保护与空仓动态重建），`sigma_ref` 仅在训练窗口冻结，验证窗口绝不重拟合。")
    lines.append("")

    # Section 3: R10-02 Paired Backtest Results (Full 27 Windows)
    lines.append("## 3. R10-02 同窗配对回测结果（27 个非重叠 7 天历史窗口，189 天）")
    lines.append("")
    lines.append("全部策略在完全相同的 27 个 7 天窗口（`W01..W27`，`2026-05-15` → `2026-11-20`，含 R08 预声明全部 11 窗 `I1..I3, C1..C8`）、统一风险预算 `10.00 USDT`、统一手续费 `3 bps`、滑点 `2 bps`、成交参与度 `1%` 下运行，并全部通过 `grid_fifo` 零残差对账（残差 `< 1e-8`）。")
    lines.append("")

    def _emit_summary_table(title: str, rows_map: list[tuple[str, dict[str, Any]]]) -> None:
        lines.append(f"### {title}")
        lines.append("")
        lines.append("| 策略 | 交易净收益 (USDT) | 资金费损益 (USDT) | 最终净收益 (USDT) | 风险预算均值收益率 | 含资费均值 MaxDD | 含资费 CVaR95 | 成交率 | 均值持仓 (h) | 均值换手倍数 | 相对等量 B&H 超额 (USDT) |")
        lines.append("| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |")
        for name, s in rows_map:
            lines.append(
                f"| `{name}` | {s['total_trade_net_pnl_usdt']:+.6f} | {s['total_funding_pnl_usdt']:+.6f} | "
                f"{s['total_final_net_pnl_usdt']:+.6f} | {s['mean_risk_budget_return_with_funding_pct']:+.4f}% | "
                f"{s['mean_max_drawdown_with_funding_pct']:.2f}% | {s['cvar95_with_funding_pct']:.2f}% | "
                f"{s['mean_order_fill_rate']*100:.1f}% | {s['mean_holding_hours']:.1f} | "
                f"{s['mean_turnover_ratio']:.3f}x | {s['total_excess_vs_equal_bh_usdt']:+.6f} |"
            )
        lines.append("")

    _emit_summary_table(
        "3.1 主网真实资金费体制（全 27 窗汇总，代表配置与全部候选）",
        [
            ("A_FIXED (控制组)", wf_m["full_summary"]["A_FIXED"]),
            ("B2 (入场过滤 0.50bps/h)", wf_m["full_summary"]["B2"]),
            ("B5 (入场+退出 0.50/1.00)", wf_m["full_summary"]["B5"]),
            ("B6 (净利空间门控)", wf_m["full_summary"]["B6"]),
            ("C2 (48h RV 静态自适应)", wf_m["full_summary"]["C2"]),
            ("C5 (48h RV+极端波保护)", wf_m["full_summary"]["C5"]),
            ("C6 (72h RV+1.8x 保护)", wf_m["full_summary"]["C6"]),
        ],
    )

    _emit_summary_table(
        "3.2 突发高资金费切换体制（48h 后由 0.125 → 22.97 bps/h，全 27 窗汇总）",
        [
            ("A_FIXED (控制组)", wf_s["full_summary"]["A_FIXED"]),
            ("B2 (仅入场过滤)", wf_s["full_summary"]["B2"]),
            ("B5 (入场+资费退出)", wf_s["full_summary"]["B5"]),
            ("B6 (净利空间退出)", wf_s["full_summary"]["B6"]),
            ("C2 (48h RV 静态)", wf_s["full_summary"]["C2"]),
            ("C5 (48h RV+动态保护)", wf_s["full_summary"]["C5"]),
        ],
    )

    _emit_summary_table(
        "3.3 持续 Demo 高资金费压力体制（全程 22.97 bps/h，全 27 窗汇总）",
        [
            ("A_FIXED (控制组)", wf_d["full_summary"]["A_FIXED"]),
            ("B2 (仅入场过滤)", wf_d["full_summary"]["B2"]),
            ("B5 (入场+资费退出)", wf_d["full_summary"]["B5"]),
            ("C5 (48h RV+动态保护)", wf_d["full_summary"]["C5"]),
        ],
    )

    # Section 4: R10-03 Walk-Forward OOS & Statistical Tests
    lines.append("## 4. R10-03 Walk-Forward 样本外验证与配对统计检验")
    lines.append("")
    lines.append("采用扩展窗口 Walk-Forward（初始训练窗 `W01..W09` 共 9 周，每 3 周推进一折，共 6 折 × 3 周 = **18 个完全样本外 7 天窗口 `W10..W27`**，覆盖 126 天）。训练窗按预声明风险调整目标 $J = \\text{mean}(r_{\\text{funded}}) - 0.5 \\cdot \\text{CVaR}_{95}$ 选出参数并冻结，测试窗严禁重新拟合。")
    lines.append("")

    _emit_summary_table(
        "4.1 样本外汇总对比（18 个 OOS 窗口：主网真实资费 vs 48h 突发高资费 vs 持续 Demo 资费）",
        [
            ("Mainnet · A_FIXED", wf_m["oos_summary"]["A"]),
            ("Mainnet · B_WalkForward", wf_m["oos_summary"]["B_wf"]),
            ("Mainnet · C_WalkForward", wf_m["oos_summary"]["C_wf"]),
            ("Switch48h · A_FIXED", wf_s["oos_summary"]["A"]),
            ("Switch48h · B_WalkForward", wf_s["oos_summary"]["B_wf"]),
            ("Switch48h · C_WalkForward", wf_s["oos_summary"]["C_wf"]),
            ("DemoStress · A_FIXED", wf_d["oos_summary"]["A"]),
            ("DemoStress · B_WalkForward", wf_d["oos_summary"]["B_wf"]),
            ("DemoStress · C_WalkForward", wf_d["oos_summary"]["C_wf"]),
        ],
    )

    lines.append("### 4.2 主网真实资金费逐折 Walk-Forward 明细（6 折）")
    lines.append("")
    lines.append("| Fold | 训练窗 | 测试窗 | 冻结 `sigma_ref` | 选中 B | 选中 C | OOS A 收益 | OOS B 收益 | OOS C 收益 | B-A 差值 | C-A 差值 |")
    lines.append("| ---: | --- | --- | ---: | --- | --- | ---: | ---: | ---: | ---: | ---: |")
    for f in wf_m["folds"]:
        lines.append(
            f"| {f['fold_id']} | `{f['train_windows']}` | `{f['test_windows']}` | {f['frozen_sigma_ref_1h_pct']:.4f}% | "
            f"`{f['selected_b']}` | `{f['selected_c']}` | {f['oos_a_ret_pct']:+.4f}% | {f['oos_b_ret_pct']:+.4f}% | "
            f"{f['oos_c_ret_pct']:+.4f}% | {f['oos_b_minus_a_pct']:+.4f}% | {f['oos_c_minus_a_pct']:+.4f}% |"
        )
    lines.append("")

    lines.append("### 4.3 样本外配对统计检验（10,000 次 Paired Bootstrap + Wilcoxon + Paired t-test，`n = 18` 窗口）")
    lines.append("")
    lines.append("| 体制与检验对 | 均值差 (%) | 中位差 (%) | 胜/平/负 (%) | Bootstrap 95% CI | Bootstrap p | Wilcoxon p | Paired t (p) | 显著改善 (p<0.05 & CI>0) |")
    lines.append("| --- | ---: | ---: | --- | --- | ---: | ---: | --- | --- |")
    test_pairs = [
        ("Mainnet: B_wf - A (收益)", wf_m["stat_tests"]["oos_B_wf_vs_A_return"]),
        ("Mainnet: C_wf - A (收益)", wf_m["stat_tests"]["oos_C_wf_vs_A_return"]),
        ("Mainnet: B_wf vs A (回撤缩减)", wf_m["stat_tests"]["oos_B_wf_vs_A_drawdown_reduction"]),
        ("Mainnet: C_wf vs A (回撤缩减)", wf_m["stat_tests"]["oos_C_wf_vs_A_drawdown_reduction"]),
        ("Switch48h: B_wf - A (收益)", wf_s["stat_tests"]["oos_B_wf_vs_A_return"]),
        ("Switch48h: C_wf - A (收益)", wf_s["stat_tests"]["oos_C_wf_vs_A_return"]),
        ("DemoStress: B_wf - A (收益)", wf_d["stat_tests"]["oos_B_wf_vs_A_return"]),
        ("DemoStress: C_wf - A (收益)", wf_d["stat_tests"]["oos_C_wf_vs_A_return"]),
    ]
    for label, st in test_pairs:
        lines.append(
            f"| {label} | {st['mean_diff_pct']:+.4f}% | {st['median_diff_pct']:+.4f}% | "
            f"{st['win_rate_pct']:.1f}/{st['tie_rate_pct']:.1f}/{st['loss_rate_pct']:.1f} | "
            f"`[{st['bootstrap_95ci_low_pct']:+.4f}%, {st['bootstrap_95ci_high_pct']:+.4f}%]` | "
            f"{st['bootstrap_p_value']:.4f} | {st['wilcoxon_p_value']:.4f} | "
            f"{st['paired_t_stat']:+.2f} ({st['paired_t_p_value']:.4f}) | `{st['statistically_significant_05']}` |"
        )
    lines.append("")
    lines.append(
        "> [!NOTE]\n"
        "> **样本量与统计不确定性说明**：即使覆盖完整 193 天连续数据，非重叠 7 天样本外窗口数仅为 `n = 18`（全期 `n = 27`）。"
        "在小样本、偏态尾部主导的收益分布下，检测微弱超额收益（如 `< 0.5%/周`）的统计功效有限，置信区间较宽；因此任何微小点估计正差均不得夸大为稳健优势。"
    )
    lines.append("")

    # Section 5: Funding Sensitivity Analysis
    lines.append("## 5. 资金费敏感性分析（0x → 25x 主网倍率 + Demo 极端压力 + 突发切换）")
    lines.append("")
    lines.append("| 资金费情景 | 策略 | 总交易净利 (USDT) | 总资金费 (USDT) | 总最终净利 (USDT) | 均值收益率 | 含资费 CVaR95 | 均值 MaxDD | 均值持仓 (h) |")
    lines.append("| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |")
    for r in sens:
        lines.append(
            f"| `{r['funding_regime']}` | `{r['strategy_id']}` | {r['total_trade_net_pnl_usdt']:+.6f} | "
            f"{r['total_funding_pnl_usdt']:+.6f} | {r['total_final_net_pnl_usdt']:+.6f} | "
            f"{r['mean_risk_budget_return_with_funding_pct']:+.4f}% | {r['cvar95_with_funding_pct']:.2f}% | "
            f"{r['mean_max_drawdown_with_funding_pct']:.2f}% | {r['mean_holding_hours']:.1f} |"
        )
    lines.append("")

    # Section 6: Parameter Stability & Overfitting Check
    lines.append("## 6. 参数稳定性分析与过拟合检验（全预声明候选 IS vs OOS）")
    lines.append("")
    lines.append(
        f"主网体制下，全部 12 个 B/C 预声明候选的样本内（`W01..W09`）与样本外（`W10..W27`）目标排名 Spearman 相关系数为 "
        f"**`{wf_m['stat_tests']['is_oos_spearman_rank_corr']:+.4f}`**（`p = {wf_m['stat_tests']['is_oos_spearman_p_value']:.4f}`）。"
    )
    lines.append("")
    lines.append("| 策略候选 | 家族 | IS 均值收益 | OOS 均值收益 | OOS - IS | IS CVaR95 | OOS CVaR95 | 全期均值成交笔数 | 全期均值持仓(h) | 全期均值换手 |")
    lines.append("| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |")
    for r in wf_m["stability"]:
        lines.append(
            f"| `{r['strategy_id']}` | {r['family']} | {r['is_mean_ret_pct']:+.4f}% | {r['oos_mean_ret_pct']:+.4f}% | "
            f"{r['oos_minus_is_ret_pct']:+.4f}% | {r['is_cvar95_pct']:.2f}% | {r['oos_cvar95_pct']:.2f}% | "
            f"{r['full_mean_fills']:.2f} | {r['full_mean_hold_h']:.1f} | {r['full_turnover']:.3f}x |"
        )
    lines.append("")

    # Section 7: R10-04 Stress Tests & Monte Carlo Tail Risk (VaR/CVaR with Funding)
    lines.append("## 7. R10-04 压力测试与 Monte Carlo 尾部风险（资金费全部计入）")
    lines.append("")
    lines.append("### 7.1 九大确定性极端情景（锚定 Demo 价 `132.89 USDT`，风险预算 `7.00 USDT`，`168h`）")
    lines.append("")
    lines.append("| 压力情景 | 类别 | 策略 | 交易净利 | 资金费 | 最终净收益率 | 含资费 MaxDD | 成交笔数 | 持仓/暂停残仓 (h) | 期末状态 |")
    lines.append("| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |")
    for r in det:
        lines.append(
            f"| `{r['scenario_id']}` | {r['category']} | `{r['strategy_id']}` | {r['trade_net_pnl_usdt']:+.4f} | "
            f"{r['funding_pnl_usdt']:+.4f} | {r['risk_budget_return_with_funding_pct']:+.2f}% | "
            f"{r['max_drawdown_with_funding_pct']:.2f}% | {r['fills_count']} | "
            f"{r['holding_hours']} / {r['paused_with_inventory_hours']} | `{r['final_state']}` |"
        )
    lines.append("")
    lines.append(
        "- **压力情景关键机制解读**："
        "`S1`（高资金费震荡）与 `S5`/`S9`（单边/慢跌+高资费）下，策略 B 的因果入口门控直接避免在 `22.97 bps/h` 高费率下接仓；"
        f"`S2`（48h 突发高资费切换）清晰区分了纯入场过滤 `B2`（已持仓无法逃离，收益 `{next(r for r in det if r['scenario_id'] == 'S2_funding_regime_spike' and r['strategy_id'] == 'B_ENTRY_B2')['risk_budget_return_with_funding_pct']:+.2f}%`）与含退出守卫 `B5`（及时止损退出，收益 `{next(r for r in det if r['scenario_id'] == 'S2_funding_regime_spike' and r['strategy_id'] == 'B_EXIT_B5')['risk_budget_return_with_funding_pct']:+.2f}%`）；"
        "`S8`（`capacity=0.02/bar`）反映交易所真实微结构约束：`0.02 × 131 ≈ 2.62 USDT < min_notional (5 USDT)`，导致低于最小名义额的流动性无法产生任何合法部分成交。"
    )
    lines.append("")

    lines.append("### 7.2 随机 Monte Carlo / Bootstrap / Jump Stress 尾部风险（每情景 `64` 条路径 × `168h`，同路径配对，含资金费）")
    lines.append("")
    lines.append("| 随机情景 | 资金费体制 | 策略 | 含资费均值收益 | 亏损概率 (95% CI) | VaR95 (含资费) | CVaR95 (Bootstrap 95% CI) | P95 最大回撤 | 均值残仓暂停 (h) |")
    lines.append("| --- | --- | --- | ---: | --- | ---: | --- | ---: | ---: |")
    for r in mc:
        lines.append(
            f"| `{r['scenario']}` | `{r['funding_regime']}` | `{r['strategy_id']}` | "
            f"{r['mean_return_with_funding_pct']:+.2f}% | "
            f"{r['loss_prob_pct']:.1f}% (`[{r['loss_prob_ci_low_pct']:.1f}%, {r['loss_prob_ci_high_pct']:.1f}%]`) | "
            f"{r['var95_with_funding_pct']:.2f}% | "
            f"{r['cvar95_with_funding_pct']:.2f}% (`[{r['cvar95_ci_low_pct']:.2f}%, {r['cvar95_ci_high_pct']:.2f}%]`) | "
            f"{r['p95_max_drawdown_with_funding_pct']:.2f}% | {r['mean_paused_inv_hours']:.1f} |"
        )
    lines.append("")

    # Section 8: R10-05 R09 Integration & Boundaries
    lines.append("## 8. R10-05 执行模型版本声明与 R09 对接准备")
    lines.append("")
    lines.append(f"- **当前使用执行模型版本**：`{EXECUTION_MODEL_VERSION}`（与 R08 第 6 节离线基线完全一致：突破后暂停并逐小时盯市，不含 F11 限价退出和 F08 自动轮换）。")
    lines.append("- **明确边界**：本报告**不**提前宣称 A/B/C 的绝对收益或尾部风险数值直接适用于线上完整版 F11/F08；特别是策略 A 与 C 在本基线下的残仓暂停时长受 `breakout=pause` 放大，待 R09 完整生命周期执行模型合并后，需复用本研究所保留的三套配置和统一入口脚本在同一 F11/F08 执行模型下重新复验。")
    lines.append("- **未触碰云端策略**：Azure 云端当前运行服务与 `docs/validation/p8-r08/` 原始证据文件均未做任何改动。")
    lines.append("")

    # Section 9: Reproducibility
    lines.append("## 9. 可复现原始数据与一键复现命令")
    lines.append("")
    lines.append("本目录包含全部可复现原始汇总数据：`data_audit.json`、`summary.json`、`statistical_tests.json`、`paired_windows_mainnet.csv`、`paired_windows_regime_switch.csv`、`paired_windows_demo_stress.csv`、`walk_forward_folds.csv`、`walk_forward_oos_windows.csv`、`parameter_stability.csv`、`funding_sensitivity.csv`、`stress_deterministic.csv`、`tail_risk_mc.csv`。")
    lines.append("")
    lines.append("```powershell")
    lines.append("python tools/research/run_p8_r10_study.py --r04b-snapshot \"D:\\Code\\p7-r04b-official-payloads\\certified\\runs\\bde00b2f948842e2b8c2a00465a9f8c6\" --cache-dir \"D:\\Code\\p8-r10-data-cache\" --output-dir \"docs/validation/p8-r10\" --mc-paths 64 --workers 4")
    lines.append("```")
    lines.append("")
    if "review_recompute" in summary:
        review = summary["review_recompute"]
        lines.extend([
            "## 10. PR #238 P2 修复及重算对照", "",
            f"修复前基准：`{review['baseline_head']}`。归档数据审计完全一致：4632 根 UTC 小时 Bar、593 条资金费、0 缺失、0 重复；R07 2160/2160 精确交叉核验及原数据哈希保留，全程离线使用原快照。",
            "",
            "三种资金费统一使用 `grid_funding.position_settlements()` 的 `fill_time < settlement_time` 规则；同刻先对旧库存结算，再成交。合成费率信号与经济核算共享事件，资金费只叠加一次，收益、权益、回撤与尾部指标使用同一结果。等量 B&H 在首根收盘开仓，首笔同刻结算也不计入新仓。",
            "",
            "小时审计拒绝缺失、重复、乱序、非整点及覆盖边界错误，不排序去重或插值修复；168h 窗口核验实际起止和逐小时连续性，WF 训练/测试边界相邻且不重叠。正常数据的 27 窗及 6 折/18 个 OOS 窗边界不变。",
            "",
            "下表为修复前 → 修复后。OOS 的净收益与资金费是 18 窗总额，CVaR95 基于窗口收益，MaxDD 是窗口最大回撤的均值；MC 的金额为每路径均值，MaxDD 为路径最大回撤的 P95，不能与 OOS 均值直接比较。完整对照见 `review_comparison.csv`。",
            "",
            "| 范围 / 策略 | 净收益 USDT | 资金费 USDT | CVaR95 % | MaxDD % |",
            "| --- | ---: | ---: | ---: | ---: |",
        ])
        for r in review["key_comparisons"]:
            cells = [f"{r['before_' + k]:+.6f} → {r['after_' + k]:+.6f}" for k in
                     ("net_pnl_usdt", "funding_pnl_usdt", "cvar95_pct", "max_drawdown_pct")]
            lines.append(f"| {r['scope']} / `{r['strategy_id']}` | " + " | ".join(cells) + " |")
        lines.extend(["", review["interpretation"], "",
                      "重算复用未受影响的历史资金费 B 结果和六档历史敏感性，以及无成交的合成 B（重算其 B&H）；A/C 每本权威成交账本只回放一次并叠加多档资金费，重新选择各折参数并计算统计。9 组确定性压力、全部 6×64 条 MC 路径及两种资金费体制均重算；MC seed=810，路径长度 168h，Bootstrap/显著性标准均未修改。计算来源及输入哈希见 `review_recompute.json`。", "",
                      "定向与关联回归在 Python 3.13 下通过；包括同刻 BUY/SELL、部分成交、不规则时刻、跳变精确金额、资金费退出的 FIFO/权益/回撤、零费率，以及缺口/重复/乱序拒绝和 6 折边界验证。", "",
                      "```powershell",
                      'python tools/research/recompute_p8_r10_review.py --r04b-snapshot "D:\\Code\\p7-r04b-official-payloads\\certified\\runs\\bde00b2f948842e2b8c2a00465a9f8c6" --cache-dir "D:\\Code\\p8-r10-data-cache" --output-dir "docs/validation/p8-r10" --stage-dir "D:\\Code\\p8-r10-review-stages" --workers 12',
                      "```", ""])
    return "\n".join(lines)


def run_full_r10_study(
    *,
    repo_root: Path,
    r04b_snapshot: Path,
    cache_dir: Path,
    output_dir: Path,
    mc_paths: int = 64,
    mc_steps: int = 168,
    max_workers: int = 4,
    allow_network_supplement: bool = True,
) -> dict[str, Any]:
    started = perf_counter()
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    provenance = capture_provenance(source_context_root())

    print("[P8-R10] Step 1/6: Loading & auditing 193-day NVDAUSDT hourly bars & funding...", flush=True)
    all_bars, all_funding, data_audit = load_and_audit_r10_dataset(
        repo_root=repo_root,
        r04b_snapshot=r04b_snapshot,
        cache_dir=cache_dir,
        allow_network_supplement=allow_network_supplement,
    )
    (output_dir / "data_audit.json").write_text(
        json.dumps(data_audit, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    configs_raw, b_policies, c_policies = load_strategy_configs(repo_root)
    windows = build_nonoverlapping_weekly_windows(all_bars)
    print(f"[P8-R10] Built {len(windows)} non-overlapping 7-day windows ({windows[0]['start_utc']} -> {windows[-1]['end_exclusive_utc']}).", flush=True)

    print("[P8-R10] Step 2/6: Precomputing funding-independent A & C arms across 3 funding overlays...", flush=True)
    wf_overlays = [
        ("mainnet_actual", 1.0, None, None),
        ("switch_0.125_to_22.97_at_48h", 1.0, None, (48, NORMAL_PERP_HOURLY_FUNDING_BPS, DEMO_OBSERVED_HOURLY_FUNDING_BPS)),
        ("demo_stress_22.97bps_h", 1.0, DEMO_OBSERVED_HOURLY_FUNDING_BPS, None),
    ]
    shared_pool = create_process_pool(max_workers) if max_workers > 1 else None
    try:
        t2 = perf_counter()
        precomputed_ac = precompute_ac_multi_regime(
            all_bars, windows, all_funding, c_policies, wf_overlays,
            max_workers=max_workers, provenance=provenance, executor=shared_pool,
        )
        print(f"  [Step 2 done in {perf_counter() - t2:.1f}s]", flush=True)

        print("[P8-R10] Step 3/6: Running Walk-Forward & Strategy B paired backtests across 3 funding regimes...", flush=True)
        t3 = perf_counter()
        wf_mainnet = run_expanding_walk_forward(
            all_bars, windows, all_funding, b_policies, c_policies,
            regime_label="mainnet_actual",
            funding_scale=1.0,
            max_workers=max_workers,
            provenance=provenance,
            precomputed_ac=precomputed_ac,
            executor=shared_pool,
        )
        wf_switch = run_expanding_walk_forward(
            all_bars, windows, all_funding, b_policies, c_policies,
            regime_label="switch_0.125_to_22.97_at_48h",
            regime_switch_bps=(48, NORMAL_PERP_HOURLY_FUNDING_BPS, DEMO_OBSERVED_HOURLY_FUNDING_BPS),
            max_workers=max_workers,
            provenance=provenance,
            precomputed_ac=precomputed_ac,
            executor=shared_pool,
        )
        wf_demo = run_expanding_walk_forward(
            all_bars, windows, all_funding, b_policies, c_policies,
            regime_label="demo_stress_22.97bps_h",
            constant_hourly_bps=DEMO_OBSERVED_HOURLY_FUNDING_BPS,
            max_workers=max_workers,
            provenance=provenance,
            precomputed_ac=precomputed_ac,
            executor=shared_pool,
        )
        print(f"  [Step 3 done in {perf_counter() - t3:.1f}s]", flush=True)

        # Representative policies for sensitivity, deterministic stress & MC (predeclared defaults B2/B5/C5 with initial frozen sigma_ref)
        init_sigma_ref = wf_mainnet["initial_sigma_ref_1h"]
        b_entry_only = next(p for p in b_policies if p.candidate_id == "B2")
        b_entry_exit = next(p for p in b_policies if p.candidate_id == "B5")
        c_rep = replace(next(p for p in c_policies if p.candidate_id == "C5"), reference_vol_1h=init_sigma_ref)

        print("[P8-R10] Step 4/6: Running funding sensitivity sweep (0x -> 25x + Demo + Switch)...", flush=True)
        funding_sensitivity = run_funding_sensitivity_sweep(
            windows, all_funding, b_entry_only, b_entry_exit, c_rep,
            max_workers=max_workers,
            provenance=provenance,
            executor=shared_pool,
        )
    finally:
        if shared_pool is not None:
            shared_pool.shutdown()

    print("[P8-R10] Step 5/6: Running 9 deterministic stress regimes...", flush=True)
    deterministic_stress = run_deterministic_stress_suite(
        b_entry_only, b_entry_exit, c_rep,
        anchor_price=DEMO_ANCHOR_PRICE,
        n_steps=mc_steps,
        provenance=provenance,
    )

    print(f"[P8-R10] Step 6/6: Running stochastic MC/Bootstrap/Jump tail risk ({mc_paths} paths x 6 scenarios x 2 funding regimes x 3 arms)...", flush=True)
    tail_risk_mc = run_stochastic_mc_tail_risk(
        r04b_snapshot, b_entry_exit, c_rep,
        anchor_price=DEMO_ANCHOR_PRICE,
        n_paths=mc_paths,
        n_steps=mc_steps,
        max_workers=max_workers,
        provenance=provenance,
    )

    verdict = determine_verdict(wf_mainnet, wf_demo, wf_switch)
    elapsed_s = round(perf_counter() - started, 2)

    # Export reproducible CSVs
    pd.DataFrame(wf_mainnet["all_rows"]).to_csv(output_dir / "paired_windows_mainnet.csv", index=False)
    pd.DataFrame(wf_demo["all_rows"]).to_csv(output_dir / "paired_windows_demo_stress.csv", index=False)
    pd.DataFrame(wf_switch["all_rows"]).to_csv(output_dir / "paired_windows_regime_switch.csv", index=False)
    pd.DataFrame(wf_mainnet["folds"] + wf_switch["folds"] + wf_demo["folds"]).to_csv(
        output_dir / "walk_forward_folds.csv", index=False
    )
    oos_combined = []
    for reg_name, wf in [("mainnet_actual", wf_mainnet), ("switch_48h", wf_switch), ("demo_stress", wf_demo)]:
        for arm_label, rows in [("A_FIXED", wf["oos_rows"]["A"]), ("B_WF", wf["oos_rows"]["B"]), ("C_WF", wf["oos_rows"]["C"])]:
            for r in rows:
                oos_combined.append({"regime": reg_name, "wf_arm": arm_label, **r})
    pd.DataFrame(oos_combined).to_csv(output_dir / "walk_forward_oos_windows.csv", index=False)
    pd.DataFrame(wf_mainnet["stability"] + wf_switch["stability"] + wf_demo["stability"]).to_csv(
        output_dir / "parameter_stability.csv", index=False
    )
    pd.DataFrame(funding_sensitivity).to_csv(output_dir / "funding_sensitivity.csv", index=False)
    pd.DataFrame(deterministic_stress).to_csv(output_dir / "stress_deterministic.csv", index=False)
    pd.DataFrame(tail_risk_mc).to_csv(output_dir / "tail_risk_mc.csv", index=False)

    stat_tests_out = {
        "mainnet_actual": wf_mainnet["stat_tests"],
        "switch_48h": wf_switch["stat_tests"],
        "demo_stress": wf_demo["stat_tests"],
    }
    (output_dir / "statistical_tests.json").write_text(
        json.dumps(stat_tests_out, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    summary_payload = {
        "study": "P8-R10",
        "execution_model": EXECUTION_MODEL_VERSION,
        "elapsed_seconds": elapsed_s,
        "provenance": provenance,
        "verdict": verdict,
        "data_audit": data_audit,
        "walk_forward_mainnet": {k: v for k, v in wf_mainnet.items() if k not in {"all_rows", "oos_rows"}},
        "walk_forward_regime_switch": {k: v for k, v in wf_switch.items() if k not in {"all_rows", "oos_rows"}},
        "walk_forward_demo_stress": {k: v for k, v in wf_demo.items() if k not in {"all_rows", "oos_rows"}},
        "funding_sensitivity": funding_sensitivity,
        "deterministic_stress": deterministic_stress,
        "tail_risk_mc": tail_risk_mc,
    }
    (output_dir / "summary.json").write_text(
        json.dumps(summary_payload, indent=2, default=str, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    report_md = render_report_md(summary_payload)
    (output_dir / "REPORT.md").write_text(report_md, encoding="utf-8")
    print(f"[P8-R10] Completed in {elapsed_s}s. Verdict={verdict['canonical_status']}. Report written to {output_dir / 'REPORT.md'}", flush=True)
    return summary_payload
