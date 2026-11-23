"""Unit tests for P8-R10 funding-sensitive and volatility-adaptive Grid research."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from factorlab.experiments.grid_funding_adaptive import (
    FundingPolicy,
    VolatilityPolicy,
    adaptive_half_width,
    causal_funding_rate_bps_h,
    causal_realized_vol_1h,
    run_paired_window,
)


def _synthetic_ohlc_bars(prices: list[float], start_iso: str = "2026-06-01T00:00:00+00:00") -> list[dict]:
    dates = pd.date_range(start_iso, periods=len(prices), freq="h", tz="UTC")
    bars = []
    for i, (t, p) in enumerate(zip(dates, prices)):
        bars.append(
            {
                "id": f"bar-{i}",
                "time": t.isoformat(),
                "price": float(p),
                "high": float(p * 1.003),
                "low": float(p * 0.997),
                "close": float(p),
                "volume": 5000,
                "completed_bar": True,
            }
        )
    return bars


def _funding_schedule(start_iso: str, n_hours: int, rate_fn) -> list[tuple[datetime, Decimal, Decimal, str]]:
    t0 = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
    events = []
    for h in range(0, n_hours, 8):
        ts = t0 + timedelta(hours=h, milliseconds=1)
        rate = Decimal(str(rate_fn(h)))
        events.append((ts, Decimal("200.0"), rate, "Regular"))
    return events


def test_causal_funding_rate_ignores_future_events():
    t0 = datetime(2026, 6, 1, 0, 0, tzinfo=timezone.utc)
    obs = t0 + timedelta(hours=24)
    past = [
        (t0 + timedelta(hours=0), Decimal("200"), Decimal("0.0001"), "Regular"),
        (t0 + timedelta(hours=8), Decimal("200"), Decimal("0.0002"), "Regular"),
        (t0 + timedelta(hours=16), Decimal("200"), Decimal("0.0001"), "Regular"),
    ]
    future_a = [(t0 + timedelta(hours=32), Decimal("200"), Decimal("0.0000"), "Regular")]
    future_b = [(t0 + timedelta(hours=32), Decimal("200"), Decimal("0.0500"), "Regular")]

    est_a = causal_funding_rate_bps_h(past + future_a, obs, lookback_hours=24, conservative=True)
    est_b = causal_funding_rate_bps_h(past + future_b, obs, lookback_hours=24, conservative=True)
    assert est_a == est_b


def test_strategy_b_lookahead_immunity(tmp_path: Path):
    """Altering funding rates strictly after cutoff hour MUST NOT change decisions up to cutoff."""
    # Oscillating prices that trigger a buy fill around hour 5 and a sell fill around hour 10
    prices = [
        200.0, 199.5, 198.5, 197.5, 197.8, 198.2, 199.5, 201.0, 202.5, 202.0,
        200.5, 199.0, 197.5, 197.0, 198.5, 200.0, 202.2, 201.5, 200.0, 199.5,
        198.5, 197.6, 198.0, 199.0, 200.0, 201.0, 202.5, 201.0, 200.0, 199.0,
    ]
    start_iso = "2026-06-01T00:00:00+00:00"
    bars = _synthetic_ohlc_bars(prices, start_iso)
    cutoff_idx = 15  # 15th bar

    # Identical funding up to hour 15, radically different after hour 16
    fund_base = _funding_schedule(start_iso, 40, lambda h: 0.0001 if h < 16 else 0.0000)
    fund_shock = _funding_schedule(start_iso, 40, lambda h: 0.0001 if h < 16 else 0.0500)

    policy = FundingPolicy(
        candidate_id="B_TEST",
        lookback_hours=24,
        conservative=True,
        entry_max_bps_h=0.50,
        expected_hold_hours=48.0,
        min_net_margin_bps=100.0,
        exit_mode="funding_bleed",
        exit_bps_h=1.00,
        max_hold_hours=72.0,
    )

    dir_a = tmp_path / "run_base"
    dir_b = tmp_path / "run_shock"
    run_paired_window(
        dir_a,
        strategy_id="B_TEST",
        bars=bars,
        capital=Decimal("10.00"),
        funding_events=fund_base,
        funding_policy=policy,
        account_id="test-b-base",
    )
    run_paired_window(
        dir_b,
        strategy_id="B_TEST",
        bars=bars,
        capital=Decimal("10.00"),
        funding_events=fund_shock,
        funding_policy=policy,
        account_id="test-b-shock",
    )

    nav_a = pd.read_csv(dir_a / "daily.csv").iloc[:cutoff_idx]
    nav_b = pd.read_csv(dir_b / "daily.csv").iloc[:cutoff_idx]
    pd.testing.assert_frame_equal(nav_a, nav_b)


def test_strategy_b_blocks_entry_under_high_funding(tmp_path: Path):
    """High observed funding must prevent Strategy B from opening new long positions."""
    prices = [200.0, 197.0, 197.0, 197.0, 200.0, 202.5, 200.0, 197.0, 200.0, 202.5]
    bars = _synthetic_ohlc_bars(prices)
    policy = FundingPolicy(
        candidate_id="B_BLOCK",
        lookback_hours=24,
        conservative=True,
        entry_max_bps_h=0.50,
        expected_hold_hours=48.0,
        min_net_margin_bps=100.0,
        exit_mode="none",
    )
    m_a = run_paired_window(
        tmp_path / "arm_a",
        strategy_id="A_FIXED",
        bars=bars,
        capital=Decimal("10.00"),
        constant_hourly_bps=22.97,
        account_id="test-a-highfund",
    )
    m_b = run_paired_window(
        tmp_path / "arm_b",
        strategy_id="B_BLOCK",
        bars=bars,
        capital=Decimal("10.00"),
        funding_policy=policy,
        constant_hourly_bps=22.97,
        account_id="test-b-highfund",
    )
    assert m_a["fills_count"] >= 1
    assert m_b["fills_count"] == 0
    assert m_b["funding_pnl_usdt"] == 0.0
    assert abs(m_a["fifo_reconciliation_residual"]) < 1e-8
    assert abs(m_b["fifo_reconciliation_residual"]) < 1e-8


def test_strategy_c_lookahead_immunity_and_width_adaptation(tmp_path: Path):
    """Strategy C width adapts causally to warmup vol and is immune to future price perturbations."""
    rng = np.random.default_rng(42)
    low_vol_warmup = list(200.0 * np.exp(np.cumsum(rng.normal(0.0, 0.001, size=48))))
    high_vol_warmup = list(200.0 * np.exp(np.cumsum(rng.normal(0.0, 0.012, size=48))))
    policy = VolatilityPolicy(
        candidate_id="C_TEST",
        vol_window_hours=24,
        k_mult=3.0,
        min_half_width=0.006,
        max_half_width=0.035,
        extreme_vol_mult=2.5,
        allow_flat_rebuild=True,
        min_rebuild_interval=12,
        reference_vol_1h=0.0045,
    )
    v_low = causal_realized_vol_1h(low_vol_warmup, 24, 0.0045)
    v_high = causal_realized_vol_1h(high_vol_warmup, 24, 0.0045)
    assert adaptive_half_width(v_low, policy) < adaptive_half_width(v_high, policy)

    # Lookahead immunity: identical bars for first 12 hours, divergent afterwards
    common = [200.0, 199.2, 198.4, 197.5, 198.0, 199.0, 200.5, 201.8, 202.4, 201.0, 199.8, 199.0]
    bars_1 = _synthetic_ohlc_bars(common + [199.5, 200.0, 200.5, 201.0, 201.5, 202.0])
    bars_2 = _synthetic_ohlc_bars(common + [170.0, 150.0, 240.0, 140.0, 260.0, 130.0])

    run_paired_window(
        tmp_path / "c_1",
        strategy_id="C_TEST",
        bars=bars_1,
        capital=Decimal("10.00"),
        vol_policy=policy,
        warmup_closes=low_vol_warmup,
        account_id="test-c-1",
    )
    run_paired_window(
        tmp_path / "c_2",
        strategy_id="C_TEST",
        bars=bars_2,
        capital=Decimal("10.00"),
        vol_policy=policy,
        warmup_closes=low_vol_warmup,
        account_id="test-c-2",
    )
    nav_1 = pd.read_csv(tmp_path / "c_1" / "daily.csv").iloc[: len(common)]
    nav_2 = pd.read_csv(tmp_path / "c_2" / "daily.csv").iloc[: len(common)]
    pd.testing.assert_frame_equal(nav_1, nav_2)


def test_strategy_b_exits_on_funding_spike_while_holding(tmp_path: Path):
    """Strategy B with funding_bleed exit must close held inventory when funding spikes."""
    # Price drops to trigger buy fill early (under low funding), then stays flat while funding spikes at hour 12
    prices = [200.0, 197.5, 197.5, 198.0, 198.5, 198.5, 198.5, 198.5, 198.5, 198.5] + [198.5] * 30
    bars = _synthetic_ohlc_bars(prices)
    policy_exit = FundingPolicy(
        candidate_id="B_EXIT_TEST",
        lookback_hours=24,
        conservative=True,
        entry_max_bps_h=0.50,
        expected_hold_hours=48.0,
        min_net_margin_bps=100.0,
        exit_mode="funding_bleed",
        exit_bps_h=1.00,
        max_hold_hours=72.0,
    )
    m_a = run_paired_window(
        tmp_path / "a_spike",
        strategy_id="A_FIXED",
        bars=bars,
        capital=Decimal("10.00"),
        regime_switch_bps=(10, 0.10, 22.97),
        account_id="test-a-spike",
    )
    m_b = run_paired_window(
        tmp_path / "b_spike",
        strategy_id="B_EXIT_TEST",
        bars=bars,
        capital=Decimal("10.00"),
        funding_policy=policy_exit,
        regime_switch_bps=(10, 0.10, 22.97),
        account_id="test-b-spike",
    )
    assert m_a["residual_inventory"] > 0
    assert m_b["residual_inventory"] == 0.0
    assert m_b["final_state"] == "EXITED"
    assert m_b["final_net_pnl_usdt"] > m_a["final_net_pnl_usdt"]


def test_strategy_c_extreme_vol_guard_blocks_entry(tmp_path: Path):
    """Extreme pre-window volatility exceeding extreme_vol_mult * reference_vol_1h blocks new buys."""
    rng = np.random.default_rng(7)
    extreme_warmup = list(200.0 * np.exp(np.cumsum(rng.normal(0.0, 0.020, size=48))))
    scale = 200.0 / extreme_warmup[-1]
    extreme_warmup = [p * scale for p in extreme_warmup]

    prices = [200.0, 190.0, 185.0, 180.0, 175.0, 170.0]
    bars = _synthetic_ohlc_bars(prices)
    policy = VolatilityPolicy(
        candidate_id="C_GUARD",
        vol_window_hours=24,
        k_mult=2.5,
        min_half_width=0.005,
        max_half_width=0.030,
        extreme_vol_mult=2.0,
        allow_flat_rebuild=True,
        reference_vol_1h=0.0045,
    )
    m_c = run_paired_window(
        tmp_path / "c_guard",
        strategy_id="C_GUARD",
        bars=bars,
        capital=Decimal("10.00"),
        vol_policy=policy,
        warmup_closes=extreme_warmup,
        account_id="test-c-guard",
    )
    assert m_c["fills_count"] == 0
    assert m_c["residual_inventory"] == 0.0


def test_predeclared_configs_load_cleanly():
    from factorlab.experiments.grid_r10_study import load_strategy_configs

    repo_root = Path(__file__).resolve().parents[1]
    cfgs, b_policies, c_policies = load_strategy_configs(repo_root)
    assert cfgs["A"]["strategy_id"] == "A_FIXED_GRID"
    assert len(b_policies) == 6
    assert len(c_policies) == 6
    assert cfgs["B"]["causality_contract"]["future_rate_forbidden"] is True
    assert cfgs["C"]["causality_contract"]["validation_refit_forbidden"] is True


def test_multi_overlay_matches_single_runs(tmp_path: Path):
    """Multi-overlay optimization for funding-independent arms must be bit-identical to separate runs."""
    from factorlab.experiments.grid_funding_adaptive import run_paired_window_multi_overlay

    prices = [200.0, 199.0, 197.8, 198.2, 199.5, 201.0, 202.3, 200.5, 198.0, 197.0, 199.0]
    bars = _synthetic_ohlc_bars(prices)
    overlays = [
        ("normal", 1.0, 0.125, None),
        ("demo", 1.0, 22.9669, None),
        ("switch", 1.0, None, (4, 0.125, 22.9669)),
    ]
    multi = run_paired_window_multi_overlay(
        tmp_path / "multi_a",
        strategy_id="A_FIXED",
        bars=bars,
        capital=Decimal("10.00"),
        overlays=overlays,
        account_id="test-multi-a",
        cleanup_dir=True,
    )
    for key, f_scale, const_bps, sw_bps in overlays:
        single = run_paired_window(
            tmp_path / f"single_{key}",
            strategy_id="A_FIXED",
            bars=bars,
            capital=Decimal("10.00"),
            funding_scale=f_scale,
            constant_hourly_bps=const_bps,
            regime_switch_bps=sw_bps,
            account_id=f"test-single-{key}",
            cleanup_dir=True,
        )
        assert multi[key] == single


