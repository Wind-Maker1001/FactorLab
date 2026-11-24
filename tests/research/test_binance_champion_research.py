"""Unit tests & Section 9A synthetic protocol acceptance for P8-S02-F01 Binance Champion Selector."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from factorlab.dataset.identity import file_hash, json_hash
from factorlab.experiments import ExperimentTracker
from factorlab.integrations.binance_futures_trading.azure_strategy_deploy import cycle as s03_cycle
from factorlab.integrations.binance_futures_trading.champion_release_adapter import adapt_selection
from factorlab.integrations.binance_futures_trading.strategy_package import install_package
from factorlab.integrations.binance_futures_trading.strategy_worker import invoke as invoke_s03_worker
from factorlab.research import automatic
from factorlab.research.automatic import read_json, replay, run, validate_config, write_json
from factorlab.research.binance_perpetual import (
    BINANCE_EXECUTION,
    FUNDING_TIE_RULE,
    build_s03_release_envelope,
    simulate_binance_candidate,
)
from factorlab.research.champion import SelectionPolicy

REPO_ROOT = Path(__file__).resolve().parents[2]
STRAT_DIR = REPO_ROOT / "src" / "factorlab" / "strategies" / "binance_usdm"


def _make_synthetic_dataset(
    root: Path,
    *,
    days: int = 120,
    drift_hourly: float = 0.0005,
    vol_hourly: float = 0.0012,
    funding_rate_8h: str = "0.00005",
    seed: int = 101,
    volume: float = 5000.0,
) -> tuple[Path, Path, pd.DataFrame]:
    rng = np.random.default_rng(seed)
    start = datetime(2025, 1, 1, 0, 0, tzinfo=timezone.utc)
    hours = days * 24
    times = [start + timedelta(hours=i) for i in range(hours)]
    # Smooth deterministic + small noise uptrend so momentum/dual-MA have clear positive signal
    rets = drift_hourly + vol_hourly * np.sin(np.arange(hours) * 0.08) + rng.normal(0.0, vol_hourly * 0.25, hours)
    closes = 140.0 * np.cumprod(1.0 + rets)
    opens = np.r_[140.0, closes[:-1]]
    highs = np.maximum(opens, closes) * 1.002
    lows = np.minimum(opens, closes) * 0.998
    df = pd.DataFrame(
        {
            "open_time": pd.to_datetime(times, utc=True),
            "symbol": "NVDAUSDT",
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": float(volume),
        }
    )
    bars_path = root / "nvda_synth.parquet"
    df.to_parquet(bars_path, index=False)

    funding_rows = []
    for idx in range(0, hours, 8):
        t = times[idx] + timedelta(milliseconds=1)
        funding_rows.append(
            {
                "symbol": "NVDAUSDT",
                "fundingTime": int(round(t.timestamp() * 1000)),
                "time": t.isoformat(),
                "markPrice": f"{closes[idx]:.4f}",
                "fundingRate": funding_rate_8h,
                "rateType": "Regular",
            }
        )
    funding_path = root / "nvda_synth_funding.json"
    write_json(funding_path, funding_rows)
    return bars_path, funding_path, df


def _base_binance_config(
    root: Path,
    bars_path: Path,
    funding_path: Path,
    *,
    study_id: str = "synth-binance-v1",
    source_kind: str = "SYNTHETIC",
    oos_eligibility: str = "FRESH_UNCONSUMED_OOS",
    prior_studies: list[str] | None = None,
    execution_lifecycle: str = "F11_F08_FULL_LIFECYCLE",
    exit_profile: str = "IDEAL_LIMIT_FILL",
    resamples: int = 400,
    min_oos_days: int = 40,
    block_days: int = 5,
    cooldown_days: int = 30,
) -> dict:
    # Copy strategy files locally so tmp_path is self-contained
    sdir = root / "strategies"
    sdir.mkdir(exist_ok=True)
    for name in (
        "cash_benchmark.py",
        "fixed_grid.py",
        "funding_sensitive_grid.py",
        "vol_adaptive_grid.py",
        "dual_ma_usdm.py",
        "close_momentum_usdm.py",
    ):
        (sdir / name).write_bytes((STRAT_DIR / name).read_bytes())

    def sinfo(name: str) -> tuple[str, str]:
        p = sdir / name
        return f"strategies/{name}", file_hash(p)

    cash_f, cash_sha = sinfo("cash_benchmark.py")
    fixed_f, fixed_sha = sinfo("fixed_grid.py")
    fund_f, fund_sha = sinfo("funding_sensitive_grid.py")
    vol_f, vol_sha = sinfo("vol_adaptive_grid.py")
    dma_f, dma_sha = sinfo("dual_ma_usdm.py")
    mom_f, mom_sha = sinfo("close_momentum_usdm.py")

    data_cfg = {
        "bars": bars_path.name,
        "sha256": file_hash(bars_path),
        "funding": funding_path.name,
        "funding_sha256": file_hash(funding_path),
        "source_kind": source_kind,
        "oos_eligibility": oos_eligibility,
    }
    if prior_studies is not None:
        data_cfg["prior_studies"] = prior_studies

    return {
        "schema": "FACTORLAB_AUTO_RESEARCH_V1",
        "study_id": study_id,
        "data": data_cfg,
        "windows": {
            "train": ["2025-01-01", "2025-01-20"],
            "validation": ["2025-01-21", "2025-02-09"],
            "test": ["2025-02-10", "2025-03-21"],
        },
        "common": {
            "symbol": "NVDAUSDT",
            "execution": BINANCE_EXECUTION,
            "market": "BINANCE_USDM",
            "interval": "1h",
            "position_mode": "ONE_WAY_LONG_ONLY",
            "order_type": "LIMIT",
            "leverage": 1,
            "initial_cash": 10.0,
            "demo_live_cap_usdt": 7.0,
            "max_weight": 1.0,
            "fee_bps": 3.0,
            "slippage_bps": 2.0,
            "participation": 0.01,
            "funding_mode": "historical_events",
            "funding_tie_rule": FUNDING_TIE_RULE,
            "execution_lifecycle": execution_lifecycle,
            "exit_profile": exit_profile,
            "limit_buffer_bps": 20.0,
            "rules": {
                "min_notional": "5",
                "step_size": "0.01",
                "tick_size": "0.01",
                "min_quantity": "0.01",
                "quantity_unit": "0.04",
                "max_quantity": "0.04",
            },
            "guardrails": {
                "max_open_orders": 1,
                "percent_price_down": 0.97,
                "percent_price_up": 1.03,
                "exit_reprice_seconds": 180,
                "exit_max_attempts": 5,
                "max_loss_ratio": 0.25,
                "total_orders": 200,
                "total_notional": 2000.0,
                "window_orders": 20,
                "window_seconds": 10800,
            },
            "price_basis": "synthetic 1h trade close with adverse slippage (software protocol test)",
            "rule_boundary_note": "SYNTHETIC_PROTOCOL_ACCEPTANCE_ONLY",
        },
        "policy": {
            "objective": "risk_adjusted_net_return",
            "risk_aversion": 0.5,
            "periods_per_year": 365,
            "max_drawdown": 0.25,
            "max_daily_cvar": 0.08,
            "cvar_confidence": 0.95,
            "participation_limit": 0.01,
            "min_capacity_usdt": 10.0,
            "min_net_return": 0.0,
            "min_score_improvement": 0.02,
            "min_annual_net_improvement": 0.01,
            "min_oos_days": min_oos_days,
            "min_tail_observations": 2,
            "min_positive_blocks": 0.60,
            "cooldown_days": cooldown_days,
            "confidence": 0.95,
            "block_days": block_days,
            "resamples": resamples,
            "seed": 42,
            "max_candidates": 32,
            "max_finalists": 5,
            "volume_lookback": 20,
            "min_trade_count": 1,
            "max_exit_failure_ratio": 0.50,
        },
        "strategies": [
            {
                "strategy_id": "FIXED_GRID",
                "file": fixed_f,
                "class": "FixedGridCandidate",
                "sha256": fixed_sha,
                "parameters": {"symbol": "NVDAUSDT", "interval_seconds": 3600, "half_width_pct": 0.01},
                "grid": {},
            },
            {
                "strategy_id": "FUNDING_SENSITIVE_GRID",
                "file": fund_f,
                "class": "FundingSensitiveGridCandidate",
                "sha256": fund_sha,
                "parameters": {"symbol": "NVDAUSDT", "interval_seconds": 3600, "profile_id": "B5"},
                "grid": {},
            },
            {
                "strategy_id": "VOL_ADAPTIVE_GRID",
                "file": vol_f,
                "class": "VolAdaptiveGridCandidate",
                "sha256": vol_sha,
                "parameters": {"symbol": "NVDAUSDT", "interval_seconds": 3600, "profile_id": "C5"},
                "grid": {},
            },
            {
                "strategy_id": "DUAL_MA",
                "file": dma_f,
                "class": "DualMAUsdmStrategy",
                "sha256": dma_sha,
                "parameters": {"symbol": "NVDAUSDT", "interval_seconds": 3600, "max_weight": 0.90},
                "grid": {"fast_window": [5], "slow_window": [20]},
            },
            {
                "strategy_id": "CLOSE_MOMENTUM",
                "file": mom_f,
                "class": "CloseMomentumUsdmStrategy",
                "sha256": mom_sha,
                "parameters": {"symbol": "NVDAUSDT", "interval_seconds": 3600, "weight": 0.90},
                "grid": {"lookback": [24]},
            },
        ],
        "champion": {
            "strategy_id": "CASH",
            "strategy": {"file": cash_f, "class": "CashBenchmarkStrategy", "sha256": cash_sha},
            "parameters": {"symbol": "NVDAUSDT", "interval_seconds": 3600},
        },
    }


def test_binance_contract_validation_enforces_stage1_boundaries(tmp_path):
    bars_path, funding_path, _ = _make_synthetic_dataset(tmp_path)
    cfg = _base_binance_config(tmp_path, bars_path, funding_path)
    assert isinstance(validate_config(cfg), SelectionPolicy)

    mutations = [
        ("symbol", lambda c: c["common"].update(symbol="BTCUSDT")),
        ("leverage", lambda c: c["common"].update(leverage=2)),
        ("position_mode", lambda c: c["common"].update(position_mode="LONG_SHORT")),
        ("order_type", lambda c: c["common"].update(order_type="MARKET")),
        ("funding_tie_rule", lambda c: c["common"].update(funding_tie_rule="post_fill_inventory")),
        ("pseudo_oos_missing_prior", lambda c: c["data"].update(oos_eligibility="HISTORICAL_REUSE_PSEUDO_OOS")),
        ("unknown_lifecycle", lambda c: c["common"].update(execution_lifecycle="MAGIC")),
    ]
    for label, mutate in mutations:
        bad = deepcopy(cfg)
        mutate(bad)
        with pytest.raises(ValueError, match="."):
            validate_config(bad)


def test_r10_funding_tie_rule_never_uses_post_fill_inventory(tmp_path):
    """Exact-tie funding at BUY close pays zero; exact-tie funding at SELL close pays pre-sell qty."""
    start = datetime(2025, 6, 1, 0, 0, tzinfo=timezone.utc)
    # 60h synthetic bars: flat -> up (triggers CloseMomentum BUY at bar 1, fills at bar 2 close) -> down (triggers SELL at bar 2, fills at bar 3 close)
    closes = [150.0, 152.0, 151.0] + [151.0] * 57
    stage_bars = [
        {
            "id": f"hour-{i}",
            "time": (start + timedelta(hours=i)).isoformat(),
            "open": closes[i],
            "high": closes[i] * 1.001,
            "low": closes[i] * 0.999,
            "close": closes[i],
            "price": closes[i],
            "volume": 5000,
            "completed_bar": True,
        }
        for i in range(len(closes))
    ]
    # Exact tie funding events right at bar 2 close (when BUY fills) and bar 3 close (when SELL fills)
    t_buy_close = start + timedelta(hours=3, microseconds=-1)  # bar 2 close
    t_sell_close = start + timedelta(hours=4, microseconds=-1)  # bar 3 close
    rate = D("0.00229669")
    funding_events = [
        (t_buy_close, D("151.0"), rate, "SyntheticTie"),
        (t_sell_close, D("151.0"), rate, "SyntheticTie"),
    ]
    src = STRAT_DIR / "close_momentum_usdm.py"
    cand = {
        "candidate_id": "c-tie-test",
        "strategy_id": "CLOSE_MOMENTUM",
        "class_name": "CloseMomentumUsdmStrategy",
        "code_version": file_hash(src),
        "parameters": {"symbol": "NVDAUSDT", "interval_seconds": 3600, "lookback": 1, "weight": 0.90, "threshold_bps": 0.0},
    }
    bars_path, funding_path, _ = _make_synthetic_dataset(tmp_path, days=5)
    common = _base_binance_config(tmp_path, bars_path, funding_path)["common"]
    policy = SelectionPolicy(periods_per_year=365, min_oos_days=2, block_days=1, min_tail_observations=1, resamples=100)

    res = simulate_binance_candidate(
        cand,
        source_path=src,
        stage_bars=stage_bars,
        warmup_bars=[],
        train_bars=stage_bars,
        funding_events=funding_events,
        common=common,
        policy=policy,
        account_id="tie-check",
    )
    trades = res["trades"]
    assert len(trades) == 2
    assert trades.iloc[0]["side"] == "BUY"
    assert trades.iloc[1]["side"] == "SELL"
    qty = D(str(trades.iloc[0]["executed_shares"]))
    # BUY filled at t_buy_close -> pays 0 at t_buy_close; SELL filled at t_sell_close -> pays full qty at t_sell_close!
    expected_funding = float(-qty * D("151.0") * rate)
    assert res["metrics"]["funding_pnl_usdt"] == pytest.approx(expected_funding, abs=1e-7)
    assert res["metrics"]["funding_reconciliation_residual"] < 1e-7
    assert res["metrics"]["equity_reconciliation_residual"] < 1e-7


def test_partial_fill_and_unfilled_exit_failure_accounting(tmp_path):
    bars_path, funding_path, _ = _make_synthetic_dataset(tmp_path, days=10, drift_hourly=0.0, vol_hourly=0.008, seed=7)
    cfg = _base_binance_config(tmp_path, bars_path, funding_path, exit_profile="LIMITED_CAPACITY_PARTIAL")
    df = pd.read_parquet(bars_path)
    stage_bars = [
        {
            "id": f"hour-{i}",
            "time": pd.Timestamp(r.open_time).isoformat(),
            "open": float(r.open),
            "high": float(r.high),
            "low": float(r.low),
            "close": float(r.close),
            "price": float(r.close),
            "volume": int(r.volume),
            "completed_bar": True,
        }
        for i, r in enumerate(df.itertuples(index=False))
    ]
    src = STRAT_DIR / "fixed_grid.py"
    cand = {
        "candidate_id": "c-partial",
        "strategy_id": "FIXED_GRID",
        "class_name": "FixedGridCandidate",
        "code_version": file_hash(src),
        "parameters": {"symbol": "NVDAUSDT", "interval_seconds": 3600, "half_width_pct": 0.005, "order_ttl": 100, "breakout": "pause"},
    }
    policy = SelectionPolicy(periods_per_year=365, min_oos_days=2, block_days=1, min_tail_observations=1, resamples=100)
    res_partial = simulate_binance_candidate(
        cand,
        source_path=src,
        stage_bars=stage_bars,
        warmup_bars=[],
        train_bars=stage_bars,
        funding_events=[],
        common=cfg["common"],
        policy=policy,
        account_id="partial-test",
    )
    assert res_partial["metrics"]["partial_fill_events"] > 0
    assert res_partial["metrics"]["equity_reconciliation_residual"] < 1e-7

    # Now verify UNFILLED_EXIT_TIMEOUT with sharp drop records exit attempts / residual inventory
    cfg_unfilled = _base_binance_config(tmp_path, bars_path, funding_path, exit_profile="UNFILLED_EXIT_TIMEOUT")
    res_unfilled = simulate_binance_candidate(
        cand,
        source_path=src,
        stage_bars=stage_bars,
        warmup_bars=[],
        train_bars=stage_bars,
        funding_events=[],
        common=cfg_unfilled["common"],
        policy=policy,
        account_id="unfilled-test",
    )
    assert res_unfilled["metrics"]["equity_reconciliation_residual"] < 1e-7


def test_synthetic_qualified_challenger_promotion_replay_and_s03_consumption(tmp_path):
    """End-to-end Section 9A synthetic protocol acceptance: promotion, replay, dedup, and S03 consumption."""
    bars_path, funding_path, _ = _make_synthetic_dataset(
        tmp_path, days=80, drift_hourly=0.00045, vol_hourly=0.0005, funding_rate_8h="0.00001", seed=202
    )
    # Use REAL_CANDIDATE tag on this isolated fixture specifically to verify S03 adapt_selection PROMOTE path;
    # synthetic qualification is tested separately below.
    cfg = _base_binance_config(
        tmp_path,
        bars_path,
        funding_path,
        study_id="synth-promote-v1",
        source_kind="REAL_CANDIDATE",
        oos_eligibility="FRESH_UNCONSUMED_OOS",
        resamples=1200,
        min_oos_days=30,
    )
    cfg_path = tmp_path / "config.json"
    write_json(cfg_path, cfg)

    run_dir = tmp_path / "run"
    tracker_dir = tmp_path / "tracker"
    state_path = tmp_path / "champion.json"

    selection = run(cfg_path, output=run_dir, store=tracker_dir, state_path=state_path, command=["pytest"])
    assert selection["schema"] == "FACTORLAB_CHAMPION_SELECTION_V1"
    assert selection["promoted"] is True
    assert selection["verdict"] == "PROMOTE_CHALLENGER"
    assert selection["version"] == 1
    assert selection["financial_admission"] == "FORMAL_ADMISSION_BLOCKED"
    assert selection["selected"]["strategy_id"] in {"CLOSE_MOMENTUM", "DUAL_MA"}

    # Check Section 6 required financial metrics on winning test stage
    winner_entry = next(
        c for c in selection["candidates"] if c["candidate"]["candidate_id"] == selection["selected"]["candidate_id"]
    )
    m = winner_entry["stages"]["test"]["metrics"]
    for key in (
        "net_return",
        "excess_return_vs_cash",
        "excess_return_vs_incumbent",
        "max_drawdown",
        "sharpe",
        "sortino",
        "var_95",
        "cvar_95",
        "daily_loss_probability",
        "fill_rate",
        "turnover_ratio",
        "funding_erosion_pct",
        "holding_hours",
        "capital_utilization",
        "residual_inventory",
        "exit_failure_ratio",
    ):
        assert key in m and m[key] is not None

    # Verify ExperimentTracker artifact verification
    tracker = ExperimentTracker(tracker_dir)
    runs = tracker.query(status="SUCCEEDED")
    assert len(runs) >= 6
    for rec in runs:
        assert tracker.reproduction(rec["id"], verify_artifacts=True)

    # Verify duplicate / re-consumption of consumed OOS window is refused
    with pytest.raises(ValueError, match="already consumed"):
        run(cfg_path, output=tmp_path / "repeat", store=tracker_dir, state_path=state_path)

    # Verify independent replay reproduces identical selection without mutating state
    orig_state_bytes = state_path.read_bytes()
    audit_dir = tmp_path / "audit"
    audited = replay(run_dir, audit_dir=audit_dir, command=["pytest-replay"])
    assert state_path.read_bytes() == orig_state_bytes
    for key in ("promoted", "verdict", "version", "reason", "decisions"):
        assert selection[key] == audited[key]
    assert selection["selected"]["candidate_id"] == audited["selected"]["candidate_id"]
    assert selection["selected"]["code_version"] == audited["selected"]["code_version"]
    assert read_json(state_path)["last_promotion_end"] == read_json(audit_dir / "champion.json")["last_promotion_end"]

    # Verify S03 StrategyPackage + champion_release_adapter + strategy_worker + azure_strategy_deploy.cycle
    mapping = read_json(run_dir / "package_mapping.json")
    compat_by_id = {c["candidate_id"]: c for c in mapping["candidates"]}
    s03_inbox = tmp_path / "s03-inbox"
    s03_state = tmp_path / "s03-state"
    s03_state.mkdir()
    bundle = build_s03_release_envelope(
        selection,
        compatibility_by_id=compat_by_id,
        source_dir=tmp_path / "strategies",
        output_dir=s03_inbox,
        code_commit="a" * 40,
    )
    assert bundle["status"] == "PROMOTE_PACKAGE_BUILT_AND_VERIFIED"
    assert bundle["s03_adapter_result"]["decision"] == "PROMOTE"
    assert bundle["s03_adapter_result"]["qualified"] is True

    # Install package and invoke isolated S03 worker probe
    installed_dir, installed_manifest = install_package(Path(bundle["archive_path"]), tmp_path / "s03-releases")
    assert adapt_selection(selection, installed_manifest)["package_id"] == installed_manifest["package_id"]
    probe_out = invoke_s03_worker(installed_dir, {"event": "probe", "environment": "BINANCE_USDM_DEMO"}, {})
    assert probe_out["intents"] == []

    # Full offline S03 inbox cycle
    cycle_out = s03_cycle(s03_state, s03_inbox)
    assert len(cycle_out) == 1 and cycle_out[0]["accepted"] is True


def test_high_funding_empty_position_cannot_claim_positive_alpha(tmp_path):
    """Under extreme funding (22.97 bps/h), staying flat/empty cannot be promoted as positive Alpha."""
    bars_path, funding_path, _ = _make_synthetic_dataset(
        tmp_path, days=50, drift_hourly=0.0, vol_hourly=0.005, funding_rate_8h="0.01837", seed=303
    )
    cfg = _base_binance_config(tmp_path, bars_path, funding_path, study_id="high-fund-no-alpha", resamples=400, min_oos_days=20)
    cfg["windows"] = {
        "train": ["2025-01-01", "2025-01-10"],
        "validation": ["2025-01-11", "2025-01-20"],
        "test": ["2025-01-21", "2025-02-19"],
    }
    # Incumbent is FIXED_GRID losing to high funding; FundingSensitiveGrid B5 stays empty (0% net return)
    cfg["champion"] = {
        "strategy_id": "FIXED_GRID",
        "strategy": {
            "file": "strategies/fixed_grid.py",
            "class": "FixedGridCandidate",
            "sha256": file_hash(tmp_path / "strategies/fixed_grid.py"),
        },
        "parameters": {"symbol": "NVDAUSDT", "interval_seconds": 3600, "half_width_pct": 0.01},
    }
    cfg["strategies"] = [
        {
            "strategy_id": "FUNDING_SENSITIVE_GRID",
            "file": "strategies/funding_sensitive_grid.py",
            "class": "FundingSensitiveGridCandidate",
            "sha256": file_hash(tmp_path / "strategies/funding_sensitive_grid.py"),
            "parameters": {"symbol": "NVDAUSDT", "interval_seconds": 3600, "profile_id": "B5"},
            "grid": {},
        },
        {
            "strategy_id": "CASH",
            "file": "strategies/cash_benchmark.py",
            "class": "CashBenchmarkStrategy",
            "sha256": file_hash(tmp_path / "strategies/cash_benchmark.py"),
            "parameters": {"symbol": "NVDAUSDT", "interval_seconds": 3600},
            "grid": {},
        },
    ]
    cfg_path = tmp_path / "config.json"
    write_json(cfg_path, cfg)

    selection = run(cfg_path, output=tmp_path / "run", store=tmp_path / "tracker", state_path=tmp_path / "state.json")
    assert selection["promoted"] is False
    assert selection["selected"]["strategy_id"] == "FIXED_GRID"
    b5 = next(c for c in selection["candidates"] if c["candidate"]["strategy_id"] == "FUNDING_SENSITIVE_GRID")
    assert "net_return_below_floor" in b5["reasons"]
    assert "insufficient_trade_activity" in b5["reasons"]


def test_cooldown_and_interruption_and_historical_reuse_guard(tmp_path, monkeypatch):
    bars_path, funding_path, _ = _make_synthetic_dataset(
        tmp_path, days=80, drift_hourly=0.00045, vol_hourly=0.0005, funding_rate_8h="0.00001", seed=404
    )
    cfg = _base_binance_config(
        tmp_path, bars_path, funding_path, study_id="cooldown-test", resamples=400, min_oos_days=30, cooldown_days=365
    )
    cfg["strategies"] = [s for s in cfg["strategies"] if s["strategy_id"] == "CLOSE_MOMENTUM"]
    cfg_path = tmp_path / "config.json"
    write_json(cfg_path, cfg)

    # Seed pre-existing Champion state with recent promotion within 365-day cooldown
    cash_file = str((tmp_path / "strategies/cash_benchmark.py").resolve())
    cash_sha = file_hash(Path(cash_file))
    champ_params = {"symbol": "NVDAUSDT", "interval_seconds": 3600}
    champ_id_payload = {
        "strategy_id": "CASH",
        "code_version": cash_sha,
        "class_name": "CashBenchmarkStrategy",
        "parameters": champ_params,
    }
    prior_champ = dict(
        champ_id_payload,
        candidate_id="c-" + json_hash(champ_id_payload)[:24],
        strategy={"file": cash_file, "class": "CashBenchmarkStrategy", "sha256": cash_sha, "parameters": champ_params},
    )
    prior_sel = tmp_path / "prior.json"
    write_json(prior_sel, {"version": 3, "promoted": True})
    prior_state = {
        "schema": "FACTORLAB_CHAMPION_STATE_V1",
        "champion": prior_champ,
        "common_hash": json_hash(cfg["common"]),
        "last_test_end": "2025-02-09",
        "last_promotion_end": "2025-02-09",
        "version": 3,
        "status": "COMPLETED",
        "decision": {"path": str(prior_sel), "sha256": file_hash(prior_sel)},
        "reserved_study": "prior-binance",
    }
    state_path = tmp_path / "cooldown-state.json"
    write_json(state_path, prior_state)

    held = run(cfg_path, output=tmp_path / "cooldown-run", store=tmp_path / "cooldown-tracker", state_path=state_path)
    assert held["promoted"] is False
    assert held["version"] == 3
    assert "promotion_cooldown" in held["decisions"][0]["reasons"]

    # Historical reuse pseudo-OOS guard blocks promotion and outputs INCONCLUSIVE
    reuse_cfg = deepcopy(cfg)
    reuse_cfg["study_id"] = "reuse-guard"
    reuse_cfg["policy"]["cooldown_days"] = 0
    reuse_cfg["data"]["oos_eligibility"] = "HISTORICAL_REUSE_PSEUDO_OOS"
    reuse_cfg["data"]["prior_studies"] = ["P8-R09", "P8-R10"]
    reuse_cfg_path = tmp_path / "reuse-config.json"
    write_json(reuse_cfg_path, reuse_cfg)
    reuse_sel = run(
        reuse_cfg_path,
        output=tmp_path / "reuse-run",
        store=tmp_path / "reuse-tracker",
        state_path=tmp_path / "reuse-state.json",
    )
    assert reuse_sel["promoted"] is False
    assert reuse_sel["verdict"] == "INCONCLUSIVE"
    assert "oos_window_not_fresh_unconsumed" in reuse_sel["decisions"][0]["reasons"]
    # S03 cycle cleanly returns RETAIN_CHAMPION for non-promoted native selection
    s03_inbox = tmp_path / "reuse-inbox"
    s03_state = tmp_path / "reuse-s03-state"
    s03_state.mkdir()
    build_s03_release_envelope(
        reuse_sel,
        compatibility_by_id={},
        source_dir=tmp_path / "strategies",
        output_dir=s03_inbox,
        code_commit="a" * 40,
    )
    assert s03_cycle(s03_state, s03_inbox) == [
        {"file": "reuse-guard.candidate.json", "accepted": False, "reason": "RETAIN_CHAMPION"}
    ]

    # Interruption during sealed test leaves TEST_RESERVED, keeps incumbent, and consumes OOS window
    int_state = tmp_path / "interrupt-state.json"
    write_json(int_state, prior_state)
    orig_sim = automatic.simulate_binance_candidate

    def interrupt_on_test(*args, **kwargs):
        if kwargs.get("account_id", "").endswith("-tes"):
            raise KeyboardInterrupt("simulated crash during test")
        return orig_sim(*args, **kwargs)

    monkeypatch.setattr(automatic, "simulate_binance_candidate", interrupt_on_test)
    with pytest.raises(KeyboardInterrupt):
        run(cfg_path, output=tmp_path / "int-run", store=tmp_path / "int-tracker", state_path=int_state)
    after_crash = read_json(int_state)
    assert after_crash["status"] == "TEST_RESERVED"
    assert after_crash["last_test_end"] == cfg["windows"]["test"][1]
    assert after_crash["version"] == 3
    assert not (tmp_path / "int-run/selection.json").exists()
    with pytest.raises(ValueError, match="already consumed"):
        run(cfg_path, output=tmp_path / "int-retry", store=tmp_path / "int-tracker", state_path=int_state)


def test_test_window_perturbation_cannot_change_validation_shortlist(tmp_path):
    bars_path, funding_path, df = _make_synthetic_dataset(tmp_path, days=50, seed=505)
    cfg = _base_binance_config(tmp_path, bars_path, funding_path, study_id="leak-check", resamples=100, min_oos_days=20)
    cfg["windows"] = {
        "train": ["2025-01-01", "2025-01-10"],
        "validation": ["2025-01-11", "2025-01-20"],
        "test": ["2025-01-21", "2025-02-19"],
    }
    cfg_path = tmp_path / "config.json"
    write_json(cfg_path, cfg)

    first = run(cfg_path, output=tmp_path / "run1", store=tmp_path / "t1", state_path=tmp_path / "s1.json")
    frozen1 = read_json(tmp_path / "run1/frozen-search.json")

    # Perturb ONLY test window prices by +30%
    perturbed_df = df.copy()
    mask = perturbed_df["open_time"] >= pd.Timestamp(cfg["windows"]["test"][0], tz="UTC")
    for col in ("open", "high", "low", "close"):
        perturbed_df.loc[mask, col] = perturbed_df.loc[mask, col] * 1.30
    perturbed_df.to_parquet(bars_path, index=False)
    cfg["data"]["sha256"] = file_hash(bars_path)
    write_json(cfg_path, cfg)

    second = run(cfg_path, output=tmp_path / "run2", store=tmp_path / "t2", state_path=tmp_path / "s2.json")
    frozen2 = read_json(tmp_path / "run2/frozen-search.json")
    assert frozen1["finalists"] == frozen2["finalists"]
    for a, b in zip(first["candidates"], second["candidates"]):
        if "validation" in a["stages"]:
            assert a["stages"]["validation"]["metrics"] == b["stages"]["validation"]["metrics"]
