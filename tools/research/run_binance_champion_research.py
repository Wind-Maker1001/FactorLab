"""Executable offline entry point for Binance USD-M NVDAUSDT Champion Research (P8-S02-F01).

Reuses FactorLab's existing S02 automatic orchestrator (run/replay), S01 protocol
candidates, R09/R10 execution & funding economics, ExperimentTracker, and S03
champion_release_adapter / azure_strategy_deploy.cycle offline verification.

NO live money, NO Azure operations, NO Demo order submissions.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Any

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from factorlab.dataset.identity import file_hash
from factorlab.experiments.grid_f11_f08_offline import MODEL_F11_F08
from factorlab.experiments.grid_funding_adaptive import load_and_audit_r10_dataset
from factorlab.integrations.binance_futures_trading.azure_strategy_deploy import cycle as s03_cycle
from factorlab.research.automatic import read_json, replay, run, write_json
from factorlab.research.binance_perpetual import (
    BINANCE_EXECUTION,
    FUNDING_TIE_RULE,
    build_s03_release_envelope,
)


def _git_head_commit() -> str:
    try:
        out = subprocess.check_output(
            ["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"], text=True
        ).strip()
        if len(out) == 40:
            return out
    except Exception:
        pass
    return "0" * 40


def export_audited_nvda_dataset(
    *,
    r04b_snapshot: Path,
    cache_dir: Path,
    dest_dir: Path,
    allow_network: bool = False,
) -> dict[str, Any]:
    """Export audited 193-day continuous NVDAUSDT 1h bars parquet and funding JSON."""
    dest_dir = Path(dest_dir).resolve()
    dest_dir.mkdir(parents=True, exist_ok=True)
    bars, funding, audit = load_and_audit_r10_dataset(
        repo_root=REPO_ROOT,
        r04b_snapshot=r04b_snapshot,
        cache_dir=cache_dir,
        allow_network_supplement=allow_network,
    )
    rows = []
    for b in bars:
        open_px = float(b.get("open", b["close"]))
        hi = max(float(b["high"]), open_px, float(b["close"]))
        lo = min(float(b["low"]), open_px, float(b["close"]))
        rows.append(
            {
                "open_time": pd.Timestamp(b["time"]).tz_convert("UTC"),
                "symbol": "NVDAUSDT",
                "open": open_px,
                "high": hi,
                "low": lo,
                "close": float(b["close"]),
                "volume": float(b["volume"]),
            }
        )
    bars_df = pd.DataFrame(rows)
    bars_path = dest_dir / "nvdausdt_1h_audited.parquet"
    bars_df.to_parquet(bars_path, index=False)

    funding_rows = [
        {
            "symbol": "NVDAUSDT",
            "fundingTime": int(round(t.timestamp() * 1000)),
            "time": t.isoformat(),
            "markPrice": str(mark),
            "fundingRate": str(rate),
            "rateType": kind,
        }
        for t, mark, rate, kind in funding
    ]
    funding_path = dest_dir / "nvdausdt_funding_audited.json"
    write_json(funding_path, funding_rows)
    audit_path = dest_dir / "data_audit.json"
    write_json(audit_path, audit)
    return {
        "bars_path": str(bars_path),
        "bars_sha256": file_hash(bars_path),
        "funding_path": str(funding_path),
        "funding_sha256": file_hash(funding_path),
        "audit_path": str(audit_path),
        "total_bars": len(rows),
        "total_funding_events": len(funding_rows),
        "audit": audit,
    }


def build_frozen_binance_config(
    *,
    study_id: str,
    bars_rel: str,
    bars_sha256: str,
    funding_rel: str,
    funding_sha256: str,
    strategy_dir_rel: str,
    strategy_dir_abs: Path,
    windows: dict[str, list[str]],
    source_kind: str = "REAL_CANDIDATE",
    oos_eligibility: str = "HISTORICAL_REUSE_PSEUDO_OOS",
    prior_studies: list[str] | None = None,
    execution_lifecycle: str = MODEL_F11_F08,
    exit_profile: str = "DELAYED_EXIT_FILL",
) -> dict[str, Any]:
    """Construct the predeclared frozen P8-S02-F01 Binance USD-M research configuration."""
    r10_b = read_json(REPO_ROOT / "configs/research/p8_r10_strategy_b_funding_sensitive.json")
    r10_c = read_json(REPO_ROOT / "configs/research/p8_r10_strategy_c_vol_adaptive.json")

    def _sfile(name: str) -> tuple[str, str]:
        p = strategy_dir_abs / name
        rel = f"{strategy_dir_rel}/{name}".replace("\\", "/")
        return rel, file_hash(p)

    cash_file, cash_sha = _sfile("cash_benchmark.py")
    fixed_file, fixed_sha = _sfile("fixed_grid.py")
    fund_file, fund_sha = _sfile("funding_sensitive_grid.py")
    vol_file, vol_sha = _sfile("vol_adaptive_grid.py")
    dma_file, dma_sha = _sfile("dual_ma_usdm.py")
    mom_file, mom_sha = _sfile("close_momentum_usdm.py")

    strategies: list[dict[str, Any]] = [
        {
            "strategy_id": "CASH",
            "file": cash_file,
            "class": "CashBenchmarkStrategy",
            "sha256": cash_sha,
            "parameters": {"symbol": "NVDAUSDT", "interval_seconds": 3600},
            "grid": {},
        },
        {
            "strategy_id": "FIXED_GRID_ALT",
            "file": fixed_file,
            "class": "FixedGridCandidate",
            "sha256": fixed_sha,
            "parameters": {"symbol": "NVDAUSDT", "interval_seconds": 3600, "order_ttl": 100, "breakout": "pause"},
            "grid": {"half_width_pct": [0.015, 0.02]},
        },
    ]

    for c in r10_b["predeclared_candidates"]:
        strategies.append(
            {
                "strategy_id": f"FUNDING_SENSITIVE_GRID_{c['id']}",
                "file": fund_file,
                "class": "FundingSensitiveGridCandidate",
                "sha256": fund_sha,
                "parameters": {
                    "symbol": "NVDAUSDT",
                    "interval_seconds": 3600,
                    "profile_id": c["id"],
                    "half_width_pct": 0.01,
                    "lookback_hours": int(c["lookback_hours"]),
                    "conservative": bool(c["conservative"]),
                    "entry_max_bps_h": float(c["entry_max_bps_h"]),
                    "expected_hold_hours": float(c["expected_hold_hours"]),
                    "min_net_margin_bps": float(c["min_net_margin_bps"]),
                    "exit_mode": str(c["exit_mode"]),
                    "exit_bps_h": float(c["exit_bps_h"]) if c["exit_bps_h"] is not None else -1.0,
                    "max_hold_hours": float(c["max_hold_hours"]) if c["max_hold_hours"] is not None else 0.0,
                },
                "grid": {},
            }
        )

    for c in r10_c["predeclared_candidates"]:
        strategies.append(
            {
                "strategy_id": f"VOL_ADAPTIVE_GRID_{c['id']}",
                "file": vol_file,
                "class": "VolAdaptiveGridCandidate",
                "sha256": vol_sha,
                "parameters": {
                    "symbol": "NVDAUSDT",
                    "interval_seconds": 3600,
                    "profile_id": c["id"],
                    "vol_window_hours": int(c["vol_window_hours"]),
                    "k_mult": float(c["k_mult"]),
                    "horizon_hours": float(c["horizon_hours"]),
                    "min_half_width": float(c["min_half_width"]),
                    "max_half_width": float(c["max_half_width"]),
                    "extreme_vol_mult": float(c["extreme_vol_mult"]) if c["extreme_vol_mult"] is not None else 0.0,
                    "allow_flat_rebuild": bool(c["allow_flat_rebuild"]),
                    "min_rebuild_interval": int(c["min_rebuild_interval"]),
                    "rebuild_width_delta": float(c["rebuild_width_delta"]),
                },
                "grid": {},
            }
        )

    strategies.append(
        {
            "strategy_id": "DUAL_MA",
            "file": dma_file,
            "class": "DualMAUsdmStrategy",
            "sha256": dma_sha,
            "parameters": {"symbol": "NVDAUSDT", "interval_seconds": 3600, "max_weight": 0.90},
            "grid": {"fast_window": [5, 12], "slow_window": [20, 48]},
        }
    )

    strategies.append(
        {
            "strategy_id": "CLOSE_MOMENTUM",
            "file": mom_file,
            "class": "CloseMomentumUsdmStrategy",
            "sha256": mom_sha,
            "parameters": {"symbol": "NVDAUSDT", "interval_seconds": 3600, "weight": 0.90},
            "grid": {"lookback": [1, 6, 24], "threshold_bps": [0.0, 25.0]},
        }
    )

    data_block: dict[str, Any] = {
        "bars": bars_rel,
        "sha256": bars_sha256,
        "funding": funding_rel,
        "funding_sha256": funding_sha256,
        "source_kind": source_kind,
        "oos_eligibility": oos_eligibility,
    }
    if oos_eligibility == "HISTORICAL_REUSE_PSEUDO_OOS":
        data_block["prior_studies"] = prior_studies or [
            "P7-R04B",
            "P7-R09",
            "P8-R07",
            "P8-R08",
            "P8-R09",
            "P8-R10",
        ]

    return {
        "schema": "FACTORLAB_AUTO_RESEARCH_V1",
        "study_id": study_id,
        "data": data_block,
        "windows": windows,
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
            "limit_buffer_bps": 10.0,
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
                "total_orders": 2000,
                "total_notional": 20000.0,
                "window_orders": 20,
                "window_seconds": 10800,
            },
            "price_basis": "1h Binance USD-M trade close with adverse slippage; offline bar close is NOT live exchange orderbook matching",
            "rule_boundary_note": (
                "Historical NVDAUSDT traded at 126-243 USDT (0.04 units = 5.05-9.70 USDT), exceeding the "
                "7.00 USDT Demo live cap on high-price weeks; offline replay uses 10.00 USDT 1x collateral "
                "risk budget matching R08/R09/R10 while retaining 5 USDT min_notional, 0.01 step/tick, and 0.97-1.03 PERCENT_PRICE."
            ),
        },
        "policy": {
            "objective": "risk_adjusted_net_return",
            "risk_aversion": 1.0,
            "periods_per_year": 365,
            "max_drawdown": 0.20,
            "max_daily_cvar": 0.05,
            "cvar_confidence": 0.95,
            "participation_limit": 0.01,
            "min_capacity_usdt": 10.0,
            "min_net_return": 0.0,
            "min_score_improvement": 0.02,
            "min_annual_net_improvement": 0.01,
            "min_oos_days": 60,
            "min_tail_observations": 3,
            "min_positive_blocks": 0.60,
            "cooldown_days": 30,
            "confidence": 0.95,
            "block_days": 5,
            "resamples": 6000,
            "seed": 42,
            "max_candidates": 32,
            "max_finalists": 6,
            "volume_lookback": 20,
            "min_trade_count": 2,
            "max_exit_failure_ratio": 0.50,
        },
        "strategies": strategies,
        "champion": {
            "strategy_id": "FIXED_GRID",
            "strategy": {
                "file": fixed_file,
                "class": "FixedGridCandidate",
                "sha256": fixed_sha,
            },
            "parameters": {
                "symbol": "NVDAUSDT",
                "interval_seconds": 3600,
                "half_width_pct": 0.01,
                "order_ttl": 100,
                "breakout": "pause",
            },
        },
    }


def verify_replay_and_s03(
    run_dir: Path,
    state_path: Path,
    audit_dir: Path,
    s03_dir: Path,
) -> dict[str, Any]:
    """Execute independent replay and S03 offline inbox consumption verification."""
    original = read_json(run_dir / "selection.json")
    original_state = read_json(state_path)
    audited = replay(run_dir, audit_dir=audit_dir, command=[sys.executable, "replay"])
    audit_state = read_json(audit_dir / "champion.json")

    match_checks = {
        "candidate_id_match": original["selected"]["candidate_id"] == audited["selected"]["candidate_id"],
        "code_version_match": original["selected"]["code_version"] == audited["selected"]["code_version"],
        "promoted_match": original["promoted"] == audited["promoted"],
        "verdict_match": original["verdict"] == audited["verdict"],
        "version_match": original["version"] == audited["version"],
        "reason_match": original["reason"] == audited["reason"],
        "decisions_match": original["decisions"] == audited["decisions"],
        "state_last_test_end_match": original_state["last_test_end"] == audit_state["last_test_end"],
        "state_last_promotion_end_match": original_state["last_promotion_end"] == audit_state["last_promotion_end"],
        "state_version_match": original_state["version"] == audit_state["version"],
        "state_status_match": original_state["status"] == audit_state["status"],
    }
    if not all(match_checks.values()):
        raise ValueError(f"independent replay verification failed: {match_checks}")

    mapping = read_json(run_dir / "package_mapping.json")
    compat_by_id = {c["candidate_id"]: c for c in mapping["candidates"]}
    inbox_dir = s03_dir / "inbox"
    s03_state_root = s03_dir / "state"
    s03_state_root.mkdir(parents=True, exist_ok=True)
    envelope_info = build_s03_release_envelope(
        original,
        compatibility_by_id=compat_by_id,
        source_dir=REPO_ROOT / "src/factorlab/strategies/binance_usdm",
        output_dir=inbox_dir,
        code_commit=_git_head_commit(),
    )
    cycle_results = s03_cycle(s03_state_root, inbox_dir) if inbox_dir.exists() and list(inbox_dir.glob("*.candidate.json")) else []
    return {
        "verified_at_utc": datetime.now(timezone.utc).isoformat(),
        "replay_checks": match_checks,
        "all_replay_checks_passed": all(match_checks.values()),
        "s03_envelope": envelope_info,
        "s03_cycle_results": cycle_results,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--r04b-snapshot", type=Path, required=True)
    parser.add_argument("--cache-dir", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True, help="Local scratch directory for exported data, tracker, and run outputs.")
    parser.add_argument("--frozen-config-out", type=Path, default=REPO_ROOT / "configs/research/p8_s02_f01_binance_nvda_frozen.json")
    parser.add_argument("--evidence-dir", type=Path, default=REPO_ROOT / "docs/validation/p8-s02-f01")
    parser.add_argument("--study-id", default="p8-s02-f01-nvda-real-v1")
    args = parser.parse_args(argv)

    work_dir = args.work_dir.resolve()
    work_dir.mkdir(parents=True, exist_ok=True)
    data_dir = work_dir / "data"
    exported = export_audited_nvda_dataset(
        r04b_snapshot=args.r04b_snapshot,
        cache_dir=args.cache_dir,
        dest_dir=data_dir,
        allow_network=False,
    )

    cfg_dir = args.frozen_config_out.resolve().parent
    cfg_dir.mkdir(parents=True, exist_ok=True)
    # Build config relative to work_dir for immediate execution AND relative to repo for frozen artifact.
    windows = {
        "train": ["2026-05-14", "2026-07-13"],
        "validation": ["2026-07-14", "2026-09-11"],
        "test": ["2026-09-12", "2026-11-22"],
    }
    strat_abs = (REPO_ROOT / "src/factorlab/strategies/binance_usdm").resolve()

    run_config = build_frozen_binance_config(
        study_id=args.study_id,
        bars_rel=str(Path(exported["bars_path"])),
        bars_sha256=exported["bars_sha256"],
        funding_rel=str(Path(exported["funding_path"])),
        funding_sha256=exported["funding_sha256"],
        strategy_dir_rel=str(strat_abs),
        strategy_dir_abs=strat_abs,
        windows=windows,
    )
    run_cfg_path = work_dir / "run-config.json"
    write_json(run_cfg_path, run_config)

    # Frozen repository config uses repo-relative strategy paths and logical dataset filenames + pinned SHA256.
    frozen_repo_cfg = build_frozen_binance_config(
        study_id=args.study_id,
        bars_rel="data/nvdausdt_1h_audited.parquet",
        bars_sha256=exported["bars_sha256"],
        funding_rel="data/nvdausdt_funding_audited.json",
        funding_sha256=exported["funding_sha256"],
        strategy_dir_rel="../../src/factorlab/strategies/binance_usdm",
        strategy_dir_abs=strat_abs,
        windows=windows,
    )
    write_json(args.frozen_config_out, frozen_repo_cfg)

    run_dir = work_dir / "run"
    tracker_dir = work_dir / "tracker"
    state_path = work_dir / "champion-state.json"
    selection = run(
        run_cfg_path,
        output=run_dir,
        store=tracker_dir,
        state_path=state_path,
        command=[sys.executable, *sys.argv],
    )
    verification = verify_replay_and_s03(
        run_dir=run_dir,
        state_path=state_path,
        audit_dir=work_dir / "audit-replay",
        s03_dir=work_dir / "s03-verify",
    )

    ev_dir = args.evidence_dir.resolve()
    ev_dir.mkdir(parents=True, exist_ok=True)
    for name in (
        "selection.json",
        "candidates.json",
        "frozen-search.json",
        "paired_statistics.json",
        "risk_stress.json",
        "package_mapping.json",
    ):
        write_json(ev_dir / name, read_json(run_dir / name))
    write_json(ev_dir / "replay_and_s03_verification.json", verification)
    write_json(ev_dir / "data_audit.json", exported["audit"])

    print(
        json.dumps(
            {
                "study_id": selection["study_id"],
                "verdict": selection["verdict"],
                "promoted": selection["promoted"],
                "reason": selection["reason"],
                "selected": selection["selected"]["candidate_id"],
                "oos_eligibility": selection["oos_eligibility"],
                "replay_passed": verification["all_replay_checks_passed"],
                "s03_cycle_results": verification["s03_cycle_results"],
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
