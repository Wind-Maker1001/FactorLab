"""Targeted regression tests for PR #252 independent review remediation:

- P2-1: MC missing L0 comparison null semantics, path identity matching, serialization & handoff parity.
- P2-2: Strict separation of fixed-parameter mechanism ablations vs lifecycle-reoptimized pipelines + unrounded Holm.
- P2-3: Event-level authoritative exit-order peak monitoring across scenarios A..I (including injected overlap=2).
- P3-1: Exact 36-cell historical B2/B5 equivalence scope & zero-slippage counterexample preservation.
- Preexisting R10 defect: FundingSensitiveGrid entry_time_utc & explicit decision state checkpoint recovery
  (continuous vs restored parity, partial fills, age exit, full exit cleanup, F08 rotation, legacy checkpoint).
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from factorlab.experiments.grid_f11_f08_offline import (
    DEFAULT_DEMO_WALLET,
    DemoGuardrails,
    EXIT_PEAK_MONITORING_METHOD,
    FundingSchedule,
    _demo_grid_config,
    default_demo_rules,
    execution_profile,
    reconstruct_exit_order_peak_from_events,
    run_offline_lifecycle,
)
from factorlab.experiments.grid_funding_adaptive import FundingPolicy, FundingSensitiveGrid
from factorlab.experiments.p8_r11_study import (
    build_separated_paired_ablations,
    compute_paired_mc_comparison,
    derive_r11_conclusions,
)
from factorlab.experiments.p8_r11_unified_lifecycle import (
    holm_bonferroni_adjust,
    paired_statistical_test_unrounded,
    run_unified_lifecycle,
)
from factorlab.research.monte_carlo.reliability import summarize_paths
from factorlab.strategies.grid_execution import _CHECKPOINT, _fingerprint
from factorlab.trading.contracts import Market, OrderEvent, OrderStatus, Side, ZERO
from factorlab.trading.control import RiskLimits, TradingController
from factorlab.trading.paper import Instrument, PaperBroker, Quote
from factorlab.trading.storage import PaperStore

D = Decimal
REPO_ROOT = Path(__file__).resolve().parents[2]


def _make_bars(closes: list[float], volumes: list[float] | None = None, start: str = "2026-11-01T00:00:00+00:00") -> list[dict[str, Any]]:
    t0 = datetime.fromisoformat(start)
    out = []
    for i, c in enumerate(closes):
        v = volumes[i] if volumes is not None else 10_000.0
        out.append(
            {
                "id": f"b{i}",
                "time": (t0 + timedelta(hours=i)).isoformat(),
                "open": c,
                "high": c,
                "low": c,
                "close": c,
                "volume": v,
            }
        )
    return out


def _fake_mc_series(n: int, *, seed: int = 1100, scenario: str = "gbm", ret_offset: float = 0.0) -> list[dict[str, Any]]:
    out = []
    for i in range(n):
        r = -0.5 - 0.01 * (i % 10) + ret_offset
        out.append(
            {
                "net_return_pct": r,
                "loss_fraction": -r / 100.0,
                "max_drawdown": max(0.0, -r / 100.0),
                "mc_path_id": f"{seed}:{scenario}:{i}",
            }
        )
    return out


# ---------------------------------------------------------------------------
# P2-1: MC missing lifecycle comparison semantics & serialization
# ---------------------------------------------------------------------------


def test_p2_1_mc_paired_comparison_all_regimes_and_serialization(tmp_path: Path):
    cand_512 = _fake_mc_series(512, ret_offset=0.2)
    base_512 = _fake_mc_series(512, ret_offset=0.0)
    cand_1024 = _fake_mc_series(1024, ret_offset=0.2)
    base_1024 = _fake_mc_series(1024, ret_offset=0.0)
    mismatch_512 = _fake_mc_series(512, seed=2200, ret_offset=0.0)

    def _rel(series: list[dict[str, Any]]):
        losses = [m["loss_fraction"] for m in series]
        dds = [m["max_drawdown"] for m in series]
        return summarize_paths(losses, dds, threshold=0.05, confidences=(0.95,), resamples=199, bootstrap_seed=1100)

    # 1. 512 vs 512 -> COMPUTED
    c_512 = compute_paired_mc_comparison(cand_512, base_512, cand_rel=_rel(cand_512), seed=1100)
    assert c_512["comparison_status"] == "COMPUTED"
    assert c_512["comparison_unavailable_reason"] is None
    assert c_512["matched_n_paths"] == 512
    assert c_512["delta_return_pp"] == pytest.approx(0.2, abs=1e-4)

    # 2. 1024 vs 1024 -> COMPUTED
    c_1024 = compute_paired_mc_comparison(cand_1024, base_1024, cand_rel=_rel(cand_1024), seed=1100)
    assert c_1024["comparison_status"] == "COMPUTED"
    assert c_1024["matched_n_paths"] == 1024
    assert c_1024["delta_return_pp"] == pytest.approx(0.2, abs=1e-4)

    # 3. 1024 vs 512 (insufficient paired baseline) -> explicit null / COMPARISON_UNAVAILABLE
    c_insuf = compute_paired_mc_comparison(
        cand_1024, None, cand_rel=_rel(cand_1024), seed=1100, baseline_available_n=512
    )
    assert c_insuf["comparison_status"] == "COMPARISON_UNAVAILABLE"
    assert c_insuf["comparison_unavailable_reason"] == "PAIRED_BASELINE_INSUFFICIENT"
    assert c_insuf["candidate_n_paths"] == 1024
    assert c_insuf["baseline_n_paths"] == 512
    assert c_insuf["matched_n_paths"] == 0
    assert c_insuf["delta_return_pp"] is None
    assert c_insuf["delta_cvar95_pp"] is None

    # Also test direct length mismatch passed as list
    c_len = compute_paired_mc_comparison(cand_1024, base_512, cand_rel=_rel(cand_1024), seed=1100)
    assert c_len["comparison_status"] == "COMPARISON_UNAVAILABLE"
    assert c_len["comparison_unavailable_reason"] == "PAIRED_BASELINE_INSUFFICIENT"
    assert c_len["delta_return_pp"] is None

    # 4. Same length but different path identity -> PATH_IDENTITY_MISMATCH
    c_mis = compute_paired_mc_comparison(cand_512, mismatch_512, cand_rel=_rel(cand_512), seed=1100)
    assert c_mis["comparison_status"] == "COMPARISON_UNAVAILABLE"
    assert c_mis["comparison_unavailable_reason"] == "PATH_IDENTITY_MISMATCH"
    assert c_mis["delta_return_pp"] is None

    # 5. Legitimate self-comparison -> real numeric 0.0 with SELF_BASELINE status
    c_self = compute_paired_mc_comparison(cand_512, None, cand_rel=_rel(cand_512), seed=1100, is_self=True)
    assert c_self["comparison_status"] == "SELF_BASELINE"
    assert c_self["delta_return_pp"] == 0.0
    assert c_self["delta_cvar95_pp"] == 0.0

    # 6. No baseline executed -> NO_BASELINE_EXECUTED
    c_none = compute_paired_mc_comparison(cand_512, None, cand_rel=_rel(cand_512), seed=1100)
    assert c_none["comparison_status"] == "COMPARISON_UNAVAILABLE"
    assert c_none["comparison_unavailable_reason"] == "NO_BASELINE_EXECUTED"
    assert c_none["delta_return_pp"] is None

    # 7. Computed exact zero differs cleanly from missing null
    c_zero = compute_paired_mc_comparison(base_512, base_512, cand_rel=_rel(base_512), seed=1100)
    assert c_zero["comparison_status"] == "COMPUTED"
    assert c_zero["delta_return_pp"] == 0.0

    # 8. JSON and CSV roundtrip preserve null/NaN without turning missing into 0.0
    jpath = tmp_path / "mc_cmp.json"
    jpath.write_text(json.dumps([c_512, c_insuf, c_self]), encoding="utf-8")
    jload = json.loads(jpath.read_text(encoding="utf-8"))
    assert jload[1]["delta_return_pp"] is None
    assert jload[2]["delta_return_pp"] == 0.0

    cpath = tmp_path / "mc_cmp.csv"
    pd.DataFrame([c_512, c_insuf, c_self]).to_csv(cpath, index=False)
    df = pd.read_csv(cpath)
    assert pd.isna(df.loc[1, "delta_return_pp"])
    assert pd.isna(df.loc[1, "delta_cvar95_pp"])
    assert df.loc[2, "delta_return_pp"] == 0.0


def test_p2_1_published_artifacts_have_zero_pseudo_zeros_and_handoff_parity():
    out_dir = REPO_ROOT / "docs" / "validation" / "p8-r11"
    if not (out_dir / "summary.json").is_file():
        pytest.skip("P8-R11 artifacts not present")

    summary = json.loads((out_dir / "summary.json").read_text(encoding="utf-8"))
    handoff = json.loads((out_dir / "s02_handoff.json").read_text(encoding="utf-8"))
    assert summary["s02_handoff"] == handoff

    exp_rows = summary["monte_carlo"]["expanded_key_scenarios"]
    unavail = [r for r in exp_rows if r["n_paths"] == 1024 and r["lifecycle_id"] != "L0"]
    assert len(unavail) == 8
    for r in unavail:
        assert r["comparison_status"] == "COMPARISON_UNAVAILABLE"
        assert r["comparison_unavailable_reason"] == "PAIRED_BASELINE_INSUFFICIENT"
        assert r["candidate_n_paths"] == 1024
        assert r["baseline_n_paths"] == 512
        assert r["matched_n_paths"] == 0
        assert r["delta_return_vs_L0_pp"] is None
        assert r["delta_cvar95_vs_L0_pp"] is None

    df = pd.read_csv(out_dir / "mc_expanded_key_scenarios.csv")
    sub = df[(df["n_paths"] == 1024) & (df["lifecycle_id"] != "L0")]
    assert len(sub) == 8
    assert sub["delta_return_vs_L0_pp"].isna().all()
    assert sub["delta_cvar95_vs_L0_pp"].isna().all()
    assert (sub["comparison_status"] == "COMPARISON_UNAVAILABLE").all()


# ---------------------------------------------------------------------------
# P2-2 + Section 10: Fixed-param vs reoptimized ablations & unrounded Holm
# ---------------------------------------------------------------------------


def test_p2_2_ablation_separation_and_unrounded_holm_precision():
    # Verify unrounded Holm uses raw p-values rather than pre-rounded 4-digit p-values
    raw_p = 0.00336456298828125
    adj_raw = holm_bonferroni_adjust([raw_p] + [0.5] * 12, round_digits=None)
    adj_rounded_input = holm_bonferroni_adjust([round(raw_p, 4)] + [0.5] * 12, round_digits=None)
    assert adj_raw[0] == pytest.approx(raw_p * 13, abs=1e-12)
    assert adj_raw[0] < adj_rounded_input[0]

    # Check published paired_ablations.csv strictly separates fixed-param vs reoptimized pipeline
    out_dir = REPO_ROOT / "docs" / "validation" / "p8-r11"
    if not (out_dir / "paired_ablations.csv").is_file():
        pytest.skip("P8-R11 artifacts not present")
    df = pd.read_csv(out_dir / "paired_ablations.csv")
    types = set(df["comparison_type"].unique())
    assert {
        "FIXED_PARAM_MECHANISM_ABLATION",
        "LIFECYCLE_REOPTIMIZED_PIPELINE_COMPARISON",
        "WITHIN_LIFECYCLE_STRATEGY_COMPARISON",
    }.issubset(types)

    # No B_WF / C_WF cross-lifecycle row may ever be labeled as fixed-param or F11 pure
    reopt = df[df["comparison_type"] == "LIFECYCLE_REOPTIMIZED_PIPELINE_COMPARISON"]
    assert len(reopt) > 0
    assert all("reoptimized pipeline" in c for c in reopt["comparison"])
    assert not any("F11 pure" in c for c in df["comparison"])

    fixed = df[df["comparison_type"] == "FIXED_PARAM_MECHANISM_ABLATION"]
    assert {"A_FIXED", "B2", "B5", "C5", "B_FROZEN_L0", "C_FROZEN_L0"}.issubset(
        {c.split(":")[0] for c in fixed["comparison"]}
    )

    # Verify MAINNET_ACTUAL / L2 / DELAYED C_WF - A_FIXED uses unrounded raw p-value
    c_row = df[
        (df["funding_regime"] == "MAINNET_ACTUAL")
        & (df["execution_profile"] == "DELAYED_EXIT_FILL")
        & (df["comparison"] == "L2: C_WF - A_FIXED")
    ].iloc[0]
    assert float(c_row["wilcoxon_p_raw"]) == pytest.approx(0.00336456298828125, abs=1e-9)
    assert float(c_row["delta_mean_return_raw_pp"]) == pytest.approx(1.18881859376, abs=1e-6)


# ---------------------------------------------------------------------------
# P2-3: Event-level authoritative exit-order peak monitoring (Cases A..I)
# ---------------------------------------------------------------------------


def test_p2_3_case_a_same_bar_submit_and_fill_reports_peak_1():
    """Exact independent review repro: 2 bars IDEAL submit+fill in bar 1 must report peak=1."""
    bars = _make_bars([132.89, 131.50])
    res = run_offline_lifecycle(
        bars,
        guardrails=DemoGuardrails(enable_f11=True, enable_f08=False),
        assumptions=execution_profile("IDEAL_LIMIT_FILL"),
        strategy_cap_usdt=D("7"),
        account_id="p23-case-a",
    )
    assert res["exit_orders_submitted"] == 1
    assert res["max_simultaneous_exit_orders"] == 1
    assert res["bar_end_max_simultaneous_exit_orders"] == 0
    assert res["exit_peak_monitoring_method"] == EXIT_PEAK_MONITORING_METHOD
    assert res["exit_overlap_violations"] == []


def test_p2_3_cases_b_c_d_e_f_g_h_lifecycle_peak_tracking():
    # B: Multi-bar active exit (DELAYED)
    r_b = run_offline_lifecycle(
        _make_bars([132.89, 131.50, 130.00, 129.00, 128.00, 127.00]),
        guardrails=DemoGuardrails(enable_f11=True, enable_f08=False),
        assumptions=execution_profile("DELAYED_EXIT_FILL"),
        strategy_cap_usdt=D("7"),
        account_id="p23-case-b",
    )
    assert r_b["max_simultaneous_exit_orders"] == 1
    assert r_b["exit_overlap_violations"] == []

    # C & D: Partial fill + below-min-notional dust remainder survives to completion
    r_d = run_offline_lifecycle(
        _make_bars([132.89, 131.50, 131.50, 131.50], volumes=[100.0, 4.0, 2.0, 2.0]),
        guardrails=DemoGuardrails(enable_f11=True, enable_f08=False),
        assumptions=replace(execution_profile("IDEAL_LIMIT_FILL"), use_bar_volume=True, participation=D("0.01")),
        strategy_cap_usdt=D("7"),
        account_id="p23-case-d",
    )
    assert r_d["exit_orders_partial_events"] == 1
    assert r_d["dust_protections_triggered"] >= 1
    assert r_d["residual_inventory"] == 0.0
    assert r_d["max_simultaneous_exit_orders"] == 1

    # E & F: Cancel pending -> UNKNOWN -> reconcile recovery without duplicate replacement
    # Keep price above min_notional (0.04 * 127.5 = 5.10 >= 5.0) while drifting down to trigger reprice + cancel fault
    r_f = run_offline_lifecycle(
        _make_bars([132.89, 131.50, 130.50, 129.50, 128.50, 127.50]),
        guardrails=DemoGuardrails(enable_f11=True, enable_f08=False, max_loss=None),
        assumptions=execution_profile("GAP_AND_CANCEL_FAULT"),
        strategy_cap_usdt=D("7"),
        account_id="p23-case-f",
    )
    assert r_f["exit_cancel_faults"] >= 1
    assert r_f["exit_reconcile_recoveries"] >= 1
    assert r_f["max_simultaneous_exit_orders"] == 1
    assert r_f["exit_overlap_violations"] == []

    # G: BREAKOUT and funding exit simultaneous trigger under unified F11
    b5 = FundingPolicy(
        candidate_id="B5",
        entry_max_bps_h=0.50,
        exit_mode="funding_bleed",
        exit_bps_h=1.0,
        max_hold_hours=72.0,
    )
    r_g = run_unified_lifecycle(
        _make_bars([132.89, 131.50] + [130.0] * 55),
        strategy_id="B5",
        lifecycle_id="L2",
        execution_profile_name="DELAYED_EXIT_FILL",
        funding_regime="SWITCH_48H",
        strategy_cap_usdt=D("7"),
        funding_policy=b5,
    )
    assert r_g["max_simultaneous_exit_orders"] == 1
    assert r_g["exit_overlap_violations"] == []

    # H: Continuous tracking across F08 generations
    r_h = run_offline_lifecycle(
        _make_bars([132.89, 131.50, 130.00, 128.00, 126.00, 124.00]),
        guardrails=DemoGuardrails(enable_f11=True, enable_f08=True, max_loss=None),
        assumptions=execution_profile("IDEAL_LIMIT_FILL"),
        strategy_cap_usdt=D("7"),
        account_id="p23-case-h",
    )
    assert r_h["rotations_completed"] >= 2
    assert r_h["max_simultaneous_exit_orders"] == 1
    assert r_h["exit_overlap_violations"] == []


def test_p2_3_case_i_injected_overlapping_exit_orders_detected_and_blocks_acceptance():
    """Negative test: two simultaneously active exit orders (even with cancel_pending/unknown)
    must be detected as peak=2 and block ENGINEERING_ACCEPTED."""
    injected_trace = [
        {"seq": 1, "event_time": "2026-11-01T01:00:00+00:00", "generation": 0, "client_order_id": "exit-1", "status": "OPEN"},
        {"seq": 2, "event_time": "2026-11-01T01:00:01+00:00", "generation": 0, "client_order_id": "exit-1", "status": "CANCEL_PENDING"},
        {"seq": 3, "event_time": "2026-11-01T01:00:02+00:00", "generation": 0, "client_order_id": "exit-2", "status": "OPEN"},
        {"seq": 4, "event_time": "2026-11-01T01:00:03+00:00", "generation": 0, "client_order_id": "exit-1", "status": "CANCELED"},
        {"seq": 5, "event_time": "2026-11-01T01:00:04+00:00", "generation": 0, "client_order_id": "exit-2", "status": "FILLED"},
    ]
    rec = reconstruct_exit_order_peak_from_events(injected_trace)
    assert rec["peak_simultaneous_exit_orders"] == 2
    assert len(rec["overlap_violations"]) == 1
    assert rec["overlap_violations"][0]["active_order_ids"] == ["exit-1", "exit-2"]
    assert rec["active_exit_orders_at_end"] == []

    # Unresolved UNKNOWN order remains active and overlaps with a new exit order
    unknown_trace = [
        {"seq": 1, "event_time": "2026-11-01T01:00:00+00:00", "generation": 0, "client_order_id": "exit-u1", "status": "UNKNOWN"},
        {"seq": 2, "event_time": "2026-11-01T02:00:00+00:00", "generation": 1, "client_order_id": "exit-u2", "status": "SUBMITTED"},
    ]
    rec_u = reconstruct_exit_order_peak_from_events(unknown_trace)
    assert rec_u["peak_simultaneous_exit_orders"] == 2
    assert rec_u["active_exit_orders_at_end"] == ["exit-u1", "exit-u2"]

    # Blocking ENGINEERING_ACCEPTED when overlap peak == 2
    dummy_oos = {r: {"mean_net_return_pct": -0.5} for r in ("A_FIXED", "B_WF", "C_WF")}
    hist_stub = {
        "all_zero_fifo_residual": True,
        "total_unique_window_runs": 10,
        "max_simultaneous_exit_orders": 1,
        "wf_results_index": {
            "L2__IDEAL_LIMIT_FILL__MAINNET_ACTUAL": {"oos_summary": dummy_oos},
            "L2__DELAYED_EXIT_FILL__MAINNET_ACTUAL": {"oos_summary": dummy_oos},
        },
    }
    mc_stub = {"all_zero_fifo_residual": True, "total_unique_mc_runs": 10, "max_simultaneous_exit_orders": 1}
    conc = derive_r11_conclusions(
        hist_stub,
        [{"reconciled_zero_residual": True, "max_simultaneous_exit_orders": 2, "exit_overlap_violations": rec["overlap_violations"]}],
        mc_stub,
        exit_peak_verification={
            "event_level_verified_runs": 10,
            "event_level_max_simultaneous_exit_orders": 2,
            "event_level_overlap_violations": 1,
        },
    )
    assert conc["ENGINEERING_ACCEPTED"] is False
    assert conc["invariants"]["single_authoritative_exit_order_verified"] is False


# ---------------------------------------------------------------------------
# Preexisting R10 defect: FundingSensitiveGrid entry_time_utc checkpoint recovery
# ---------------------------------------------------------------------------


def _setup_funding_grid_controller(
    tmp_path: Path,
    *,
    policy: FundingPolicy,
    funding_events: list[tuple[datetime, Decimal, Decimal, str]],
    use_f11_limit_exit: bool = True,
    deployment_id: str = "fl-r11-rec-0",
    db_name: str = "paper.db",
):
    rules = default_demo_rules()
    cfg = _demo_grid_config(
        D("132.89"),
        rules,
        max_cash_committed=D("7"),
        fee_rate=D("0.0003"),
        slippage=D("0"),
    )
    now = [datetime(2026, 11, 1, 1, 0, tzinfo=timezone.utc)]
    broker = PaperBroker(
        Market.BINANCE_USDM,
        "r11-rec",
        (Instrument("NVDAUSDT", "USDT"),),
        {"USDT": DEFAULT_DEMO_WALLET},
        fee_rate=D("0.0003"),
    )
    store = PaperStore(tmp_path / db_name)
    controller = TradingController(
        broker,
        store,
        clock=lambda: now[0],
        limits=RiskLimits(D("100"), D("100"), D("200"), 10),
    )

    def make_grid() -> FundingSensitiveGrid:
        return FundingSensitiveGrid(
            cfg,
            rules,
            deployment_id=deployment_id,
            account_id="r11-rec",
            market=Market.BINANCE_USDM,
            instrument_id="NVDAUSDT",
            initial_equity=DEFAULT_DEMO_WALLET,
            fee_rate=D("0.0003"),
            continuous_capacity=True,
            funding_policy=policy,
            funding_events=funding_events,
            use_f11_limit_exit=use_f11_limit_exit,
        )

    return broker, store, controller, now, make_grid


def test_volatility_grid_checkpoint_restores_live_window_and_next_decision(tmp_path):
    from factorlab.experiments.grid_funding_adaptive import VolatilityAdaptiveGrid, VolatilityPolicy
    broker, store, controller, now, make_funding = _setup_funding_grid_controller(
        tmp_path, policy=FundingPolicy(), funding_events=[])
    try:
        base = make_funding()
        def create():
            return VolatilityAdaptiveGrid(base.config, base.rules, deployment_id=base.deployment_id,
                account_id='r11-rec', market=Market.BINANCE_USDM, instrument_id='NVDAUSDT',
                initial_equity=DEFAULT_DEMO_WALLET, fee_rate=D('.0003'), continuous_capacity=True,
                vol_policy=VolatilityPolicy(), warmup_closes=[130, 131, 132, 133])
        grid = create()
        grid.observe_paper(controller, dict(id='v0', time=now[0].replace(tzinfo=None).isoformat(), price=D('132.89'),
            high=D('132.89'), low=D('132.89'), close=D('132.89'), volume=10000,
            completed_bar=True), delivery=1, observed_at=now[0])
        restored = create()
        restored.restore(controller)
        assert restored.close_history == grid.close_history
        assert restored._subclass_checkpoint() == grid._subclass_checkpoint()
        event = dict(id='v1', time=(now[0]+timedelta(hours=1)).replace(tzinfo=None).isoformat(),
            price=D(134), high=D(134), low=D(134), close=D(134), volume=10000, completed_bar=True)
        # Compare staged next decisions without committing competing checkpoints.
        from factorlab.strategies.grid_execution import _observation
        for candidate in (grid, restored):
            candidate._observe(_observation(event), candidate.account)
        assert restored._subclass_checkpoint() == grid._subclass_checkpoint()
        assert restored.prices == grid.prices
    finally:
        store.close()


def test_r10_funding_grid_checkpoint_recovery_matches_continuous_and_handles_partial_and_age(tmp_path: Path):
    """Continuous run vs checkpoint-restored instance must behave identically across:
    partial fills, funding spike exit, age exit, full exit cleanup, and F08 rotation."""
    t0 = datetime(2026, 11, 1, 0, 0, tzinfo=timezone.utc)
    policy = FundingPolicy(
        candidate_id="B5",
        lookback_hours=24,
        entry_max_bps_h=0.50,
        exit_mode="funding_bleed",
        exit_bps_h=1.0,
        max_hold_hours=4.0,
    )
    # Normal funding at hour 1..2, spike at hour 3
    funding_events = [
        (t0 + timedelta(hours=1, minutes=30), D("131.56"), D("0.00001"), "Low"),
        (t0 + timedelta(hours=2, minutes=30), D("131.56"), D("0.00001"), "Low"),
        (t0 + timedelta(hours=3, minutes=30), D("131.56"), D("0.00200"), "Spike"),
    ]

    def run_scenario(*, restart_after_partial: bool, db_name: str):
        broker, store, controller, now, make_grid = _setup_funding_grid_controller(
            tmp_path, policy=policy, funding_events=funding_events, db_name=db_name
        )
        try:
            grid = make_grid()
            # Bar 0: observe at 132.89 -> propose BUY at lower (131.56)
            now[0] = t0 + timedelta(hours=1)
            grid.observe_paper(
                controller,
                {"id": "b0", "time": t0.replace(tzinfo=None).isoformat(), "price": D("132.89"), "high": D("132.89"), "low": D("132.89"), "close": D("132.89"), "volume": 10000, "completed_bar": True},
                delivery=grid.delivery + 1,
                observed_at=now[0],
            )
            buy_id = next(iter(grid.orders))
            grid.submit_paper(controller, buy_id, delivery=grid.delivery + 1)

            # Bar 1: first partial fill (0.02 of 0.04) inside band via authoritative trade report
            now[0] = t0 + timedelta(hours=2)
            t_first_fill = now[0]
            ev1 = OrderEvent(
                client_order_id=buy_id,
                exchange_order_id=f"paper:{buy_id}",
                status=OrderStatus.PARTIALLY_FILLED,
                cumulative_filled_quantity=D("0.02"),
                fill_price=D("131.56"),
                event_time=t_first_fill,
                source="test-partial",
                event_id=f"pf1:{buy_id}",
                cumulative_fee=D("0.02") * D("131.56") * D("0.0003"),
            )
            grid.confirm_paper(controller, ev1, delivery=grid.delivery + 1)
            assert grid.entry_time_utc == t_first_fill

            if restart_after_partial:
                # Re-instantiate and restore from checkpoint!
                grid = make_grid()
                grid.restore(controller)
                assert grid.entry_time_utc == t_first_fill
                assert grid.entry_time_unrecoverable is False

            # Bar 2: second partial fill (remaining 0.02 -> FILLED) MUST NOT reset entry_time_utc
            now[0] = t0 + timedelta(hours=3)
            ev2 = OrderEvent(
                client_order_id=buy_id,
                exchange_order_id=f"paper:{buy_id}",
                status=OrderStatus.FILLED,
                cumulative_filled_quantity=D("0.04"),
                fill_price=D("131.56"),
                event_time=now[0],
                source="test-partial",
                event_id=f"pf2:{buy_id}",
                cumulative_fee=D("0.04") * D("131.56") * D("0.0003"),
            )
            grid.confirm_paper(controller, ev2, delivery=grid.delivery + 1)
            assert grid.entry_time_utc == t_first_fill

            # Bar 3: observe funding spike (still inside band at 131.56) -> FUNDING_SPIKE_EXIT
            now[0] = t0 + timedelta(hours=4)
            grid.observe_paper(
                controller,
                {"id": "b3", "time": (t0 + timedelta(hours=3)).replace(tzinfo=None).isoformat(), "price": D("131.56"), "high": D("131.56"), "low": D("131.56"), "close": D("131.56"), "volume": 10000, "completed_bar": True},
                delivery=grid.delivery + 1,
                observed_at=now[0],
            )
            state_after_spike = (grid.state, grid.reason, list(grid.exit_reasons), grid.entry_time_utc)

            # Restart again after funding trigger, then execute full F11 exit & verify cleanup + F08 rotation
            if restart_after_partial:
                grid = make_grid()
                grid.restore(controller)
                assert grid.state == "PAUSED"
                assert grid.reason == "FUNDING_SPIKE_EXIT"
                assert grid.entry_time_utc == t_first_fill

            # Withdraw any unsubmitted reverse sell proposals and execute F11 limit exit
            unsub = [o.intent.client_order_id for o in grid.active if o.intent.client_order_id not in broker.state.orders]
            if unsub:
                grid.withdraw_unsubmitted_paper(controller, unsub, delivery=grid.delivery + 1)
            now[0] = t0 + timedelta(hours=5)
            exit_intent = grid.propose_limit_exit_paper(
                controller,
                {"price": D("131.56"), "quantity": D("0.04")},
                delivery=grid.delivery + 1,
                quote=Quote("NVDAUSDT", D("131.56"), now[0]),
            )
            grid.submit_paper(controller, exit_intent.client_order_id, delivery=grid.delivery + 1)
            grid.fill_paper(controller, exit_intent.client_order_id, D("0.04"), D("131.56"), delivery=grid.delivery + 1)
            assert grid.state == "EXITED"
            assert grid.entry_time_utc is None
            assert grid.entry_price_dec is None

            # Rotate to clean new F08 generation
            rules = default_demo_rules()
            cfg2 = _demo_grid_config(D("131.56"), rules, max_cash_committed=D("7"), fee_rate=D("0.0003"), slippage=D("0"))
            gen1 = FundingSensitiveGrid(
                cfg2,
                rules,
                deployment_id="fl-r11-rec-1",
                account_id="r11-rec",
                market=Market.BINANCE_USDM,
                instrument_id="NVDAUSDT",
                initial_equity=grid.account.equity,
                fee_rate=D("0.0003"),
                continuous_capacity=True,
                funding_policy=policy,
                funding_events=funding_events,
                use_f11_limit_exit=True,
            )
            grid.rotate_paper(controller, gen1)
            assert gen1.entry_time_utc is None
            return state_after_spike
        finally:
            store.db.close()

    cont_res = run_scenario(restart_after_partial=False, db_name="cont.db")
    rest_res = run_scenario(restart_after_partial=True, db_name="rest.db")
    assert cont_res == rest_res
    assert cont_res[0] == "PAUSED"
    assert cont_res[1] == "FUNDING_SPIKE_EXIT"


def test_r10_legacy_checkpoint_recovery_from_events_and_unrecoverable_never_fabricates(tmp_path: Path):
    """Legacy checkpoint missing subclass_state:
    1. Reconstructs entry_time_utc strictly from authoritative PaperStore events when available.
    2. Never fabricates current time or zero age when authoritative events cannot explain inventory.
    3. Preserves cash, inventory, and fill records untouched.
    """
    t0 = datetime(2026, 11, 1, 0, 0, tzinfo=timezone.utc)
    policy = FundingPolicy(
        candidate_id="B5",
        lookback_hours=24,
        entry_max_bps_h=0.50,
        exit_mode="funding_bleed",
        exit_bps_h=10.0,
        max_hold_hours=3.0,
    )
    funding_events = [
        (t0 + timedelta(hours=h, minutes=30), D("131.56"), D("0.00002"), "Mild")
        for h in range(1, 10)
    ]
    broker, store, controller, now, make_grid = _setup_funding_grid_controller(
        tmp_path, policy=policy, funding_events=funding_events, db_name="legacy.db"
    )
    try:
        grid = make_grid()
        now[0] = t0 + timedelta(hours=1)
        grid.observe_paper(
            controller,
            {"id": "b0", "time": t0.replace(tzinfo=None).isoformat(), "price": D("132.89"), "high": D("132.89"), "low": D("132.89"), "close": D("132.89"), "volume": 10000, "completed_bar": True},
            delivery=grid.delivery + 1,
            observed_at=now[0],
        )
        buy_id = next(iter(grid.orders))
        grid.submit_paper(controller, buy_id, delivery=grid.delivery + 1)
        now[0] = t0 + timedelta(hours=2)
        t_fill = now[0]
        grid.fill_paper(controller, buy_id, D("0.04"), D("131.56"), delivery=grid.delivery + 1)

        cash_before = dict(broker.state.cash)
        inv_before = broker.state.positions["NVDAUSDT"].quantity

        # Strip subclass_state to simulate a legacy R10 checkpoint (re-seal fingerprint legally)
        legacy_payload = dict(broker.state.runtime[_CHECKPOINT]["checkpoint"])
        legacy_payload.pop("subclass_state", None)
        broker.state.runtime[_CHECKPOINT] = {
            "revision": _fingerprint(legacy_payload),
            "checkpoint": legacy_payload,
        }

        restored = make_grid()
        restored.restore(controller)
        assert restored.entry_time_utc == t_fill
        assert restored.entry_time_unrecoverable is False
        assert broker.state.cash == cash_before
        assert broker.state.positions["NVDAUSDT"].quantity == inv_before

        # Long holding age triggers FUNDING_AGE_EXIT at t0 + 6h (hold_h >= 3.0)
        now[0] = t0 + timedelta(hours=6)
        restored.observe_paper(
            controller,
            {"id": "b5", "time": (t0 + timedelta(hours=5)).replace(tzinfo=None).isoformat(), "price": D("131.56"), "high": D("131.56"), "low": D("131.56"), "close": D("131.56"), "volume": 10000, "completed_bar": True},
            delivery=restored.delivery + 1,
            observed_at=now[0],
        )
        assert restored.state == "PAUSED"
        assert restored.reason == "FUNDING_AGE_EXIT"

        # Unrecoverable case: baseline > all fill cursors -> must NOT fabricate now or zero age
        unrec_grid = make_grid()
        unrec_grid._baseline = 999_999
        rec_t, rec_p = unrec_grid._recover_entry_from_authoritative_events(broker, store, inv_before)
        assert rec_t is None and rec_p is None
    finally:
        store.db.close()
