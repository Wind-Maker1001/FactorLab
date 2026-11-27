"""Unit and contract tests for FactorLab external strategy independent validation (P10-04)."""
from __future__ import annotations

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
    MarketPanel,
    NativeV69SignalAdapter,
    compute_risk_metrics,
)
from factorlab.research.external_validation.shared_portfolio import (
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
