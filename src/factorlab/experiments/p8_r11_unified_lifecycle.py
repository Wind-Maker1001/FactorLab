"""FactorLab P8-R11: Unified Grid Lifecycle Execution Adapter (L0 / L1 / L2 x A / B / C / CASH).

Reuses the existing authoritative Paper/Grid stack without creating a third Grid engine:
- R09: ``GridExecution``, ``PaperBroker``, ``TradingController``, ``PaperStore``,
  ``run_offline_lifecycle``, ``DemoGuardrails``, ``ExecutionAssumptions``,
  ``execution_profile``, ``FundingSchedule``, ``grid_fifo.attribute_events``.
- R10: ``FundingPolicy``, ``VolatilityPolicy``, ``FundingSensitiveGrid``,
  ``VolatilityAdaptiveGrid``, ``synthetic_hourly_funding_events``,
  ``compute_equal_bh_benchmark``, ``paired_statistical_test``.

Guarantees:
1. Single authoritative exit order lifecycle (no duplicate Strategy B + F11 sells).
2. Same-timestamp ordering: funding settles FIRST on prior inventory, THEN fills execute.
3. Strict causality for Strategy B (settled funding <= bar_close_utc) and Strategy C
   (completed closes <= bar_close_utc and training-frozen reference_vol_1h).
4. Zero-residual FIFO reconciliation across all generations and funding settlements.
"""
from __future__ import annotations

from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import math
from pathlib import Path
from typing import Any

import numpy as np

from factorlab.experiments.grid_f11_f08_offline import (
    DEFAULT_DEMO_WALLET,
    DEFAULT_STRATEGY_CAP,
    EXIT_PEAK_MONITORING_METHOD,
    MODEL_BASELINE,
    MODEL_F11_F08,
    MODEL_F11_ONLY,
    DemoGuardrails,
    ExecutionAssumptions,
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
    compute_equal_bh_benchmark,
    parse_utc,
    sha256_file,
    synthetic_hourly_funding_events,
)
from factorlab.research.monte_carlo import _loss_tail

D = Decimal

UNIFIED_EXECUTION_MODEL_VERSION = "UNIFIED_F11_F08_LIFECYCLE_V1"

LIFECYCLE_LABELS: dict[str, str] = {
    "L0": MODEL_BASELINE,
    "L1": MODEL_F11_ONLY,
    "L2": MODEL_F11_F08,
}

ALL_EXECUTION_PROFILES: tuple[str, ...] = (
    "IDEAL_LIMIT_FILL",
    "DELAYED_EXIT_FILL",
    "LIMITED_CAPACITY_PARTIAL",
    "UNFILLED_EXIT_TIMEOUT",
    "GAP_AND_CANCEL_FAULT",
)

ALL_FUNDING_REGIMES: tuple[str, ...] = (
    "EX_FUNDING",
    "MAINNET_ACTUAL",
    "NORMAL_SYNTHETIC_0.125BPS",
    "DEMO_HIGH_FUNDING_22.97BPS",
    "SWITCH_48H",
)


def lifecycle_guardrails(
    lifecycle_id: str,
    *,
    max_notional: Decimal = DEFAULT_STRATEGY_CAP,
    max_loss: Decimal | None = D("1"),
    total_orders: int = 100,
    total_notional: Decimal | None = None,
) -> DemoGuardrails:
    """Construct DemoGuardrails for L0 (stranded baseline), L1 (F11 only), or L2 (F11+F08)."""
    if lifecycle_id == "L0":
        enable_f11, enable_f08 = False, False
    elif lifecycle_id == "L1":
        enable_f11, enable_f08 = True, False
    elif lifecycle_id == "L2":
        enable_f11, enable_f08 = True, True
    else:
        raise ValueError(f"Unknown lifecycle_id: {lifecycle_id}")
    tot_not = total_notional if total_notional is not None else max(D("700"), max_notional * D("100"))
    return DemoGuardrails(
        max_notional=max_notional,
        max_loss=max_loss,
        total_orders=total_orders,
        total_notional=tot_not,
        enable_f11=enable_f11,
        enable_f08=enable_f08,
    )


def build_execution_assumptions(
    profile_name: str,
    *,
    historical_bar_volume: bool = False,
    participation: Decimal = D("0.01"),
    inject_cancel_fill_race_once: bool = False,
) -> ExecutionAssumptions:
    """Build conditional execution assumptions reusing R09's predeclared profiles."""
    if profile_name == "LIMITED_CAPACITY_PARTIAL":
        base = execution_profile("LIMITED_CAPACITY_PARTIAL", use_bar_volume=False)
    else:
        base = execution_profile(profile_name, use_bar_volume=historical_bar_volume)
        if historical_bar_volume:
            base = replace(base, participation=participation)
    if inject_cancel_fill_race_once:
        base = replace(
            base,
            profile_name=f"{base.profile_name}_CANCEL_FILL_RACE",
            inject_cancel_fill_race_once=True,
        )
    return base


def build_unified_funding_schedule(
    bars: list[dict[str, Any]],
    *,
    regime_id: str,
    historical_funding_events: list[tuple[datetime, Decimal, Decimal, str]] | None = None,
) -> tuple[FundingSchedule, list[tuple[datetime, Decimal, Decimal, str]]]:
    """Build synchronized (ledger_schedule, causal_signal_events) for a funding regime.

    Both ledger settlement (``PaperBroker.apply_funding``) and Strategy B's causal estimator
    consume the exact same in-window event timestamps & rates. Pre-window warmup events are
    visible only to Strategy B's causal 24h lookback and are never billed to the ledger.
    """
    if not bars:
        raise ValueError("nonempty bars required")
    start_dt = parse_utc(bars[0]["time"])
    end_dt = parse_utc(bars[-1]["time"]) + timedelta(hours=1)
    anchor = float(bars[0]["close"])

    if regime_id == "EX_FUNDING":
        return FundingSchedule(mode="zero", hourly_bps=0.0, events=(), label="EX_FUNDING"), []

    if regime_id == "MAINNET_ACTUAL":
        all_hist = sorted(historical_funding_events or [], key=lambda e: e[0])
        in_win = tuple(e for e in all_hist if start_dt <= e[0] < end_dt)
        return (
            FundingSchedule(mode="historical_events", hourly_bps=0.0, events=in_win, label="MAINNET_ACTUAL"),
            all_hist,
        )

    dates = [b["time"] for b in bars]
    closes = [b["close"] for b in bars]
    if regime_id == "NORMAL_SYNTHETIC_0.125BPS":
        init_bps = NORMAL_PERP_HOURLY_FUNDING_BPS
        synth = synthetic_hourly_funding_events(dates, closes, constant_hourly_bps=init_bps)
    elif regime_id == "DEMO_HIGH_FUNDING_22.97BPS":
        init_bps = DEMO_OBSERVED_HOURLY_FUNDING_BPS
        synth = synthetic_hourly_funding_events(dates, closes, constant_hourly_bps=init_bps)
    elif regime_id == "SWITCH_48H":
        init_bps = NORMAL_PERP_HOURLY_FUNDING_BPS
        synth = synthetic_hourly_funding_events(
            dates, closes, regime_switch_bps=(48, NORMAL_PERP_HOURLY_FUNDING_BPS, DEMO_OBSERVED_HOURLY_FUNDING_BPS)
        )
    else:
        raise ValueError(f"Unknown funding regime_id: {regime_id}")

    warmup_pre = [
        (start_dt - timedelta(hours=h), D(str(anchor)), D(str(init_bps)) / D(10000), "SyntheticWarmup")
        for h in range(24, 0, -1)
    ]
    return (
        FundingSchedule(mode="historical_events", hourly_bps=0.0, events=tuple(synth), label=regime_id),
        warmup_pre + synth,
    )


def cash_reference_result(
    *,
    bars: list[dict[str, Any]],
    strategy_cap_usdt: Decimal,
    lifecycle_id: str,
    execution_profile_name: str,
    funding_label: str,
    historical_funding_events: list[tuple[datetime, Decimal, Decimal, str]] | None = None,
) -> dict[str, Any]:
    """Zero-exposure CASH reference arm aligned to the exact same window and denominator."""
    bh = _equal_bh_for_regime(bars, strategy_cap_usdt, funding_label, historical_funding_events)
    return {
        "strategy_id": "CASH",
        "lifecycle_id": lifecycle_id,
        "execution_model_version": UNIFIED_EXECUTION_MODEL_VERSION,
        "model_label": LIFECYCLE_LABELS[lifecycle_id],
        "execution_profile": execution_profile_name,
        "evidence_label": "ZERO_EXPOSURE_REFERENCE",
        "funding_label": funding_label,
        "initial_half_width_pct": 0.0,
        "bars": len(bars),
        "interval": "1h",
        "opening_wallet_usdt": str(DEFAULT_DEMO_WALLET),
        "strategy_cap_usdt": str(strategy_cap_usdt),
        "final_wallet_usdt": str(DEFAULT_DEMO_WALLET),
        "final_equity_usdt": str(DEFAULT_DEMO_WALLET),
        "final_nav_account": 1.0,
        "net_pnl_usdt": 0.0,
        "net_return_pct": 0.0,
        "loss_fraction": 0.0,
        "max_drawdown": 0.0,
        "max_drawdown_pct": 0.0,
        "realized_trade_gross_usdt": 0.0,
        "trade_gross_pnl_usdt": 0.0,
        "trading_net_usdt": 0.0,
        "realized_pnl_usdt": 0.0,
        "unrealized_pnl_usdt": 0.0,
        "fees_usdt": 0.0,
        "slippage_usdt": 0.0,
        "realized_funding_usdt": 0.0,
        "funding_events_count": 0,
        "funding_erosion_of_cap_pct": 0.0,
        "funding_erosion_of_gross_pct": None,
        "grid_cycles": 0,
        "fills_total": 0,
        "orders_total": 0,
        "submitted_orders": 0,
        "filled_orders": 0,
        "order_fill_rate": 0.0,
        "traded_notional_usdt": 0.0,
        "turnover_ratio": 0.0,
        "posts_consumed": 0,
        "deletes_consumed": 0,
        "submitted_notional_usdt": 0.0,
        "holding_hours": 0.0,
        "mean_lot_holding_hours": 0.0,
        "paused_with_inventory_hours": 0.0,
        "residual_inventory": 0.0,
        "residual_notional_usdt": 0.0,
        "stranded_at_end": False,
        "breakout_events": 0,
        "flat_after_breakout_events": 0,
        "exit_orders_submitted": 0,
        "exit_orders_filled": 0,
        "exit_orders_partial_events": 0,
        "exit_reprices": 0,
        "exit_cancel_faults": 0,
        "exit_reconcile_recoveries": 0,
        "dust_protections_triggered": 0,
        "exit_block_counts": {},
        "last_exit_block_reason": None,
        "mean_exit_delay_hours": 0.0,
        "max_exit_delay_hours": 0.0,
        "rotations_completed": 0,
        "rotations_blocked_count": 0,
        "rotation_blocked_bars": 0,
        "rotations_blocked": [],
        "rotation_success_rate": None,
        "flat_rotation_success_rate": None,
        "generations_total": 1,
        "halted": False,
        "halt_reason": None,
        "final_state": "CASH",
        "final_reason": None,
        "max_simultaneous_exit_orders": 0,
        "bar_end_max_simultaneous_exit_orders": 0,
        "exit_peak_monitoring_method": EXIT_PEAK_MONITORING_METHOD,
        "exit_overlap_violations": [],
        "funding_blocked_hours": 0,
        "vol_blocked_hours": 0,
        "adaptive_rebuilds": 0,
        "funding_exit_triggers": 0,
        "exit_trigger_reasons": [],
        "reconciliation_residual_usdt": "0E-8",
        "reconciled_zero_residual": True,
        "bh_net_pnl_with_funding_usdt": round(bh["bh_net_pnl_with_funding_usdt"], 8),
        "bh_return_with_funding_pct": round(bh["bh_return_with_funding_pct"], 4),
        "excess_vs_equal_bh_usdt": round(-bh["bh_net_pnl_with_funding_usdt"], 8),
        "excess_vs_equal_bh_pct": round(-bh["bh_return_with_funding_pct"], 4),
    }


def _equal_bh_for_regime(
    bars: list[dict[str, Any]],
    strategy_cap_usdt: Decimal,
    funding_regime: str,
    historical_funding_events: list[tuple[datetime, Decimal, Decimal, str]] | None,
) -> dict[str, float]:
    cap_f = float(strategy_cap_usdt)
    start_dt = parse_utc(bars[0]["time"])
    end_dt = parse_utc(bars[-1]["time"]) + timedelta(hours=1)
    if funding_regime == "EX_FUNDING":
        return compute_equal_bh_benchmark(bars, qty=0.04, capital=cap_f, funding_events=[], funding_scale=0.0)
    if funding_regime == "MAINNET_ACTUAL":
        w_ev = [e for e in (historical_funding_events or []) if start_dt <= e[0] < end_dt]
        return compute_equal_bh_benchmark(bars, qty=0.04, capital=cap_f, funding_events=w_ev, funding_scale=1.0)
    if funding_regime == "NORMAL_SYNTHETIC_0.125BPS":
        return compute_equal_bh_benchmark(
            bars, qty=0.04, capital=cap_f, constant_hourly_bps=NORMAL_PERP_HOURLY_FUNDING_BPS
        )
    if funding_regime == "DEMO_HIGH_FUNDING_22.97BPS":
        return compute_equal_bh_benchmark(
            bars, qty=0.04, capital=cap_f, constant_hourly_bps=DEMO_OBSERVED_HOURLY_FUNDING_BPS
        )
    if funding_regime == "SWITCH_48H":
        return compute_equal_bh_benchmark(
            bars,
            qty=0.04,
            capital=cap_f,
            regime_switch_bps=(48, NORMAL_PERP_HOURLY_FUNDING_BPS, DEMO_OBSERVED_HOURLY_FUNDING_BPS),
        )
    raise ValueError(f"Unknown funding regime: {funding_regime}")


def run_unified_lifecycle(
    bars: list[dict[str, Any]],
    *,
    strategy_id: str = "A_FIXED",
    lifecycle_id: str = "L2",
    execution_profile_name: str = "IDEAL_LIMIT_FILL",
    funding_regime: str = "MAINNET_ACTUAL",
    strategy_cap_usdt: Decimal = DEFAULT_STRATEGY_CAP,
    opening_wallet: Decimal = DEFAULT_DEMO_WALLET,
    funding_policy: FundingPolicy | None = None,
    vol_policy: VolatilityPolicy | None = None,
    warmup_closes: list[float] | None = None,
    historical_funding_events: list[tuple[datetime, Decimal, Decimal, str]] | None = None,
    historical_bar_volume: bool = False,
    participation: Decimal = D("0.01"),
    max_loss: Decimal | None = D("1"),
    account_id: str = "r11-unified",
    family_prefix: str = "fl-r11",
    inject_cancel_fill_race_once: bool = False,
    record_hourly_series: bool = False,
) -> dict[str, Any]:
    """Single canonical entry point for P8-R11 and S02-F01 unified lifecycle research.

    Dispatches CASH, Strategy A (fixed Grid), Strategy B (FundingSensitiveGrid), and
    Strategy C (VolatilityAdaptiveGrid) across lifecycles L0/L1/L2, execution realism
    profiles, and funding regimes over one authoritative Paper/Grid/FIFO engine.
    """
    if strategy_id == "CASH":
        return cash_reference_result(
            bars=bars,
            strategy_cap_usdt=strategy_cap_usdt,
            lifecycle_id=lifecycle_id,
            execution_profile_name=execution_profile_name,
            funding_label=funding_regime,
            historical_funding_events=historical_funding_events,
        )

    guardrails = lifecycle_guardrails(
        lifecycle_id,
        max_notional=strategy_cap_usdt,
        max_loss=max_loss,
    )
    assumptions = build_execution_assumptions(
        execution_profile_name,
        historical_bar_volume=historical_bar_volume,
        participation=participation,
        inject_cancel_fill_race_once=inject_cancel_fill_race_once,
    )
    funding_schedule, causal_events = build_unified_funding_schedule(
        bars,
        regime_id=funding_regime,
        historical_funding_events=historical_funding_events,
    )

    res = run_offline_lifecycle(
        bars,
        rules=default_demo_rules(),
        guardrails=guardrails,
        assumptions=assumptions,
        funding=funding_schedule,
        opening_wallet=opening_wallet,
        strategy_cap_usdt=strategy_cap_usdt,
        interval="1h",
        instrument_id="NVDAUSDT",
        account_id=account_id,
        family_prefix=family_prefix,
        store_path=":memory:",
        record_hourly_series=record_hourly_series,
        strategy_id=strategy_id,
        funding_policy=funding_policy,
        vol_policy=vol_policy,
        warmup_closes=warmup_closes,
        causal_funding_events=causal_events,
        defer_rotation_when_entry_blocked=True,
    )

    # Hard runtime integrity assertions (fail closed)
    if not res["reconciled_zero_residual"]:
        raise AssertionError(
            f"Non-zero FIFO residual {res['reconciliation_residual_usdt']} for "
            f"{strategy_id}/{lifecycle_id}/{execution_profile_name}/{funding_regime}"
        )
    if res["max_simultaneous_exit_orders"] > 1 or res.get("exit_overlap_violations"):
        raise AssertionError(
            f"Duplicate simultaneous exit orders ({res['max_simultaneous_exit_orders']}) for "
            f"{strategy_id}/{lifecycle_id}/{execution_profile_name}/{funding_regime}: "
            f"{res.get('exit_overlap_violations')}"
        )

    bh = _equal_bh_for_regime(bars, strategy_cap_usdt, funding_regime, historical_funding_events)
    res["lifecycle_id"] = lifecycle_id
    res["execution_model_version"] = UNIFIED_EXECUTION_MODEL_VERSION
    res["bh_net_pnl_with_funding_usdt"] = round(bh["bh_net_pnl_with_funding_usdt"], 8)
    res["bh_return_with_funding_pct"] = round(bh["bh_return_with_funding_pct"], 4)
    res["excess_vs_equal_bh_usdt"] = round(res["net_pnl_usdt"] - bh["bh_net_pnl_with_funding_usdt"], 8)
    res["excess_vs_equal_bh_pct"] = round(res["net_return_pct"] - bh["bh_return_with_funding_pct"], 4)
    return res


def holm_bonferroni_adjust(
    p_values: list[float],
    *,
    round_digits: int | None = 6,
) -> list[float]:
    """Compute Holm-Bonferroni step-down adjusted p-values from unrounded p-values, preserving order."""
    m = len(p_values)
    if m == 0:
        return []
    indexed = sorted(enumerate(p_values), key=lambda pair: float(pair[1]))
    adjusted = [1.0] * m
    running_max = 0.0
    for rank, (orig_idx, p) in enumerate(indexed):
        val = min(1.0, max(running_max, float(p) * (m - rank)))
        running_max = val
        adjusted[orig_idx] = round(val, round_digits) if round_digits is not None else val
    return adjusted


def paired_statistical_test_unrounded(
    candidate_vals: np.ndarray,
    control_vals: np.ndarray,
    *,
    n_bootstrap: int = 10_000,
    seed: int = 202610,
) -> dict[str, Any]:
    """Paired bootstrap CI, Wilcoxon signed-rank, and paired t-test retaining unrounded p-values
    for downstream Holm-Bonferroni family-wise correction before display rounding."""
    from scipy import stats as scipy_stats

    diff = np.asarray(candidate_vals, dtype=float) - np.asarray(control_vals, dtype=float)
    n = len(diff)
    if n == 0:
        return {"n_windows": 0}
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
        "mean_diff_raw_pct": mean_diff,
        "mean_diff_pct": round(mean_diff, 4),
        "median_diff_pct": round(median_diff, 4),
        "std_diff_pct": round(std_diff, 4),
        "win_rate_pct": round(win_rate * 100.0, 2),
        "tie_rate_pct": round(tie_rate * 100.0, 2),
        "loss_rate_pct": round(loss_rate * 100.0, 2),
        "bootstrap_95ci_low_pct": round(ci_low, 4),
        "bootstrap_95ci_high_pct": round(ci_high, 4),
        "bootstrap_p_raw": p_boot,
        "bootstrap_p_value": round(p_boot, 4),
        "wilcoxon_p_raw": wilcoxon_p,
        "wilcoxon_p_value": round(wilcoxon_p, 4),
        "paired_t_stat": round(t_stat, 4),
        "paired_t_p_raw": t_p,
        "paired_t_p_value": round(t_p, 4),
        "ci_strictly_positive": bool(ci_low > 0.0),
        "statistically_significant_05": bool(ci_low > 0.0 and wilcoxon_p < 0.05),
    }


def summarize_unified_windows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate Section 9 metrics across a paired window group."""
    if not rows:
        return {}
    n = len(rows)
    rets = np.asarray([float(r["net_return_pct"]) / 100.0 for r in rows], dtype=float)
    losses = -rets
    var95, cvar95 = _loss_tail(losses, 0.95)
    dds = np.asarray([float(r["max_drawdown_pct"]) for r in rows], dtype=float)
    tot_bo = int(sum(int(r["breakout_events"]) for r in rows))
    tot_flat = int(sum(int(r["flat_after_breakout_events"]) for r in rows))
    tot_rot = int(sum(int(r["rotations_completed"]) for r in rows))
    loss_count = int(sum(1 for r in rows if float(r["net_pnl_usdt"]) < -1e-12))
    pos_count = int(sum(1 for r in rows if float(r["net_pnl_usdt"]) > 1e-12))

    # Wilson 95% CI for loss probability
    z = 1.959963984540054
    p_hat = loss_count / n
    denom = 1.0 + z * z / n
    center = (p_hat + z * z / (2.0 * n)) / denom
    half = z * math.sqrt((p_hat * (1.0 - p_hat) + z * z / (4.0 * n)) / n) / denom
    wilson_low = max(0.0, center - half)
    wilson_high = min(1.0, center + half)

    return {
        "windows": n,
        "effective_tail_samples_95": round(0.05 * n, 2),
        "total_trade_gross_pnl_usdt": round(sum(float(r["trade_gross_pnl_usdt"]) for r in rows), 6),
        "total_realized_trade_gross_usdt": round(sum(float(r["realized_trade_gross_usdt"]) for r in rows), 6),
        "total_fees_usdt": round(sum(float(r["fees_usdt"]) for r in rows), 6),
        "total_slippage_usdt": round(sum(float(r["slippage_usdt"]) for r in rows), 6),
        "total_trading_net_usdt": round(sum(float(r["trading_net_usdt"]) for r in rows), 6),
        "total_realized_pnl_usdt": round(sum(float(r["realized_pnl_usdt"]) for r in rows), 6),
        "total_unrealized_pnl_usdt": round(sum(float(r["unrealized_pnl_usdt"]) for r in rows), 6),
        "total_funding_pnl_usdt": round(sum(float(r["realized_funding_usdt"]) for r in rows), 6),
        "total_final_net_pnl_usdt": round(sum(float(r["net_pnl_usdt"]) for r in rows), 6),
        "mean_final_net_pnl_usdt": round(float(np.mean([float(r["net_pnl_usdt"]) for r in rows])), 6),
        "mean_net_return_pct": round(float(np.mean(rets) * 100.0), 4),
        "median_net_return_pct": round(float(np.median(rets) * 100.0), 4),
        "worst_window_return_pct": round(float(np.min(rets) * 100.0), 4),
        "best_window_return_pct": round(float(np.max(rets) * 100.0), 4),
        "positive_windows": pos_count,
        "loss_windows": loss_count,
        "loss_prob_pct": round(p_hat * 100.0, 2),
        "loss_prob_wilson_95_low_pct": round(wilson_low * 100.0, 2),
        "loss_prob_wilson_95_high_pct": round(wilson_high * 100.0, 2),
        "var95_pct": round(float(var95 * 100.0), 4),
        "cvar95_pct": round(float(cvar95 * 100.0), 4),
        "mean_max_drawdown_pct": round(float(np.mean(dds)), 4),
        "worst_max_drawdown_pct": round(float(np.max(dds)), 4),
        "mean_order_fill_rate": round(float(np.mean([float(r["order_fill_rate"]) for r in rows])), 4),
        "total_fills": int(sum(int(r["fills_total"]) for r in rows)),
        "mean_fills_per_window": round(float(np.mean([int(r["fills_total"]) for r in rows])), 3),
        "total_grid_cycles": int(sum(int(r["grid_cycles"]) for r in rows)),
        "mean_turnover_ratio": round(float(np.mean([float(r["turnover_ratio"]) for r in rows])), 4),
        "mean_holding_hours": round(float(np.mean([float(r["holding_hours"]) for r in rows])), 2),
        "mean_lot_holding_hours": round(float(np.mean([float(r["mean_lot_holding_hours"]) for r in rows])), 2),
        "mean_paused_with_inventory_hours": round(
            float(np.mean([float(r["paused_with_inventory_hours"]) for r in rows])), 2
        ),
        "mean_exit_delay_hours": round(float(np.mean([float(r["mean_exit_delay_hours"]) for r in rows])), 2),
        "stranded_windows": int(sum(1 for r in rows if bool(r["stranded_at_end"]))),
        "mean_residual_inventory": round(float(np.mean([float(r["residual_inventory"]) for r in rows])), 4),
        "total_breakouts": tot_bo,
        "total_rotations": tot_rot,
        "mean_rotations_per_window": round(tot_rot / n, 3),
        "rotation_success_rate": round(tot_rot / tot_bo, 4) if tot_bo > 0 else None,
        "flat_rotation_success_rate": round(tot_rot / tot_flat, 4) if tot_flat > 0 else None,
        "mean_posts_consumed": round(float(np.mean([int(r["posts_consumed"]) for r in rows])), 2),
        "total_posts_consumed": int(sum(int(r["posts_consumed"]) for r in rows)),
        "mean_submitted_notional_usdt": round(
            float(np.mean([float(r["submitted_notional_usdt"]) for r in rows])), 4
        ),
        "halted_windows": int(sum(1 for r in rows if bool(r["halted"]))),
        "total_funding_exit_triggers": int(sum(int(r.get("funding_exit_triggers", 0)) for r in rows)),
        "mean_funding_blocked_hours": round(
            float(np.mean([int(r.get("funding_blocked_hours", 0)) for r in rows])), 2
        ),
        "mean_vol_blocked_hours": round(
            float(np.mean([int(r.get("vol_blocked_hours", 0)) for r in rows])), 2
        ),
        "all_zero_fifo_residual": bool(all(bool(r["reconciled_zero_residual"]) for r in rows)),
        "max_fifo_residual_usdt": max(abs(float(r["reconciliation_residual_usdt"])) for r in rows),
        "max_simultaneous_exit_orders": max(int(r.get("max_simultaneous_exit_orders", 0)) for r in rows),
        "risk_adjusted_objective": round(float(np.mean(rets) - 0.5 * cvar95), 6),
    }


def build_s02_handoff_contract(
    *,
    repo_root: Path,
    conclusions: dict[str, Any],
    historical_oos_summary: dict[str, Any],
    mc_key_summary: list[dict[str, Any]],
    data_audit: dict[str, Any],
    paired_ablations: list[dict[str, Any]] | None = None,
    engineering_verification_scope: dict[str, Any] | None = None,
    b2_b5_equivalence_boundary: dict[str, Any] | None = None,
    mc_tail_convergence_notice: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build machine-readable S02-F01 consumer contract without duplicating ChampionSelector."""
    cfg_paths = {
        "study": repo_root / "configs" / "research" / "p8_r11_unified_lifecycle_study.json",
        "A": repo_root / "configs" / "research" / "p8_r10_strategy_a_fixed.json",
        "B": repo_root / "configs" / "research" / "p8_r10_strategy_b_funding_sensitive.json",
        "C": repo_root / "configs" / "research" / "p8_r10_strategy_c_vol_adaptive.json",
    }
    config_manifest = {
        k: {
            "path": str(p.relative_to(repo_root)).replace("\\", "/"),
            "sha256": sha256_file(p) if p.is_file() else None,
        }
        for k, p in cfg_paths.items()
    }
    default_eng_scope = {
        "fifo_zero_residual_scope": "FULL_STUDY_AND_TARGETED_RECOMPUTES",
        "exit_peak_monitoring_method": EXIT_PEAK_MONITORING_METHOD,
        "event_level_exit_peak_verified_scope": "TARGETED_UNIT_STRESS_HISTORICAL_AND_MC_VERIFICATION_BATCHES",
        "legacy_bar_end_only_notice": (
            "Original 60,240 historical+MC runs used bar-end sampling for max_simultaneous_exit_orders; "
            "event-level full-lifecycle peak verification covers all post-fix unit/regression tests, "
            "the full 338-run deterministic stress suite, historical mechanism-ablation recomputes, "
            "and representative MC verification runs."
        ),
    }
    default_b2_b5 = {
        "historical_l1_l2_paired_cells_executed": 36,
        "historical_l1_l2_full_matrix_cells": 50,
        "uncovered_cells_count": 14,
        "covered_regimes_and_profiles": {
            "MAINNET_ACTUAL": list(ALL_EXECUTION_PROFILES),
            "SWITCH_48H": list(ALL_EXECUTION_PROFILES),
            "DEMO_HIGH_FUNDING_22.97BPS": list(ALL_EXECUTION_PROFILES),
            "EX_FUNDING": ["L1:IDEAL_LIMIT_FILL", "L2:IDEAL_LIMIT_FILL", "L2:DELAYED_EXIT_FILL"],
            "NORMAL_SYNTHETIC_0.125BPS": ["L1:IDEAL_LIMIT_FILL", "L2:IDEAL_LIMIT_FILL", "L2:DELAYED_EXIT_FILL"],
        },
        "uncovered_combinations": (
            "EX_FUNDING and NORMAL_SYNTHETIC_0.125BPS under L1 (DELAYED_EXIT_FILL, "
            "LIMITED_CAPACITY_PARTIAL, UNFILLED_EXIT_TIMEOUT, GAP_AND_CANCEL_FAULT) and "
            "L2 (LIMITED_CAPACITY_PARTIAL, UNFILLED_EXIT_TIMEOUT, GAP_AND_CANCEL_FAULT)."
        ),
        "deterministic_counterexample_preserved": (
            "Under zero slippage with in-band open inventory before breakout, B5 funding exit "
            "triggers ahead of BREAKOUT through the single authoritative F11 limit exit; "
            "equivalence is scoped strictly to the 36 executed single-level 2-bps-slippage historical cells."
        ),
    }
    default_mc_tail = {
        "effective_historical_oos_tail_samples_95": 0.9,
        "historical_tail_certified": False,
        "mc_missing_comparison_semantics": {
            "missing_numeric_value": None,
            "status_when_insufficient": "COMPARISON_UNAVAILABLE",
            "reason_when_insufficient": "PAIRED_BASELINE_INSUFFICIENT",
            "downstream_rule": "Consumers MUST NOT coerce null comparison deltas to 0.0 (no fillna(0)).",
        },
        "unconverged_heavy_tail_notice": (
            "moving_block_bootstrap_24h SWITCH_48H L2/DELAYED B5 CVaR95 remains wide across seeds "
            "(n=256 cross-seed 8.98% vs 16.56%, n=512 CI width 8.79 pp) and is not certified as converged."
        ),
    }
    ablation_summary = {
        "fixed_param_mechanism_ablations": [
            r for r in (paired_ablations or []) if r.get("comparison_type") == "FIXED_PARAM_MECHANISM_ABLATION"
        ],
        "reoptimized_pipeline_comparisons": [
            r
            for r in (paired_ablations or [])
            if r.get("comparison_type") == "LIFECYCLE_REOPTIMIZED_PIPELINE_COMPARISON"
        ],
        "within_lifecycle_strategy_comparisons": [
            r
            for r in (paired_ablations or [])
            if r.get("comparison_type") == "WITHIN_LIFECYCLE_STRATEGY_COMPARISON"
        ],
    }
    return {
        "schema_version": 2,
        "producer": "P8-R11",
        "consumer": "P8-S02-F01",
        "execution_model_version": UNIFIED_EXECUTION_MODEL_VERSION,
        "supported_lifecycles": LIFECYCLE_LABELS,
        "supported_execution_profiles": list(ALL_EXECUTION_PROFILES),
        "supported_funding_regimes": list(ALL_FUNDING_REGIMES),
        "entry_points": {
            "python_callable": "factorlab.experiments.p8_r11_unified_lifecycle:run_unified_lifecycle",
            "study_cli": "tools/research/run_p8_r11_study.py",
        },
        "candidate_configs": config_manifest,
        "engineering_verification_scope": engineering_verification_scope or default_eng_scope,
        "funding_entry_time_recovery": {
            "origin": "PREEXISTING_R10_DEFECT",
            "status": "FIXED_IN_PR252_CHECKPOINT_WITH_LEGACY_EVENT_FALLBACK",
            "checkpoint_persisted_fields": [
                "entry_time_utc",
                "entry_price_dec",
                "entry_time_unrecoverable",
                "buy_blocked",
                "last_funding_bps_h",
                "exit_reasons",
                "funding_exit_triggers",
                "blocked_hours",
                "total_observed_hours",
            ],
            "legacy_checkpoint_policy": (
                "Reconstructs open-lot entry_time_utc strictly from authoritative PaperStore fill events; "
                "when authoritative events cannot reliably explain open inventory, leaves entry_time_utc=None "
                "and sets entry_time_unrecoverable=True without fabricating current time or zero age."
            ),
        },
        "b2_b5_equivalence_boundary": b2_b5_equivalence_boundary or default_b2_b5,
        "mc_tail_convergence_and_missingness": mc_tail_convergence_notice or default_mc_tail,
        "data_independence_and_qualification": {
            "historical_dataset_coverage": data_audit.get("coverage", {}),
            "windows_reused_from_r10": True,
            "independent_unseen_oos_available": False,
            "oos_advantage_supported": False,
            "champion_eligible_from_r11_evidence": False,
            "financial_admission": "FORMAL_ADMISSION_BLOCKED",
            "qualification_rule": (
                "P8-R11 reuses the R10 193-day 27-window / 18-OOS historical dataset for comparable "
                "lifecycle re-validation. S02-F01 MUST NOT grant Champion promotion or "
                "OOS_ADVANTAGE_SUPPORTED on these reused windows; a fresh, non-overlapping future "
                "holdout window audited after 2026-11-23T00:00:00Z is required."
            ),
        },
        "conclusions": conclusions,
        "historical_oos_cost_and_risk": historical_oos_summary,
        "historical_paired_ablations_separated": ablation_summary,
        "monte_carlo_key_risk_summary": mc_key_summary,
    }
