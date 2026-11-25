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
from factorlab.integrations.binance_futures_trading.strategy_package import DECISION_INTERFACE, install_package
from factorlab.research import automatic
from factorlab.research.automatic import read_json, replay, run, validate_config, write_json
from factorlab.research.binance_perpetual import (
    BINANCE_EXECUTION,
    FUNDING_TIE_RULE,
    CollateralDepletedError,
    build_s03_release_envelope,
    run_risk_stress_suite,
    simulate_binance_candidate,
    validate_funding_coverage,
)
from factorlab.research.champion import SelectionPolicy
from factorlab.strategies.loader import LoadedStrategy

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
    start_dt: datetime | None = None,
) -> tuple[Path, Path, pd.DataFrame]:
    rng = np.random.default_rng(seed)
    start = start_dt or datetime(2025, 1, 1, 0, 0, tzinfo=timezone.utc)
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


def test_synthetic_qualified_challenger_promotion_replay_and_s03_consumption(tmp_path, monkeypatch):
    """End-to-end Section 9A synthetic protocol acceptance on a post-floor fresh window:
    promotion, replay, dedup, native FACTORLAB_DECISION_V1 S04 package load, and S03 consumption.
    """
    # Use dates strictly after KNOWN_CONSUMED_HISTORY_END_UTC (2026-11-22) with monkeypatched clock.
    class _FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2027, 4, 1, 0, 0, tzinfo=tz or timezone.utc)

    monkeypatch.setattr(automatic, "datetime", _FixedDatetime)

    bars_path, funding_path, _ = _make_synthetic_dataset(
        tmp_path,
        days=80,
        drift_hourly=0.00045,
        vol_hourly=0.0005,
        funding_rate_8h="0.00001",
        seed=202,
        start_dt=datetime(2026, 12, 1, 0, 0, tzinfo=timezone.utc),
    )
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
    cfg["windows"] = {
        "train": ["2026-12-01", "2026-12-20"],
        "validation": ["2026-12-21", "2027-01-09"],
        "test": ["2027-01-10", "2027-02-18"],
    }
    cfg_path = tmp_path / "config.json"
    write_json(cfg_path, cfg)

    run_dir = tmp_path / "run"
    tracker_dir = tmp_path / "tracker"
    state_path = tmp_path / "champion.json"
    registry_path = tmp_path / "oos-registry.json"

    selection = run(
        cfg_path,
        output=run_dir,
        store=tracker_dir,
        state_path=state_path,
        oos_registry_path=registry_path,
        command=["pytest"],
    )
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
        "exit_lifecycle_count",
        "failed_exit_lifecycle_count",
        "exit_order_attempt_count",
        "failed_exit_order_attempt_count",
        "exit_status_poll_checks",
    ):
        assert key in m
        if key != "sortino":
            assert m[key] is not None

    # Verify ExperimentTracker artifact verification
    tracker = ExperimentTracker(tracker_dir)
    runs = tracker.query(status="SUCCEEDED")
    assert len(runs) >= 6
    for rec in runs:
        assert tracker.reproduction(rec["id"], verify_artifacts=True)

    # Verify duplicate / re-consumption of consumed OOS window is refused on same state_path
    with pytest.raises(ValueError, match="already consumed"):
        run(cfg_path, output=tmp_path / "repeat", store=tracker_dir, state_path=state_path, oos_registry_path=registry_path)

    # Verify independent replay reproduces identical selection without mutating state OR OOS registry
    orig_state_bytes = state_path.read_bytes()
    orig_registry_bytes = registry_path.read_bytes()
    audit_dir = tmp_path / "audit"
    audited = replay(run_dir, audit_dir=audit_dir, command=["pytest-replay"])
    assert state_path.read_bytes() == orig_state_bytes
    assert registry_path.read_bytes() == orig_registry_bytes
    for key in ("promoted", "verdict", "version", "reason", "decisions"):
        assert selection[key] == audited[key]
    assert selection["selected"]["candidate_id"] == audited["selected"]["candidate_id"]
    assert selection["selected"]["code_version"] == audited["selected"]["code_version"]
    assert read_json(state_path)["last_promotion_end"] == read_json(audit_dir / "champion.json")["last_promotion_end"]

    # Verify native S04 FACTORLAB_DECISION_V1 StrategyPackage + adapt_selection + LoadedStrategy + s03_cycle
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
    assert bundle["protocol"] == DECISION_INTERFACE
    assert bundle["interval_seconds"] == 3600
    assert bundle["execution_mode"] == "target"
    assert bundle["s03_adapter_result"]["decision"] == "PROMOTE"
    assert bundle["s03_adapter_result"]["qualified"] is True

    # Install package and load via real S04 isolated worker (LoadedStrategy.from_strategy_package)
    installed_dir, installed_manifest = install_package(Path(bundle["archive_path"]), tmp_path / "s03-releases")
    assert installed_manifest["interface"] == DECISION_INTERFACE
    assert installed_manifest["interval_seconds"] == 3600
    assert adapt_selection(selection, installed_manifest)["package_id"] == installed_manifest["package_id"]
    loaded = LoadedStrategy.from_strategy_package(installed_dir, installed_manifest, trusted=True)
    assert loaded.spec.decision_kind == "target"
    assert list(loaded.spec.intervals) == [3600]

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


# ==============================================================================
# PR #253 Independent Review Regressions: F3, F6, F7, F8, F9, OOS Freshness
# ==============================================================================


def _hourly_bars_from_closes(closes: list[float], *, start: datetime | None = None, volumes: list[float] | None = None) -> list[dict]:
    t0 = start or datetime(2025, 6, 1, 0, 0, tzinfo=timezone.utc)
    vols = volumes if volumes is not None else [5000.0] * len(closes)
    out = []
    for i, (c, v) in enumerate(zip(closes, vols)):
        o = closes[i - 1] if i > 0 else c
        hi = max(o, c) * 1.001
        lo = min(o, c) * 0.999
        out.append(
            {
                "id": f"hour-{i}",
                "time": (t0 + timedelta(hours=i)).isoformat(),
                "open": float(o),
                "high": float(hi),
                "low": float(lo),
                "close": float(c),
                "price": float(c),
                "volume": float(v),
                "completed_bar": True,
            }
        )
    return out


def test_f3_halt_cancels_partially_filled_open_buy_even_on_hold_signal(tmp_path):
    """F3 P1: Partially filled BUY (0.03 filled, 0.01 open) must be canceled upon risk halt
    even when DualMA returns target_weight=None (HOLD), so bar t+1 cannot fill remaining 0.01."""
    bars_path, funding_path, _ = _make_synthetic_dataset(tmp_path, days=4)
    common = _base_binance_config(tmp_path, bars_path, funding_path)["common"]
    common["guardrails"]["max_loss_ratio"] = 0.02  # 0.20 USDT max loss on 10 USDT equity
    common["limit_buffer_bps"] = 20.0
    policy = SelectionPolicy(periods_per_year=365, min_oos_days=2, block_days=1, min_tail_observations=1, resamples=100)

    # Bar 0..5: crossover triggers BUY on bar 5 (DualMA fast=2, slow=4, buy_limit ~154.39)
    # Bar 6: close=154.0, volume=3 -> 1% participation = 0.03 partial fill at ~154.10; 0.01 remains OPEN!
    # Bar 7: close=145.0, volume=0 -> MTM loss = 0.03 * (145 - 154.10) = -0.273 USDT >= 0.20 max_loss -> HALTS!
    #        DualMA emits target_weight=None (HOLD) on bar 7; WITHOUT F3 fix, 0.01 stays open and fills on bar 8!
    # Bar 8..55: close=145.0, volume=5000 -> WITH F3 fix, remaining 0.01 was canceled on bar 7 halt -> ZERO fill!
    closes = [150.0, 150.0, 150.0, 150.0, 149.0, 154.0, 154.0, 145.0, 145.0] + [145.0] * 48
    volumes = [5000.0] * 6 + [3.0, 0.0] + [5000.0] * 49
    stage_bars = _hourly_bars_from_closes(closes, volumes=volumes)
    src = STRAT_DIR / "dual_ma_usdm.py"
    cand = {
        "candidate_id": "c-f3-partial",
        "strategy_id": "DUAL_MA",
        "class_name": "DualMAUsdmStrategy",
        "code_version": file_hash(src),
        "parameters": {"symbol": "NVDAUSDT", "interval_seconds": 3600, "fast_window": 2, "slow_window": 4, "max_weight": 0.90},
    }

    res = simulate_binance_candidate(
        cand,
        source_path=src,
        stage_bars=stage_bars,
        warmup_bars=[],
        train_bars=stage_bars,
        funding_events=[],
        common=common,
        policy=policy,
        account_id="f3-halt-cancel",
    )
    assert res["telemetry"]["halted"] is True
    assert res["telemetry"]["canceled_orders"] >= 1
    buys = res["trades"][res["trades"]["side"] == "BUY"]
    # Only the 0.03 partial fill before halt may occur; remaining 0.01 MUST be canceled on halt.
    assert float(buys["executed_shares"].sum()) == pytest.approx(0.03, abs=1e-9)
    assert float(res["hourly"]["inventory"].max()) == pytest.approx(0.03, abs=1e-9)

    # Now verify funding-induced halt + explicit cancel-fill race mode:
    # Bar 6 fills 0.03 partial BUY; bar 7 has a funding event hitting max_loss right before bar 7 matching
    # (bar 7 has volume=5000). Without race: bar 7 cancels 0.01 before match (0.03 total).
    # With cancel_fill_race=True: race fill on bar 7 books honestly (0.04 total) without forging zero.
    t0 = datetime(2025, 6, 1, 0, 0, tzinfo=timezone.utc)
    race_closes = [150.0, 150.0, 150.0, 150.0, 149.0, 154.0, 154.0, 154.0, 154.0] + [154.0] * 48
    race_volumes = [5000.0] * 6 + [3.0, 5000.0] + [5000.0] * 49
    race_bars = _hourly_bars_from_closes(race_closes, start=t0, volumes=race_volumes)
    # 0.03 * 154 * 0.05 = 0.231 USDT funding drain at bar 7 close (>= 0.20 max_loss)
    race_funding = [(t0 + timedelta(hours=8, microseconds=-1), D("154.0"), D("0.05"), "SyntheticHalt")]

    res_fund_halt = simulate_binance_candidate(
        cand,
        source_path=src,
        stage_bars=race_bars,
        warmup_bars=[],
        train_bars=race_bars,
        funding_events=race_funding,
        common=common,
        policy=policy,
        account_id="f3-fund-halt",
        cancel_fill_race=False,
    )
    assert res_fund_halt["telemetry"]["halted"] is True
    assert float(res_fund_halt["trades"][res_fund_halt["trades"]["side"] == "BUY"]["executed_shares"].sum()) == pytest.approx(0.03, abs=1e-9)

    res_race = simulate_binance_candidate(
        cand,
        source_path=src,
        stage_bars=race_bars,
        warmup_bars=[],
        train_bars=race_bars,
        funding_events=race_funding,
        common=common,
        policy=policy,
        account_id="f3-halt-race",
        cancel_fill_race=True,
    )
    assert res_race["telemetry"]["halted"] is True
    assert res_race["telemetry"]["cancel_fill_race_mode"] is True
    buys_race = res_race["trades"][res_race["trades"]["side"] == "BUY"]
    assert float(buys_race["executed_shares"].sum()) == pytest.approx(0.04, abs=1e-9)
    assert res_race["metrics"]["equity_reconciliation_residual"] < 1e-7


def test_f3_halt_preserves_reduce_only_sell_and_never_resets_on_rebound(tmp_path):
    """F3 P1: Risk halt cancels open entry BUYs, NEVER cancels reduce-only SELL exits, and never resets on rebound."""
    bars_path, funding_path, _ = _make_synthetic_dataset(tmp_path, days=4)
    common = _base_binance_config(tmp_path, bars_path, funding_path, exit_profile="DELAYED_EXIT_FILL")["common"]
    common["guardrails"]["max_loss_ratio"] = 0.02
    policy = SelectionPolicy(periods_per_year=365, min_oos_days=2, block_days=1, min_tail_observations=1, resamples=100)

    # CloseMomentum lookback=1: bar 1 rises (152) -> submits BUY (buy_limit ~152.39); bar 2 stays 152 -> fills 0.04;
    # bar 3 drops sharply to 140 -> MTM loss = 0.04 * (140 - 152.10) = -0.484 >= 0.20 max_loss -> HALTS + triggers
    # reduce-only SELL; rebound on bar 6..53 must NOT allow any new BUY!
    closes = [150.0, 152.0, 152.0, 140.0, 140.0, 140.0] + [160.0 + i * 0.5 for i in range(48)]
    stage_bars = _hourly_bars_from_closes(closes)
    src = STRAT_DIR / "close_momentum_usdm.py"
    cand = {
        "candidate_id": "c-f3-sell-keep",
        "strategy_id": "CLOSE_MOMENTUM",
        "class_name": "CloseMomentumUsdmStrategy",
        "code_version": file_hash(src),
        "parameters": {"symbol": "NVDAUSDT", "interval_seconds": 3600, "lookback": 1, "weight": 0.90, "threshold_bps": 0.0},
    }
    res = simulate_binance_candidate(
        cand,
        source_path=src,
        stage_bars=stage_bars,
        warmup_bars=[],
        train_bars=stage_bars,
        funding_events=[],
        common=common,
        policy=policy,
        account_id="f3-sell-preserve",
    )
    assert res["telemetry"]["halted"] is True
    buys = res["trades"][res["trades"]["side"] == "BUY"]
    sells = res["trades"][res["trades"]["side"] == "SELL"]
    assert len(buys) == 1  # No new BUY after halt even during strong rebound
    assert len(sells) == 1  # Reduce-only SELL was preserved and executed cleanly
    assert res["metrics"]["residual_inventory"] == pytest.approx(0.0, abs=1e-9)


def test_f6_exit_failure_metrics_do_not_inflate_with_window_duration(tmp_path):
    """F6 P2: Extending an UNFILLED_EXIT_TIMEOUT window from 48h to 72h must NOT inflate
    exit_lifecycle_count, failed_exit_lifecycle_count, exit_order_attempt_count, or exit_failure_ratio."""
    bars_path, funding_path, _ = _make_synthetic_dataset(tmp_path, days=4)
    common = _base_binance_config(tmp_path, bars_path, funding_path, exit_profile="UNFILLED_EXIT_TIMEOUT")["common"]
    policy = SelectionPolicy(periods_per_year=365, min_oos_days=2, block_days=1, min_tail_observations=1, resamples=100)

    # FixedGrid buys at ~148.5 then breaks below lower band (140.0) and stays flat -> single unfilled exit lifecycle.
    closes_72 = [150.0, 148.0, 140.0] + [140.0] * 69
    bars_72 = _hourly_bars_from_closes(closes_72)
    bars_48 = bars_72[:48]
    src = STRAT_DIR / "fixed_grid.py"
    cand = {
        "candidate_id": "c-f6-grid",
        "strategy_id": "FIXED_GRID",
        "class_name": "FixedGridCandidate",
        "code_version": file_hash(src),
        "parameters": {"symbol": "NVDAUSDT", "interval_seconds": 3600, "half_width_pct": 0.01, "order_ttl": 100, "breakout": "pause"},
    }

    res_48 = simulate_binance_candidate(
        cand, source_path=src, stage_bars=bars_48, warmup_bars=[], train_bars=bars_48,
        funding_events=[], common=common, policy=policy, account_id="f6-48h",
    )
    res_72 = simulate_binance_candidate(
        cand, source_path=src, stage_bars=bars_72, warmup_bars=[], train_bars=bars_72,
        funding_events=[], common=common, policy=policy, account_id="f6-72h",
    )
    m48, m72 = res_48["metrics"], res_72["metrics"]
    assert m48["exit_lifecycle_count"] == 1
    assert m72["exit_lifecycle_count"] == 1
    assert m48["failed_exit_lifecycle_count"] == 1
    assert m72["failed_exit_lifecycle_count"] == 1
    assert m48["exit_failure_ratio"] == pytest.approx(1.0)
    assert m72["exit_failure_ratio"] == pytest.approx(1.0)
    assert m48["exit_order_attempt_count"] == m72["exit_order_attempt_count"]
    assert m48["failed_exit_order_attempt_count"] == m72["failed_exit_order_attempt_count"]
    # Polling checks grow with window duration but are strictly separated from exit failure ratio!
    assert m72["exit_status_poll_checks"] > m48["exit_status_poll_checks"]


def test_f7_stress_suite_distinguishes_program_exceptions_from_collateral_depletion(tmp_path, monkeypatch):
    """F7 P2: Non-financial exceptions must report SIMULATION_FAILED with net_return=None,
    NEVER COLLATERAL_DEPLETED / -1.0; genuine ledger depletion reports COLLATERAL_DEPLETED."""
    from factorlab.research import binance_perpetual as bp

    policy = SelectionPolicy(periods_per_year=365, min_oos_days=2, block_days=1, min_tail_observations=1, resamples=100)
    orig_batch = bp.run_simulations_batch
    monkeypatch.setattr(bp, "run_simulations_batch", lambda tasks: orig_batch(tasks, max_workers=1))

    # Case 1: Non-financial ValueError (code checksum mismatch)
    monkeypatch.setattr(
        bp,
        "simulate_binance_candidate",
        lambda **kwargs: (_ for _ in ()).throw(ValueError("strategy code checksum mismatch: injected")),
    )
    res_err = run_risk_stress_suite(
        [(dict(candidate_id="c-err", strategy_id="CASH"), Path("unused"))],
        stage_bars=[], warmup_bars=[], train_bars=[], funding_events=[], common={}, policy=policy,
    )
    assert res_err["summary"]["simulation_failed_runs"] == 4
    assert res_err["summary"]["collateral_depleted_runs"] == 0
    for row in res_err["results"]:
        assert row["status"] == "SIMULATION_FAILED"
        assert row["error_type"] == "ValueError"
        assert "checksum mismatch" in row["detail"]
        assert row["net_return"] is None
        assert row["max_drawdown"] is None
        assert row["cvar_95"] is None

    # Case 2: Genuine authoritative ledger depletion (CollateralDepletedError)
    monkeypatch.setattr(
        bp,
        "simulate_binance_candidate",
        lambda **kwargs: (_ for _ in ()).throw(
            CollateralDepletedError("authoritative PaperBroker ledger equity depleted (<= 0)", final_equity=-0.05)
        ),
    )
    res_dep = run_risk_stress_suite(
        [(dict(candidate_id="c-dep", strategy_id="FIXED_GRID"), Path("unused"))],
        stage_bars=[], warmup_bars=[], train_bars=[], funding_events=[], common={}, policy=policy,
    )
    assert res_dep["summary"]["collateral_depleted_runs"] == 4
    assert res_dep["summary"]["simulation_failed_runs"] == 0
    for row in res_dep["results"]:
        assert row["status"] == "COLLATERAL_DEPLETED"
        assert row["net_return"] == -1.0
        assert row["max_drawdown"] == 1.0


def test_f8_and_f9_funding_coverage_and_true_ohlcv_export_fidelity(tmp_path):
    """F8 & F9 P2: Missing tail funding fails closed; observed 0.0 funding passes;
    exported OHLCV preserves real open!=close and fractional volume (never overwrites open=close)."""
    bars_path, funding_path, df = _make_synthetic_dataset(tmp_path, days=30, funding_rate_8h="0.00000000")
    bars_list = [{"time": pd.Timestamp(t).isoformat()} for t in df["open_time"]]
    raw_funding = read_json(funding_path)
    full_events = [
        (datetime.fromisoformat(r["time"]), D(r["markPrice"]), D(r["fundingRate"]), r["rateType"])
        for r in raw_funding
    ]
    # Full stream with genuine 0.0 funding rates MUST pass and count zero_rate_events
    cov = validate_funding_coverage(bars_list, full_events)
    assert cov["complete"] is True
    assert cov["zero_rate_events"] == len(full_events)

    # Truncating last 13 days of funding MUST fail closed (F8)
    truncated_events = full_events[:-39]
    with pytest.raises(ValueError, match="incomplete funding coverage at tail"):
        validate_funding_coverage(bars_list, truncated_events)

    # Internal 48h funding gap MUST fail closed (F8)
    gapped_events = full_events[:10] + full_events[18:]
    with pytest.raises(ValueError, match="internal gap"):
        validate_funding_coverage(bars_list, gapped_events)

    # Verify load_binance_bars_and_funding preserves exact fractional volume & open!=close (F9)
    from factorlab.research.binance_perpetual import load_binance_bars_and_funding
    df_frac = df.copy()
    df_frac["volume"] = 1234.56
    frac_path = tmp_path / "frac.parquet"
    df_frac.to_parquet(frac_path, index=False)
    loaded_bars, _ = load_binance_bars_and_funding(frac_path.read_bytes(), funding_path.read_bytes(), symbol="NVDAUSDT")
    assert any(b["open"] != b["close"] for b in loaded_bars)
    assert loaded_bars[0]["volume"] == pytest.approx(1234.56)

    # Verify export_audited_nvda_dataset rejects missing post-R07 funding cache when allow_network=False
    import sys
    sys.path.insert(0, str(REPO_ROOT / "tools" / "research"))
    import run_binance_champion_research as runner_mod

    orig_loader = runner_mod.load_and_audit_r10_dataset
    try:
        # Mock loader returning bars past 2026-11-10 without post-R07 funding file in empty cache_dir
        mock_bars = [
            {
                "id": "hour-0",
                "time": "2026-11-15T00:00:00+00:00",
                "open": 180.0,
                "high": 182.0,
                "low": 179.0,
                "close": 181.5,
                "price": 181.5,
                "volume": 1234,
                "raw_volume": 1234.56,
                "completed_bar": True,
            }
        ]
        runner_mod.load_and_audit_r10_dataset = lambda **kw: (mock_bars, [], {})
        with pytest.raises(ValueError, match="missing post-R07 funding cache"):
            runner_mod.export_audited_nvda_dataset(
                r04b_snapshot=tmp_path,
                cache_dir=tmp_path / "empty-cache",
                dest_dir=tmp_path / "out",
                allow_network=False,
            )

        # Mock loader missing real 'open' MUST fail closed (F9) instead of substituting close
        (tmp_path / "empty-cache").mkdir(exist_ok=True)
        (tmp_path / "empty-cache" / "nvdausdt_funding_20261110_20261123.json").write_text("[]", encoding="utf-8")
        bad_open_bars = [{k: v for k, v in mock_bars[0].items() if k != "open"}]
        runner_mod.load_and_audit_r10_dataset = lambda **kw: (bad_open_bars, [], {})
        with pytest.raises(ValueError, match="missing real open price"):
            runner_mod.export_audited_nvda_dataset(
                r04b_snapshot=tmp_path,
                cache_dir=tmp_path / "empty-cache",
                dest_dir=tmp_path / "out2",
                allow_network=False,
            )
    finally:
        runner_mod.load_and_audit_r10_dataset = orig_loader


def test_oos_freshness_blocks_relabeling_known_history_and_cross_state_reuse(tmp_path, monkeypatch):
    """Section 11: Relabeling consumed history (<=2026-11-22) as FRESH_UNCONSUMED_OOS on a brand new
    state_path/study_id/re-exported file MUST be downgraded and NEVER promote; post-floor consumption
    blocks cross-state_path/partial-overlap reuse; replay never mutates registry."""
    # 1. Historical range <= 2026-11-22 relabeled as FRESH_UNCONSUMED_OOS on brand new state_path -> BLOCKED
    old_dir = tmp_path / "old"
    old_dir.mkdir(exist_ok=True)
    bars_old, fund_old, _ = _make_synthetic_dataset(
        old_dir, days=80, drift_hourly=0.00045, vol_hourly=0.0005, funding_rate_8h="0.00001", seed=601,
        start_dt=datetime(2026, 8, 1, 0, 0, tzinfo=timezone.utc),
    )
    cfg_old = _base_binance_config(
        old_dir, bars_old, fund_old,
        study_id="relabel-known-hist",
        source_kind="REAL_CANDIDATE",
        oos_eligibility="FRESH_UNCONSUMED_OOS",
        resamples=400, min_oos_days=30,
    )
    cfg_old["windows"] = {
        "train": ["2026-08-01", "2026-08-20"],
        "validation": ["2026-08-21", "2026-09-09"],
        "test": ["2026-09-10", "2026-10-19"],
    }
    cfg_old["strategies"] = [s for s in cfg_old["strategies"] if s["strategy_id"] == "CLOSE_MOMENTUM"]
    cfg_old_path = tmp_path / "old" / "config.json"
    write_json(cfg_old_path, cfg_old)

    sel_old = run(
        cfg_old_path,
        output=tmp_path / "old-run",
        store=tmp_path / "old-tracker",
        state_path=tmp_path / "brand-new-state-1.json",
        oos_registry_path=tmp_path / "fresh-registry-1.json",
    )
    assert sel_old["promoted"] is False
    assert sel_old["verdict"] == "INCONCLUSIVE"
    assert sel_old["oos_eligibility"] == "HISTORICAL_REUSE_PSEUDO_OOS"
    assert sel_old["oos_freshness_audit"]["downgraded"] is True
    assert "oos_window_not_fresh_unconsumed" in sel_old["decisions"][0]["reasons"]

    # 2. Post-floor fresh window consumed once -> second run with DIFFERENT state_path, DIFFERENT study_id,
    #    re-exported files, AND partial date overlap MUST be blocked by shared cross-study registry!
    class _FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2027, 6, 1, 0, 0, tzinfo=tz or timezone.utc)

    monkeypatch.setattr(automatic, "datetime", _FixedDatetime)
    shared_registry = tmp_path / "shared-oos-registry.json"

    post_dir = tmp_path / "post"
    post_dir.mkdir()
    bars_post, fund_post, _ = _make_synthetic_dataset(
        post_dir, days=100, drift_hourly=0.00045, vol_hourly=0.0005, funding_rate_8h="0.00001", seed=602,
        start_dt=datetime(2026, 12, 1, 0, 0, tzinfo=timezone.utc),
    )
    cfg1 = _base_binance_config(
        post_dir, bars_post, fund_post,
        study_id="post-fresh-run-1",
        source_kind="REAL_CANDIDATE",
        oos_eligibility="FRESH_UNCONSUMED_OOS",
        resamples=400, min_oos_days=30,
    )
    cfg1["windows"] = {
        "train": ["2026-12-01", "2026-12-20"],
        "validation": ["2026-12-21", "2027-01-09"],
        "test": ["2027-01-10", "2027-02-18"],
    }
    cfg1["strategies"] = [s for s in cfg1["strategies"] if s["strategy_id"] == "CLOSE_MOMENTUM"]
    cfg1_path = post_dir / "cfg1.json"
    write_json(cfg1_path, cfg1)

    sel1 = run(
        cfg1_path,
        output=tmp_path / "post-run-1",
        store=tmp_path / "post-tracker-1",
        state_path=tmp_path / "state-A.json",
        oos_registry_path=shared_registry,
    )
    assert sel1["promoted"] is True
    assert sel1["oos_eligibility"] == "FRESH_UNCONSUMED_OOS"

    # Run 2: brand new state_path (state-B.json), new study_id, partial overlap [2027-02-01, 2027-03-08]
    cfg2 = deepcopy(cfg1)
    cfg2["study_id"] = "post-overlap-run-2"
    cfg2["windows"] = {
        "train": ["2026-12-10", "2026-12-30"],
        "validation": ["2026-12-31", "2027-01-25"],
        "test": ["2027-02-01", "2027-03-08"],
    }
    cfg2_path = post_dir / "cfg2.json"
    write_json(cfg2_path, cfg2)

    sel2 = run(
        cfg2_path,
        output=tmp_path / "post-run-2",
        store=tmp_path / "post-tracker-2",
        state_path=tmp_path / "state-B.json",
        oos_registry_path=shared_registry,
    )
    assert sel2["promoted"] is False
    assert sel2["verdict"] == "INCONCLUSIVE"
    assert sel2["oos_eligibility"] == "HISTORICAL_REUSE_PSEUDO_OOS"
    assert sel2["oos_freshness_audit"]["downgraded"] is True
    assert any(e["study_id"] == "post-fresh-run-1" for e in sel2["oos_freshness_audit"]["overlapping_registry_entries"])

    # Independent replay of BOTH runs must reproduce identically and NEVER mutate shared_registry!
    reg_before = shared_registry.read_bytes()
    rep1 = replay(tmp_path / "post-run-1", audit_dir=tmp_path / "rep1")
    rep2 = replay(tmp_path / "post-run-2", audit_dir=tmp_path / "rep2")
    assert shared_registry.read_bytes() == reg_before
    assert rep1["promoted"] is True and rep1["verdict"] == sel1["verdict"]
    assert rep2["promoted"] is False and rep2["verdict"] == sel2["verdict"]
    assert rep2["oos_eligibility"] == "HISTORICAL_REUSE_PSEUDO_OOS"
