"""Master orchestration pipeline for FactorLab external strategy independent validation (P10-04)."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
from time import perf_counter
from typing import Any

import pandas as pd

from .audit import audit_market_fixtures, run_isolated_qlib_reference_replays, verify_v179_v182_package
from .config import ExternalValidationConfig, save_validation_config
from .evaluation import evaluate_champion_and_status
from .financial import (
    run_complete_strategy_comparison,
    run_concentration_and_ablation,
    run_execution_robustness_suite,
    run_parameter_sensitivity_and_overfitting,
    run_regime_decomposition,
)
from .monte_carlo import (
    prepare_calibration_dataset,
    run_tier1_fast_portfolio_mc,
    run_tier2_dynamic_strategy_mc,
)
from .replay import (
    FactorLabIndependentBacktester,
    build_signal_provider,
    compare_against_reference,
    load_market_panel,
)
from .shared_portfolio import run_shared_account_study


def _git_code_identity(repo_dir: Path) -> dict[str, Any]:
    try:
        commit = subprocess.check_output(["git", "-C", str(repo_dir), "rev-parse", "HEAD"], text=True).strip()
        tree = subprocess.check_output(["git", "-C", str(repo_dir), "rev-parse", "HEAD^{tree}"], text=True).strip()
        return {"commit": commit, "tree": tree}
    except Exception:
        return {"commit": None, "tree": None}


def _write_json(path: Path, payload: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8")
    return path


def run_external_validation_pipeline(
    config: ExternalValidationConfig,
    output_dir: Path,
    *,
    skip_qlib: bool = False,
    skip_mc: bool = False,
) -> dict[str, Any]:
    """Execute Phases 1-6 & 9 end-to-end and persist reproducible JSON/CSV evidence artifacts."""
    started = perf_counter()
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    save_validation_config(config, out / "validation_config_frozen.json")

    v182_dir = Path(config.packages.v179_v182_dir)
    v69_dir = Path(config.packages.v69_dir)
    wmap = config.window_map()

    # ------------------------------------------------------------------
    # Phase 1: Source Data Audit & Isolated Qlib 23-Account + V69 Replay
    # ------------------------------------------------------------------
    p1_pkg = verify_v179_v182_package(v182_dir)
    p1_fixtures = audit_market_fixtures(v182_dir, v69_dir)
    if skip_qlib:
        p1_qlib = {"status": "SKIPPED_BY_FLAG", "passed_accounts": 0, "total_accounts": 23, "accounts": [], "v69_replay": None}
    else:
        p1_qlib = run_isolated_qlib_reference_replays(config, out / "qlib_replays", max_workers=6)

    phase1 = {
        "package_verification": p1_pkg,
        "market_fixtures_audit": p1_fixtures,
        "isolated_qlib_replay": p1_qlib,
    }
    _write_json(out / "phase1_source_audit.json", phase1)
    if p1_qlib.get("accounts"):
        pd.DataFrame(p1_qlib["accounts"]).to_csv(out / "qlib_23_account_replay_matrix.csv", index=False)

    # ------------------------------------------------------------------
    # Phase 2: FactorLab Independent Historical Replay & 7-Layer Diff
    # ------------------------------------------------------------------
    markets = {w.name: load_market_panel(v182_dir, w, tickers=config.tickers) for w in config.windows}
    archived_accounts = p1_pkg["archived_accounts"]

    phase2_diffs: list[dict[str, Any]] = []
    # V69 native FL replay against original V69 fixture (both qlib_float32 and float64)
    for mode in ("qlib_float32", "float64"):
        sig_v69 = build_signal_provider("v69", markets["target"], package_dir=v182_dir, omit_sndk=False)
        bt_v69 = FactorLabIndependentBacktester(
            initial_capital=config.initial_capital_usd,
            fee_rate=config.normal_fee_rate,
            open_price_mode=mode,
            gross_cap=config.v69_reference_cap,
            tickers=config.tickers,
        )
        res_v69 = bt_v69.run(sig_v69, markets["target"], window_name="target", start=wmap["target"].start, end=wmap["target"].end, case="normal")
        phase2_diffs.append(compare_against_reference(res_v69, v69_dir / "fixture" / "v69_target_normal", is_v69_fixture=True))

    # All 23 V179/V182 archived accounts through FL independent backtester (both qlib_float32 and float64)
    # Cache signal providers per (version, window, omit_sndk) so we don't recompute Ridge needlessly
    sig_cache: dict[tuple[str, str, bool], Any] = {}
    for spec in archived_accounts:
        ver, win, case = spec.split("/")
        omit = win.startswith("dev") or case == "no_sndk"
        fee = config.stress_fee_rate if case == "stress" else config.normal_fee_rate
        skey = (ver, win, omit)
        if skey not in sig_cache:
            sig_cache[skey] = build_signal_provider(ver, markets[win], package_dir=v182_dir, omit_sndk=omit)
        for mode in ("qlib_float32", "float64"):
            bt = FactorLabIndependentBacktester(
                initial_capital=config.initial_capital_usd,
                fee_rate=fee,
                open_price_mode=mode,
                gross_cap=config.gross_cap,
                tickers=config.tickers,
            )
            res = bt.run(sig_cache[skey], markets[win], window_name=win, start=wmap[win].start, end=wmap[win].end, case=case)
            phase2_diffs.append(compare_against_reference(res, v182_dir / "accounts" / ver / win / case))

    _write_json(out / "phase2_fl_independent_diff.json", phase2_diffs)
    flat_diffs = [
        {
            "version": d["version"],
            "window": d["window"],
            "case": d["case"],
            "open_price_mode": d["open_price_mode"],
            "signal_source": d["signal_source"],
            "exact_reference_match": d["exact_reference_match"],
            "max_target_diff": d["layer2_targets"]["max_abs_weight_diff"],
            "orig_fills": d["layer3_4_orders_and_fills"]["orig_fills"],
            "fl_fills": d["layer3_4_orders_and_fills"]["fl_fills"],
            "max_fill_px_diff_usd": d["layer3_4_orders_and_fills"]["max_fill_price_diff_usd"],
            "max_nav_diff_usd": d["layer5_daily_ledger"]["max_abs_nav_diff_usd"],
            "max_attr_diff_usd": d["layer6_attribution"]["max_abs_ticker_pnl_diff_usd"],
            "max_sp500_metric_diff": d["layer7_metrics"]["max_sp500_metric_diff"],
            "max_qqq_metric_diff": d["layer7_metrics"]["max_qqq_metric_diff"],
        }
        for d in phase2_diffs
    ]
    pd.DataFrame(flat_diffs).to_csv(out / "fl_independent_replay_diff_matrix.csv", index=False)

    # ------------------------------------------------------------------
    # Phase 3: Real Financial Research & Independent Robustness
    # ------------------------------------------------------------------
    comparison_rows, base_results = run_complete_strategy_comparison(config, markets)
    stress_rows = run_execution_robustness_suite(config, markets)
    concentration = run_concentration_and_ablation(config, markets, base_results)
    regimes = run_regime_decomposition(base_results, markets)
    sensitivity = run_parameter_sensitivity_and_overfitting(config, markets, base_results)

    phase3 = {
        "strategy_window_comparison": comparison_rows,
        "execution_robustness": stress_rows,
        "concentration_and_ablation": concentration,
        "regime_decomposition": regimes,
        "parameter_sensitivity_and_overfitting": sensitivity,
    }
    _write_json(out / "phase3_financial_research.json", phase3)
    pd.DataFrame(comparison_rows).to_csv(out / "financial_comparison_matrix.csv", index=False)
    pd.DataFrame(stress_rows).to_csv(out / "execution_robustness_matrix.csv", index=False)
    pd.DataFrame(concentration["leave_one_out_ablation"]).to_csv(out / "leave_one_out_ablation.csv", index=False)

    # ------------------------------------------------------------------
    # Phase 4 & 5: Monte Carlo Risk Study & Statistical Convergence
    # ------------------------------------------------------------------
    if skip_mc:
        mc_tier1 = {"tier": "SKIPPED_BY_FLAG", "rows": []}
        mc_tier2 = {
            "tier": "SKIPPED_BY_FLAG",
            "convergence_rows": [],
            "repeat_summaries_by_strategy": {s: [] for s in config.strategies},
            "model_comparison_rows": [],
            "calibration_sensitivity_rows": [],
            "execution_stress_mc_rows": [],
            "resources": {},
        }
    else:
        calib_windows = {
            "target_4y": prepare_calibration_dataset(markets["target"], start="2023-01-03", end="2026-11-23", include_sndk=False),
            "target_4y_with_sndk": prepare_calibration_dataset(markets["target"], start="2025-02-24", end="2026-11-23", include_sndk=True),
            "history_10y": prepare_calibration_dataset(markets["twenty"], start="2016-11-21", end="2026-11-20", include_sndk=False),
            "history_20y": prepare_calibration_dataset(markets["twenty"], start="2010-02-11", end="2026-11-20", include_sndk=False),
        }
        # Compute average realized target weights on twenty/normal for Tier-1 fixed-weight reference
        avg_weights: dict[str, dict[str, float]] = {}
        for s in config.strategies:
            tdf = base_results[(s, "twenty", "normal")].targets
            avg_weights[s] = {col: float(tdf[col].mean()) for col in config.tickers if col in tdf.columns}

        mc_tier1 = run_tier1_fast_portfolio_mc(config, calib_windows["history_20y"], avg_weights, out)
        mc_tier2 = run_tier2_dynamic_strategy_mc(
            config,
            {k: calib_windows[k] for k in ("target_4y", "history_10y", "history_20y")},
            primary_window="history_20y",
        )
        _write_json(out / "phase4_5_monte_carlo_tier1.json", mc_tier1)
        _write_json(out / "phase4_5_monte_carlo_tier2.json", mc_tier2)
        if mc_tier1.get("rows"):
            pd.DataFrame(mc_tier1["rows"]).to_csv(out / "mc_tier1_fixed_portfolio.csv", index=False)

    # ------------------------------------------------------------------
    # Phase 6: Multi-Strategy Shared Account Joint Risk (#260 Policy)
    # ------------------------------------------------------------------
    phase6 = run_shared_account_study(config, markets, base_results)
    _write_json(out / "phase6_shared_account_risk.json", phase6)
    pd.DataFrame(phase6["alpaca_shared_account_schemes"]).to_csv(out / "shared_account_alpaca_schemes.csv", index=False)

    # ------------------------------------------------------------------
    # Phase 9: Champion Selector & ExperimentTracker Integration
    # ------------------------------------------------------------------
    repo_dir = Path(__file__).resolve().parents[4]
    code_id = _git_code_identity(repo_dir)
    phase9 = evaluate_champion_and_status(
        config=config,
        phase1_audit=p1_pkg,
        phase1_qlib=p1_qlib,
        phase2_diffs=phase2_diffs,
        base_results=base_results,
        concentration=concentration,
        mc_tier2=mc_tier2,
        tracker_dir=out / "experiment_tracker",
        code_identity=code_id,
    )
    _write_json(out / "phase9_champion_evaluation.json", phase9)

    summary = {
        "schema": "FACTORLAB_EXTERNAL_VALIDATION_SUMMARY_V1",
        "study_id": config.study_id,
        "code": code_id,
        "total_wall_seconds": perf_counter() - started,
        "phase1_qlib_status": p1_qlib.get("status"),
        "phase1_qlib_passed": f"{p1_qlib.get('passed_accounts', 0)}/{p1_qlib.get('total_accounts', 23)}",
        "phase1_v69_qlib_verified": bool((p1_qlib.get("v69_replay") or {}).get("verified")),
        "phase2_fl_exact_qlib_f32": sum(1 for d in phase2_diffs if d["open_price_mode"] == "qlib_float32" and d["exact_reference_match"]),
        "phase2_fl_total_qlib_f32": sum(1 for d in phase2_diffs if d["open_price_mode"] == "qlib_float32"),
        "phase9_champion_verdict": phase9["overall_champion_verdict"],
        "phase9_promoted": phase9["overall_promoted"],
        "phase9_status_matrix": phase9["status_matrix"],
        "output_dir": str(out),
    }
    _write_json(out / "summary.json", summary)
    return {
        "summary": summary,
        "phase1": phase1,
        "phase2": phase2_diffs,
        "phase3": phase3,
        "phase4_5_tier1": mc_tier1,
        "phase4_5_tier2": mc_tier2,
        "phase6": phase6,
        "phase9": phase9,
    }
