"""Phase 3: Real financial validation, execution robustness, concentration, regimes, and overfitting diagnostics."""
from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from scipy.stats import norm

from .config import DEFAULT_TICKERS, ExternalValidationConfig, StressScenarioSpec
from .replay import (
    FactorLabIndependentBacktester,
    IndependentReplayResult,
    MarketPanel,
    build_signal_provider,
    compute_risk_metrics,
    load_market_panel,
)


def _baseline_benchmark_metrics(market: MarketPanel, start: str, end: str) -> dict[str, dict[str, Any]]:
    """Compute Cash (0%), QQQ 100%, QQQ 70%/30% Cash, and S&P 500 Total Return baselines on exact window."""
    idx = market.close.index
    sessions = idx[(idx >= pd.Timestamp(start)) & (idx <= pd.Timestamp(end))]
    sp_ret = market.benchmark_close.pct_change(fill_method=None).reindex(sessions)
    qqq_ret = market.close["QQQ"].pct_change(fill_method=None).reindex(sessions)
    cash_ret = pd.Series(0.0, index=sessions)
    qqq70_ret = 0.70 * qqq_ret

    out: dict[str, dict[str, Any]] = {}
    for name, series, exp in (
        ("cash_0pct", cash_ret, 0.0),
        ("qqq_70pct_rebal", qqq70_ret, 0.70),
        ("qqq_100pct", qqq_ret, 1.0),
        ("sp500_tr", sp_ret, 1.0),
    ):
        m = compute_risk_metrics(series, sp_ret)
        m.update(days=int(len(sessions)), exposure=exp, fees=0.0, fills=0)
        out[name] = m
    return out


def run_complete_strategy_comparison(
    config: ExternalValidationConfig,
    markets: dict[str, MarketPanel],
) -> tuple[list[dict[str, Any]], dict[tuple[str, str, str], IndependentReplayResult]]:
    """Run V69, V179, V182 + Cash/QQQ/SP500TR across all windows & cases, tagging original vs FL new experiments."""
    pkg = Path(config.packages.v179_v182_dir)
    archived_set = {
        f.parent.relative_to(pkg / "accounts").as_posix()
        for f in (pkg / "accounts").glob("*/*/*/result.json")
    }
    archived_set.add("v69/target/normal")

    rows: list[dict[str, Any]] = []
    results_cache: dict[tuple[str, str, str], IndependentReplayResult] = {}

    for win in config.windows:
        market = markets[win.name]
        baselines = _baseline_benchmark_metrics(market, win.start, win.end)
        for bname, bm in baselines.items():
            rows.append(
                {
                    "strategy": bname,
                    "window": win.name,
                    "window_role": win.window_role,
                    "case": "baseline",
                    "experiment_origin": "FL_NEW_EXPERIMENT",
                    "signal_source": "PASSIVE_BENCHMARK",
                    "days": bm["days"],
                    "fills": 0,
                    "fees": 0.0,
                    "exposure": bm["exposure"],
                    **{k: bm[k] for k in ("net_return", "cagr", "max_dd", "sharpe", "sortino", "calmar", "alpha", "beta", "benchmark_return")},
                }
            )

        cases = ["normal", "stress"]
        if win.name in ("target", "twenty"):
            cases.append("no_sndk")

        sig_cache: dict[tuple[str, bool], Any] = {}
        for version in config.strategies:
            cap = config.v69_reference_cap if version == "v69" else config.gross_cap
            for case in cases:
                omit_sndk = win.default_omit_sndk or (case == "no_sndk")
                fee = config.stress_fee_rate if case == "stress" else config.normal_fee_rate
                if (version, omit_sndk) not in sig_cache:
                    sig_cache[(version, omit_sndk)] = build_signal_provider(version, market, package_dir=pkg, omit_sndk=omit_sndk)
                sig = sig_cache[(version, omit_sndk)]
                bt = FactorLabIndependentBacktester(
                    initial_capital=config.initial_capital_usd,
                    fee_rate=fee,
                    open_price_mode="float64",
                    gross_cap=cap,
                )
                res = bt.run(sig, market, window_name=win.name, start=win.start, end=win.end, case=case)
                results_cache[(version, win.name, case)] = res
                key = f"{version}/{win.name}/{case}"
                sp = res.sp500_metrics
                rows.append(
                    {
                        "strategy": version,
                        "window": win.name,
                        "window_role": win.window_role,
                        "case": case,
                        "experiment_origin": "ARCHIVED_ORIGINAL_COMBO" if key in archived_set else "FL_NEW_EXPERIMENT",
                        "signal_source": res.signal_source,
                        "days": sp["days"],
                        "fills": sp["fills"],
                        "fees": sp["fees"],
                        "exposure": sp["exposure"],
                        "net_usd": sp["net_usd"],
                        "turnover_usd": sp["turnover_usd"],
                        **{k: sp[k] for k in ("net_return", "cagr", "max_dd", "sharpe", "sortino", "calmar", "alpha", "beta", "benchmark_return")},
                        "qqq_alpha": res.qqq_metrics["alpha"],
                        "qqq_beta": res.qqq_metrics["beta"],
                    }
                )
    return rows, results_cache


def run_execution_robustness_suite(
    config: ExternalValidationConfig,
    markets: dict[str, MarketPanel],
) -> list[dict[str, Any]]:
    """Evaluate execution cost, slippage, capacity, partial fill, delay, and capital scale sensitivity."""
    pkg = Path(config.packages.v179_v182_dir)
    scenarios = [
        StressScenarioSpec("base_normal_10bps", fee_rate=0.001, description="Reference 10 bps one-way"),
        StressScenarioSpec("fee_3x_30bps", fee_rate=0.003, description="3x fee (30 bps one-way)"),
        StressScenarioSpec("fee_5x_50bps", fee_rate=0.005, description="5x fee (50 bps one-way)"),
        StressScenarioSpec("slippage_10bps", fee_rate=0.001, slippage_bps=10.0, description="10 bps fee + 10 bps adverse slippage"),
        StressScenarioSpec("slippage_25bps", fee_rate=0.001, slippage_bps=25.0, description="10 bps fee + 25 bps adverse slippage"),
        StressScenarioSpec("fee_3x_slip_10bps", fee_rate=0.003, slippage_bps=10.0, description="30 bps fee + 10 bps slippage"),
        StressScenarioSpec("vol_cap_tight_0.02pct", fee_rate=0.001, volume_cap_ratio=0.0002, description="5x tighter 0.02% volume cap"),
        StressScenarioSpec("partial_fill_70pct", fee_rate=0.001, fill_ratio=0.70, description="70% partial fill per order"),
        StressScenarioSpec("partial_fill_50pct", fee_rate=0.001, fill_ratio=0.50, description="50% partial fill per order"),
        StressScenarioSpec("delay_1d_open", fee_rate=0.001, execution_delay_days=1, description="1 trading day execution delay"),
        StressScenarioSpec("intraday_adverse_mid", fee_rate=0.001, open_price_mode="open_high_mid", description="Adverse open-high/low midpoint fill"),
        StressScenarioSpec("capital_100k", fee_rate=0.001, initial_capital=100_000.0, description="$100k capital"),
        StressScenarioSpec("capital_1m", fee_rate=0.001, initial_capital=1_000_000.0, description="$1M capital"),
        StressScenarioSpec("capital_5m", fee_rate=0.001, initial_capital=5_000_000.0, description="$5M capital"),
        StressScenarioSpec("capital_20m", fee_rate=0.001, initial_capital=20_000_000.0, description="$20M capital (volume-cap test)"),
    ]

    rows: list[dict[str, Any]] = []
    wmap = config.window_map()
    for win_name in ("target", "twenty"):
        win = wmap[win_name]
        market = markets[win_name]
        for version in config.strategies:
            cap = config.v69_reference_cap if version == "v69" else config.gross_cap
            # Build signal provider once per (version, window) since signals depend only on market & omit_sndk
            sig = build_signal_provider(version, market, package_dir=pkg, omit_sndk=False)
            for sc in scenarios:
                bt = FactorLabIndependentBacktester(
                    initial_capital=sc.initial_capital,
                    fee_rate=sc.fee_rate,
                    slippage_bps=sc.slippage_bps,
                    volume_cap_ratio=sc.volume_cap_ratio,
                    fill_ratio=sc.fill_ratio,
                    execution_delay_days=sc.execution_delay_days,
                    open_price_mode=sc.open_price_mode,
                    gross_cap=cap,
                )
                res = bt.run(sig, market, window_name=win_name, start=win.start, end=win.end, case=sc.name)
                sp = res.sp500_metrics
                rows.append(
                    {
                        "strategy": version,
                        "window": win_name,
                        "scenario": sc.name,
                        "description": sc.description,
                        "experiment_origin": "FL_NEW_EXPERIMENT",
                        "initial_capital": sc.initial_capital,
                        "fills": sp["fills"],
                        "fees": sp["fees"],
                        "exposure": sp["exposure"],
                        "net_return": sp["net_return"],
                        "cagr": sp["cagr"],
                        "max_dd": sp["max_dd"],
                        "sharpe": sp["sharpe"],
                        "sortino": sp["sortino"],
                        "calmar": sp["calmar"],
                        "alpha": sp["alpha"],
                        "beta": sp["beta"],
                    }
                )
    return rows


def run_concentration_and_ablation(
    config: ExternalValidationConfig,
    markets: dict[str, MarketPanel],
    base_results: dict[tuple[str, str, str], IndependentReplayResult],
) -> dict[str, Any]:
    """Leave-one-asset-out ablation and PnL concentration analysis (especially SNDK and TQQQ)."""
    pkg = Path(config.packages.v179_v182_dir)
    wmap = config.window_map()

    attribution_summary: list[dict[str, Any]] = []
    ablation_rows: list[dict[str, Any]] = []

    for win_name in ("target", "twenty"):
        win = wmap[win_name]
        market = markets[win_name]
        for version in config.strategies:
            base = base_results[(version, win_name, "normal")]
            attr = base.attribution
            total_pnl = float(attr.sum())
            pos_pnl = attr.clip(lower=0.0)
            pos_sum = float(pos_pnl.sum())
            shares = {k: (float(attr[k]) / total_pnl if abs(total_pnl) > 1e-9 else 0.0) for k in DEFAULT_TICKERS}
            pos_hhi = float(((pos_pnl / pos_sum) ** 2).sum()) if pos_sum > 0 else 0.0
            top_ticker = str(attr.idxmax())
            attribution_summary.append(
                {
                    "strategy": version,
                    "window": win_name,
                    "total_net_pnl_usd": total_pnl,
                    "top_contributor": top_ticker,
                    "top_contributor_usd": float(attr[top_ticker]),
                    "top_contributor_share": shares[top_ticker],
                    "sndk_pnl_usd": float(attr["SNDK"]),
                    "sndk_share": shares["SNDK"],
                    "tqqq_pnl_usd": float(attr["TQQQ"]),
                    "tqqq_share": shares["TQQQ"],
                    "positive_pnl_hhi": pos_hhi,
                    "per_ticker_usd": {k: float(attr[k]) for k in DEFAULT_TICKERS},
                    "per_ticker_share": shares,
                }
            )

            cap = config.v69_reference_cap if version == "v69" else config.gross_cap
            omit_sets: list[tuple[str, tuple[str, ...]]] = [(f"omit_{t}", (t,)) for t in DEFAULT_TICKERS]
            omit_sets.append(("omit_SNDK_TQQQ", ("SNDK", "TQQQ")))
            for label, omits in omit_sets:
                sig = build_signal_provider(
                    version,
                    market,
                    package_dir=pkg,
                    omit_sndk=("SNDK" in omits),
                    omit_symbols=omits,
                )
                bt = FactorLabIndependentBacktester(
                    initial_capital=config.initial_capital_usd,
                    fee_rate=config.normal_fee_rate,
                    open_price_mode="float64",
                    gross_cap=cap,
                )
                res = bt.run(sig, market, window_name=win_name, start=win.start, end=win.end, case=label)
                sp = res.sp500_metrics
                ablation_rows.append(
                    {
                        "strategy": version,
                        "window": win_name,
                        "ablation": label,
                        "omitted": list(omits),
                        "net_return": sp["net_return"],
                        "delta_net_return_pp": (sp["net_return"] - base.sp500_metrics["net_return"]) * 100.0,
                        "cagr": sp["cagr"],
                        "delta_cagr_pp": ((sp["cagr"] or 0.0) - (base.sp500_metrics["cagr"] or 0.0)) * 100.0,
                        "max_dd": sp["max_dd"],
                        "sharpe": sp["sharpe"],
                        "alpha": sp["alpha"],
                        "beta": sp["beta"],
                        "exposure": sp["exposure"],
                    }
                )

    return {
        "attribution_summary": attribution_summary,
        "leave_one_out_ablation": ablation_rows,
    }


def run_regime_decomposition(
    base_results: dict[tuple[str, str, str], IndependentReplayResult],
    markets: dict[str, MarketPanel],
) -> dict[str, Any]:
    """Decompose performance across historical crises, bear/sideways/bull, and volatility regimes."""
    # Combine challenge (2001-2006) + twenty (2006-2026) daily series for full 25-year regime view
    calendar_regimes = [
        ("dotcom_bust_2001_2002", "challenge", "2001-01-02", "2002-12-31"),
        ("recovery_2003_2006", "challenge", "2003-01-02", "2006-11-17"),
        ("gfc_crisis_2008_2009", "twenty", "2008-01-02", "2009-06-30"),
        ("euro_sideways_2011_2012", "twenty", "2011-01-03", "2012-12-31"),
        ("low_vol_bull_2013_2019", "twenty", "2013-01-02", "2019-12-31"),
        ("covid_crash_rebound_2020", "twenty", "2020-01-02", "2020-12-31"),
        ("rate_hike_bear_2022", "twenty", "2022-01-03", "2022-12-30"),
        ("ai_bull_2023_2026", "target", "2023-01-03", "2026-11-23"),
    ]
    calendar_rows: list[dict[str, Any]] = []
    for rname, src_win, sdate, edate in calendar_regimes:
        for version in ("v69", "v179", "v182"):
            res = base_results[(version, src_win, "normal")]
            sub = res.daily.loc[sdate:edate]
            m = compute_risk_metrics(sub["net_daily"], sub["sp500_daily"])
            calendar_rows.append(
                {
                    "regime": rname,
                    "source_window": src_win,
                    "start": str(sub.index.min().date()),
                    "end": str(sub.index.max().date()),
                    "days": int(len(sub)),
                    "strategy": version,
                    "exposure": float(sub["weight"].mean()),
                    **m,
                }
            )

    # Quantitative volatility & trend regimes on the 20-year window (2006-2026)
    quant_rows: list[dict[str, Any]] = []
    sp_close = markets["twenty"].benchmark_close
    sp_ret = sp_close.pct_change(fill_method=None)
    sp_vol21 = sp_ret.rolling(21).std() * np.sqrt(252.0)
    sp_ret63 = sp_close.pct_change(63, fill_method=None)

    twenty_idx = base_results[("v179", "twenty", "normal")].daily.index
    vol_s = sp_vol21.reindex(twenty_idx)
    ret63_s = sp_ret63.reindex(twenty_idx)
    vol_hi_cut = float(vol_s.quantile(0.75))
    vol_lo_cut = float(vol_s.quantile(0.25))

    regime_masks = {
        "high_vol_top25pct": vol_s >= vol_hi_cut,
        "normal_vol_mid50pct": (vol_s > vol_lo_cut) & (vol_s < vol_hi_cut),
        "low_vol_bot25pct": vol_s <= vol_lo_cut,
        "bear_63d_lt_neg5pct": ret63_s < -0.05,
        "sideways_63d_neg5_to_pos5pct": (ret63_s >= -0.05) & (ret63_s <= 0.05),
        "bull_63d_gt_pos5pct": ret63_s > 0.05,
    }
    for rname, mask in regime_masks.items():
        for version in ("v69", "v179", "v182"):
            sub = base_results[(version, "twenty", "normal")].daily.loc[mask]
            r = sub["net_daily"].to_numpy(dtype=np.float64)
            b = sub["sp500_daily"].to_numpy(dtype=np.float64)
            ann_ret = float(r.mean() * 252.0)
            ann_vol = float(r.std(ddof=1) * np.sqrt(252.0))
            ann_bench = float(b.mean() * 252.0)
            b_var = float(b.var(ddof=1))
            beta = float(np.cov(r, b, ddof=1)[0, 1] / b_var) if b_var > 0 else float("nan")
            quant_rows.append(
                {
                    "regime": rname,
                    "days": int(len(sub)),
                    "strategy": version,
                    "ann_mean_return": ann_ret,
                    "ann_volatility": ann_vol,
                    "conditional_sharpe": ann_ret / ann_vol if ann_vol > 1e-12 else None,
                    "bench_ann_mean_return": ann_bench,
                    "conditional_beta": beta,
                    "conditional_alpha": float((r.mean() - beta * b.mean()) * 252.0),
                    "mean_exposure": float(sub["weight"].mean()),
                    "worst_day_return": float(r.min()),
                }
            )

    return {
        "calendar_regimes": calendar_rows,
        "quantitative_regimes_twenty_year": quant_rows,
        "volatility_thresholds_ann": {"p25": vol_lo_cut, "p75": vol_hi_cut},
    }


def deflated_sharpe_ratio(returns: np.ndarray, *, n_trials: int) -> dict[str, float]:
    """Bailey & Lopez de Prado (2014) Deflated Sharpe Ratio accounting for skewness, kurtosis & multiple testing."""
    r = np.asarray(returns, dtype=np.float64)
    n = len(r)
    sr_daily = float(r.mean() / r.std(ddof=1))
    sr_ann = sr_daily * math.sqrt(252.0)
    z = (r - r.mean()) / r.std(ddof=1)
    skew = float(np.mean(z**3))
    kurt = float(np.mean(z**4))
    euler_gamma = 0.5772156649015329
    if n_trials <= 1:
        sr_star_daily = 0.0
    else:
        # Expected max Sharpe under null (in daily units, assuming cross-trial SR variance ~ 1/n)
        sr_std_trials = math.sqrt(1.0 / max(1, n - 1))
        z1 = float(norm.ppf(1.0 - 1.0 / n_trials))
        z2 = float(norm.ppf(1.0 - 1.0 / (n_trials * math.e)))
        sr_star_daily = sr_std_trials * ((1.0 - euler_gamma) * z1 + euler_gamma * z2)
    denom_sq = max(1e-12, 1.0 - skew * sr_daily + 0.25 * (kurt - 1.0) * (sr_daily**2))
    t_stat = (sr_daily - sr_star_daily) * math.sqrt(max(1, n - 1)) / math.sqrt(denom_sq)
    dsr_prob = float(norm.cdf(t_stat))
    return {
        "n_days": n,
        "n_trials": n_trials,
        "sharpe_ann": sr_ann,
        "skewness": skew,
        "kurtosis": kurt,
        "expected_max_null_sr_ann": sr_star_daily * math.sqrt(252.0),
        "dsr_probability": dsr_prob,
    }


def run_parameter_sensitivity_and_overfitting(
    config: ExternalValidationConfig,
    markets: dict[str, MarketPanel],
    base_results: dict[tuple[str, str, str], IndependentReplayResult],
) -> dict[str, Any]:
    """Assess deadband sensitivity, blend weight sensitivity, and Deflated Sharpe overfitting diagnostics."""
    pkg = Path(config.packages.v179_v182_dir)
    wmap = config.window_map()

    deadband_rows: list[dict[str, Any]] = []
    for win_name in ("target", "twenty"):
        win = wmap[win_name]
        market = markets[win_name]
        for version in config.strategies:
            cap = config.v69_reference_cap if version == "v69" else config.gross_cap
            sig = build_signal_provider(version, market, package_dir=pkg, omit_sndk=False)
            for db in (0.0025, 0.005, 0.010, 0.020):
                bt = FactorLabIndependentBacktester(
                    initial_capital=config.initial_capital_usd,
                    fee_rate=config.normal_fee_rate,
                    rebalance_deadband=db,
                    open_price_mode="float64",
                    gross_cap=cap,
                )
                res = bt.run(sig, market, window_name=win_name, start=win.start, end=win.end, case=f"deadband_{db}")
                sp = res.sp500_metrics
                deadband_rows.append(
                    {
                        "strategy": version,
                        "window": win_name,
                        "deadband": db,
                        "fills": sp["fills"],
                        "fees": sp["fees"],
                        "cagr": sp["cagr"],
                        "max_dd": sp["max_dd"],
                        "sharpe": sp["sharpe"],
                        "alpha": sp["alpha"],
                    }
                )

    blend_rows: list[dict[str, Any]] = []
    for win_name in ("target", "twenty", "challenge"):
        win = wmap[win_name]
        market = markets[win_name]
        for version, perturbations in (
            ("v179", [("ref_75_25", (0.75, 0.25)), ("mild_65_35", (0.65, 0.35)), ("steep_85_15", (0.85, 0.15)), ("static_50_50", (0.50, 0.50))]),
            ("v182", [("ref_75_40", (0.75, 0.40)), ("mild_65_40", (0.65, 0.40)), ("steep_85_30", (0.85, 0.30)), ("static_50_50", (0.50, 0.50))]),
        ):
            for label, override in perturbations:
                sig = build_signal_provider(version, market, package_dir=pkg, omit_sndk=False, blend_override=override)
                bt = FactorLabIndependentBacktester(
                    initial_capital=config.initial_capital_usd,
                    fee_rate=config.normal_fee_rate,
                    open_price_mode="float64",
                    gross_cap=config.gross_cap,
                )
                res = bt.run(sig, market, window_name=win_name, start=win.start, end=win.end, case=label)
                sp = res.sp500_metrics
                blend_rows.append(
                    {
                        "strategy": version,
                        "window": win_name,
                        "blend_variant": label,
                        "high_share": override[0],
                        "low_share": override[1],
                        "cagr": sp["cagr"],
                        "max_dd": sp["max_dd"],
                        "sharpe": sp["sharpe"],
                        "alpha": sp["alpha"],
                    }
                )

    dsr_rows: list[dict[str, Any]] = []
    for win_name in ("target", "twenty", "challenge"):
        for case in ("normal", "no_sndk") if win_name in ("target", "twenty") else ("normal",):
            for version in config.strategies:
                r = base_results[(version, win_name, case)].daily["net_daily"].to_numpy(dtype=np.float64)
                for n_trials in (1, 10, 50, 135, 182):
                    dsr = deflated_sharpe_ratio(r, n_trials=n_trials)
                    dsr_rows.append({"strategy": version, "window": win_name, "case": case, **dsr})

    return {
        "deadband_sensitivity": deadband_rows,
        "blend_parameter_sensitivity": blend_rows,
        "deflated_sharpe_overfitting": dsr_rows,
    }
