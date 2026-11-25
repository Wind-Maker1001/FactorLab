"""Binance USD-M NVDAUSDT 1x LIMIT long-only automatic research engine.

Reuses FactorLab's existing Paper execution authority (PaperBroker,
TradingController, PaperStore, execution_candidate, GridExecution), R09
complete offline F11/F08 lifecycle guardrails, R10 causal funding settlement
tie rule (fill_time < funding_time), S01 decision protocol, S02 Champion
Selector, ExperimentTracker, and S03 StrategyPackage / release adapter.
"""
from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_EVEN
import hashlib
import importlib.util
from io import BytesIO
import json
import math
import os
from pathlib import Path
import re
from typing import Any

import numpy as np
import pandas as pd

from factorlab.dataset.identity import file_hash, json_hash
from factorlab.forward.integrity import workflow_lock
from factorlab.experiments.grid_f11_f08_offline import (
    DEFAULT_PAPER_FEE_RATE,
    DEFAULT_PAPER_SLIPPAGE,
    MODEL_BASELINE,
    MODEL_F11_F08,
    USDT_QUANTUM,
    DemoGuardrails,
    ExecutionAssumptions,
    _CampaignPaperBroker,
    _check_percent_price,
    _plan_offline_limit_exit,
    _synthetic_book,
    execution_profile,
)
from factorlab.experiments.grid_funding import (
    cashflow as funding_cashflow,
    position_settlements,
)
from factorlab.experiments.grid_funding_adaptive import (
    DEMO_OBSERVED_HOURLY_FUNDING_BPS,
    FundingSensitiveGrid,
    NORMAL_PERP_HOURLY_FUNDING_BPS,
    VolatilityAdaptiveGrid,
    adaptive_half_width,
    causal_realized_vol_1h,
    make_grid_config,
    parse_utc,
    synthetic_hourly_funding_events,
    validate_hourly_timeline,
)
from factorlab.research.champion import (
    OOS_ELIGIBILITY_VALUES,
    SelectionPolicy,
    objective,
)
from factorlab.research.monte_carlo import _loss_tail
from factorlab.strategies.grid_execution import GridExecution, GridRules, project_paper_account
from factorlab.trading.contracts import (
    FundingEvent,
    Market,
    OrderEvent,
    OrderIntent,
    OrderSnapshot,
    OrderStatus,
    OrderType,
    Side,
    TERMINAL,
    ZERO,
    funding_event_id,
)
from factorlab.trading.control import RiskLimits, RiskRejected, TradingController
from factorlab.trading.execution import execution_candidate
from factorlab.trading.paper import InsufficientCollateral, Instrument, Quote
from factorlab.trading.storage import PaperStore
from factorlab.trading.target import target_quantity

D = Decimal
BINANCE_EXECUTION = "binance_usdm_demo_nvda_limit_long_only"
FUNDING_TIE_RULE = "settlement_before_exact_tie_fill"
OOS_REGISTRY_SCHEMA = "FACTORLAB_OOS_CONSUMPTION_REGISTRY_V1"
# Audited historical range already consumed by prior R04B/R07/R08/R09/R10/R11 studies.
# Any REAL_CANDIDATE test window whose start <= known_end can NEVER claim FRESH_UNCONSUMED_OOS.
KNOWN_CONSUMED_HISTORY_END_UTC: dict[tuple[str, str], str] = {
    ("BINANCE_USDM", "NVDAUSDT"): "2026-11-22",
}
KNOWN_PRIOR_STUDIES: dict[tuple[str, str], list[str]] = {
    ("BINANCE_USDM", "NVDAUSDT"): ["P7-R04B", "P7-R09", "P8-R07", "P8-R08", "P8-R09", "P8-R10"],
}


class CollateralDepletedError(ValueError):
    """Raised ONLY when authoritative PaperBroker ledger proves equity <= 0."""

    def __init__(self, message: str, *, final_equity: float, halt_reason: str | None = None):
        super().__init__(message)
        self.final_equity = float(final_equity)
        self.halt_reason = halt_reason


SUPPORTED_LIFECYCLES = {MODEL_F11_F08, MODEL_BASELINE}
SUPPORTED_EXIT_PROFILES = {
    "IDEAL_LIMIT_FILL",
    "DELAYED_EXIT_FILL",
    "LIMITED_CAPACITY_PARTIAL",
    "UNFILLED_EXIT_TIMEOUT",
}
REQUIRED_COMMON_KEYS = {
    "symbol",
    "execution",
    "market",
    "interval",
    "position_mode",
    "order_type",
    "leverage",
    "initial_cash",
    "max_weight",
    "demo_live_cap_usdt",
    "fee_bps",
    "slippage_bps",
    "participation",
    "funding_mode",
    "funding_tie_rule",
    "execution_lifecycle",
    "exit_profile",
    "limit_buffer_bps",
    "rules",
    "guardrails",
    "price_basis",
    "rule_boundary_note",
}
REQUIRED_RULES_KEYS = {
    "min_notional",
    "step_size",
    "tick_size",
    "min_quantity",
    "quantity_unit",
    "max_quantity",
}
REQUIRED_GUARDRAILS_KEYS = {
    "max_open_orders",
    "percent_price_down",
    "percent_price_up",
    "exit_reprice_seconds",
    "exit_max_attempts",
    "max_loss_ratio",
    "total_orders",
    "total_notional",
    "window_orders",
    "window_seconds",
}


def _floor_step(value: Decimal, step: Decimal) -> Decimal:
    return (value / step).to_integral_value(rounding=ROUND_FLOOR) * step


def _ceil_step(value: Decimal, step: Decimal) -> Decimal:
    return (value / step).to_integral_value(rounding=ROUND_CEILING) * step


def validate_binance_config(config: dict[str, Any]) -> SelectionPolicy:
    """Validate Stage-1 Binance USD-M NVDAUSDT 1x LIMIT long-only research contract."""
    policy = SelectionPolicy(**config["policy"])
    common = config["common"]
    if not isinstance(common, dict) or set(common) != REQUIRED_COMMON_KEYS:
        missing = sorted(REQUIRED_COMMON_KEYS - set(common or {}))
        extra = sorted(set(common or {}) - REQUIRED_COMMON_KEYS)
        raise ValueError(f"invalid Binance research contract keys (missing={missing}, extra={extra})")
    if common["execution"] != BINANCE_EXECUTION:
        raise ValueError("unsupported Binance execution contract")
    if common["symbol"] != "NVDAUSDT":
        raise ValueError("Stage-1 Binance research is strictly scoped to NVDAUSDT")
    if common["market"] != "BINANCE_USDM":
        raise ValueError("Stage-1 Binance research requires BINANCE_USDM market")
    if common["interval"] != "1h":
        raise ValueError("Stage-1 Binance research requires 1h UTC bars")
    if common["position_mode"] != "ONE_WAY_LONG_ONLY":
        raise ValueError("Stage-1 Binance research requires ONE_WAY_LONG_ONLY position mode")
    if common["order_type"] != "LIMIT":
        raise ValueError("Stage-1 Binance research requires LIMIT orders only")
    if type(common["leverage"]) is not int or common["leverage"] != 1:
        raise ValueError("Stage-1 Binance research requires leverage == 1 (no leverage)")
    if common["funding_tie_rule"] != FUNDING_TIE_RULE:
        raise ValueError("funding_tie_rule must be settlement_before_exact_tie_fill (R10 rule)")
    if common["funding_mode"] not in {"historical_events", "constant_hourly_bps", "regime_switch"}:
        raise ValueError("unsupported funding_mode")
    if common["execution_lifecycle"] not in SUPPORTED_LIFECYCLES:
        raise ValueError("unsupported execution_lifecycle")
    if common["exit_profile"] not in SUPPORTED_EXIT_PROFILES:
        raise ValueError("unsupported exit_profile")

    for name in ("initial_cash", "demo_live_cap_usdt"):
        v = common[name]
        if type(v) not in (int, float) or not math.isfinite(v) or v <= 0:
            raise ValueError(f"{name} must be finite and positive")
    for name in ("fee_bps", "slippage_bps", "limit_buffer_bps"):
        v = common[name]
        if type(v) not in (int, float) or not math.isfinite(v) or v < 0:
            raise ValueError(f"{name} must be finite and nonnegative")
    if common["limit_buffer_bps"] > 300:
        raise ValueError("limit_buffer_bps exceeds 3% PERCENT_PRICE safety band")
    if not (0 < float(common["max_weight"]) <= 1.0):
        raise ValueError("max_weight must be within (0, 1]")
    if not (0 < float(common["participation"]) <= 1.0):
        raise ValueError("participation must be within (0, 1]")
    if not isinstance(common["price_basis"], str) or not common["price_basis"].strip():
        raise ValueError("explicit price_basis required")
    if not isinstance(common["rule_boundary_note"], str) or not common["rule_boundary_note"].strip():
        raise ValueError("explicit rule_boundary_note required")

    rules = common["rules"]
    if not isinstance(rules, dict) or set(rules) != REQUIRED_RULES_KEYS:
        raise ValueError("explicit Binance exchange rules required")
    for k, v in rules.items():
        dec = D(str(v))
        if not dec.is_finite() or dec <= ZERO:
            raise ValueError(f"exchange rule {k} must be positive Decimal")

    g = common["guardrails"]
    if not isinstance(g, dict) or set(g) != REQUIRED_GUARDRAILS_KEYS:
        raise ValueError("explicit Binance Demo guardrails required")
    if type(g["max_open_orders"]) is not int or g["max_open_orders"] < 1:
        raise ValueError("max_open_orders must be >= 1")
    if not (0.5 <= float(g["percent_price_down"]) < 1.0 < float(g["percent_price_up"]) <= 1.5):
        raise ValueError("invalid percent_price band")
    if type(g["exit_reprice_seconds"]) is not int or g["exit_reprice_seconds"] < 1:
        raise ValueError("exit_reprice_seconds must be >= 1")
    if type(g["exit_max_attempts"]) is not int or not (1 <= g["exit_max_attempts"] <= 20):
        raise ValueError("exit_max_attempts must be in [1, 20]")
    if not (0.0 < float(g["max_loss_ratio"]) <= 1.0):
        raise ValueError("max_loss_ratio must be in (0, 1]")
    if type(g["total_orders"]) is not int or g["total_orders"] < 1:
        raise ValueError("total_orders must be >= 1")
    if float(g["total_notional"]) <= 0:
        raise ValueError("total_notional must be positive")
    if type(g["window_orders"]) is not int or g["window_orders"] < 1:
        raise ValueError("window_orders must be >= 1")
    if type(g["window_seconds"]) is not int or g["window_seconds"] < 1:
        raise ValueError("window_seconds must be >= 1")

    data = config["data"]
    allowed_data_keys = {"bars", "sha256", "funding", "funding_sha256", "source_kind", "oos_eligibility", "prior_studies"}
    required_data_keys = {"bars", "sha256", "funding", "funding_sha256", "source_kind", "oos_eligibility"}
    if not isinstance(data, dict) or not required_data_keys <= set(data) or not set(data) <= allowed_data_keys:
        raise ValueError("pinned bars, funding, checksums, source_kind and oos_eligibility required")
    if data["source_kind"] not in {"SYNTHETIC", "REAL_CANDIDATE"}:
        raise ValueError("invalid source_kind")
    if data["oos_eligibility"] not in OOS_ELIGIBILITY_VALUES:
        raise ValueError("invalid oos_eligibility")
    if data["oos_eligibility"] == "HISTORICAL_REUSE_PSEUDO_OOS":
        prior = data.get("prior_studies")
        if not isinstance(prior, list) or not prior or not all(isinstance(x, str) and x for x in prior):
            raise ValueError("HISTORICAL_REUSE_PSEUDO_OOS requires non-empty prior_studies list")
    for hash_key in ("sha256", "funding_sha256"):
        if not isinstance(data[hash_key], str) or not re.fullmatch(r"[a-f0-9]{64}", data[hash_key]):
            raise ValueError(f"explicit 64-hex {hash_key} required")
    return policy


def _load_class(file_path: Path, class_name: str, expected_sha256: str) -> Any:
    raw = file_path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise ValueError(f"strategy code checksum mismatch for {file_path.name}")
    module_name = f"_factorlab_binance_candidate_{expected_sha256[:16]}"
    spec = importlib.util.spec_from_file_location(module_name, str(file_path))
    if spec is None or spec.loader is None:
        raise ValueError(f"cannot load strategy module from {file_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not hasattr(module, class_name):
        raise ValueError(f"strategy class {class_name} not found in {file_path.name}")
    return getattr(module, class_name)


def register_binance_candidate(
    item: dict[str, Any],
    base: Path,
    common: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Verify code hash, instantiate candidate, normalize parameters, and return identity + compatibility."""
    spec = dict(item["strategy"], parameters=dict(item["parameters"]))
    file_path = (base / spec["file"]).resolve()
    if not re.fullmatch(r"[a-f0-9]{64}", str(spec.get("sha256", ""))):
        raise ValueError("explicit strategy SHA256 code version required")
    cls = _load_class(file_path, spec["class"], spec["sha256"])
    instance = cls(**spec["parameters"])
    if not hasattr(instance, "describe") or not hasattr(instance, "normalized_parameters"):
        raise ValueError("Binance candidate must implement describe() and normalized_parameters()")
    proto_spec = instance.describe()
    if common["symbol"] not in proto_spec.symbols or 3600 not in proto_spec.intervals:
        raise ValueError(f"candidate {item['strategy_id']} incompatible with NVDAUSDT 1h contract")
    normalized_params = json.loads(json.dumps(instance.normalized_parameters(), allow_nan=False))
    spec["file"] = str(file_path)
    spec["parameters"] = normalized_params
    identity = dict(
        strategy_id=item["strategy_id"],
        code_version=spec["sha256"],
        class_name=spec["class"],
        parameters=normalized_params,
    )
    candidate = dict(identity, candidate_id="c-" + json_hash(identity)[:24], strategy=spec)
    compatibility = dict(
        strategy_family=getattr(instance, "strategy_family", "unknown"),
        decision_kind=proto_spec.decision_kind,
        spec=asdict(proto_spec),
        s03_compatible=bool(getattr(instance, "s03_compatible", False)),
        s03_protocol=getattr(instance, "s03_protocol", None),
        s03_entrypoint=getattr(instance, "s03_entrypoint", None),
        s04_requirements=getattr(instance, "s04_requirements", None),
        source_filename=file_path.name,
    )
    return candidate, compatibility


def validate_funding_coverage(
    bars: list[dict[str, Any]],
    funding_events: list[tuple[datetime, Decimal, Decimal, str]],
    *,
    max_boundary_lag_hours: float = 8.05,
    max_internal_gap_hours: float = 24.0,
) -> dict[str, Any]:
    """Fail closed if funding events do not cover the full exported bar timeline.

    Distinguishes observed 0.0 funding-rate events (present in the stream) from
    missing funding cache files or truncated tail/internal segments.
    """
    if not bars:
        raise ValueError("cannot validate funding coverage on empty bars")
    bar_start = parse_utc(bars[0]["time"])
    bar_end_excl = parse_utc(bars[-1]["time"]) + timedelta(hours=1)
    in_range = sorted([ev for ev in funding_events if bar_start <= ev[0] < bar_end_excl], key=lambda e: e[0])
    if not in_range:
        raise ValueError(
            f"incomplete funding coverage: zero funding events in [{bar_start.isoformat()}, {bar_end_excl.isoformat()})"
        )
    first_ts = in_range[0][0]
    last_ts = in_range[-1][0]
    start_lag_h = (first_ts - bar_start).total_seconds() / 3600.0
    end_lag_h = (bar_end_excl - last_ts).total_seconds() / 3600.0
    if start_lag_h > max_boundary_lag_hours:
        raise ValueError(
            f"incomplete funding coverage at start: first funding {first_ts.isoformat()} lags bar start "
            f"{bar_start.isoformat()} by {start_lag_h:.2f}h (> {max_boundary_lag_hours}h)"
        )
    if end_lag_h > max_boundary_lag_hours:
        raise ValueError(
            f"incomplete funding coverage at tail: last funding {last_ts.isoformat()} ends {end_lag_h:.2f}h "
            f"before bar end {bar_end_excl.isoformat()} (> {max_boundary_lag_hours}h)"
        )
    max_gap_h = 0.0
    for prev_ev, curr_ev in zip(in_range, in_range[1:]):
        gap_h = (curr_ev[0] - prev_ev[0]).total_seconds() / 3600.0
        if gap_h > max_gap_h:
            max_gap_h = gap_h
    if max_gap_h > max_internal_gap_hours:
        raise ValueError(
            f"incomplete funding coverage: internal gap of {max_gap_h:.2f}h exceeds {max_internal_gap_hours}h limit"
        )
    zero_rate_events = sum(1 for ev in in_range if ev[2] == ZERO)
    return {
        "complete": True,
        "bar_start_utc": bar_start.isoformat(),
        "bar_end_excl_utc": bar_end_excl.isoformat(),
        "funding_events_in_range": len(in_range),
        "funding_first_utc": first_ts.isoformat(),
        "funding_last_utc": last_ts.isoformat(),
        "start_lag_hours": round(start_lag_h, 4),
        "end_lag_hours": round(end_lag_h, 4),
        "max_internal_gap_hours": round(max_gap_h, 4),
        "zero_rate_events": zero_rate_events,
    }


def _atomic_write_json(path: Path, payload: Any) -> None:
    import tempfile

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, sort_keys=True, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def evaluate_and_reserve_oos_freshness(
    *,
    market: str,
    symbol: str,
    test_window: list[str],
    source_kind: str,
    declared_oos_eligibility: str,
    declared_prior_studies: list[str],
    study_id: str,
    bars_sha256: str,
    funding_sha256: str,
    registry_path: Path | None,
    audit: bool = False,
) -> tuple[str, list[str], dict[str, Any]]:
    """Verify OOS freshness against known history and cross-study consumption registry.

    Rules:
    1. REAL_CANDIDATE overlapping KNOWN_CONSUMED_HISTORY_END_UTC can NEVER claim FRESH_UNCONSUMED_OOS.
    2. REAL_CANDIDATE overlapping any previously consumed [test_start, test_end] in the shared registry
       (even partial overlap, new state_path, new study_id, or re-exported files) is downgraded to
       HISTORICAL_REUSE_PSEUDO_OOS.
    3. Non-audit REAL_CANDIDATE runs atomically record a TEST_RESERVED entry BEFORE test execution.
    4. audit=True (independent replay) NEVER mutates the consumption registry.
    """
    test_start, test_end = str(test_window[0]), str(test_window[1])
    ts_start, ts_end = pd.Timestamp(test_start), pd.Timestamp(test_end)
    effective = declared_oos_eligibility
    prior_studies = list(declared_prior_studies)
    blocked_reasons: list[str] = []
    overlapping_entries: list[dict[str, Any]] = []

    if source_kind == "REAL_CANDIDATE":
        known_end = KNOWN_CONSUMED_HISTORY_END_UTC.get((market, symbol))
        if known_end is not None and ts_start <= pd.Timestamp(known_end):
            if effective == "FRESH_UNCONSUMED_OOS":
                effective = "HISTORICAL_REUSE_PSEUDO_OOS"
            blocked_reasons.append(f"overlaps_known_consumed_history_through_{known_end}")
            for s in KNOWN_PRIOR_STUDIES.get((market, symbol), []):
                if s not in prior_studies:
                    prior_studies.append(s)

    registry_recorded = False
    if not audit and source_kind == "REAL_CANDIDATE" and registry_path is not None:
        reg_path = Path(registry_path).resolve()
        reg_path.parent.mkdir(parents=True, exist_ok=True)
        with workflow_lock(reg_path.with_suffix(reg_path.suffix + ".lock")):
            if reg_path.exists():
                reg = json.loads(reg_path.read_text(encoding="utf-8"))
                if reg.get("schema") != OOS_REGISTRY_SCHEMA or not isinstance(reg.get("entries"), list):
                    raise ValueError("corrupt cross-study OOS consumption registry")
            else:
                reg = {"schema": OOS_REGISTRY_SCHEMA, "entries": []}

            for entry in reg["entries"]:
                if entry.get("market") != market or entry.get("symbol") != symbol:
                    continue
                if entry.get("source_kind") != "REAL_CANDIDATE":
                    continue
                e_start = pd.Timestamp(entry["test_start"])
                e_end = pd.Timestamp(entry["test_end"])
                if e_start <= ts_end and ts_start <= e_end:
                    overlapping_entries.append(
                        {
                            "study_id": entry["study_id"],
                            "test_start": entry["test_start"],
                            "test_end": entry["test_end"],
                            "status": entry.get("status"),
                        }
                    )
            if overlapping_entries:
                if effective == "FRESH_UNCONSUMED_OOS":
                    effective = "HISTORICAL_REUSE_PSEUDO_OOS"
                blocked_reasons.append("overlaps_prior_consumed_oos_registry_entry")
                for ov in overlapping_entries:
                    if ov["study_id"] not in prior_studies:
                        prior_studies.append(ov["study_id"])

            reg["entries"].append(
                {
                    "market": market,
                    "symbol": symbol,
                    "test_start": test_start,
                    "test_end": test_end,
                    "study_id": study_id,
                    "source_kind": source_kind,
                    "status": "TEST_RESERVED",
                    "declared_oos_eligibility": declared_oos_eligibility,
                    "effective_oos_eligibility": effective,
                    "bars_sha256": bars_sha256,
                    "funding_sha256": funding_sha256,
                    "reserved_at_utc": datetime.now(timezone.utc).isoformat(),
                }
            )
            _atomic_write_json(reg_path, reg)
            registry_recorded = True

    audit_info = {
        "declared_oos_eligibility": declared_oos_eligibility,
        "effective_oos_eligibility": effective,
        "downgraded": effective != declared_oos_eligibility,
        "blocked_reasons": blocked_reasons,
        "known_consumed_through": KNOWN_CONSUMED_HISTORY_END_UTC.get((market, symbol)) if source_kind == "REAL_CANDIDATE" else None,
        "overlapping_registry_entries": overlapping_entries,
        "registry_path": str(Path(registry_path).resolve()) if (registry_path is not None and not audit) else None,
        "registry_recorded": registry_recorded,
        "audit_mode": bool(audit),
    }
    return effective, prior_studies, audit_info


def complete_oos_registry_entry(
    *,
    registry_path: Path | None,
    study_id: str,
    market: str,
    symbol: str,
    test_window: list[str],
    promoted: bool,
    verdict: str,
    audit: bool = False,
) -> None:
    """Mark the reserved OOS registry entry as COMPLETED without deleting history."""
    if audit or registry_path is None:
        return
    reg_path = Path(registry_path).resolve()
    if not reg_path.exists():
        return
    with workflow_lock(reg_path.with_suffix(reg_path.suffix + ".lock")):
        reg = json.loads(reg_path.read_text(encoding="utf-8"))
        for entry in reversed(reg.get("entries", [])):
            if (
                entry.get("study_id") == study_id
                and entry.get("market") == market
                and entry.get("symbol") == symbol
                and entry.get("test_start") == str(test_window[0])
                and entry.get("test_end") == str(test_window[1])
                and entry.get("status") == "TEST_RESERVED"
            ):
                entry["status"] = "COMPLETED"
                entry["promoted"] = bool(promoted)
                entry["verdict"] = str(verdict)
                entry["completed_at_utc"] = datetime.now(timezone.utc).isoformat()
                break
        _atomic_write_json(reg_path, reg)


def load_binance_bars_and_funding(
    bars_payload: bytes,
    funding_payload: bytes,
    *,
    symbol: str,
) -> tuple[list[dict[str, Any]], list[tuple[datetime, Decimal, Decimal, str]]]:
    """Load and strictly validate pinned hourly UTC bars parquet and funding JSON."""
    df = pd.read_parquet(BytesIO(bars_payload))
    required_cols = {"open_time", "open", "high", "low", "close", "volume"}
    if not required_cols <= set(df.columns):
        raise ValueError(f"Binance 1h parquet missing required columns: {sorted(required_cols - set(df.columns))}")
    if "symbol" in df.columns and not df["symbol"].eq(symbol).all():
        raise ValueError(f"Binance parquet contains symbols other than {symbol}")
    times = validate_hourly_timeline(df["open_time"])
    bars: list[dict[str, Any]] = []
    for idx, (ts, row) in enumerate(zip(times, df.itertuples(index=False))):
        o, h, l, c, v = float(row.open), float(row.high), float(row.low), float(row.close), float(row.volume)
        if not all(math.isfinite(x) for x in (o, h, l, c, v)) or not (0 < l <= min(o, c) <= max(o, c) <= h) or v < 0:
            raise ValueError(f"invalid OHLCV bar at {ts.isoformat()}")
        bars.append(
            {
                "id": f"hour-{idx}",
                "time": ts.isoformat(),
                "open": o,
                "high": h,
                "low": l,
                "close": c,
                "price": c,
                "volume": v,
                "completed_bar": True,
            }
        )

    raw_funding = json.loads(funding_payload.decode("utf-8"))
    if not isinstance(raw_funding, list):
        raise ValueError("funding JSON must be a list of settlement events")
    events: list[tuple[datetime, Decimal, Decimal, str]] = []
    seen: set[tuple[datetime, str]] = set()
    for row in raw_funding:
        if "symbol" in row and row["symbol"] != symbol:
            raise ValueError("funding event symbol mismatch")
        if "fundingTime" in row:
            t_utc = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(milliseconds=int(row["fundingTime"]))
            mark = D(str(row["markPrice"]))
            rate = D(str(row["fundingRate"]))
            kind = str(row.get("rateType", "Regular"))
        else:
            t_utc = parse_utc(row["time"])
            mark = D(str(row["mark_price"]))
            rate = D(str(row["rate"]))
            kind = str(row.get("rate_type", "Regular"))
        if not mark.is_finite() or mark <= ZERO or not rate.is_finite():
            raise ValueError("invalid funding settlement mark price or rate")
        key = (t_utc, kind)
        if key in seen:
            raise ValueError("duplicate funding settlement event")
        seen.add(key)
        events.append((t_utc, mark, rate, kind))
    events.sort(key=lambda e: e[0])
    return bars, events


def slice_stage_bars(
    all_bars: list[dict[str, Any]],
    start_date: str,
    end_date: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return (warmup_bars_before_start, stage_bars_inclusive_utc_dates)."""
    start_ts = pd.Timestamp(start_date, tz="UTC")
    end_excl = pd.Timestamp(end_date, tz="UTC") + pd.Timedelta(days=1)
    warmup = []
    stage = []
    for b in all_bars:
        t = pd.Timestamp(b["time"])
        if t < start_ts:
            warmup.append(b)
        elif start_ts <= t < end_excl:
            stage.append(b)
    if len(stage) < 48:
        raise ValueError(f"window [{start_date}, {end_date}] has insufficient hourly observations ({len(stage)})")
    # Ensure boundary dates exist as complete UTC days
    first_day = pd.Timestamp(stage[0]["time"]).tz_convert("UTC").normalize()
    last_day = pd.Timestamp(stage[-1]["time"]).tz_convert("UTC").normalize()
    if first_day != start_ts or last_day != pd.Timestamp(end_date, tz="UTC"):
        raise ValueError(f"window boundaries [{start_date}, {end_date}] not fully observed in hourly bars")
    return warmup, stage


def _fit_reference_vol_train_only(train_bars: list[dict[str, Any]]) -> float:
    closes = np.asarray([float(b["close"]) for b in train_bars], dtype=float)
    if len(closes) < 3:
        raise ValueError("insufficient training bars to estimate reference volatility")
    rets = np.diff(np.log(closes))
    vol = float(np.std(rets, ddof=1))
    if not math.isfinite(vol) or vol <= 0:
        raise ValueError("degenerate training volatility")
    return vol


def _build_grid_rules(common: dict[str, Any]) -> GridRules:
    r = common["rules"]
    return GridRules(
        quantity_unit=D(str(r["quantity_unit"])),
        step_size=D(str(r["step_size"])),
        tick_size=D(str(r["tick_size"])),
        min_quantity=D(str(r["min_quantity"])),
        min_notional=D(str(r["min_notional"])),
    )


def _build_guardrails(common: dict[str, Any]) -> DemoGuardrails:
    r = common["rules"]
    g = common["guardrails"]
    cap = D(str(common["initial_cash"]))
    max_loss = (cap * D(str(g["max_loss_ratio"]))).quantize(USDT_QUANTUM, rounding=ROUND_HALF_EVEN)
    enable_f11 = common["execution_lifecycle"] == MODEL_F11_F08
    enable_f08 = common["execution_lifecycle"] == MODEL_F11_F08
    return DemoGuardrails(
        min_notional=D(str(r["min_notional"])),
        max_notional=cap,
        max_quantity=D(str(r["max_quantity"])),
        max_open_orders=int(g["max_open_orders"]),
        percent_price_down=D(str(g["percent_price_down"])),
        percent_price_up=D(str(g["percent_price_up"])),
        exit_reprice_seconds=int(g["exit_reprice_seconds"]),
        exit_max_attempts=int(g["exit_max_attempts"]),
        total_orders=int(g["total_orders"]),
        total_notional=D(str(g["total_notional"])),
        window_orders=int(g["window_orders"]),
        window_seconds=int(g["window_seconds"]),
        max_loss=max_loss,
        enable_f11=enable_f11,
        enable_f08=enable_f08,
        enforce_min_notional_on_exit=True,
        enforce_max_notional_on_exit=True,
        enforce_percent_price=True,
    )


def _build_assumptions(common: dict[str, Any]) -> ExecutionAssumptions:
    base = execution_profile(common["exit_profile"], use_bar_volume=True)
    fee_rate = D(str(common["fee_bps"])) / D("10000")
    slippage = D(str(common["slippage_bps"])) / D("10000")
    participation = D(str(common["participation"]))
    return ExecutionAssumptions(
        profile_name=base.profile_name,
        assumption_summary=base.assumption_summary,
        evidence_label=base.evidence_label,
        fee_rate=fee_rate,
        slippage=slippage,
        half_spread_bps=base.half_spread_bps,
        capacity_per_bar=base.capacity_per_bar,
        use_bar_volume=base.use_bar_volume,
        participation=participation,
        exit_marketable_at_submit=base.exit_marketable_at_submit,
        exit_fill_delay_bars=base.exit_fill_delay_bars,
        exit_cannot_fill=base.exit_cannot_fill,
        gap_band_check=base.gap_band_check,
    )


def _resolve_funding_events(
    bars: list[dict[str, Any]],
    historical_events: list[tuple[datetime, Decimal, Decimal, str]],
    *,
    funding_override: dict[str, Any] | None = None,
) -> list[tuple[datetime, Decimal, Decimal, str]]:
    if funding_override:
        mode = funding_override.get("mode", "historical_events")
        if mode == "constant_hourly_bps":
            return synthetic_hourly_funding_events(
                [b["time"] for b in bars],
                [b["close"] for b in bars],
                constant_hourly_bps=float(funding_override["hourly_bps"]),
            )
        if mode == "regime_switch":
            return synthetic_hourly_funding_events(
                [b["time"] for b in bars],
                [b["close"] for b in bars],
                regime_switch_bps=(
                    int(funding_override["switch_hour"]),
                    float(funding_override["pre_bps_h"]),
                    float(funding_override["post_bps_h"]),
                ),
            )
        if mode == "scaled_historical":
            scale = D(str(funding_override["scale"]))
            return [(t, px, r * scale, k) for t, px, r, k in historical_events]
    return list(historical_events)


def _holding_durations_from_trades(
    trades: list[dict[str, Any]],
    window_end: datetime,
) -> tuple[float, float]:
    """FIFO quantity-weighted mean and max holding hours across closed & residual lots."""
    if not trades:
        return 0.0, 0.0
    lots: list[list[Any]] = []  # [remaining_qty, buy_time]
    fragments: list[tuple[float, float]] = []
    for t in trades:
        qty = float(t["executed_shares"])
        ts = parse_utc(t["event_time"])
        if t["side"] == "BUY":
            lots.append([qty, ts])
        else:
            rem = qty
            while rem > 1e-12 and lots:
                take = min(rem, lots[0][0])
                hours = max(0.0, (ts - lots[0][1]).total_seconds() / 3600.0)
                fragments.append((take, hours))
                lots[0][0] -= take
                rem -= take
                if lots[0][0] <= 1e-12:
                    lots.pop(0)
    cutoff = window_end - timedelta(microseconds=1)
    for rem, buy_ts in lots:
        if rem > 1e-12:
            hours = max(0.0, (cutoff - buy_ts).total_seconds() / 3600.0)
            fragments.append((rem, hours))
    if not fragments:
        return 0.0, 0.0
    total_q = sum(q for q, _ in fragments)
    mean_h = sum(q * h for q, h in fragments) / total_q if total_q > 0 else 0.0
    max_h = max(h for _, h in fragments)
    return mean_h, max_h


def _read_usdm_account(broker: _CampaignPaperBroker, symbol: str) -> dict[str, Decimal]:
    """Read authoritative PaperBroker USD-M account (equity = wallet + unrealized MTM)."""
    snap = broker.read_account()
    asset = next(a for a in snap.assets if a.currency == "USDT")
    pos = broker.state.positions.get(symbol)
    inventory = pos.quantity if pos is not None else ZERO
    unrealized = inventory * (pos.mark_price - pos.entry_price) if pos is not None else ZERO
    equity = asset.total + unrealized
    raw_available = asset.available
    return {
        "equity": equity,
        "inventory": inventory,
        "unrealized": unrealized,
        "committed": asset.reserved,
        "available": max(ZERO, raw_available),
        "raw_available": raw_available,
    }


def _compute_financial_metrics(
    hourly_df: pd.DataFrame,
    trades: list[dict[str, Any]],
    telemetry: dict[str, Any],
    *,
    initial_cash: float,
    policy: SelectionPolicy,
    volume_demands: list[float],
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Build daily UTC net-return NAV and complete Section-6 financial metrics."""
    work = hourly_df.copy()
    work["ts"] = pd.to_datetime(work["time"], utc=True)
    work["utc_day"] = work["ts"].dt.tz_localize(None).dt.normalize()
    daily_close = work.groupby("utc_day", as_index=False).agg(
        equity=("equity", "last"),
        close=("close", "last"),
        inventory=("inventory", "last"),
        funding_cumulative=("funding_cumulative", "last"),
        fee=("fee_bar", "sum"),
        slippage=("slippage_bar", "sum"),
        notional=("notional_bar", "sum"),
    )
    daily_close = daily_close.rename(columns={"utc_day": "date"})
    eq = daily_close["equity"].to_numpy(dtype=float)
    hourly_eq_raw = work["equity"].to_numpy(dtype=float)
    if not np.isfinite(eq).all() or not np.isfinite(hourly_eq_raw).all():
        raise ValueError("nonfinite Binance equity series")
    if (eq <= 0).any() or (hourly_eq_raw <= 0).any():
        raise CollateralDepletedError(
            "authoritative PaperBroker ledger equity depleted (<= 0)",
            final_equity=float(hourly_eq_raw[-1]),
            halt_reason=telemetry.get("halt_reason"),
        )
    if len(eq) < 2:
        raise ValueError("insufficient Binance daily equity observations")
    origin_eq = np.r_[float(initial_cash), eq]
    daily_returns = origin_eq[1:] / origin_eq[:-1] - 1.0
    if not np.isfinite(daily_returns).all() or (daily_returns <= -1.0).any():
        raise CollateralDepletedError(
            "authoritative PaperBroker daily net return reached <= -100%",
            final_equity=float(eq[-1]),
            halt_reason=telemetry.get("halt_reason"),
        )
    daily_close["strategy_nav"] = eq / float(initial_cash)
    daily_close["strategy_return"] = daily_returns

    # Origin-inclusive hourly drawdown (strictly captures intradaily dips too)
    hourly_eq = np.r_[float(initial_cash), work["equity"].to_numpy(dtype=float)]
    hourly_peak = np.maximum.accumulate(hourly_eq)
    max_drawdown = float(np.max(np.clip(1.0 - hourly_eq / hourly_peak, 0.0, 1.0)))

    # Daily VaR95 / CVaR95 on loss distribution (-r)
    var95, cvar95 = _loss_tail(-daily_returns, policy.cvar_confidence)
    var95 = max(0.0, float(var95))
    cvar95 = max(0.0, float(cvar95))

    periods = policy.periods_per_year
    mean_daily = float(daily_returns.mean())
    std_daily = float(daily_returns.std(ddof=1)) if len(daily_returns) >= 2 else 0.0
    ann_mean = mean_daily * periods
    ann_vol = std_daily * math.sqrt(periods)
    sharpe = (ann_mean / ann_vol) if (len(daily_returns) >= 5 and ann_vol > 1e-12) else None

    downside = np.minimum(daily_returns, 0.0)
    downside_rms = float(np.sqrt(np.mean(downside ** 2))) * math.sqrt(periods)
    sortino = (ann_mean / downside_rms) if (len(daily_returns) >= 5 and downside_rms > 1e-12) else None

    score = objective(daily_returns, policy)
    net_return = float(eq[-1] / float(initial_cash) - 1.0)
    daily_loss_prob = float(np.mean(daily_returns < 0.0))

    max_participation = max(volume_demands, default=0.0)
    if max_participation > 0.0 and math.isfinite(max_participation):
        capacity_usdt = float(initial_cash * policy.participation_limit / max_participation)
    else:
        capacity_usdt = None

    submitted_orders = int(telemetry["submitted_orders"])
    filled_orders = int(telemetry["filled_orders"])
    fill_rate = (filled_orders / submitted_orders) if submitted_orders > 0 else 0.0
    traded_notional = float(sum(float(t["trade_notional"]) for t in trades))
    turnover_ratio = traded_notional / float(initial_cash)

    total_funding = float(telemetry["total_funding_usdt"])
    funding_erosion_pct = -total_funding / float(initial_cash)

    holding_hours = int(work["holding_flag"].sum())
    holding_time_fraction = float(holding_hours / max(1, len(work)))
    capital_utilization = float(work["net_exposure"].mean() / float(initial_cash))
    max_committed_ratio = float(work["committed"].max() / float(initial_cash))

    exit_lifecycle_count = int(telemetry["exit_lifecycle_count"])
    failed_exit_lifecycle_count = int(telemetry["failed_exit_lifecycle_count"])
    exit_order_attempt_count = int(telemetry["exit_order_attempt_count"])
    failed_exit_order_attempt_count = int(telemetry["failed_exit_order_attempt_count"])
    exit_status_poll_checks = int(telemetry["exit_status_poll_checks"])
    exit_attempts = exit_lifecycle_count
    exit_failures = failed_exit_lifecycle_count
    exit_failure_ratio = (failed_exit_lifecycle_count / exit_lifecycle_count) if exit_lifecycle_count > 0 else 0.0
    exit_order_failure_ratio = (
        failed_exit_order_attempt_count / exit_order_attempt_count if exit_order_attempt_count > 0 else 0.0
    )

    final_equity = float(eq[-1])
    total_fees = float(telemetry["total_fees_usdt"])
    total_slippage = float(telemetry["total_slippage_usdt"])
    residual_inventory = float(work["inventory"].iloc[-1])
    residual_notional = float(work["net_exposure"].iloc[-1])
    residual_mtm_pnl = float(work["unrealized"].iloc[-1])
    net_trade_pnl = final_equity - float(initial_cash) - total_funding
    gross_trade_pnl = net_trade_pnl + total_fees + total_slippage

    metrics = {
        "score": None if score is None or not math.isfinite(score) else float(score),
        "net_return": net_return,
        "annual_mean_net_return": ann_mean,
        "annual_volatility": ann_vol,
        "sharpe": sharpe,
        "sortino": sortino,
        "max_drawdown": max_drawdown,
        "var_95": var95,
        "daily_cvar": cvar95,
        "cvar_95": cvar95,
        "daily_loss_probability": daily_loss_prob,
        "period_net_loss": 1.0 if net_return < 0.0 else 0.0,
        "tail_observations": float((1.0 - policy.cvar_confidence) * len(daily_returns)),
        "days": int(len(daily_returns)),
        "hours": int(len(work)),
        "capacity_usdt": capacity_usdt,
        "max_participation": max_participation if math.isfinite(max_participation) else None,
        "submitted_orders": submitted_orders,
        "filled_orders": filled_orders,
        "partial_fill_events": int(telemetry["partial_fill_events"]),
        "canceled_orders": int(telemetry["canceled_orders"]),
        "unfilled_orders": int(telemetry["unfilled_orders"]),
        "fill_rate": float(fill_rate),
        "trade_count": int(len(trades)),
        "traded_notional_usdt": traded_notional,
        "turnover_ratio": float(turnover_ratio),
        "transaction_cost": float(total_fees + total_slippage),
        "fees_usdt": total_fees,
        "slippage_usdt": total_slippage,
        "gross_trade_pnl_usdt": float(gross_trade_pnl),
        "net_trade_pnl_usdt": float(net_trade_pnl),
        "funding_pnl_usdt": total_funding,
        "funding_erosion_pct": float(funding_erosion_pct),
        "holding_hours": holding_hours,
        "mean_holding_hours": float(telemetry["mean_holding_hours"]),
        "max_holding_hours": float(telemetry["max_holding_hours"]),
        "holding_time_fraction": holding_time_fraction,
        "capital_utilization": capital_utilization,
        "max_committed_ratio": max_committed_ratio,
        "residual_inventory": residual_inventory,
        "residual_notional_usdt": residual_notional,
        "residual_mtm_pnl_usdt": residual_mtm_pnl,
        "exit_attempts": exit_attempts,
        "exit_failures": exit_failures,
        "exit_failure_ratio": float(exit_failure_ratio),
        "exit_lifecycle_count": exit_lifecycle_count,
        "failed_exit_lifecycle_count": failed_exit_lifecycle_count,
        "exit_order_attempt_count": exit_order_attempt_count,
        "failed_exit_order_attempt_count": failed_exit_order_attempt_count,
        "exit_order_failure_ratio": float(exit_order_failure_ratio),
        "exit_status_poll_checks": exit_status_poll_checks,
        "final_equity_usdt": final_equity,
        "funding_reconciliation_residual": float(telemetry["funding_reconciliation_residual"]),
        "equity_reconciliation_residual": float(telemetry["equity_reconciliation_residual"]),
        "halted_flag": 1.0 if telemetry["halted"] else 0.0,
    }
    return daily_close, metrics


class _LimitOnlyFundingSensitiveGrid(FundingSensitiveGrid):
    """Enforce Binance LIMIT-only contract on FundingSensitiveGrid exit proposals."""

    def _propose(self, side, target, quantity, level=-1, market=False):
        if market and side == Side.SELL and self.state == "EXITING":
            # Convert R10 funding exit trigger into a legal reduce-only LIMIT order
            # (strictly respecting tick_size, min_notional, and LIMIT-only contract).
            if not self._can_propose():
                return None
            r, a, c = self.rules, self.account, self.config
            slip = D(str(c.slippage_bps)) / D("10000")
            price = _floor_step(self.price * (D("1") - slip), r.tick_size)
            qty = _floor_step(min(D(str(quantity)), a.inventory), r.step_size)
            if price < D(str(c.minimum_price)) or qty < r.min_quantity or qty * price < r.min_notional:
                return None
            next_order = self.issued_orders + 1
            identity = f"{self.deployment_id}-{next_order:08d}"
            intent = OrderIntent(
                self.market,
                self.instrument_id,
                Side.SELL,
                OrderType.LIMIT,
                qty,
                price,
                identity,
                position_side="BOTH",
                reduce_only=True,
            )
            self.issued_orders = next_order
            self.orders[identity] = OrderSnapshot(intent)
            self.targets[identity], self.levels[identity], self.created[identity] = price, -1, self.sequence
            self.exit_orders.append(identity)
            return intent
        return super()._propose(side, target, quantity, level, market)


def simulate_binance_candidate(
    candidate: dict[str, Any],
    *,
    source_path: Path,
    stage_bars: list[dict[str, Any]],
    warmup_bars: list[dict[str, Any]],
    train_bars: list[dict[str, Any]],
    funding_events: list[tuple[datetime, Decimal, Decimal, str]],
    common: dict[str, Any],
    policy: SelectionPolicy,
    funding_override: dict[str, Any] | None = None,
    account_id: str = "s02-binance",
    cancel_fill_race: bool = False,
) -> dict[str, Any]:
    """Execute one candidate over stage_bars using the authoritative PaperBroker + R09/R10 stack."""
    cls = _load_class(source_path, candidate["class_name"], candidate["code_version"])
    instance = cls(**candidate["parameters"])
    family = getattr(instance, "strategy_family", "unknown")

    symbol = common["symbol"]
    initial_cash = D(str(common["initial_cash"]))
    rules = _build_grid_rules(common)
    guardrails = _build_guardrails(common)
    assumptions = _build_assumptions(common)
    limit_buffer = D(str(common["limit_buffer_bps"])) / D("10000")

    window_start = parse_utc(stage_bars[0]["time"])
    window_end = parse_utc(stage_bars[-1]["time"]) + timedelta(hours=1)
    effective_funding = _resolve_funding_events(stage_bars, funding_events, funding_override=funding_override)
    window_funding = [ev for ev in effective_funding if window_start <= ev[0] < window_end]
    # Causal funding visible to Strategy B includes pre-window settled history up to bar close.
    if funding_override and funding_override.get("mode") in {"constant_hourly_bps", "regime_switch"}:
        init_bps = (
            float(funding_override["hourly_bps"])
            if funding_override["mode"] == "constant_hourly_bps"
            else float(funding_override["pre_bps_h"])
        )
        anchor_px = D(str(stage_bars[0]["close"]))
        pre_events = [
            (window_start - timedelta(hours=h), anchor_px, D(str(init_bps)) / D("10000"), "SyntheticHourly")
            for h in range(24, 0, -1)
        ]
        visible_funding = pre_events + window_funding
    else:
        visible_funding = [ev for ev in effective_funding if ev[0] < window_end]

    # Precompute trailing hourly volume strictly using prior completed bars (for conservative capacity).
    all_vol_bars = warmup_bars + stage_bars
    vol_series = pd.Series([float(b["volume"]) for b in all_vol_bars], dtype=float)
    trailing_vol_all = vol_series.rolling(policy.volume_lookback, min_periods=policy.volume_lookback).mean().to_numpy()
    warmup_len = len(warmup_bars)

    def bar_close_utc(b: dict[str, Any]) -> datetime:
        return parse_utc(b["time"]) + timedelta(hours=1, microseconds=-1)

    now = [bar_close_utc(stage_bars[0])]
    broker = _CampaignPaperBroker(
        Market.BINANCE_USDM,
        account_id,
        (Instrument(symbol, "USDT"),),
        {"USDT": initial_cash},
        fee_rate=assumptions.fee_rate,
        guardrails=guardrails,
    )
    store = PaperStore(":memory:", continuous_capacity=True)
    risk_cap = max(guardrails.max_notional, initial_cash)
    limits = RiskLimits(risk_cap, risk_cap, risk_cap, orders_per_minute=20)

    try:
        controller = TradingController(broker, store, clock=lambda: now[0], limits=limits)

        # Shared telemetry
        trades: list[dict[str, Any]] = []
        hourly_rows: list[dict[str, Any]] = []
        order_meta: dict[str, dict[str, Any]] = {}
        order_submitted_bar: dict[str, int] = {}
        consumed_delete_ids: set[str] = set()
        volume_demands: list[float] = []
        funding_seq = 0
        total_booked_funding = ZERO
        total_fees = ZERO
        total_slippage = ZERO
        partial_fill_events = 0
        canceled_orders = 0
        in_exit_lifecycle = False
        current_exit_failed_recorded = False
        current_exit_order_blocked_recorded = False
        exit_lifecycle_count = 0
        failed_exit_lifecycle_count = 0
        exit_order_attempt_count = 0
        failed_exit_order_attempt_count = 0
        exit_status_poll_checks = 0
        active_exit_order_ids: set[str] = set()
        pending_halt_cancels: set[str] = set()
        halted = False
        halt_reason: str | None = None

        hist_funding_idx = 0
        sorted_window_funding = sorted(window_funding, key=lambda x: x[0])

        def begin_exit_lifecycle() -> None:
            nonlocal in_exit_lifecycle, current_exit_failed_recorded, current_exit_order_blocked_recorded
            nonlocal exit_lifecycle_count
            if not in_exit_lifecycle:
                in_exit_lifecycle = True
                current_exit_failed_recorded = False
                current_exit_order_blocked_recorded = False
                exit_lifecycle_count += 1

        def close_exit_lifecycle_if_flat(inv: Decimal) -> None:
            nonlocal in_exit_lifecycle, current_exit_failed_recorded, current_exit_order_blocked_recorded
            if in_exit_lifecycle and inv == ZERO:
                in_exit_lifecycle = False
                current_exit_failed_recorded = False
                current_exit_order_blocked_recorded = False

        def record_exit_order_submitted(oid: str) -> None:
            nonlocal exit_order_attempt_count, current_exit_order_blocked_recorded
            exit_order_attempt_count += 1
            active_exit_order_ids.add(oid)
            current_exit_order_blocked_recorded = False

        def record_exit_order_filled(oid: str) -> None:
            active_exit_order_ids.discard(oid)

        def record_exit_order_failed(oid: str) -> None:
            nonlocal failed_exit_order_attempt_count
            if oid in active_exit_order_ids:
                active_exit_order_ids.discard(oid)
                failed_exit_order_attempt_count += 1

        def record_exit_submission_rejected() -> None:
            nonlocal exit_order_attempt_count, failed_exit_order_attempt_count
            exit_order_attempt_count += 1
            failed_exit_order_attempt_count += 1

        def record_exit_blocked_poll(*, terminal_block: bool = False) -> None:
            nonlocal exit_status_poll_checks, exit_order_attempt_count, failed_exit_order_attempt_count
            nonlocal current_exit_order_blocked_recorded, failed_exit_lifecycle_count, current_exit_failed_recorded
            exit_status_poll_checks += 1
            if not active_exit_order_ids and not current_exit_order_blocked_recorded:
                exit_order_attempt_count += 1
                failed_exit_order_attempt_count += 1
                current_exit_order_blocked_recorded = True
            if terminal_block and in_exit_lifecycle and not current_exit_failed_recorded:
                failed_exit_lifecycle_count += 1
                current_exit_failed_recorded = True

        def cancel_open_entry_buys_on_halt(*, grid_ref=None) -> None:
            """Upon risk halt, withdraw unsubmitted BUY proposals and cancel active non-reduce-only BUY orders.

            Preserves legitimate reduce-only SELL exits. When cancel_fill_race is True, queues cancel for
            post-race confirmation so any real fill prior to cancel acknowledgement is booked honestly.
            """
            nonlocal canceled_orders
            if grid_ref is not None:
                unsub_buys = [
                    o.intent.client_order_id
                    for o in grid_ref.active
                    if o.intent.client_order_id not in broker.state.orders
                    and o.intent.side is Side.BUY
                    and not o.intent.reduce_only
                ]
                if unsub_buys:
                    grid_ref.withdraw_unsubmitted_paper(controller, unsub_buys, delivery=grid_ref.delivery + 1)
            for oo in list(controller.account().open_orders):
                if oo.intent.side is not Side.BUY or oo.intent.reduce_only:
                    continue
                ok = oo.intent.client_order_id
                if ok in consumed_delete_ids:
                    continue
                if cancel_fill_race:
                    pending_halt_cancels.add(ok)
                    continue
                if grid_ref is not None:
                    if ok not in grid_ref.cancellations:
                        grid_ref.cancel_exit_paper(controller, ok, delivery=grid_ref.delivery + 1)
                    grid_ref.cancel_paper(controller, ok, delivery=grid_ref.delivery + 1)
                else:
                    controller.cancel(ok)
                consumed_delete_ids.add(ok)
                canceled_orders += 1

        def flush_pending_halt_cancels(*, grid_ref=None) -> None:
            nonlocal canceled_orders
            for ok in sorted(pending_halt_cancels):
                if ok in broker.state.orders and broker.state.orders[ok].snapshot.status not in TERMINAL and ok not in consumed_delete_ids:
                    if grid_ref is not None:
                        if ok not in grid_ref.cancellations:
                            grid_ref.cancel_exit_paper(controller, ok, delivery=grid_ref.delivery + 1)
                        grid_ref.cancel_paper(controller, ok, delivery=grid_ref.delivery + 1)
                    else:
                        controller.cancel(ok)
                    consumed_delete_ids.add(ok)
                    canceled_orders += 1
            pending_halt_cancels.clear()

        def check_loss_halt(current_equity: Decimal) -> bool:
            nonlocal halted, halt_reason
            if halted:
                return True
            if guardrails.max_loss is not None and (initial_cash - current_equity) >= guardrails.max_loss:
                halted = True
                halt_reason = "LOSS_BUDGET_EXHAUSTED"
                return True
            return False

        def record_volume_demand(qty: Decimal, bar_idx: int) -> None:
            denom = trailing_vol_all[warmup_len + bar_idx]
            if not math.isfinite(denom) or denom <= 0:
                volume_demands.append(math.inf)
            else:
                volume_demands.append(float(qty) / float(denom))

        def book_funding(pos_qty: Decimal, settlement_time: datetime, mark_price: Decimal, rate: Decimal, grid_ref=None) -> None:
            nonlocal funding_seq, total_booked_funding
            amount = funding_cashflow(pos_qty, mark_price, rate)
            quantized = amount.quantize(USDT_QUANTUM, rounding=ROUND_HALF_EVEN)
            if quantized == ZERO:
                return
            funding_seq += 1
            tran_id = f"s02-{account_id}-{funding_seq:08d}"
            ev = FundingEvent(
                account_id=account_id,
                tran_id=tran_id,
                symbol=symbol,
                asset="USDT",
                income_type="FUNDING_FEE",
                amount=quantized,
                event_time=settlement_time,
                source="s02-binance-funding",
                event_id=funding_event_id("FUNDING_FEE", tran_id),
                metadata={"mark_price": str(mark_price), "rate": str(rate)},
            )
            if grid_ref is not None:
                grid_ref.apply_funding_paper(controller, ev, delivery=grid_ref.delivery + 1)
            else:
                controller.replay(ev)
            total_booked_funding += quantized

        def settle_funding_before_fills(prev_close_ts: datetime | None, curr_close_ts: datetime, grid_ref=None) -> None:
            """STRICT R10 TIE RULE: settle (prev_close_ts, curr_close_ts] using PRE-FILL inventory."""
            nonlocal hist_funding_idx
            while hist_funding_idx < len(sorted_window_funding):
                f_ts, f_mark, f_rate, _ = sorted_window_funding[hist_funding_idx]
                if prev_close_ts is not None and f_ts <= prev_close_ts:
                    hist_funding_idx += 1
                    continue
                if f_ts > curr_close_ts:
                    break
                pos = broker.state.positions.get(symbol)
                if pos is not None and pos.quantity > ZERO:
                    book_funding(pos.quantity, f_ts, f_mark, f_rate, grid_ref=grid_ref)
                hist_funding_idx += 1

        def execute_fill_generic(
            key: str,
            qty: Decimal,
            px: Decimal,
            *,
            grid_ref=None,
        ) -> OrderEvent | None:
            try:
                if grid_ref is not None:
                    ev = grid_ref.fill_paper(controller, key, qty, px, delivery=grid_ref.delivery + 1)
                else:
                    ev = controller.fill(key, qty, px)
            except InsufficientCollateral:
                return None
            if ev is not None:
                return ev
            snap = broker.state.orders[key].snapshot
            if snap.status in TERMINAL or snap.reconcile_required:
                return None
            rem = snap.intent.quantity - snap.cumulative_filled_quantity
            fill_q = _floor_step(min(qty, rem), rules.step_size)
            if fill_q <= ZERO or fill_q >= snap.intent.quantity:
                return None
            total_q = snap.cumulative_filled_quantity + fill_q
            new_status = OrderStatus.FILLED if total_q == snap.intent.quantity else OrderStatus.PARTIALLY_FILLED
            fee_inc = fill_q * px * assumptions.fee_rate
            event = OrderEvent(
                client_order_id=key,
                exchange_order_id=f"paper:{key}",
                status=new_status,
                cumulative_filled_quantity=total_q,
                fill_price=px,
                event_time=now[0],
                source="s02-partial-fill",
                event_id=f"partial:{key}:{broker.state.sequence + 1}:{total_q}",
                cumulative_fee=snap.cumulative_fee + fee_inc,
            )
            try:
                if grid_ref is not None:
                    grid_ref.confirm_paper(controller, event, delivery=grid_ref.delivery + 1)
                else:
                    controller.replay(event)
            except InsufficientCollateral:
                return None
            return event

        def record_trade(event: OrderEvent, order_snap: Any, ref_price: Decimal, bar_time: str) -> tuple[Decimal, Decimal, Decimal]:
            nonlocal total_fees, total_slippage, partial_fill_events
            qty_delta = event.cumulative_filled_quantity - order_snap.cumulative_filled_quantity
            fee_delta = event.cumulative_fee - order_snap.cumulative_fee
            price = event.fill_price
            assert price is not None
            slip = qty_delta * abs(price - ref_price)
            total_fees += fee_delta
            total_slippage += slip
            if event.status == OrderStatus.PARTIALLY_FILLED:
                partial_fill_events += 1
            trades.append(
                {
                    "event_id": event.event_id,
                    "order_id": event.client_order_id,
                    "side": order_snap.intent.side.value,
                    "event_time": event.event_time.isoformat(),
                    "execution_date": bar_time,
                    "executed_shares": float(qty_delta),
                    "execution_price": float(price),
                    "fee": float(fee_delta),
                    "slippage": float(slip),
                    "trade_notional": float(qty_delta * price),
                    "transaction_cost": float(fee_delta + slip),
                    "purpose": order_meta.get(event.client_order_id, {}).get("purpose", "STRATEGY"),
                }
            )
            return qty_delta, fee_delta, slip

        def compute_bar_budget(bar: dict[str, Any]) -> Decimal:
            vol_budget = D(str(bar["volume"])) * assumptions.participation
            if assumptions.capacity_per_bar is not None:
                return min(vol_budget, assumptions.capacity_per_bar)
            return vol_budget

        # ------------------------------------------------------------------
        # Branch 1: Grid-family strategies (FIXED_GRID, FUNDING_SENSITIVE_GRID, VOL_ADAPTIVE_GRID)
        # ------------------------------------------------------------------
        if family in {"fixed_grid", "funding_sensitive_grid", "vol_adaptive_grid"}:
            sigma_ref = _fit_reference_vol_train_only(train_bars) if family == "vol_adaptive_grid" else None
            vol_policy = instance.to_vol_policy(reference_vol_1h=sigma_ref) if family == "vol_adaptive_grid" else None
            funding_policy = instance.to_funding_policy() if family == "funding_sensitive_grid" else None
            warmup_closes = [float(b["close"]) for b in warmup_bars[-96:]]

            def make_grid_instance(
                anchor_px: float,
                gen: int,
                prior_closes: list[float],
                init_equity: Decimal = initial_cash,
            ) -> GridExecution:
                dep_id = f"{account_id}-g{gen}"
                if family == "vol_adaptive_grid":
                    assert vol_policy is not None
                    wc = list(prior_closes) + [anchor_px]
                    init_vol = causal_realized_vol_1h(wc, vol_policy.vol_window_hours, vol_policy.reference_vol_1h)
                    hw = adaptive_half_width(init_vol, vol_policy)
                    cfg = make_grid_config(
                        anchor_px,
                        half_width_pct=hw,
                        max_cash_committed=float(initial_cash),
                        fee_bps=float(common["fee_bps"]),
                        slippage_bps=float(common["slippage_bps"]),
                    )
                    return VolatilityAdaptiveGrid(
                        cfg,
                        rules,
                        vol_policy=vol_policy,
                        warmup_closes=wc,
                        deployment_id=dep_id,
                        account_id=account_id,
                        market=Market.BINANCE_USDM,
                        instrument_id=symbol,
                        initial_equity=init_equity,
                        fee_rate=assumptions.fee_rate,
                        continuous_capacity=True,
                    )
                if family == "funding_sensitive_grid":
                    assert funding_policy is not None
                    cfg = make_grid_config(
                        anchor_px,
                        half_width_pct=float(instance.half_width_pct),
                        max_cash_committed=float(initial_cash),
                        fee_bps=float(common["fee_bps"]),
                        slippage_bps=float(common["slippage_bps"]),
                    )
                    return _LimitOnlyFundingSensitiveGrid(
                        cfg,
                        rules,
                        funding_policy=funding_policy,
                        funding_events=visible_funding,
                        deployment_id=dep_id,
                        account_id=account_id,
                        market=Market.BINANCE_USDM,
                        instrument_id=symbol,
                        initial_equity=init_equity,
                        fee_rate=assumptions.fee_rate,
                        continuous_capacity=True,
                    )
                cfg = make_grid_config(
                    anchor_px,
                    half_width_pct=float(instance.half_width_pct),
                    max_cash_committed=float(initial_cash),
                    fee_bps=float(common["fee_bps"]),
                    slippage_bps=float(common["slippage_bps"]),
                    order_ttl=int(instance.order_ttl),
                    breakout=str(instance.breakout),
                )
                return GridExecution(
                    cfg,
                    rules,
                    deployment_id=dep_id,
                    account_id=account_id,
                    market=Market.BINANCE_USDM,
                    instrument_id=symbol,
                    initial_equity=init_equity,
                    fee_rate=assumptions.fee_rate,
                    continuous_capacity=True,
                )

            generation = 0
            grid = make_grid_instance(float(stage_bars[0]["close"]), generation, warmup_closes)
            dispatched_exits_this_gen = 0
            prev_close: Decimal | None = None
            prev_close_ts: datetime | None = None
            seen_closes = list(warmup_closes)

            for bar_idx, bar in enumerate(stage_bars):
                now[0] = bar_close_utc(bar)
                close = D(str(bar["close"]))
                book = _synthetic_book(close, prev_close, rules, assumptions)
                exit_limit_bid = book["bid"] * (D("1") - assumptions.slippage) if assumptions.exit_marketable_at_submit else book["bid"]
                budget = compute_bar_budget(bar)
                bar_fee = ZERO
                bar_slip = ZERO
                bar_notional = ZERO

                # 1. Funding strictly BEFORE current-bar fills
                settle_funding_before_fills(prev_close_ts, now[0], grid_ref=grid)

                # Check collateral integrity before GridExecution (GridAccount forbids available < 0)
                pre_acct = _read_usdm_account(broker, symbol)
                if pre_acct["raw_available"] < ZERO or pre_acct["equity"] <= ZERO:
                    halted = True
                    halt_reason = halt_reason or "COLLATERAL_EXHAUSTED_UNDER_FUNDING"
                    cancel_open_entry_buys_on_halt(grid_ref=grid)
                    flush_pending_halt_cancels(grid_ref=grid)
                    controller.quote(Quote(symbol, close, now[0]))
                    acct_end = _read_usdm_account(broker, symbol)
                    pos_end = broker.state.positions.get(symbol)
                    unrealized = pos_end.quantity * (pos_end.mark_price - pos_end.entry_price) if pos_end else ZERO
                    hourly_rows.append(
                        {
                            "time": bar["time"],
                            "close": float(close),
                            "equity": float(acct_end["equity"]),
                            "cash": float(broker.state.cash["USDT"]),
                            "inventory": float(acct_end["inventory"]),
                            "unrealized": float(unrealized),
                            "net_exposure": float(acct_end["inventory"] * close),
                            "committed": float(acct_end["committed"]),
                            "available": float(acct_end["available"]),
                            "funding_cumulative": float(total_booked_funding),
                            "fee_bar": float(bar_fee),
                            "slippage_bar": float(bar_slip),
                            "notional_bar": float(bar_notional),
                            "holding_flag": 1 if acct_end["inventory"] > ZERO else 0,
                        }
                    )
                    seen_closes.append(float(close))
                    prev_close = close
                    prev_close_ts = now[0]
                    continue

                if check_loss_halt(pre_acct["equity"]):
                    cancel_open_entry_buys_on_halt(grid_ref=grid)

                # 2. Capture prior working orders
                prior_orders = [
                    (key, record.snapshot)
                    for key, record in broker.state.orders.items()
                    if record.observed_at < now[0] and record.snapshot.status not in TERMINAL
                ]

                # 3. Observe bar in Grid
                obs = {
                    "id": bar["id"],
                    "time": parse_utc(bar["time"]).replace(tzinfo=None).isoformat(),
                    "price": close,
                    "high": D(str(bar["high"])),
                    "low": D(str(bar["low"])),
                    "close": close,
                    "volume": max(0, int(D(str(bar["volume"])))),
                    "completed_bar": True,
                }
                grid.observe_paper(controller, obs, delivery=grid.delivery + 1, observed_at=now[0])

                # 4. Match prior working orders
                for key, order_snap in prior_orders:
                    if key not in broker.state.orders or broker.state.orders[key].snapshot.status in TERMINAL:
                        continue
                    is_exit = order_meta.get(key, {}).get("purpose") == "RISK_EXIT"
                    if is_exit:
                        if assumptions.exit_cannot_fill:
                            continue
                        if (bar_idx - order_submitted_bar.get(key, bar_idx)) < assumptions.exit_fill_delay_bars:
                            continue
                    cand = execution_candidate(
                        order_snap,
                        ask=book["ask"],
                        bid=book["bid"],
                        budget=budget,
                        slippage=assumptions.slippage,
                        step=rules.step_size,
                    )
                    if cand is None:
                        continue
                    qty_c, px_c = cand
                    if is_exit and not assumptions.exit_marketable_at_submit and order_snap.intent.limit_price is not None:
                        px_c = order_snap.intent.limit_price
                    ev = execute_fill_generic(key, qty_c, px_c, grid_ref=grid)
                    if ev is not None:
                        ref = book["ask"] if order_snap.intent.side == Side.BUY else book["bid"]
                        qd, fd, sd = record_trade(ev, order_snap, ref, bar["time"])
                        bar_fee += fd
                        bar_slip += sd
                        bar_notional += qd * ev.fill_price
                        budget -= qd
                        if is_exit and ev.status == OrderStatus.FILLED:
                            record_exit_order_filled(key)

                flush_pending_halt_cancels(grid_ref=grid)
                close_exit_lifecycle_if_flat(_read_usdm_account(broker, symbol)["inventory"])

                # 5. Standard cancellations
                for key in sorted(grid.cancellations):
                    if key in broker.state.orders and broker.state.orders[key].snapshot.status not in TERMINAL:
                        if key in grid.exit_orders:
                            continue
                        grid.cancel_paper(controller, key, delivery=grid.delivery + 1)
                        consumed_delete_ids.add(key)
                        canceled_orders += 1
                        record_exit_order_failed(key)

                if grid.pending_bar is not None:
                    grid.complete_bar_paper(controller, delivery=grid.delivery + 1)

                # 6. Normal submissions vs F11 breakout exit & F08 rotation
                acct_now = grid.account or project_paper_account(controller, symbol)
                if check_loss_halt(acct_now.equity):
                    cancel_open_entry_buys_on_halt(grid_ref=grid)

                if not grid.closed_by_breakout:
                    for key, order_snap in list(grid.orders.items()):
                        if order_snap.status == OrderStatus.NEW:
                            is_grid_exit = grid.state == "EXITING" or order_snap.intent.reduce_only
                            if is_grid_exit:
                                begin_exit_lifecycle()
                            if halted and not is_grid_exit:
                                grid.withdraw_unsubmitted_paper(controller, [key], delivery=grid.delivery + 1)
                                continue
                            notional = order_snap.intent.quantity * (order_snap.intent.limit_price or close)
                            if (
                                broker.global_post_blocked(notional)
                                or broker.window_post_blocked()
                                or len(controller.account().open_orders) >= guardrails.max_open_orders
                            ):
                                if is_grid_exit:
                                    record_exit_blocked_poll(terminal_block=False)
                                break
                            band_err = _check_percent_price(
                                order_snap.intent.limit_price or close, book["ask"], book["mark"], guardrails
                            )
                            if band_err is not None:
                                if is_grid_exit:
                                    record_exit_submission_rejected()
                                grid.withdraw_unsubmitted_paper(controller, [key], delivery=grid.delivery + 1)
                                continue
                            order_meta[key] = {"purpose": "RISK_EXIT" if is_grid_exit else "GRID", "generation": generation}
                            try:
                                grid.submit_paper(controller, key, delivery=grid.delivery + 1)
                                order_submitted_bar[key] = bar_idx
                                record_volume_demand(order_snap.intent.quantity, bar_idx)
                                if is_grid_exit:
                                    record_exit_order_submitted(key)
                            except RiskRejected:
                                order_meta.pop(key, None)
                                if is_grid_exit:
                                    record_exit_submission_rejected()
                elif guardrails.enable_f11:
                    unsubmitted = [
                        o.intent.client_order_id
                        for o in grid.active
                        if o.intent.client_order_id not in broker.state.orders
                    ]
                    if unsubmitted:
                        grid.withdraw_unsubmitted_paper(controller, unsubmitted, delivery=grid.delivery + 1)
                    acct_now = grid.account or project_paper_account(controller, symbol)
                    if acct_now.inventory > ZERO:
                        begin_exit_lifecycle()
                    open_orders = controller.account().open_orders
                    open_buys = [o for o in open_orders if o.intent.side is Side.BUY]
                    if open_buys:
                        bk = open_buys[0].intent.client_order_id
                        if bk not in consumed_delete_ids:
                            if bk not in grid.cancellations:
                                grid.cancel_exit_paper(controller, bk, delivery=grid.delivery + 1)
                            grid.cancel_paper(controller, bk, delivery=grid.delivery + 1)
                            consumed_delete_ids.add(bk)
                            canceled_orders += 1
                    elif acct_now.inventory > ZERO and open_orders:
                        sell = open_orders[0]
                        sk = sell.intent.client_order_id
                        if sell.intent.side is Side.SELL and sell.intent.reduce_only and sk in grid.orders:
                            if sk not in consumed_delete_ids:
                                age_sec = (bar_idx - order_submitted_bar.get(sk, bar_idx)) * 3600.0
                                target_px, _, plan_block = _plan_offline_limit_exit(
                                    grid=grid,
                                    inventory=acct_now.inventory,
                                    book=book,
                                    limit_bid=exit_limit_bid,
                                    quote_price_state=broker.state.quotes[symbol].price,
                                    guardrails=guardrails,
                                    dispatched_exit_attempts=dispatched_exits_this_gen,
                                    halted=halted,
                                    opening_equity=initial_cash,
                                    current_equity=acct_now.equity,
                                    broker=broker,
                                )
                                if (
                                    plan_block is None
                                    and target_px is not None
                                    and age_sec >= guardrails.exit_reprice_seconds
                                    and sell.intent.limit_price is not None
                                    and target_px < sell.intent.limit_price
                                ):
                                    grid.cancel_exit_paper(controller, sk, delivery=grid.delivery + 1)
                                    grid.cancel_paper(controller, sk, delivery=grid.delivery + 1)
                                    consumed_delete_ids.add(sk)
                                    canceled_orders += 1
                                    record_exit_order_failed(sk)
                                elif plan_block is not None and not (
                                    plan_block == "EXIT_MIN_NOTIONAL" and sell.cumulative_filled_quantity > ZERO
                                ):
                                    record_exit_blocked_poll(
                                        terminal_block=plan_block in {"EXIT_MAX_ATTEMPTS", "HALTED_BLOCK", "EXIT_MIN_NOTIONAL"}
                                    )
                                else:
                                    exit_status_poll_checks += 1
                    elif acct_now.inventory > ZERO and not open_orders:
                        target_px, target_qty, plan_block = _plan_offline_limit_exit(
                            grid=grid,
                            inventory=acct_now.inventory,
                            book=book,
                            limit_bid=exit_limit_bid,
                            quote_price_state=broker.state.quotes[symbol].price,
                            guardrails=guardrails,
                            dispatched_exit_attempts=dispatched_exits_this_gen,
                            halted=halted,
                            opening_equity=initial_cash,
                            current_equity=acct_now.equity,
                            broker=broker,
                        )
                        if plan_block is None and target_px is not None and target_qty is not None:
                            intent = grid.propose_limit_exit_paper(
                                controller,
                                {"price": target_px, "quantity": target_qty},
                                delivery=grid.delivery + 1,
                            )
                            if intent is not None:
                                ex_key = intent.client_order_id
                                order_meta[ex_key] = {"purpose": "RISK_EXIT", "generation": generation}
                                try:
                                    grid.submit_paper(controller, ex_key, delivery=grid.delivery + 1)
                                    dispatched_exits_this_gen += 1
                                    order_submitted_bar[ex_key] = bar_idx
                                    record_volume_demand(target_qty, bar_idx)
                                    record_exit_order_submitted(ex_key)
                                    if (
                                        assumptions.exit_marketable_at_submit
                                        and not assumptions.exit_cannot_fill
                                        and assumptions.exit_fill_delay_bars == 0
                                    ):
                                        osnap = broker.state.orders[ex_key].snapshot
                                        cand_ex = execution_candidate(
                                            osnap,
                                            ask=book["ask"],
                                            bid=book["bid"],
                                            budget=budget,
                                            slippage=assumptions.slippage,
                                            step=rules.step_size,
                                        )
                                        if cand_ex is not None:
                                            ev_ex = execute_fill_generic(ex_key, cand_ex[0], cand_ex[1], grid_ref=grid)
                                            if ev_ex is not None:
                                                qd, fd, sd = record_trade(ev_ex, osnap, book["bid"], bar["time"])
                                                bar_fee += fd
                                                bar_slip += sd
                                                bar_notional += qd * ev_ex.fill_price
                                                budget -= qd
                                                if ev_ex.status == OrderStatus.FILLED:
                                                    record_exit_order_filled(ex_key)
                                                close_exit_lifecycle_if_flat(_read_usdm_account(broker, symbol)["inventory"])
                                except RiskRejected:
                                    order_meta.pop(ex_key, None)
                                    grid.withdraw_unsubmitted_paper(controller, [ex_key], delivery=grid.delivery + 1)
                                    record_exit_submission_rejected()
                            else:
                                record_exit_blocked_poll(terminal_block=False)
                        else:
                            record_exit_blocked_poll(
                                terminal_block=plan_block in {"EXIT_MAX_ATTEMPTS", "HALTED_BLOCK", "EXIT_MIN_NOTIONAL"}
                            )

                # 7. F08 post-flat rotation
                if guardrails.enable_f08 and grid.closed_by_breakout and not halted and bar_idx < len(stage_bars) - 1:
                    acct_rot = grid.account or project_paper_account(controller, symbol)
                    if acct_rot.inventory == ZERO and not controller.account().open_orders and not grid.active:
                        entry_notional = rules.quantity_unit * close
                        if (
                            entry_notional >= guardrails.min_notional
                            and entry_notional <= guardrails.max_notional
                            and not broker.global_post_blocked(entry_notional)
                            and not broker.window_post_blocked()
                        ):
                            generation += 1
                            dispatched_exits_this_gen = 0
                            replacement = make_grid_instance(
                                float(close), generation, seen_closes[-96:], init_equity=acct_rot.equity
                            )
                            grid.rotate_paper(controller, replacement)
                            grid = replacement

                # 8. Record hourly snapshot
                acct_end = _read_usdm_account(broker, symbol)
                pos_end = broker.state.positions.get(symbol)
                unrealized = pos_end.quantity * (pos_end.mark_price - pos_end.entry_price) if pos_end else ZERO
                hourly_rows.append(
                    {
                        "time": bar["time"],
                        "close": float(close),
                        "equity": float(acct_end["equity"]),
                        "cash": float(broker.state.cash["USDT"]),
                        "inventory": float(acct_end["inventory"]),
                        "unrealized": float(unrealized),
                        "net_exposure": float(acct_end["inventory"] * close),
                        "committed": float(acct_end["committed"]),
                        "available": float(acct_end["available"]),
                        "funding_cumulative": float(total_booked_funding),
                        "fee_bar": float(bar_fee),
                        "slippage_bar": float(bar_slip),
                        "notional_bar": float(bar_notional),
                        "holding_flag": 1 if acct_end["inventory"] > ZERO else 0,
                    }
                )
                seen_closes.append(float(close))
                prev_close = close
                prev_close_ts = now[0]

        # ------------------------------------------------------------------
        # Branch 2: S01 Target-protocol strategies (CASH, DUAL_MA, CLOSE_MOMENTUM)
        # ------------------------------------------------------------------
        else:
            proto_spec = instance.describe()
            strategy_state: dict[str, Any] = {}
            history_bars: list[dict[str, Any]] = [
                {
                    "symbol": symbol,
                    "opened_at": b["time"],
                    "closed_at": bar_close_utc(b).isoformat(),
                    "open": float(b["open"]),
                    "high": float(b["high"]),
                    "low": float(b["low"]),
                    "close": float(b["close"]),
                    "volume": float(b["volume"]),
                }
                for b in warmup_bars[-proto_spec.history_limit:]
            ]
            prev_close = None
            prev_close_ts = None
            order_seq = 0
            consecutive_exit_attempts = 0

            for bar_idx, bar in enumerate(stage_bars):
                now[0] = bar_close_utc(bar)
                close = D(str(bar["close"]))
                book = _synthetic_book(close, prev_close, rules, assumptions)
                budget = compute_bar_budget(bar)
                bar_fee = ZERO
                bar_slip = ZERO
                bar_notional = ZERO

                # 1. Funding strictly BEFORE current-bar fills
                settle_funding_before_fills(prev_close_ts, now[0])

                # 2. Update mark quote & check pre-fill risk halt (funding/MTM breach cancels open BUY immediately unless race modeled)
                controller.quote(Quote(symbol, close, now[0]))
                pre_acct = _read_usdm_account(broker, symbol)
                if pre_acct["raw_available"] < ZERO or pre_acct["equity"] <= ZERO:
                    halted = True
                    halt_reason = halt_reason or "COLLATERAL_EXHAUSTED_UNDER_FUNDING"
                if check_loss_halt(pre_acct["equity"]):
                    cancel_open_entry_buys_on_halt()

                # Capture prior working orders (after immediate halt cancellation if not in race mode)
                prior_orders = [
                    (key, record.snapshot)
                    for key, record in broker.state.orders.items()
                    if record.observed_at < now[0] and record.snapshot.status not in TERMINAL
                ]

                # 3. Match prior working orders
                for key, order_snap in prior_orders:
                    if key not in broker.state.orders or broker.state.orders[key].snapshot.status in TERMINAL:
                        continue
                    is_exit = order_meta.get(key, {}).get("purpose") == "RISK_EXIT"
                    if is_exit:
                        if assumptions.exit_cannot_fill:
                            continue
                        if (bar_idx - order_submitted_bar.get(key, bar_idx)) < assumptions.exit_fill_delay_bars:
                            continue
                    cand = execution_candidate(
                        order_snap,
                        ask=book["ask"],
                        bid=book["bid"],
                        budget=budget,
                        slippage=assumptions.slippage,
                        step=rules.step_size,
                    )
                    if cand is None:
                        continue
                    qty_c, px_c = cand
                    ev = execute_fill_generic(key, qty_c, px_c)
                    if ev is not None:
                        ref = book["ask"] if order_snap.intent.side == Side.BUY else book["bid"]
                        qd, fd, sd = record_trade(ev, order_snap, ref, bar["time"])
                        bar_fee += fd
                        bar_slip += sd
                        bar_notional += qd * ev.fill_price
                        budget -= qd
                        if is_exit and ev.status == OrderStatus.FILLED:
                            consecutive_exit_attempts = 0
                            record_exit_order_filled(key)

                flush_pending_halt_cancels()
                close_exit_lifecycle_if_flat(_read_usdm_account(broker, symbol)["inventory"])

                # 4. Append completed bar strictly causally & ask S01 strategy for Decision
                history_bars.append(
                    {
                        "symbol": symbol,
                        "opened_at": bar["time"],
                        "closed_at": now[0].isoformat(),
                        "open": float(bar["open"]),
                        "high": float(bar["high"]),
                        "low": float(bar["low"]),
                        "close": float(bar["close"]),
                        "volume": float(bar["volume"]),
                    }
                )
                if len(history_bars) > proto_spec.history_limit:
                    del history_bars[:-proto_spec.history_limit]
                event = {
                    "kind": "closed_bar",
                    "symbol": symbol,
                    "now": now[0].isoformat(),
                    "bars": list(history_bars),
                }
                decision = instance.decide(event, strategy_state).validate(proto_spec, symbol)
                strategy_state = dict(decision.state)

                # 5. Enforce account-level loss halt & LIMIT order lifecycle
                acct_now = _read_usdm_account(broker, symbol)
                if acct_now["raw_available"] < ZERO or acct_now["equity"] <= ZERO:
                    halted = True
                    halt_reason = halt_reason or "COLLATERAL_EXHAUSTED_UNDER_FUNDING"
                if check_loss_halt(acct_now["equity"]):
                    cancel_open_entry_buys_on_halt()
                open_orders = list(controller.account().open_orders)

                if decision.target_weight is not None:
                    desired_weight = min(decision.target_weight, D(str(common["max_weight"])))
                    buy_limit = _ceil_step(book["ask"] * (D("1") + limit_buffer), rules.tick_size)
                    sell_limit = max(rules.tick_size, _floor_step(book["bid"] * (D("1") - limit_buffer), rules.tick_size))
                    desired_qty = target_quantity(
                        equity=max(ZERO, acct_now["equity"]),
                        weight=desired_weight,
                        price=buy_limit,
                        budget_price=buy_limit,
                        fee_rate=assumptions.fee_rate,
                        current=acct_now["inventory"],
                        available=acct_now["available"],
                        step=rules.step_size,
                    )
                    if desired_qty < acct_now["inventory"]:
                        desired_qty = target_quantity(
                            equity=max(ZERO, acct_now["equity"]),
                            weight=desired_weight,
                            price=sell_limit,
                            budget_price=sell_limit,
                            fee_rate=assumptions.fee_rate,
                            current=acct_now["inventory"],
                            available=acct_now["available"],
                            step=rules.step_size,
                        )
                    ref_cap_price = buy_limit if desired_qty >= acct_now["inventory"] else sell_limit
                    desired_qty = min(
                        desired_qty,
                        _floor_step(guardrails.max_notional / ref_cap_price, rules.step_size),
                        guardrails.max_quantity,
                    )
                    delta = desired_qty - acct_now["inventory"]

                    # Cancel stale or conflicting working orders
                    for oo in open_orders:
                        ok = oo.intent.client_order_id
                        should_cancel = (
                            (delta > ZERO and oo.intent.side is Side.SELL)
                            or (delta < ZERO and oo.intent.side is Side.BUY)
                            or (delta == ZERO)
                            or (delta > ZERO and oo.intent.side is Side.BUY and oo.intent.limit_price != buy_limit)
                            or (
                                delta < ZERO
                                and oo.intent.side is Side.SELL
                                and oo.intent.limit_price is not None
                                and sell_limit < oo.intent.limit_price
                                and (bar_idx - order_submitted_bar.get(ok, bar_idx)) * 3600 >= guardrails.exit_reprice_seconds
                            )
                        )
                        if should_cancel and ok not in consumed_delete_ids:
                            controller.cancel(ok)
                            consumed_delete_ids.add(ok)
                            canceled_orders += 1
                            record_exit_order_failed(ok)

                    open_orders = list(controller.account().open_orders)
                    acct_now = _read_usdm_account(broker, symbol)
                    delta = desired_qty - acct_now["inventory"]
                    if delta >= ZERO or acct_now["inventory"] == ZERO:
                        consecutive_exit_attempts = 0
                        if acct_now["inventory"] == ZERO:
                            close_exit_lifecycle_if_flat(ZERO)
                        elif in_exit_lifecycle and desired_weight > ZERO:
                            in_exit_lifecycle = False
                            current_exit_failed_recorded = False
                            current_exit_order_blocked_recorded = False

                    if not open_orders and delta > ZERO and not halted:
                        buy_qty = _floor_step(min(delta, guardrails.max_quantity - acct_now["inventory"]), rules.step_size)
                        notional = buy_qty * buy_limit
                        band_err = _check_percent_price(buy_limit, book["ask"], book["mark"], guardrails)
                        if (
                            buy_qty >= rules.min_quantity
                            and notional >= rules.min_notional
                            and notional <= guardrails.max_notional
                            and notional * (D("1") + assumptions.fee_rate) <= acct_now["available"]
                            and band_err is None
                            and not broker.global_post_blocked(notional)
                            and not broker.window_post_blocked()
                        ):
                            order_seq += 1
                            oid = f"{account_id}-b{order_seq:05d}"
                            intent = OrderIntent(
                                Market.BINANCE_USDM,
                                symbol,
                                Side.BUY,
                                OrderType.LIMIT,
                                buy_qty,
                                buy_limit,
                                oid,
                                position_side="BOTH",
                                reduce_only=False,
                            )
                            order_meta[oid] = {"purpose": "ENTRY"}
                            try:
                                controller.submit(intent)
                                order_submitted_bar[oid] = bar_idx
                                record_volume_demand(buy_qty, bar_idx)
                            except RiskRejected:
                                order_meta.pop(oid, None)

                    elif open_orders and delta < ZERO and acct_now["inventory"] > ZERO:
                        exit_status_poll_checks += 1

                    elif not open_orders and delta < ZERO and acct_now["inventory"] > ZERO:
                        raw_sell_qty = _floor_step(min(abs(delta), acct_now["inventory"]), rules.step_size)
                        is_true_exit = desired_weight == ZERO or raw_sell_qty >= acct_now["inventory"]
                        # Chunk reduce-only SELL if MTM rally pushed notional above max_notional (matching R09 _plan_offline_limit_exit).
                        quote_ref = max(sell_limit, book["ask"])
                        max_by_notional = _floor_step(guardrails.max_notional / quote_ref, rules.step_size)
                        exit_cap = min(guardrails.max_quantity, max_by_notional)
                        if raw_sell_qty <= exit_cap:
                            sell_qty = raw_sell_qty
                        else:
                            min_chunk = _ceil_step(max(rules.min_quantity, rules.min_notional / sell_limit), rules.step_size)
                            if exit_cap >= min_chunk:
                                chunks = (raw_sell_qty / exit_cap).to_integral_value(rounding=ROUND_CEILING)
                                sell_qty = _floor_step(min(exit_cap, raw_sell_qty - (chunks - 1) * min_chunk), rules.step_size)
                            else:
                                sell_qty = raw_sell_qty
                        notional = sell_qty * sell_limit
                        # Skip sub-min_notional partial rebalance trims while staying long;
                        # count exit lifecycles / order attempts strictly on genuine exit intents.
                        if not is_true_exit and (sell_qty < rules.min_quantity or notional < rules.min_notional):
                            pass
                        else:
                            if is_true_exit:
                                begin_exit_lifecycle()
                            band_err = _check_percent_price(sell_limit, book["ask"], book["mark"], guardrails)
                            if (
                                sell_qty >= rules.min_quantity
                                and notional >= rules.min_notional
                                and notional <= guardrails.max_notional
                                and band_err is None
                                and consecutive_exit_attempts < guardrails.exit_max_attempts
                                and not broker.global_post_blocked(notional)
                                and not broker.window_post_blocked()
                            ):
                                order_seq += 1
                                consecutive_exit_attempts += 1
                                oid = f"{account_id}-s{order_seq:05d}"
                                intent = OrderIntent(
                                    Market.BINANCE_USDM,
                                    symbol,
                                    Side.SELL,
                                    OrderType.LIMIT,
                                    sell_qty,
                                    sell_limit,
                                    oid,
                                    position_side="BOTH",
                                    reduce_only=True,
                                )
                                order_meta[oid] = {"purpose": "RISK_EXIT"}
                                try:
                                    controller.submit(intent)
                                    order_submitted_bar[oid] = bar_idx
                                    record_volume_demand(sell_qty, bar_idx)
                                    if is_true_exit:
                                        record_exit_order_submitted(oid)
                                except RiskRejected:
                                    order_meta.pop(oid, None)
                                    if is_true_exit:
                                        record_exit_submission_rejected()
                            elif is_true_exit:
                                record_exit_blocked_poll(
                                    terminal_block=(
                                        consecutive_exit_attempts >= guardrails.exit_max_attempts
                                        or sell_qty < rules.min_quantity
                                        or notional < rules.min_notional
                                    )
                                )

                # 6. Record hourly snapshot
                acct_end = _read_usdm_account(broker, symbol)
                pos_end = broker.state.positions.get(symbol)
                unrealized = pos_end.quantity * (pos_end.mark_price - pos_end.entry_price) if pos_end else ZERO
                hourly_rows.append(
                    {
                        "time": bar["time"],
                        "close": float(close),
                        "equity": float(acct_end["equity"]),
                        "cash": float(broker.state.cash["USDT"]),
                        "inventory": float(acct_end["inventory"]),
                        "unrealized": float(unrealized),
                        "net_exposure": float(acct_end["inventory"] * close),
                        "committed": float(acct_end["committed"]),
                        "available": float(acct_end["available"]),
                        "funding_cumulative": float(total_booked_funding),
                        "fee_bar": float(bar_fee),
                        "slippage_bar": float(bar_slip),
                        "notional_bar": float(bar_notional),
                        "holding_flag": 1 if acct_end["inventory"] > ZERO else 0,
                    }
                )
                prev_close = close
                prev_close_ts = now[0]

        # ------------------------------------------------------------------
        # Independent Accounting & Funding Tie-Rule Verification
        # ------------------------------------------------------------------
        ref_flows = position_settlements(
            [
                {
                    "event_time": t["event_time"],
                    "side": t["side"],
                    "executed_shares": str(t["executed_shares"]),
                }
                for t in trades
            ],
            sorted_window_funding,
        )
        ref_funding_total = sum((f["funding"] for f in ref_flows), ZERO)
        funding_residual = abs(float(total_booked_funding - ref_funding_total))
        if funding_residual > 1e-4:
            raise ValueError(
                f"funding settlement tie-rule reconciliation mismatch ({funding_residual:.8f} USDT)"
            )

        acct_final = _read_usdm_account(broker, symbol)
        pos_final = broker.state.positions.get(symbol)
        unrealized_final = pos_final.quantity * (pos_final.mark_price - pos_final.entry_price) if pos_final else ZERO
        # PaperBroker USD-M cash = initial_cash + realized_trade_pnl - total_fees + total_funding
        # equity = cash + unrealized_final
        equity_residual = abs(float(acct_final["equity"] - (broker.state.cash["USDT"] + unrealized_final)))
        if equity_residual > 1e-6:
            raise ValueError(f"equity mark-to-market reconciliation mismatch ({equity_residual:.8f} USDT)")

        orders_map = broker.state.orders
        submitted_orders = len(orders_map)
        filled_orders = sum(1 for r in orders_map.values() if r.snapshot.cumulative_filled_quantity > ZERO)
        unfilled_orders = sum(1 for r in orders_map.values() if r.snapshot.cumulative_filled_quantity == ZERO)
        mean_hold_h, max_hold_h = _holding_durations_from_trades(trades, window_end)

        # Finalize any exit orders / exit lifecycle still unresolved at window end
        failed_exit_order_attempt_count += len(active_exit_order_ids)
        active_exit_order_ids.clear()
        if in_exit_lifecycle and acct_final["inventory"] > ZERO and not current_exit_failed_recorded:
            failed_exit_lifecycle_count += 1
            current_exit_failed_recorded = True

        hourly_df = pd.DataFrame(hourly_rows)
        telemetry = {
            "submitted_orders": submitted_orders,
            "filled_orders": filled_orders,
            "partial_fill_events": partial_fill_events,
            "canceled_orders": canceled_orders,
            "unfilled_orders": unfilled_orders,
            "total_fees_usdt": float(total_fees),
            "total_slippage_usdt": float(total_slippage),
            "total_funding_usdt": float(total_booked_funding),
            "mean_holding_hours": mean_hold_h,
            "max_holding_hours": max_hold_h,
            "exit_attempts": exit_lifecycle_count,
            "exit_failures": failed_exit_lifecycle_count,
            "exit_lifecycle_count": exit_lifecycle_count,
            "failed_exit_lifecycle_count": failed_exit_lifecycle_count,
            "exit_order_attempt_count": exit_order_attempt_count,
            "failed_exit_order_attempt_count": failed_exit_order_attempt_count,
            "exit_status_poll_checks": exit_status_poll_checks,
            "funding_reconciliation_residual": funding_residual,
            "equity_reconciliation_residual": equity_residual,
            "halted": halted,
            "halt_reason": halt_reason,
            "cancel_fill_race_mode": bool(cancel_fill_race),
        }
        daily_nav, metrics = _compute_financial_metrics(
            hourly_df,
            trades,
            telemetry,
            initial_cash=float(initial_cash),
            policy=policy,
            volume_demands=volume_demands,
        )
        trades_df = pd.DataFrame(
            trades,
            columns=[
                "event_id",
                "order_id",
                "side",
                "event_time",
                "execution_date",
                "executed_shares",
                "execution_price",
                "fee",
                "slippage",
                "trade_notional",
                "transaction_cost",
                "purpose",
            ],
        )
        return {
            "candidate": candidate,
            "metrics": metrics,
            "nav": daily_nav,
            "hourly": hourly_df,
            "trades": trades_df,
            "telemetry": telemetry,
        }
    finally:
        store.db.close()


def _simulate_worker(payload: tuple[str, dict[str, Any]]) -> tuple[str, dict[str, Any] | None, dict[str, Any] | None]:
    tag, kwargs = payload
    try:
        return tag, simulate_binance_candidate(**kwargs), None
    except CollateralDepletedError as exc:
        return tag, None, {
            "category": "COLLATERAL_DEPLETED",
            "error_type": type(exc).__name__,
            "detail": str(exc),
            "final_equity": exc.final_equity,
            "halt_reason": exc.halt_reason,
        }
    except Exception as exc:
        return tag, None, {
            "category": "SIMULATION_FAILED",
            "error_type": type(exc).__name__,
            "detail": str(exc),
        }


def run_simulations_batch(
    tasks: list[tuple[str, dict[str, Any]]],
    *,
    max_workers: int | None = None,
) -> dict[str, tuple[dict[str, Any] | None, dict[str, Any] | str | None]]:
    """Run independent PaperBroker simulations deterministically (parallel when >= 4 tasks)."""
    if not tasks:
        return {}
    workers = max_workers if max_workers is not None else min(6, os.cpu_count() or 1)
    if workers <= 1 or len(tasks) < 4:
        return {tag: (sim, err) for tag, sim, err in (_simulate_worker(t) for t in tasks)}
    results: dict[str, tuple[dict[str, Any] | None, dict[str, Any] | str | None]] = {}
    with ProcessPoolExecutor(max_workers=min(workers, len(tasks))) as pool:
        for tag, sim, err in pool.map(_simulate_worker, tasks):
            results[tag] = (sim, err)
    return results


def run_risk_stress_suite(
    candidates_to_stress: list[tuple[dict[str, Any], Path]],
    *,
    stage_bars: list[dict[str, Any]],
    warmup_bars: list[dict[str, Any]],
    train_bars: list[dict[str, Any]],
    funding_events: list[tuple[datetime, Decimal, Decimal, str]],
    common: dict[str, Any],
    policy: SelectionPolicy,
) -> dict[str, Any]:
    """Run funding & tail-risk stress evaluations (explicitly NOT OOS promotion evidence).

    Demo high-funding stress (22.97 bps/h) and 48h burst switch use the 7-day (168h)
    horizon matching R08/R09/R10; 5x historical Mainnet funding runs on the full window.
    """
    stress_168_bars = stage_bars[-168:] if len(stage_bars) > 168 else stage_bars
    stress_168_warmup = (warmup_bars + stage_bars[:-168]) if len(stage_bars) > 168 else warmup_bars
    regimes = [
        ("mainnet_historical_full", stage_bars, warmup_bars, None),
        ("mainnet_5x_funding_full", stage_bars, warmup_bars, {"mode": "scaled_historical", "scale": 5.0}),
        (
            "demo_high_funding_22_97bps_h_168h",
            stress_168_bars,
            stress_168_warmup,
            {"mode": "constant_hourly_bps", "hourly_bps": DEMO_OBSERVED_HOURLY_FUNDING_BPS},
        ),
        (
            "funding_regime_switch_48h_168h",
            stress_168_bars,
            stress_168_warmup,
            {
                "mode": "regime_switch",
                "switch_hour": 48,
                "pre_bps_h": NORMAL_PERP_HOURLY_FUNDING_BPS,
                "post_bps_h": DEMO_OBSERVED_HOURLY_FUNDING_BPS,
            },
        ),
    ]
    tasks: list[tuple[str, dict[str, Any]]] = []
    meta_by_tag: dict[str, tuple[dict[str, Any], str]] = {}
    for cand, src_path in candidates_to_stress:
        for regime_name, r_bars, r_warmup, override in regimes:
            tag = f"{cand['candidate_id']}::{regime_name}"
            meta_by_tag[tag] = (cand, regime_name)
            tasks.append(
                (
                    tag,
                    dict(
                        candidate=cand,
                        source_path=src_path,
                        stage_bars=r_bars,
                        warmup_bars=r_warmup,
                        train_bars=train_bars,
                        funding_events=funding_events,
                        common=common,
                        policy=policy,
                        funding_override=override,
                        account_id=f"stress-{cand['strategy_id']}-{regime_name[:12]}",
                    ),
                )
            )
    batch = run_simulations_batch(tasks)
    rows: list[dict[str, Any]] = []
    for tag, _ in tasks:
        cand, regime_name = meta_by_tag[tag]
        res, err = batch[tag]
        if res is not None:
            m = res["metrics"]
            t_info = res["telemetry"]
            row_status = "RISK_HALTED" if t_info.get("halted") else "OK"
            rows.append(
                {
                    "candidate_id": cand["candidate_id"],
                    "strategy_id": cand["strategy_id"],
                    "regime": regime_name,
                    "status": row_status,
                    "halt_reason": t_info.get("halt_reason"),
                    "error_type": None,
                    "detail": None,
                    "net_return": m["net_return"],
                    "max_drawdown": m["max_drawdown"],
                    "cvar_95": m["cvar_95"],
                    "funding_pnl_usdt": m["funding_pnl_usdt"],
                    "funding_erosion_pct": m["funding_erosion_pct"],
                    "fill_rate": m["fill_rate"],
                    "trade_count": m["trade_count"],
                    "holding_hours": m["holding_hours"],
                    "residual_inventory": m["residual_inventory"],
                    "exit_failure_ratio": m["exit_failure_ratio"],
                }
            )
        elif isinstance(err, dict) and err.get("category") == "COLLATERAL_DEPLETED":
            rows.append(
                {
                    "candidate_id": cand["candidate_id"],
                    "strategy_id": cand["strategy_id"],
                    "regime": regime_name,
                    "status": "COLLATERAL_DEPLETED",
                    "halt_reason": err.get("halt_reason"),
                    "error_type": err.get("error_type", "CollateralDepletedError"),
                    "detail": err.get("detail") or "authoritative PaperBroker ledger equity depleted",
                    "net_return": -1.0,
                    "max_drawdown": 1.0,
                    "cvar_95": None,
                    "funding_pnl_usdt": None,
                    "funding_erosion_pct": None,
                    "fill_rate": None,
                    "trade_count": None,
                    "holding_hours": None,
                    "residual_inventory": None,
                    "exit_failure_ratio": None,
                }
            )
        else:
            err_type = err.get("error_type", "Exception") if isinstance(err, dict) else "Exception"
            err_detail = err.get("detail", "simulation_failed") if isinstance(err, dict) else (err or "simulation_failed")
            rows.append(
                {
                    "candidate_id": cand["candidate_id"],
                    "strategy_id": cand["strategy_id"],
                    "regime": regime_name,
                    "status": "SIMULATION_FAILED",
                    "halt_reason": None,
                    "error_type": err_type,
                    "detail": err_detail,
                    "net_return": None,
                    "max_drawdown": None,
                    "cvar_95": None,
                    "funding_pnl_usdt": None,
                    "funding_erosion_pct": None,
                    "fill_rate": None,
                    "trade_count": None,
                    "holding_hours": None,
                    "residual_inventory": None,
                    "exit_failure_ratio": None,
                }
            )
    summary = {
        "total_runs": len(rows),
        "ok_runs": sum(1 for r in rows if r["status"] == "OK"),
        "risk_halted_runs": sum(1 for r in rows if r["status"] == "RISK_HALTED"),
        "collateral_depleted_runs": sum(1 for r in rows if r["status"] == "COLLATERAL_DEPLETED"),
        "simulation_failed_runs": sum(1 for r in rows if r["status"] == "SIMULATION_FAILED"),
    }
    return {
        "evidence_classification": "RISK_STRESS_ONLY_NOT_OOS_EVIDENCE",
        "note": (
            "Funding stress regimes (5x Mainnet, Demo 22.97 bps/h over 168h, and 48h burst switch over 168h) "
            "test downside resilience only; they NEVER substitute for fresh unseen OOS qualification. "
            "Program/data/strategy exceptions are classified as SIMULATION_FAILED (net_return=None) and excluded from financial risk summaries."
        ),
        "regimes": [name for name, _, _, _ in regimes],
        "summary": summary,
        "results": rows,
    }


def build_s03_release_envelope(
    selection: dict[str, Any],
    *,
    compatibility_by_id: dict[str, dict[str, Any]],
    source_dir: Path,
    output_dir: Path,
    code_commit: str,
    expected_active: str | None = None,
    migration: str = "wait_flat",
) -> dict[str, Any]:
    """Build native S04 FACTORLAB_DECISION_V1 StrategyPackage & verify via adapt_selection + LoadedStrategy."""
    import tempfile

    from factorlab.integrations.binance_futures_trading.champion_release_adapter import adapt_selection
    from factorlab.integrations.binance_futures_trading.strategy_deployment import LEGACY
    from factorlab.integrations.binance_futures_trading.strategy_package import (
        DECISION_INTERFACE,
        build_package,
        digest,
        install_package,
    )
    from factorlab.strategies.loader import LoadedStrategy

    if expected_active is None:
        expected_active = LEGACY

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    selected = selection["selected"]
    cid = selected["candidate_id"]
    compat = compatibility_by_id.get(cid, {})

    if selection.get("promoted") is not True:
        assert adapt_selection(selection, {}) is None
        envelope = {"selection": selection}
        envelope_path = output_dir / f"{selection['study_id']}.candidate.json"
        envelope_path.write_text(json.dumps(envelope, sort_keys=True, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        return {
            "status": "RETAIN_ENVELOPE_WRITTEN",
            "promoted": False,
            "s03_adapter_result": None,
            "envelope_path": str(envelope_path),
            "package_id": None,
        }

    if (
        not compat.get("s03_compatible")
        or not compat.get("s03_entrypoint")
        or compat.get("s03_entrypoint") == "LEGACY_DEMO_GRID_BRIDGE"
        or compat.get("s03_protocol") != DECISION_INTERFACE
    ):
        return {
            "status": "PROMOTED_REQUIRES_S04_RUNTIME_BRIDGE",
            "promoted": True,
            "s03_compatible": False,
            "s04_requirements": compat.get("s04_requirements") or "Native FACTORLAB_DECISION_V1 strategy bridge required",
            "package_id": None,
        }

    spec = compat.get("spec") or {}
    interval_seconds = int(selected["parameters"].get("interval_seconds", 3600))
    if not spec.get("strategy_id") or not spec.get("version") or not spec.get("decision_kind"):
        raise ValueError("missing StrategySpec metadata for native FACTORLAB_DECISION_V1 release package")
    if interval_seconds not in (spec.get("intervals") or []):
        raise ValueError("selected interval_seconds is not declared in frozen StrategySpec.intervals")

    evidence_entry = next(c for c in selection["candidates"] if c["candidate"]["candidate_id"] == cid)
    test_stage = evidence_entry["stages"]["test"]
    source_name = selected["strategy"]["file"].replace("\\", "/").rsplit("/", 1)[-1]
    manifest_draft = {
        "schema_version": 1,
        "interface": DECISION_INTERFACE,
        "environment": "BINANCE_USDM_DEMO",
        "symbol": selection["common"]["symbol"],
        "code_commit": code_commit,
        "strategy_id": str(spec["strategy_id"]),
        "strategy_version": str(spec["version"]),
        "state_version": str(spec.get("state_version", "1")),
        "execution_mode": str(spec["decision_kind"]),
        "interval_seconds": interval_seconds,
        "entrypoint": compat["s03_entrypoint"],
        "files": [source_name],
        "dependencies": [],
        "configuration": selected["parameters"],
        "migration": migration,
        "evidence": {
            "selection_sha256": digest(selection),
            "candidate_id": cid,
            "run_id": test_stage["run_id"],
            "record_sha256": test_stage["record_sha256"],
            "financial_admission": selection["financial_admission"],
            "expected_active": expected_active,
        },
    }
    archive_name = f"{selection['study_id']}-{cid}.zip"
    archive_path = output_dir / archive_name
    if archive_path.exists():
        archive_path.unlink()
    manifest = build_package(source_dir, manifest_draft, archive_path)
    adapted = adapt_selection(selection, manifest)

    # Verify the built package loads cleanly through the real S04 isolated worker & matches StrategySpec.
    with tempfile.TemporaryDirectory(prefix="factorlab-s04-pkg-verify-") as staging:
        release_dir, installed = install_package(archive_path, Path(staging) / "releases")
        loaded = LoadedStrategy.from_strategy_package(release_dir, installed, trusted=True)
        loaded.spec.require(
            symbol=selection["common"]["symbol"],
            interval_seconds=interval_seconds,
            data_types=["closed_bar"],
        )

    envelope = {
        "archive": archive_name,
        "selection": selection,
        "manifest": manifest,
    }
    envelope_path = output_dir / f"{selection['study_id']}.candidate.json"
    envelope_path.write_text(json.dumps(envelope, sort_keys=True, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    return {
        "status": "PROMOTE_PACKAGE_BUILT_AND_VERIFIED",
        "promoted": True,
        "s03_compatible": True,
        "protocol": DECISION_INTERFACE,
        "interval_seconds": interval_seconds,
        "execution_mode": str(spec["decision_kind"]),
        "package_id": manifest["package_id"],
        "archive_path": str(archive_path),
        "envelope_path": str(envelope_path),
        "s03_adapter_result": adapted,
    }
