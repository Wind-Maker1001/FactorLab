"""Phase 4 & 5: Multi-asset Monte Carlo risk study and statistical convergence analysis.

Tier 1: Fast fixed-weight portfolio risk simulation via existing FactorLab MonteCarloRunner
        (RTX 2070 CuPy CUDA when available, otherwise NumPy CPU).
Tier 2: Full dynamic strategy lifecycle simulation over jointly generated causal multi-asset
        paths (8 equities/ETFs OHLCV + S&P 500 Total Return index), recomputing all indicators,
        breadth, Ridge forecasts, dynamic targets, orders, fills, fees, and daily NAV on paired
        paths across V69, V179, and V182.
"""
from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor
import json
import math
import os
from pathlib import Path
import subprocess
import tempfile
from time import perf_counter, process_time
from typing import Any

import numpy as np
import pandas as pd

from factorlab.compute import ComputeConfig
from factorlab.research.monte_carlo import MonteCarloConfig, MonteCarloInput, MonteCarloRunner
from factorlab.research.monte_carlo.reliability import repeat_summary, summarize_paths, wilson_interval

from .config import DEFAULT_TICKERS, ExternalValidationConfig, MonteCarloValidationConfig
from .replay import MarketPanel

# 9 joint price dimensions: 8 universe tickers + SP500TR benchmark
JOINT_ASSETS: tuple[str, ...] = (*DEFAULT_TICKERS, "SP500TR")


def _peak_rss_bytes() -> int | None:
    try:
        import ctypes
        from ctypes import wintypes

        class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        counters = PROCESS_MEMORY_COUNTERS()
        counters.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS)
        handle = ctypes.windll.kernel32.GetCurrentProcess()
        if ctypes.windll.psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
            return int(counters.PeakWorkingSetSize)
    except Exception:
        pass
    return None


def prepare_calibration_dataset(
    market: MarketPanel,
    *,
    start: str,
    end: str,
    include_sndk: bool = False,
) -> dict[str, Any]:
    """Extract aligned 9-asset simple returns, overnight gaps, volume ratios, and warmup anchor."""
    close_df = market.close.loc[start:end, list(DEFAULT_TICKERS)].copy()
    open_df = market.opening.loc[start:end, list(DEFAULT_TICKERS)].copy()
    vol_df = market.volume.loc[start:end, list(DEFAULT_TICKERS)].copy()
    sp = market.benchmark_close.reindex(close_df.index)

    # For pre-SNDK or pre-TQQQ history, keep complete core rows or use valid rows for each window
    joint_close = close_df.assign(SP500TR=sp)
    if not include_sndk:
        # Exclude SNDK from complete-case joint calibration when calibrating on long history (prior to 2025-02-24),
        # and synthesize SNDK as correlated tech proxy only if active; during MC we can test both SNDK-excluded
        # and SNDK-included regimes cleanly.
        valid_cols = [c for c in JOINT_ASSETS if c != "SNDK"]
        joint_valid = joint_close[valid_cols].dropna()
    else:
        joint_valid = joint_close.dropna()

    idx = joint_valid.index
    returns = joint_valid.pct_change(fill_method=None).dropna()
    ret_idx = returns.index

    # Overnight open/prev_close log-gap ratios and normalized volume dynamics
    prev_close = close_df.shift(1).reindex(ret_idx)
    open_gap = (open_df.reindex(ret_idx) / prev_close - 1.0).fillna(0.0).clip(-0.15, 0.15)
    vol_med = vol_df.reindex(ret_idx).median().replace(0.0, 1.0).fillna(1e7)
    vol_ratio = (vol_df.reindex(ret_idx) / vol_med).fillna(1.0).clip(0.1, 10.0)

    # Historical warmup tail (last 252 sessions of actual data before synthetic horizon)
    warmup_close = close_df.reindex(idx).tail(252)
    warmup_open = open_df.reindex(idx).tail(252)
    warmup_vol = vol_df.reindex(idx).tail(252)
    warmup_sp = sp.reindex(idx).tail(252)

    return {
        "assets": list(joint_valid.columns),
        "include_sndk": include_sndk,
        "dates": [str(d.date()) for d in ret_idx],
        "returns": returns.to_numpy(dtype=np.float64),
        "open_gap": open_gap[[c for c in DEFAULT_TICKERS if c in joint_valid.columns]].to_numpy(dtype=np.float64),
        "vol_ratio": vol_ratio[[c for c in DEFAULT_TICKERS if c in joint_valid.columns]].to_numpy(dtype=np.float64),
        "vol_median": vol_med.to_dict(),
        "warmup_close": warmup_close.to_numpy(dtype=np.float64),
        "warmup_open": warmup_open.to_numpy(dtype=np.float64),
        "warmup_vol": warmup_vol.to_numpy(dtype=np.float64),
        "warmup_sp": warmup_sp.to_numpy(dtype=np.float64),
        "n_obs": int(len(returns)),
        "first_date": str(ret_idx.min().date()),
        "last_date": str(ret_idx.max().date()),
    }


def run_tier1_fast_portfolio_mc(
    config: ExternalValidationConfig,
    calib: dict[str, Any],
    strategy_avg_weights: dict[str, dict[str, float]],
    output_dir: Path,
) -> dict[str, Any]:
    """Tier 1: Fast fixed-weight multi-asset MC using existing FactorLab MonteCarloRunner (GPU if available)."""
    mc_cfg = config.monte_carlo
    assets = [c for c in calib["assets"] if c != "SP500TR"]
    ret_mat = calib["returns"][:, : len(assets)]
    log_ret = np.log1p(ret_mat)
    drift = (log_ret.mean(axis=0) + 0.5 * log_ret.var(axis=0, ddof=1)) * 252.0
    vol = log_ret.std(axis=0, ddof=1) * math.sqrt(252.0)
    corr = np.corrcoef(log_ret, rowvar=False)
    corr = 0.5 * (corr + corr.T)
    np.fill_diagonal(corr, 1.0)
    timestamps = pd.to_datetime(calib["dates"])
    as_of = timestamps[-1]

    gpu_py = config.packages.gpu_python
    use_gpu_sub = bool(gpu_py and Path(gpu_py).is_file())
    started = perf_counter()

    if use_gpu_sub:
        # Delegate Tier-1 runs to gpu-venv where cupy-cuda12x is installed, reusing exact MonteCarloRunner!
        payload = {
            "assets": assets,
            "returns": ret_mat.tolist(),
            "dates": calib["dates"],
            "drift": drift.tolist(),
            "vol": vol.tolist(),
            "corr": corr.tolist(),
            "strategy_avg_weights": {
                s: [float(w.get(a, 0.0)) for a in assets] for s, w in strategy_avg_weights.items()
            },
            "scales": list(mc_cfg.tier1_path_scales),
            "seeds": list(mc_cfg.seeds),
            "horizon": mc_cfg.horizon_days,
            "block_size": mc_cfg.block_size,
            "resamples": mc_cfg.bootstrap_resamples,
        }
        with tempfile.TemporaryDirectory(prefix="p10-04-tier1-gpu-", dir=str(output_dir)) as tmp:
            in_path = Path(tmp) / "in.json"
            out_path = Path(tmp) / "out.json"
            in_path.write_text(json.dumps(payload), encoding="utf-8")
            script = Path(tmp) / "run_gpu.py"
            script.write_text(
                """import json, sys, numpy as np, pandas as pd
from time import perf_counter
from factorlab.compute import ComputeConfig
from factorlab.research.monte_carlo import MonteCarloConfig, MonteCarloInput, MonteCarloRunner
from factorlab.research.monte_carlo.reliability import summarize_paths

data = json.loads(open(sys.argv[1], encoding='utf-8').read())
assets = tuple(data['assets'])
ret_mat = np.asarray(data['returns'], dtype=np.float64)
ts = pd.to_datetime(data['dates']).to_numpy(dtype='datetime64[ns]')
as_of = ts[-1]
drift = np.asarray(data['drift'], dtype=np.float64)
vol = np.asarray(data['vol'], dtype=np.float64)
corr = np.asarray(data['corr'], dtype=np.float64)

rows = []
t0 = perf_counter()
for strat, w_list in data['strategy_avg_weights'].items():
    w = np.asarray(w_list, dtype=np.float64)
    if w.sum() > 1.0:
        w = w / w.sum()
    for model in ('gbm', 'moving_block_bootstrap'):
        for n_paths in data['scales']:
            for seed in data['seeds']:
                cfg = MonteCarloConfig(
                    model=model, n_paths=int(n_paths), n_steps=int(data['horizon']),
                    seed=int(seed), dt=1.0/252.0, initial_value=1.0,
                    block_size=int(data['block_size']),
                    compute=ComputeConfig(device='auto', allow_fallback=True),
                )
                if model == 'gbm':
                    inp = MonteCarloInput(weights=w, drift=drift, volatility=vol, correlation=corr, assets=assets)
                else:
                    inp = MonteCarloInput(
                        weights=w, historical_returns=ret_mat, timestamps=ts, available_at=ts,
                        as_of=as_of, assets=assets,
                    )
                res = MonteCarloRunner(cfg).run(inp)
                losses = 1.0 - res.terminal_values
                stats = summarize_paths(losses, res.maximum_drawdowns, threshold=0.20,
                                        confidences=(0.95, 0.99), resamples=int(data['resamples']),
                                        bootstrap_seed=int(seed))
                rows.append({
                    'strategy': strat, 'model': model, 'n_paths': int(n_paths), 'seed': int(seed),
                    'backend': res.metadata['compute']['backend'],
                    'device': res.metadata['compute']['device'],
                    'elapsed_seconds': res.metadata['elapsed_seconds'],
                    'private_pool_peak_bytes': res.metadata['compute']['execution'].get('private_pool_peak_bytes'),
                    'var_95': stats['tails']['0.95']['var'],
                    'cvar_95': stats['tails']['0.95']['cvar'],
                    'cvar_95_ci_width': stats['tails']['0.95']['cvar_interval']['upper'] - stats['tails']['0.95']['cvar_interval']['lower'],
                    'var_99': stats['tails']['0.99']['var'],
                    'cvar_99': stats['tails']['0.99']['cvar'],
                    'cvar_99_ci_width': stats['tails']['0.99']['cvar_interval']['upper'] - stats['tails']['0.99']['cvar_interval']['lower'],
                    'loss_probability': stats['loss_probability']['estimate'],
                    'dd_breach_20pct': stats['drawdown_breach_probability']['estimate'],
                    'mdd_p50': stats['maximum_drawdown_quantiles']['0.5']['estimate'],
                    'mdd_p95': stats['maximum_drawdown_quantiles']['0.95']['estimate'],
                    'mdd_p99': stats['maximum_drawdown_quantiles']['0.99']['estimate'],
                    'tail_99_status': stats['tails']['0.99']['statistical_status'],
                    'expected_tail_99': stats['tails']['0.99']['expected_tail_paths'],
                })
open(sys.argv[2], 'w', encoding='utf-8').write(json.dumps({'total_seconds': perf_counter()-t0, 'rows': rows}, indent=2))
""",
                encoding="utf-8",
            )
            env = dict(os.environ)
            src_root = str(Path(__file__).resolve().parents[3])
            env["PYTHONPATH"] = src_root + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
            proc = subprocess.run([gpu_py, str(script), str(in_path), str(out_path)], env=env, capture_output=True, text=True)
            if proc.returncode == 0:
                gpu_res = json.loads(out_path.read_text(encoding="utf-8"))
                return {
                    "tier": "TIER1_FAST_FIXED_WEIGHT_PORTFOLIO_NOT_DYNAMIC_STRATEGY",
                    "execution_mode": "GPU_SUBPROCESS_CUPY",
                    "wall_seconds": perf_counter() - started,
                    "gpu_inner_seconds": gpu_res["total_seconds"],
                    "calibration": {
                        "assets": assets,
                        "n_obs": calib["n_obs"],
                        "first_date": calib["first_date"],
                        "last_date": calib["last_date"],
                    },
                    "rows": gpu_res["rows"],
                }

    # Fallback: CPU in-process MonteCarloRunner
    rows = []
    ts_np = timestamps.to_numpy(dtype="datetime64[ns]")
    for strat, w_dict in strategy_avg_weights.items():
        w = np.asarray([float(w_dict.get(a, 0.0)) for a in assets], dtype=np.float64)
        if w.sum() > 1.0:
            w = w / w.sum()
        for model in ("gbm", "moving_block_bootstrap"):
            for n_paths in mc_cfg.tier1_path_scales:
                for seed in mc_cfg.seeds:
                    cfg = MonteCarloConfig(
                        model=model,
                        n_paths=int(n_paths),
                        n_steps=int(mc_cfg.horizon_days),
                        seed=int(seed),
                        dt=1.0 / 252.0,
                        initial_value=1.0,
                        block_size=int(mc_cfg.block_size),
                        compute=ComputeConfig(device="cpu"),
                    )
                    if model == "gbm":
                        inp = MonteCarloInput(weights=w, drift=drift, volatility=vol, correlation=corr, assets=tuple(assets))
                    else:
                        inp = MonteCarloInput(
                            weights=w,
                            historical_returns=ret_mat,
                            timestamps=ts_np,
                            available_at=ts_np,
                            as_of=ts_np[-1],
                            assets=tuple(assets),
                        )
                    res = MonteCarloRunner(cfg).run(inp)
                    losses = 1.0 - res.terminal_values
                    stats = summarize_paths(
                        losses,
                        res.maximum_drawdowns,
                        threshold=0.20,
                        confidences=(0.95, 0.99),
                        resamples=mc_cfg.bootstrap_resamples,
                        bootstrap_seed=int(seed),
                    )
                    rows.append(
                        {
                            "strategy": strat,
                            "model": model,
                            "n_paths": int(n_paths),
                            "seed": int(seed),
                            "backend": res.metadata["compute"]["backend"],
                            "device": res.metadata["compute"]["device"],
                            "elapsed_seconds": res.metadata["elapsed_seconds"],
                            "private_pool_peak_bytes": None,
                            "var_95": stats["tails"]["0.95"]["var"],
                            "cvar_95": stats["tails"]["0.95"]["cvar"],
                            "cvar_95_ci_width": stats["tails"]["0.95"]["cvar_interval"]["upper"]
                            - stats["tails"]["0.95"]["cvar_interval"]["lower"],
                            "var_99": stats["tails"]["0.99"]["var"],
                            "cvar_99": stats["tails"]["0.99"]["cvar"],
                            "cvar_99_ci_width": stats["tails"]["0.99"]["cvar_interval"]["upper"]
                            - stats["tails"]["0.99"]["cvar_interval"]["lower"],
                            "loss_probability": stats["loss_probability"]["estimate"],
                            "dd_breach_20pct": stats["drawdown_breach_probability"]["estimate"],
                            "mdd_p50": stats["maximum_drawdown_quantiles"]["0.5"]["estimate"],
                            "mdd_p95": stats["maximum_drawdown_quantiles"]["0.95"]["estimate"],
                            "mdd_p99": stats["maximum_drawdown_quantiles"]["0.99"]["estimate"],
                            "tail_99_status": stats["tails"]["0.99"]["statistical_status"],
                            "expected_tail_99": stats["tails"]["0.99"]["expected_tail_paths"],
                        }
                    )
    return {
        "tier": "TIER1_FAST_FIXED_WEIGHT_PORTFOLIO_NOT_DYNAMIC_STRATEGY",
        "execution_mode": "CPU_NUMPY_FALLBACK",
        "wall_seconds": perf_counter() - started,
        "calibration": {
            "assets": assets,
            "n_obs": calib["n_obs"],
            "first_date": calib["first_date"],
            "last_date": calib["last_date"],
        },
        "rows": rows,
    }


# ---------------------------------------------------------------------------
# Tier 2: Full Dynamic Strategy Lifecycle Simulation (Joint 9-Asset Causal Paths)
# ---------------------------------------------------------------------------

def _fit_joint_model_params(calib: dict[str, Any], block_size: int = 20) -> dict[str, Any]:
    """Calibrate all 6 joint multi-asset + SP500TR scenario generators strictly from historical calibration window."""
    ret = calib["returns"]  # shape (T, d), last column is SP500TR
    d = ret.shape[1]
    log_ret = np.log1p(ret)
    mu_log = log_ret.mean(axis=0)
    cov_log = np.cov(log_ret, rowvar=False, ddof=1)
    eig, vec = np.linalg.eigh(0.5 * (cov_log + cov_log.T))
    chol = vec * np.sqrt(np.maximum(eig, 1e-12))

    # High-correlation crisis covariance (correlation pushed toward 0.88)
    sd = np.sqrt(np.maximum(np.diag(cov_log), 1e-12))
    corr = cov_log / np.outer(sd, sd)
    corr_shock = 0.25 * corr + 0.75 * np.full((d, d), 0.88)
    np.fill_diagonal(corr_shock, 1.0)
    cov_shock = corr_shock * np.outer(sd * 1.45, sd * 1.45)
    eig_s, vec_s = np.linalg.eigh(0.5 * (cov_shock + cov_shock.T))
    chol_shock = vec_s * np.sqrt(np.maximum(eig_s, 1e-12))

    # Regime states from SP500TR (last column) 20-day rolling vol and 20-day return
    sp_log = log_ret[:, -1]
    lookback = block_size
    n_obs = len(sp_log)
    vols = np.array([sp_log[i - lookback : i].std(ddof=1) for i in range(lookback, n_obs)])
    trends = np.array([sp_log[i - lookback : i].sum() for i in range(lookback, n_obs)])
    vol_med = float(np.median(vols))
    # 4 states: 0=low_down, 1=low_up, 2=high_down, 3=high_up
    states = (vols > vol_med).astype(int) * 2 + (trends >= 0.0).astype(int)
    # Admissible block starts (offset by lookback in original ret array)
    max_start = n_obs - block_size
    pools: dict[int, list[int]] = {s: [] for s in range(4)}
    trans = np.ones((4, 4), dtype=np.float64) * 0.5  # Laplace-smoothed empirical transitions
    for idx_s, st in enumerate(states):
        orig_i = lookback + idx_s
        if orig_i <= max_start:
            pools[int(st)].append(orig_i)
            nxt_idx = idx_s + block_size
            if nxt_idx < len(states):
                trans[int(st), int(states[nxt_idx])] += 1.0
    trans = trans / trans.sum(axis=1, keepdims=True)
    for s in range(4):
        if not pools[s]:
            pools[s] = list(range(0, max_start + 1))

    # GARCH(1,1) + Student-t (nu=5) parameters per asset (quasi-MLE style calibrated moments)
    # Standard equity daily persistence: omega = (1 - alpha - beta) * var, alpha=0.07, beta=0.90
    garch_alpha = 0.07
    garch_beta = 0.90
    uncond_var = np.maximum(np.var(log_ret, axis=0, ddof=1), 1e-10)
    garch_omega = uncond_var * (1.0 - garch_alpha - garch_beta)
    corr_eig, corr_vec = np.linalg.eigh(0.5 * (corr + corr.T))
    corr_root = corr_vec * np.sqrt(np.maximum(corr_eig, 1e-12))

    # Open-gap standard deviation & mean volume ratios per equity asset
    n_eq = d - 1
    gap_std = calib["open_gap"].std(axis=0, ddof=1)
    vol_log_mean = np.log(np.maximum(calib["vol_ratio"], 0.1)).mean(axis=0)
    vol_log_std = np.log(np.maximum(calib["vol_ratio"], 0.1)).std(axis=0, ddof=1)

    return {
        "assets": calib["assets"],
        "n_dim": d,
        "n_eq": n_eq,
        "mu_log": mu_log,
        "chol": chol,
        "chol_shock": chol_shock,
        "ret_hist": ret,
        "open_gap_hist": calib["open_gap"],
        "vol_ratio_hist": calib["vol_ratio"],
        "block_size": block_size,
        "regime_pools": {k: np.asarray(v, dtype=np.int64) for k, v in pools.items()},
        "regime_trans": trans,
        "initial_state": int(states[-1]) if len(states) else 1,
        "garch_alpha": garch_alpha,
        "garch_beta": garch_beta,
        "garch_omega": garch_omega,
        "uncond_var": uncond_var,
        "corr_root": corr_root,
        "student_df": 5.0,
        "gap_std": gap_std,
        "vol_log_mean": vol_log_mean,
        "vol_log_std": vol_log_std,
    }


def generate_joint_path(
    model: str,
    params: dict[str, Any],
    *,
    n_steps: int,
    seed: int,
    path_id: int,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Generate one causal joint path: (simple_returns[T, d], open_gaps[T, d-1], vol_ratios[T, d-1])."""
    rng = np.random.Generator(np.random.PCG64(np.random.SeedSequence([seed, path_id])))
    d = params["n_dim"]
    n_eq = params["n_eq"]
    block_size = params["block_size"]

    if model == "corr_gbm":
        z = rng.standard_normal((n_steps, d))
        log_r = params["mu_log"] + z @ params["chol"].T
        ret = np.expm1(np.clip(log_r, -0.5, 0.5))
        gaps = rng.normal(0.0, params["gap_std"], size=(n_steps, n_eq))
        vrat = np.exp(rng.normal(params["vol_log_mean"], params["vol_log_std"], size=(n_steps, n_eq)))
    elif model == "sync_mbb":
        hist = params["ret_hist"]
        n_blocks = (n_steps + block_size - 1) // block_size
        starts = rng.integers(0, len(hist) - block_size + 1, size=n_blocks)
        idx = (starts[:, None] + np.arange(block_size)[None, :]).ravel()[:n_steps]
        ret = hist[idx]
        gaps = params["open_gap_hist"][idx]
        vrat = params["vol_ratio_hist"][idx]
    elif model == "regime_cond_bootstrap":
        hist = params["ret_hist"]
        n_blocks = (n_steps + block_size - 1) // block_size
        state = params["initial_state"]
        chosen = []
        for _ in range(n_blocks):
            pool = params["regime_pools"][state]
            st_i = int(pool[rng.integers(0, len(pool))])
            chosen.append(np.arange(st_i, st_i + block_size))
            state = int(rng.choice(4, p=params["regime_trans"][state]))
        idx = np.concatenate(chosen)[:n_steps]
        ret = hist[idx]
        gaps = params["open_gap_hist"][idx]
        vrat = params["vol_ratio_hist"][idx]
    elif model == "down_jump_stress":
        z = rng.standard_normal((n_steps, d))
        log_r = params["mu_log"] + z @ params["chol"].T
        # Poisson-like market-wide downward jumps (~2.5 jumps per year, -6% to -12% common shock scaled by asset vol)
        jump_mask = rng.random(n_steps) < (2.5 / 252.0)
        if jump_mask.any():
            n_j = int(jump_mask.sum())
            # Common market crash factor (Beta-like amplification via asset volatility ratio to SP500TR)
            rel_vol = np.sqrt(params["uncond_var"]) / math.sqrt(params["uncond_var"][-1])
            rel_vol = np.clip(rel_vol, 0.8, 3.0)
            jump_mag = rng.uniform(-0.10, -0.05, size=(n_j, 1)) * rel_vol[None, :]
            log_r[jump_mask] += jump_mag
        ret = np.expm1(np.clip(log_r, -0.65, 0.5))
        gaps = rng.normal(0.0, params["gap_std"] * 1.25, size=(n_steps, n_eq))
        # Down-jump days also exhibit overnight gap-down and elevated volume
        if jump_mask.any():
            gaps[jump_mask] -= 0.025
        vrat = np.exp(rng.normal(params["vol_log_mean"], params["vol_log_std"], size=(n_steps, n_eq)))
        if jump_mask.any():
            vrat[jump_mask] *= 2.0
    elif model == "corr_shock_stress":
        # Two-regime correlation breakdown: 75% normal correlation, 25% persistent crisis burst (corr -> 0.88, vol 1.45x, neg drift)
        z = rng.standard_normal((n_steps, d))
        log_r = np.empty((n_steps, d), dtype=np.float64)
        crisis = False
        crisis_flags = np.zeros(n_steps, dtype=bool)
        for t in range(n_steps):
            if not crisis and rng.random() < 0.012:
                crisis = True
            elif crisis and rng.random() < 0.045:
                crisis = False
            crisis_flags[t] = crisis
            if crisis:
                log_r[t] = (params["mu_log"] - 0.0018) + z[t] @ params["chol_shock"].T
            else:
                log_r[t] = params["mu_log"] + z[t] @ params["chol"].T
        ret = np.expm1(np.clip(log_r, -0.65, 0.5))
        gaps = rng.normal(0.0, params["gap_std"], size=(n_steps, n_eq))
        gaps[crisis_flags] *= 1.6
        vrat = np.exp(rng.normal(params["vol_log_mean"], params["vol_log_std"], size=(n_steps, n_eq)))
        vrat[crisis_flags] *= 1.6
    elif model == "student_t_garch":
        # Multivariate Student-t (df=5) innovations + per-asset GARCH(1,1) conditional variance
        nu = params["student_df"]
        z = rng.standard_normal((n_steps, d)) @ params["corr_root"].T
        chi2 = rng.chisquare(nu, size=(n_steps, 1))
        # Unit-variance standardized multivariate Student-t draws
        eps = z * np.sqrt((nu - 2.0) / chi2)
        h = params["uncond_var"].copy()
        log_r = np.empty((n_steps, d), dtype=np.float64)
        alpha = params["garch_alpha"]
        beta = params["garch_beta"]
        omega = params["garch_omega"]
        mu = params["mu_log"]
        for t in range(n_steps):
            sig = np.sqrt(h)
            shock = sig * eps[t]
            log_r[t] = mu + shock
            h = np.clip(omega + alpha * (shock**2) + beta * h, 1e-10, 0.04)
        ret = np.expm1(np.clip(log_r, -0.65, 0.5))
        gaps = rng.normal(0.0, params["gap_std"], size=(n_steps, n_eq))
        vrat = np.exp(rng.normal(params["vol_log_mean"], params["vol_log_std"], size=(n_steps, n_eq)))
    else:
        raise ValueError(f"Unknown joint MC model: {model}")

    return ret, np.clip(gaps, -0.20, 0.20), np.clip(vrat, 0.1, 15.0)


def _fast_ridge_residual_forecast(
    close: np.ndarray,
    residual: np.ndarray,
    score: np.ndarray,
    risk: np.ndarray,
    breadth: np.ndarray,
    sp: np.ndarray,
    eligible_idx: list[int],
    rebalance_days: list[int],
) -> np.ndarray:
    """Fast exact closed-form Ridge(alpha=80) matching research_v86_v100.policies.predict_residual.

    Evaluated strictly causally at monthly boundaries on features available up to t-5 (matured 5-day label).
    """
    n_days, n_assets = close.shape
    preds = np.full((n_days, n_assets), np.nan, dtype=np.float64)
    if not rebalance_days:
        return preds

    # Compute 5-day forward labels & features (strictly causal slicing below ensures zero lookahead)
    fwd = np.full((n_days, n_assets), np.nan, dtype=np.float64)
    fwd[:-5] = close[5:] / close[:-5] - 1.0
    sp_fwd = np.full(n_days, np.nan, dtype=np.float64)
    sp_fwd[:-5] = sp[5:] / sp[:-5] - 1.0

    rr = np.full((n_days, n_assets), np.nan, dtype=np.float64)
    rr[1:] = close[1:] / close[:-1] - 1.0
    mm = np.full(n_days, np.nan, dtype=np.float64)
    mm[1:] = sp[1:] / sp[:-1] - 1.0

    # Rolling 63-day beta to SP500TR
    beta = np.full((n_days, n_assets), np.nan, dtype=np.float64)
    for t in range(63, n_days):
        m_win = mm[t - 62 : t + 1]
        m_var = np.var(m_win, ddof=1)
        if m_var > 1e-16:
            mc = m_win - m_win.mean()
            r_win = rr[t - 62 : t + 1]
            rc = r_win - r_win.mean(axis=0, keepdims=True)
            beta[t] = (rc * mc[:, None]).sum(axis=0) / (62.0 * m_var)

    label = (fwd - beta * sp_fwd[:, None]) / (risk * math.sqrt(5.0))

    # Trend (close/ma28 - 1), relative5 (rolling 5 sum of residual), market21
    ma28 = np.full((n_days, n_assets), np.nan, dtype=np.float64)
    for t in range(27, n_days):
        ma28[t] = close[t - 27 : t + 1].mean(axis=0)
    trend = close / ma28 - 1.0

    rel5 = np.full((n_days, n_assets), np.nan, dtype=np.float64)
    for t in range(4, n_days):
        rel5[t] = residual[t - 4 : t + 1].sum(axis=0)

    mkt21 = np.full(n_days, np.nan, dtype=np.float64)
    mkt21[21:] = sp[21:] / sp[:-21] - 1.0

    # Monthly re-fit schedule (every 21 trading days approximates calendar months cleanly in synthetic paths)
    el = np.asarray(eligible_idx, dtype=np.int64)
    k_el = len(el)
    # Pre-stack features [n_days, k_el, 6]
    feats = np.empty((n_days, k_el, 6), dtype=np.float64)
    feats[:, :, 0] = score[:, el]
    feats[:, :, 1] = trend[:, el]
    feats[:, :, 2] = rel5[:, el]
    feats[:, :, 3] = risk[:, el]
    feats[:, :, 4] = breadth[:, None]
    feats[:, :, 5] = mkt21[:, None]
    labels_el = label[:, el]

    # Fit once per 21-day block covering rebalance_days
    min_day = min(rebalance_days)
    max_day = max(rebalance_days)
    eye6 = np.eye(6, dtype=np.float64) * 80.0
    for block_start in range(min_day, max_day + 1, 21):
        if block_start < 5:
            continue
        t_lo = max(0, block_start - 508)
        t_hi = block_start - 5
        x_block = feats[t_lo : t_hi + 1]
        y_block = labels_el[t_lo : t_hi + 1]
        valid_mask = np.isfinite(x_block).all(axis=2) & np.isfinite(y_block)
        # Need at least 126 distinct valid dates
        if int(valid_mask.any(axis=1).sum()) < 126:
            continue
        X = x_block[valid_mask]
        y = np.clip(y_block[valid_mask], -3.0, 3.0)
        x_mean = X.mean(axis=0)
        x_std = X.std(axis=0, ddof=0)
        x_std = np.where(x_std == 0.0, 1.0, x_std)
        Xs = (X - x_mean) / x_std
        y_mean = y.mean()
        w_coef = np.linalg.solve(Xs.T @ Xs + eye6, Xs.T @ (y - y_mean))
        block_end = min(n_days, block_start + 21)
        x_pred = feats[block_start:block_end]
        pmask = np.isfinite(x_pred).all(axis=2)
        if pmask.any():
            Xp = (x_pred[pmask] - x_mean) / x_std
            out_vals = y_mean + Xp @ w_coef
            sub_pred = np.full((block_end - block_start, k_el), np.nan, dtype=np.float64)
            sub_pred[pmask] = out_vals
            preds[block_start:block_end, el] = sub_pred

    return preds


def _tilt_fixed_budget_np(w: np.ndarray, score: np.ndarray, fraction: float) -> np.ndarray:
    active = (w > 0.0) & np.isfinite(score)
    if int(active.sum()) < 2:
        return w
    wa = w[active]
    sa = np.clip(score[active], -2.0, 2.0)
    w_mean = float(np.sum(wa * sa) / np.sum(wa))
    centered = sa - w_mean
    denom = max(1.0, float(np.max(np.abs(centered))))
    out = w.copy()
    out[active] = wa * (1.0 + fraction * centered / denom)
    return out


def _simulate_paired_path_strategies(
    close: np.ndarray,
    opening: np.ndarray,
    volume: np.ndarray,
    sp: np.ndarray,
    *,
    warmup_days: int,
    omit_sndk: bool,
    fee_rate: float = 0.001,
    slippage_bps: float = 0.0,
    volume_cap_ratio: float = 0.001,
    initial_capital: float = 300_000.0,
) -> dict[str, dict[str, float]]:
    """Run full dynamic V69, V179, and V182 on ONE shared simulated path (paired comparison)."""
    n_days, n_assets = close.shape
    # Ticker indices: 0:AAPL, 1:SNDK, 2:BA, 3:C, 4:JPM, 5:GE, 6:TQQQ, 7:QQQ
    idx_sndk, idx_tqqq, idx_qqq = 1, 6, 7
    base_target = np.array([0.04, 0.04, 0.04, 0.04, 0.04, 0.04, 0.04 / 3.0, 0.04], dtype=np.float64)
    eligible = [i for i in range(n_assets) if (i != idx_sndk or not omit_sndk)]
    stock_idx = [0, 1, 2, 3, 4, 5]

    # Precompute all strictly causal daily indicators ONCE for the shared path
    df_c = pd.DataFrame(close)
    df_sp = pd.Series(sp)
    ma28 = df_c.rolling(28, min_periods=28).mean().to_numpy(dtype=np.float64)
    ma63 = df_c.rolling(63, min_periods=63).mean().to_numpy(dtype=np.float64)
    qqq_ma63 = ma63[:, idx_qqq]
    daily_ret = df_c.pct_change(fill_method=None)
    vol21 = daily_ret.rolling(21).std().to_numpy(dtype=np.float64)
    ret_np = daily_ret.to_numpy(dtype=np.float64)

    # Breadth over eligible symbols
    c_el = close[:, eligible]
    m_el = ma28[:, eligible]
    ready = np.isfinite(m_el) & np.isfinite(c_el) & (c_el > 0.0)
    ready_cnt = ready.sum(axis=1).astype(np.float64)
    breadth = np.where(ready_cnt > 0, ((c_el > m_el) & ready).sum(axis=1) / np.maximum(ready_cnt, 1.0), np.nan)
    breadth_diff10 = pd.Series(breadth).diff(10).to_numpy(dtype=np.float64)

    # V69 42-day idiosyncratic residual volatility & 2-day confirmation
    d_el = daily_ret.iloc[:, eligible]
    qqq_r = daily_ret.iloc[:, idx_qqq]
    mvar42 = qqq_r.rolling(42).var().replace(0.0, np.nan)
    cov42 = d_el.rolling(42).cov(qqq_r)
    res42 = (d_el.rolling(42).var() - cov42.pow(2).div(mvar42, axis=0)).clip(lower=0.0).pow(0.5)
    q75 = res42.quantile(0.75, axis=1)
    high_el = res42.gt(q75, axis=0).to_numpy(dtype=bool)
    high = np.zeros((n_days, n_assets), dtype=bool)
    high[:, eligible] = high_el
    cross = close > (ma28 * (1.0 + 0.5 * vol21))
    confirmed = cross & np.vstack([np.zeros((1, n_assets), dtype=bool), cross[:-1]])

    # V100..V182 market & stock indicators (using SP500TR)
    sp_fast63 = df_sp.rolling(63).mean().to_numpy(dtype=np.float64)
    sp_slow126 = df_sp.rolling(126).mean().to_numpy(dtype=np.float64)
    sp_long200 = df_sp.rolling(200).mean().to_numpy(dtype=np.float64)
    sp_dd = (df_sp / df_sp.rolling(252, min_periods=126).max() - 1.0).to_numpy(dtype=np.float64)
    sp_trend = df_sp.gt(df_sp.rolling(126).mean()).rolling(10).mean().to_numpy(dtype=np.float64)

    mm = df_sp.pct_change(fill_method=None)
    mvar63 = mm.rolling(63).var().replace(0.0, np.nan)
    beta63 = daily_ret.rolling(63).cov(mm).div(mvar63, axis=0).clip(-1.0, 4.0)
    residual_df = daily_ret - beta63.mul(mm, axis=0)
    residual_np = residual_df.to_numpy(dtype=np.float64)
    risk42 = daily_ret.rolling(42).std().replace(0.0, np.nan).to_numpy(dtype=np.float64)
    rel10 = residual_df.rolling(10).sum().to_numpy(dtype=np.float64)
    score42 = (residual_df.rolling(42).sum().to_numpy(dtype=np.float64)) / (risk42 * math.sqrt(42.0))
    dd42 = (df_c / df_c.rolling(42).max() - 1.0).to_numpy(dtype=np.float64)
    down_sq = daily_ret.clip(upper=0.0).pow(2)
    down_jump = (down_sq.rolling(10).mean() / down_sq.rolling(63).mean().replace(0.0, np.nan)).pow(0.5).to_numpy(dtype=np.float64)
    semidev63 = down_sq.rolling(63).mean().pow(0.5).to_numpy(dtype=np.float64)

    # Weekly rebalance schedule: every 5 trading days starting at warmup_days
    rebal_days = list(range(warmup_days, n_days, 5))
    rebal_set = set(rebal_days)
    signal_days = [d - 1 for d in rebal_days]
    forecast = _fast_ridge_residual_forecast(close, residual_np, score42, risk42, breadth, sp, eligible, signal_days)

    def _compute_targets(version: str, prev: int, prices: np.ndarray, valid: np.ndarray, held: np.ndarray) -> np.ndarray:
        # 1. V69 base
        fast = ma28[prev]
        qqq_on = bool(valid[idx_qqq] and np.isfinite(qqq_ma63[prev]) and prices[idx_qqq] > qqq_ma63[prev])
        bw = 0.5 * vol21[prev]
        enter = prices > (fast * (1.0 + bw))
        stay = (held > 0.0) & (prices >= (fast * (1.0 - bw)))
        w = np.where(enter | stay, base_target, 0.0)
        b_val = breadth[prev]
        if qqq_on and np.isfinite(b_val) and b_val >= 2.0 / 3.0:
            w = w * 1.10
        if not qqq_on:
            w[idx_qqq] = 0.0
            w[idx_tqqq] = 0.0
        if omit_sndk:
            w[idx_sndk] = 0.0
        w = np.where(valid, w, 0.0)

        # V51 WeakExit
        weak_v51 = (not np.isfinite(b_val) or b_val < 0.5) or (not np.isfinite(qqq_ma63[prev]) or prices[idx_qqq] <= qqq_ma63[prev])
        if weak_v51:
            w = np.where((held > 0.0) & (prices <= fast), 0.0, w)
        # V61 Scaled
        w = 1.10 * w
        # V69 high-vol entry confirm
        w = np.where((held <= 0.0) & high[prev] & (~confirmed[prev]), 0.0, w)

        if version == "v69":
            return w

        # 2. V100 rules = ('giveback', 'ridge_confirm', 'dual_strength') with 0.375 cap after each rule
        sp_val = sp[prev]
        rising = bool(np.isfinite(sp_slow126[prev]) and sp_val > sp_slow126[prev])
        weak_mkt = bool(np.isfinite(b_val) and b_val < 0.5 and np.isfinite(sp_fast63[prev]) and sp_val <= sp_fast63[prev])
        sc = score42[prev]

        # rule 1: giveback
        wk_st = (held > 0.0) & (dd42[prev] < -0.08) & (rel10[prev] < 0.0)
        w = np.where(wk_st, 0.88 * w, w)
        if w.sum() > 0.375:
            w = w * (0.375 / w.sum())
        # rule 2: ridge_confirm
        fc = forecast[prev]
        low_fc = np.isfinite(fc) & (fc < -0.10) & high[prev] & (held <= 0.0)
        w = np.where(low_fc, 0.5 * w, w)
        if w.sum() > 0.375:
            w = w * (0.375 / w.sum())
        # rule 3: dual_strength
        if rising and np.isfinite(b_val) and b_val >= 2.0 / 3.0:
            cand = [i for i in range(n_assets) if i != idx_tqqq and w[i] > 0.0 and np.isfinite(sc[i]) and sc[i] > 0.15]
            cand.sort(key=lambda i: -sc[i])
            for i in cand[:2]:
                w[i] += 0.006
        elif weak_mkt:
            dj = down_jump[prev]
            w = np.where(np.isfinite(dj) & (dj > 1.20), 0.9 * w, w)
        if w.sum() > 0.375:
            w = w * (0.375 / w.sum())

        # 3. V105: scale=4, gross_limit=0.90
        w = w * 4.0
        if w.sum() > 0.90:
            w = w * (0.90 / w.sum())

        # 4. V125: defence='recovery', allocation='relative'
        dd_v, tr_v = sp_dd[prev], sp_trend[prev]
        if np.isfinite(dd_v) and np.isfinite(b_val) and np.isfinite(tr_v):
            if dd_v < -0.10 and b_val < 0.5 and tr_v < 0.5:
                w = w * 0.65
        st_w = w[stock_idx]
        st_sc = sc[stock_idx]
        w[stock_idx] = _tilt_fixed_budget_np(st_w, st_sc, 0.30)
        growth = w.copy()

        # 5. V150: entry_rule='half'
        d_w = np.where((held <= 0.0) & (growth > 0.0), 0.5 * growth, growth)

        # 6. V161: risk_rule='tail_budget'
        before = d_w.copy()
        active_idx = np.where(d_w > 0.0)[0]
        if len(active_idx) >= 2:
            sd_a = semidev63[prev, active_idx]
            r_a = np.where(np.isfinite(sd_a), sd_a * d_w[active_idx], 0.0)
            r_max = float(r_a.sum()) * 0.32
            for j, a_i in enumerate(active_idx):
                if r_a[j] > r_max and r_a[j] > 0.0:
                    d_w[a_i] *= r_max / r_a[j]
            freed = float(before.sum() - d_w.sum())
            if freed >= 1e-12:
                recip = (d_w > 0.0) & (d_w >= before - 1e-12) & (d_w < 0.22)
                recip[idx_tqqq] = False
                if recip.any():
                    hr = np.minimum(0.22 - d_w[recip], 0.04)
                    hr_sum = float(hr.sum())
                    if hr_sum > 0.0:
                        d_w[recip] += hr * min(1.0, freed / hr_sum)
            else:
                d_w = before

        # 7. V165: reserve_rule='fixed'
        tot = float(d_w.sum())
        if 0.0 < tot < 0.90 - 1e-10:
            add = min(0.90 - tot, 0.10 * tot)
            elig_r = d_w > 0.0
            r_sum = float(d_w[elig_r].sum())
            if r_sum > 0.0 and add > 0.0:
                extra = np.where(elig_r, d_w * (add / r_sum), 0.0)
                extra = np.minimum(extra, 0.04)
                extra[idx_tqqq] = min(extra[idx_tqqq], 0.012)
                d_w = d_w + extra
        defensive = d_w

        # 8. V179 vs V182 Blend
        if version == "v179":
            cond = bool(np.isfinite(sp_long200[prev]) and sp_val > sp_long200[prev] and np.isfinite(b_val) and b_val >= 0.5)
            share = 0.75 if cond else 0.25
        else:
            bd = breadth_diff10[prev]
            cond = bool(np.isfinite(bd) and bd > 0.10 and np.isfinite(sp_fast63[prev]) and sp_val > sp_fast63[prev])
            share = 0.75 if cond else 0.40
        out_w = growth * share + defensive * (1.0 - share)
        if out_w.sum() > 0.90:
            out_w = out_w * (0.90 / out_w.sum())
        return out_w

    slip = slippage_bps / 10_000.0
    out_metrics: dict[str, dict[str, float]] = {}
    for version in ("v69", "v179", "v182"):
        cash = initial_capital
        held = np.zeros(n_assets, dtype=np.float64)
        peak = initial_capital
        max_dd = 0.0
        total_fees = 0.0
        total_fills = 0
        exp_sum = 0.0
        eval_days = n_days - warmup_days
        for t in range(warmup_days, n_days):
            if t in rebal_set:
                prev = t - 1
                px_prev = close[prev]
                vol_prev = volume[prev]
                valid = np.isfinite(px_prev) & (px_prev > 0.0)
                tradable = valid & np.isfinite(vol_prev) & (vol_prev >= 0.0)
                w = _compute_targets(version, prev, px_prev, valid, held)
                h_val = np.where(tradable, held * px_prev, 0.0)
                nav_prev = float(cash + h_val.sum())
                sells, buys = [], []
                for s in range(n_assets):
                    if not tradable[s]:
                        continue
                    chg = float(w[s] - h_val[s] / nav_prev)
                    if w[s] == 0.0:
                        amt = -held[s]
                    elif abs(chg) >= 0.005:
                        amt = chg * nav_prev / px_prev[s]
                    else:
                        continue
                    sz = min(abs(amt), volume_cap_ratio * vol_prev[s])
                    if sz <= 1e-8:
                        continue
                    if amt < 0.0:
                        sells.append((s, sz))
                    else:
                        buys.append((s, sz))
                for s, sz in sells:
                    px_ex = opening[t, s] * (1.0 - slip)
                    if not np.isfinite(px_ex) or px_ex <= 0.0:
                        continue
                    qty = min(sz, held[s])
                    if qty <= 0.0:
                        continue
                    tv = qty * px_ex
                    fee = tv * fee_rate
                    held[s] -= qty
                    if abs(held[s]) < 1e-12:
                        held[s] = 0.0
                    cash += tv - fee
                    total_fees += fee
                    total_fills += 1
                for s, sz in buys:
                    px_ex = opening[t, s] * (1.0 + slip)
                    if not np.isfinite(px_ex) or px_ex <= 0.0:
                        continue
                    max_q = max(0.0, cash / (px_ex * (1.0 + fee_rate)))
                    qty = min(sz, max_q)
                    if qty <= 0.0:
                        continue
                    tv = qty * px_ex
                    fee = tv * fee_rate
                    held[s] += qty
                    cash -= tv + fee
                    total_fees += fee
                    total_fills += 1

            c_t = close[t]
            val_t = float(np.where(np.isfinite(c_t), held * c_t, 0.0).sum())
            nav_t = cash + val_t
            if nav_t > peak:
                peak = nav_t
            dd_t = 1.0 - nav_t / peak
            if dd_t > max_dd:
                max_dd = dd_t
            exp_sum += val_t / nav_t if nav_t > 0.0 else 0.0

        out_metrics[version] = {
            "terminal_nav": float(nav_t),
            "loss": float(1.0 - nav_t / initial_capital),
            "net_return": float(nav_t / initial_capital - 1.0),
            "max_dd": float(max_dd),
            "fees": float(total_fees),
            "fills": int(total_fills),
            "mean_exposure": float(exp_sum / max(1, eval_days)),
        }
    return out_metrics


def _worker_simulate_chunk(task: dict[str, Any]) -> list[dict[str, dict[str, float]]]:
    """Worker evaluating a chunk of simulated joint paths across V69, V179, and V182."""
    model = task["model"]
    params = task["params"]
    seed = int(task["seed"])
    path_ids = task["path_ids"]
    horizon = int(task["horizon"])
    warmup_close = task["warmup_close"]
    warmup_open = task["warmup_open"]
    warmup_vol = task["warmup_vol"]
    warmup_sp = task["warmup_sp"]
    vol_medians = task["vol_medians"]
    include_sndk = bool(task["include_sndk"])
    fee_rate = float(task["fee_rate"])
    slippage_bps = float(task["slippage_bps"])
    volume_cap_ratio = float(task["volume_cap_ratio"])
    w_len = warmup_close.shape[0]
    eq_indices = [i for i in range(8) if (i != 1 or include_sndk)]

    chunk_out = []
    for pid in path_ids:
        ret, gaps, vrat = generate_joint_path(model, params, n_steps=horizon, seed=seed, path_id=int(pid))
        # Stitch simulated horizon onto historical warmup
        close = np.full((w_len + horizon, 8), np.nan, dtype=np.float64)
        opening = np.full((w_len + horizon, 8), np.nan, dtype=np.float64)
        volume = np.full((w_len + horizon, 8), np.nan, dtype=np.float64)
        sp = np.empty(w_len + horizon, dtype=np.float64)

        close[:w_len] = warmup_close
        opening[:w_len] = warmup_open
        volume[:w_len] = warmup_vol
        sp[:w_len] = warmup_sp

        # Simulate prices cumulatively from last warmup close
        sp[w_len:] = warmup_sp[-1] * np.cumprod(1.0 + ret[:, -1])
        for j, a_idx in enumerate(eq_indices):
            c_prev = warmup_close[-1, a_idx]
            c_sim = c_prev * np.cumprod(1.0 + ret[:, j])
            close[w_len:, a_idx] = c_sim
            prev_vec = np.r_[c_prev, c_sim[:-1]]
            opening[w_len:, a_idx] = prev_vec * (1.0 + gaps[:, j])
            volume[w_len:, a_idx] = vol_medians[a_idx] * vrat[:, j]

        res = _simulate_paired_path_strategies(
            close,
            opening,
            volume,
            sp,
            warmup_days=w_len,
            omit_sndk=not include_sndk,
            fee_rate=fee_rate,
            slippage_bps=slippage_bps,
            volume_cap_ratio=volume_cap_ratio,
        )
        chunk_out.append(res)
    return chunk_out


def run_tier2_dynamic_strategy_mc(
    config: ExternalValidationConfig,
    calib_by_window: dict[str, dict[str, Any]],
    *,
    primary_window: str = "history_20y",
) -> dict[str, Any]:
    """Tier 2: Full dynamic strategy lifecycle MC with progressive scaling, multi-seed convergence & paired tests."""
    mc_cfg = config.monte_carlo
    started = perf_counter()
    cpu_started = process_time()

    primary_calib = calib_by_window[primary_window]
    primary_params = _fit_joint_model_params(primary_calib, block_size=mc_cfg.block_size)
    vol_medians = np.array([float(primary_calib["vol_median"].get(t, 1e7)) for t in DEFAULT_TICKERS], dtype=np.float64)

    max_workers = max(1, min(mc_cfg.max_workers, os.cpu_count() or 4))

    def _run_batch(
        model: str,
        params: dict[str, Any],
        calib: dict[str, Any],
        n_paths: int,
        seed: int,
        *,
        fee_rate: float = 0.001,
        slippage_bps: float = 0.0,
        volume_cap_ratio: float = 0.001,
        pool: ProcessPoolExecutor,
    ) -> dict[str, list[dict[str, float]]]:
        v_meds = np.array([float(calib["vol_median"].get(t, 1e7)) for t in DEFAULT_TICKERS], dtype=np.float64)
        chunk_size = max(8, math.ceil(n_paths / (max_workers * 2)))
        tasks = []
        for begin in range(0, n_paths, chunk_size):
            end = min(n_paths, begin + chunk_size)
            tasks.append(
                {
                    "model": model,
                    "params": params,
                    "seed": seed,
                    "path_ids": list(range(begin, end)),
                    "horizon": mc_cfg.horizon_days,
                    "warmup_close": calib["warmup_close"],
                    "warmup_open": calib["warmup_open"],
                    "warmup_vol": calib["warmup_vol"],
                    "warmup_sp": calib["warmup_sp"],
                    "vol_medians": v_meds,
                    "include_sndk": calib["include_sndk"],
                    "fee_rate": fee_rate,
                    "slippage_bps": slippage_bps,
                    "volume_cap_ratio": volume_cap_ratio,
                }
            )
        by_strat: dict[str, list[dict[str, float]]] = {"v69": [], "v179": [], "v182": []}
        for chunk_res in pool.map(_worker_simulate_chunk, tasks):
            for row in chunk_res:
                for s in ("v69", "v179", "v182"):
                    by_strat[s].append(row[s])
        return by_strat

    def _summarize_strat_runs(
        by_strat: dict[str, list[dict[str, float]]],
        *,
        seed: int,
    ) -> dict[str, Any]:
        summary: dict[str, Any] = {}
        for s in ("v69", "v179", "v182"):
            losses = np.array([r["loss"] for r in by_strat[s]], dtype=np.float64)
            dds = np.array([r["max_dd"] for r in by_strat[s]], dtype=np.float64)
            rets = np.array([r["net_return"] for r in by_strat[s]], dtype=np.float64)
            st = summarize_paths(
                losses,
                dds,
                threshold=0.20,
                confidences=(0.95, 0.99),
                resamples=mc_cfg.bootstrap_resamples,
                bootstrap_seed=seed,
            )
            breaches = {
                f"dd_gt_{int(th*100)}pct": wilson_interval(int(np.sum(dds > th)), len(dds))
                for th in mc_cfg.drawdown_thresholds
            }
            summary[s] = {
                "mean_return": float(rets.mean()),
                "median_return": float(np.median(rets)),
                "mean_exposure": float(np.mean([r["mean_exposure"] for r in by_strat[s]])),
                "mean_fees": float(np.mean([r["fees"] for r in by_strat[s]])),
                "statistics": st,
                "drawdown_breaches": breaches,
            }
        # Paired pathwise contrasts on identical paths
        paired = {}
        for a, b in (("v179", "v69"), ("v182", "v69"), ("v182", "v179")):
            ra = np.array([r["net_return"] for r in by_strat[a]], dtype=np.float64)
            rb = np.array([r["net_return"] for r in by_strat[b]], dtype=np.float64)
            dda = np.array([r["max_dd"] for r in by_strat[a]], dtype=np.float64)
            ddb = np.array([r["max_dd"] for r in by_strat[b]], dtype=np.float64)
            diff_r = ra - rb
            diff_dd = dda - ddb
            paired[f"{a}_minus_{b}"] = {
                "mean_return_diff": float(diff_r.mean()),
                "se_return_diff": float(diff_r.std(ddof=1) / math.sqrt(len(diff_r))),
                "win_rate": wilson_interval(int(np.sum(diff_r > 0.0)), len(diff_r)),
                "mean_mdd_diff": float(diff_dd.mean()),
                "lower_mdd_rate": wilson_interval(int(np.sum(diff_dd < 0.0)), len(diff_dd)),
            }
        return {"strategies": summary, "paired_contrasts": paired}

    convergence_rows: list[dict[str, Any]] = []
    model_comparison_rows: list[dict[str, Any]] = []
    calibration_sensitivity_rows: list[dict[str, Any]] = []
    execution_stress_mc_rows: list[dict[str, Any]] = []

    with ProcessPoolExecutor(max_workers=max_workers) as pool:
        # 1. Progressive statistical convergence curve on primary models (sync_mbb & corr_gbm)
        #    Scales: 256 -> 512 -> 1024 -> 2048 -> 4096 across all 5 seeds (nested prefix reuse per seed!)
        max_conv_n = max(mc_cfg.tier2_path_scales)
        for model in ("sync_mbb", "corr_gbm"):
            for seed in mc_cfg.seeds:
                t0 = perf_counter()
                full_runs = _run_batch(model, primary_params, primary_calib, max_conv_n, seed, pool=pool)
                batch_sec = perf_counter() - t0
                for n_scale in mc_cfg.tier2_path_scales:
                    sub = {s: full_runs[s][:n_scale] for s in ("v69", "v179", "v182")}
                    summ = _summarize_strat_runs(sub, seed=seed)
                    convergence_rows.append(
                        {
                            "model": model,
                            "window": primary_window,
                            "block_size": mc_cfg.block_size,
                            "n_paths": int(n_scale),
                            "seed": int(seed),
                            "full_batch_seconds": batch_sec if n_scale == max_conv_n else None,
                            **summ,
                        }
                    )

        # Check precision at max_conv_n; if sync_mbb CVaR99 halfwidth > target and budget allows, expand to 10,000 for 1 seed
        conv_max_widths = [
            0.5
            * (
                r["strategies"]["v179"]["statistics"]["tails"]["0.99"]["cvar_interval"]["upper"]
                - r["strategies"]["v179"]["statistics"]["tails"]["0.99"]["cvar_interval"]["lower"]
            )
            * 100.0
            for r in convergence_rows
            if r["model"] == "sync_mbb" and r["n_paths"] == max_conv_n
        ]
        expanded_10k = None
        if mc_cfg.tier2_max_scale > max_conv_n:
            t10 = perf_counter()
            runs_10k = _run_batch(
                "sync_mbb", primary_params, primary_calib, mc_cfg.tier2_max_scale, mc_cfg.seeds[0], pool=pool
            )
            sec_10k = perf_counter() - t10
            expanded_10k = {
                "model": "sync_mbb",
                "window": primary_window,
                "n_paths": mc_cfg.tier2_max_scale,
                "seed": mc_cfg.seeds[0],
                "elapsed_seconds": sec_10k,
                "trigger_cvar99_halfwidth_pp_at_prev_scale": float(np.mean(conv_max_widths)) if conv_max_widths else None,
                **_summarize_strat_runs(runs_10k, seed=mc_cfg.seeds[0]),
            }

        # 2. All 6 market models comparison at 2,048 paths × 3 seeds
        comp_n = min(2048, max_conv_n)
        for model in mc_cfg.models:
            for seed in mc_cfg.seeds[:3]:
                t0 = perf_counter()
                runs = _run_batch(model, primary_params, primary_calib, comp_n, seed, pool=pool)
                summ = _summarize_strat_runs(runs, seed=seed)
                model_comparison_rows.append(
                    {
                        "model": model,
                        "window": primary_window,
                        "n_paths": comp_n,
                        "seed": int(seed),
                        "elapsed_seconds": perf_counter() - t0,
                        **summ,
                    }
                )

        # 3. Calibration window sensitivity (target_4y vs history_10y vs history_20y) at 2,048 paths
        for wname, cal in calib_by_window.items():
            w_params = _fit_joint_model_params(cal, block_size=mc_cfg.block_size)
            for model in ("sync_mbb", "student_t_garch"):
                t0 = perf_counter()
                runs = _run_batch(model, w_params, cal, comp_n, mc_cfg.seeds[0], pool=pool)
                summ = _summarize_strat_runs(runs, seed=mc_cfg.seeds[0])
                calibration_sensitivity_rows.append(
                    {
                        "calibration_window": wname,
                        "n_obs": cal["n_obs"],
                        "include_sndk": cal["include_sndk"],
                        "model": model,
                        "n_paths": comp_n,
                        "seed": mc_cfg.seeds[0],
                        "elapsed_seconds": perf_counter() - t0,
                        **summ,
                    }
                )

        # 4. Execution cost & liquidity stress in MC (sync_mbb & down_jump_stress at 2,048 paths)
        for sc_name, fee, slip, vcap in (
            ("normal_10bps", 0.001, 0.0, 0.001),
            ("stress_30bps_slip10bps_tightvol", 0.003, 10.0, 0.0002),
        ):
            for model in ("sync_mbb", "down_jump_stress"):
                t0 = perf_counter()
                runs = _run_batch(
                    model,
                    primary_params,
                    primary_calib,
                    comp_n,
                    mc_cfg.seeds[0],
                    fee_rate=fee,
                    slippage_bps=slip,
                    volume_cap_ratio=vcap,
                    pool=pool,
                )
                summ = _summarize_strat_runs(runs, seed=mc_cfg.seeds[0])
                execution_stress_mc_rows.append(
                    {
                        "execution_scenario": sc_name,
                        "fee_rate": fee,
                        "slippage_bps": slip,
                        "volume_cap_ratio": vcap,
                        "model": model,
                        "n_paths": comp_n,
                        "seed": mc_cfg.seeds[0],
                        "elapsed_seconds": perf_counter() - t0,
                        **summ,
                    }
                )

    # Aggregate multi-seed convergence summaries using FactorLab's repeat_summary() per strategy
    repeat_summaries_by_strategy: dict[str, Any] = {}
    for s in ("v69", "v179", "v182"):
        adapted = [
            {
                "model": r["model"],
                "window": r["window"],
                "block_size": r["block_size"],
                "n_paths": r["n_paths"],
                "seed": r["seed"],
                "statistics": r["strategies"][s]["statistics"],
            }
            for r in convergence_rows
        ]
        repeat_summaries_by_strategy[s] = repeat_summary(adapted)

    return {
        "tier": "TIER2_FULL_DYNAMIC_STRATEGY_LIFECYCLE_MC",
        "primary_calibration_window": primary_window,
        "precision_targets": {
            "cvar95_halfwidth_pp": mc_cfg.precision_target_cvar95_halfwidth_pp,
            "cvar99_halfwidth_pp": mc_cfg.precision_target_cvar99_halfwidth_pp,
            "seed_sd_pp": mc_cfg.precision_target_seed_sd_pp,
        },
        "resources": {
            "wall_seconds": perf_counter() - started,
            "main_process_cpu_seconds": process_time() - cpu_started,
            "max_workers": max_workers,
            "cpu_count": os.cpu_count(),
            "peak_rss_bytes": _peak_rss_bytes(),
        },
        "convergence_rows": convergence_rows,
        "expanded_10k_check": expanded_10k,
        "repeat_summaries_by_strategy": repeat_summaries_by_strategy,
        "model_comparison_rows": model_comparison_rows,
        "calibration_sensitivity_rows": calibration_sensitivity_rows,
        "execution_stress_mc_rows": execution_stress_mc_rows,
    }
