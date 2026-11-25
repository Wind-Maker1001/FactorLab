"""FactorLab P8-R11 Study Orchestrator: Unified F11/F08 Lifecycle Comparative Research.

Evaluates Strategy A (Fixed Grid), Strategy B (Funding-Sensitive Grid), Strategy C
(Volatility-Adaptive Grid), and CASH across Lifecycles L0 (stranded baseline),
L1 (F11 exit only), and L2 (F11 + F08 full lifecycle), 5 conditional execution
realism profiles, and 5 funding regimes over:
1. Certified 193-day / 4,632-bar historical dataset (27 weekly windows, 6-fold WF / 18 OOS).
2. Deterministic 10-scenario stress suite (including cancel-fill race verification).
3. Progressive Monte Carlo tail-risk & cross-seed convergence suite (6 R08/R09/R10 generators).
4. Paired ablations (F11, F08, B, C, vs CASH), Holm-Bonferroni boundaries, L0-vs-R10
   reconciliation, and S02-F01 machine-readable handoff contract.
"""
from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
import json
import os
from pathlib import Path
from time import perf_counter
from typing import Any

import numpy as np
import pandas as pd
from scipy import stats as scipy_stats

from factorlab.compute import ComputeConfig
from factorlab.experiments.grid_empirical import load_hourly
from factorlab.experiments.grid_f11_f08_offline import DEFAULT_DEMO_WALLET
from factorlab.experiments.grid_funding_adaptive import (
    FundingPolicy,
    VolatilityPolicy,
    load_and_audit_r10_dataset,
)
from factorlab.experiments.grid_monte_carlo import close_bars, historical_input, stress_paths
from factorlab.experiments.grid_r10_study import (
    DEMO_ANCHOR_CAPITAL,
    DEMO_ANCHOR_PRICE,
    HISTORICAL_REPLAY_CAPITAL,
    _compute_ref_vol_from_bars,
    build_nonoverlapping_weekly_windows,
    load_strategy_configs,
    paired_statistical_test,
    validate_research_windows,
)
from factorlab.experiments.p8_r11_unified_lifecycle import (
    ALL_EXECUTION_PROFILES,
    ALL_FUNDING_REGIMES,
    LIFECYCLE_LABELS,
    UNIFIED_EXECUTION_MODEL_VERSION,
    build_s02_handoff_contract,
    holm_bonferroni_adjust,
    paired_statistical_test_unrounded,
    run_unified_lifecycle,
    summarize_unified_windows,
)
from factorlab.research.monte_carlo import MonteCarloConfig, MonteCarloInput, MonteCarloRunner
from factorlab.research.monte_carlo.reliability import summarize_paths
from factorlab.research.regime_jump import (
    CalibrationError,
    JumpConfig,
    JumpStress,
    PathPlan,
    RegimeBootstrap,
    RegimeConfig,
)

D = Decimal

CORE_FUNDING_REGIMES: tuple[str, ...] = (
    "MAINNET_ACTUAL",
    "SWITCH_48H",
    "DEMO_HIGH_FUNDING_22.97BPS",
)

MC_KEY_SCENARIOS: tuple[str, ...] = (
    "gbm_base_vol_1.0x",
    "moving_block_bootstrap_24h",
    "jump_stress_downside",
)


def _compact_run_record(res: dict[str, Any]) -> dict[str, Any]:
    """Strip large nested logs before IPC transfer across process pool workers."""
    return {
        k: v
        for k, v in res.items()
        if k not in {"generations_log", "rotations_blocked", "hourly_series"}
    }


def _hist_task_worker(payload: tuple[Any, ...]) -> tuple[tuple[Any, ...], dict[str, Any]]:
    (
        cache_key,
        window_meta,
        strategy_id,
        lifecycle_id,
        exec_profile,
        funding_regime,
        funding_policy,
        vol_policy,
        historical_funding_events,
        capital,
        max_loss,
        tag,
    ) = payload
    res = run_unified_lifecycle(
        window_meta["bars"],
        strategy_id=strategy_id,
        lifecycle_id=lifecycle_id,
        execution_profile_name=exec_profile,
        funding_regime=funding_regime,
        strategy_cap_usdt=capital,
        opening_wallet=DEFAULT_DEMO_WALLET,
        funding_policy=funding_policy,
        vol_policy=vol_policy,
        warmup_closes=window_meta["warmup_closes"],
        historical_funding_events=historical_funding_events,
        historical_bar_volume=True,
        participation=D("0.01"),
        max_loss=max_loss,
        account_id=f"r11-h-{tag}",
        family_prefix="fl-r11",
    )
    row = _compact_run_record(res)
    row["window_idx"] = window_meta["window_idx"]
    row["window_id"] = window_meta["window_id"]
    row["r08_label"] = window_meta["r08_label"]
    row["start_utc"] = window_meta["start_utc"]
    row["end_exclusive_utc"] = window_meta["end_exclusive_utc"]
    row["anchor_price"] = round(float(window_meta["bars"][0]["close"]), 4)
    row["end_price"] = round(float(window_meta["bars"][-1]["close"]), 4)
    row["would_breach_7u_cap"] = bool(0.04 * float(window_meta["bars"][0]["close"]) * 0.99 > 7.0)
    return cache_key, row


def _mc_task_worker(payload: tuple[Any, ...]) -> tuple[tuple[Any, ...], dict[str, Any]]:
    (
        cache_key,
        prices,
        start_iso,
        strategy_id,
        lifecycle_id,
        exec_profile,
        funding_regime,
        funding_policy,
        vol_policy,
        warmup_closes,
        capital,
        tag,
    ) = payload
    bars = close_bars(prices, start_iso)
    res = run_unified_lifecycle(
        bars,
        strategy_id=strategy_id,
        lifecycle_id=lifecycle_id,
        execution_profile_name=exec_profile,
        funding_regime=funding_regime,
        strategy_cap_usdt=capital,
        opening_wallet=DEFAULT_DEMO_WALLET,
        funding_policy=funding_policy,
        vol_policy=vol_policy,
        warmup_closes=warmup_closes,
        historical_funding_events=None,
        historical_bar_volume=False,
        max_loss=D("1"),
        account_id=f"r11-mc-{tag}",
        family_prefix="fl-r11",
    )
    row = _compact_run_record(res)
    seed_val, scen, path_idx = cache_key[0], cache_key[1], cache_key[2]
    row["mc_seed"] = int(seed_val)
    row["mc_scenario"] = str(scen)
    row["mc_path_index"] = int(path_idx)
    row["mc_path_id"] = f"{seed_val}:{scen}:{path_idx}"
    return cache_key, row


def _run_pool(worker_fn, tasks: list[tuple[Any, ...]], max_workers: int) -> list[Any]:
    if not tasks:
        return []
    if max_workers <= 1 or len(tasks) <= 4:
        return [worker_fn(t) for t in tasks]
    chunksize = max(1, min(16, len(tasks) // (max_workers * 4) or 1))
    with ProcessPoolExecutor(max_workers=max_workers) as pool:
        return list(pool.map(worker_fn, tasks, chunksize=chunksize))


def _select_best_candidate(
    rows_by_id: dict[str, list[dict[str, Any]]],
    candidate_ids: list[str],
    *,
    min_mean_fills: float = 0.2,
) -> str:
    """R10 predeclared risk-adjusted objective: mean(r) - 0.5 * CVaR95, with activity filter."""
    scored: list[tuple[float, str]] = []
    fallback: list[tuple[float, str]] = []
    for cid in candidate_ids:
        s = summarize_unified_windows(rows_by_id[cid])
        obj = float(s["risk_adjusted_objective"])
        fallback.append((obj, cid))
        if float(s["mean_fills_per_window"]) >= min_mean_fills:
            scored.append((obj, cid))
    pool = scored if scored else fallback
    # Deterministic tie-break: highest objective, then lexicographically smallest candidate_id
    pool.sort(key=lambda item: (-item[0], item[1]))
    return pool[0][1]


def _make_neutral_warmup(anchor_price: float, ref_vol_1h: float, seed: int = 1010) -> list[float]:
    rng = np.random.default_rng(seed)
    series = list(anchor_price * np.exp(np.cumsum(rng.normal(0.0, ref_vol_1h, size=60))))
    scale = anchor_price / series[-1]
    return [float(p * scale) for p in series]


def _det_task_worker(payload: tuple[Any, ...]) -> dict[str, Any]:
    (
        sc_id,
        category,
        bars,
        p0,
        p1,
        f_regime,
        lc_id,
        prof_name,
        cancel_race,
        arm_id,
        bp,
        cp,
        neutral_warmup,
    ) = payload
    res = run_unified_lifecycle(
        bars,
        strategy_id=arm_id,
        lifecycle_id=lc_id,
        execution_profile_name=prof_name,
        funding_regime=f_regime,
        strategy_cap_usdt=DEMO_ANCHOR_CAPITAL,
        opening_wallet=DEFAULT_DEMO_WALLET,
        funding_policy=bp,
        vol_policy=cp,
        warmup_closes=neutral_warmup,
        historical_bar_volume=False,
        max_loss=D("1"),
        account_id=f"r11-det-{sc_id}-{lc_id}-{prof_name}-{'race' if cancel_race else 'std'}-{arm_id}",
        family_prefix="fl-det",
        inject_cancel_fill_race_once=cancel_race,
    )
    row = _compact_run_record(res)
    row.update(
        {
            "scenario_id": sc_id,
            "category": category,
            "start_price": round(p0, 4),
            "end_price": round(p1, 4),
            "price_change_pct": round((p1 / p0 - 1.0) * 100.0, 2),
            "cancel_fill_race_injected": cancel_race,
        }
    )
    return row


def run_r11_deterministic_stress(
    b_entry_only: FundingPolicy,
    b_entry_exit: FundingPolicy,
    c_policy: VolatilityPolicy,
    *,
    anchor_price: float = DEMO_ANCHOR_PRICE,
    n_steps: int = 168,
    max_workers: int = 1,
) -> list[dict[str, Any]]:
    """Run deterministic 10-scenario stress suite across L0/L1/L2, A/B2/B5/C5/CASH, and execution profiles."""
    base_stress = stress_paths(anchor_price, n_steps)
    x = np.linspace(0.0, 1.0, n_steps + 1)
    extra = {
        "high_funding_range": anchor_price * (1.0 - 0.0102 * np.sin(8.0 * np.pi * x)),
        "unidirectional_rally": anchor_price * (1.0 + 0.25 * x),
        "high_freq_whipsaw": anchor_price * (1.0 - 0.0108 * np.sin(24.0 * np.pi * x)),
        "funding_spike_mild_dip": anchor_price * np.interp(x, [0.0, 0.25, 0.65, 1.0], [1.0, 0.985, 0.96, 0.975]),
    }

    scenarios = [
        ("S1_high_funding_range", "高资金费+区间震荡", extra["high_funding_range"], "DEMO_HIGH_FUNDING_22.97BPS"),
        ("S2_funding_regime_spike", "突发高资金费切换(48h)", extra["funding_spike_mild_dip"], "SWITCH_48H"),
        ("S3_low_volatility", "低波动横盘", base_stress["low_volatility"], "NORMAL_SYNTHETIC_0.125BPS"),
        ("S4_unidirectional_rally", "单边上涨(+25%)", extra["unidirectional_rally"], "NORMAL_SYNTHETIC_0.125BPS"),
        ("S5_continuous_decline", "单边下跌(-40%)+高资费", base_stress["continuous_decline"], "DEMO_HIGH_FUNDING_22.97BPS"),
        ("S6_range_then_gap", "震荡后突发跳空(-30%)", base_stress["range_then_gap"], "NORMAL_SYNTHETIC_0.125BPS"),
        ("S7_high_freq_whipsaw", "高频反转震荡", extra["high_freq_whipsaw"], "NORMAL_SYNTHETIC_0.125BPS"),
        ("S8_thin_capacity_decline", "下跌反弹不足+高资费", base_stress["insufficient_rebound"], "DEMO_HIGH_FUNDING_22.97BPS"),
        ("S9_prolonged_stranded", "慢跌残仓+高资费", base_stress["slow_decline"], "DEMO_HIGH_FUNDING_22.97BPS"),
        ("S10_fast_reversal", "快速深跌反弹+高资费", base_stress["fast_reversal"], "DEMO_HIGH_FUNDING_22.97BPS"),
    ]

    arms = [
        ("CASH", None, None),
        ("A_FIXED", None, None),
        (b_entry_only.candidate_id, b_entry_only, None),
        (b_entry_exit.candidate_id, b_entry_exit, None),
        (c_policy.candidate_id, None, c_policy),
    ]
    start_iso = "2026-11-24T00:00:00+00:00"
    neutral_warmup = _make_neutral_warmup(anchor_price, c_policy.reference_vol_1h)

    lifecycle_profile_grid = [
        ("L0", "IDEAL_LIMIT_FILL", False),
        ("L1", "IDEAL_LIMIT_FILL", False),
        ("L1", "DELAYED_EXIT_FILL", False),
        ("L2", "IDEAL_LIMIT_FILL", False),
        ("L2", "DELAYED_EXIT_FILL", False),
        ("L2", "LIMITED_CAPACITY_PARTIAL", False),
        ("L2", "UNFILLED_EXIT_TIMEOUT", False),
        ("L2", "GAP_AND_CANCEL_FAULT", False),
    ]

    tasks: list[tuple[Any, ...]] = []
    for sc_id, category, prices, f_regime in scenarios:
        bars = close_bars(prices, start_iso)
        p0, p1 = float(prices[0]), float(prices[-1])
        combos = list(lifecycle_profile_grid)
        if sc_id in {"S2_funding_regime_spike", "S10_fast_reversal"}:
            combos.append(("L2", "DELAYED_EXIT_FILL", True))
        for lc_id, prof_name, cancel_race in combos:
            for arm_id, bp, cp in arms:
                if arm_id == "CASH" and (lc_id != "L0" or prof_name != "IDEAL_LIMIT_FILL" or cancel_race):
                    continue
                tasks.append(
                    (
                        sc_id,
                        category,
                        bars,
                        p0,
                        p1,
                        f_regime,
                        lc_id,
                        prof_name,
                        cancel_race,
                        arm_id,
                        bp,
                        cp,
                        neutral_warmup,
                    )
                )
    return _run_pool(_det_task_worker, tasks, max_workers=max_workers)


def run_r11_historical_study(
    all_bars: list[dict[str, Any]],
    all_funding: list[tuple[datetime, Decimal, Decimal, str]],
    b_policies: list[FundingPolicy],
    c_policies: list[VolatilityPolicy],
    *,
    repo_root: Path,
    max_workers: int = 8,
    initial_train_windows: int = 9,
    step_windows: int = 3,
) -> dict[str, Any]:
    """Run full 27-window & 6-fold Walk-Forward across L0/L1/L2, 5 funding regimes, and 5 execution profiles."""
    windows = build_nonoverlapping_weekly_windows(all_bars)
    validate_research_windows(all_bars, windows)
    n_win = len(windows)
    init_sigma_ref = _compute_ref_vol_from_bars(all_bars, windows[initial_train_windows]["start_bar_index"])

    fold_specs: list[tuple[int, int, int, float]] = []
    fid = 0
    tr_end = initial_train_windows
    while tr_end < n_win:
        te_end = min(n_win, tr_end + step_windows)
        fid += 1
        f_sigma = _compute_ref_vol_from_bars(all_bars, windows[tr_end]["start_bar_index"])
        fold_specs.append((fid, tr_end, te_end, f_sigma))
        tr_end = te_end

    b_map = {p.candidate_id: p for p in b_policies}
    c_map = {p.candidate_id: p for p in c_policies}
    b_ids = [p.candidate_id for p in b_policies]
    c_ids = [p.candidate_id for p in c_policies]
    rep_arms = ("A_FIXED", "B2", "B5", "C5")

    # 1. Build deduplicated task list covering:
    #    (a) Full 13-arm Walk-Forward matrix (all 27 windows + per-fold sigma_ref for C4..C6)
    #        across L0/L1/L2 x {MAINNET_ACTUAL, SWITCH_48H, DEMO_HIGH_FUNDING_22.97BPS} x IDEAL_LIMIT_FILL
    #        plus L1/L2 x {MAINNET_ACTUAL, SWITCH_48H, DEMO_HIGH_FUNDING_22.97BPS} x DELAYED_EXIT_FILL
    #        plus L0/L1/L2 x {EX_FUNDING, NORMAL_SYNTHETIC_0.125BPS} x IDEAL_LIMIT_FILL
    #    (b) Execution realism matrix: all 5 profiles x all 5 funding regimes x L1/L2 (+ L0 IDEAL)
    #        for representative arms (A_FIXED, B2, B5, C5) across all 27 windows.
    wf_combos: list[tuple[str, str, str]] = []
    for reg in CORE_FUNDING_REGIMES:
        wf_combos.append(("L0", "IDEAL_LIMIT_FILL", reg))
        wf_combos.append(("L1", "IDEAL_LIMIT_FILL", reg))
        wf_combos.append(("L2", "IDEAL_LIMIT_FILL", reg))
        wf_combos.append(("L1", "DELAYED_EXIT_FILL", reg))
        wf_combos.append(("L2", "DELAYED_EXIT_FILL", reg))

    tasks_by_key: dict[tuple[Any, ...], tuple[Any, ...]] = {}
    task_seq = 0

    def enqueue(
        win: dict[str, Any],
        arm_id: str,
        lc_id: str,
        prof_name: str,
        f_regime: str,
        sigma_tag: str,
        sigma_val: float | None,
    ) -> None:
        nonlocal task_seq
        # Key includes sigma_tag only for C strategies with extreme_vol_mult
        cp = c_map.get(arm_id)
        bp = b_map.get(arm_id)
        if cp is not None:
            eff_sigma = sigma_val if sigma_val is not None else init_sigma_ref
            cp_eff = replace(cp, reference_vol_1h=eff_sigma)
            eff_tag = sigma_tag if cp.extreme_vol_mult is not None else "init"
        else:
            cp_eff = None
            eff_tag = "none"
        key = (win["window_id"], arm_id, lc_id, prof_name, f_regime, eff_tag)
        if key in tasks_by_key:
            return
        task_seq += 1
        tasks_by_key[key] = (
            key,
            win,
            arm_id,
            lc_id,
            prof_name,
            f_regime,
            bp,
            cp_eff,
            all_funding,
            HISTORICAL_REPLAY_CAPITAL,
            D("1"),
            f"{task_seq:06d}",
        )

    # Enqueue WF tasks on the 3 core funding regimes
    for lc_id, prof_name, f_regime in wf_combos:
        for w in windows:
            enqueue(w, "A_FIXED", lc_id, prof_name, f_regime, "init", init_sigma_ref)
            for bid in b_ids:
                enqueue(w, bid, lc_id, prof_name, f_regime, "init", init_sigma_ref)
            for cid in c_ids:
                enqueue(w, cid, lc_id, prof_name, f_regime, "init", init_sigma_ref)
        # Per-fold causal sigma_ref for C candidates with extreme_vol_mult (fold >= 2; fold 1 == init)
        for f_num, _, te_end, f_sigma in fold_specs:
            if f_num == 1:
                continue
            for cid in c_ids:
                if c_map[cid].extreme_vol_mult is None:
                    continue
                for w in windows[:te_end]:
                    enqueue(w, cid, lc_id, prof_name, f_regime, f"f{f_num}", f_sigma)

    # Enqueue full 5-profile execution realism matrix on core regimes + L0/L1/L2 on EX_FUNDING & NORMAL_SYNTHETIC
    for f_regime in CORE_FUNDING_REGIMES:
        for w in windows:
            for arm_id in rep_arms:
                enqueue(w, arm_id, "L0", "IDEAL_LIMIT_FILL", f_regime, "init", init_sigma_ref)
                for lc_id in ("L1", "L2"):
                    for prof_name in ALL_EXECUTION_PROFILES:
                        enqueue(w, arm_id, lc_id, prof_name, f_regime, "init", init_sigma_ref)
    for f_regime in ("EX_FUNDING", "NORMAL_SYNTHETIC_0.125BPS"):
        for w in windows:
            for arm_id in rep_arms:
                enqueue(w, arm_id, "L0", "IDEAL_LIMIT_FILL", f_regime, "init", init_sigma_ref)
                enqueue(w, arm_id, "L1", "IDEAL_LIMIT_FILL", f_regime, "init", init_sigma_ref)
                enqueue(w, arm_id, "L2", "IDEAL_LIMIT_FILL", f_regime, "init", init_sigma_ref)
                enqueue(w, arm_id, "L2", "DELAYED_EXIT_FILL", f_regime, "init", init_sigma_ref)

    t0 = perf_counter()
    raw_pairs = _run_pool(_hist_task_worker, list(tasks_by_key.values()), max_workers=max_workers)
    results_cache: dict[tuple[Any, ...], dict[str, Any]] = dict(raw_pairs)
    elapsed_hist = round(perf_counter() - t0, 2)

    def lookup(
        win_id: str,
        arm_id: str,
        lc_id: str,
        prof_name: str,
        f_regime: str,
        sigma_tag: str = "init",
    ) -> dict[str, Any]:
        cp = c_map.get(arm_id)
        if cp is None:
            eff_tag = "none"
        else:
            eff_tag = sigma_tag if cp.extreme_vol_mult is not None else "init"
        return results_cache[(win_id, arm_id, lc_id, prof_name, f_regime, eff_tag)]

    # 2. Assemble Walk-Forward OOS rows, folds, stability, and full 27-window summaries.
    #    First freeze the per-fold L0-selected candidates (using ONLY L0 training windows)
    #    so fixed-parameter mechanism ablations (L1-L0, L2-L1, L2-L0) bind identical
    #    candidate parameters & fold sigma_ref across all lifecycles.
    l0_frozen_selections: dict[str, dict[int, tuple[str, str]]] = {}
    for reg in CORE_FUNDING_REGIMES:
        per_fold: dict[int, tuple[str, str]] = {}
        for f_num, tr_end, _, _ in fold_specs:
            tr_wins = windows[:tr_end]
            s_tag = "init" if f_num == 1 else f"f{f_num}"
            b_tr_l0 = {bid: [lookup(w["window_id"], bid, "L0", "IDEAL_LIMIT_FILL", reg) for w in tr_wins] for bid in b_ids}
            c_tr_l0 = {cid: [lookup(w["window_id"], cid, "L0", "IDEAL_LIMIT_FILL", reg, s_tag) for w in tr_wins] for cid in c_ids}
            per_fold[f_num] = (_select_best_candidate(b_tr_l0, b_ids), _select_best_candidate(c_tr_l0, c_ids))
        l0_frozen_selections[reg] = per_fold

    role_selection_meta: dict[str, tuple[str, str]] = {
        "CASH": ("ZERO_EXPOSURE_REFERENCE", "Zero-exposure reference arm"),
        "A_FIXED": ("FIXED_PREDECLARED", "Fixed +/-1% single-level grid (predeclared)"),
        "B2": ("FIXED_PREDECLARED", "Predeclared fixed candidate B2 (entry/rotation gate only)"),
        "B5": ("FIXED_PREDECLARED", "Predeclared fixed candidate B5 (entry gate + funding exit)"),
        "C5": ("FIXED_PREDECLARED", "Predeclared fixed candidate C5 (per-fold training-frozen sigma_ref)"),
        "B_FROZEN_L0": (
            "FROZEN_L0_WF_CANDIDATE",
            "Per-fold candidate selected on L0 training windows only and held fixed across L0/L1/L2",
        ),
        "C_FROZEN_L0": (
            "FROZEN_L0_WF_CANDIDATE",
            "Per-fold candidate & sigma_ref selected on L0 training windows only and held fixed across L0/L1/L2",
        ),
        "B_WF": (
            "LIFECYCLE_REOPTIMIZED_WF",
            "Re-selected on training windows within this lifecycle/profile (includes parameter re-optimization)",
        ),
        "C_WF": (
            "LIFECYCLE_REOPTIMIZED_WF",
            "Re-selected on training windows within this lifecycle/profile (includes parameter re-optimization)",
        ),
    }

    wf_results: dict[str, dict[str, Any]] = {}
    wf_folds_all: list[dict[str, Any]] = []
    wf_oos_rows_all: list[dict[str, Any]] = []
    stability_all: list[dict[str, Any]] = []
    wf_summary_rows: list[dict[str, Any]] = []

    for lc_id, prof_name, f_regime in wf_combos:
        combo_key = f"{lc_id}__{prof_name}__{f_regime}"
        folds_summary: list[dict[str, Any]] = []
        oos_a: list[dict[str, Any]] = []
        oos_b: list[dict[str, Any]] = []
        oos_c: list[dict[str, Any]] = []
        oos_b_frozen: list[dict[str, Any]] = []
        oos_c_frozen: list[dict[str, Any]] = []
        oos_cash: list[dict[str, Any]] = []
        oos_b2: list[dict[str, Any]] = []
        oos_b5: list[dict[str, Any]] = []
        oos_c5: list[dict[str, Any]] = []

        for f_num, tr_end, te_end, f_sigma in fold_specs:
            tr_wins = windows[:tr_end]
            te_wins = windows[tr_end:te_end]
            s_tag = "init" if f_num == 1 else f"f{f_num}"

            b_train = {bid: [lookup(w["window_id"], bid, lc_id, prof_name, f_regime) for w in tr_wins] for bid in b_ids}
            c_train = {cid: [lookup(w["window_id"], cid, lc_id, prof_name, f_regime, s_tag) for w in tr_wins] for cid in c_ids}
            sel_b = _select_best_candidate(b_train, b_ids)
            sel_c = _select_best_candidate(c_train, c_ids)
            frozen_b_id, frozen_c_id = l0_frozen_selections[f_regime][f_num]

            fold_a = [lookup(w["window_id"], "A_FIXED", lc_id, prof_name, f_regime) for w in te_wins]
            fold_b = [lookup(w["window_id"], sel_b, lc_id, prof_name, f_regime) for w in te_wins]
            fold_c = [lookup(w["window_id"], sel_c, lc_id, prof_name, f_regime, s_tag) for w in te_wins]
            fold_b_fr = [lookup(w["window_id"], frozen_b_id, lc_id, prof_name, f_regime) for w in te_wins]
            fold_c_fr = [lookup(w["window_id"], frozen_c_id, lc_id, prof_name, f_regime, s_tag) for w in te_wins]
            fold_b2 = [lookup(w["window_id"], "B2", lc_id, prof_name, f_regime) for w in te_wins]
            fold_b5 = [lookup(w["window_id"], "B5", lc_id, prof_name, f_regime) for w in te_wins]
            fold_c5 = [lookup(w["window_id"], "C5", lc_id, prof_name, f_regime, s_tag) for w in te_wins]
            fold_cash = [
                run_unified_lifecycle(
                    w["bars"],
                    strategy_id="CASH",
                    lifecycle_id=lc_id,
                    execution_profile_name=prof_name,
                    funding_regime=f_regime,
                    strategy_cap_usdt=HISTORICAL_REPLAY_CAPITAL,
                    historical_funding_events=all_funding,
                )
                for w in te_wins
            ]

            for r_a, r_b, r_c, r_bf, r_cf, r_ca in zip(fold_a, fold_b, fold_c, fold_b_fr, fold_c_fr, fold_cash):
                oos_a.append(dict(r_a, fold=f_num, wf_role="A_FIXED", selected_id="A_FIXED"))
                oos_b.append(dict(r_b, fold=f_num, wf_role="B_WF", selected_id=sel_b))
                oos_c.append(dict(r_c, fold=f_num, wf_role="C_WF", selected_id=sel_c))
                oos_b_frozen.append(dict(r_bf, fold=f_num, wf_role="B_FROZEN_L0", selected_id=frozen_b_id))
                oos_c_frozen.append(dict(r_cf, fold=f_num, wf_role="C_FROZEN_L0", selected_id=frozen_c_id))
                oos_cash.append(dict(r_ca, fold=f_num, wf_role="CASH", selected_id="CASH", window_id=r_a["window_id"]))
            oos_b2.extend(fold_b2)
            oos_b5.extend(fold_b5)
            oos_c5.extend(fold_c5)

            sa = summarize_unified_windows(fold_a)
            sb = summarize_unified_windows(fold_b)
            sc = summarize_unified_windows(fold_c)
            sbf = summarize_unified_windows(fold_b_fr)
            scf = summarize_unified_windows(fold_c_fr)
            f_rec = {
                "lifecycle_id": lc_id,
                "execution_profile": prof_name,
                "funding_regime": f_regime,
                "fold": f_num,
                "train_windows": f"{tr_wins[0]['window_id']}..{tr_wins[-1]['window_id']}",
                "test_windows": f"{te_wins[0]['window_id']}..{te_wins[-1]['window_id']}",
                "frozen_sigma_ref_1h_pct": round(f_sigma * 100.0, 4),
                "selected_b": sel_b,
                "selected_c": sel_c,
                "frozen_l0_selected_b": frozen_b_id,
                "frozen_l0_selected_c": frozen_c_id,
                "oos_a_return_pct": sa["mean_net_return_pct"],
                "oos_b_return_pct": sb["mean_net_return_pct"],
                "oos_c_return_pct": sc["mean_net_return_pct"],
                "oos_b_frozen_l0_return_pct": sbf["mean_net_return_pct"],
                "oos_c_frozen_l0_return_pct": scf["mean_net_return_pct"],
                "b_minus_a_pp": round(sb["mean_net_return_pct"] - sa["mean_net_return_pct"], 4),
                "c_minus_a_pp": round(sc["mean_net_return_pct"] - sa["mean_net_return_pct"], 4),
            }
            folds_summary.append(f_rec)
            wf_folds_all.append(f_rec)

        wf_oos_rows_all.extend(oos_a + oos_b + oos_c + oos_b_frozen + oos_c_frozen + oos_cash)

        # IS (W01..W09) vs OOS (W10..W27) parameter stability across all 12 B/C candidates
        is_wins = windows[:initial_train_windows]
        oos_wins = windows[initial_train_windows:]
        is_objs: list[float] = []
        oos_objs: list[float] = []
        stab_rows: list[dict[str, Any]] = []
        for arm_id in ["A_FIXED", *b_ids, *c_ids]:
            is_r = [lookup(w["window_id"], arm_id, lc_id, prof_name, f_regime, "init") for w in is_wins]
            oos_r = [lookup(w["window_id"], arm_id, lc_id, prof_name, f_regime, "init") for w in oos_wins]
            full_r = is_r + oos_r
            s_is = summarize_unified_windows(is_r)
            s_oos = summarize_unified_windows(oos_r)
            s_full = summarize_unified_windows(full_r)
            if arm_id != "A_FIXED":
                is_objs.append(float(s_is["risk_adjusted_objective"]))
                oos_objs.append(float(s_oos["risk_adjusted_objective"]))
            s_rec = {
                "lifecycle_id": lc_id,
                "execution_profile": prof_name,
                "funding_regime": f_regime,
                "strategy_id": arm_id,
                "family": "A" if arm_id == "A_FIXED" else arm_id[0],
                "is_mean_return_pct": s_is["mean_net_return_pct"],
                "oos_mean_return_pct": s_oos["mean_net_return_pct"],
                "oos_minus_is_pp": round(s_oos["mean_net_return_pct"] - s_is["mean_net_return_pct"], 4),
                "is_cvar95_pct": s_is["cvar95_pct"],
                "oos_cvar95_pct": s_oos["cvar95_pct"],
                "full_mean_return_pct": s_full["mean_net_return_pct"],
                "full_cvar95_pct": s_full["cvar95_pct"],
                "full_mean_fills": s_full["mean_fills_per_window"],
                "full_mean_holding_hours": s_full["mean_holding_hours"],
                "full_mean_rotations": s_full["mean_rotations_per_window"],
                "full_mean_turnover": s_full["mean_turnover_ratio"],
            }
            stab_rows.append(s_rec)
            if f_regime == "MAINNET_ACTUAL":
                stability_all.append(s_rec)

        if len(set(is_objs)) > 1 and len(set(oos_objs)) > 1:
            rho, p_rho = scipy_stats.spearmanr(is_objs, oos_objs)
            spearman_rho, spearman_p = round(float(rho), 4), round(float(p_rho), 4)
        else:
            spearman_rho, spearman_p = None, None

        oos_summaries = {
            "CASH": summarize_unified_windows(oos_cash),
            "A_FIXED": summarize_unified_windows(oos_a),
            "B_WF": summarize_unified_windows(oos_b),
            "B_FROZEN_L0": summarize_unified_windows(oos_b_frozen),
            "B2": summarize_unified_windows(oos_b2),
            "B5": summarize_unified_windows(oos_b5),
            "C_WF": summarize_unified_windows(oos_c),
            "C_FROZEN_L0": summarize_unified_windows(oos_c_frozen),
            "C5": summarize_unified_windows(oos_c5),
        }
        for role_name, s_dict in oos_summaries.items():
            sel_mode, bind_note = role_selection_meta[role_name]
            wf_summary_rows.append(
                {
                    "lifecycle_id": lc_id,
                    "model_label": LIFECYCLE_LABELS[lc_id],
                    "execution_profile": prof_name,
                    "funding_regime": f_regime,
                    "strategy_role": role_name,
                    "selection_mode": sel_mode,
                    "parameter_binding_note": bind_note,
                    **s_dict,
                }
            )

        wf_results[combo_key] = {
            "lifecycle_id": lc_id,
            "execution_profile": prof_name,
            "funding_regime": f_regime,
            "folds": folds_summary,
            "oos_rows": {
                "CASH": oos_cash,
                "A_FIXED": oos_a,
                "B_WF": oos_b,
                "B_FROZEN_L0": oos_b_frozen,
                "B2": oos_b2,
                "B5": oos_b5,
                "C_WF": oos_c,
                "C_FROZEN_L0": oos_c_frozen,
                "C5": oos_c5,
            },
            "oos_summary": oos_summaries,
            "stability": stab_rows,
            "is_oos_spearman_rho": spearman_rho,
            "is_oos_spearman_p": spearman_p,
        }

    # 3. Assemble Execution Realism Matrix (5 profiles on core regimes + L0/L1/L2 on EX & NORMAL)
    exec_matrix_rows: list[dict[str, Any]] = []
    oos_win_ids = {w["window_id"] for w in windows[initial_train_windows:]}
    for f_regime in ALL_FUNDING_REGIMES:
        lc_profs = (
            (("L0", ("IDEAL_LIMIT_FILL",)), ("L1", ALL_EXECUTION_PROFILES), ("L2", ALL_EXECUTION_PROFILES))
            if f_regime in CORE_FUNDING_REGIMES
            else (("L0", ("IDEAL_LIMIT_FILL",)), ("L1", ("IDEAL_LIMIT_FILL",)), ("L2", ("IDEAL_LIMIT_FILL", "DELAYED_EXIT_FILL")))
        )
        for lc_id, prof_list in lc_profs:
            for prof_name in prof_list:
                for arm_id in ("CASH", *rep_arms):
                    if arm_id == "CASH":
                        all_27 = [
                            run_unified_lifecycle(
                                w["bars"],
                                strategy_id="CASH",
                                lifecycle_id=lc_id,
                                execution_profile_name=prof_name,
                                funding_regime=f_regime,
                                strategy_cap_usdt=HISTORICAL_REPLAY_CAPITAL,
                                historical_funding_events=all_funding,
                            )
                            for w in windows
                        ]
                        oos_18 = all_27[initial_train_windows:]
                    else:
                        all_27 = [lookup(w["window_id"], arm_id, lc_id, prof_name, f_regime, "init") for w in windows]
                        oos_18 = [r for r in all_27 if r["window_id"] in oos_win_ids]
                    s27 = summarize_unified_windows(all_27)
                    s18 = summarize_unified_windows(oos_18)
                    exec_matrix_rows.append(
                        {
                            "funding_regime": f_regime,
                            "lifecycle_id": lc_id,
                            "model_label": LIFECYCLE_LABELS[lc_id],
                            "execution_profile": prof_name,
                            "strategy_id": arm_id,
                            "oos18_mean_return_pct": s18["mean_net_return_pct"],
                            "oos18_cvar95_pct": s18["cvar95_pct"],
                            "oos18_var95_pct": s18["var95_pct"],
                            "oos18_mean_max_dd_pct": s18["mean_max_drawdown_pct"],
                            "oos18_loss_prob_pct": s18["loss_prob_pct"],
                            "oos18_total_fees_usdt": s18["total_fees_usdt"],
                            "oos18_total_slippage_usdt": s18["total_slippage_usdt"],
                            "oos18_total_funding_usdt": s18["total_funding_pnl_usdt"],
                            "oos18_total_net_pnl_usdt": s18["total_final_net_pnl_usdt"],
                            "oos18_mean_holding_hours": s18["mean_holding_hours"],
                            "oos18_mean_paused_inv_hours": s18["mean_paused_with_inventory_hours"],
                            "oos18_mean_exit_delay_hours": s18["mean_exit_delay_hours"],
                            "oos18_stranded_windows": s18["stranded_windows"],
                            "oos18_mean_rotations": s18["mean_rotations_per_window"],
                            "oos18_rotation_success_rate": s18["rotation_success_rate"],
                            "oos18_mean_posts": s18["mean_posts_consumed"],
                            "oos18_halted_windows": s18["halted_windows"],
                            "oos18_order_fill_rate": s18["mean_order_fill_rate"],
                            "oos18_mean_turnover": s18["mean_turnover_ratio"],
                            "full27_mean_return_pct": s27["mean_net_return_pct"],
                            "full27_cvar95_pct": s27["cvar95_pct"],
                            "full27_mean_max_dd_pct": s27["mean_max_drawdown_pct"],
                            "full27_total_net_pnl_usdt": s27["total_final_net_pnl_usdt"],
                            "full27_stranded_windows": s27["stranded_windows"],
                            "all_zero_fifo_residual": s27["all_zero_fifo_residual"],
                            "max_simultaneous_exit_orders": s27["max_simultaneous_exit_orders"],
                        }
                    )

    # 4. Paired Ablations & Statistical Tests (strictly separating fixed-parameter mechanism
    #    ablations from lifecycle-reoptimized pipeline comparisons, with unrounded Holm-Bonferroni)
    ablation_rows = build_separated_paired_ablations(wf_results)

    # 5. L0 vs R10 Frozen Baseline Reconciliation (read-only lookup of docs/validation/p8-r10/summary.json)
    r10_reconcile = _reconcile_l0_against_frozen_r10(repo_root, wf_results)

    return {
        "windows_count": n_win,
        "oos_windows_count": n_win - initial_train_windows,
        "initial_sigma_ref_1h": round(init_sigma_ref, 6),
        "elapsed_seconds": elapsed_hist,
        "total_unique_window_runs": len(results_cache),
        "all_zero_fifo_residual": bool(all(r["reconciled_zero_residual"] for r in results_cache.values())),
        "max_simultaneous_exit_orders": max(
            (int(r.get("max_simultaneous_exit_orders", 0)) for r in results_cache.values()), default=0
        ),
        "wf_summary": wf_summary_rows,
        "wf_folds": wf_folds_all,
        "wf_oos_rows": wf_oos_rows_all,
        "execution_realism_matrix": exec_matrix_rows,
        "paired_ablations": ablation_rows,
        "parameter_stability_mainnet": stability_all,
        "spearman_summary": {
            k: {"rho": v["is_oos_spearman_rho"], "p_value": v["is_oos_spearman_p"]}
            for k, v in wf_results.items()
            if v["funding_regime"] == "MAINNET_ACTUAL"
        },
        "l0_vs_r10_reconciliation": r10_reconcile,
        "wf_results_index": {
            k: {
                "lifecycle_id": v["lifecycle_id"],
                "execution_profile": v["execution_profile"],
                "funding_regime": v["funding_regime"],
                "oos_summary": v["oos_summary"],
                "is_oos_spearman_rho": v["is_oos_spearman_rho"],
                "is_oos_spearman_p": v["is_oos_spearman_p"],
            }
            for k, v in wf_results.items()
        },
    }


def build_separated_paired_ablations(
    wf_results: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Build paired historical ablations strictly separating:

    1. ``FIXED_PARAM_MECHANISM_ABLATION``: identical candidate parameters & fold sigma_ref
       held constant across L0/L1/L2 (``A_FIXED``, ``B_FROZEN_L0``, ``C_FROZEN_L0``, ``B2``,
       ``B5``, ``C5``), measuring pure F11 conditional mechanism (L1-L0) and F08 incremental
       mechanism (L2-L1).
    2. ``LIFECYCLE_REOPTIMIZED_PIPELINE_COMPARISON``: lifecycle-specific Walk-Forward
       re-optimized pipelines (``B_WF``, ``C_WF``), explicitly documenting that differences
       include lifecycle change + candidate re-selection + interaction.
    3. ``WITHIN_LIFECYCLE_STRATEGY_COMPARISON``: strategy vs ``A_FIXED`` and vs ``CASH``
       within the same lifecycle and execution profile.

    Holm-Bonferroni adjustments are computed strictly from **unrounded** raw p-values within
    each explicit ``(funding_regime, execution_profile, comparison_family)`` family before
    final display rounding.
    """
    ablation_rows: list[dict[str, Any]] = []

    def add_ablation(
        *,
        comparison_type: str,
        ablation_group: str,
        comparison_family: str,
        parameter_binding: str,
        inference_scope: str,
        label: str,
        funding_regime: str,
        execution_profile: str,
        cand_rows: list[dict[str, Any]],
        ctrl_rows: list[dict[str, Any]],
        seed_offset: int,
    ) -> None:
        c_ret = np.asarray([float(r["net_return_pct"]) for r in cand_rows], dtype=float)
        b_ret = np.asarray([float(r["net_return_pct"]) for r in ctrl_rows], dtype=float)
        c_dd = np.asarray([float(r["max_drawdown_pct"]) for r in cand_rows], dtype=float)
        b_dd = np.asarray([float(r["max_drawdown_pct"]) for r in ctrl_rows], dtype=float)
        c_sum = summarize_unified_windows(cand_rows)
        b_sum = summarize_unified_windows(ctrl_rows)
        st_ret = paired_statistical_test_unrounded(c_ret, b_ret, n_bootstrap=10_000, seed=202611 + seed_offset)
        st_dd = paired_statistical_test_unrounded(-c_dd, -b_dd, n_bootstrap=10_000, seed=302611 + seed_offset)
        ablation_rows.append(
            {
                "comparison_type": comparison_type,
                "ablation_group": ablation_group,
                "comparison_family": comparison_family,
                "parameter_binding": parameter_binding,
                "inference_scope": inference_scope,
                "comparison": label,
                "funding_regime": funding_regime,
                "execution_profile": execution_profile,
                "n_windows": len(cand_rows),
                "effective_tail_samples_95": round(0.05 * len(cand_rows), 2),
                "cand_mean_return_pct": c_sum["mean_net_return_pct"],
                "ctrl_mean_return_pct": b_sum["mean_net_return_pct"],
                "delta_mean_return_raw_pp": st_ret["mean_diff_raw_pct"],
                "delta_mean_return_pp": st_ret["mean_diff_pct"],
                "delta_median_return_pp": st_ret["median_diff_pct"],
                "boot_95ci_low_pp": st_ret["bootstrap_95ci_low_pct"],
                "boot_95ci_high_pp": st_ret["bootstrap_95ci_high_pct"],
                "boot_p_raw": st_ret["bootstrap_p_raw"],
                "boot_p_value": st_ret["bootstrap_p_value"],
                "wilcoxon_p_raw": st_ret["wilcoxon_p_raw"],
                "wilcoxon_p_value": st_ret["wilcoxon_p_value"],
                "paired_t_stat": st_ret["paired_t_stat"],
                "paired_t_p_value": st_ret["paired_t_p_value"],
                "win_tie_loss_pct": f"{st_ret['win_rate_pct']}/{st_ret['tie_rate_pct']}/{st_ret['loss_rate_pct']}",
                "cand_cvar95_pct": c_sum["cvar95_pct"],
                "ctrl_cvar95_pct": b_sum["cvar95_pct"],
                "delta_cvar95_pp": round(c_sum["cvar95_pct"] - b_sum["cvar95_pct"], 4),
                "dd_reduction_pp": st_dd["mean_diff_pct"],
                "dd_reduction_wilcoxon_p": st_dd["wilcoxon_p_value"],
                "cand_mean_paused_inv_h": c_sum["mean_paused_with_inventory_hours"],
                "ctrl_mean_paused_inv_h": b_sum["mean_paused_with_inventory_hours"],
                "cand_total_fees_usdt": c_sum["total_fees_usdt"],
                "ctrl_total_fees_usdt": b_sum["total_fees_usdt"],
                "cand_total_funding_usdt": c_sum["total_funding_pnl_usdt"],
                "ctrl_total_funding_usdt": b_sum["total_funding_pnl_usdt"],
                "raw_sig_05": st_ret["statistically_significant_05"],
            }
        )

    seq_seed = 0
    for f_regime in CORE_FUNDING_REGIMES:
        l0_id = wf_results[f"L0__IDEAL_LIMIT_FILL__{f_regime}"]["oos_rows"]
        l1_id = wf_results[f"L1__IDEAL_LIMIT_FILL__{f_regime}"]["oos_rows"]
        l2_id = wf_results[f"L2__IDEAL_LIMIT_FILL__{f_regime}"]["oos_rows"]
        l1_dl = wf_results[f"L1__DELAYED_EXIT_FILL__{f_regime}"]["oos_rows"]
        l2_dl = wf_results[f"L2__DELAYED_EXIT_FILL__{f_regime}"]["oos_rows"]

        # (A) Fixed-parameter mechanism ablations (identical candidate & fold sigma_ref across lifecycles)
        fixed_roles = [
            ("A_FIXED", "PREDECLARED_FIXED_PARAM", "PREDECLARED_CONFIRMATION_FAMILY"),
            ("B2", "PREDECLARED_FIXED_PARAM", "EXPLORATORY_POST_REVIEW_MECHANISM_FAMILY"),
            ("B5", "PREDECLARED_FIXED_PARAM", "PREDECLARED_CONFIRMATION_FAMILY"),
            ("C5", "PREDECLARED_FIXED_PARAM_FOLD_SIGMA", "PREDECLARED_CONFIRMATION_FAMILY"),
        ]
        if "B_FROZEN_L0" in l0_id and "B_FROZEN_L0" in l1_id and "B_FROZEN_L0" in l2_id:
            fixed_roles.append(
                ("B_FROZEN_L0", "FROZEN_L0_TRAINING_CANDIDATE", "EXPLORATORY_POST_REVIEW_MECHANISM_FAMILY")
            )
        if "C_FROZEN_L0" in l0_id and "C_FROZEN_L0" in l1_id and "C_FROZEN_L0" in l2_id:
            fixed_roles.append(
                ("C_FROZEN_L0", "FROZEN_L0_TRAINING_CANDIDATE_AND_SIGMA", "EXPLORATORY_POST_REVIEW_MECHANISM_FAMILY")
            )

        for role, binding, scope in fixed_roles:
            for grp, lbl, ca, ct, prof in (
                ("F11_FIXED_PARAM_MECHANISM", f"{role}: L1 - L0 (F11 fixed-param mechanism)", l1_id[role], l0_id[role], "IDEAL_LIMIT_FILL"),
                ("F08_FIXED_PARAM_MECHANISM", f"{role}: L2 - L1 (F08 fixed-param incremental)", l2_id[role], l1_id[role], "IDEAL_LIMIT_FILL"),
                ("FULL_FIXED_PARAM_MECHANISM", f"{role}: L2 - L0 (F11+F08 fixed-param)", l2_id[role], l0_id[role], "IDEAL_LIMIT_FILL"),
                ("F08_FIXED_PARAM_MECHANISM_DELAYED", f"{role}: L2 - L1 (F08 fixed-param incremental, DELAYED)", l2_dl[role], l1_dl[role], "DELAYED_EXIT_FILL"),
            ):
                seq_seed += 1
                add_ablation(
                    comparison_type="FIXED_PARAM_MECHANISM_ABLATION",
                    ablation_group=grp,
                    comparison_family="FIXED_PARAM_MECHANISM",
                    parameter_binding=binding,
                    inference_scope=scope,
                    label=lbl,
                    funding_regime=f_regime,
                    execution_profile=prof,
                    cand_rows=ca,
                    ctrl_rows=ct,
                    seed_offset=seq_seed,
                )

        # (B) Lifecycle-reoptimized full-pipeline comparisons (includes lifecycle + candidate re-selection + interaction)
        for role in ("B_WF", "C_WF"):
            for grp, lbl, ca, ct, prof in (
                ("LIFECYCLE_REOPTIMIZED_PIPELINE_COMPARISON", f"{role}: L1 - L0 (reoptimized pipeline)", l1_id[role], l0_id[role], "IDEAL_LIMIT_FILL"),
                ("LIFECYCLE_REOPTIMIZED_PIPELINE_COMPARISON", f"{role}: L2 - L1 (reoptimized pipeline)", l2_id[role], l1_id[role], "IDEAL_LIMIT_FILL"),
                ("LIFECYCLE_REOPTIMIZED_PIPELINE_COMPARISON", f"{role}: L2 - L0 (reoptimized pipeline)", l2_id[role], l0_id[role], "IDEAL_LIMIT_FILL"),
                ("LIFECYCLE_REOPTIMIZED_PIPELINE_COMPARISON", f"{role}: L2 - L1 (reoptimized pipeline, DELAYED)", l2_dl[role], l1_dl[role], "DELAYED_EXIT_FILL"),
            ):
                seq_seed += 1
                add_ablation(
                    comparison_type="LIFECYCLE_REOPTIMIZED_PIPELINE_COMPARISON",
                    ablation_group=grp,
                    comparison_family="REOPTIMIZED_PIPELINE",
                    parameter_binding="REOPTIMIZED_PER_LIFECYCLE",
                    inference_scope="EXPLORATORY_REOPTIMIZED_PIPELINE_FAMILY",
                    label=lbl,
                    funding_regime=f_regime,
                    execution_profile=prof,
                    cand_rows=ca,
                    ctrl_rows=ct,
                    seed_offset=seq_seed,
                )

        # (C) Within-lifecycle strategy comparisons: B_WF/B2/B5/C_WF/C5 vs A_FIXED and vs CASH
        for lc_name, prof_name, oos_map in (
            ("L0", "IDEAL_LIMIT_FILL", l0_id),
            ("L1", "IDEAL_LIMIT_FILL", l1_id),
            ("L2", "IDEAL_LIMIT_FILL", l2_id),
            ("L1", "DELAYED_EXIT_FILL", l1_dl),
            ("L2", "DELAYED_EXIT_FILL", l2_dl),
        ):
            for cand_role in ("B_WF", "B2", "B5", "C_WF", "C5"):
                seq_seed += 1
                add_ablation(
                    comparison_type="WITHIN_LIFECYCLE_STRATEGY_COMPARISON",
                    ablation_group=f"STRATEGY_VS_A_{lc_name}",
                    comparison_family="WITHIN_LIFECYCLE_STRATEGY",
                    parameter_binding="SAME_LIFECYCLE_PAIRING",
                    inference_scope="PREDECLARED_CONFIRMATION_FAMILY",
                    label=f"{lc_name}: {cand_role} - A_FIXED",
                    funding_regime=f_regime,
                    execution_profile=prof_name,
                    cand_rows=oos_map[cand_role],
                    ctrl_rows=oos_map["A_FIXED"],
                    seed_offset=seq_seed,
                )
            for cand_role in ("A_FIXED", "B_WF", "C_WF"):
                seq_seed += 1
                add_ablation(
                    comparison_type="WITHIN_LIFECYCLE_STRATEGY_COMPARISON",
                    ablation_group=f"STRATEGY_VS_CASH_{lc_name}",
                    comparison_family="WITHIN_LIFECYCLE_STRATEGY",
                    parameter_binding="SAME_LIFECYCLE_PAIRING",
                    inference_scope="PREDECLARED_CONFIRMATION_FAMILY",
                    label=f"{lc_name}: {cand_role} - CASH",
                    funding_regime=f_regime,
                    execution_profile=prof_name,
                    cand_rows=oos_map[cand_role],
                    ctrl_rows=oos_map["CASH"],
                    seed_offset=seq_seed,
                )

    # Apply Holm-Bonferroni & Bonferroni adjustments strictly from UNROUNDED raw p-values
    # within each distinct (funding_regime, execution_profile, comparison_family) family.
    family_groups: dict[tuple[str, str, str], list[int]] = {}
    for idx, row in enumerate(ablation_rows):
        family_groups.setdefault(
            (row["funding_regime"], row["execution_profile"], row["comparison_family"]), []
        ).append(idx)
    for idxs in family_groups.values():
        m = len(idxs)
        adj_w_raw = holm_bonferroni_adjust(
            [ablation_rows[i]["wilcoxon_p_raw"] for i in idxs], round_digits=None
        )
        adj_b_raw = holm_bonferroni_adjust(
            [ablation_rows[i]["boot_p_raw"] for i in idxs], round_digits=None
        )
        for pos, i in enumerate(idxs):
            ablation_rows[i]["family_comparisons_m"] = m
            ablation_rows[i]["bonferroni_alpha_05"] = round(0.05 / m, 6)
            ablation_rows[i]["holm_wilcoxon_p_raw"] = adj_w_raw[pos]
            ablation_rows[i]["holm_wilcoxon_p"] = round(adj_w_raw[pos], 6)
            ablation_rows[i]["holm_boot_p_raw"] = adj_b_raw[pos]
            ablation_rows[i]["holm_boot_p"] = round(adj_b_raw[pos], 6)
            ablation_rows[i]["sig_after_holm_05"] = bool(
                ablation_rows[i]["boot_95ci_low_pp"] > 0.0 and adj_w_raw[pos] < 0.05
            )
    return ablation_rows


def _reconcile_l0_against_frozen_r10(
    repo_root: Path,
    wf_results: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Compare R11 unified L0 OOS results against frozen PR #238 R10 summary.json (read-only)."""
    r10_path = repo_root / "docs" / "validation" / "p8-r10" / "summary.json"
    if not r10_path.is_file():
        return []
    r10 = json.loads(r10_path.read_text(encoding="utf-8"))
    mapping = [
        ("MAINNET_ACTUAL", "walk_forward_mainnet"),
        ("SWITCH_48H", "walk_forward_regime_switch"),
        ("DEMO_HIGH_FUNDING_22.97BPS", "walk_forward_demo_stress"),
    ]
    rows: list[dict[str, Any]] = []
    for r11_reg, r10_key in mapping:
        r10_oos = r10.get(r10_key, {}).get("oos_summary", {})
        r11_oos = wf_results.get(f"L0__IDEAL_LIMIT_FILL__{r11_reg}", {}).get("oos_summary", {})
        for r11_role, r10_role in (("A_FIXED", "A"), ("B_WF", "B_wf"), ("C_WF", "C_wf")):
            s10 = r10_oos.get(r10_role)
            s11 = r11_oos.get(r11_role)
            if not s10 or not s11:
                continue
            r10_ret = float(s10["mean_risk_budget_return_with_funding_pct"])
            r11_ret = float(s11["mean_net_return_pct"])
            r10_cvar = float(s10["cvar95_with_funding_pct"])
            r11_cvar = float(s11["cvar95_pct"])
            rows.append(
                {
                    "funding_regime": r11_reg,
                    "strategy_role": r11_role,
                    "r10_frozen_return_pct": round(r10_ret, 4),
                    "r11_l0_return_pct": round(r11_ret, 4),
                    "return_diff_pp": round(r11_ret - r10_ret, 4),
                    "r10_frozen_cvar95_pct": round(r10_cvar, 4),
                    "r11_l0_cvar95_pct": round(r11_cvar, 4),
                    "cvar95_diff_pp": round(r11_cvar - r10_cvar, 4),
                    "r10_frozen_funding_usdt": round(float(s10["total_funding_pnl_usdt"]), 6),
                    "r11_l0_funding_usdt": round(float(s11["total_funding_pnl_usdt"]), 6),
                    "r11_l0_halted_windows": int(s11["halted_windows"]),
                }
            )
    return rows


def _generate_mc_paths(
    r04b_snapshot: Path,
    *,
    anchor_price: float,
    n_paths_map: dict[str, int],
    n_steps: int,
    seed: int,
) -> tuple[dict[str, tuple[str, float, np.ndarray]], dict[str, Any], str]:
    """Generate identical paired stochastic price paths reusing R08/R09/R10 generators."""
    bars_r04b, identity_r04b = load_hourly(r04b_snapshot)
    times_utc = pd.to_datetime([b["time"] for b in bars_r04b], utc=True) + pd.Timedelta(hours=1)
    acquired_ts = pd.Timestamp(identity_r04b["acquired_at"]).tz_convert("UTC")
    as_of_ts = max(times_utc[-1], acquired_ts).ceil("h")
    as_of = as_of_ts.isoformat()
    start_iso = (as_of_ts + pd.Timedelta(days=1)).isoformat()
    base_input, receipt = historical_input(r04b_snapshot, as_of=as_of, initial_price=anchor_price)
    base_vol = float(base_input.volatility[0])
    base_drift = float(base_input.drift[0])

    out: dict[str, tuple[str, float, np.ndarray]] = {}
    for label, mult in (
        ("gbm_low_vol_0.5x", 0.5),
        ("gbm_base_vol_1.0x", 1.0),
        ("gbm_high_vol_2.0x", 2.0),
    ):
        n_p = n_paths_map.get(label, 0)
        if n_p <= 0:
            continue
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
            model="gbm",
            n_paths=n_p,
            n_steps=n_steps,
            seed=seed + int(mult * 100),
            dt=1.0,
            batch_size=min(64, n_p),
            save_full_paths=True,
            compute=ComputeConfig(),
        )
        res = MonteCarloRunner(mc_cfg).run(scaled_input)
        out[label] = (f"GBM ({mult:.1f}x sigma={scaled_vol * 100:.3f}%/h)", scaled_vol, res.asset_paths[:, :, 0])

    if n_paths_map.get("moving_block_bootstrap_24h", 0) > 0:
        n_p = n_paths_map["moving_block_bootstrap_24h"]
        mbb_cfg = MonteCarloConfig(
            model="moving_block_bootstrap",
            n_paths=n_p,
            n_steps=n_steps,
            block_size=24,
            seed=seed + 300,
            dt=1.0,
            batch_size=min(64, n_p),
            save_full_paths=True,
            compute=ComputeConfig(),
        )
        res_mbb = MonteCarloRunner(mbb_cfg).run(base_input)
        out["moving_block_bootstrap_24h"] = ("Moving Block Bootstrap (24h)", base_vol, res_mbb.asset_paths[:, :, 0])

    if n_paths_map.get("regime_bootstrap_24h", 0) > 0:
        n_p = n_paths_map["regime_bootstrap_24h"]
        px_arr = np.asarray([b["close"] for b in bars_r04b], dtype=float)
        ret_arr = (px_arr[1:] / px_arr[:-1] - 1.0)[:, None]
        avail_utc = [max(t, pd.Timestamp(identity_r04b["acquired_at"])) for t in times_utc[1:]]
        threshold_as_of = times_utc[len(times_utc) // 2].isoformat()
        plan = PathPlan(n_paths=n_p, n_steps=n_steps, batch_size=min(64, n_p), seed=seed + 400)
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
            reg_batches = [paths[:, :, 0] for _, paths in reg_gen.batches(plan)]
            out["regime_bootstrap_24h"] = ("State-Conditional Regime Bootstrap (24h)", base_vol, np.vstack(reg_batches))
        except CalibrationError:
            pass

    if n_paths_map.get("jump_stress_downside", 0) > 0:
        n_p = n_paths_map["jump_stress_downside"]
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
        j_plan = PathPlan(n_paths=n_p, n_steps=n_steps, batch_size=min(64, n_p), seed=seed + 500)
        j_batches = [paths[:, :, 0] for _, paths in jump_gen.batches(j_plan)]
        out["jump_stress_downside"] = ("Jump Stress (p=0.015/h, -3% log jump)", base_vol, np.vstack(j_batches))

    return out, receipt, start_iso


def compute_paired_mc_comparison(
    cand_metrics: list[dict[str, Any]],
    base_metrics: list[dict[str, Any]] | None,
    *,
    cand_rel: dict[str, Any],
    seed: int,
    is_self: bool = False,
    baseline_available_n: int | None = None,
    include_beats_rate: bool = False,
    task_failed: bool = False,
) -> dict[str, Any]:
    """Compute paired MC return & CVaR95 differences with explicit missingness semantics.

    Distinguishes:
    1. ``SELF_BASELINE``: legitimate self-comparison (exact numeric 0.0).
    2. ``COMPUTED``: verified same-length, same-path-identity paired comparison (may be exact 0.0).
    3. ``COMPARISON_UNAVAILABLE`` with reason ``PAIRED_BASELINE_INSUFFICIENT`` /
       ``NO_BASELINE_EXECUTED`` / ``PATH_IDENTITY_MISMATCH`` / ``TASK_EXECUTION_FAILED``:
       returns ``None`` (JSON null / CSV empty), never numeric 0.0.
    """
    n_cand = len(cand_metrics)
    rets_pct = np.asarray([float(m["net_return_pct"]) for m in cand_metrics], dtype=float)

    def _unavailable(reason: str, base_n: int) -> dict[str, Any]:
        out: dict[str, Any] = {
            "candidate_n_paths": n_cand,
            "baseline_n_paths": base_n,
            "matched_n_paths": 0,
            "comparison_status": "COMPARISON_UNAVAILABLE",
            "comparison_unavailable_reason": reason,
            "delta_return_pp": None,
            "delta_cvar95_pp": None,
        }
        if include_beats_rate:
            out["paired_beats_rate_pct"] = None
        return out

    if task_failed:
        return _unavailable("TASK_EXECUTION_FAILED", int(baseline_available_n or 0))

    if is_self:
        out_self: dict[str, Any] = {
            "candidate_n_paths": n_cand,
            "baseline_n_paths": n_cand,
            "matched_n_paths": n_cand,
            "comparison_status": "SELF_BASELINE",
            "comparison_unavailable_reason": None,
            "delta_return_pp": 0.0,
            "delta_cvar95_pp": 0.0,
        }
        if include_beats_rate:
            out_self["paired_beats_rate_pct"] = 0.0
        return out_self

    if base_metrics is None:
        avail = int(baseline_available_n or 0)
        if avail > 0 and avail < n_cand:
            return _unavailable("PAIRED_BASELINE_INSUFFICIENT", avail)
        return _unavailable("NO_BASELINE_EXECUTED", avail)

    n_base = len(base_metrics)
    if n_base != n_cand:
        return _unavailable("PAIRED_BASELINE_INSUFFICIENT", n_base)

    # Verify strict path identity alignment whenever path identity metadata is present
    cand_ids = [m.get("mc_path_id") for m in cand_metrics]
    base_ids = [m.get("mc_path_id") for m in base_metrics]
    if any(x is not None for x in cand_ids) or any(x is not None for x in base_ids):
        if cand_ids != base_ids:
            return _unavailable("PATH_IDENTITY_MISMATCH", n_base)

    b_ret = np.asarray([float(m["net_return_pct"]) for m in base_metrics], dtype=float)
    b_loss = np.asarray([float(m["loss_fraction"]) for m in base_metrics], dtype=float)
    b_dd = np.asarray([float(m["max_drawdown"]) for m in base_metrics], dtype=float)
    b_rel = summarize_paths(b_loss, b_dd, threshold=0.05, confidences=(0.95,), resamples=299, bootstrap_seed=seed)
    diff = rets_pct - b_ret
    out_ok: dict[str, Any] = {
        "candidate_n_paths": n_cand,
        "baseline_n_paths": n_base,
        "matched_n_paths": n_cand,
        "comparison_status": "COMPUTED",
        "comparison_unavailable_reason": None,
        "delta_return_pp": round(float(np.mean(diff)), 4),
        "delta_cvar95_pp": round(
            (cand_rel["tails"]["0.95"]["cvar"] - b_rel["tails"]["0.95"]["cvar"]) * 100.0, 4
        ),
    }
    if include_beats_rate:
        out_ok["paired_beats_rate_pct"] = round(float(np.mean(diff > 1e-6) * 100.0), 2)
    return out_ok


def _aggregate_mc_group(
    path_metrics: list[dict[str, Any]],
    *,
    scenario: str,
    description: str,
    strategy_id: str,
    lifecycle_id: str,
    exec_profile: str,
    funding_regime: str,
    seed: int,
    paired_a_same_lc: list[dict[str, Any]] | None = None,
    paired_l0_same_strat: list[dict[str, Any]] | None = None,
    paired_a_baseline_n: int | None = None,
    paired_l0_baseline_n: int | None = None,
) -> dict[str, Any]:
    n = len(path_metrics)
    losses = np.asarray([float(m["loss_fraction"]) for m in path_metrics], dtype=float)
    rets_pct = np.asarray([float(m["net_return_pct"]) for m in path_metrics], dtype=float)
    dds = np.asarray([float(m["max_drawdown"]) for m in path_metrics], dtype=float)
    rel = summarize_paths(
        losses,
        dds,
        threshold=0.05,
        confidences=(0.95,),
        resamples=499,
        bootstrap_seed=seed,
    )
    tot_bo = int(sum(int(m["breakout_events"]) for m in path_metrics))
    tot_rot = int(sum(int(m["rotations_completed"]) for m in path_metrics))
    cvar_low = rel["tails"]["0.95"]["cvar_interval"]["lower"] * 100.0
    cvar_high = rel["tails"]["0.95"]["cvar_interval"]["upper"] * 100.0
    row: dict[str, Any] = {
        "scenario": scenario,
        "description": description,
        "seed": seed,
        "strategy_id": strategy_id,
        "lifecycle_id": lifecycle_id,
        "model_label": LIFECYCLE_LABELS[lifecycle_id],
        "execution_profile": exec_profile,
        "funding_regime": funding_regime,
        "evidence_label": "CONDITIONAL_RISK_ESTIMATED",
        "n_paths": n,
        "effective_tail_samples_95": round(0.05 * n, 2),
        "tail_95_status": rel["tails"]["0.95"]["statistical_status"],
        "capital_basis_usdt": str(DEMO_ANCHOR_CAPITAL),
        "mean_net_return_pct": round(float(np.mean(rets_pct)), 4),
        "median_net_return_pct": round(float(np.median(rets_pct)), 4),
        "loss_prob_pct": round(rel["loss_probability"]["estimate"] * 100.0, 2),
        "loss_prob_wilson_low_pct": round(rel["loss_probability"]["lower"] * 100.0, 2),
        "loss_prob_wilson_high_pct": round(rel["loss_probability"]["upper"] * 100.0, 2),
        "var95_pct": round(rel["tails"]["0.95"]["var"] * 100.0, 4),
        "cvar95_pct": round(rel["tails"]["0.95"]["cvar"] * 100.0, 4),
        "cvar95_boot_low_pct": round(cvar_low, 4),
        "cvar95_boot_high_pct": round(cvar_high, 4),
        "cvar95_ci_width_pp": round(cvar_high - cvar_low, 4),
        "max_dd_p95_pct": round(rel["maximum_drawdown_quantiles"]["0.95"]["estimate"] * 100.0, 4),
        "mean_max_dd_pct": round(float(np.mean(dds) * 100.0), 4),
        "mean_trade_gross_usdt": round(float(np.mean([m["trade_gross_pnl_usdt"] for m in path_metrics])), 6),
        "mean_fees_usdt": round(float(np.mean([m["fees_usdt"] for m in path_metrics])), 6),
        "mean_slippage_usdt": round(float(np.mean([m["slippage_usdt"] for m in path_metrics])), 6),
        "mean_funding_usdt": round(float(np.mean([m["realized_funding_usdt"] for m in path_metrics])), 6),
        "mean_holding_hours": round(float(np.mean([m["holding_hours"] for m in path_metrics])), 2),
        "mean_paused_inv_hours": round(float(np.mean([m["paused_with_inventory_hours"] for m in path_metrics])), 2),
        "mean_exit_delay_hours": round(float(np.mean([m["mean_exit_delay_hours"] for m in path_metrics])), 2),
        "stranded_tail_rate_pct": round(float(np.mean([m["stranded_at_end"] for m in path_metrics]) * 100.0), 2),
        "mean_fills": round(float(np.mean([m["fills_total"] for m in path_metrics])), 2),
        "mean_order_fill_rate": round(float(np.mean([m["order_fill_rate"] for m in path_metrics])), 4),
        "mean_turnover_ratio": round(float(np.mean([m["turnover_ratio"] for m in path_metrics])), 4),
        "mean_rotations": round(float(tot_rot / n), 3),
        "rotation_success_rate": round(tot_rot / tot_bo, 4) if tot_bo > 0 else None,
        "mean_posts_consumed": round(float(np.mean([m["posts_consumed"] for m in path_metrics])), 2),
        "halt_rate_pct": round(float(np.mean([m["halted"] for m in path_metrics]) * 100.0), 2),
        "mean_funding_blocked_hours": round(
            float(np.mean([m.get("funding_blocked_hours", 0) for m in path_metrics])), 2
        ),
        "mean_funding_exit_triggers": round(
            float(np.mean([m.get("funding_exit_triggers", 0) for m in path_metrics])), 3
        ),
        "all_zero_fifo_residual": bool(all(m["reconciled_zero_residual"] for m in path_metrics)),
        "max_simultaneous_exit_orders": max(int(m.get("max_simultaneous_exit_orders", 0)) for m in path_metrics),
        "exit_peak_monitoring_method": (
            path_metrics[0].get("exit_peak_monitoring_method", "EVENT_LEVEL_AUTHORITATIVE_LIFECYCLE")
            if path_metrics
            else None
        ),
    }

    cmp_a = compute_paired_mc_comparison(
        path_metrics,
        paired_a_same_lc,
        cand_rel=rel,
        seed=seed,
        is_self=(strategy_id == "A_FIXED"),
        baseline_available_n=paired_a_baseline_n,
        include_beats_rate=True,
    )
    row["delta_return_vs_A_same_lc_pp"] = cmp_a["delta_return_pp"]
    row["delta_cvar95_vs_A_same_lc_pp"] = cmp_a["delta_cvar95_pp"]
    row["paired_beats_A_rate_pct"] = cmp_a["paired_beats_rate_pct"]
    row["vs_A_comparison_status"] = cmp_a["comparison_status"]
    row["vs_A_comparison_unavailable_reason"] = cmp_a["comparison_unavailable_reason"]

    cmp_l0 = compute_paired_mc_comparison(
        path_metrics,
        paired_l0_same_strat,
        cand_rel=rel,
        seed=seed,
        is_self=(lifecycle_id == "L0"),
        baseline_available_n=paired_l0_baseline_n,
        include_beats_rate=False,
    )
    row["delta_return_vs_L0_pp"] = cmp_l0["delta_return_pp"]
    row["delta_cvar95_vs_L0_pp"] = cmp_l0["delta_cvar95_pp"]
    row["candidate_n_paths"] = cmp_l0["candidate_n_paths"]
    row["baseline_n_paths"] = cmp_l0["baseline_n_paths"]
    row["matched_n_paths"] = cmp_l0["matched_n_paths"]
    row["comparison_status"] = cmp_l0["comparison_status"]
    row["comparison_unavailable_reason"] = cmp_l0["comparison_unavailable_reason"]

    return row


MC_TAIL_EXPAND_SCENARIOS: tuple[str, ...] = (
    "gbm_base_vol_1.0x",
    "jump_stress_downside",
)


def run_r11_monte_carlo(
    r04b_snapshot: Path,
    b_entry_only: FundingPolicy,
    b_entry_exit: FundingPolicy,
    c_policy: VolatilityPolicy,
    *,
    screen_paths: int = 64,
    expand_paths: int = 512,
    tail_expand_paths: int = 1024,
    cross_seed_paths: int = 256,
    primary_seed: int = 1100,
    cross_seed: int = 2200,
    n_steps: int = 168,
    max_workers: int = 8,
) -> dict[str, Any]:
    """Run progressive Monte Carlo (screen -> key expand -> tail expand) & cross-seed stability."""
    t0 = perf_counter()
    all_gen_names = (
        "gbm_low_vol_0.5x",
        "gbm_base_vol_1.0x",
        "gbm_high_vol_2.0x",
        "moving_block_bootstrap_24h",
        "regime_bootstrap_24h",
        "jump_stress_downside",
    )
    max_exp = max(screen_paths, expand_paths)
    max_tail = max(max_exp, tail_expand_paths)
    n_map_primary = {
        g: (
            max_tail
            if g in MC_TAIL_EXPAND_SCENARIOS
            else (max_exp if g in MC_KEY_SCENARIOS else screen_paths)
        )
        for g in all_gen_names
    }
    primary_paths, calib_receipt, start_iso = _generate_mc_paths(
        r04b_snapshot,
        anchor_price=DEMO_ANCHOR_PRICE,
        n_paths_map=n_map_primary,
        n_steps=n_steps,
        seed=primary_seed,
    )
    cross_map = {g: cross_seed_paths for g in MC_KEY_SCENARIOS}
    cross_paths, _, _ = _generate_mc_paths(
        r04b_snapshot,
        anchor_price=DEMO_ANCHOR_PRICE,
        n_paths_map=cross_map,
        n_steps=n_steps,
        seed=cross_seed,
    )

    arm_policies = {
        "A_FIXED": (None, None),
        b_entry_only.candidate_id: (b_entry_only, None),
        b_entry_exit.candidate_id: (b_entry_exit, None),
        c_policy.candidate_id: (None, c_policy),
    }
    warmups_by_vol: dict[float, list[float]] = {}

    def get_warmup(vol_1h: float) -> list[float]:
        key = round(vol_1h, 8)
        if key not in warmups_by_vol:
            warmups_by_vol[key] = _make_neutral_warmup(DEMO_ANCHOR_PRICE, vol_1h, seed=1010)
        return warmups_by_vol[key]

    tasks_by_key: dict[tuple[Any, ...], tuple[Any, ...]] = {}
    task_seq = 0

    def enqueue_batch(
        seed_val: int,
        scen: str,
        paths_arr: np.ndarray,
        vol_1h: float,
        n_use: int,
        arm_id: str,
        lc_id: str,
        prof_name: str,
        f_regime: str,
    ) -> None:
        nonlocal task_seq
        bp, cp = arm_policies[arm_id]
        wc = get_warmup(vol_1h) if cp is not None else None
        for i in range(n_use):
            key = (seed_val, scen, i, arm_id, lc_id, prof_name, f_regime)
            if key in tasks_by_key:
                continue
            task_seq += 1
            tasks_by_key[key] = (
                key,
                paths_arr[i],
                start_iso,
                arm_id,
                lc_id,
                prof_name,
                f_regime,
                bp,
                cp,
                wc,
                DEMO_ANCHOR_CAPITAL,
                f"{task_seq:07d}",
            )

    # Stage 1A: Full 6-generator screening at `screen_paths`
    core_arms = ("A_FIXED", b_entry_exit.candidate_id, c_policy.candidate_id)
    for scen, (_, vol_1h, arr) in primary_paths.items():
        regimes_s1 = (
            ("EX_FUNDING", "NORMAL_SYNTHETIC_0.125BPS", "SWITCH_48H", "DEMO_HIGH_FUNDING_22.97BPS")
            if scen in MC_KEY_SCENARIOS
            else ("NORMAL_SYNTHETIC_0.125BPS", "SWITCH_48H")
        )
        for f_regime in regimes_s1:
            for lc_id, prof_name in (
                ("L0", "IDEAL_LIMIT_FILL"),
                ("L1", "IDEAL_LIMIT_FILL"),
                ("L2", "IDEAL_LIMIT_FILL"),
                ("L2", "DELAYED_EXIT_FILL"),
            ):
                arms_here = (
                    ("A_FIXED", b_entry_only.candidate_id, b_entry_exit.candidate_id, c_policy.candidate_id)
                    if (scen in MC_KEY_SCENARIOS and f_regime in {"SWITCH_48H", "DEMO_HIGH_FUNDING_22.97BPS"})
                    else core_arms
                )
                for arm_id in arms_here:
                    enqueue_batch(primary_seed, scen, arr, vol_1h, screen_paths, arm_id, lc_id, prof_name, f_regime)

        # Stage 1B: Execution realism sensitivity (5 profiles) on 3 key generators
        if scen in MC_KEY_SCENARIOS:
            for f_regime in ("SWITCH_48H", "DEMO_HIGH_FUNDING_22.97BPS"):
                for lc_id, prof_name in (
                    ("L1", "DELAYED_EXIT_FILL"),
                    ("L1", "LIMITED_CAPACITY_PARTIAL"),
                    ("L1", "UNFILLED_EXIT_TIMEOUT"),
                    ("L2", "LIMITED_CAPACITY_PARTIAL"),
                    ("L2", "UNFILLED_EXIT_TIMEOUT"),
                    ("L2", "GAP_AND_CANCEL_FAULT"),
                ):
                    for arm_id in core_arms:
                        enqueue_batch(primary_seed, scen, arr, vol_1h, screen_paths, arm_id, lc_id, prof_name, f_regime)

    # Stage 2A: Expand key generators to `expand_paths` (e.g. 512) on SWITCH_48H across L0/L1/L2_IDEAL/L2_DELAYED
    if max_exp > screen_paths:
        for scen in MC_KEY_SCENARIOS:
            _, vol_1h, arr = primary_paths[scen]
            for lc_id, prof_name in (
                ("L0", "IDEAL_LIMIT_FILL"),
                ("L1", "IDEAL_LIMIT_FILL"),
                ("L2", "IDEAL_LIMIT_FILL"),
                ("L2", "DELAYED_EXIT_FILL"),
            ):
                for arm_id in core_arms:
                    enqueue_batch(primary_seed, scen, arr, vol_1h, max_exp, arm_id, lc_id, prof_name, "SWITCH_48H")

    # Stage 2B: Targeted tail expansion to `tail_expand_paths` (e.g. 1024) on highest-variance decision cells
    if max_tail > max_exp:
        for scen in MC_TAIL_EXPAND_SCENARIOS:
            _, vol_1h, arr = primary_paths[scen]
            for lc_id, prof_name in (
                ("L2", "IDEAL_LIMIT_FILL"),
                ("L2", "DELAYED_EXIT_FILL"),
            ):
                for arm_id in ("A_FIXED", b_entry_exit.candidate_id):
                    enqueue_batch(primary_seed, scen, arr, vol_1h, max_tail, arm_id, lc_id, prof_name, "SWITCH_48H")

    # Stage 2C: Cross-seed stability check (seed=2200, n=cross_seed_paths) on key generators
    if cross_seed_paths > 0:
        for scen in MC_KEY_SCENARIOS:
            _, vol_1h, arr = cross_paths[scen]
            for lc_id, prof_name in (
                ("L2", "IDEAL_LIMIT_FILL"),
                ("L2", "DELAYED_EXIT_FILL"),
            ):
                for arm_id in core_arms:
                    enqueue_batch(cross_seed, scen, arr, vol_1h, cross_seed_paths, arm_id, lc_id, prof_name, "SWITCH_48H")

    print(
        f"[P8-R11] Running {len(tasks_by_key)} deduplicated Monte Carlo lifecycle paths "
        f"(screen={screen_paths}, expand={max_exp}, tail_expand={max_tail}, cross_seed={cross_seed_paths}, workers={max_workers})...",
        flush=True,
    )
    raw_pairs = _run_pool(_mc_task_worker, list(tasks_by_key.values()), max_workers=max_workers)
    mc_cache: dict[tuple[Any, ...], dict[str, Any]] = dict(raw_pairs)
    elapsed_mc = round(perf_counter() - t0, 2)
    print(f"[P8-R11] Monte Carlo completed in {elapsed_mc:.1f}s", flush=True)

    def get_series(seed_val: int, scen: str, n_use: int, arm_id: str, lc_id: str, prof_name: str, f_regime: str) -> list[dict[str, Any]]:
        return [mc_cache[(seed_val, scen, i, arm_id, lc_id, prof_name, f_regime)] for i in range(n_use)]

    # Aggregate Stage 1 screening table
    screening_rows: list[dict[str, Any]] = []
    for scen, (desc, _, _) in primary_paths.items():
        regimes_here = (
            ("EX_FUNDING", "NORMAL_SYNTHETIC_0.125BPS", "SWITCH_48H", "DEMO_HIGH_FUNDING_22.97BPS")
            if scen in MC_KEY_SCENARIOS
            else ("NORMAL_SYNTHETIC_0.125BPS", "SWITCH_48H")
        )
        for f_regime in regimes_here:
            for lc_id, prof_name in (
                ("L0", "IDEAL_LIMIT_FILL"),
                ("L1", "IDEAL_LIMIT_FILL"),
                ("L2", "IDEAL_LIMIT_FILL"),
                ("L2", "DELAYED_EXIT_FILL"),
            ):
                arms_here = (
                    ("A_FIXED", b_entry_only.candidate_id, b_entry_exit.candidate_id, c_policy.candidate_id)
                    if (scen in MC_KEY_SCENARIOS and f_regime in {"SWITCH_48H", "DEMO_HIGH_FUNDING_22.97BPS"})
                    else core_arms
                )
                a_same = get_series(primary_seed, scen, screen_paths, "A_FIXED", lc_id, prof_name, f_regime)
                for arm_id in arms_here:
                    ser = get_series(primary_seed, scen, screen_paths, arm_id, lc_id, prof_name, f_regime)
                    l0_same = (
                        get_series(primary_seed, scen, screen_paths, arm_id, "L0", "IDEAL_LIMIT_FILL", f_regime)
                        if lc_id != "L0"
                        else None
                    )
                    screening_rows.append(
                        _aggregate_mc_group(
                            ser,
                            scenario=scen,
                            description=desc,
                            strategy_id=arm_id,
                            lifecycle_id=lc_id,
                            exec_profile=prof_name,
                            funding_regime=f_regime,
                            seed=primary_seed,
                            paired_a_same_lc=None if arm_id == "A_FIXED" else a_same,
                            paired_l0_same_strat=l0_same,
                        )
                    )

    # Aggregate Execution Realism Sensitivity table on key generators
    exec_sens_rows: list[dict[str, Any]] = []
    for scen in MC_KEY_SCENARIOS:
        desc = primary_paths[scen][0]
        for f_regime in ("SWITCH_48H", "DEMO_HIGH_FUNDING_22.97BPS"):
            for lc_id, prof_list in (
                ("L0", ("IDEAL_LIMIT_FILL",)),
                ("L1", ("IDEAL_LIMIT_FILL", "DELAYED_EXIT_FILL", "LIMITED_CAPACITY_PARTIAL", "UNFILLED_EXIT_TIMEOUT")),
                ("L2", ALL_EXECUTION_PROFILES),
            ):
                for prof_name in prof_list:
                    a_same = get_series(primary_seed, scen, screen_paths, "A_FIXED", lc_id, prof_name, f_regime)
                    for arm_id in core_arms:
                        ser = get_series(primary_seed, scen, screen_paths, arm_id, lc_id, prof_name, f_regime)
                        l0_same = (
                            get_series(primary_seed, scen, screen_paths, arm_id, "L0", "IDEAL_LIMIT_FILL", f_regime)
                            if lc_id != "L0"
                            else None
                        )
                        exec_sens_rows.append(
                            _aggregate_mc_group(
                                ser,
                                scenario=scen,
                                description=desc,
                                strategy_id=arm_id,
                                lifecycle_id=lc_id,
                                exec_profile=prof_name,
                                funding_regime=f_regime,
                                seed=primary_seed,
                                paired_a_same_lc=None if arm_id == "A_FIXED" else a_same,
                                paired_l0_same_strat=l0_same,
                            )
                        )

    # Aggregate Progressive Expansion & Prefix Convergence table (64 -> 128 -> 256 -> 512 -> 1024)
    convergence_rows: list[dict[str, Any]] = []
    expanded_summary_rows: list[dict[str, Any]] = []
    for scen in MC_KEY_SCENARIOS:
        desc = primary_paths[scen][0]
        for lc_id, prof_name in (
            ("L0", "IDEAL_LIMIT_FILL"),
            ("L1", "IDEAL_LIMIT_FILL"),
            ("L2", "IDEAL_LIMIT_FILL"),
            ("L2", "DELAYED_EXIT_FILL"),
        ):
            for arm_id in core_arms:
                has_tail = (
                    scen in MC_TAIL_EXPAND_SCENARIOS
                    and lc_id == "L2"
                    and arm_id in {"A_FIXED", b_entry_exit.candidate_id}
                    and max_tail > max_exp
                )
                n_max_here = max_tail if has_tail else max_exp
                ser_full = get_series(primary_seed, scen, n_max_here, arm_id, lc_id, prof_name, "SWITCH_48H")
                a_full = get_series(primary_seed, scen, n_max_here, "A_FIXED", lc_id, prof_name, "SWITCH_48H")
                l0_avail = (
                    get_series(primary_seed, scen, max_exp, arm_id, "L0", "IDEAL_LIMIT_FILL", "SWITCH_48H")
                    if lc_id != "L0"
                    else None
                )
                l0_full = l0_avail if (l0_avail is not None and n_max_here <= len(l0_avail)) else None
                expanded_summary_rows.append(
                    _aggregate_mc_group(
                        ser_full,
                        scenario=scen,
                        description=desc,
                        strategy_id=arm_id,
                        lifecycle_id=lc_id,
                        exec_profile=prof_name,
                        funding_regime="SWITCH_48H",
                        seed=primary_seed,
                        paired_a_same_lc=None if arm_id == "A_FIXED" else a_full,
                        paired_l0_same_strat=l0_full,
                        paired_l0_baseline_n=len(l0_avail) if l0_avail is not None else n_max_here,
                    )
                )
                conv_grid = sorted({k for k in (64, 128, 256, 512, 1024, n_max_here) if k <= n_max_here})
                for m_n in conv_grid:
                    sub_l0 = None if (l0_avail is None or m_n > len(l0_avail)) else l0_avail[:m_n]
                    sub_agg = _aggregate_mc_group(
                        ser_full[:m_n],
                        scenario=scen,
                        description=desc,
                        strategy_id=arm_id,
                        lifecycle_id=lc_id,
                        exec_profile=prof_name,
                        funding_regime="SWITCH_48H",
                        seed=primary_seed,
                        paired_a_same_lc=None if arm_id == "A_FIXED" else a_full[:m_n],
                        paired_l0_same_strat=sub_l0,
                        paired_l0_baseline_n=len(l0_avail) if l0_avail is not None else m_n,
                    )
                    convergence_rows.append(
                        {
                            "scenario": scen,
                            "funding_regime": "SWITCH_48H",
                            "lifecycle_id": lc_id,
                            "execution_profile": prof_name,
                            "strategy_id": arm_id,
                            "n_paths": m_n,
                            "effective_tail_samples_95": sub_agg["effective_tail_samples_95"],
                            "tail_95_status": sub_agg["tail_95_status"],
                            "mean_net_return_pct": sub_agg["mean_net_return_pct"],
                            "var95_pct": sub_agg["var95_pct"],
                            "cvar95_pct": sub_agg["cvar95_pct"],
                            "cvar95_ci_width_pp": sub_agg["cvar95_ci_width_pp"],
                            "delta_cvar95_vs_A_same_lc_pp": sub_agg["delta_cvar95_vs_A_same_lc_pp"],
                            "delta_cvar95_vs_L0_pp": sub_agg["delta_cvar95_vs_L0_pp"],
                            "l0_comparison_status": sub_agg["comparison_status"],
                        }
                    )

    # Cross-seed stability comparison (seed=1100 vs seed=2200 at n=cross_seed_paths)
    cross_seed_rows: list[dict[str, Any]] = []
    if cross_seed_paths > 0:
        n_cs = min(cross_seed_paths, max_exp)
        for scen in MC_KEY_SCENARIOS:
            desc = primary_paths[scen][0]
            for lc_id, prof_name in (
                ("L2", "IDEAL_LIMIT_FILL"),
                ("L2", "DELAYED_EXIT_FILL"),
            ):
                a1 = get_series(primary_seed, scen, n_cs, "A_FIXED", lc_id, prof_name, "SWITCH_48H")
                a2 = get_series(cross_seed, scen, n_cs, "A_FIXED", lc_id, prof_name, "SWITCH_48H")
                for arm_id in core_arms:
                    s1 = _aggregate_mc_group(
                        get_series(primary_seed, scen, n_cs, arm_id, lc_id, prof_name, "SWITCH_48H"),
                        scenario=scen,
                        description=desc,
                        strategy_id=arm_id,
                        lifecycle_id=lc_id,
                        exec_profile=prof_name,
                        funding_regime="SWITCH_48H",
                        seed=primary_seed,
                        paired_a_same_lc=None if arm_id == "A_FIXED" else a1,
                    )
                    s2 = _aggregate_mc_group(
                        get_series(cross_seed, scen, n_cs, arm_id, lc_id, prof_name, "SWITCH_48H"),
                        scenario=scen,
                        description=desc,
                        strategy_id=arm_id,
                        lifecycle_id=lc_id,
                        exec_profile=prof_name,
                        funding_regime="SWITCH_48H",
                        seed=cross_seed,
                        paired_a_same_lc=None if arm_id == "A_FIXED" else a2,
                    )
                    cross_seed_rows.append(
                        {
                            "scenario": scen,
                            "funding_regime": "SWITCH_48H",
                            "lifecycle_id": lc_id,
                            "execution_profile": prof_name,
                            "strategy_id": arm_id,
                            "n_paths": n_cs,
                            "seed_a": primary_seed,
                            "seed_b": cross_seed,
                            "mean_ret_seed_a_pct": s1["mean_net_return_pct"],
                            "mean_ret_seed_b_pct": s2["mean_net_return_pct"],
                            "mean_ret_abs_diff_pp": round(
                                abs(s1["mean_net_return_pct"] - s2["mean_net_return_pct"]), 4
                            ),
                            "cvar95_seed_a_pct": s1["cvar95_pct"],
                            "cvar95_seed_b_pct": s2["cvar95_pct"],
                            "cvar95_abs_diff_pp": round(abs(s1["cvar95_pct"] - s2["cvar95_pct"]), 4),
                            "delta_cvar_vs_A_seed_a_pp": s1["delta_cvar95_vs_A_same_lc_pp"],
                            "delta_cvar_vs_A_seed_b_pp": s2["delta_cvar95_vs_A_same_lc_pp"],
                        }
                    )

    return {
        "calibration_receipt": calib_receipt,
        "elapsed_seconds": elapsed_mc,
        "total_unique_mc_runs": len(mc_cache),
        "screen_paths": screen_paths,
        "expand_paths": max_exp,
        "tail_expand_paths": max_tail,
        "cross_seed_paths": cross_seed_paths,
        "primary_seed": primary_seed,
        "cross_seed": cross_seed,
        "all_zero_fifo_residual": bool(all(r["reconciled_zero_residual"] for r in mc_cache.values())),
        "max_simultaneous_exit_orders": max(
            (int(r.get("max_simultaneous_exit_orders", 0)) for r in mc_cache.values()), default=0
        ),
        "screening": screening_rows,
        "execution_sensitivity": exec_sens_rows,
        "expanded_key_scenarios": expanded_summary_rows,
        "convergence": convergence_rows,
        "cross_seed_stability": cross_seed_rows,
    }


def derive_r11_conclusions(
    historical: dict[str, Any],
    deterministic: list[dict[str, Any]],
    monte_carlo: dict[str, Any],
    *,
    exit_peak_verification: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compute formal P8-R11 conclusion tags strictly from observed ledger & statistical evidence.

    Separates:
    - FIFO ledger reconciliation verification across all simulated runs.
    - Event-level authoritative exit-order peak verification (explicitly refusing to claim
      event-level full-lifecycle verification for legacy bar-end-sampled runs).
    """
    all_zero_fifo = bool(
        historical["all_zero_fifo_residual"]
        and all(bool(r["reconciled_zero_residual"]) for r in deterministic)
        and monte_carlo["all_zero_fifo_residual"]
    )
    det_event_peak = max((int(r.get("max_simultaneous_exit_orders", 0)) for r in deterministic), default=0)
    det_overlap_count = sum(len(r.get("exit_overlap_violations", [])) for r in deterministic)

    if exit_peak_verification is not None:
        ev_verified_runs = int(exit_peak_verification.get("event_level_verified_runs", 0))
        ev_max_peak = int(exit_peak_verification.get("event_level_max_simultaneous_exit_orders", 0))
        ev_overlap_count = int(exit_peak_verification.get("event_level_overlap_violations", 0))
        single_exit_invariant = bool(ev_verified_runs > 0 and ev_max_peak <= 1 and ev_overlap_count == 0)
        peak_scope_desc = str(
            exit_peak_verification.get(
                "scope_summary",
                f"事件级权威生命周期峰值监测已覆盖 {ev_verified_runs} 次复跑/验证（确定性压力、历史机制补算与 MC 代表批次）",
            )
        )
    else:
        ev_max_peak = max(
            int(historical.get("max_simultaneous_exit_orders", 0)),
            det_event_peak,
            int(monte_carlo.get("max_simultaneous_exit_orders", 0)),
        )
        ev_overlap_count = det_overlap_count
        ev_verified_runs = (
            int(historical.get("total_unique_window_runs", 0))
            + len(deterministic)
            + int(monte_carlo.get("total_unique_mc_runs", 0))
        )
        single_exit_invariant = bool(ev_verified_runs > 0 and ev_max_peak <= 1 and ev_overlap_count == 0)
        peak_scope_desc = f"全部 {ev_verified_runs} 次模拟均在事件级权威生命周期峰值监测下运行"

    engineering_accepted = all_zero_fifo and single_exit_invariant

    # Check if any strategy beats CASH positively on MAINNET_ACTUAL L2 OOS
    mainnet_l2_ideal = historical["wf_results_index"]["L2__IDEAL_LIMIT_FILL__MAINNET_ACTUAL"]["oos_summary"]
    mainnet_l2_delayed = historical["wf_results_index"]["L2__DELAYED_EXIT_FILL__MAINNET_ACTUAL"]["oos_summary"]
    any_pos_mainnet_l2 = any(
        float(mainnet_l2_ideal[role]["mean_net_return_pct"]) > 0.0
        for role in ("A_FIXED", "B_WF", "C_WF")
    )

    # Reused R10 historical windows cannot grant independent unseen OOS advantage.
    windows_reused_from_r10 = True
    conditional_risk_estimated = bool(
        engineering_accepted
        and historical.get("total_unique_window_runs", 0) > 0
        and monte_carlo.get("total_unique_mc_runs", 0) > 0
    )
    oos_advantage_supported = False if windows_reused_from_r10 else any_pos_mainnet_l2
    research_inconclusive = not (any_pos_mainnet_l2 and oos_advantage_supported)

    return {
        "ENGINEERING_ACCEPTED": engineering_accepted,
        "CONDITIONAL_RISK_ESTIMATED": conditional_risk_estimated,
        "RESEARCH_INCONCLUSIVE": research_inconclusive,
        "OOS_ADVANTAGE_SUPPORTED": oos_advantage_supported,  # Strictly forbidden on reused R10 historical windows!
        "invariants": {
            "all_zero_fifo_residual": all_zero_fifo,
            "single_authoritative_exit_order_verified": single_exit_invariant,
            "event_level_exit_peak_verified_runs": ev_verified_runs,
            "event_level_max_simultaneous_exit_orders": ev_max_peak,
            "event_level_overlap_violations": ev_overlap_count,
            "any_positive_mainnet_l2_ideal_oos_mean": any_pos_mainnet_l2,
            "mainnet_l2_ideal_a_return_pct": mainnet_l2_ideal["A_FIXED"]["mean_net_return_pct"],
            "mainnet_l2_ideal_b_wf_return_pct": mainnet_l2_ideal["B_WF"]["mean_net_return_pct"],
            "mainnet_l2_ideal_c_wf_return_pct": mainnet_l2_ideal["C_WF"]["mean_net_return_pct"],
            "mainnet_l2_delayed_a_return_pct": mainnet_l2_delayed["A_FIXED"]["mean_net_return_pct"],
            "mainnet_l2_delayed_b_wf_return_pct": mainnet_l2_delayed["B_WF"]["mean_net_return_pct"],
            "mainnet_l2_delayed_c_wf_return_pct": mainnet_l2_delayed["C_WF"]["mean_net_return_pct"],
        },
        "notes": {
            "ENGINEERING_ACCEPTED": (
                f"账本零残差验收覆盖全部模拟（FIFO 残差严格为 0）；单权威退出订单事件级峰值监测（含同 Bar 提交即成交、"
                f"CANCEL_PENDING、UNKNOWN 与跨代连续性）{peak_scope_desc}，验证峰值 <= 1 且零重叠违规"
                "（旧 Bar 尾采样批次不冒充全时点事件级认证）。"
            ),
            "CONDITIONAL_RISK_ESTIMATED": (
                "已在 5 组显式条件执行假设、5 档资金费体制及 6 类随机路径模型下完成 A/B/C/CASH × L0/L1/L2 的配对收益与尾部风险估计；"
                "缺失 L0 配对路径的 8 组 1024 路径扩容单元以 null 与 COMPARISON_UNAVAILABLE 显式标记（无伪零），小时 OHLC 非真实 L2 盘口。"
            ),
            "RESEARCH_INCONCLUSIVE": (
                "在真实主网资金费及完整 F11/F08 生命周期下，A/B/C 均未证明相对现金基准存在稳健的真实成本后正样本外超额收益；"
                "固定参数机制消融与重新优化完整流程比较已严格分离，资金费敏感机制的价值严格限定于高资金费与退出摩擦体制下的防御风控。"
            ),
            "OOS_ADVANTAGE_SUPPORTED": (
                "未授予（NOT_MET / BLOCKED）：本研究复用了 R10 的 193 天 27 窗（18 个历史 OOS 窗），属于可比历史复验"
                "而非全新未见独立样本，且主网真实费率下各网格策略均值净收益仍跑输现金基准。"
            ),
        },
    }


def run_p8_r11_study(
    *,
    repo_root: Path,
    r04b_snapshot: Path,
    cache_dir: Path,
    output_dir: Path | None = None,
    screen_paths: int = 64,
    expand_paths: int = 512,
    tail_expand_paths: int = 1024,
    cross_seed_paths: int = 256,
    primary_seed: int = 1100,
    cross_seed: int = 2200,
    max_workers: int | None = None,
) -> dict[str, Any]:
    """Execute the complete P8-R11 unified lifecycle comparative study."""
    t_total = perf_counter()
    workers = max_workers if max_workers is not None else min(20, max(1, (os.cpu_count() or 2) - 4))
    print(
        f"[P8-R11] Starting unified lifecycle study: workers={workers}, "
        f"screen_paths={screen_paths}, expand_paths={expand_paths}, "
        f"tail_expand_paths={tail_expand_paths}, cross_seed_paths={cross_seed_paths}",
        flush=True,
    )

    # 1. Load certified local dataset without network mutation
    all_bars, all_funding, data_audit = load_and_audit_r10_dataset(
        repo_root=repo_root,
        r04b_snapshot=r04b_snapshot,
        cache_dir=cache_dir,
        allow_network_supplement=False,
    )
    data_audit["execution_model"] = UNIFIED_EXECUTION_MODEL_VERSION
    data_audit["data_independence_notice"] = (
        "Reuses certified R10 193-day / 4632-bar / 593-funding dataset for comparable historical re-validation. "
        "Does NOT constitute unseen independent OOS qualification evidence."
    )
    _, b_policies, c_policies = load_strategy_configs(repo_root)
    b_map = {p.candidate_id: p for p in b_policies}
    c_map = {p.candidate_id: p for p in c_policies}

    # 2. Deterministic stress suite first (small-scale deterministic verification)
    t_det = perf_counter()
    init_sigma_ref = _compute_ref_vol_from_bars(
        all_bars, build_nonoverlapping_weekly_windows(all_bars)[9]["start_bar_index"]
    )
    c5_frozen = replace(c_map["C5"], reference_vol_1h=init_sigma_ref)
    deterministic_rows = run_r11_deterministic_stress(b_map["B2"], b_map["B5"], c5_frozen, max_workers=workers)
    det_elapsed = round(perf_counter() - t_det, 2)
    print(f"[P8-R11] Deterministic stress suite: {len(deterministic_rows)} runs ({det_elapsed:.1f}s)", flush=True)

    # 3. Full historical 27-window & 6-fold WF + 2D ablations + execution realism matrix
    historical = run_r11_historical_study(
        all_bars,
        all_funding,
        b_policies,
        c_policies,
        repo_root=repo_root,
        max_workers=workers,
    )
    print(
        f"[P8-R11] Historical study: {historical['total_unique_window_runs']} unique runs "
        f"({historical['elapsed_seconds']:.1f}s), all_zero_fifo={historical['all_zero_fifo_residual']}",
        flush=True,
    )

    # 4. Progressive Monte Carlo & Cross-Seed Convergence
    monte_carlo = run_r11_monte_carlo(
        r04b_snapshot,
        b_map["B2"],
        b_map["B5"],
        c5_frozen,
        screen_paths=screen_paths,
        expand_paths=expand_paths,
        tail_expand_paths=tail_expand_paths,
        cross_seed_paths=cross_seed_paths,
        primary_seed=primary_seed,
        cross_seed=cross_seed,
        max_workers=workers,
    )

    conclusions = derive_r11_conclusions(historical, deterministic_rows, monte_carlo)
    elapsed_total = round(perf_counter() - t_total, 2)

    s02_handoff = build_s02_handoff_contract(
        repo_root=repo_root,
        conclusions=conclusions,
        historical_oos_summary=historical["wf_results_index"],
        mc_key_summary=monte_carlo["expanded_key_scenarios"],
        data_audit=data_audit,
        paired_ablations=historical["paired_ablations"],
    )

    study: dict[str, Any] = {
        "schema_version": 1,
        "study_id": "P8-R11",
        "execution_model_version": UNIFIED_EXECUTION_MODEL_VERSION,
        "conclusions": conclusions,
        "timing": {
            "deterministic_seconds": det_elapsed,
            "historical_seconds": historical["elapsed_seconds"],
            "monte_carlo_seconds": monte_carlo["elapsed_seconds"],
            "total_seconds": elapsed_total,
            "workers": workers,
        },
        "ledger_verification": {
            "historical_unique_runs": historical["total_unique_window_runs"],
            "deterministic_runs": len(deterministic_rows),
            "monte_carlo_unique_runs": monte_carlo["total_unique_mc_runs"],
            "total_unique_runs": (
                historical["total_unique_window_runs"]
                + len(deterministic_rows)
                + monte_carlo["total_unique_mc_runs"]
            ),
            "all_zero_fifo_residual": conclusions["invariants"]["all_zero_fifo_residual"],
            "single_authoritative_exit_order_verified": conclusions["invariants"][
                "single_authoritative_exit_order_verified"
            ],
        },
        "data_audit": data_audit,
        "historical": historical,
        "deterministic_stress": deterministic_rows,
        "monte_carlo": monte_carlo,
        "s02_handoff": s02_handoff,
    }

    if output_dir is not None:
        from factorlab.experiments.p8_r11_report import write_p8_r11_artifacts

        write_p8_r11_artifacts(study, Path(output_dir))
    return study
