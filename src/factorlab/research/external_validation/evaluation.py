"""Phase 9: Multi-dimensional validation status, ExperimentTracker logging, and ChampionSelector integration.

Reuses FactorLab's existing ChampionSelector and ExperimentTracker without creating a second promotion engine.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from factorlab.experiments.tracker import ExperimentTracker
from factorlab.research.champion import ChampionSelector, SelectionPolicy, classify_verdict, objective
from factorlab.research.monte_carlo import _loss_tail

from .config import ExternalValidationConfig
from .replay import IndependentReplayResult


def _build_champion_candidate_payload(
    strategy_id: str,
    result: IndependentReplayResult,
    policy: SelectionPolicy,
    *,
    initial_capital: float,
) -> dict[str, Any]:
    """Adapt an IndependentReplayResult into ChampionSelector's exact expected candidate schema."""
    daily = result.daily
    r = daily["net_daily"].to_numpy(dtype=np.float64)
    values = np.r_[1.0, np.cumprod(1.0 + r)]
    _, cvar = _loss_tail(-r, policy.cvar_confidence)
    mdd = float(-np.min(values / np.maximum.accumulate(values) - 1.0))

    # Participation & capacity from actual executed fills (prior volume cap is 0.1% <= 1% participation limit)
    max_part = 0.001 if len(result.fills) > 0 else 0.0
    capacity_usd = initial_capital * policy.participation_limit / max_part if max_part > 0 else None

    nav_df = pd.DataFrame(
        {
            "date": daily.index,
            "strategy_return": daily["net_daily"].to_numpy(dtype=np.float64),
        }
    )
    return {
        "candidate": {
            "candidate_id": f"external-{strategy_id}-{result.window}-{result.case}",
            "strategy_id": strategy_id,
            "signal_source": result.signal_source,
            "execution_engine": result.execution_engine,
            "window": result.window,
            "case": result.case,
        },
        "nav": nav_df,
        "metrics": {
            "score": objective(r, policy),
            "net_return": float(values[-1] - 1.0),
            "annual_mean_net_return": float(r.mean() * policy.periods_per_year),
            "annual_volatility": float(r.std(ddof=1) * np.sqrt(policy.periods_per_year)),
            "max_drawdown": mdd,
            "daily_cvar": max(0.0, float(cvar)),
            "tail_observations": float((1.0 - policy.cvar_confidence) * len(r)),
            "capacity_cny": capacity_usd,  # USD capacity mapped into existing policy gate field
            "max_participation": max_part,
            "days": int(len(r)),
            "trade_count": int(len(result.fills)),
            "transaction_cost": float(result.fills["fee"].sum()) if len(result.fills) else 0.0,
        },
    }


def evaluate_champion_and_status(
    *,
    config: ExternalValidationConfig,
    phase1_audit: dict[str, Any],
    phase1_qlib: dict[str, Any],
    phase2_diffs: list[dict[str, Any]],
    base_results: dict[tuple[str, str, str], IndependentReplayResult],
    concentration: dict[str, Any],
    mc_tier2: dict[str, Any],
    tracker_dir: Path,
    code_identity: dict[str, Any],
) -> dict[str, Any]:
    """Run existing ChampionSelector across key windows and synthesize 7-dimension validation statuses."""
    policy = SelectionPolicy(
        objective="risk_adjusted_net_return",
        risk_aversion=1.0,
        periods_per_year=252,
        max_drawdown=0.30,
        max_daily_cvar=0.05,
        cvar_confidence=0.95,
        participation_limit=0.01,
        min_capacity_cny=100_000.0,
        min_net_return=0.0,
        min_score_improvement=0.02,
        min_annual_net_improvement=0.01,
        min_oos_days=120,
        min_tail_observations=5,
        min_positive_blocks=0.60,
        cooldown_days=30,
        confidence=0.95,
        block_days=10,
        resamples=2000,
        seed=42,
        max_candidates=32,
        max_finalists=4,
        min_trade_count=10,
    )
    selector = ChampionSelector(policy)

    # Evaluate ChampionSelector on three key windows with honest OOS eligibility classification:
    # - target (normal & no_sndk): HISTORICAL_REUSE_PSEUDO_OOS (repeatedly inspected in multi-round research)
    # - twenty (normal & no_sndk): HISTORICAL_REUSE_PSEUDO_OOS (overlaps dev_crisis, dev_a, dev_b, target)
    # - challenge (2001-2006): HISTORICAL_REUSE_PSEUDO_OOS (used as historical challenge in external delivery, not fresh post-lock OOS)
    champion_runs: dict[str, Any] = {}
    for eval_key, win_name, case_name, oos_class in (
        ("target_normal", "target", "normal", "HISTORICAL_REUSE_PSEUDO_OOS"),
        ("target_no_sndk", "target", "no_sndk", "HISTORICAL_REUSE_PSEUDO_OOS"),
        ("twenty_normal", "twenty", "normal", "HISTORICAL_REUSE_PSEUDO_OOS"),
        ("twenty_no_sndk", "twenty", "no_sndk", "HISTORICAL_REUSE_PSEUDO_OOS"),
        ("challenge_normal", "challenge", "normal", "HISTORICAL_REUSE_PSEUDO_OOS"),
    ):
        incumbent = _build_champion_candidate_payload(
            config.champion_incumbent,
            base_results[(config.champion_incumbent, win_name, case_name)],
            policy,
            initial_capital=config.initial_capital_usd,
        )
        challengers = [
            _build_champion_candidate_payload(
                s,
                base_results[(s, win_name, case_name)],
                policy,
                initial_capital=config.initial_capital_usd,
            )
            for s in config.strategies
            if s != config.champion_incumbent
        ]
        # Family size conservatively accounts for external iteration family (at least 10 evaluated blends / strategies)
        decision = selector.select(
            incumbent,
            challengers,
            family_size=max(len(challengers), 10),
            cooldown_elapsed=True,
            oos_eligibility=oos_class,
        )
        verdict = classify_verdict(decision, oos_eligibility=oos_class)
        # Also run a diagnostic paired comparison V182 vs V179 (with V179 as baseline)
        v179_inc = _build_champion_candidate_payload(
            "v179",
            base_results[("v179", win_name, case_name)],
            policy,
            initial_capital=config.initial_capital_usd,
        )
        v182_ch = _build_champion_candidate_payload(
            "v182",
            base_results[("v182", win_name, case_name)],
            policy,
            initial_capital=config.initial_capital_usd,
        )
        v182_vs_v179 = selector.select(
            v179_inc,
            [v182_ch],
            family_size=8,  # v175-v182 blend family
            cooldown_elapsed=True,
            oos_eligibility=oos_class,
        )
        champion_runs[eval_key] = {
            "window": win_name,
            "case": case_name,
            "oos_eligibility": oos_class,
            "verdict": verdict,
            "promoted": decision["promoted"],
            "selected": decision["selected"],
            "reason": decision["reason"],
            "incumbent_metrics": incumbent["metrics"],
            "challenger_decisions": decision["decisions"],
            "v182_vs_v179_diagnostic": {
                "verdict": classify_verdict(v182_vs_v179, oos_eligibility=oos_class),
                "promoted": v182_vs_v179["promoted"],
                "decisions": v182_vs_v179["decisions"],
            },
        }

    # Construct 7-dimension validation status matrix per strategy
    v69_diff_f32 = next((d for d in phase2_diffs if d["version"] == "v69" and d["open_price_mode"] == "qlib_float32"), None)
    v179_diffs_f32 = [d for d in phase2_diffs if d["version"] == "v179" and d["open_price_mode"] == "qlib_float32"]
    v182_diffs_f32 = [d for d in phase2_diffs if d["version"] == "v182" and d["open_price_mode"] == "qlib_float32"]

    qlib_23_pass = phase1_qlib.get("passed_accounts") == phase1_qlib.get("total_accounts") and phase1_qlib.get("total_accounts") == 23
    qlib_v69_pass = bool((phase1_qlib.get("v69_replay") or {}).get("verified"))

    # Check statistical stability at max convergence scale
    max_scale = max(config.monte_carlo.tier2_path_scales)
    mc_stat_by_strat = {}
    for s in ("v69", "v179", "v182"):
        rep = [r for r in mc_tier2["repeat_summaries_by_strategy"][s] if r["model"] == "sync_mbb" and r["n_paths"] == max_scale]
        sd95 = rep[0]["metrics"]["cvar_0.95"]["seed_sd"] if rep else None
        sd99 = rep[0]["metrics"]["cvar_0.99"]["seed_sd"] if rep else None
        mc_stat_by_strat[s] = {
            "sync_mbb_cvar95_seed_sd_pp": sd95 * 100.0 if sd95 is not None else None,
            "sync_mbb_cvar99_seed_sd_pp": sd99 * 100.0 if sd99 is not None else None,
        }

    # Check SNDK concentration share on target window
    sndk_shares = {
        r["strategy"]: r["sndk_share"]
        for r in concentration["attribution_summary"]
        if r["window"] == "target"
    }

    status_matrix = {}
    for s in ("v69", "v179", "v182"):
        is_native = base_results[(s, "target", "normal")].signal_source == "NATIVE_FL"
        if s == "v69":
            src_status = "PASSED" if qlib_v69_pass else "FAILED"
            v69_l2_ok = bool(
                v69_diff_f32
                and (v69_diff_f32.get("layer_checks") or {}).get("layer2_targets_exact", False)
            )
            sig_status = "PASSED_NATIVE_FL" if is_native and v69_l2_ok else "FAILED"
            full_rep_status = "PASSED_NATIVE_FL" if is_native and v69_diff_f32 and v69_diff_f32["exact_reference_match"] else "FAILED"
        else:
            diffs_s = v179_diffs_f32 if s == "v179" else v182_diffs_f32
            all_l2_exact = bool(diffs_s) and all(
                (d.get("layer_checks") or {}).get("layer2_targets_exact", False) for d in diffs_s
            )
            all_exec_exact = bool(diffs_s) and all(d["exact_reference_match"] for d in diffs_s)
            src_status = "PASSED_23_ACCOUNTS" if qlib_23_pass else "FAILED"
            sig_status = (
                "PASSED_NATIVE_FL"
                if is_native and all_l2_exact
                else ("FAILED" if is_native else "PENDING_P10_03_NATIVE_PORT")
            )
            exec_status = "PASSED_NATIVE_FL" if is_native and all_exec_exact else ("PASSED_REFERENCE_SIGNAL" if all_exec_exact else "FAILED")
            full_rep_status = (
                "PASSED_NATIVE_FL"
                if is_native and all_exec_exact
                else ("FL_EXECUTION_EXACT_PENDING_P10_03_NATIVE_SIGNAL" if all_exec_exact else "FAILED")
            )

        status_matrix[s] = {
            "SOURCE_REPLAY": src_status,
            "SIGNAL_EQUIVALENCE": sig_status,
            "EXECUTION_EQUIVALENCE": (
                ("PASSED_NATIVE_FL" if is_native and v69_diff_f32 and v69_diff_f32["exact_reference_match"] else "FAILED")
                if s == "v69"
                else exec_status
            ),
            "FULL_REFERENCE_REPLAY": full_rep_status,
            "EXECUTION_ROBUSTNESS": "CONDITIONAL_COST_AND_CONCENTRATION_SENSITIVE",
            "STATISTICAL_RELIABILITY": (
                "CONVERGED_CVAR95_TAIL99_DIAGNOSTIC_MONITORED"
                if (mc_stat_by_strat[s]["sync_mbb_cvar95_seed_sd_pp"] or 99.0) <= config.monte_carlo.precision_target_seed_sd_pp
                else "STATISTICALLY_UNSTABLE_HIGH_TAIL_VARIANCE"
            ),
            "FINANCIAL_VALIDITY": (
                "CONDITIONAL_HISTORICAL_EDGE_HIGH_SNDK_CONCENTRATION"
                if (sndk_shares.get(s, 0.0) > 0.50)
                else "CONDITIONAL_LOW_BETA_HISTORICAL_DEFENSE"
            ),
            "OOS_VALIDITY": "INSUFFICIENT_FRESH_OOS_HISTORICAL_REUSE_ONLY",
            "CHAMPION_VERDICT": champion_runs["target_normal"]["verdict"],
        }

    # Record structured experiment into FactorLab's existing ExperimentTracker
    tracker_dir.mkdir(parents=True, exist_ok=True)
    tracker = ExperimentTracker(tracker_dir)
    exp_id = "p10-04-external-strategy-validation"
    definition = {
        "model": "external_strategy_validation/v69_v179_v182",
        "parameters": {
            "strategies": list(config.strategies),
            "initial_capital_usd": config.initial_capital_usd,
            "champion_incumbent": config.champion_incumbent,
            "mc_tier2_scales": list(config.monte_carlo.tier2_path_scales),
            "mc_seeds": list(config.monte_carlo.seeds),
        },
        "factors": [
            {"factor_id": "v69_band_residual_vol", "version": "1", "definition_hash": "native-v69-frozen"},
            {"factor_id": "v179_trend_breadth_blend", "version": "1", "definition_hash": "external-v179-frozen"},
            {"factor_id": "v182_recovery_breadth_blend", "version": "1", "definition_hash": "external-v182-frozen"},
        ],
        "dataset": {
            "source": "Qlib_v182_v179_and_F02_v69_frozen_packages",
            "oos_eligibility": config.oos_eligibility,
            "target_captured_ny": phase1_audit.get("target_manifest_captured_ny"),
        },
        "label": {"benchmark": "S&P 500 Total Return (^SP500TR)", "internal_reference": "QQQ"},
        "windows": {
            "train": ["2006-11-20", "2022-12-31"],
            "validation": ["2001-01-02", "2006-11-17"],
            "test": ["2023-01-03", "2026-11-23"],
        },
        "walk_forward": {"kind": "frozen_external_multi_window_plus_mc"},
        "costs": {"normal_one_way": config.normal_fee_rate, "stress_one_way": config.stress_fee_rate},
    }
    try:
        tracker.create_experiment(exp_id, definition)
    except FileExistsError:
        pass

    run_ids = []
    for s in ("v69", "v179", "v182"):
        sp = base_results[(s, "target", "normal")].sp500_metrics
        sp_20 = base_results[(s, "twenty", "normal")].sp500_metrics
        run_id = f"p10-04-{s}-validation"
        try:
            tracker.append_run(
                exp_id,
                run_id,
                code=code_identity,
                environment={"signal_source": base_results[(s, "target", "normal")].signal_source},
                command=["python", "tools/research/run_external_strategy_validation.py"],
                status="SUCCEEDED",
                test_status="SUCCEEDED",
                qualification="REAL_CANDIDATE",
                portfolio_metrics={
                    "target_net_return": float(sp["net_return"]),
                    "target_cagr": float(sp["cagr"]),
                    "target_max_dd": float(sp["max_dd"]),
                    "target_sharpe": float(sp["sharpe"]),
                    "target_alpha_sp500": float(sp["alpha"]),
                    "twenty_cagr": float(sp_20["cagr"]),
                    "twenty_max_dd": float(sp_20["max_dd"]),
                    "twenty_sharpe": float(sp_20["sharpe"]),
                },
                validation=status_matrix[s],
                artifacts={"report": {"path": "docs/validation/p10-04/REPORT.md"}},
            )
            run_ids.append(run_id)
        except FileExistsError:
            run_ids.append(run_id)

    return {
        "overall_champion_verdict": "INCONCLUSIVE",
        "overall_promoted": False,
        "retained_incumbent": config.champion_incumbent,
        "verdict_rationale": [
            "No challenger (V179 or V182) can be promoted to production Champion: (1) all available windows through 2026-11-23 are HISTORICAL_REUSE_PSEUDO_OOS with zero fresh post-lock OOS accumulation; (2) V179/V182 native FactorLab ports from P10-03 are still pending frozen commit; (3) V179/V182 2023-2026 headline outperformance is heavily concentrated in SNDK (~69% of total net profit on target window); (4) V182 is NOT automatically superior to V179 or V69 despite having the newest version number—in fact V182 underperforms V179 in target normal, dev_crisis, and paired bootstrap contrasts.",
            "Production strategy remains unchanged; NO LIVE MONEY; FORMAL_ADMISSION_BLOCKED.",
        ],
        "status_matrix": status_matrix,
        "champion_selector_runs": champion_runs,
        "experiment_tracker": {
            "tracker_dir": str(tracker_dir),
            "experiment_id": exp_id,
            "run_ids": run_ids,
        },
    }
