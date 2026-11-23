"""Deterministic tests for P8-R09 offline F11/F08/F10 lifecycle and paired risk comparison."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

import numpy as np
import pytest

from factorlab.experiments.grid_f11_f08_offline import (
    DEFAULT_DEMO_WALLET,
    DEFAULT_STRATEGY_CAP,
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
from factorlab.experiments.grid_monte_carlo import close_bars, stress_paths
from factorlab.strategies.grid import GridConfig
from factorlab.strategies.grid_execution import GridExecution, project_paper_account
from factorlab.trading.contracts import FundingEvent, Market, OrderStatus as S, OrderType, Side, ZERO, funding_event_id
from factorlab.trading.control import RiskLimits, TradingController
from factorlab.trading.paper import Instrument, PaperBroker, Quote
from factorlab.trading.storage import PaperStore


def _bars_from_prices(prices: list[float], *, start: str = "2026-08-13T00:00:00+00:00"):
    return close_bars(np.asarray(prices, dtype=float), start)


def test_grid_execution_paper_f11_funding_unknown_and_f08_rotation(tmp_path):
    """Verify every new Paper-side hook on GridExecution directly against a single PaperStore."""
    start = datetime(2026, 8, 13, 0, 59, 59, 999999, tzinfo=timezone.utc)
    clock = [start]
    wallet = D("1000.00000000")
    rules = default_demo_rules()
    cfg = GridConfig(
        mode="arithmetic", lower=129.59, upper=132.21, levels=1,
        initial_cash=1000.0, initial_inventory=0, initial_price=130.90,
        quantity=1, max_inventory=1, max_cash_committed=7.0,
        breakout="pause", max_drawdown=0.9, fee_bps=0, slippage_bps=0,
    )
    broker = PaperBroker(
        Market.BINANCE_USDM, "acct-1", (Instrument("NVDAUSDT", "USDT"),),
        {"USDT": wallet}, fee_rate=ZERO,
    )
    store = PaperStore(tmp_path / "paper.sqlite3", continuous_capacity=True)
    try:
        ctrl = TradingController(broker, store, clock=lambda: clock[0], limits=RiskLimits(D("7"), D("7"), D("7"), 20))
        g0 = GridExecution(
            cfg, rules, deployment_id="fl-test-g", account_id="acct-1",
            market=Market.BINANCE_USDM, instrument_id="NVDAUSDT",
            initial_equity=wallet, fee_rate=ZERO, continuous_capacity=True,
        )
        # 1. Seed & submit BUY
        g0.observe_paper(ctrl, dict(id="b0", time="2026-08-13T00:00:00", price=130.90, scenario="CLOSE_ONLY_SCENARIO", completed_bar=True), delivery=1, observed_at=clock[0])
        buy_id = next(iter(g0.orders))
        g0.submit_paper(ctrl, buy_id, delivery=2)
        # 2. Fill BUY
        clock[0] += timedelta(hours=1)
        g0.observe_paper(ctrl, dict(id="b1", time="2026-08-13T01:00:00", price=129.59, scenario="CLOSE_ONLY_SCENARIO", completed_bar=True), delivery=3, observed_at=clock[0])
        fill_ev = g0.fill_paper(ctrl, buy_id, D("0.04"), D("129.59"), delivery=4)
        assert fill_ev is not None and fill_ev.status == S.FILLED
        # 3. Book in-ledger FundingEvent atomically
        fev = FundingEvent(
            account_id="acct-1", tran_id="t1", symbol="NVDAUSDT", asset="USDT",
            income_type="FUNDING_FEE", amount=D("-0.01500000"),
            event_time=clock[0], source="test", event_id=funding_event_id("FUNDING_FEE", "t1"),
        )
        assert g0.apply_funding_paper(ctrl, fev, delivery=5) is True
        assert broker.state.cash["USDT"] == wallet - D("0.01500000")
        # 4. Breakout & withdraw unsubmitted reverse SELL
        clock[0] += timedelta(hours=1)
        g0.observe_paper(ctrl, dict(id="b2", time="2026-08-13T02:00:00", price=132.89, scenario="CLOSE_ONLY_SCENARIO", completed_bar=True), delivery=6, observed_at=clock[0])
        assert g0.closed_by_breakout is True and g0.state == "PAUSED"
        assert not g0.active  # unsubmitted reverse SELL was discarded on breakout cancel
        # 5. Propose reduce-only LIMIT exit, mark UNKNOWN -> reconcile -> cancel -> replace -> fill
        exit1 = g0.propose_limit_exit_paper(ctrl, {"price": D("132.89"), "quantity": D("0.04")}, delivery=7)
        assert exit1.reduce_only is True and exit1.order_type == OrderType.LIMIT
        g0.submit_paper(ctrl, exit1.client_order_id, delivery=8)
        g0.mark_unknown_paper(ctrl, exit1.client_order_id, delivery=9)
        assert g0.orders[exit1.client_order_id].status == S.UNKNOWN
        assert broker.state.reconciled is False
        g0.reconcile_paper(ctrl, delivery=10)
        assert broker.state.reconciled is True
        assert g0.orders[exit1.client_order_id].status == S.SUBMITTED
        g0.cancel_exit_paper(ctrl, exit1.client_order_id, delivery=11)
        g0.cancel_paper(ctrl, exit1.client_order_id, delivery=12)
        exit2 = g0.propose_limit_exit_paper(
            ctrl, {"price": D("132.80"), "quantity": D("0.04")}, delivery=13,
            quote=Quote("NVDAUSDT", D("132.80"), clock[0]),
        )
        g0.submit_paper(ctrl, exit2.client_order_id, delivery=14)
        ex_fill = g0.fill_paper(ctrl, exit2.client_order_id, D("0.04"), D("132.80"), delivery=15)
        assert ex_fill is not None and ex_fill.status == S.FILLED
        assert g0.state == "EXITED" and g0.reason == "BREAKOUT"
        # 6. F08 rotate_paper to Generation 1 on the SAME shared controller/store
        acct = project_paper_account(ctrl, "NVDAUSDT")
        assert acct.inventory == ZERO
        expected_eq = wallet + D("0.04") * (D("132.80") - D("129.59")) - D("0.01500000")
        assert acct.equity == expected_eq
        cfg1 = GridConfig(
            mode="arithmetic", lower=131.47, upper=134.13, levels=1,
            initial_cash=float(acct.equity), initial_inventory=0, initial_price=132.80,
            quantity=1, max_inventory=1, max_cash_committed=7.0,
            breakout="pause", max_drawdown=0.9, fee_bps=0, slippage_bps=0,
        )
        g1 = GridExecution(
            cfg1, rules, deployment_id="fl-test-g-00001", account_id="acct-1",
            market=Market.BINANCE_USDM, instrument_id="NVDAUSDT",
            initial_equity=acct.equity, fee_rate=ZERO, continuous_capacity=True,
        )
        g0.rotate_paper(ctrl, g1)
        clock[0] += timedelta(hours=1)
        g1.observe_paper(ctrl, dict(id="b3", time="2026-08-13T03:00:00", price=132.80, scenario="CLOSE_ONLY_SCENARIO", completed_bar=True), delivery=1, observed_at=clock[0])
        g1_buy = next(iter(g1.orders))
        assert g1_buy == "fl-test-g-00001-00000001"
        g1.submit_paper(ctrl, g1_buy, delivery=2)
        assert len(broker.state.runtime["grid_rotation_history"]) == 1
    finally:
        store.db.close()


def test_offline_full_lifecycle_f11_exit_f08_rotation_and_zero_fifo_residual():
    """Verify full 10-step lifecycle: BUY -> funding -> BREAKOUT -> F11 exit -> F08 rotation -> Gen 1 fill."""
    prices = [132.89, 131.40, 131.50, 134.50, 134.40, 133.00, 135.90, 135.50]
    bars = _bars_from_prices(prices)
    res_a = run_offline_lifecycle(
        bars,
        guardrails=DemoGuardrails(enable_f11=False, enable_f08=False),
        assumptions=execution_profile("IDEAL_LIMIT_FILL"),
        funding=FundingSchedule("constant_hourly_bps", 22.9669, label="DEMO_STRESS"),
    )
    res_b = run_offline_lifecycle(
        bars,
        guardrails=DemoGuardrails(enable_f11=True, enable_f08=True),
        assumptions=execution_profile("IDEAL_LIMIT_FILL"),
        funding=FundingSchedule("constant_hourly_bps", 22.9669, label="DEMO_STRESS"),
    )
    assert res_a["model_label"] == MODEL_BASELINE
    assert res_b["model_label"] == MODEL_F11_F08
    assert res_a["reconciled_zero_residual"] is True
    assert res_b["reconciled_zero_residual"] is True
    assert res_a["rotations_completed"] == 0
    assert res_b["rotations_completed"] >= 1
    assert res_b["generations_total"] >= 2
    assert res_b["exit_orders_filled"] >= 1


def test_offline_partial_fill_min_notional_and_anti_dust_protection():
    """Verify LIMITED_CAPACITY_PARTIAL triggers BOTH partial-BUY EXIT_MIN_NOTIONAL and partial-SELL anti-dust."""
    # Case 1: bar 1 = 131.50 (< lower=131.56) -> BUY fills 0.02 then BREAKOUT cancels rest -> 0.02 blocked by EXIT_MIN_NOTIONAL
    bars_case1 = _bars_from_prices([132.89, 131.50, 131.00, 130.00])
    res1 = run_offline_lifecycle(
        bars_case1,
        guardrails=DemoGuardrails(enable_f11=True, enable_f08=True),
        assumptions=execution_profile("LIMITED_CAPACITY_PARTIAL"),
        funding=FundingSchedule("constant_hourly_bps", 10.0, label="TEST_FUNDING"),
    )
    assert res1["reconciled_zero_residual"] is True
    assert res1["residual_inventory"] == pytest.approx(0.02)
    assert res1["exit_block_counts"].get("EXIT_MIN_NOTIONAL", 0) >= 1

    # Case 2: BUY fills 0.02 at 131.56 on bar 1 and 0.02 at 131.56 on bar 2 (0 slippage -> inside [131.56, 134.22]),
    # bar 3 = 131.00 triggers BREAKOUT -> F11 SELL 0.04 @ 131.00 partial fills 0.02 -> bar 4 drops to 130.00 -> anti-dust protects!
    bars_case2 = _bars_from_prices([132.89, 131.56, 131.56, 131.00, 130.00, 129.00, 131.20])
    prof_0slip = ExecutionAssumptions(
        profile_name="LIMITED_CAPACITY_PARTIAL",
        assumption_summary="0.02/bar capacity with 0 slippage to exercise working SELL anti-dust",
        slippage=ZERO,
        capacity_per_bar=D("0.02"),
        exit_marketable_at_submit=True,
    )
    res2 = run_offline_lifecycle(
        bars_case2,
        guardrails=DemoGuardrails(enable_f11=True, enable_f08=True),
        assumptions=prof_0slip,
        funding=FundingSchedule("constant_hourly_bps", 10.0, label="TEST_FUNDING"),
    )
    assert res2["reconciled_zero_residual"] is True
    assert res2["exit_orders_partial_events"] >= 1
    assert res2["dust_protections_triggered"] >= 1
    # Bar 6 rebounds to 131.20 >= 131.00 -> protected working SELL completes remaining 0.02!
    assert res2["residual_inventory"] == pytest.approx(0.0)
    assert res2["exit_orders_filled"] >= 1


def test_offline_unfilled_exit_hits_retry_limit_and_preserves_residual():
    """Verify UNFILLED_EXIT_TIMEOUT exhausts EXIT_MAX_ATTEMPTS=5 and never fabricates a fill."""
    prices = [132.89, 131.40, 130.80, 130.20, 129.60, 129.00, 128.40, 127.80, 127.20, 126.60, 126.00, 125.50]
    bars = _bars_from_prices(prices)
    res = run_offline_lifecycle(
        bars,
        guardrails=DemoGuardrails(enable_f11=True, enable_f08=True, max_loss=D("10")),
        assumptions=execution_profile("UNFILLED_EXIT_TIMEOUT"),
        funding=FundingSchedule("zero", 0.0, label="EX_FUNDING"),
    )
    assert res["reconciled_zero_residual"] is True
    assert res["exit_orders_filled"] == 0
    assert res["residual_inventory"] == pytest.approx(0.04)
    assert res["stranded_at_end"] is True
    assert res["exit_block_counts"].get("EXIT_RETRY_LIMIT_EXCEEDED", 0) >= 1


def test_offline_gap_price_band_and_cancel_unknown_recovery():
    """Verify GAP_AND_CANCEL_FAULT exercises PERCENT_PRICE band block and UNKNOWN->reconcile recovery."""
    # Gentle entry, then gradual decline triggering exit + cancel fault, then >4% gap triggering EXIT_PRICE_BAND
    prices = [132.89, 131.40, 130.80, 130.30, 129.80, 129.30, 123.00, 126.00, 127.00, 128.00]
    bars = _bars_from_prices(prices)
    res = run_offline_lifecycle(
        bars,
        guardrails=DemoGuardrails(enable_f11=True, enable_f08=True, max_loss=D("10")),
        assumptions=execution_profile("GAP_AND_CANCEL_FAULT"),
        funding=FundingSchedule("zero", 0.0, label="EX_FUNDING"),
    )
    assert res["reconciled_zero_residual"] is True
    assert res["exit_cancel_faults"] >= 1
    assert res["exit_reconcile_recoveries"] >= 1


def test_offline_loss_budget_halt_stops_new_orders_and_rotation():
    """Verify Demo max_loss=1.00 USDT hard halt latches under severe crash + funding bleed."""
    sp = stress_paths(132.89, 168)
    bars = close_bars(sp["insufficient_rebound"], "2026-08-13T00:00:00+00:00")
    res = run_offline_lifecycle(
        bars,
        guardrails=DemoGuardrails(enable_f11=True, enable_f08=True, max_loss=D("1")),
        assumptions=execution_profile("UNFILLED_EXIT_TIMEOUT"),
        funding=FundingSchedule("constant_hourly_bps", 22.9669, label="DEMO_STRESS"),
    )
    assert res["reconciled_zero_residual"] is True
    assert res["halted"] is True
    assert res["halt_reason"] in {"LOSS_BUDGET_EXHAUSTED", "EXIT_LOSS_BUDGET_EXHAUSTED"}
    assert res["rotations_completed"] == 0


def test_paired_comparison_a_vs_b_reproduces_r08_baseline_on_continuous_decline():
    """Verify Model A reproduces R08 continuous_decline (-29.51% ex-funding / -52.22% stress) and B diverges cleanly."""
    sp = stress_paths(132.89, 168)
    bars = close_bars(sp["continuous_decline"], "2026-08-13T00:00:00+00:00")
    a_ex = run_offline_lifecycle(
        bars,
        guardrails=DemoGuardrails(enable_f11=False, enable_f08=False),
        assumptions=execution_profile("IDEAL_LIMIT_FILL"),
        funding=FundingSchedule("zero", 0.0, label="EX_FUNDING"),
    )
    a_stress = run_offline_lifecycle(
        bars,
        guardrails=DemoGuardrails(enable_f11=False, enable_f08=False),
        assumptions=execution_profile("IDEAL_LIMIT_FILL"),
        funding=FundingSchedule("constant_hourly_bps", 22.9669, label="DEMO_STRESS"),
    )
    b_ideal = run_offline_lifecycle(
        bars,
        guardrails=DemoGuardrails(enable_f11=True, enable_f08=True),
        assumptions=execution_profile("IDEAL_LIMIT_FILL"),
        funding=FundingSchedule("constant_hourly_bps", 22.9669, label="DEMO_STRESS"),
    )
    b_unfilled = run_offline_lifecycle(
        bars,
        guardrails=DemoGuardrails(enable_f11=True, enable_f08=True),
        assumptions=execution_profile("UNFILLED_EXIT_TIMEOUT"),
        funding=FundingSchedule("constant_hourly_bps", 22.9669, label="DEMO_STRESS"),
    )
    assert a_ex["net_return_pct"] == pytest.approx(-29.51, abs=0.05)
    assert a_stress["net_return_pct"] == pytest.approx(-52.22, abs=0.05)
    assert b_ideal["net_return_pct"] > a_stress["net_return_pct"] + 40.0
    assert b_unfilled["net_return_pct"] == pytest.approx(a_stress["net_return_pct"], abs=0.2)
    assert all(r["reconciled_zero_residual"] for r in (a_ex, a_stress, b_ideal, b_unfilled))
