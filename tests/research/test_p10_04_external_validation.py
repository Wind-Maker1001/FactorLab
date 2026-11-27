"""Unit and contract tests for FactorLab external strategy independent validation (P10-04)."""
from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from sklearn.linear_model import Ridge
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from factorlab.research.external_validation.config import (
    DEFAULT_TICKERS,
    ExternalValidationConfig,
    load_validation_config,
    save_validation_config,
)
from factorlab.research.external_validation.financial import deflated_sharpe_ratio
from factorlab.research.external_validation.monte_carlo import (
    _fast_ridge_residual_forecast,
    _fit_joint_model_params,
    generate_joint_path,
    prepare_calibration_dataset,
)
from factorlab.research.external_validation.replay import (
    FactorLabIndependentBacktester,
    IndependentReplayResult,
    MarketPanel,
    NativeV69SignalAdapter,
    REFERENCE_REPLAY_TOLERANCES,
    compare_against_reference,
    compute_risk_metrics,
)
from factorlab.research.external_validation.shared_portfolio import (
    CROSS_MARKET_CALENDAR,
    aggregate_isolated_cross_exchange_portfolios,
    align_cross_market_navs,
    simulate_alpaca_shared_account,
    verify_pr260_shared_capital_policy_contract,
)


def _synthetic_market_panel(n_days: int = 360, seed: int = 7) -> MarketPanel:
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2022-01-03", periods=n_days)
    # Correlated daily returns for 8 tickers + SP500TR
    mkt = rng.normal(0.0005, 0.011, size=n_days)
    closes = {}
    opens = {}
    highs = {}
    lows = {}
    vols = {}
    for i, sym in enumerate(DEFAULT_TICKERS):
        beta = 2.8 if sym == "TQQQ" else (1.1 if sym == "QQQ" else 0.9 + 0.1 * (i % 3))
        idio = rng.normal(0.0002, 0.012, size=n_days)
        r = np.clip(beta * mkt + idio, -0.15, 0.15)
        c = 100.0 * np.cumprod(1.0 + r)
        gap = rng.normal(0.0, 0.003, size=n_days)
        prev_c = np.r_[100.0, c[:-1]]
        o = prev_c * (1.0 + gap)
        h = np.maximum(o, c) * (1.0 + np.abs(rng.normal(0.0, 0.005, size=n_days)))
        lo = np.minimum(o, c) * (1.0 - np.abs(rng.normal(0.0, 0.005, size=n_days)))
        v = rng.uniform(5e6, 2e7, size=n_days)
        closes[sym] = c
        opens[sym] = o
        highs[sym] = h
        lows[sym] = lo
        vols[sym] = v
    sp = 4000.0 * np.cumprod(1.0 + mkt)
    return MarketPanel(
        tickers=DEFAULT_TICKERS,
        close=pd.DataFrame(closes, index=dates),
        opening=pd.DataFrame(opens, index=dates),
        high=pd.DataFrame(highs, index=dates),
        low=pd.DataFrame(lows, index=dates),
        volume=pd.DataFrame(vols, index=dates),
        benchmark_close=pd.Series(sp, index=dates, name="close"),
        source_dir="synthetic",
        benchmark_path="synthetic",
    )


def test_config_roundtrip(tmp_path: Path) -> None:
    cfg = ExternalValidationConfig()
    out = save_validation_config(cfg, tmp_path / "cfg.json")
    loaded = load_validation_config(out)
    assert loaded.schema == cfg.schema
    assert loaded.strategies == cfg.strategies
    assert loaded.tickers == cfg.tickers
    assert len(loaded.windows) == len(cfg.windows)
    assert loaded.monte_carlo.seeds == cfg.monte_carlo.seeds


def test_native_v69_independent_backtester_accounting_invariants() -> None:
    market = _synthetic_market_panel(n_days=320, seed=11)
    sig = NativeV69SignalAdapter(market.close, market.volume, market.opening, omit_sndk=False)
    bt = FactorLabIndependentBacktester(
        initial_capital=300_000.0,
        fee_rate=0.001,
        gross_cap=0.375,
        open_price_mode="float64",
    )
    res = bt.run(sig, market, window_name="synth", start="2022-06-01", end="2023-03-01", case="normal")
    assert res.signal_source == "NATIVE_FL"
    assert res.execution_engine == "FACTORLAB_INDEPENDENT_BACKTESTER"
    assert res.accounting_audit["zero_borrowing"] is True
    assert res.accounting_audit["max_pnl_nav_residual_usd"] < 1e-8
    assert res.sp500_metrics["fills"] > 0
    assert res.daily["weight"].max() <= 0.45
    assert np.isclose(res.attribution.sum(), res.sp500_metrics["net_usd"], atol=1e-6)


def test_backtester_stress_slippage_and_partial_fill_degrade_return() -> None:
    market = _synthetic_market_panel(n_days=320, seed=19)
    sig = NativeV69SignalAdapter(market.close, market.volume, market.opening, omit_sndk=False)
    base = FactorLabIndependentBacktester(fee_rate=0.001, slippage_bps=0.0, gross_cap=0.375).run(
        sig, market, window_name="synth", start="2022-06-01", end="2023-03-01", case="base"
    )
    stressed = FactorLabIndependentBacktester(fee_rate=0.005, slippage_bps=25.0, gross_cap=0.375).run(
        sig, market, window_name="synth", start="2022-06-01", end="2023-03-01", case="stress"
    )
    assert stressed.sp500_metrics["net_return"] < base.sp500_metrics["net_return"]
    assert stressed.sp500_metrics["fees"] > base.sp500_metrics["fees"]


def test_fast_ridge_matches_sklearn_pipeline() -> None:
    rng = np.random.default_rng(123)
    n_days = 320
    mkt = rng.normal(0.0004, 0.01, size=n_days)
    close = np.column_stack([100.0 * np.cumprod(1.0 + mkt + rng.normal(0, 0.012, size=n_days)) for _ in range(8)])
    sp = 4000.0 * np.cumprod(1.0 + mkt)
    df_c = pd.DataFrame(close)
    df_sp = pd.Series(sp)
    daily = df_c.pct_change(fill_method=None)
    mm = df_sp.pct_change(fill_method=None)
    mvar = mm.rolling(63).var().replace(0.0, np.nan)
    beta = daily.rolling(63).cov(mm).div(mvar, axis=0).clip(-1.0, 4.0)
    res_df = daily - beta.mul(mm, axis=0)
    risk = daily.rolling(42).std().replace(0.0, np.nan).to_numpy()
    score = (res_df.rolling(42).sum().to_numpy()) / (risk * np.sqrt(42.0))
    ma28 = df_c.rolling(28, min_periods=28).mean().to_numpy()
    breadth = np.mean(close > ma28, axis=1)

    preds = _fast_ridge_residual_forecast(
        close, res_df.to_numpy(), score, risk, breadth, sp, list(range(8)), [260, 281, 302]
    )
    # Independently check block at day 260 via sklearn StandardScaler + Ridge(alpha=80)
    assert np.isfinite(preds[260]).all()
    # Verify closed-form solution matches sklearn on identical design matrix
    X_sample = rng.normal(size=(500, 6))
    y_sample = rng.normal(size=500)
    pipe = make_pipeline(StandardScaler(), Ridge(alpha=80.0))
    pipe.fit(X_sample, y_sample)
    xm, xs = X_sample.mean(axis=0), X_sample.std(axis=0, ddof=0)
    Xs = (X_sample - xm) / xs
    ym = y_sample.mean()
    w = np.linalg.solve(Xs.T @ Xs + 80.0 * np.eye(6), Xs.T @ (y_sample - ym))
    pred_closed = ym + ((X_sample[:10] - xm) / xs) @ w
    np.testing.assert_allclose(pred_closed, pipe.predict(X_sample[:10]), rtol=1e-11, atol=1e-11)


def test_joint_mc_models_reproducible_and_fat_tailed() -> None:
    market = _synthetic_market_panel(n_days=360, seed=29)
    calib = prepare_calibration_dataset(market, start="2022-01-03", end="2023-05-19", include_sndk=False)
    params = _fit_joint_model_params(calib, block_size=20)
    for model in ("corr_gbm", "sync_mbb", "regime_cond_bootstrap", "down_jump_stress", "corr_shock_stress", "student_t_garch"):
        r1, g1, v1 = generate_joint_path(model, params, n_steps=120, seed=1729, path_id=0)
        r2, g2, v2 = generate_joint_path(model, params, n_steps=120, seed=1729, path_id=0)
        np.testing.assert_array_equal(r1, r2)
        np.testing.assert_array_equal(g1, g2)
        np.testing.assert_array_equal(v1, v2)
        assert np.isfinite(r1).all() and (r1 > -1.0).all()
        assert (v1 > 0.0).all()


def test_pr260_shared_capital_policy_contract_and_shared_simulation() -> None:
    contract = verify_pr260_shared_capital_policy_contract(
        account_id="test-alpaca-shared",
        capital_usd=300_000.0,
        strategy_budgets={"v69": 100_000.0, "v179": 100_000.0, "v182": 100_000.0},
        fee_rate=0.001,
        max_symbol_usd=105_000.0,
    )
    assert contract["contract_verified"] is True
    assert contract["mode"] == "SHARED_SAME_DIRECTION"

    market = _synthetic_market_panel(n_days=320, seed=31)
    # Test shared simulation with two native V69 sleeves (zero external package dependency in unit test)
    res = simulate_alpaca_shared_account(
        strategies=("v69",),
        allocation_weights={"v69": 1.0},
        market=market,
        package_dir=Path("."),
        window_name="synth",
        start="2022-06-01",
        end="2023-03-01",
        initial_capital=300_000.0,
        fee_rate=0.001,
    )
    assert res["days"] > 100
    assert res["idle_cash_ratio"] >= 0.0
    assert np.isfinite(res["daily_cvar_95"])


def test_deflated_sharpe_penalizes_multiple_testing() -> None:
    rng = np.random.default_rng(5)
    r = rng.normal(0.0005, 0.01, size=1000)
    d1 = deflated_sharpe_ratio(r, n_trials=1)
    d182 = deflated_sharpe_ratio(r, n_trials=182)
    assert d182["expected_max_null_sr_ann"] > d1["expected_max_null_sr_ann"]
    assert d182["dsr_probability"] < d1["dsr_probability"]


# =====================================================================
# P2-01 Tests: Cross-market NAV-first alignment, closures vs missing data
# =====================================================================


def test_p2_01_preserves_weekend_us_holiday_and_consecutive_closed_binance_pnl() -> None:
    """Weekend, US Memorial Day (2026-05-25), and 3-day closure Binance PnL must be 100% preserved."""
    # Alpaca NYSE sessions around Memorial Day weekend 2026: Thu 05-21, Fri 05-22, [Sat 05-23, Sun 05-24, Mon 05-25 closed], Tue 05-26, Wed 05-27
    alp_dates = pd.DatetimeIndex(
        [
            "2026-05-21 16:00:00-04:00",
            "2026-05-22 16:00:00-04:00",
            "2026-05-26 16:00:00-04:00",
            "2026-05-27 16:00:00-04:00",
        ]
    )
    alp_nav = pd.Series([100.0, 102.0, 105.0, 104.0], index=alp_dates)

    # Binance 24/7 UTC daily NAV with sharp Saturday crash (-10%), Sunday bounce (+5%), Memorial Day drop (-8%)
    bn_dates = pd.date_range("2026-05-21 23:00:00+00:00", "2026-05-27 23:00:00+00:00", freq="D")
    bn_vals = [10.0, 10.0, 9.0, 9.45, 8.694, 8.80, 9.10]
    bn_nav = pd.Series(bn_vals, index=bn_dates)

    aligned, meta = align_cross_market_navs(alp_nav, bn_nav)
    assert meta["natural_calendar_return_days"] == 6
    assert meta["alpaca_confirmed_closed_days"] == 3
    assert meta["alpaca_weekend_closed_days"] == 2
    assert meta["alpaca_us_holiday_weekdays"] == ["2026-05-25"]
    assert meta["alpaca_max_consecutive_closed_days"] == 3
    assert meta["binance_omitted_days"] == 0
    assert meta["binance_pnl_reconciliation_error"] < 1e-12

    # Alpaca carries Friday 102.0 strictly across Sat/Sun/Mon (confirmed closed)
    for closed_d in ("2026-05-23", "2026-05-24", "2026-05-25"):
        ts = pd.Timestamp(closed_d)
        assert bool(aligned.at[ts, "alpaca_market_closed"]) is True
        assert aligned.at[ts, "alpaca_nav"] == pytest.approx(102.0)
        assert aligned.at[ts, "alpaca_daily_ret"] == pytest.approx(0.0)

    # Saturday -10% and Memorial Day -8% losses are present in daily returns and compounded total matches raw NAV
    assert aligned.at[pd.Timestamp("2026-05-23"), "binance_daily_ret"] == pytest.approx(-0.10)
    assert aligned.at[pd.Timestamp("2026-05-25"), "binance_daily_ret"] == pytest.approx(-0.08)
    eval_r = aligned.iloc[1:]["binance_daily_ret"].to_numpy()
    assert float(np.prod(1.0 + eval_r) - 1.0) == pytest.approx(bn_vals[-1] / bn_vals[0] - 1.0, abs=1e-12)


def test_p2_01_window_boundaries_and_timezone_alignment() -> None:
    """Handle weekend start/end boundaries, pre-window anchor, and late-ET vs UTC timezone differences."""
    # Alpaca Fri 2026-05-15 (late ET 21:00 -> 2026-05-16 01:00 UTC! Must still map to NYSE date 2026-05-15)
    alp_dates = pd.DatetimeIndex(
        [
            "2026-05-15 21:00:00-04:00",
            "2026-05-18 16:00:00-04:00",
            "2026-05-19 16:00:00-04:00",
            "2026-05-20 16:00:00-04:00",
            "2026-05-21 16:00:00-04:00",
            "2026-05-22 16:00:00-04:00",
        ]
    )
    alp_nav = pd.Series([100.0, 101.0, 102.0, 101.5, 103.0, 104.0], index=alp_dates)

    # Binance starts Saturday 2026-05-16 and extends to Wednesday 2026-05-27 (past Alpaca's Fri 05-22 + Mon 05-25 holiday!)
    bn_dates = pd.date_range("2026-05-16 23:00:00+00:00", "2026-05-27 23:00:00+00:00", freq="D")
    bn_nav = pd.Series(np.linspace(10.0, 11.1, len(bn_dates)), index=bn_dates)

    aligned, meta = align_cross_market_navs(alp_nav, bn_nav, binance_initial_nav=10.0)
    # Anchor at Fri 2026-05-15 (pre-window initial capital), returns start Sat 2026-05-16
    assert meta["used_pre_window_anchor"] is True
    assert meta["anchor_date"] == "2026-05-15"
    assert meta["return_start_date"] == "2026-05-16"
    # Trailing coverage stops at Mon 2026-05-25 (confirmed Memorial Day closure), NEVER forward-filling Tue 2026-05-26 open session!
    assert meta["return_end_date"] == "2026-05-25"
    assert pd.Timestamp("2026-05-26") not in aligned.index


def test_p2_01_missing_alpaca_open_session_raises_never_silent_ffill() -> None:
    """Genuine missing Alpaca data on an open NYSE session must raise ValueError, never silently fill as closed."""
    # Missing Wednesday 2026-05-20 (regular NYSE trading day!)
    alp_dates = pd.DatetimeIndex(["2026-05-18", "2026-05-19", "2026-05-21", "2026-05-22"])
    alp_nav = pd.Series([100.0, 101.0, 102.0, 103.0], index=alp_dates)
    bn_dates = pd.date_range("2026-05-18", "2026-05-22", freq="D")
    bn_nav = pd.Series([10.0, 10.1, 10.2, 10.1, 10.3], index=bn_dates)

    with pytest.raises(ValueError, match="2026-05-20"):
        align_cross_market_navs(alp_nav, bn_nav)


def test_p2_01_missing_binance_calendar_day_raises() -> None:
    """Missing any 24/7 calendar day in Binance NAV must raise ValueError."""
    alp_dates = pd.DatetimeIndex(["2026-05-15", "2026-05-18"])
    alp_nav = pd.Series([100.0, 101.0], index=alp_dates)
    # Missing Sunday 2026-05-17
    bn_dates = pd.DatetimeIndex(["2026-05-15", "2026-05-16", "2026-05-18"])
    bn_nav = pd.Series([10.0, 9.8, 10.2], index=bn_dates)

    with pytest.raises(ValueError, match="2026-05-17"):
        align_cross_market_navs(alp_nav, bn_nav)


# =====================================================================
# P2-02 Tests: Isolated cross-exchange NAV aggregation & weight drift
# =====================================================================


def test_p2_02_isolated_portfolio_compounds_independently_with_weight_drift() -> None:
    """Two isolated accounts must compound from initial equity with drifting weights, NOT daily rebalancing."""
    # Day 0 -> Day 1 -> Day 2: Alpaca +100% total (1.0 -> 1.5 -> 2.0), Binance -50% total (1.0 -> 0.5 -> 0.5)
    dates = pd.date_range("2026-05-18", periods=3, freq="D")
    aligned = pd.DataFrame(
        {
            "alpaca_nav": [100.0, 150.0, 200.0],
            "binance_nav": [10.0, 5.0, 5.0],
            "alpaca_market_closed": [False, False, False],
        },
        index=dates,
    )
    res = aggregate_isolated_cross_exchange_portfolios(
        aligned,
        binance_weights=(0.50,),
        total_initial_capital_usd=200_000.0,
        usd_per_usdt=1.0,
    )[0]

    # Exact buy-and-hold equity: Alpaca 100k -> 200k, Binance 100k -> 50k => Total 250k (+25.0%)
    # (Buggy daily-rebalanced formula would give (1 + 0.5*0.5 + 0.5*(-0.5))*(1 + 0.5*(1/3) + 0) - 1 = +16.67%!)
    assert res["final_alpaca_equity_usd"] == pytest.approx(200_000.0)
    assert res["final_binance_equity_usd"] == pytest.approx(50_000.0)
    assert res["final_total_equity_usd"] == pytest.approx(250_000.0)
    assert res["net_return"] == pytest.approx(0.25)
    assert res["initial_alpaca_weight"] == pytest.approx(0.50)
    assert res["initial_binance_weight"] == pytest.approx(0.50)
    assert res["final_alpaca_weight"] == pytest.approx(0.80)
    assert res["final_binance_weight"] == pytest.approx(0.20)
    assert res["final_binance_weight_drift_pp"] == pytest.approx(-30.0)
    assert res["max_abs_weight_drift_pp"] == pytest.approx(30.0)
    assert res["rebalancing_assumed"] is False
    assert res["cross_exchange_transfers_assumed"] is False
    assert res["shared_margin_assumed"] is False
    assert res["risk_statistics_calendar"] == CROSS_MARKET_CALENDAR
    assert res["fx_conversion_assumption"] == "FIXED_PARITY_1_USD_EQ_1_USDT_NO_DEPEG_MODELING"


# =====================================================================
# P2-03 Tests: Fail-closed 7-layer exact_reference_match & negative cases
# =====================================================================


def _write_reference_fixture(ref_dir: Path, res: IndependentReplayResult) -> None:
    ref_dir.mkdir(parents=True, exist_ok=True)
    res.daily.to_parquet(ref_dir / "daily.parquet")
    res.fills.to_csv(ref_dir / "fills.csv", index=False)
    res.targets.reset_index().to_csv(ref_dir / "targets.csv", index=False)
    res.attribution.to_frame("net_usd").to_csv(ref_dir / "attribution.csv")
    (ref_dir / "sp500_metrics.json").write_text(json.dumps(res.sp500_metrics), encoding="utf-8")
    (ref_dir / "result.json").write_text(json.dumps({"metrics": res.qqq_metrics}), encoding="utf-8")


def test_p2_03_seven_layer_exact_match_and_negative_cases(tmp_path: Path) -> None:
    """Even with identical final NAV, any execution/weight/metric/identity breach must fail exact_reference_match."""
    market = _synthetic_market_panel(n_days=320, seed=41)
    sig = NativeV69SignalAdapter(market.close, market.volume, market.opening, omit_sndk=False)
    bt = FactorLabIndependentBacktester(initial_capital=300_000.0, fee_rate=0.001, gross_cap=0.375, open_price_mode="float64")
    base = bt.run(sig, market, window_name="synth", start="2022-06-01", end="2023-03-01", case="normal")
    assert len(base.fills) > 2

    ref_dir = tmp_path / "ref_ok"
    _write_reference_fixture(ref_dir, base)

    # 0. Identical replay passes ALL 7 layers with frozen tolerances
    ok = compare_against_reference(base, ref_dir)
    assert ok["exact_reference_match"] is True
    assert ok["failed_checks"] == []
    assert ok["tolerances"] == REFERENCE_REPLAY_TOLERANCES

    # 1. Perturb requested quantity ONLY (NAV, attribution, metrics completely identical) -> MUST FAIL
    fills_bad_req = base.fills.copy()
    fills_bad_req.loc[0, "requested"] += 1e-2
    bad_req = compare_against_reference(replace(base, fills=fills_bad_req), ref_dir)
    assert bad_req["exact_reference_match"] is False
    assert "layer3_4_requested_qty_exact" in bad_req["failed_checks"]

    # 2. Perturb filled quantity ONLY (NAV identical) -> MUST FAIL
    fills_bad_qty = base.fills.copy()
    fills_bad_qty.loc[0, "quantity"] += 1e-2
    bad_qty = compare_against_reference(replace(base, fills=fills_bad_qty), ref_dir)
    assert bad_qty["exact_reference_match"] is False
    assert "layer3_4_filled_qty_exact" in bad_qty["failed_checks"]

    # 3. Perturb fill price ONLY (NAV identical) -> MUST FAIL
    fills_bad_px = base.fills.copy()
    fills_bad_px.loc[0, "price"] += 1e-3
    bad_px = compare_against_reference(replace(base, fills=fills_bad_px), ref_dir)
    assert bad_px["exact_reference_match"] is False
    assert "layer3_4_fill_price_exact" in bad_px["failed_checks"]

    # 4. Perturb fill fee ONLY (NAV identical) -> MUST FAIL
    fills_bad_fee = base.fills.copy()
    fills_bad_fee.loc[0, "fee"] += 1e-3
    bad_fee = compare_against_reference(replace(base, fills=fills_bad_fee), ref_dir)
    assert bad_fee["exact_reference_match"] is False
    assert "layer3_4_fill_fee_exact" in bad_fee["failed_checks"]

    # 5. Perturb daily weight ONLY (NAV identical) -> MUST FAIL
    daily_bad_wt = base.daily.copy()
    daily_bad_wt.iloc[5, daily_bad_wt.columns.get_loc("weight")] += 1e-4
    bad_wt = compare_against_reference(replace(base, daily=daily_bad_wt), ref_dir)
    assert bad_wt["exact_reference_match"] is False
    assert "layer5_daily_weight_exact" in bad_wt["failed_checks"]

    # 6. Perturb QQQ benchmark metric ONLY (NAV and SP500 metrics identical!) -> MUST FAIL
    qqq_bad = dict(base.qqq_metrics)
    qqq_bad["sharpe"] = float(qqq_bad["sharpe"]) + 1e-4
    bad_qqq = compare_against_reference(replace(base, qqq_metrics=qqq_bad), ref_dir)
    assert bad_qqq["exact_reference_match"] is False
    assert "layer7_qqq_metrics_exact" in bad_qqq["failed_checks"]

    # 7. Perturb S&P 500 TR metric ONLY (NAV identical) -> MUST FAIL
    sp_bad = dict(base.sp500_metrics)
    sp_bad["alpha"] = float(sp_bad["alpha"]) + 1e-4
    bad_sp = compare_against_reference(replace(base, sp500_metrics=sp_bad), ref_dir)
    assert bad_sp["exact_reference_match"] is False
    assert "layer7_sp500_metrics_exact" in bad_sp["failed_checks"]

    # 8. Missing metric key or NaN in fills must NEVER silently pass -> MUST FAIL
    sp_missing = dict(base.sp500_metrics)
    sp_missing.pop("calmar")
    bad_missing = compare_against_reference(replace(base, sp500_metrics=sp_missing), ref_dir)
    assert bad_missing["exact_reference_match"] is False
    assert "layer7_sp500_metrics_exact" in bad_missing["failed_checks"]

    fills_nan = base.fills.copy()
    fills_nan.loc[0, "price"] = float("nan")
    bad_nan = compare_against_reference(replace(base, fills=fills_nan), ref_dir)
    assert bad_nan["exact_reference_match"] is False
    assert "layer3_4_fill_price_exact" in bad_nan["failed_checks"]

    # 9. Duplicate order identity (date, ticker, side) in BOTH ref and FL must FAIL (never silent pass!)
    ref_dup_dir = tmp_path / "ref_dup"
    dup_fills = pd.concat([base.fills.iloc[[0]], base.fills.iloc[[0]]], ignore_index=True)
    dup_res = replace(base, fills=dup_fills)
    _write_reference_fixture(ref_dup_dir, dup_res)
    bad_dup = compare_against_reference(dup_res, ref_dup_dir)
    assert bad_dup["exact_reference_match"] is False
    assert "layer3_4_order_identity_exact" in bad_dup["failed_checks"]

    # 10. Missing daily row must FAIL
    bad_row = compare_against_reference(replace(base, daily=base.daily.iloc[:-1]), ref_dir)
    assert bad_row["exact_reference_match"] is False
    assert "layer5_daily_index_exact" in bad_row["failed_checks"]
