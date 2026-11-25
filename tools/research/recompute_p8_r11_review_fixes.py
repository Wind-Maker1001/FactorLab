"""Targeted P8-R11 post-review remediation & artifact recomputation script.

Applies minimal, auditable recomputations to ``docs/validation/p8-r11/`` for PR #252
independent review findings without unnecessarily re-running all 60,240 historical+MC runs:

1. P2-1: Replaces all 8 pseudo-zero 1024-vs-512 MC L0 comparison cells with explicit
   null / COMPARISON_UNAVAILABLE (PAIRED_BASELINE_INSUFFICIENT) semantics and path counts.
2. P2-2 + Section 10: Recomputes minimal paired 18-OOS historical windows for frozen
   L0-selected candidates (B_FROZEN_L0, C_FROZEN_L0) alongside predeclared A_FIXED/B2/B5/C5
   and re-optimized B_WF/C_WF under the new event-level exit-peak monitor, then builds
   strictly separated FIXED_PARAM_MECHANISM_ABLATION vs LIFECYCLE_REOPTIMIZED_PIPELINE_COMPARISON
   tables with unrounded Holm-Bonferroni p-values.
3. P2-3: Re-executes the full 338-run deterministic stress suite, the historical OOS
   recompute batch, and a representative 1,152-path MC verification batch under
   EVENT_LEVEL_AUTHORITATIVE_LIFECYCLE monitoring, reporting honest split acceptance scope.
4. P3-1: Programmatically audits the exact 36 covered historical L1/L2 B2/B5 cells.
5. Synchronizes summary.json, s02_handoff.json (strictly identical), CSVs, and REPORT.md.
"""
from __future__ import annotations

import argparse
from dataclasses import replace
from decimal import Decimal
import json
import os
from pathlib import Path
import subprocess
import sys
from time import perf_counter
from typing import Any

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))
os.environ["PYTHONPATH"] = f"{SRC_ROOT}{os.pathsep}{os.environ.get('PYTHONPATH', '')}"

from factorlab.experiments.grid_f11_f08_offline import EXIT_PEAK_MONITORING_METHOD
from factorlab.experiments.grid_funding_adaptive import load_and_audit_r10_dataset
from factorlab.experiments.grid_r10_study import (
    DEMO_ANCHOR_CAPITAL,
    DEMO_ANCHOR_PRICE,
    HISTORICAL_REPLAY_CAPITAL,
    _compute_ref_vol_from_bars,
    build_nonoverlapping_weekly_windows,
    load_strategy_configs,
    validate_research_windows,
)
from factorlab.experiments.p8_r11_report import render_p8_r11_report
from factorlab.experiments.p8_r11_study import (
    CORE_FUNDING_REGIMES,
    MC_KEY_SCENARIOS,
    _generate_mc_paths,
    _hist_task_worker,
    _make_neutral_warmup,
    _mc_task_worker,
    _reconcile_l0_against_frozen_r10,
    _run_pool,
    build_separated_paired_ablations,
    derive_r11_conclusions,
    run_r11_deterministic_stress,
)
from factorlab.experiments.p8_r11_unified_lifecycle import (
    ALL_EXECUTION_PROFILES,
    LIFECYCLE_LABELS,
    build_s02_handoff_contract,
    run_unified_lifecycle,
    summarize_unified_windows,
)

D = Decimal


def _dump_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    flat = []
    for r in rows:
        item = {}
        for k, v in r.items():
            if isinstance(v, (dict, list)):
                item[k] = json.dumps(v, sort_keys=True, ensure_ascii=False)
            else:
                item[k] = v
        flat.append(item)
    pd.DataFrame(flat).to_csv(path, index=False, encoding="utf-8")


def _patch_mc_missingness(mc: dict[str, Any]) -> list[dict[str, Any]]:
    """Apply P2-1 explicit null & status semantics to all saved MC aggregation tables."""
    expand_n = int(mc["expand_paths"])
    pseudo_zero_cells: list[dict[str, Any]] = []

    def patch_row(row: dict[str, Any], *, baseline_l0_max_n: int) -> None:
        n_p = int(row["n_paths"])
        is_a = row["strategy_id"] == "A_FIXED"
        is_l0 = row["lifecycle_id"] == "L0"

        if is_a:
            row["delta_return_vs_A_same_lc_pp"] = 0.0
            row["delta_cvar95_vs_A_same_lc_pp"] = 0.0
            row["paired_beats_A_rate_pct"] = 0.0
            row["vs_A_comparison_status"] = "SELF_BASELINE"
            row["vs_A_comparison_unavailable_reason"] = None
        else:
            row["vs_A_comparison_status"] = "COMPUTED"
            row["vs_A_comparison_unavailable_reason"] = None

        row["candidate_n_paths"] = n_p
        if is_l0:
            row["baseline_n_paths"] = n_p
            row["matched_n_paths"] = n_p
            row["comparison_status"] = "SELF_BASELINE"
            row["comparison_unavailable_reason"] = None
            row["delta_return_vs_L0_pp"] = 0.0
            row["delta_cvar95_vs_L0_pp"] = 0.0
        elif n_p > baseline_l0_max_n:
            row["baseline_n_paths"] = baseline_l0_max_n
            row["matched_n_paths"] = 0
            row["comparison_status"] = "COMPARISON_UNAVAILABLE"
            row["comparison_unavailable_reason"] = "PAIRED_BASELINE_INSUFFICIENT"
            row["delta_return_vs_L0_pp"] = None
            row["delta_cvar95_vs_L0_pp"] = None
        else:
            row["baseline_n_paths"] = n_p
            row["matched_n_paths"] = n_p
            row["comparison_status"] = "COMPUTED"
            row["comparison_unavailable_reason"] = None

    for r in mc["screening"]:
        patch_row(r, baseline_l0_max_n=int(mc["screen_paths"]))
    for r in mc["execution_sensitivity"]:
        patch_row(r, baseline_l0_max_n=int(mc["screen_paths"]))
    for r in mc["expanded_key_scenarios"]:
        patch_row(r, baseline_l0_max_n=expand_n)
        if r["comparison_status"] == "COMPARISON_UNAVAILABLE":
            pseudo_zero_cells.append(
                {
                    "scenario": r["scenario"],
                    "lifecycle_id": r["lifecycle_id"],
                    "execution_profile": r["execution_profile"],
                    "strategy_id": r["strategy_id"],
                    "candidate_n_paths": r["candidate_n_paths"],
                    "baseline_n_paths": r["baseline_n_paths"],
                    "matched_n_paths": r["matched_n_paths"],
                    "comparison_status": r["comparison_status"],
                    "comparison_unavailable_reason": r["comparison_unavailable_reason"],
                }
            )

    # Also annotate mc["convergence"] explicitly for 1024-path L0 missingness
    exp_by_key = {
        (r["scenario"], r["lifecycle_id"], r["execution_profile"], r["strategy_id"], int(r["n_paths"])): r
        for r in mc["expanded_key_scenarios"]
    }
    for c in mc["convergence"]:
        n_p = int(c["n_paths"])
        if c["lifecycle_id"] == "L0":
            c["l0_comparison_status"] = "SELF_BASELINE"
            c["delta_cvar95_vs_L0_pp"] = 0.0
        elif n_p > expand_n:
            c["l0_comparison_status"] = "COMPARISON_UNAVAILABLE"
            c["delta_cvar95_vs_L0_pp"] = None
        else:
            c["l0_comparison_status"] = "COMPUTED"
            match = exp_by_key.get(
                (c["scenario"], c["lifecycle_id"], c["execution_profile"], c["strategy_id"], n_p)
            )
            c["delta_cvar95_vs_L0_pp"] = match["delta_cvar95_vs_L0_pp"] if match else None

    return pseudo_zero_cells


def _audit_b2_b5_coverage(exec_mat: list[dict[str, Any]]) -> dict[str, Any]:
    """Verify exact historical L1/L2 B2 vs B5 cell coverage and numerical equivalence."""
    by_key = {
        (r["funding_regime"], r["lifecycle_id"], r["execution_profile"], r["strategy_id"]): r
        for r in exec_mat
        if r["lifecycle_id"] in {"L1", "L2"}
    }
    covered: list[tuple[str, str, str]] = []
    mismatches: list[dict[str, Any]] = []
    for (reg, lc, prof, sid), r2 in sorted(by_key.items()):
        if sid != "B2":
            continue
        r5 = by_key.get((reg, lc, prof, "B5"))
        if r5 is None:
            continue
        covered.append((reg, lc, prof))
        for fld in (
            "oos18_mean_return_pct",
            "oos18_cvar95_pct",
            "oos18_total_fees_usdt",
            "oos18_total_funding_usdt",
        ):
            if abs(float(r2[fld]) - float(r5[fld])) > 1e-9:
                mismatches.append({"cell": (reg, lc, prof), "field": fld, "B2": r2[fld], "B5": r5[fld]})

    all_regimes = (
        "MAINNET_ACTUAL",
        "SWITCH_48H",
        "DEMO_HIGH_FUNDING_22.97BPS",
        "EX_FUNDING",
        "NORMAL_SYNTHETIC_0.125BPS",
    )
    all_possible = [
        (reg, lc, prof)
        for reg in all_regimes
        for lc in ("L1", "L2")
        for prof in ALL_EXECUTION_PROFILES
    ]
    covered_set = set(covered)
    uncovered = [c for c in all_possible if c not in covered_set]
    return {
        "historical_l1_l2_paired_cells_executed": len(covered),
        "historical_l1_l2_full_matrix_cells": len(all_possible),
        "uncovered_cells_count": len(uncovered),
        "all_covered_cells_equal_on_return_cvar_fees_funding": len(mismatches) == 0,
        "mismatches": mismatches,
        "covered_regimes_and_profiles": {
            reg: sorted({f"{lc}:{prof}" for r, lc, prof in covered if r == reg}) for reg in all_regimes
        },
        "uncovered_combinations": [f"{r}|{lc}|{p}" for r, lc, p in uncovered],
        "deterministic_counterexample_preserved": (
            "Under zero slippage with in-band open inventory before breakout, B5 funding exit "
            "triggers ahead of BREAKOUT through the single authoritative F11 limit exit; "
            "equivalence is scoped strictly to the 36 executed single-level 2-bps-slippage historical cells."
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Recompute minimal P8-R11 post-review remediation artifacts")
    parser.add_argument("--r04b-snapshot", type=Path, required=True)
    parser.add_argument("--cache-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, default=Path("docs/validation/p8-r11"))
    parser.add_argument("--workers", type=int, default=16)
    args = parser.parse_args()

    t_start = perf_counter()
    out_dir = args.output_dir
    study = json.loads((out_dir / "summary.json").read_text(encoding="utf-8"))

    # 1. P2-1: Fix MC missing L0 comparison semantics
    pseudo_zero_cells = _patch_mc_missingness(study["monte_carlo"])
    print(f"[P2-1] Fixed {len(pseudo_zero_cells)} MC pseudo-zero 1024-vs-512 L0 cells", flush=True)
    assert len(pseudo_zero_cells) == 8, f"Expected 8 pseudo-zero cells, got {len(pseudo_zero_cells)}"

    # 2. Load certified dataset & predeclared strategy configs
    all_bars, all_funding, _ = load_and_audit_r10_dataset(
        repo_root=REPO_ROOT,
        r04b_snapshot=args.r04b_snapshot,
        cache_dir=args.cache_dir,
        allow_network_supplement=False,
    )
    windows = build_nonoverlapping_weekly_windows(all_bars)
    validate_research_windows(all_bars, windows)
    init_sigma_ref = _compute_ref_vol_from_bars(all_bars, windows[9]["start_bar_index"])
    fold_specs: list[tuple[int, int, int, float]] = []
    fid, tr_end = 0, 9
    while tr_end < len(windows):
        te_end = min(len(windows), tr_end + 3)
        fid += 1
        f_sigma = _compute_ref_vol_from_bars(all_bars, windows[tr_end]["start_bar_index"])
        fold_specs.append((fid, tr_end, te_end, f_sigma))
        tr_end = te_end

    _, b_policies, c_policies = load_strategy_configs(REPO_ROOT)
    b_map = {p.candidate_id: p for p in b_policies}
    c_map = {p.candidate_id: p for p in c_policies}
    c5_frozen = replace(c_map["C5"], reference_vol_1h=init_sigma_ref)

    # 3. P2-3: Re-run full 338-run deterministic stress suite under event-level exit-peak monitor
    t_det = perf_counter()
    det_rows = run_r11_deterministic_stress(b_map["B2"], b_map["B5"], c5_frozen, max_workers=args.workers)
    det_max_peak = max(int(r["max_simultaneous_exit_orders"]) for r in det_rows)
    det_overlaps = sum(len(r.get("exit_overlap_violations", [])) for r in det_rows)
    assert all(r["reconciled_zero_residual"] for r in det_rows)
    assert det_max_peak == 1 and det_overlaps == 0
    print(
        f"[P2-3] Deterministic stress recomputed: {len(det_rows)} runs ({perf_counter()-t_det:.1f}s), "
        f"event_peak={det_max_peak}, overlaps={det_overlaps}",
        flush=True,
    )

    # 4. P2-2 + P2-3: Minimal historical OOS recompute for separated fixed-param vs reoptimized ablations
    t_hist = perf_counter()
    orig_folds = study["historical"]["wf_folds"]
    folds_by_combo: dict[tuple[str, str, str], dict[int, dict[str, Any]]] = {}
    for fd in orig_folds:
        folds_by_combo.setdefault(
            (fd["lifecycle_id"], fd["execution_profile"], fd["funding_regime"]), {}
        )[int(fd["fold"])] = dict(fd)

    # Extract frozen L0-selected candidates per (funding_regime, fold)
    l0_frozen_sel: dict[str, dict[int, tuple[str, str]]] = {}
    for reg in CORE_FUNDING_REGIMES:
        l0_frozen_sel[reg] = {
            f_num: (
                folds_by_combo[("L0", "IDEAL_LIMIT_FILL", reg)][f_num]["selected_b"],
                folds_by_combo[("L0", "IDEAL_LIMIT_FILL", reg)][f_num]["selected_c"],
            )
            for f_num, _, _, _ in fold_specs
        }

    wf_combos = [
        (lc, prof, reg)
        for reg in CORE_FUNDING_REGIMES
        for lc, prof in (
            ("L0", "IDEAL_LIMIT_FILL"),
            ("L1", "IDEAL_LIMIT_FILL"),
            ("L2", "IDEAL_LIMIT_FILL"),
            ("L1", "DELAYED_EXIT_FILL"),
            ("L2", "DELAYED_EXIT_FILL"),
        )
    ]

    hist_tasks: dict[tuple[Any, ...], tuple[Any, ...]] = {}
    seq = 0

    def enqueue_hist(w: dict[str, Any], arm_id: str, lc: str, prof: str, reg: str, s_tag: str, f_sigma: float) -> None:
        nonlocal seq
        cp = c_map.get(arm_id)
        bp = b_map.get(arm_id)
        if cp is not None:
            cp_eff = replace(cp, reference_vol_1h=f_sigma)
            eff_tag = s_tag if cp.extreme_vol_mult is not None else "init"
        else:
            cp_eff = None
            eff_tag = "none"
        key = (w["window_id"], arm_id, lc, prof, reg, eff_tag)
        if key in hist_tasks:
            return
        seq += 1
        hist_tasks[key] = (
            key,
            w,
            arm_id,
            lc,
            prof,
            reg,
            bp,
            cp_eff,
            all_funding,
            HISTORICAL_REPLAY_CAPITAL,
            D("1"),
            f"rev{seq:05d}",
        )

    for lc, prof, reg in wf_combos:
        for f_num, tr_end, te_end, f_sigma in fold_specs:
            te_wins = windows[tr_end:te_end]
            s_tag = "init" if f_num == 1 else f"f{f_num}"
            fd = folds_by_combo[(lc, prof, reg)][f_num]
            sel_b, sel_c = fd["selected_b"], fd["selected_c"]
            fr_b, fr_c = l0_frozen_sel[reg][f_num]
            for w in te_wins:
                for arm in ("A_FIXED", "B2", "B5", "C5", sel_b, sel_c, fr_b, fr_c):
                    enqueue_hist(w, arm, lc, prof, reg, s_tag, f_sigma)

    print(f"[P2-2/P2-3] Running {len(hist_tasks)} deduplicated historical OOS windows...", flush=True)
    hist_cache = dict(_run_pool(_hist_task_worker, list(hist_tasks.values()), max_workers=args.workers))
    hist_max_peak = max(int(r["max_simultaneous_exit_orders"]) for r in hist_cache.values())
    hist_overlaps = sum(len(r.get("exit_overlap_violations", [])) for r in hist_cache.values())
    assert all(r["reconciled_zero_residual"] for r in hist_cache.values())
    assert hist_max_peak <= 1 and hist_overlaps == 0
    print(
        f"[P2-2/P2-3] Historical OOS recompute done ({perf_counter()-t_hist:.1f}s), "
        f"event_peak={hist_max_peak}, overlaps={hist_overlaps}",
        flush=True,
    )

    def lookup_h(win_id: str, arm_id: str, lc: str, prof: str, reg: str, s_tag: str) -> dict[str, Any]:
        cp = c_map.get(arm_id)
        eff_tag = "none" if cp is None else (s_tag if cp.extreme_vol_mult is not None else "init")
        return hist_cache[(win_id, arm_id, lc, prof, reg, eff_tag)]

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
    wf_folds_new: list[dict[str, Any]] = []
    wf_oos_rows_new: list[dict[str, Any]] = []
    wf_summary_new: list[dict[str, Any]] = []

    for lc, prof, reg in wf_combos:
        combo_key = f"{lc}__{prof}__{reg}"
        oos_a, oos_b, oos_c, oos_bf, oos_cf, oos_b2, oos_b5, oos_c5, oos_cash = [], [], [], [], [], [], [], [], []
        for f_num, tr_end, te_end, f_sigma in fold_specs:
            te_wins = windows[tr_end:te_end]
            s_tag = "init" if f_num == 1 else f"f{f_num}"
            fd = dict(folds_by_combo[(lc, prof, reg)][f_num])
            sel_b, sel_c = fd["selected_b"], fd["selected_c"]
            fr_b, fr_c = l0_frozen_sel[reg][f_num]

            fold_a = [lookup_h(w["window_id"], "A_FIXED", lc, prof, reg, s_tag) for w in te_wins]
            fold_b = [lookup_h(w["window_id"], sel_b, lc, prof, reg, s_tag) for w in te_wins]
            fold_c = [lookup_h(w["window_id"], sel_c, lc, prof, reg, s_tag) for w in te_wins]
            fold_bf = [lookup_h(w["window_id"], fr_b, lc, prof, reg, s_tag) for w in te_wins]
            fold_cf = [lookup_h(w["window_id"], fr_c, lc, prof, reg, s_tag) for w in te_wins]
            fold_b2 = [lookup_h(w["window_id"], "B2", lc, prof, reg, s_tag) for w in te_wins]
            fold_b5 = [lookup_h(w["window_id"], "B5", lc, prof, reg, s_tag) for w in te_wins]
            fold_c5 = [lookup_h(w["window_id"], "C5", lc, prof, reg, s_tag) for w in te_wins]
            fold_cash = [
                run_unified_lifecycle(
                    w["bars"],
                    strategy_id="CASH",
                    lifecycle_id=lc,
                    execution_profile_name=prof,
                    funding_regime=reg,
                    strategy_cap_usdt=HISTORICAL_REPLAY_CAPITAL,
                    historical_funding_events=all_funding,
                )
                for w in te_wins
            ]

            for ra, rb, rc, rbf, rcf, rca in zip(fold_a, fold_b, fold_c, fold_bf, fold_cf, fold_cash):
                oos_a.append(dict(ra, fold=f_num, wf_role="A_FIXED", selected_id="A_FIXED"))
                oos_b.append(dict(rb, fold=f_num, wf_role="B_WF", selected_id=sel_b))
                oos_c.append(dict(rc, fold=f_num, wf_role="C_WF", selected_id=sel_c))
                oos_bf.append(dict(rbf, fold=f_num, wf_role="B_FROZEN_L0", selected_id=fr_b))
                oos_cf.append(dict(rcf, fold=f_num, wf_role="C_FROZEN_L0", selected_id=fr_c))
                oos_cash.append(dict(rca, fold=f_num, wf_role="CASH", selected_id="CASH", window_id=ra["window_id"]))
            oos_b2.extend(fold_b2)
            oos_b5.extend(fold_b5)
            oos_c5.extend(fold_c5)

            fd["frozen_l0_selected_b"] = fr_b
            fd["frozen_l0_selected_c"] = fr_c
            fd["oos_b_frozen_l0_return_pct"] = summarize_unified_windows(fold_bf)["mean_net_return_pct"]
            fd["oos_c_frozen_l0_return_pct"] = summarize_unified_windows(fold_cf)["mean_net_return_pct"]
            wf_folds_new.append(fd)

        wf_oos_rows_new.extend(oos_a + oos_b + oos_c + oos_bf + oos_cf + oos_cash)
        oos_summaries = {
            "CASH": summarize_unified_windows(oos_cash),
            "A_FIXED": summarize_unified_windows(oos_a),
            "B_WF": summarize_unified_windows(oos_b),
            "B_FROZEN_L0": summarize_unified_windows(oos_bf),
            "B2": summarize_unified_windows(oos_b2),
            "B5": summarize_unified_windows(oos_b5),
            "C_WF": summarize_unified_windows(oos_c),
            "C_FROZEN_L0": summarize_unified_windows(oos_cf),
            "C5": summarize_unified_windows(oos_c5),
        }
        for role_name, s_dict in oos_summaries.items():
            sel_mode, bind_note = role_selection_meta[role_name]
            wf_summary_new.append(
                {
                    "lifecycle_id": lc,
                    "model_label": LIFECYCLE_LABELS[lc],
                    "execution_profile": prof,
                    "funding_regime": reg,
                    "strategy_role": role_name,
                    "selection_mode": sel_mode,
                    "parameter_binding_note": bind_note,
                    **s_dict,
                }
            )
        old_idx = study["historical"]["wf_results_index"][combo_key]
        wf_results[combo_key] = {
            "lifecycle_id": lc,
            "execution_profile": prof,
            "funding_regime": reg,
            "oos_rows": {
                "CASH": oos_cash,
                "A_FIXED": oos_a,
                "B_WF": oos_b,
                "B_FROZEN_L0": oos_bf,
                "B2": oos_b2,
                "B5": oos_b5,
                "C_WF": oos_c,
                "C_FROZEN_L0": oos_cf,
                "C5": oos_c5,
            },
            "oos_summary": oos_summaries,
            "is_oos_spearman_rho": old_idx["is_oos_spearman_rho"],
            "is_oos_spearman_p": old_idx["is_oos_spearman_p"],
        }

    # Verify exact parity with frozen R10 L0 baseline
    r10_rec = _reconcile_l0_against_frozen_r10(REPO_ROOT, wf_results)
    assert len(r10_rec) == 9
    assert all(abs(r["return_diff_pp"]) < 1e-4 for r in r10_rec), f"L0 parity drift: {r10_rec}"

    separated_ablations = build_separated_paired_ablations(wf_results)
    print(f"[P2-2] Built {len(separated_ablations)} separated paired ablation rows", flush=True)

    # 5. P2-3: Representative MC verification batch under event-level exit-peak monitor
    t_mc = perf_counter()
    mc_ver_n = 64
    mc_paths, _, start_iso = _generate_mc_paths(
        args.r04b_snapshot,
        anchor_price=DEMO_ANCHOR_PRICE,
        n_paths_map={g: mc_ver_n for g in MC_KEY_SCENARIOS},
        n_steps=168,
        seed=int(study["monte_carlo"]["primary_seed"]),
    )
    mc_tasks: list[tuple[Any, ...]] = []
    m_seq = 0
    arm_pol = {"A_FIXED": (None, None), "B5": (b_map["B5"], None), "C5": (None, c5_frozen)}
    for scen in MC_KEY_SCENARIOS:
        _, vol_1h, arr = mc_paths[scen]
        wc = _make_neutral_warmup(DEMO_ANCHOR_PRICE, vol_1h, seed=1010)
        for lc, prof in (("L1", "IDEAL_LIMIT_FILL"), ("L2", "IDEAL_LIMIT_FILL"), ("L2", "DELAYED_EXIT_FILL")):
            for arm_id in ("A_FIXED", "B5", "C5"):
                bp, cp = arm_pol[arm_id]
                for i in range(mc_ver_n):
                    m_seq += 1
                    key = (int(study["monte_carlo"]["primary_seed"]), scen, i, arm_id, lc, prof, "SWITCH_48H")
                    mc_tasks.append(
                        (
                            key,
                            arr[i],
                            start_iso,
                            arm_id,
                            lc,
                            prof,
                            "SWITCH_48H",
                            bp,
                            cp,
                            wc if cp is not None else None,
                            DEMO_ANCHOR_CAPITAL,
                            f"ev{m_seq:05d}",
                        )
                    )
    mc_ver_res = dict(_run_pool(_mc_task_worker, mc_tasks, max_workers=args.workers))
    mc_ver_peak = max(int(r["max_simultaneous_exit_orders"]) for r in mc_ver_res.values())
    mc_ver_overlaps = sum(len(r.get("exit_overlap_violations", [])) for r in mc_ver_res.values())
    assert all(r["reconciled_zero_residual"] for r in mc_ver_res.values())
    assert mc_ver_peak == 1 and mc_ver_overlaps == 0
    print(
        f"[P2-3] Representative MC event-level verification: {len(mc_ver_res)} paths ({perf_counter()-t_mc:.1f}s), "
        f"event_peak={mc_ver_peak}, overlaps={mc_ver_overlaps}",
        flush=True,
    )

    # 6. P3-1: Audit exact 36 historical L1/L2 B2/B5 paired cells
    b2_b5_audit = _audit_b2_b5_coverage(study["historical"]["execution_realism_matrix"])
    assert b2_b5_audit["historical_l1_l2_paired_cells_executed"] == 36
    assert b2_b5_audit["all_covered_cells_equal_on_return_cvar_fees_funding"] is True

    # 7. Assemble updated study, honest split engineering scope, conclusions, and S02 handoff
    total_ev_runs = len(det_rows) + len(hist_cache) + len(mc_ver_res)
    overall_ev_peak = max(det_max_peak, hist_max_peak, mc_ver_peak)
    overall_ev_overlaps = det_overlaps + hist_overlaps + mc_ver_overlaps
    exit_peak_verification = {
        "monitoring_method": EXIT_PEAK_MONITORING_METHOD,
        "event_level_verified_runs": total_ev_runs,
        "deterministic_runs": len(det_rows),
        "historical_runs": len(hist_cache),
        "mc_runs": len(mc_ver_res),
        "event_level_max_simultaneous_exit_orders": overall_ev_peak,
        "event_level_overlap_violations": overall_ev_overlaps,
        "legacy_bar_end_only_runs": int(study["ledger_verification"]["total_unique_runs"]),
        "scope_summary": (
            f"事件级权威生命周期峰值监测已验证 {total_ev_runs} 次完整运行"
            f"（全部 {len(det_rows)} 次确定性压力 + {len(hist_cache)} 次历史 OOS 机制补算窗 + {len(mc_ver_res)} 条 MC 代表路径）"
            "及全部单元/负向重叠注入测试"
        ),
    }

    study["deterministic_stress"] = det_rows
    study["historical"]["wf_folds"] = wf_folds_new
    study["historical"]["wf_summary"] = wf_summary_new
    study["historical"]["paired_ablations"] = separated_ablations
    study["historical"]["l0_vs_r10_reconciliation"] = r10_rec
    study["historical"]["wf_results_index"] = {
        k: {
            "lifecycle_id": v["lifecycle_id"],
            "execution_profile": v["execution_profile"],
            "funding_regime": v["funding_regime"],
            "oos_summary": v["oos_summary"],
            "is_oos_spearman_rho": v["is_oos_spearman_rho"],
            "is_oos_spearman_p": v["is_oos_spearman_p"],
        }
        for k, v in wf_results.items()
    }
    study["historical"]["b2_b5_equivalence_audit"] = b2_b5_audit

    conclusions = derive_r11_conclusions(
        study["historical"],
        det_rows,
        study["monte_carlo"],
        exit_peak_verification=exit_peak_verification,
    )
    study["conclusions"] = conclusions
    study["ledger_verification"]["event_level_exit_peak_verification"] = exit_peak_verification
    study["ledger_verification"]["single_authoritative_exit_order_verified"] = conclusions["invariants"][
        "single_authoritative_exit_order_verified"
    ]

    synced_main = subprocess.check_output(
        ["git", "rev-parse", "--short", "origin/main"], cwd=REPO_ROOT, text=True
    ).strip()
    recompute_elapsed = round(perf_counter() - t_start, 2)
    study["independent_review_remediation"] = {
        "synced_main_commit": synced_main,
        "mc_pseudo_zero_cells_fixed": len(pseudo_zero_cells),
        "mc_pseudo_zero_cells": pseudo_zero_cells,
        "event_level_exit_peak_runs": total_ev_runs,
        "det_recomputed_runs": len(det_rows),
        "hist_recomputed_runs": len(hist_cache),
        "mc_recomputed_runs": len(mc_ver_res),
        "b2_b5_covered_cells": b2_b5_audit["historical_l1_l2_paired_cells_executed"],
        "b2_b5_uncovered_cells": b2_b5_audit["uncovered_cells_count"],
        "recompute_elapsed_seconds": recompute_elapsed,
    }

    s02_handoff = build_s02_handoff_contract(
        repo_root=REPO_ROOT,
        conclusions=conclusions,
        historical_oos_summary=study["historical"]["wf_results_index"],
        mc_key_summary=study["monte_carlo"]["expanded_key_scenarios"],
        data_audit=study["data_audit"],
        paired_ablations=separated_ablations,
        engineering_verification_scope={
            "fifo_zero_residual_verified_runs": int(study["ledger_verification"]["total_unique_runs"]) + total_ev_runs,
            "fifo_zero_residual_all_passed": conclusions["invariants"]["all_zero_fifo_residual"],
            **exit_peak_verification,
        },
        b2_b5_equivalence_boundary=b2_b5_audit,
    )
    study["s02_handoff"] = s02_handoff

    # 8. Write all updated JSON, CSV, and REPORT.md artifacts
    (out_dir / "summary.json").write_text(
        json.dumps(study, indent=2, sort_keys=True, ensure_ascii=False, default=str) + "\n",
        encoding="utf-8",
    )
    (out_dir / "s02_handoff.json").write_text(
        json.dumps(s02_handoff, indent=2, sort_keys=True, ensure_ascii=False, default=str) + "\n",
        encoding="utf-8",
    )
    _dump_csv(out_dir / "l0_vs_r10_reconciliation.csv", r10_rec)
    _dump_csv(out_dir / "historical_wf_summary.csv", wf_summary_new)
    _dump_csv(out_dir / "historical_wf_folds.csv", wf_folds_new)
    _dump_csv(out_dir / "historical_wf_oos_windows.csv", wf_oos_rows_new)
    _dump_csv(out_dir / "paired_ablations.csv", separated_ablations)
    _dump_csv(out_dir / "deterministic_stress.csv", det_rows)
    _dump_csv(out_dir / "mc_screening.csv", study["monte_carlo"]["screening"])
    _dump_csv(out_dir / "mc_execution_sensitivity.csv", study["monte_carlo"]["execution_sensitivity"])
    _dump_csv(out_dir / "mc_expanded_key_scenarios.csv", study["monte_carlo"]["expanded_key_scenarios"])
    _dump_csv(out_dir / "mc_convergence.csv", study["monte_carlo"]["convergence"])

    report_md = render_p8_r11_report(study)
    (out_dir / "REPORT.md").write_text(report_md, encoding="utf-8")

    # Verify strict equality between embedded and standalone S02 handoff
    reloaded_sum = json.loads((out_dir / "summary.json").read_text(encoding="utf-8"))
    reloaded_h = json.loads((out_dir / "s02_handoff.json").read_text(encoding="utf-8"))
    assert reloaded_sum["s02_handoff"] == reloaded_h, "Embedded s02_handoff differs from standalone file!"

    print(
        f"[DONE] P8-R11 post-review remediation completed in {recompute_elapsed:.1f}s "
        f"(REPORT.md: {len(report_md)} chars, {report_md.count(chr(10))} lines)",
        flush=True,
    )


if __name__ == "__main__":
    main()
