"""P8-R10: Funding-Sensitive Grid and Volatility-Adaptive Grid research engine.

Reuses FactorLab's authoritative Paper execution authority (``GridExecution``,
``run_experiment``, ``PaperBroker``, ``TradingController``, ``grid_fifo``,
``grid_lifecycle``, and ``grid_funding``).

Strict causality guarantees:
- ``FundingSensitiveGrid`` only reads settled funding events with
  ``funding_time <= current_bar_close_utc``. Future settlements are never visible.
- ``VolatilityAdaptiveGrid`` only uses completed historical closes with
  ``close_time <= current_bar_close_utc`` and a reference volatility frozen on
  the training window.
- Neither strategy intercepts fills already triggered during the current bar
  using end-of-bar signals; cancellations and re-arming apply cleanly for
  subsequent bars.
"""
from __future__ import annotations

from bisect import bisect_right
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
import math
from pathlib import Path
from typing import Any
import urllib.request

import numpy as np
import pandas as pd

from factorlab.experiments.grid_empirical import load_hourly
from factorlab.experiments.grid_fifo import (
    attribute_events as paper_fifo_attribute_events,
    attribution as paper_fifo_attribution,
)
from factorlab.experiments.grid_funding import (
    cashflow as funding_cashflow,
    position_settlements,
)
from factorlab.experiments.grid_lifecycle import lifecycle_metrics
from factorlab.experiments.grid_paper import D, run_experiment
from factorlab.strategies.grid import GridConfig
from factorlab.strategies.grid_execution import GridExecution, GridRules
from factorlab.trading.contracts import Market, Side, ZERO

EXECUTION_MODEL_VERSION = "NO_F11_NO_F08_STRANDED_POSITION_BASELINE"
PAPER_FEE_RATE = Decimal("0.0003")
PAPER_SLIPPAGE = Decimal("0.0002")
DEMO_OBSERVED_HOURLY_FUNDING_BPS = 22.9669
NORMAL_PERP_HOURLY_FUNDING_BPS = 0.125


def parse_utc(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def validate_hourly_timeline(
    values: Any, *, start: datetime | None = None, end: datetime | None = None,
) -> pd.DatetimeIndex:
    """Reject malformed hours in source order; never sort, deduplicate or fill."""
    stamps = [pd.Timestamp(v) for v in values]
    if not stamps or any(pd.isna(t) or t.tzinfo is None for t in stamps):
        raise ValueError("Complete timezone-aware UTC hourly timeline required")
    times = pd.DatetimeIndex([t.tz_convert("UTC") for t in stamps])
    if times.has_duplicates:
        raise ValueError("Duplicate hourly timestamps")
    if not times.is_monotonic_increasing:
        raise ValueError("Hourly timestamps must be strictly increasing")
    if not times.equals(times.floor("h")):
        raise ValueError("UTC timestamps must align to whole hours")
    expected = pd.date_range(
        start if start is not None else times[0],
        end if end is not None else times[-1] + pd.Timedelta(hours=1),
        freq="h", inclusive="left",
    )
    if not times.equals(expected):
        raise ValueError("Missing hourly bars or incorrect coverage boundary; no filling permitted")
    return times


def synthetic_hourly_funding_events(
    dates: Any, closes: Any, *, constant_hourly_bps: float | None = None,
    regime_switch_bps: tuple[int, float, float] | None = None,
) -> list[tuple[datetime, Decimal, Decimal, str]]:
    """One settlement per observed bar close, shared by signals and accounting."""
    events = []
    for i, (date, close) in enumerate(zip(dates, closes)):
        if constant_hourly_bps is not None:
            bps = constant_hourly_bps
        else:
            assert regime_switch_bps is not None
            switch, pre, post = regime_switch_bps
            bps = pre if i < switch else post
        events.append((
            parse_utc(date) + timedelta(hours=1, microseconds=-1),
            D(str(close)), D(str(bps)) / D(10000), "SyntheticHourly",
        ))
    return events


def demo_grid_rules() -> GridRules:
    return GridRules(
        quantity_unit=D("0.04"),
        step_size=D("0.01"),
        tick_size=D("0.01"),
        min_quantity=D("0.01"),
        min_notional=D("5"),
    )


def make_grid_config(
    anchor_price: float,
    *,
    half_width_pct: float = 0.01,
    max_cash_committed: float = 10.0,
    fee_bps: float = 3.0,
    slippage_bps: float = 2.0,
    order_ttl: int = 100,
    breakout: str = "pause",
) -> GridConfig:
    """Create a GridConfig aligned to the R08 single-level control structure."""
    half_w = max(0.002, min(0.20, float(half_width_pct)))
    lower = round(anchor_price * (1.0 - half_w), 2)
    upper = round(anchor_price * (1.0 + half_w), 2)
    if lower >= anchor_price:
        lower = round(anchor_price - 0.02, 2)
    if upper <= anchor_price:
        upper = round(anchor_price + 0.02, 2)
    return GridConfig(
        mode="arithmetic",
        lower=lower,
        upper=upper,
        levels=1,
        initial_cash=10000.0,
        initial_inventory=0,
        initial_price=round(anchor_price, 2),
        quantity=1,
        max_inventory=1,
        max_cash_committed=float(max_cash_committed),
        atr_window=3,
        atr_multiplier=1.0,
        rebuild_threshold=2.0,
        min_rebuild_interval=3,
        max_turnover=10.0,
        breakout=breakout,
        max_drawdown=0.2,
        minimum_price=0.01,
        minimum_quantity=1,
        fee_bps=float(fee_bps),
        slippage_bps=float(slippage_bps),
        order_ttl=int(order_ttl),
        center_rule="last_close",
        expiry_reseed_limit=0,
        expiry_reseed_interval=100,
    )


# ---------------------------------------------------------------------------
# Strategy B: Funding-Sensitive Grid (strictly causal funding information set)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FundingPolicy:
    candidate_id: str = "B5"
    lookback_hours: int = 24
    conservative: bool = True
    entry_max_bps_h: float = 0.50
    expected_hold_hours: float = 48.0
    min_net_margin_bps: float = 100.0
    exit_mode: str = "funding_bleed"  # "none" | "funding_bleed" | "net_margin"
    exit_bps_h: float | None = 1.00
    max_hold_hours: float | None = 72.0

    def __post_init__(self) -> None:
        if self.exit_mode not in {"none", "funding_bleed", "net_margin"}:
            raise ValueError(f"Unsupported exit_mode: {self.exit_mode}")
        if self.lookback_hours < 1:
            raise ValueError("lookback_hours must be >= 1")


def causal_funding_rate_bps_h(
    funding_events: list[tuple[datetime, Decimal, Decimal, str]],
    obs_utc: datetime,
    *,
    lookback_hours: int = 24,
    conservative: bool = True,
    _sorted_times: list[datetime] | None = None,
) -> tuple[float, int, float]:
    """Estimate current hourly funding rate (bps/h) using ONLY events <= obs_utc.

    Returns ``(estimated_bps_h, event_count_in_window, last_event_bps_h)``.
    Positive values mean longs pay shorts.
    """
    window_start = obs_utc - timedelta(hours=lookback_hours)
    if _sorted_times is not None:
        end_idx = bisect_right(_sorted_times, obs_utc)
        if end_idx == 0:
            return 0.0, 0, 0.0
        start_idx = bisect_right(_sorted_times, window_start, 0, end_idx)
        if start_idx >= end_idx:
            return 0.0, 0, 0.0
        total_bps = sum(float(funding_events[i][2]) * 10_000.0 for i in range(start_idx, end_idx))
        n_recent = end_idx - start_idx
        last_ev = funding_events[end_idx - 1]
        if end_idx >= 2:
            gap_h = max(1.0, min(8.0, (last_ev[0] - funding_events[end_idx - 2][0]).total_seconds() / 3600.0))
        else:
            gap_h = 8.0
    else:
        past_all = [ev for ev in funding_events if ev[0] <= obs_utc]
        if not past_all:
            return 0.0, 0, 0.0
        recent = [ev for ev in past_all if ev[0] > window_start]
        if not recent:
            # Decay gracefully if no settlement occurred inside lookback window.
            return 0.0, 0, 0.0
        total_bps = sum(float(ev[2]) * 10_000.0 for ev in recent)
        n_recent = len(recent)
        last_ev = past_all[-1]
        if len(past_all) >= 2:
            gap_h = max(1.0, min(8.0, (last_ev[0] - past_all[-2][0]).total_seconds() / 3600.0))
        else:
            gap_h = 8.0
    mean_bps_h = total_bps / float(lookback_hours)
    last_bps_h = float(last_ev[2]) * 10_000.0 / gap_h
    est = max(mean_bps_h, last_bps_h) if conservative else mean_bps_h
    return est, n_recent, last_bps_h


class FundingSensitiveGrid(GridExecution):
    """GridExecution subclass applying causal funding entry & exit gates."""

    def __init__(
        self,
        *args: Any,
        funding_policy: FundingPolicy,
        funding_events: list[tuple[datetime, Decimal, Decimal, str]],
        use_f11_limit_exit: bool = False,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.funding_policy = funding_policy
        self.use_f11_limit_exit = bool(use_f11_limit_exit)
        # Pre-sort immutably by UTC timestamp; decision code filters <= obs_utc strictly.
        self._all_funding_events = sorted(funding_events, key=lambda e: e[0])
        self._all_funding_times = [e[0] for e in self._all_funding_events]
        self.buy_blocked = False
        self.last_funding_bps_h = 0.0
        self.entry_time_utc: datetime | None = None
        self.entry_price_dec: Decimal | None = None
        self.entry_time_unrecoverable: bool = False
        self.gate_events: list[dict[str, Any]] = []
        self.exit_reasons: list[str] = []
        self._recorded_exit_reasons: set[str] = set()
        self.funding_exit_triggers = 0
        self.blocked_hours = 0
        self.total_observed_hours = 0

    def _stage(self) -> FundingSensitiveGrid:
        staged = super()._stage()
        staged.gate_events = self.gate_events.copy()
        staged.exit_reasons = self.exit_reasons.copy()
        staged._recorded_exit_reasons = self._recorded_exit_reasons.copy()
        return staged

    def _subclass_checkpoint(self) -> dict[str, Any]:
        """Persist explicit FundingSensitiveGrid decision state in the authoritative checkpoint."""
        return {
            "schema_version": 1,
            "entry_time_utc": self.entry_time_utc.isoformat() if self.entry_time_utc is not None else None,
            "entry_price_dec": str(self.entry_price_dec) if self.entry_price_dec is not None else None,
            "entry_time_unrecoverable": bool(self.entry_time_unrecoverable),
            "buy_blocked": bool(self.buy_blocked),
            "last_funding_bps_h": float(self.last_funding_bps_h),
            "exit_reasons": list(self.exit_reasons),
            "funding_exit_triggers": int(self.funding_exit_triggers),
            "blocked_hours": int(self.blocked_hours),
            "total_observed_hours": int(self.total_observed_hours),
        }

    def _recover_entry_from_authoritative_events(
        self,
        broker: Any,
        store: Any,
        expected_inventory: Decimal,
    ) -> tuple[datetime | None, Decimal | None]:
        """Reconstruct open-lot entry timestamp/price strictly from authoritative Paper fill events.

        Returns ``(None, None)`` if authoritative events cannot reliably explain the current
        inventory (never fabricates ``now`` or zero age).
        """
        if expected_inventory <= ZERO or store is None or broker is None:
            return None, None
        filled_by_order: dict[str, Decimal] = {}
        running_inv = ZERO
        lot_open_time: datetime | None = None
        lot_open_price: Decimal | None = None
        orders_map = broker.state.orders
        for cursor, ev in store.order_events():
            prev_q = filled_by_order.get(ev.client_order_id, ZERO)
            delta = ev.cumulative_filled_quantity - prev_q
            filled_by_order[ev.client_order_id] = ev.cumulative_filled_quantity
            if delta <= ZERO:
                continue
            rec = orders_map.get(ev.client_order_id)
            if rec is None or rec.snapshot.intent.instrument_id != self.instrument_id:
                continue
            if self._baseline is not None and cursor <= self._baseline:
                continue
            side = rec.snapshot.intent.side
            if side == Side.BUY:
                if running_inv == ZERO:
                    lot_open_time = parse_utc(ev.event_time)
                    lot_open_price = ev.fill_price if ev.fill_price is not None else rec.snapshot.intent.limit_price
                running_inv += delta
            else:
                running_inv = max(ZERO, running_inv - delta)
                if running_inv == ZERO:
                    lot_open_time = None
                    lot_open_price = None
        if running_inv == expected_inventory and lot_open_time is not None:
            return lot_open_time, lot_open_price
        return None, None

    def _restore_subclass_checkpoint(self, state: Any, payload: dict[str, Any], broker: Any, store: Any) -> None:
        """Restore FundingSensitiveGrid explicit decision state with legacy checkpoint fallback."""
        sub = payload.get("subclass_state")
        inv = self.account.inventory if self.account is not None else ZERO
        if isinstance(sub, dict):
            raw_t = sub.get("entry_time_utc")
            raw_p = sub.get("entry_price_dec")
            self.entry_time_utc = parse_utc(raw_t) if raw_t is not None else None
            self.entry_price_dec = D(str(raw_p)) if raw_p is not None else None
            self.entry_time_unrecoverable = bool(sub.get("entry_time_unrecoverable", False))
            self.buy_blocked = bool(sub.get("buy_blocked", False))
            self.last_funding_bps_h = float(sub.get("last_funding_bps_h", 0.0))
            restored_reasons = [str(r) for r in sub.get("exit_reasons", [])]
            self.exit_reasons = restored_reasons
            self._recorded_exit_reasons = set(restored_reasons)
            self.funding_exit_triggers = int(sub.get("funding_exit_triggers", 0))
            self.blocked_hours = int(sub.get("blocked_hours", 0))
            self.total_observed_hours = int(sub.get("total_observed_hours", 0))
            if inv == ZERO:
                self.entry_time_utc = None
                self.entry_price_dec = None
                self.entry_time_unrecoverable = False
            elif self.entry_time_utc is None:
                rec_t, rec_p = self._recover_entry_from_authoritative_events(broker, store, inv)
                if rec_t is not None:
                    self.entry_time_utc = rec_t
                    if self.entry_price_dec is None:
                        self.entry_price_dec = rec_p
                    self.entry_time_unrecoverable = False
                else:
                    self.entry_time_unrecoverable = True
            return

        # Legacy checkpoint without subclass_state: never fabricate current time or zero age.
        if inv == ZERO:
            self.entry_time_utc = None
            self.entry_price_dec = None
            self.entry_time_unrecoverable = False
        else:
            rec_t, rec_p = self._recover_entry_from_authoritative_events(broker, store, inv)
            if rec_t is not None:
                self.entry_time_utc = rec_t
                self.entry_price_dec = rec_p
                self.entry_time_unrecoverable = False
            else:
                self.entry_time_utc = None
                self.entry_price_dec = None
                self.entry_time_unrecoverable = True

    def deployment(self) -> dict[str, Any]:
        extra: dict[str, Any] = {
            "r10_strategy": "B_FUNDING_SENSITIVE_GRID",
            "funding_policy": asdict(self.funding_policy),
        }
        if self.use_f11_limit_exit:
            extra["use_f11_limit_exit"] = True
        return super().deployment() | extra

    def _grid_gross_spread_bps(self) -> float:
        if len(self.prices) < 2 or self.prices[0] <= 0:
            return 0.0
        return (float(self.prices[-1]) - float(self.prices[0])) / float(self.prices[0]) * 10_000.0

    def _roundtrip_tx_bps(self) -> float:
        return 2.0 * (float(self.config.fee_bps) + float(self.config.slippage_bps))

    def _evaluate_entry_block(self, obs_utc: datetime) -> tuple[bool, float, float, str]:
        p = self.funding_policy
        est_bps_h, n_ev, _ = causal_funding_rate_bps_h(
            self._all_funding_events,
            obs_utc,
            lookback_hours=p.lookback_hours,
            conservative=p.conservative,
            _sorted_times=self._all_funding_times,
        )
        gross_bps = self._grid_gross_spread_bps()
        tx_bps = self._roundtrip_tx_bps()
        expected_funding_bps = max(0.0, est_bps_h) * float(p.expected_hold_hours)
        expected_net_margin_bps = gross_bps - tx_bps - expected_funding_bps

        if est_bps_h > p.entry_max_bps_h:
            return True, est_bps_h, expected_net_margin_bps, "HIGH_FUNDING_RATE"
        if expected_net_margin_bps < p.min_net_margin_bps:
            return True, est_bps_h, expected_net_margin_bps, "INSUFFICIENT_NET_MARGIN"
        return False, est_bps_h, expected_net_margin_bps, "CLEAR"

    def _propose(self, side, target, quantity, level=-1, market=False):
        if side == Side.BUY and self.buy_blocked:
            return None
        return super()._propose(side, target, quantity, level, market)

    def _accept_confirmation(self, record, account, *, defer_decision=False):
        updated = record.snapshot
        order_id = updated.intent.client_order_id
        old = self.orders[order_id]
        delta = updated.cumulative_filled_quantity - old.cumulative_filled_quantity
        if delta > ZERO:
            if updated.intent.side == Side.BUY:
                if self.entry_time_utc is None:
                    self.entry_time_utc = parse_utc(record.observed_at)
                    self.entry_price_dec = updated.intent.limit_price or self.price
                    self.entry_time_unrecoverable = False
            elif account.inventory == ZERO:
                self.entry_time_utc = None
                self.entry_price_dec = None
                self.entry_time_unrecoverable = False
        return super()._accept_confirmation(record, account, defer_decision=defer_decision)

    def _observe(self, event, account):
        # Compute causal observation time strictly BEFORE super()._observe so initial
        # sequence==1 seeding obeys the causal funding gate.
        obs_utc = parse_utc(event["time"]) + timedelta(hours=1, microseconds=-1)
        blocked, est_bps_h, net_margin_bps, block_reason = self._evaluate_entry_block(obs_utc)
        prev_blocked = self.buy_blocked
        self.buy_blocked = blocked
        self.last_funding_bps_h = est_bps_h
        self.total_observed_hours += 1
        if blocked:
            self.blocked_hours += 1
        if blocked != prev_blocked or (self.sequence == 0 and blocked):
            self.gate_events.append(
                {
                    "time": obs_utc.isoformat(),
                    "action": "BLOCK_BUY" if blocked else "UNBLOCK_BUY",
                    "reason": block_reason,
                    "est_funding_bps_h": round(est_bps_h, 4),
                    "expected_net_margin_bps": round(net_margin_bps, 2),
                }
            )

        super()._observe(event, account)

        if self.use_f11_limit_exit:
            if self.closed_by_breakout and self.reason and self.reason not in self._recorded_exit_reasons:
                self._recorded_exit_reasons.add(self.reason)
                self.exit_reasons.append(self.reason)
            elif (
                self.closed_for_limit_exit
                and not (D(self.prices[0]) <= self.price <= D(self.prices[-1]))
                and "BREAKOUT" not in self._recorded_exit_reasons
            ):
                self._recorded_exit_reasons.add("BREAKOUT")
                self.exit_reasons.append("BREAKOUT")

        if self.state == "RUNNING":
            # Cancel working BUY orders if funding gate is active.
            if self.buy_blocked:
                for order in self.active:
                    if order.intent.side == Side.BUY:
                        self.cancellations.add(order.intent.client_order_id)
            else:
                # Re-arm buy order if previously blocked and currently flat with no active orders.
                if self.account.inventory == ZERO and not self.active and self.pending_rebuild is None:
                    self._seed()

        # Optional causal exit timing gate while holding inventory (active in both RUNNING and PAUSED states,
        # mirroring InventoryRiskGrid so stranded breakout inventory can also exit on high funding bleed).
        p = self.funding_policy
        if (
            self.state in {"RUNNING", "PAUSED"}
            and p.exit_mode != "none"
            and self.account.inventory > ZERO
            and self.entry_time_utc is not None
        ):
            hold_h = max(0.0, (obs_utc - self.entry_time_utc).total_seconds() / 3600.0)
            # Compute causal cumulative funding already paid on this open lot (events strictly <= obs_utc).
            lo = bisect_right(self._all_funding_times, self.entry_time_utc)
            hi = bisect_right(self._all_funding_times, obs_utc, lo)
            paid_bps = sum(
                float(self._all_funding_events[i][2]) * 10_000.0
                for i in range(lo, hi)
            )
            exit_reason = None
            if p.exit_mode == "funding_bleed":
                if p.exit_bps_h is not None and est_bps_h >= p.exit_bps_h and paid_bps > 0:
                    exit_reason = "FUNDING_SPIKE_EXIT"
                elif p.exit_bps_h is not None and est_bps_h >= p.exit_bps_h * 2.0:
                    exit_reason = "FUNDING_EXTREME_EXIT"
                elif p.max_hold_hours is not None and hold_h >= p.max_hold_hours and est_bps_h > 0:
                    exit_reason = "FUNDING_AGE_EXIT"
            elif p.exit_mode == "net_margin":
                gross_bps = self._grid_gross_spread_bps()
                tx_bps = self._roundtrip_tx_bps()
                remaining_margin = gross_bps - tx_bps - paid_bps - max(0.0, est_bps_h) * 24.0
                if remaining_margin <= 0.0:
                    exit_reason = "FUNDING_MARGIN_EXHAUSTED"
                elif p.max_hold_hours is not None and hold_h >= p.max_hold_hours and est_bps_h > 0:
                    exit_reason = "FUNDING_AGE_EXIT"
            if exit_reason is not None:
                if self.use_f11_limit_exit:
                    if exit_reason not in self._recorded_exit_reasons:
                        self._recorded_exit_reasons.add(exit_reason)
                        self.exit_reasons.append(exit_reason)
                        self.funding_exit_triggers += 1
                        self.gate_events.append(
                            {
                                "time": obs_utc.isoformat(),
                                "action": "EXIT_POSITION_F11",
                                "reason": exit_reason,
                                "already_in_limit_exit": bool(self.closed_for_limit_exit),
                                "hold_hours": round(hold_h, 2),
                                "paid_funding_bps": round(paid_bps, 2),
                                "est_funding_bps_h": round(est_bps_h, 4),
                            }
                        )
                    # Route into the single authoritative F11 reduce-only LIMIT exit lifecycle
                    # without duplicating sell orders or overwriting an already-active F11 exit.
                    if not self.closed_for_limit_exit:
                        self._risk(exit_reason, exit_position=False)
                        self._settle()
                else:
                    self.funding_exit_triggers += 1
                    self.gate_events.append(
                        {
                            "time": obs_utc.isoformat(),
                            "action": "EXIT_POSITION",
                            "reason": exit_reason,
                            "hold_hours": round(hold_h, 2),
                            "paid_funding_bps": round(paid_bps, 2),
                            "est_funding_bps_h": round(est_bps_h, 4),
                        }
                    )
                    self._risk(exit_reason, exit_position=True)
                    self._settle()


# ---------------------------------------------------------------------------
# Strategy C: Volatility-Adaptive Grid (strictly causal realized volatility)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VolatilityPolicy:
    candidate_id: str = "C5"
    vol_window_hours: int = 48
    k_mult: float = 3.0
    horizon_hours: float = 1.0
    min_half_width: float = 0.006
    max_half_width: float = 0.035
    extreme_vol_mult: float | None = 2.0
    allow_flat_rebuild: bool = True
    min_rebuild_interval: int = 24
    rebuild_width_delta: float = 0.25
    reference_vol_1h: float = 0.00445  # Frozen from training window; never refit on OOS!

    def __post_init__(self) -> None:
        if self.vol_window_hours < 2:
            raise ValueError("vol_window_hours must be >= 2")
        if not (0 < self.min_half_width <= self.max_half_width < 0.25):
            raise ValueError("invalid half-width bounds")
        if self.reference_vol_1h <= 0:
            raise ValueError("reference_vol_1h must be positive")


def causal_realized_vol_1h(closes: list[float], window: int, fallback_vol: float) -> float:
    """Hourly log-return standard deviation using only strictly past closes."""
    if len(closes) < 3:
        return fallback_vol
    tail = closes[-(window + 1) :]
    arr = np.asarray(tail, dtype=float)
    if np.any(arr <= 0):
        return fallback_vol
    rets = np.diff(np.log(arr))
    if len(rets) < 2:
        return fallback_vol
    vol = float(np.std(rets, ddof=1))
    return vol if math.isfinite(vol) and vol > 1e-8 else fallback_vol


def adaptive_half_width(vol_1h: float, policy: VolatilityPolicy) -> float:
    raw = policy.k_mult * vol_1h * math.sqrt(policy.horizon_hours)
    return float(max(policy.min_half_width, min(policy.max_half_width, raw)))


class VolatilityAdaptiveGrid(GridExecution):
    """GridExecution subclass adapting grid width and entry gating to causal RV."""

    def __init__(
        self,
        *args: Any,
        vol_policy: VolatilityPolicy,
        warmup_closes: list[float],
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.vol_policy = vol_policy
        # Keep only the necessary trailing warmup closes (strictly pre-window).
        keep = max(vol_policy.vol_window_hours + 2, 10)
        self.close_history: list[float] = [float(x) for x in warmup_closes[-keep:]]
        self.current_vol_1h = causal_realized_vol_1h(
            self.close_history, vol_policy.vol_window_hours, vol_policy.reference_vol_1h
        )
        self.current_half_width = adaptive_half_width(self.current_vol_1h, vol_policy)
        self.buy_blocked = False
        self.blocked_hours = 0
        self.total_observed_hours = 0
        self.adaptive_rebuilds = 0
        self.vol_events: list[dict[str, Any]] = []

    def _stage(self) -> VolatilityAdaptiveGrid:
        staged = super()._stage()
        staged.close_history = self.close_history.copy()
        staged.vol_events = self.vol_events.copy()
        return staged

    def deployment(self) -> dict[str, Any]:
        return super().deployment() | {
            "r10_strategy": "C_VOL_ADAPTIVE_GRID",
            "vol_policy": asdict(self.vol_policy),
        }

    def _propose(self, side, target, quantity, level=-1, market=False):
        if side == Side.BUY and self.buy_blocked:
            return None
        return super()._propose(side, target, quantity, level, market)

    def _is_extreme_vol(self, vol_1h: float) -> bool:
        mult = self.vol_policy.extreme_vol_mult
        if mult is None:
            return False
        return vol_1h > mult * self.vol_policy.reference_vol_1h

    def _observe(self, event, account):
        p = self.vol_policy
        # Pre-observation extreme volatility check using strictly available history
        # (including current completed bar close, which only affects orders for future bars).
        price_f = float(event["price"])
        if self.sequence > 0:
            self.close_history.append(price_f)
            if len(self.close_history) > p.vol_window_hours + 8:
                self.close_history = self.close_history[-(p.vol_window_hours + 8) :]
        self.current_vol_1h = causal_realized_vol_1h(
            self.close_history, p.vol_window_hours, p.reference_vol_1h
        )
        new_hw = adaptive_half_width(self.current_vol_1h, p)
        extreme = self._is_extreme_vol(self.current_vol_1h)
        prev_blocked = self.buy_blocked
        self.buy_blocked = extreme
        self.total_observed_hours += 1
        if extreme:
            self.blocked_hours += 1
        if extreme != prev_blocked or (self.sequence == 0 and extreme):
            self.vol_events.append(
                {
                    "sequence": self.sequence + 1,
                    "action": "EXTREME_VOL_BLOCK" if extreme else "VOL_NORMALIZED",
                    "vol_1h": round(self.current_vol_1h, 6),
                    "ref_vol_1h": round(p.reference_vol_1h, 6),
                    "target_half_width": round(new_hw, 5),
                }
            )

        super()._observe(event, account)

        if self.state != "RUNNING":
            return

        if self.buy_blocked:
            for order in self.active:
                if order.intent.side == Side.BUY:
                    self.cancellations.add(order.intent.client_order_id)
            return

        # Re-arm if unblocked and flat with no working orders.
        if self.account.inventory == ZERO and not self.active and self.pending_rebuild is None:
            self._seed()

        # Optional flat-only adaptive width rebuild.
        if (
            p.allow_flat_rebuild
            and self.account.inventory == ZERO
            and self.pending_rebuild is None
            and (self.sequence - self.last_rebuild) >= p.min_rebuild_interval
        ):
            width_rel_change = abs(new_hw - self.current_half_width) / max(1e-9, self.current_half_width)
            center_drift = abs(price_f - float(self.center)) / max(1e-9, float(self.center))
            if width_rel_change >= p.rebuild_width_delta or center_drift >= 0.5 * self.current_half_width:
                self.current_half_width = new_hw
                spacing = 2.0 * new_hw * price_f
                self.adaptive_rebuilds += 1
                self.vol_events.append(
                    {
                        "sequence": self.sequence,
                        "action": "ADAPTIVE_FLAT_REBUILD",
                        "center": round(price_f, 4),
                        "vol_1h": round(self.current_vol_1h, 6),
                        "half_width": round(new_hw, 5),
                    }
                )
                self._rebuild(price_f, spacing)


# ---------------------------------------------------------------------------
# R10-01: Historical Data Loading, Supplemental Public Fetch & Data Audit
# ---------------------------------------------------------------------------


def _http_get_json(url: str, timeout: int = 15) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": "FactorLab-P8-R10-Research/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_binance_usdm_klines_1h(
    symbol: str,
    start_utc: datetime,
    end_utc: datetime,
    *,
    endpoint: str = "klines",
) -> list[dict[str, Any]]:
    """Fetch complete 1h klines from Binance USDM public REST (read-only, no key)."""
    start_ms = int(start_utc.timestamp() * 1000)
    end_ms = int(end_utc.timestamp() * 1000) - 1
    rows: list[dict[str, Any]] = []
    cursor = start_ms
    while cursor <= end_ms:
        url = (
            f"https://fapi.binance.com/fapi/v1/{endpoint}"
            f"?symbol={symbol}&interval=1h&startTime={cursor}&endTime={end_ms}&limit=1500"
        )
        batch = _http_get_json(url)
        if not batch:
            break
        for k in batch:
            open_ms = int(k[0])
            close_ms = int(k[6])
            if open_ms < start_ms or open_ms > end_ms:
                continue
            rows.append(
                {
                    "open_time": pd.to_datetime(open_ms, unit="ms", utc=True),
                    "close_time": pd.to_datetime(close_ms, unit="ms", utc=True),
                    "open": str(k[1]),
                    "high": str(k[2]),
                    "low": str(k[3]),
                    "close": str(k[4]),
                    "volume": str(k[5]) if endpoint == "klines" else "0",
                }
            )
        last_open_ms = int(batch[-1][0])
        if last_open_ms < cursor:
            break
        cursor = last_open_ms + 3_600_000
        if len(batch) < 1500:
            break
    return rows


def fetch_binance_usdm_funding(
    symbol: str,
    start_utc: datetime,
    end_utc: datetime,
) -> list[dict[str, Any]]:
    """Fetch official Binance USDM fundingRate events (read-only, no key)."""
    start_ms = int(start_utc.timestamp() * 1000)
    end_ms = int(end_utc.timestamp() * 1000) - 1
    out: list[dict[str, Any]] = []
    cursor = start_ms
    while cursor <= end_ms:
        url = (
            f"https://fapi.binance.com/fapi/v1/fundingRate"
            f"?symbol={symbol}&startTime={cursor}&endTime={end_ms}&limit=1000"
        )
        batch = _http_get_json(url)
        if not batch:
            break
        for ev in batch:
            t_ms = int(ev["fundingTime"])
            if t_ms < start_ms or t_ms > end_ms:
                continue
            out.append(ev)
        last_ms = int(batch[-1]["fundingTime"])
        if last_ms < cursor or len(batch) < 1000:
            break
        cursor = last_ms + 1
    return out


def load_funding_csv(csv_path: Path) -> list[tuple[datetime, Decimal, Decimal, str]]:
    df = pd.read_csv(csv_path)
    events = []
    for row in df.itertuples(index=False):
        events.append((parse_utc(row.time), D(str(row.mark_price)), D(str(row.rate)), str(row.rate_type)))
    return sorted(events, key=lambda e: e[0])


def load_and_audit_r10_dataset(
    *,
    repo_root: Path,
    r04b_snapshot: Path,
    cache_dir: Path,
    allow_network_supplement: bool = True,
) -> tuple[list[dict[str, Any]], list[tuple[datetime, Decimal, Decimal, str]], dict[str, Any]]:
    """Load certified R04B + corroborated R07 + post-R07 continuous 1h dataset and audit it."""
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)

    # 1. Load certified R04B (2026-05-14 -> 2026-08-12, 2160h)
    r04b_bars, r04b_identity = load_hourly(r04b_snapshot)
    r04b_funding_path = repo_root / "docs" / "validation" / "p7-r09" / "R04B-base-baseline-funding-settlements.csv"
    r04b_funding = load_funding_csv(r04b_funding_path)

    # 2. Load/supplement R07 (2026-08-12 -> 2026-11-10, 2160h) & Post-R07 (2026-11-10 -> 2026-11-23, 312h)
    r07_ref_hourly_path = repo_root / "docs" / "validation" / "p7-r09" / "R07-base-baseline-hourly.csv"
    r07_funding_path = repo_root / "docs" / "validation" / "p7-r09" / "R07-base-baseline-funding-settlements.csv"
    r07_funding = load_funding_csv(r07_funding_path)

    supp_bars_path = cache_dir / "nvdausdt_1h_r07_post_20260812_20261123.parquet"
    supp_mark_path = cache_dir / "nvdausdt_mark_1h_20261110_20261123.parquet"
    supp_funding_path = cache_dir / "nvdausdt_funding_20261110_20261123.json"

    r07_start = datetime(2026, 8, 12, 0, 0, tzinfo=timezone.utc)
    r07_end = datetime(2026, 11, 10, 0, 0, tzinfo=timezone.utc)
    post_end = datetime(2026, 11, 23, 0, 0, tzinfo=timezone.utc)

    supp_bars_loaded = False
    supp_bars: list[dict[str, Any]] = []
    post_funding: list[tuple[datetime, Decimal, Decimal, str]] = []
    mark_price_diff_audit: dict[str, Any] = {"available": False}

    if not supp_bars_path.is_file() and allow_network_supplement:
        rows_1h = fetch_binance_usdm_klines_1h("NVDAUSDT", r07_start, post_end, endpoint="klines")
        if rows_1h:
            pd.DataFrame(rows_1h).to_parquet(supp_bars_path, index=False)
    if not supp_funding_path.is_file() and allow_network_supplement:
        raw_fund = fetch_binance_usdm_funding("NVDAUSDT", r07_end, post_end)
        supp_funding_path.write_text(json.dumps(raw_fund, indent=2), encoding="utf-8")
    if not supp_mark_path.is_file() and allow_network_supplement:
        rows_mark = fetch_binance_usdm_klines_1h("NVDAUSDT", r07_end, post_end, endpoint="markPriceKlines")
        if rows_mark:
            pd.DataFrame(rows_mark).to_parquet(supp_mark_path, index=False)

    r07_close_corroboration = {"checked": False}
    if supp_bars_path.is_file():
        df_supp = pd.read_parquet(supp_bars_path)
        validate_hourly_timeline(df_supp["open_time"], start=r07_start, end=post_end)
        # Cross-verify R07 segment against repository R07-base-baseline-hourly.csv (2160 closes).
        r07_ref = pd.read_csv(r07_ref_hourly_path)
        r07_ref["ts"] = pd.to_datetime(r07_ref["date"], utc=True)
        df_r07_part = df_supp[(df_supp["open_time"] >= r07_start) & (df_supp["open_time"] < r07_end)].reset_index(drop=True)
        if len(df_r07_part) == len(r07_ref):
            diffs = (df_r07_part["close"].astype(float) - r07_ref["close"].astype(float)).abs()
            max_abs_diff = float(diffs.max())
            exact_matches = int((diffs < 1e-9).sum())
            r07_close_corroboration = {
                "checked": True,
                "expected_bars": len(r07_ref),
                "matched_bars": exact_matches,
                "max_abs_close_diff": max_abs_diff,
                "exact_100pct_match": exact_matches == len(r07_ref),
            }
            if not r07_close_corroboration["exact_100pct_match"]:
                raise ValueError(f"R07 supplemented klines disagree with repository R07 close series (max diff={max_abs_diff})")
        for idx, row in enumerate(df_supp.itertuples(index=False)):
            o, h, l, c, v = (float(row.open), float(row.high), float(row.low), float(row.close), float(row.volume))
            if not (0 < l <= min(o, c) <= max(o, c) <= h) or v < 0:
                raise ValueError(f"Invalid OHLCV in supplemented bar at {row.open_time}")
            supp_bars.append(
                {
                    "id": f"hour-{len(r04b_bars) + idx}",
                    "time": pd.Timestamp(row.open_time).tz_convert("UTC").isoformat(),
                    "price": c,
                    "high": h,
                    "low": l,
                    "close": c,
                    "volume": int(D(str(row.volume))),
                    "completed_bar": True,
                }
            )
        supp_bars_loaded = True

    if supp_funding_path.is_file():
        raw_fund = json.loads(supp_funding_path.read_text(encoding="utf-8"))
        for ev in raw_fund:
            t_utc = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(milliseconds=int(ev["fundingTime"]))
            post_funding.append(
                (t_utc, D(str(ev["markPrice"])), D(str(ev["fundingRate"])), str(ev.get("rateType", "Regular")))
            )
        post_funding.sort(key=lambda e: e[0])

    if supp_mark_path.is_file() and supp_bars_path.is_file():
        df_m = pd.read_parquet(supp_mark_path)
        df_k = pd.read_parquet(supp_bars_path)
        merged_km = pd.merge(
            df_k[["open_time", "close"]].rename(columns={"close": "trade_close"}),
            df_m[["open_time", "close"]].rename(columns={"close": "mark_close"}),
            on="open_time",
            how="inner",
        )
        if not merged_km.empty:
            tc = merged_km["trade_close"].astype(float)
            mc = merged_km["mark_close"].astype(float)
            basis_bps = (tc - mc) / mc * 10_000.0
            mark_price_diff_audit = {
                "available": True,
                "window": ["2026-11-10T00:00:00+00:00", "2026-11-23T00:00:00+00:00"],
                "rows": len(merged_km),
                "mean_trade_minus_mark_bps": round(float(basis_bps.mean()), 4),
                "abs_p95_basis_bps": round(float(basis_bps.abs().quantile(0.95)), 4),
                "max_abs_basis_bps": round(float(basis_bps.abs().max()), 4),
            }

    all_bars = list(r04b_bars) + (supp_bars if supp_bars_loaded else [])
    # Deduplicate funding by (timestamp, rate_type)
    funding_map: dict[tuple[datetime, str], tuple[datetime, Decimal, Decimal, str]] = {}
    for ev in r04b_funding + r07_funding + post_funding:
        funding_map[(ev[0], ev[3])] = ev
    all_funding = sorted(funding_map.values(), key=lambda e: e[0])

    # Run strict continuity & alignment checks on the combined timeline
    bar_times = validate_hourly_timeline(
        [b["time"] for b in all_bars], start=parse_utc(r04b_identity["start"]), end=post_end,
    )
    expected_range = pd.date_range(bar_times[0], bar_times[-1], freq="h", tz="UTC")
    missing_bars = len(expected_range) - len(bar_times)
    duplicate_bars = int(bar_times.duplicated().sum())
    zero_vol_bars = int(sum(1 for b in all_bars if b["volume"] == 0))
    min_vol = int(min(b["volume"] for b in all_bars))
    p05_vol = float(np.quantile([b["volume"] for b in all_bars], 0.05))
    median_vol = float(np.median([b["volume"] for b in all_bars]))

    # Funding rate statistics & autocorrelation (critical for testing predictability!)
    f_rates_bps = np.asarray([float(e[2]) * 10_000.0 for e in all_funding], dtype=float)
    f_times = [e[0] for e in all_funding]
    bar_start = bar_times[0].to_pydatetime()
    bar_end_excl = (bar_times[-1] + pd.Timedelta(hours=1)).to_pydatetime()
    funding_in_range = [e for e in all_funding if bar_start <= e[0] < bar_end_excl]
    ac_lag1 = float(pd.Series(f_rates_bps).autocorr(lag=1)) if len(f_rates_bps) > 5 else 0.0
    ac_lag3 = float(pd.Series(f_rates_bps).autocorr(lag=3)) if len(f_rates_bps) > 5 else 0.0

    # Compare funding event markPrice vs closest completed 1h trade close
    bar_close_map = {parse_utc(b["time"]) + timedelta(hours=1): float(b["close"]) for b in all_bars}
    mark_vs_close_bps = []
    for t_ev, m_px, _, _ in funding_in_range:
        rounded_hr = t_ev.replace(minute=0, second=0, microsecond=0)
        if rounded_hr in bar_close_map:
            tc = bar_close_map[rounded_hr]
            mark_vs_close_bps.append((tc - float(m_px)) / float(m_px) * 10_000.0)

    # Check R08 Demo empirical funding vs Mainnet funding during Gen 1 window (2026-11-21T19:00Z -> 2026-11-22T17:00Z)
    r08_fills_path = repo_root / "docs" / "validation" / "p8-r08" / "fills_and_funding.csv"
    demo_vs_mainnet_funding: dict[str, Any] = {"available": False}
    if r08_fills_path.is_file():
        r08_df = pd.read_csv(r08_fills_path)
        r08_fund = r08_df[r08_df["record_type"] == "FUNDING_SETTLEMENT"]
        gen1_start = datetime(2026, 11, 21, 19, 0, tzinfo=timezone.utc)
        gen1_end = datetime(2026, 11, 22, 17, 0, tzinfo=timezone.utc)
        mainnet_gen1 = [e for e in all_funding if gen1_start <= e[0] <= gen1_end]
        demo_vs_mainnet_funding = {
            "available": True,
            "gen1_window_utc": [gen1_start.isoformat(), gen1_end.isoformat()],
            "demo_settlement_count": int(len(r08_fund)),
            "demo_total_funding_usdt": round(float(r08_fund["funding_usdt"].astype(float).sum()), 8),
            "demo_implied_hourly_bps": DEMO_OBSERVED_HOURLY_FUNDING_BPS,
            "mainnet_settlement_count": len(mainnet_gen1),
            "mainnet_rates_bps": [round(float(e[2]) * 10_000.0, 4) for e in mainnet_gen1],
            "mainnet_mean_rate_per_settlement_bps": round(
                float(np.mean([float(e[2]) * 10_000.0 for e in mainnet_gen1])), 4
            )
            if mainnet_gen1
            else 0.0,
            "finding": (
                "Official Binance Demo experienced hourly settlements averaging ~22.97 bps/h during weekend "
                "US-equity market closure, whereas Binance Mainnet settled every 8h with much smaller rates "
                "(mostly 0 bps over the weekend). Therefore funding studies MUST evaluate both actual Mainnet "
                "funding AND Demo/high-funding stress regimes separately."
            ),
        }

    closes_all = np.asarray([b["close"] for b in all_bars], dtype=float)
    log_rets_all = np.diff(np.log(closes_all))
    audit = {
        "execution_model": EXECUTION_MODEL_VERSION,
        "instrument_id": r04b_identity["instrument_id"],
        "interval": "1h",
        "coverage": {
            "start_utc": all_bars[0]["time"],
            "end_exclusive_utc": bar_end_excl.isoformat(),
            "total_bars": len(all_bars),
            "total_days": round(len(all_bars) / 24.0, 2),
            "segments": {
                "r04b_certified": {
                    "bars": len(r04b_bars),
                    "start_utc": r04b_bars[0]["time"],
                    "end_exclusive_utc": r04b_identity["end_exclusive"],
                    "bars_sha256": r04b_identity["bars_sha256"],
                    "funding_events": len(r04b_funding),
                },
                "r07_and_post_supplemented": {
                    "loaded": supp_bars_loaded,
                    "bars": len(supp_bars),
                    "start_utc": supp_bars[0]["time"] if supp_bars else None,
                    "end_exclusive_utc": bar_end_excl.isoformat() if supp_bars else None,
                    "parquet_sha256": sha256_file(supp_bars_path) if supp_bars_path.is_file() else None,
                    "r07_funding_events": len(r07_funding),
                    "post_r07_funding_events": len(post_funding),
                },
            },
        },
        "data_quality": {
            "missing_bars": missing_bars,
            "duplicate_timestamps": duplicate_bars,
            "invalid_ohlcv": 0,
            "zero_volume_bars": zero_vol_bars,
            "r07_close_corroboration": r07_close_corroboration,
        },
        "price_and_volatility": {
            "start_close": round(float(closes_all[0]), 4),
            "end_close": round(float(closes_all[-1]), 4),
            "min_close": round(float(closes_all.min()), 4),
            "max_close": round(float(closes_all.max()), 4),
            "full_period_return_pct": round(float((closes_all[-1] / closes_all[0] - 1.0) * 100.0), 4),
            "hourly_log_return_vol_pct": round(float(np.std(log_rets_all, ddof=1) * 100.0), 4),
            "mark_vs_trade_close_at_settlement": {
                "matched_settlements": len(mark_vs_close_bps),
                "mean_trade_minus_mark_bps": round(float(np.mean(mark_vs_close_bps)), 4) if mark_vs_close_bps else None,
                "p95_abs_diff_bps": round(float(np.quantile(np.abs(mark_vs_close_bps), 0.95)), 4) if mark_vs_close_bps else None,
                "max_abs_diff_bps": round(float(np.max(np.abs(mark_vs_close_bps))), 4) if mark_vs_close_bps else None,
            },
            "mark_price_klines_basis_post_r07": mark_price_diff_audit,
        },
        "liquidity_and_capacity": {
            "min_hourly_volume_units": min_vol,
            "p05_hourly_volume_units": round(p05_vol, 2),
            "median_hourly_volume_units": round(median_vol, 2),
            "participation_rate": 0.01,
            "min_participating_capacity_units": round(min_vol * 0.01, 4),
            "grid_order_unit": 0.04,
            "min_notional_usdt": 5.0,
            "note": (
                "At 1% hourly volume participation, even the 5th percentile hour exceeds the 0.04 unit "
                "grid order size; however, in May-Nov 2026 NVDAUSDT traded at 128-243 USDT, so 0.04 units "
                "requires 5.1-9.7 USDT collateral, exceeding the Demo 7.00 USDT cap in most historical weeks. "
                "Historical replay uses 10.00 USDT 1x risk budget, matching R08 Section 6.1."
            ),
        },
        "funding_alignment": {
            "total_events_in_range": len(funding_in_range),
            "first_event_utc": funding_in_range[0][0].isoformat() if funding_in_range else None,
            "last_event_utc": funding_in_range[-1][0].isoformat() if funding_in_range else None,
            "regular_count": sum(1 for e in funding_in_range if e[3] == "Regular"),
            "special_count": sum(1 for e in funding_in_range if e[3] != "Regular"),
            "mean_rate_bps": round(float(f_rates_bps.mean()), 4),
            "median_rate_bps": round(float(np.median(f_rates_bps)), 4),
            "p90_rate_bps": round(float(np.quantile(f_rates_bps, 0.90)), 4),
            "max_rate_bps": round(float(f_rates_bps.max()), 4),
            "min_rate_bps": round(float(f_rates_bps.min()), 4),
            "pct_positive": round(float(np.mean(f_rates_bps > 0) * 100.0), 2),
            "pct_zero": round(float(np.mean(f_rates_bps == 0) * 100.0), 2),
            "pct_negative": round(float(np.mean(f_rates_bps < 0) * 100.0), 2),
            "autocorr_lag1": round(ac_lag1, 4),
            "autocorr_lag3": round(ac_lag3, 4),
            "demo_vs_mainnet_comparison": demo_vs_mainnet_funding,
        },
        "lookahead_audit": {
            "paper_execution_lag": "Orders created on bar t can only fill on bars >= t+1 using bar close",
            "funding_settlement_tie_rule": "fill_time < funding_time (grid_funding.position_settlements)",
            "strategy_b_information_set": "Strictly funding_time <= bar_close_utc; verified by unit test",
            "strategy_c_information_set": "Strictly bar_close_time <= current_bar_close_utc; sigma_ref frozen on training fold",
            "lookahead_detected": False,
        },
    }
    return all_bars, funding_in_range, audit


# ---------------------------------------------------------------------------
# R10-02: Unified Paired Window Backtest & Full Attribution Accounting
# ---------------------------------------------------------------------------


def compute_funding_overlay_series(
    nav_df: pd.DataFrame,
    trades_df: pd.DataFrame,
    *,
    capital: float,
    funding_events: list[tuple[datetime, Decimal, Decimal, str]] | None = None,
    funding_scale: float = 1.0,
    constant_hourly_bps: float | None = None,
    regime_switch_bps: tuple[int, float, float] | None = None,
) -> tuple[float, float, pd.Series]:
    """Compute funding PnL (negative = cost to long), max drawdown with funding, and funded equity series.

    Supports:
    - Exact Binance historical settlement events (with optional ``funding_scale`` multiplier).
    - Constant hourly funding bps stress (e.g. Demo 22.9669 bps/h).
    - Dynamic regime-switch hourly funding ``(switch_hour, pre_bps_h, post_bps_h)``.
    """
    if constant_hourly_bps is not None or regime_switch_bps is not None:
        events = synthetic_hourly_funding_events(
            nav_df["date"], nav_df["close"], constant_hourly_bps=constant_hourly_bps,
            regime_switch_bps=regime_switch_bps,
        )
    else:
        events = [
            (t, px, rate * D(str(funding_scale)), kind) for t, px, rate, kind in (funding_events or [])
        ]
    flows = position_settlements(trades_df.to_dict("records"), events)
    bar_close_times = [parse_utc(d) + timedelta(hours=1, microseconds=-1) for d in nav_df["date"]]
    cum_vals = []
    f_idx = 0
    running = ZERO
    flow_times = [parse_utc(f["time"]) for f in flows]
    for obs in bar_close_times:
        while f_idx < len(flows) and flow_times[f_idx] <= obs:
            running += flows[f_idx]["funding"]
            f_idx += 1
        cum_vals.append(float(running))
    cum_funding = pd.Series(cum_vals, index=nav_df.index)

    funded_equity = nav_df["equity"].astype(float) + cum_funding
    peak = funded_equity.cummax().clip(lower=float(capital))
    dd_series = (1.0 - funded_equity / peak).clip(lower=0.0, upper=1.0)
    return float(cum_funding.iloc[-1]), float(dd_series.max()), funded_equity


def compute_equal_bh_benchmark(
    bars: list[dict[str, Any]],
    *,
    qty: float = 0.04,
    capital: float = 10.0,
    funding_events: list[tuple[datetime, Decimal, Decimal, str]] | None = None,
    funding_scale: float = 1.0,
    constant_hourly_bps: float | None = None,
    regime_switch_bps: tuple[int, float, float] | None = None,
) -> dict[str, float]:
    """Compute equal-quantity (0.04) Buy-and-Hold over the exact same window and funding regime."""
    closes = np.asarray([float(b["close"]) for b in bars], dtype=float)
    p0 = closes[0]
    gross_series = qty * (closes - p0)
    if constant_hourly_bps is not None or regime_switch_bps is not None:
        # B&H enters at the first close, just like its historical-event benchmark.
        # The coincident first settlement therefore precedes that entry.
        bh_nav = pd.DataFrame({"date": [b["time"] for b in bars], "close": closes,
                               "equity": capital + gross_series})
        entry = parse_utc(bars[0]["time"]) + timedelta(hours=1, microseconds=-1)
        bh_trades = pd.DataFrame([{"event_time": entry.isoformat(), "side": "BUY", "executed_shares": str(qty)}])
        _, _, funded = compute_funding_overlay_series(
            bh_nav, bh_trades, capital=capital, constant_hourly_bps=constant_hourly_bps,
            regime_switch_bps=regime_switch_bps,
        )
        cum_fund = funded.to_numpy() - bh_nav["equity"].to_numpy()
    else:
        events = funding_events or []
        bar_close_times = [parse_utc(b["time"]) + timedelta(hours=1, microseconds=-1) for b in bars]
        start_close = bar_close_times[0]
        cum_vals = []
        running = ZERO
        e_idx = 0
        # Assume BH enters at first bar close so funding applies to events strictly after start_close.
        for obs in bar_close_times:
            while e_idx < len(events) and events[e_idx][0] <= obs:
                if events[e_idx][0] > start_close:
                    running += funding_cashflow(D(str(qty)), events[e_idx][1], events[e_idx][2] * D(str(funding_scale)))
                e_idx += 1
            cum_vals.append(float(running))
        cum_fund = np.asarray(cum_vals, dtype=float)

    eq_ex = capital + gross_series
    eq_with = capital + gross_series + cum_fund
    dd_ex = float(np.max(np.clip(1.0 - eq_ex / np.maximum.accumulate(np.maximum(eq_ex, capital)), 0.0, 1.0)))
    dd_with = float(np.max(np.clip(1.0 - eq_with / np.maximum.accumulate(np.maximum(eq_with, capital)), 0.0, 1.0)))
    return {
        "bh_gross_pnl_usdt": float(gross_series[-1]),
        "bh_funding_pnl_usdt": float(cum_fund[-1]),
        "bh_net_pnl_with_funding_usdt": float(gross_series[-1] + cum_fund[-1]),
        "bh_return_ex_funding_pct": float(gross_series[-1] / capital * 100.0),
        "bh_return_with_funding_pct": float((gross_series[-1] + cum_fund[-1]) / capital * 100.0),
        "bh_max_drawdown_ex_funding_pct": dd_ex * 100.0,
        "bh_max_drawdown_with_funding_pct": dd_with * 100.0,
    }


def _fifo_holding_hours(
    fifo: dict[str, Any],
    trades_df: pd.DataFrame,
    window_end_utc: datetime,
) -> tuple[float, float]:
    """Quantity-weighted mean holding hours & max holding hours across matched + residual lots."""
    if trades_df.empty:
        return 0.0, 0.0
    by_id = {row.event_id: parse_utc(row.event_time) for row in trades_df.itertuples(index=False)}
    fragments: list[tuple[float, float]] = []
    for m in fifo.get("matches", []):
        h = max(0.0, (by_id[m["sell_event_id"]] - by_id[m["buy_event_id"]]).total_seconds() / 3600.0)
        fragments.append((float(m["quantity"]), h))
    cutoff = window_end_utc - timedelta(microseconds=1)
    for lot in fifo.get("lots", []):
        rem = float(lot["remaining"])
        if rem > 0:
            h = max(0.0, (cutoff - by_id[lot["event_id"]]).total_seconds() / 3600.0)
            fragments.append((rem, h))
    if not fragments:
        return 0.0, 0.0
    tot_q = sum(q for q, _ in fragments)
    mean_h = sum(q * h for q, h in fragments) / tot_q if tot_q > 0 else 0.0
    max_h = max(h for _, h in fragments)
    return mean_h, max_h


def run_paired_window(
    out_dir: Path,
    *,
    strategy_id: str,
    bars: list[dict[str, Any]],
    capital: Decimal,
    funding_events: list[tuple[datetime, Decimal, Decimal, str]] | None = None,
    funding_policy: FundingPolicy | None = None,
    vol_policy: VolatilityPolicy | None = None,
    warmup_closes: list[float] | None = None,
    funding_scale: float = 1.0,
    constant_hourly_bps: float | None = None,
    regime_switch_bps: tuple[int, float, float] | None = None,
    fee_rate: Decimal = PAPER_FEE_RATE,
    slippage: Decimal = PAPER_SLIPPAGE,
    participation: Decimal = D("0.01"),
    scenario: str | None = None,
    synthetic_capacity: Decimal | None = None,
    provenance: dict[str, Any] | None = None,
    account_id: str = "r10-paired",
    cleanup_dir: bool = False,
) -> dict[str, Any]:
    """Run one strategy arm on one window using the authoritative Paper engine and compute full metrics."""
    anchor = float(bars[0]["close"])
    rules = demo_grid_rules()
    fee_bps = float(fee_rate * 10_000)
    slip_bps = float(slippage * 10_000)

    # Determine initial grid width strictly causally.
    if strategy_id.startswith("C"):
        assert vol_policy is not None
        wc = list(warmup_closes or []) + [anchor]
        init_vol = causal_realized_vol_1h(wc, vol_policy.vol_window_hours, vol_policy.reference_vol_1h)
        init_hw = adaptive_half_width(init_vol, vol_policy)
        cfg = make_grid_config(
            anchor,
            half_width_pct=init_hw,
            max_cash_committed=float(capital),
            fee_bps=fee_bps,
            slippage_bps=slip_bps,
        )

        def grid_factory(*a, **kw):
            return VolatilityAdaptiveGrid(*a, vol_policy=vol_policy, warmup_closes=wc, **kw)

    elif strategy_id.startswith("B"):
        assert funding_policy is not None
        init_hw = 0.01
        cfg = make_grid_config(
            anchor,
            half_width_pct=init_hw,
            max_cash_committed=float(capital),
            fee_bps=fee_bps,
            slippage_bps=slip_bps,
        )
        # Build the exact funding stream visible causally during this window.
        if constant_hourly_bps is not None or regime_switch_bps is not None:
            # Synthesize hourly observed settlements at each bar close matching the stress schedule.
            synth_events: list[tuple[datetime, Decimal, Decimal, str]] = []
            # Include 24h pre-window warmup settlements at initial regime rate so t=0 forecast is realistic.
            start_dt = parse_utc(bars[0]["time"])
            init_bps = constant_hourly_bps if constant_hourly_bps is not None else regime_switch_bps[1]
            for h_back in range(24, 0, -1):
                t_pre = start_dt - timedelta(hours=h_back)
                synth_events.append((t_pre, D(str(anchor)), D(str(init_bps / 10_000.0)), "SyntheticHourly"))
            synth_events.extend(synthetic_hourly_funding_events(
                [b["time"] for b in bars], [b["close"] for b in bars],
                constant_hourly_bps=constant_hourly_bps, regime_switch_bps=regime_switch_bps,
            ))
            visible_funding = synth_events
        else:
            visible_funding = [
                (t, px, rate * D(str(funding_scale)), kind) for t, px, rate, kind in (funding_events or [])
            ]

        def grid_factory(*a, **kw):
            return FundingSensitiveGrid(*a, funding_policy=funding_policy, funding_events=visible_funding, **kw)

    else:
        init_hw = 0.01
        cfg = make_grid_config(
            anchor,
            half_width_pct=init_hw,
            max_cash_committed=float(capital),
            fee_bps=fee_bps,
            slippage_bps=slip_bps,
        )
        grid_factory = None

    if cleanup_dir:
        _, artifacts = run_experiment(
            out_dir,
            bars=bars,
            config=cfg,
            rules=rules,
            capital=capital,
            fee_rate=fee_rate,
            slippage=slippage,
            participation=participation,
            interval="1h",
            market=Market.BINANCE_USDM,
            instrument_id="NVDAUSDT",
            scenario=scenario,
            synthetic_capacity=synthetic_capacity,
            research_wal=True,
            account_id=account_id,
            grid_factory=grid_factory,
            provenance=provenance,
            in_memory_store=True,
            return_artifacts=True,
        )
        result = artifacts["result"]
        nav_df = artifacts["daily"]
        trades_df = artifacts["ledger"]
        closes = {
            datetime.fromisoformat(str(r["date"]).replace("Z", "+00:00")): D(str(r["close"]))
            for r in artifacts["rows"]
        }
        fifo_full = paper_fifo_attribute_events(
            result,
            artifacts["trades"],
            closes,
            artifacts["fill_events"],
            artifacts["state"],
            funding_events=artifacts["funding_events"] or None,
        )
        fifo = fifo_full["summary"]
        life = {"final_state": nav_df["state"].iloc[-1]}
    else:
        run_experiment(
            out_dir,
            bars=bars,
            config=cfg,
            rules=rules,
            capital=capital,
            fee_rate=fee_rate,
            slippage=slippage,
            participation=participation,
            interval="1h",
            market=Market.BINANCE_USDM,
            instrument_id="NVDAUSDT",
            scenario=scenario,
            synthetic_capacity=synthetic_capacity,
            research_wal=True,
            account_id=account_id,
            grid_factory=grid_factory,
            provenance=provenance,
        )
        fifo_full = paper_fifo_attribution(out_dir)
        fifo = fifo_full["summary"]
        life = lifecycle_metrics(out_dir)
        result = json.loads((out_dir / "result.json").read_text(encoding="utf-8"))
        nav_df = pd.read_csv(out_dir / "daily.csv")
        trades_df = pd.read_csv(out_dir / "trades.csv")

    if abs(float(fifo["reconciliation_residual"])) > 1e-8:
        raise ValueError(f"FIFO reconciliation residual {fifo['reconciliation_residual']} for {account_id}")

    cap_f = float(capital)
    window_start = parse_utc(bars[0]["time"])
    window_end = parse_utc(bars[-1]["time"]) + timedelta(hours=1)
    w_funding = [e for e in (funding_events or []) if window_start <= e[0] < window_end]

    funding_pnl, dd_with_funding, funded_equity = compute_funding_overlay_series(
        nav_df,
        trades_df,
        capital=cap_f,
        funding_events=w_funding,
        funding_scale=funding_scale,
        constant_hourly_bps=constant_hourly_bps,
        regime_switch_bps=regime_switch_bps,
    )
    bh = compute_equal_bh_benchmark(
        bars,
        qty=0.04,
        capital=cap_f,
        funding_events=w_funding,
        funding_scale=funding_scale,
        constant_hourly_bps=constant_hourly_bps,
        regime_switch_bps=regime_switch_bps,
    )

    fees_usdt = float(fifo["actual_fees"])
    slippage_usdt = float(fifo["actual_slippage"])
    trade_net_pnl = float(fifo["strategy_net_pnl"])
    trade_gross_pnl = trade_net_pnl + fees_usdt + slippage_usdt
    final_net_pnl = trade_net_pnl + funding_pnl
    unrealized_pnl = float(nav_df["unrealized"].iloc[-1])
    final_nav_ex_funding = float(nav_df["equity"].iloc[-1])
    final_nav_with_funding = float(funded_equity.iloc[-1])

    orders = result.get("orders", {})
    submitted_orders = len(orders)
    filled_orders = sum(1 for o in orders.values() if float(o.get("cumulative_filled_quantity", 0)) > 0)
    fill_rate = (filled_orders / submitted_orders) if submitted_orders > 0 else 0.0
    mean_hold_h, max_hold_h = _fifo_holding_hours(fifo_full, trades_df, window_end)

    traded_notional = float(trades_df["trade_notional"].sum()) if not trades_df.empty else 0.0
    turnover_ratio = traded_notional / cap_f
    paused_inv_hours = int((nav_df["inventory"].gt(0) & nav_df["state"].eq("PAUSED")).sum())
    holding_hours = int(nav_df["inventory"].gt(0).sum())

    metrics = {
        "strategy_id": strategy_id,
        "execution_model": EXECUTION_MODEL_VERSION,
        "initial_half_width_pct": round(init_hw * 100.0, 4),
        "capital_usdt": cap_f,
        "anchor_price": round(anchor, 4),
        "end_price": round(float(bars[-1]["close"]), 4),
        "underlying_return_pct": round((float(bars[-1]["close"]) / anchor - 1.0) * 100.0, 4),
        "trade_gross_pnl_usdt": round(trade_gross_pnl, 8),
        "fees_usdt": round(fees_usdt, 8),
        "slippage_usdt": round(slippage_usdt, 8),
        "trade_net_pnl_usdt": round(trade_net_pnl, 8),
        "funding_pnl_usdt": round(funding_pnl, 8),
        "final_net_pnl_usdt": round(final_net_pnl, 8),
        "risk_budget_return_ex_funding_pct": round(trade_net_pnl / cap_f * 100.0, 4),
        "risk_budget_return_with_funding_pct": round(final_net_pnl / cap_f * 100.0, 4),
        "final_nav_ex_funding_usdt": round(final_nav_ex_funding, 8),
        "final_nav_with_funding_usdt": round(final_nav_with_funding, 8),
        "unrealized_pnl_usdt": round(unrealized_pnl, 8),
        "max_drawdown_ex_funding_pct": round(float(nav_df["drawdown"].max()) * 100.0, 4),
        "max_drawdown_with_funding_pct": round(dd_with_funding * 100.0, 4),
        "submitted_orders": submitted_orders,
        "filled_orders": filled_orders,
        "fills_count": int(len(trades_df)),
        "order_fill_rate": round(fill_rate, 4),
        "grid_cycles": int(fifo["grid_cycles"]),
        "mean_holding_hours": round(mean_hold_h, 4),
        "max_holding_hours": round(max_hold_h, 4),
        "holding_hours": holding_hours,
        "holding_time_fraction": round(holding_hours / max(1, len(nav_df)), 4),
        "paused_with_inventory_hours": paused_inv_hours,
        "residual_inventory": float(fifo["residual_inventory"]),
        "traded_notional_usdt": round(traded_notional, 6),
        "turnover_ratio": round(turnover_ratio, 4),
        "mean_notional_usdt": round(float(nav_df["net_exposure"].mean()), 4),
        "max_notional_usdt": round(float(nav_df["net_exposure"].max()), 4),
        "max_committed_usdt": round(float(nav_df["committed"].max()), 4),
        "final_state": life["final_state"],
        "final_state_reason": str(nav_df["state_reason"].iloc[-1]) if pd.notna(nav_df["state_reason"].iloc[-1]) else "",
        "fifo_reconciliation_residual": float(fifo["reconciliation_residual"]),
        "bh_net_pnl_with_funding_usdt": round(bh["bh_net_pnl_with_funding_usdt"], 8),
        "bh_return_with_funding_pct": round(bh["bh_return_with_funding_pct"], 4),
        "excess_vs_equal_bh_usdt": round(final_net_pnl - bh["bh_net_pnl_with_funding_usdt"], 8),
        "excess_vs_equal_bh_pct": round(
            (final_net_pnl - bh["bh_net_pnl_with_funding_usdt"]) / cap_f * 100.0, 4
        ),
        "excess_vs_equal_bh_ex_funding_usdt": round(trade_net_pnl - bh["bh_gross_pnl_usdt"], 8),
    }

    if cleanup_dir and out_dir.exists():
        for p in sorted(out_dir.rglob("*"), reverse=True):
            if p.is_file():
                p.unlink()
            elif p.is_dir():
                p.rmdir()
        out_dir.rmdir()
    return metrics


def run_paired_window_multi_overlay(
    out_dir: Path,
    *,
    strategy_id: str,
    bars: list[dict[str, Any]],
    capital: Decimal,
    overlays: list[tuple[str, float, float | None, tuple[int, float, float] | None]],
    funding_events: list[tuple[datetime, Decimal, Decimal, str]] | None = None,
    vol_policy: VolatilityPolicy | None = None,
    warmup_closes: list[float] | None = None,
    fee_rate: Decimal = PAPER_FEE_RATE,
    slippage: Decimal = PAPER_SLIPPAGE,
    participation: Decimal = D("0.01"),
    scenario: str | None = None,
    synthetic_capacity: Decimal | None = None,
    provenance: dict[str, Any] | None = None,
    account_id: str = "r10-multi",
    cleanup_dir: bool = True,
) -> dict[str, dict[str, Any]]:
    """Run one funding-independent arm (Strategy A or C) once and evaluate multiple funding overlays identically."""
    if strategy_id.startswith("B"):
        raise ValueError("Funding-sensitive Strategy B depends on funding rates and cannot reuse one ledger across regimes")
    anchor = float(bars[0]["close"])
    rules = demo_grid_rules()
    fee_bps = float(fee_rate * 10_000)
    slip_bps = float(slippage * 10_000)

    if strategy_id.startswith("C"):
        assert vol_policy is not None
        wc = list(warmup_closes or []) + [anchor]
        init_vol = causal_realized_vol_1h(wc, vol_policy.vol_window_hours, vol_policy.reference_vol_1h)
        init_hw = adaptive_half_width(init_vol, vol_policy)
        cfg = make_grid_config(
            anchor, half_width_pct=init_hw, max_cash_committed=float(capital),
            fee_bps=fee_bps, slippage_bps=slip_bps,
        )

        def grid_factory(*a, **kw):
            return VolatilityAdaptiveGrid(*a, vol_policy=vol_policy, warmup_closes=wc, **kw)
    else:
        init_hw = 0.01
        cfg = make_grid_config(
            anchor, half_width_pct=init_hw, max_cash_committed=float(capital),
            fee_bps=fee_bps, slippage_bps=slip_bps,
        )
        grid_factory = None

    if cleanup_dir:
        _, artifacts = run_experiment(
            out_dir,
            bars=bars,
            config=cfg,
            rules=rules,
            capital=capital,
            fee_rate=fee_rate,
            slippage=slippage,
            participation=participation,
            interval="1h",
            market=Market.BINANCE_USDM,
            instrument_id="NVDAUSDT",
            scenario=scenario,
            synthetic_capacity=synthetic_capacity,
            research_wal=True,
            account_id=account_id,
            grid_factory=grid_factory,
            provenance=provenance,
            in_memory_store=True,
            return_artifacts=True,
        )
        result = artifacts["result"]
        nav_df = artifacts["daily"]
        trades_df = artifacts["ledger"]
        closes = {
            datetime.fromisoformat(str(r["date"]).replace("Z", "+00:00")): D(str(r["close"]))
            for r in artifacts["rows"]
        }
        fifo_full = paper_fifo_attribute_events(
            result,
            artifacts["trades"],
            closes,
            artifacts["fill_events"],
            artifacts["state"],
            funding_events=artifacts["funding_events"] or None,
        )
        fifo = fifo_full["summary"]
        life = {"final_state": nav_df["state"].iloc[-1]}
    else:
        run_experiment(
            out_dir,
            bars=bars,
            config=cfg,
            rules=rules,
            capital=capital,
            fee_rate=fee_rate,
            slippage=slippage,
            participation=participation,
            interval="1h",
            market=Market.BINANCE_USDM,
            instrument_id="NVDAUSDT",
            scenario=scenario,
            synthetic_capacity=synthetic_capacity,
            research_wal=True,
            account_id=account_id,
            grid_factory=grid_factory,
            provenance=provenance,
        )
        fifo_full = paper_fifo_attribution(out_dir)
        fifo = fifo_full["summary"]
        life = lifecycle_metrics(out_dir)
        result = json.loads((out_dir / "result.json").read_text(encoding="utf-8"))
        nav_df = pd.read_csv(out_dir / "daily.csv")
        trades_df = pd.read_csv(out_dir / "trades.csv")

    if abs(float(fifo["reconciliation_residual"])) > 1e-8:
        raise ValueError(f"FIFO reconciliation residual {fifo['reconciliation_residual']} for {account_id}")

    cap_f = float(capital)
    window_start = parse_utc(bars[0]["time"])
    window_end = parse_utc(bars[-1]["time"]) + timedelta(hours=1)
    w_funding = [e for e in (funding_events or []) if window_start <= e[0] < window_end]

    fees_usdt = float(fifo["actual_fees"])
    slippage_usdt = float(fifo["actual_slippage"])
    trade_net_pnl = float(fifo["strategy_net_pnl"])
    trade_gross_pnl = trade_net_pnl + fees_usdt + slippage_usdt
    unrealized_pnl = float(nav_df["unrealized"].iloc[-1])
    final_nav_ex_funding = float(nav_df["equity"].iloc[-1])

    orders = result.get("orders", {})
    submitted_orders = len(orders)
    filled_orders = sum(1 for o in orders.values() if float(o.get("cumulative_filled_quantity", 0)) > 0)
    fill_rate = (filled_orders / submitted_orders) if submitted_orders > 0 else 0.0
    mean_hold_h, max_hold_h = _fifo_holding_hours(fifo_full, trades_df, window_end)
    traded_notional = float(trades_df["trade_notional"].sum()) if not trades_df.empty else 0.0
    turnover_ratio = traded_notional / cap_f
    paused_inv_hours = int((nav_df["inventory"].gt(0) & nav_df["state"].eq("PAUSED")).sum())
    holding_hours = int(nav_df["inventory"].gt(0).sum())

    base_metrics = {
        "strategy_id": strategy_id,
        "execution_model": EXECUTION_MODEL_VERSION,
        "initial_half_width_pct": round(init_hw * 100.0, 4),
        "capital_usdt": cap_f,
        "anchor_price": round(anchor, 4),
        "end_price": round(float(bars[-1]["close"]), 4),
        "underlying_return_pct": round((float(bars[-1]["close"]) / anchor - 1.0) * 100.0, 4),
        "trade_gross_pnl_usdt": round(trade_gross_pnl, 8),
        "fees_usdt": round(fees_usdt, 8),
        "slippage_usdt": round(slippage_usdt, 8),
        "trade_net_pnl_usdt": round(trade_net_pnl, 8),
        "risk_budget_return_ex_funding_pct": round(trade_net_pnl / cap_f * 100.0, 4),
        "final_nav_ex_funding_usdt": round(final_nav_ex_funding, 8),
        "unrealized_pnl_usdt": round(unrealized_pnl, 8),
        "max_drawdown_ex_funding_pct": round(float(nav_df["drawdown"].max()) * 100.0, 4),
        "submitted_orders": submitted_orders,
        "filled_orders": filled_orders,
        "fills_count": int(len(trades_df)),
        "order_fill_rate": round(fill_rate, 4),
        "grid_cycles": int(fifo["grid_cycles"]),
        "mean_holding_hours": round(mean_hold_h, 4),
        "max_holding_hours": round(max_hold_h, 4),
        "holding_hours": holding_hours,
        "holding_time_fraction": round(holding_hours / max(1, len(nav_df)), 4),
        "paused_with_inventory_hours": paused_inv_hours,
        "residual_inventory": float(fifo["residual_inventory"]),
        "traded_notional_usdt": round(traded_notional, 6),
        "turnover_ratio": round(turnover_ratio, 4),
        "mean_notional_usdt": round(float(nav_df["net_exposure"].mean()), 4),
        "max_notional_usdt": round(float(nav_df["net_exposure"].max()), 4),
        "max_committed_usdt": round(float(nav_df["committed"].max()), 4),
        "final_state": life["final_state"],
        "final_state_reason": str(nav_df["state_reason"].iloc[-1]) if pd.notna(nav_df["state_reason"].iloc[-1]) else "",
        "fifo_reconciliation_residual": float(fifo["reconciliation_residual"]),
    }

    out: dict[str, dict[str, Any]] = {}
    for ov_key, f_scale, const_bps, sw_bps in overlays:
        funding_pnl, dd_with_funding, funded_equity = compute_funding_overlay_series(
            nav_df, trades_df, capital=cap_f, funding_events=w_funding,
            funding_scale=f_scale, constant_hourly_bps=const_bps, regime_switch_bps=sw_bps,
        )
        bh = compute_equal_bh_benchmark(
            bars, qty=0.04, capital=cap_f, funding_events=w_funding,
            funding_scale=f_scale, constant_hourly_bps=const_bps, regime_switch_bps=sw_bps,
        )
        final_net_pnl = trade_net_pnl + funding_pnl
        out[ov_key] = dict(
            base_metrics,
            funding_pnl_usdt=round(funding_pnl, 8),
            final_net_pnl_usdt=round(final_net_pnl, 8),
            risk_budget_return_with_funding_pct=round(final_net_pnl / cap_f * 100.0, 4),
            final_nav_with_funding_usdt=round(float(funded_equity.iloc[-1]), 8),
            max_drawdown_with_funding_pct=round(dd_with_funding * 100.0, 4),
            bh_net_pnl_with_funding_usdt=round(bh["bh_net_pnl_with_funding_usdt"], 8),
            bh_return_with_funding_pct=round(bh["bh_return_with_funding_pct"], 4),
            excess_vs_equal_bh_usdt=round(final_net_pnl - bh["bh_net_pnl_with_funding_usdt"], 8),
            excess_vs_equal_bh_pct=round((final_net_pnl - bh["bh_net_pnl_with_funding_usdt"]) / cap_f * 100.0, 4),
            excess_vs_equal_bh_ex_funding_usdt=round(trade_net_pnl - bh["bh_gross_pnl_usdt"], 8),
        )

    if cleanup_dir and out_dir.exists():
        for p in sorted(out_dir.rglob("*"), reverse=True):
            if p.is_file():
                p.unlink()
            elif p.is_dir():
                p.rmdir()
        out_dir.rmdir()
    return out
