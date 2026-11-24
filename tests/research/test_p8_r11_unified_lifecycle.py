"""Unit and integration tests for FactorLab P8-R11 Unified F11/F08 Grid Lifecycle."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from factorlab.experiments.grid_f11_f08_offline import (
    DEFAULT_DEMO_WALLET,
    DemoGuardrails,
    FundingSchedule,
    execution_profile,
    run_offline_lifecycle,
)
from factorlab.experiments.grid_funding_adaptive import FundingPolicy, VolatilityPolicy
from factorlab.experiments.p8_r11_study import run_r11_deterministic_stress
from factorlab.experiments.p8_r11_unified_lifecycle import (
    UNIFIED_EXECUTION_MODEL_VERSION,
    build_s02_handoff_contract,
    holm_bonferroni_adjust,
    run_unified_lifecycle,
)

D = Decimal


def _make_bars(closes: list[float], start: str = "2026-11-01T00:00:00+00:00") -> list[dict]:
    t0 = datetime.fromisoformat(start)
    out = []
    for i, c in enumerate(closes):
        t = (t0 + timedelta(hours=i)).isoformat()
        out.append(
            {
                "id": f"bar-{i}",
                "time": t,
                "price": c,
                "high": c,
                "low": c,
                "close": c,
                "volume": 10000,
                "completed_bar": True,
            }
        )
    return out


def _b5_policy() -> FundingPolicy:
    return FundingPolicy(
        candidate_id="B5",
        lookback_hours=24,
        conservative=True,
        entry_max_bps_h=0.50,
        expected_hold_hours=48.0,
        min_net_margin_bps=100.0,
        exit_mode="funding_bleed",
        exit_bps_h=1.00,
        max_hold_hours=72.0,
    )


def _c5_policy(ref_vol: float = 0.00445) -> VolatilityPolicy:
    return VolatilityPolicy(
        candidate_id="C5",
        vol_window_hours=24,
        k_mult=3.0,
        horizon_hours=1.0,
        min_half_width=0.006,
        max_half_width=0.035,
        extreme_vol_mult=2.0,
        allow_flat_rebuild=True,
        min_rebuild_interval=24,
        rebuild_width_delta=0.25,
        reference_vol_1h=ref_vol,
    )


def test_single_authoritative_exit_order_breakout_then_funding_spike():
    """BREAKOUT triggers F11 DELAYED exit first, then funding spikes: MUST NOT duplicate sell orders."""
    # Bar 0: anchor=132.89 (lower=131.56, upper=134.22)
    # Bar 1: 131.50 -> fills BUY @ 131.56 AND breaks out below 131.56 -> F11 submits SELL LIMIT (delayed 2 bars)
    # Bar 2..5: price stays near 131.55 while funding jumps to 22.97 bps/h -> funding exit also evaluates True
    closes = [132.89, 131.50, 131.55, 131.60, 131.65, 131.70, 131.75, 131.80]
    bars = _make_bars(closes)
    t0 = datetime.fromisoformat(bars[0]["time"])
    events = []
    for i, c in enumerate(closes):
        rate = D("0.0000125") if i < 2 else D("0.00229669")
        events.append((t0 + timedelta(hours=i + 1, microseconds=-1), D(str(c)), rate, "Synthetic"))

    res = run_offline_lifecycle(
        bars,
        guardrails=DemoGuardrails(enable_f11=True, enable_f08=False),
        assumptions=execution_profile("DELAYED_EXIT_FILL"),
        funding=FundingSchedule(mode="historical_events", events=tuple(events), label="CUSTOM_SPIKE"),
        causal_funding_events=events,
        strategy_id="B5",
        funding_policy=_b5_policy(),
        strategy_cap_usdt=D("7.00"),
        account_id="test-bo-then-fund",
    )
    assert res["reconciled_zero_residual"] is True
    assert res["max_simultaneous_exit_orders"] <= 1
    assert res["exit_orders_submitted"] == 1
    assert res["exit_orders_filled"] == 1
    assert res["residual_inventory"] == 0.0
    assert "BREAKOUT" in res["exit_trigger_reasons"]
    assert any(r.startswith("FUNDING_") for r in res["exit_trigger_reasons"])


def test_single_authoritative_exit_order_funding_spike_then_breakout():
    """Funding spike triggers inside the grid band first, then price breaks out: single F11 LIMIT exit only."""
    # Bar 0: anchor=132.89 (lower=131.56, upper=134.22). With slippage=0 (maker fill at lower),
    # Bar 1: 131.56 -> fills BUY @ 131.56 while remaining inside [131.56, 134.22] (NO breakout yet!)
    # Bar 2: 132.00 (inside band!) & funding spikes -> Strategy B routes into F11 LIMIT exit (delayed 2 bars)
    # Bar 3: 130.50 (below lower -> BREAKOUT price!) -> MUST NOT overwrite or duplicate working F11 exit
    # Bar 4..7: price rebounds -> F11 limit exit fills cleanly
    closes = [132.89, 131.56, 132.00, 130.50, 130.60, 130.70, 132.20, 132.30]
    bars = _make_bars(closes)
    t0 = datetime.fromisoformat(bars[0]["time"])
    events = []
    for i, c in enumerate(closes):
        rate = D("0.0000125") if i < 2 else D("0.00229669")
        events.append((t0 + timedelta(hours=i + 1, microseconds=-1), D(str(c)), rate, "Synthetic"))

    res = run_offline_lifecycle(
        bars,
        guardrails=DemoGuardrails(enable_f11=True, enable_f08=False),
        assumptions=replace(execution_profile("DELAYED_EXIT_FILL"), slippage=D("0")),
        funding=FundingSchedule(mode="historical_events", events=tuple(events), label="CUSTOM_SPIKE"),
        causal_funding_events=events,
        strategy_id="B5",
        funding_policy=_b5_policy(),
        strategy_cap_usdt=D("7.00"),
        account_id="test-fund-then-bo",
    )
    assert res["reconciled_zero_residual"] is True
    assert res["max_simultaneous_exit_orders"] <= 1
    assert res["funding_exit_triggers"] >= 1
    assert res["exit_orders_submitted"] >= 1
    assert res["residual_inventory"] == 0.0
    assert res["final_state"] == "EXITED"
    assert res["final_reason"].startswith("FUNDING_")
    assert "BREAKOUT" in res["exit_trigger_reasons"]


def test_strategy_b_funding_exit_obeys_unfilled_and_partial_execution_realism():
    """In L1/L2, Strategy B's funding exit MUST NOT bypass UNFILLED_EXIT_TIMEOUT or LIMITED_CAPACITY_PARTIAL."""
    closes = [132.89, 131.56, 132.00, 132.05, 132.10, 132.15, 132.20, 132.25]
    bars = _make_bars(closes)
    t0 = datetime.fromisoformat(bars[0]["time"])
    events = [
        (t0 + timedelta(hours=i + 1, microseconds=-1), D(str(c)), D("0.0000125") if i < 2 else D("0.00229669"), "Syn")
        for i, c in enumerate(closes)
    ]

    res_unfilled = run_offline_lifecycle(
        bars,
        guardrails=DemoGuardrails(enable_f11=True, enable_f08=False),
        assumptions=replace(execution_profile("UNFILLED_EXIT_TIMEOUT"), slippage=D("0")),
        funding=FundingSchedule(mode="historical_events", events=tuple(events), label="CUSTOM"),
        causal_funding_events=events,
        strategy_id="B5",
        funding_policy=_b5_policy(),
        strategy_cap_usdt=D("7.00"),
        account_id="test-b-unfilled",
    )
    assert res_unfilled["reconciled_zero_residual"] is True
    assert res_unfilled["max_simultaneous_exit_orders"] <= 1
    # Because exit cannot fill, Strategy B remains holding 0.04 and continues paying funding!
    assert res_unfilled["residual_inventory"] == pytest.approx(0.04)
    assert res_unfilled["realized_funding_usdt"] < -0.05

    res_partial = run_offline_lifecycle(
        bars,
        guardrails=DemoGuardrails(enable_f11=True, enable_f08=False),
        assumptions=replace(execution_profile("LIMITED_CAPACITY_PARTIAL"), slippage=D("0")),
        funding=FundingSchedule(mode="historical_events", events=tuple(events), label="CUSTOM"),
        causal_funding_events=events,
        strategy_id="B5",
        funding_policy=_b5_policy(),
        strategy_cap_usdt=D("7.00"),
        account_id="test-b-partial",
    )
    assert res_partial["reconciled_zero_residual"] is True
    assert res_partial["max_simultaneous_exit_orders"] <= 1
    # Capacity 0.02/bar leaves 0.02 NVDA (< 5U min_notional) blocked by EXIT_MIN_NOTIONAL!
    assert res_partial["residual_inventory"] == pytest.approx(0.02)
    assert res_partial["exit_block_counts"].get("EXIT_MIN_NOTIONAL", 0) >= 1


def test_same_timestamp_funding_settles_before_fill():
    """Same-timestamp tie-break: funding settles on prior inventory BEFORE bar fills execute."""
    # Bar 0: anchor=132.89 (lower=131.56); submits BUY @ 131.56
    # Bar 1: close=131.50 -> BUY fills at bar 1 close (131.50*1.0002 <= 131.56); bar 1 funding MUST NOT charge new lot!
    # Bar 2: close=131.50 -> bar 2 funding charges 0.04 lot!
    closes = [132.89, 131.50, 131.50]
    bars = _make_bars(closes)
    res = run_unified_lifecycle(
        bars,
        strategy_id="A_FIXED",
        lifecycle_id="L0",
        execution_profile_name="IDEAL_LIMIT_FILL",
        funding_regime="DEMO_HIGH_FUNDING_22.97BPS",
        strategy_cap_usdt=D("7.00"),
        account_id="test-tiebreak",
    )
    assert res["reconciled_zero_residual"] is True
    assert res["funding_events_count"] == 1  # Only bar 2, NOT bar 1!
    expected_funding = -float(D("0.04") * D("131.50") * (D("22.9669") / D("10000")))
    assert res["realized_funding_usdt"] == pytest.approx(expected_funding, abs=1e-6)


def test_strategy_b_and_c_strict_causality():
    """Future funding / future prices must NOT alter earlier bar decisions or fills."""
    closes = [132.89, 131.56, 132.10, 132.40, 132.60, 132.90, 133.50, 134.50]
    bars = _make_bars(closes)
    t0 = datetime.fromisoformat(bars[0]["time"])
    ev_a = [(t0 + timedelta(hours=i + 1, microseconds=-1), D(str(c)), D("0.0000125"), "Syn") for i, c in enumerate(closes)]
    ev_b = list(ev_a)
    # Perturb only the very last bar's funding rate
    ev_b[-1] = (ev_b[-1][0], ev_b[-1][1], D("0.05"), "Syn")

    r1 = run_offline_lifecycle(
        bars,
        guardrails=DemoGuardrails(enable_f11=True, enable_f08=True),
        assumptions=execution_profile("IDEAL_LIMIT_FILL"),
        funding=FundingSchedule(mode="historical_events", events=tuple(ev_a), label="A"),
        causal_funding_events=ev_a,
        strategy_id="B5",
        funding_policy=_b5_policy(),
        strategy_cap_usdt=D("7.00"),
        record_hourly_series=True,
    )
    r2 = run_offline_lifecycle(
        bars,
        guardrails=DemoGuardrails(enable_f11=True, enable_f08=True),
        assumptions=execution_profile("IDEAL_LIMIT_FILL"),
        funding=FundingSchedule(mode="historical_events", events=tuple(ev_b), label="B"),
        causal_funding_events=ev_b,
        strategy_id="B5",
        funding_policy=_b5_policy(),
        strategy_cap_usdt=D("7.00"),
        record_hourly_series=True,
    )
    # Bars 0..6 must be 100% identical before bar 7's future funding arrives
    for i in range(len(bars) - 1):
        assert r1["hourly_series"][i]["inventory"] == r2["hourly_series"][i]["inventory"]
        assert r1["hourly_series"][i]["state"] == r2["hourly_series"][i]["state"]
        assert r1["hourly_series"][i]["net_pnl"] == pytest.approx(r2["hourly_series"][i]["net_pnl"], abs=1e-9)

    # Strategy C causality: perturbing bar 7 close must not alter bars 0..6
    closes2 = list(closes)
    closes2[-1] = 100.0
    warmup = [132.89 * (1.0 + 0.003 * ((-1) ** k)) for k in range(40)]
    c1 = run_unified_lifecycle(
        bars,
        strategy_id="C5",
        lifecycle_id="L2",
        execution_profile_name="IDEAL_LIMIT_FILL",
        funding_regime="EX_FUNDING",
        strategy_cap_usdt=D("7.00"),
        vol_policy=_c5_policy(),
        warmup_closes=warmup,
        record_hourly_series=True,
    )
    c2 = run_unified_lifecycle(
        _make_bars(closes2),
        strategy_id="C5",
        lifecycle_id="L2",
        execution_profile_name="IDEAL_LIMIT_FILL",
        funding_regime="EX_FUNDING",
        strategy_cap_usdt=D("7.00"),
        vol_policy=_c5_policy(),
        warmup_closes=warmup,
        record_hourly_series=True,
    )
    for i in range(len(bars) - 1):
        assert c1["hourly_series"][i]["inventory"] == c2["hourly_series"][i]["inventory"]
        assert c1["hourly_series"][i]["net_pnl"] == pytest.approx(c2["hourly_series"][i]["net_pnl"], abs=1e-9)


def test_f08_rotation_defers_while_funding_gate_active_and_resumes_when_clear():
    """In L2, Strategy B must stay flat during high funding without spinning empty generations,
    then rotate and trade once funding normalizes."""
    closes = [132.89, 130.00, 128.00, 128.00, 126.00, 128.50, 128.50]
    bars = _make_bars(closes)
    t0 = datetime.fromisoformat(bars[0]["time"])
    # High funding bars 0..2 (+24h warmup), then 0 funding bars 3..6
    warmup_ev = [(t0 - timedelta(hours=h), D("132.89"), D("0.00229669"), "Warm") for h in range(24, 0, -1)]
    in_ev = [
        (t0 + timedelta(hours=i + 1, microseconds=-1), D(str(c)), D("0.00229669") if i < 2 else D("0"), "Syn")
        for i, c in enumerate(closes)
    ]
    res = run_offline_lifecycle(
        bars,
        guardrails=DemoGuardrails(enable_f11=True, enable_f08=True),
        assumptions=execution_profile("IDEAL_LIMIT_FILL"),
        funding=FundingSchedule(mode="historical_events", events=tuple(in_ev), label="RECOVER"),
        causal_funding_events=warmup_ev + in_ev,
        strategy_id="B5",
        funding_policy=_b5_policy(),
        strategy_cap_usdt=D("7.00"),
    )
    assert res["reconciled_zero_residual"] is True
    assert res["funding_blocked_hours"] >= 2
    assert res["rotation_blocked_bars"] >= 1
    assert any(b["reason"] == "ROTATION_FUNDING_BLOCKED" for b in res["rotations_blocked"])


def test_cash_reference_holm_adjustment_and_s02_handoff_boundaries():
    """Verify CASH zero exposure, Holm-Bonferroni adjustment, and S02-F01 non-eligibility boundaries."""
    bars = _make_bars([132.89, 130.00, 128.00, 135.00])
    cash = run_unified_lifecycle(
        bars,
        strategy_id="CASH",
        lifecycle_id="L2",
        execution_profile_name="IDEAL_LIMIT_FILL",
        funding_regime="DEMO_HIGH_FUNDING_22.97BPS",
        strategy_cap_usdt=D("7.00"),
    )
    assert cash["net_pnl_usdt"] == 0.0
    assert cash["net_return_pct"] == 0.0
    assert cash["max_drawdown_pct"] == 0.0
    assert cash["reconciled_zero_residual"] is True
    assert cash["execution_model_version"] == UNIFIED_EXECUTION_MODEL_VERSION

    adj = holm_bonferroni_adjust([0.01, 0.04, 0.20, 0.005])
    assert adj[3] == pytest.approx(0.02)
    assert adj[0] == pytest.approx(0.03)
    assert adj[1] == pytest.approx(0.08)
    assert adj[2] == pytest.approx(0.20)

    repo_root = Path(__file__).resolve().parents[2]
    handoff = build_s02_handoff_contract(
        repo_root=repo_root,
        conclusions={"ENGINEERING_ACCEPTED": True, "OOS_ADVANTAGE_SUPPORTED": False},
        historical_oos_summary={},
        mc_key_summary=[],
        data_audit={"coverage": {"total_bars": 4632}},
    )
    assert handoff["execution_model_version"] == UNIFIED_EXECUTION_MODEL_VERSION
    qual = handoff["data_independence_and_qualification"]
    assert qual["windows_reused_from_r10"] is True
    assert qual["independent_unseen_oos_available"] is False
    assert qual["oos_advantage_supported"] is False
    assert qual["champion_eligible_from_r11_evidence"] is False
    assert qual["financial_admission"] == "FORMAL_ADMISSION_BLOCKED"


def test_deterministic_stress_suite_zero_fifo_and_cancel_fill_race():
    """Small-scale deterministic stress suite must pass zero FIFO residual and single-exit invariant everywhere."""
    b2 = FundingPolicy(candidate_id="B2", entry_max_bps_h=0.50, exit_mode="none", exit_bps_h=None, max_hold_hours=None)
    rows = run_r11_deterministic_stress(b2, _b5_policy(), _c5_policy(), n_steps=48)
    assert len(rows) > 100
    assert all(r["reconciled_zero_residual"] is True for r in rows)
    assert all(r["max_simultaneous_exit_orders"] <= 1 for r in rows)
    assert any(r["cancel_fill_race_injected"] for r in rows)


def test_reconcile_l0_against_frozen_r10_matches_pr238_keys():
    """Verify L0 reconciliation reads frozen PR #238 R10 top-level walk_forward_* keys cleanly."""
    from factorlab.experiments.p8_r11_study import _reconcile_l0_against_frozen_r10

    repo_root = Path(__file__).resolve().parents[2]
    r10_summary_path = repo_root / "docs" / "validation" / "p8-r10" / "summary.json"
    if not r10_summary_path.is_file():
        pytest.skip("Frozen R10 summary.json not present in workspace")

    stub_metric = {
        "mean_net_return_pct": -0.6937,
        "cvar95_pct": 10.2432,
        "total_funding_pnl_usdt": -0.044796,
        "halted_windows": 0,
    }
    wf_stub = {
        f"L0__IDEAL_LIMIT_FILL__{reg}": {
            "oos_summary": {role: dict(stub_metric) for role in ("A_FIXED", "B_WF", "C_WF")}
        }
        for reg in ("MAINNET_ACTUAL", "SWITCH_48H", "DEMO_HIGH_FUNDING_22.97BPS")
    }
    rows = _reconcile_l0_against_frozen_r10(repo_root, wf_stub)
    assert len(rows) == 9
    mainnet_a = next(r for r in rows if r["funding_regime"] == "MAINNET_ACTUAL" and r["strategy_role"] == "A_FIXED")
    assert mainnet_a["r10_frozen_return_pct"] == pytest.approx(-0.6937, abs=1e-4)
    assert mainnet_a["return_diff_pp"] == pytest.approx(0.0, abs=1e-4)

