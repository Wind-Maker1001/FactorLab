"""Offline F11 (breakout reduce-only LIMIT exit) and F08 (post-flat Grid rotation)
lifecycle execution engine built strictly over the existing FactorLab Paper/Grid stack.

Reuses:
- ``factorlab.strategies.grid_execution.GridExecution``
- ``factorlab.trading.paper.PaperBroker``
- ``factorlab.trading.control.TradingController``
- ``factorlab.trading.storage.PaperStore``
- ``factorlab.trading.execution.execution_candidate``
- ``factorlab.experiments.grid_fifo.attribute_events``
- ``factorlab.experiments.grid_funding.cashflow``

No live network IO, no second independent trading engine, and explicit separation
between observed Demo event replay and conditional offline execution assumptions.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_EVEN
from pathlib import Path
from typing import Any

import pandas as pd

from factorlab.experiments.grid_fifo import attribute_events
from factorlab.experiments.grid_funding import cashflow as funding_cashflow
from factorlab.integrations.binance_futures_trading.demo_runtime import grid_deployment_id
from factorlab.integrations.binance_futures_trading.rules import common_step
from factorlab.strategies.grid import GridConfig
from factorlab.strategies.grid_execution import GridExecution, GridRules, project_paper_account
from factorlab.trading.contracts import (
    FundingEvent,
    Market,
    OrderEvent,
    OrderIntent,
    OrderStatus,
    OrderType,
    Side,
    TERMINAL,
    ZERO,
    funding_event_id,
)
from factorlab.trading.control import RiskLimits, RiskRejected, TradingController
from factorlab.trading.execution import execution_candidate
from factorlab.trading.paper import InsufficientCollateral, Instrument, PaperBroker, Quote
from factorlab.trading.storage import PaperStore

D = Decimal
USDT_QUANTUM = D("0.00000001")
DEFAULT_DEMO_WALLET = D("5000.00000000")
DEFAULT_STRATEGY_CAP = D("7.00000000")
DEFAULT_PAPER_FEE_RATE = D("0.0003")
DEFAULT_PAPER_SLIPPAGE = D("0.0002")

MODEL_BASELINE = "NO_F11_NO_F08_STRANDED_POSITION_BASELINE"
MODEL_F11_ONLY = "F11_EXIT_ONLY_NO_ROTATION"
MODEL_F11_F08 = "F11_F08_FULL_LIFECYCLE"


@dataclass(frozen=True)
class DemoGuardrails:
    """Explicit Binance USD-M Demo contract & continuous-authorization guardrails."""

    min_notional: Decimal = D("5")
    max_notional: Decimal = D("7")
    max_quantity: Decimal = D("0.04")
    max_open_orders: int = 1
    percent_price_down: Decimal = D("0.97")
    percent_price_up: Decimal = D("1.03")
    exit_reprice_seconds: int = 180
    exit_max_attempts: int = 5
    total_orders: int = 100
    total_notional: Decimal = D("700")
    window_orders: int = 4
    window_seconds: int = 10800
    max_loss: Decimal | None = D("1")
    enable_f11: bool = True
    enable_f08: bool = True
    enforce_min_notional_on_exit: bool = True
    enforce_max_notional_on_exit: bool = True
    enforce_percent_price: bool = True

    @property
    def model_label(self) -> str:
        if not self.enable_f11 and not self.enable_f08:
            return MODEL_BASELINE
        if self.enable_f11 and not self.enable_f08:
            return MODEL_F11_ONLY
        return MODEL_F11_F08


class _CampaignPaperBroker(PaperBroker):
    """Keep simulated POST consumption in the account snapshot across Grid generations."""

    def __init__(self, *args, guardrails: DemoGuardrails, **kwargs):
        super().__init__(*args, **kwargs)
        self.guardrails = guardrails

    @property
    def campaign(self) -> dict[str, Any]:
        return self.state.runtime["offline_campaign"]

    def set_quote(self, quote: Quote) -> None:
        super().set_quote(quote)
        campaign = self.state.runtime.get("offline_campaign")
        if campaign is None:
            campaign = {"expires_at": quote.observed_at, "window_posts": 0, "posts": 0, "notional": ZERO}
            self.state.runtime["offline_campaign"] = campaign
        if quote.observed_at >= campaign["expires_at"]:
            campaign["expires_at"] = quote.observed_at + timedelta(seconds=self.guardrails.window_seconds)
            campaign["window_posts"] = 0

    def global_post_blocked(self, notional: Decimal) -> bool:
        return (
            self.campaign["posts"] >= self.guardrails.total_orders
            or self.campaign["notional"] + notional > self.guardrails.total_notional
        )

    def window_post_blocked(self) -> bool:
        return self.campaign["window_posts"] >= self.guardrails.window_orders

    def submit_order(self, intent: OrderIntent) -> OrderEvent:
        # Controller admission has passed. Persist consumption in the same
        # transaction as the dispatch and its receipt, whatever the order status.
        campaign = self.campaign
        campaign["window_posts"] += 1
        campaign["posts"] += 1
        campaign["notional"] += intent.quantity * intent.limit_price
        return super().submit_order(intent)


@dataclass(frozen=True)
class ExecutionAssumptions:
    """Explicit conditional execution assumptions for offline bar/close replay.

    Hourly OHLC closes and synthetic close-only paths are NOT real executable
    exchange orderbooks. Every offline simulation explicitly declares its
    fill/liquidity/delay/fault assumption profile here.
    """

    profile_name: str
    assumption_summary: str
    evidence_label: str = "CONDITIONAL_EXECUTION_ASSUMPTION"
    fee_rate: Decimal = DEFAULT_PAPER_FEE_RATE
    slippage: Decimal = DEFAULT_PAPER_SLIPPAGE
    half_spread_bps: Decimal = D("0")
    capacity_per_bar: Decimal | None = D("0.10")
    use_bar_volume: bool = False
    participation: Decimal = D("1")
    exit_marketable_at_submit: bool = True
    exit_fill_delay_bars: int = 0
    exit_cannot_fill: bool = False
    gap_band_check: bool = False
    inject_cancel_unknown_once: bool = False
    inject_cancel_already_consumed_after: int | None = None
    inject_cancel_fill_race_once: bool = False


def execution_profile(name: str, *, use_bar_volume: bool = False) -> ExecutionAssumptions:
    """Return one of the predeclared R09 conditional execution assumption profiles."""
    if name == "IDEAL_LIMIT_FILL":
        return ExecutionAssumptions(
            profile_name="IDEAL_LIMIT_FILL",
            assumption_summary=(
                "Conditional assumption: marketable F11 reduce-only SELL LIMIT at the tick-rounded "
                "adverse bid fills immediately within bar capacity (0.10 NVDA/bar or bar volume), "
                "with 3 bps fee and 2 bps slippage; all real exchange filters & Demo guardrails enforced."
            ),
            fee_rate=DEFAULT_PAPER_FEE_RATE,
            slippage=DEFAULT_PAPER_SLIPPAGE,
            half_spread_bps=D("0"),
            capacity_per_bar=None if use_bar_volume else D("0.10"),
            use_bar_volume=use_bar_volume,
            exit_marketable_at_submit=True,
            exit_fill_delay_bars=0,
        )
    if name == "LIMITED_CAPACITY_PARTIAL":
        return ExecutionAssumptions(
            profile_name="LIMITED_CAPACITY_PARTIAL",
            assumption_summary=(
                "Conditional assumption: executable liquidity capped at 0.02 NVDA/bar (50% of 0.04 lot), "
                "same entry price basis as baseline (3 bps fee, 2 bps slippage), forcing multi-bar partial "
                "fills and exercising F11 anti-dust / MIN_NOTIONAL protection on <5 USDT remainders."
            ),
            fee_rate=DEFAULT_PAPER_FEE_RATE,
            slippage=DEFAULT_PAPER_SLIPPAGE,
            half_spread_bps=D("0"),
            capacity_per_bar=D("0.02"),
            use_bar_volume=False,
            exit_marketable_at_submit=True,
            exit_fill_delay_bars=0,
        )
    if name == "DELAYED_EXIT_FILL":
        return ExecutionAssumptions(
            profile_name="DELAYED_EXIT_FILL",
            assumption_summary=(
                "Conditional assumption: same entry basis as baseline, but passive F11 reduce-only SELL LIMIT "
                "rests in queue for >=2 bars (2h on 1h bars) before filling at its limit when the adverse bid "
                "reaches it; reprices downward "
                "after cooldown if price drops (up to EXIT_MAX_ATTEMPTS=5)."
            ),
            fee_rate=DEFAULT_PAPER_FEE_RATE,
            slippage=DEFAULT_PAPER_SLIPPAGE,
            half_spread_bps=D("0"),
            capacity_per_bar=None if use_bar_volume else D("0.10"),
            use_bar_volume=use_bar_volume,
            exit_marketable_at_submit=False,
            exit_fill_delay_bars=2,
        )
    if name == "UNFILLED_EXIT_TIMEOUT":
        return ExecutionAssumptions(
            profile_name="UNFILLED_EXIT_TIMEOUT",
            assumption_summary=(
                "Conditional assumption: same entry basis as baseline, severe liquidity drought where F11 "
                "reduce-only SELL LIMIT orders cannot fill; exercises bounded repricing up to "
                "EXIT_MAX_ATTEMPTS=5 then EXIT_RETRY_LIMIT_EXCEEDED."
            ),
            fee_rate=DEFAULT_PAPER_FEE_RATE,
            slippage=DEFAULT_PAPER_SLIPPAGE,
            half_spread_bps=D("0"),
            capacity_per_bar=None if use_bar_volume else D("0.10"),
            use_bar_volume=use_bar_volume,
            exit_marketable_at_submit=False,
            exit_fill_delay_bars=999999,
            exit_cannot_fill=True,
        )
    if name == "GAP_AND_CANCEL_FAULT":
        return ExecutionAssumptions(
            profile_name="GAP_AND_CANCEL_FAULT",
            assumption_summary=(
                "Conditional assumption: widened 10 bps half-spread, PERCENT_PRICE [0.97, 1.03] gap block "
                "when bar jump exceeds 3%, 1-bar passive exit wait, plus injected cancel UNKNOWN->RECONCILE fault."
            ),
            fee_rate=DEFAULT_PAPER_FEE_RATE,
            slippage=DEFAULT_PAPER_SLIPPAGE,
            half_spread_bps=D("10"),
            capacity_per_bar=None if use_bar_volume else D("0.10"),
            use_bar_volume=use_bar_volume,
            exit_marketable_at_submit=False,
            exit_fill_delay_bars=1,
            gap_band_check=True,
            inject_cancel_unknown_once=True,
        )
    raise ValueError(f"Unknown execution profile: {name}")


@dataclass(frozen=True)
class FundingSchedule:
    """Funding booked through PaperBroker. The constant schedule uses basis points per hour."""

    mode: str = "zero"  # 'zero' | 'constant_hourly_bps' | 'historical_events'
    hourly_bps: float = 0.0
    events: tuple[tuple[datetime, Decimal, Decimal, str], ...] = ()
    label: str = "EX_FUNDING"


def _parse_utc(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _floor_step(value: Decimal, step: Decimal) -> Decimal:
    return (value / step).to_integral_value(rounding=ROUND_FLOOR) * step


def _ceil_step(value: Decimal, step: Decimal) -> Decimal:
    return (value / step).to_integral_value(rounding=ROUND_CEILING) * step


def _demo_grid_config(
    anchor_price: Decimal,
    rules: GridRules,
    *,
    max_cash_committed: Decimal,
    fee_rate: Decimal,
    slippage: Decimal,
    order_ttl: int = 100,
) -> GridConfig:
    """Construct the exact Demo ±1% single-level GridConfig anchored at ``anchor_price``."""
    tick = rules.tick_size
    lower = _floor_step(anchor_price * D("0.99"), tick)
    upper = _ceil_step(anchor_price * D("1.01"), tick)
    if upper <= lower:
        upper = lower + tick
    return GridConfig(
        mode="arithmetic",
        lower=float(lower),
        upper=float(upper),
        levels=1,
        initial_cash=10000.0,
        initial_inventory=0,
        initial_price=float(anchor_price),
        quantity=1,
        max_inventory=1,
        max_cash_committed=float(max_cash_committed),
        atr_window=3,
        atr_multiplier=1.0,
        rebuild_threshold=2.0,
        min_rebuild_interval=3,
        max_turnover=10.0,
        breakout="pause",
        max_drawdown=0.99,  # Demo risk halt is enforced by guardrails.max_loss, not 20% of 5000U wallet
        minimum_price=0.01,
        minimum_quantity=1,
        fee_bps=float(fee_rate * D("10000")),
        slippage_bps=float(slippage * D("10000")),
        order_ttl=order_ttl,
        center_rule="last_close",
        expiry_reseed_limit=0,
        expiry_reseed_interval=order_ttl,
    )


def default_demo_rules() -> GridRules:
    return GridRules(
        quantity_unit=D("0.04"),
        step_size=D("0.01"),
        tick_size=D("0.01"),
        min_quantity=D("0.01"),
        min_notional=D("5"),
    )


def _synthetic_book(
    close: Decimal,
    prev_close: Decimal | None,
    rules: GridRules,
    assumptions: ExecutionAssumptions,
) -> dict[str, Decimal]:
    """Derive synthetic top-of-book and mark price from bar close under explicit assumptions."""
    tick = rules.tick_size
    half_spread = assumptions.half_spread_bps / D("10000")
    bid_raw = close * (D("1") - half_spread)
    ask_raw = close * (D("1") + half_spread)
    bid = max(tick, _floor_step(bid_raw, tick))
    ask = max(bid, _ceil_step(ask_raw, tick))
    # Under gap_band_check, when a single bar jumps >3% relative to prev_close,
    # model transient mark-vs-bid lag (mark lagging halfway toward prev_close)
    # so PERCENT_PRICE [0.97, 1.03] protection can realistically trigger.
    mark = close
    if assumptions.gap_band_check and prev_close is not None and prev_close > ZERO:
        jump = abs(close - prev_close) / prev_close
        if jump > D("0.03"):
            mark = prev_close
    return {"bid": bid, "ask": ask, "mark": mark}


def _check_percent_price(
    price: Decimal,
    ask: Decimal,
    mark: Decimal,
    guardrails: DemoGuardrails,
) -> str | None:
    if not guardrails.enforce_percent_price:
        return None
    low = mark * guardrails.percent_price_down
    high = mark * guardrails.percent_price_up
    if not (low <= price <= high) or not (low <= ask <= high):
        return "EXIT_PRICE_BAND"
    return None


def _plan_offline_limit_exit(
    *,
    grid: GridExecution,
    inventory: Decimal,
    book: dict[str, Decimal],
    limit_bid: Decimal,
    quote_price_state: Decimal,
    guardrails: DemoGuardrails,
    dispatched_exit_attempts: int,
    halted: bool,
    opening_equity: Decimal,
    current_equity: Decimal,
    broker: _CampaignPaperBroker,
) -> tuple[Decimal | None, Decimal | None, str | None]:
    """Mirror ``DemoGridBridge._plan_limit_exit`` faithfully in offline Paper mode."""
    if halted:
        return None, None, "HALTED"
    tick = common_step(grid.rules.tick_size, grid.rules.tick_size)
    price = _floor_step(limit_bid, tick)
    if price <= ZERO or price < D(str(grid.config.minimum_price)):
        return None, None, "EXIT_PRICE_FILTER"

    step = common_step(grid.rules.step_size, grid.rules.step_size)
    min_qty = grid.rules.min_quantity
    max_qty = guardrails.max_quantity
    min_notional = guardrails.min_notional if guardrails.enforce_min_notional_on_exit else ZERO
    available_qty = _floor_step(inventory, step)
    if available_qty <= ZERO or available_qty < min_qty:
        return None, None, "EXIT_MIN_QUANTITY"
    if available_qty * price < min_notional:
        return None, None, "EXIT_MIN_NOTIONAL"

    quote_price = max(price, book["ask"], quote_price_state)
    if guardrails.enforce_max_notional_on_exit:
        max_by_notional = _floor_step(guardrails.max_notional / quote_price, step)
        cap = min(max_qty, max_by_notional)
    else:
        max_by_notional = max_qty
        cap = max_qty

    if available_qty <= cap:
        quantity = available_qty
    else:
        minimum = _ceil_step(max(min_qty, (min_notional / price) if min_notional > ZERO else min_qty), step)
        if cap < minimum:
            return None, None, "EXIT_MAX_NOTIONAL" if max_by_notional < minimum else "EXIT_MAX_QUANTITY"
        chunks = (available_qty / cap).to_integral_value(rounding=ROUND_CEILING)
        quantity = _floor_step(min(cap, available_qty - (chunks - 1) * minimum), step)
        if quantity < minimum:
            return None, None, "EXIT_MAX_NOTIONAL"

    if quantity <= ZERO or quantity > inventory or quantity < min_qty or quantity > max_qty:
        return None, None, "EXIT_MIN_QUANTITY"
    if quantity * price < min_notional:
        return None, None, "EXIT_MIN_NOTIONAL"
    if guardrails.enforce_max_notional_on_exit and quantity * quote_price > guardrails.max_notional:
        return None, None, "EXIT_MAX_NOTIONAL"

    band_err = _check_percent_price(price, book["ask"], book["mark"], guardrails)
    if band_err is not None:
        return None, None, band_err

    if dispatched_exit_attempts >= guardrails.exit_max_attempts:
        return None, None, "EXIT_RETRY_LIMIT_EXCEEDED"

    if guardrails.max_loss is not None and (opening_equity - current_equity) >= guardrails.max_loss:
        return None, None, "EXIT_LOSS_BUDGET_EXHAUSTED"

    if broker.global_post_blocked(quantity * price):
        return None, None, "EXIT_GLOBAL_BUDGET_EXHAUSTED"
    if broker.window_post_blocked():
        return None, None, "EXIT_WINDOW_BUDGET_EXHAUSTED"

    return price, quantity, None


def run_offline_lifecycle(
    bars: list[dict[str, Any]],
    *,
    rules: GridRules | None = None,
    guardrails: DemoGuardrails | None = None,
    assumptions: ExecutionAssumptions | None = None,
    funding: FundingSchedule | None = None,
    opening_wallet: Decimal = DEFAULT_DEMO_WALLET,
    strategy_cap_usdt: Decimal = DEFAULT_STRATEGY_CAP,
    interval: str = "1h",
    instrument_id: str = "NVDAUSDT",
    account_id: str = "offline-f11-f08",
    family_prefix: str = "fl-offline",
    store_path: Path | str = ":memory:",
    initial_anchor_override: Decimal | None = None,
    order_ttl: int = 100,
    record_hourly_series: bool = False,
) -> dict[str, Any]:
    """Execute the full offline Grid + F11 + F08 + Funding lifecycle over PaperBroker.

    All orders, partial fills, cancels, funding settlements, and F08 rotations
    share ONE ``TradingController`` + ``PaperBroker`` + ``PaperStore`` ledger
    and reconcile through ``factorlab.experiments.grid_fifo.attribute_events``.
    """
    if not bars:
        raise ValueError("nonempty bars required")
    rules = rules or default_demo_rules()
    guardrails = guardrails or DemoGuardrails()
    assumptions = assumptions or execution_profile("IDEAL_LIMIT_FILL")
    funding = funding or FundingSchedule()

    bar_delta = pd.Timedelta(interval)
    bar_seconds = float(bar_delta.total_seconds())

    def bar_close_utc(b: dict[str, Any]) -> datetime:
        t = _parse_utc(b["time"])
        return (t + bar_delta - timedelta(microseconds=1)).astimezone(timezone.utc)

    first_close_price = D(str(bars[0]["close"]))
    initial_book = _synthetic_book(first_close_price, None, rules, assumptions)
    anchor_price = initial_anchor_override if initial_anchor_override is not None else initial_book["ask"]

    now = [bar_close_utc(bars[0])]
    broker = _CampaignPaperBroker(
        Market.BINANCE_USDM,
        account_id,
        (Instrument(instrument_id, "USDT"),),
        {"USDT": opening_wallet},
        fee_rate=assumptions.fee_rate,
        guardrails=guardrails,
    )
    store = PaperStore(str(store_path), continuous_capacity=True)
    # Use RiskLimits aligned to strategy max_notional (matching Demo DEMO_LIMITS)
    risk_cap = max(guardrails.max_notional, strategy_cap_usdt)
    limits = RiskLimits(risk_cap, risk_cap, risk_cap, orders_per_minute=20)

    try:
        controller = TradingController(broker, store, clock=lambda: now[0], limits=limits)

        generation = 0
        dep_id = grid_deployment_id(family_prefix, generation)
        cfg = _demo_grid_config(
            anchor_price,
            rules,
            max_cash_committed=strategy_cap_usdt,
            fee_rate=assumptions.fee_rate,
            slippage=assumptions.slippage,
            order_ttl=order_ttl,
        )
        grid = GridExecution(
            cfg,
            rules,
            deployment_id=dep_id,
            account_id=account_id,
            market=Market.BINANCE_USDM,
            instrument_id=instrument_id,
            initial_equity=opening_wallet,
            fee_rate=assumptions.fee_rate,
            continuous_capacity=True,
        )

        # Lifecycle telemetry & state
        opening_equity = opening_wallet
        halted = False
        halt_reason: str | None = None
        deletes_consumed = 0
        consumed_delete_ids: set[str] = set()
        order_meta: dict[str, dict[str, Any]] = {}
        trade_rows: list[dict[str, Any]] = []
        close_refs: dict[datetime, Decimal] = {}
        epoch_ref = datetime(2000, 1, 1)
        trade_seq = 0
        funding_seq = 0

        # Per-generation & F11/F08 tracking
        breakout_events = 0
        flat_after_breakout_events = 0
        rotations_completed = 0
        rotations_blocked: list[dict[str, Any]] = []
        exit_orders_submitted = 0
        exit_orders_filled = 0
        exit_orders_partial_events = 0
        exit_reprices = 0
        exit_cancel_faults = 0
        exit_reconcile_recoveries = 0
        exit_block_counts: dict[str, int] = {}
        last_exit_block_reason: str | None = None
        dust_protections_triggered = 0
        dispatched_exits_this_gen = 0
        breakout_started_bar_idx: int | None = None
        current_breakout_recorded = False
        exit_latencies_bars: list[int] = []
        order_submitted_bar: dict[str, int] = {}
        cancel_fault_injected = False
        cancel_fill_race_injected = False
        pending_unknown_order_id: str | None = None
        rotated_on_this_bar = False
        generations_log: list[dict[str, Any]] = [
            {
                "generation": 0,
                "deployment_id": dep_id,
                "anchor_price": float(anchor_price),
                "lower": cfg.lower,
                "upper": cfg.upper,
                "start_bar": 0,
                "start_time": bars[0]["time"],
            }
        ]

        # Historical funding pointer (sorted by timestamp)
        hist_funding_events = sorted(funding.events, key=lambda x: x[0]) if funding.mode == "historical_events" else []
        hist_funding_idx = 0
        hourly_rate = D(str(funding.hourly_bps)) / D("10000") if funding.mode == "constant_hourly_bps" else ZERO

        # Strategy-cap equity curve & drawdown tracking
        strat_peak = strategy_cap_usdt
        max_drawdown = ZERO
        holding_hours = ZERO
        paused_with_inventory_hours = ZERO
        total_slippage = ZERO
        hourly_series: list[dict[str, Any]] = []

        def current_account_equity() -> Decimal:
            if grid.account is not None:
                return grid.account.equity
            return project_paper_account(controller, instrument_id).equity

        def check_loss_halt() -> bool:
            nonlocal halted, halt_reason
            if halted:
                return True
            if guardrails.max_loss is not None:
                eq = current_account_equity()
                if (opening_equity - eq) >= guardrails.max_loss:
                    halted = True
                    halt_reason = "LOSS_BUDGET_EXHAUSTED"
                    return True
            return False

        def record_fill_trade(event: OrderEvent, order_snap: Any, ref_price: Decimal) -> None:
            nonlocal trade_seq, total_slippage, exit_orders_partial_events, exit_orders_filled
            qty_delta = event.cumulative_filled_quantity - order_snap.cumulative_filled_quantity
            fee_delta = event.cumulative_fee - order_snap.cumulative_fee
            price = event.fill_price
            assert price is not None
            slip = qty_delta * abs(price - ref_price)
            total_slippage += slip
            key_ts = epoch_ref + timedelta(microseconds=trade_seq)
            trade_seq += 1
            close_refs[key_ts] = ref_price
            trade_rows.append(
                {
                    "event_id": event.event_id,
                    "order_id": event.client_order_id,
                    "side": order_snap.intent.side.value,
                    "event_time": event.event_time.isoformat(),
                    "execution_date": key_ts.isoformat(),
                    "cumulative_filled_quantity": event.cumulative_filled_quantity,
                    "cumulative_fee": event.cumulative_fee,
                    "executed_shares": qty_delta,
                    "execution_price": price,
                    "fee": fee_delta,
                    "trade_notional": qty_delta * price,
                    "slippage": slip,
                    "transaction_cost": fee_delta + slip,
                }
            )
            if order_meta[event.client_order_id]["purpose"] == "RISK_EXIT":
                if event.status == OrderStatus.PARTIALLY_FILLED:
                    exit_orders_partial_events += 1
                elif event.status == OrderStatus.FILLED:
                    exit_orders_filled += 1

        def submit_order_through_paper(key: str, *, purpose: str, bar_idx: int) -> bool:
            if check_loss_halt():
                return False
            order_snap = grid.orders[key]
            intent = order_snap.intent
            notional = intent.quantity * intent.limit_price
            if broker.global_post_blocked(notional) or broker.window_post_blocked():
                return False
            if len(controller.account().open_orders) >= guardrails.max_open_orders:
                return False
            order_meta[key] = {
                "deployment_id": grid.deployment_id,
                "generation": generation,
                "purpose": purpose,
            }
            try:
                grid.submit_paper(controller, key, delivery=grid.delivery + 1)
            except RiskRejected:
                order_meta.pop(key, None)
                return False
            order_submitted_bar[key] = bar_idx
            return True

        def book_funding_event(amount: Decimal, settlement_time: datetime, mark_price: Decimal, rate: Decimal) -> None:
            nonlocal funding_seq
            quantized = amount.quantize(USDT_QUANTUM, rounding=ROUND_HALF_EVEN)
            if quantized == ZERO:
                return
            funding_seq += 1
            tran_id = f"off-{account_id}-{funding_seq:08d}"
            ev = FundingEvent(
                account_id=account_id,
                tran_id=tran_id,
                symbol=instrument_id,
                asset="USDT",
                income_type="FUNDING_FEE",
                amount=quantized,
                event_time=settlement_time,
                source="offline-funding-model",
                event_id=funding_event_id("FUNDING_FEE", tran_id),
                metadata={"mark_price": str(mark_price), "rate": str(rate)},
            )
            grid.apply_funding_paper(controller, ev, delivery=grid.delivery + 1)

        def execute_fill(key: str, qty: Decimal, px: Decimal) -> OrderEvent | None:
            """Execute full or partial fill through Paper; sub-min_notional partial trade reports
            on an already-admitted legal order use ``confirm_paper`` just like real exchange fills.
            """
            try:
                ev = grid.fill_paper(controller, key, qty, px, delivery=grid.delivery + 1)
            except InsufficientCollateral:
                return None
            if ev is not None:
                return ev
            snap = broker.state.orders[key].snapshot
            if snap.status in TERMINAL or snap.reconcile_required:
                return None
            rem = snap.intent.quantity - snap.cumulative_filled_quantity
            fill_q = _floor_step(min(qty, rem), rules.step_size)
            if fill_q <= ZERO:
                return None
            # Only use confirm_paper for genuine partial fills of an already-admitted legal order
            # (exchange minimums apply to OrderIntent submission, not individual trade reports).
            if fill_q >= snap.intent.quantity:
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
                source="offline-partial-fill",
                event_id=f"partial:{key}:{broker.state.sequence + 1}:{total_q}",
                cumulative_fee=snap.cumulative_fee + fee_inc,
            )
            try:
                grid.confirm_paper(controller, event, delivery=grid.delivery + 1)
            except InsufficientCollateral:
                return None
            return event

        def match_order(key, order_snap, book, budget, *, resting_exit=False) -> Decimal:
            candidate = execution_candidate(
                order_snap,
                ask=book["ask"],
                bid=book["bid"],
                budget=budget,
                slippage=assumptions.slippage,
                step=rules.step_size,
            )
            if candidate is None:
                return ZERO
            quantity, execution_price = candidate
            if resting_exit:
                # A resting F11 order executes at its posted limit once the adverse bid reaches it.
                execution_price = order_snap.intent.limit_price
            event = execute_fill(key, quantity, execution_price)
            if event is None:
                return ZERO
            reference = book["ask"] if order_snap.intent.side == Side.BUY else book["bid"]
            record_fill_trade(event, order_snap, reference)
            return event.cumulative_filled_quantity - order_snap.cumulative_filled_quantity

        prev_close: Decimal | None = None
        prev_bar_close_ts: datetime | None = None

        for bar_idx, bar in enumerate(bars):
            now[0] = bar_close_utc(bar)
            close = D(str(bar["close"]))
            book = _synthetic_book(close, prev_close, rules, assumptions)
            # Plan marketable exits against the adverse bid while retaining the unadjusted
            # book for risk checks and measuring execution cost.
            exit_limit_bid = book["bid"]
            if assumptions.exit_marketable_at_submit:
                exit_limit_bid *= D("1") - assumptions.slippage
            if assumptions.use_bar_volume and bar.get("volume") is not None:
                budget = D(str(bar["volume"])) * assumptions.participation
            else:
                budget = assumptions.capacity_per_bar if assumptions.capacity_per_bar is not None else D("0.10")

            # 0. Reconcile any pending UNKNOWN order from a previous cancel fault
            if pending_unknown_order_id is not None:
                grid.reconcile_paper(controller, delivery=grid.delivery + 1)
                exit_reconcile_recoveries += 1
                pending_unknown_order_id = None

            # 1. Accrue on the inventory held between closes before any fills at this close.
            position = broker.state.positions.get(instrument_id)
            if prev_bar_close_ts is not None and position is not None and position.quantity > ZERO:
                elapsed_seconds = (now[0] - prev_bar_close_ts).total_seconds()
                elapsed_hours = D(str(elapsed_seconds)) / D("3600")
                holding_hours += elapsed_hours
                if grid.closed_by_breakout:
                    paused_with_inventory_hours += elapsed_hours
                if hourly_rate:
                    rate = hourly_rate * elapsed_hours
                    amount = funding_cashflow(position.quantity, close, rate)
                    book_funding_event(amount, now[0], close, rate)

            # Historical funding settlements apply strictly in (prev_bar_close_ts, now[0]].
            if funding.mode == "historical_events" and prev_bar_close_ts is not None:
                while hist_funding_idx < len(hist_funding_events):
                    f_ts, f_mark, f_rate, _ = hist_funding_events[hist_funding_idx]
                    if f_ts <= prev_bar_close_ts:
                        hist_funding_idx += 1
                        continue
                    if f_ts > now[0]:
                        break
                    pos = broker.state.positions.get(instrument_id)
                    if pos is not None and pos.quantity > ZERO:
                        amt = funding_cashflow(pos.quantity, f_mark, f_rate)
                        book_funding_event(amt, f_ts, f_mark, f_rate)
                    hist_funding_idx += 1

            # 2. Capture prior working orders (submitted on earlier bars) before observing this bar
            prior_orders = [
                (key, record.snapshot)
                for key, record in broker.state.orders.items()
                if record.observed_at < now[0] and record.snapshot.status not in TERMINAL
            ]

            # 3. Observe bar in active GridExecution (unless this generation just rotated on prior bar? No,
            #    rotation happens at end of previous bar, so this bar is the first bar for the new generation!)
            close_only = "high" not in bar or "low" not in bar or "volume" not in bar
            obs: dict[str, Any] = {
                "id": str(bar.get("id", f"bar-{bar_idx}")),
                "time": _parse_utc(bar["time"]).replace(tzinfo=None).isoformat(),
                "price": close,
                "completed_bar": True,
            }
            if close_only:
                obs["scenario"] = "CLOSE_ONLY_SCENARIO"
            else:
                obs["high"] = D(str(bar["high"]))
                obs["low"] = D(str(bar["low"]))
                obs["close"] = close
                obs["volume"] = max(0, int(D(str(bar["volume"]))))

            grid.observe_paper(controller, obs, delivery=grid.delivery + 1, observed_at=now[0])
            rotated_on_this_bar = False

            # Track first breakout bar for this generation
            if grid.closed_by_breakout and not current_breakout_recorded:
                breakout_events += 1
                current_breakout_recorded = True
                breakout_started_bar_idx = bar_idx

            # 4. Match prior working orders against current bar
            for key, order_snap in prior_orders:
                if key not in broker.state.orders or broker.state.orders[key].snapshot.status in TERMINAL:
                    continue
                is_exit = order_meta.get(key, {}).get("purpose") == "RISK_EXIT"
                if is_exit:
                    if assumptions.exit_cannot_fill:
                        continue
                    age_bars = bar_idx - order_submitted_bar.get(key, bar_idx)
                    if age_bars < assumptions.exit_fill_delay_bars:
                        continue
                budget -= match_order(key, order_snap, book, budget, resting_exit=is_exit)

            # 5. Process standard Grid cancellations (e.g., breakout cancel or TTL cancel)
            for key in sorted(grid.cancellations):
                if key in broker.state.orders and broker.state.orders[key].snapshot.status not in TERMINAL:
                    # Exit order cancellations are managed in step 6 below
                    if key in grid.exit_orders:
                        continue
                    grid.cancel_paper(controller, key, delivery=grid.delivery + 1)
                    deletes_consumed += 1
                    consumed_delete_ids.add(key)

            # 6. Process normal RUNNING Grid proposals vs F11 Breakout Exit lifecycle
            check_loss_halt()
            if not grid.closed_by_breakout:
                # Normal RUNNING Grid submission (respecting max_open_orders=1 and budgets)
                for key, order_snap in list(grid.orders.items()):
                    if order_snap.status == OrderStatus.NEW:
                        if halted:
                            grid.withdraw_unsubmitted_paper(controller, [key], delivery=grid.delivery + 1)
                            break
                        submitted = submit_order_through_paper(key, purpose="GRID", bar_idx=bar_idx)
                        if not submitted and key in grid.orders and grid.orders[key].status == OrderStatus.NEW:
                            # Hold reservation or withdraw if blocked by open-order slot
                            if len(controller.account().open_orders) >= guardrails.max_open_orders:
                                break
            elif guardrails.enable_f11:
                # Withdraw any unsubmitted local proposals first (matching DemoGridBridge.step_breakout_exit)
                unsubmitted = [
                    o.intent.client_order_id
                    for o in grid.active
                    if o.intent.client_order_id not in broker.state.orders
                ]
                if unsubmitted:
                    grid.withdraw_unsubmitted_paper(controller, unsubmitted, delivery=grid.delivery + 1)

                acct_now = grid.account or project_paper_account(controller, instrument_id)
                open_orders = controller.account().open_orders

                # 6a. Cancel any remaining open BUY first
                open_buys = [o for o in open_orders if o.intent.side is Side.BUY]
                if open_buys:
                    buy_key = open_buys[0].intent.client_order_id
                    if buy_key not in consumed_delete_ids:
                        if buy_key not in grid.cancellations:
                            grid.cancel_exit_paper(controller, buy_key, delivery=grid.delivery + 1)
                        grid.cancel_paper(controller, buy_key, delivery=grid.delivery + 1)
                        deletes_consumed += 1
                        consumed_delete_ids.add(buy_key)
                elif acct_now.inventory > ZERO and open_orders:
                    # 6b. Existing working reduce-only SELL: monitor & bounded reprice
                    sell = open_orders[0]
                    sell_key = sell.intent.client_order_id
                    if sell.intent.side is Side.SELL and sell.intent.reduce_only and sell_key in grid.orders:
                        if sell_key in consumed_delete_ids:
                            last_exit_block_reason = "EXIT_CANCEL_ALREADY_CONSUMED"
                            exit_block_counts[last_exit_block_reason] = (
                                exit_block_counts.get(last_exit_block_reason, 0) + 1
                            )
                        else:
                            age_sec = (bar_idx - order_submitted_bar.get(sell_key, bar_idx)) * bar_seconds
                            target_px, _, plan_block = _plan_offline_limit_exit(
                                grid=grid,
                                inventory=acct_now.inventory,
                                book=book,
                                limit_bid=exit_limit_bid,
                                quote_price_state=broker.state.quotes[instrument_id].price,
                                guardrails=guardrails,
                                dispatched_exit_attempts=dispatched_exits_this_gen,
                                halted=halted,
                                opening_equity=opening_equity,
                                current_equity=acct_now.equity,
                                broker=broker,
                            )
                            if plan_block == "EXIT_MIN_NOTIONAL" and sell.cumulative_filled_quantity > ZERO:
                                # Anti-dust protection: keep working partial SELL rather than canceling!
                                dust_protections_triggered += 1
                                last_exit_block_reason = "EXIT_PARTIAL_DUST_PROTECTED"
                            elif (
                                plan_block is None
                                and target_px is not None
                                and age_sec >= guardrails.exit_reprice_seconds
                                and sell.intent.limit_price is not None
                                and target_px < sell.intent.limit_price
                            ):
                                # Pre-cancel price band check already verified in _plan_offline_limit_exit
                                grid.cancel_exit_paper(controller, sell_key, delivery=grid.delivery + 1)
                                if assumptions.inject_cancel_unknown_once and not cancel_fault_injected:
                                    # Simulate cancel HTTP timeout -> UNKNOWN -> EXIT_RECONCILING (forbids replacement)
                                    cancel_fault_injected = True
                                    exit_cancel_faults += 1
                                    grid.mark_unknown_paper(controller, sell_key, delivery=grid.delivery + 1)
                                    pending_unknown_order_id = sell_key
                                elif assumptions.inject_cancel_fill_race_once and not cancel_fill_race_injected:
                                    # Simulate cancel-fill race: order filled before cancel reached exchange!
                                    cancel_fill_race_injected = True
                                    rem = sell.intent.quantity - sell.cumulative_filled_quantity
                                    ev = grid.fill_paper(
                                        controller, sell_key, rem, sell.intent.limit_price, delivery=grid.delivery + 1
                                    )
                                    if ev is not None:
                                        record_fill_trade(ev, sell, book["bid"])
                                else:
                                    grid.cancel_paper(controller, sell_key, delivery=grid.delivery + 1)
                                    deletes_consumed += 1
                                    consumed_delete_ids.add(sell_key)
                                    exit_reprices += 1
                                    # Note: online F11 is two-phase (cancel -> reconcile -> replace next step);
                                    # on hourly bars, we wait until next bar to submit replacement, preserving
                                    # the exact 1-write-action-per-step / reconcile-before-replace invariant!
                            elif plan_block is not None:
                                last_exit_block_reason = plan_block
                                exit_block_counts[plan_block] = exit_block_counts.get(plan_block, 0) + 1
                                if plan_block == "EXIT_LOSS_BUDGET_EXHAUSTED":
                                    halted = True
                                    halt_reason = plan_block
                elif acct_now.inventory > ZERO and not open_orders:
                    # 6c. No open orders and confirmed inventory > 0: propose one legal reduce-only SELL LIMIT
                    price, qty, block_reason = _plan_offline_limit_exit(
                        grid=grid,
                        inventory=acct_now.inventory,
                        book=book,
                        limit_bid=exit_limit_bid,
                        quote_price_state=broker.state.quotes[instrument_id].price,
                        guardrails=guardrails,
                        dispatched_exit_attempts=dispatched_exits_this_gen,
                        halted=halted,
                        opening_equity=opening_equity,
                        current_equity=acct_now.equity,
                        broker=broker,
                    )
                    if block_reason is not None:
                        last_exit_block_reason = block_reason
                        exit_block_counts[block_reason] = exit_block_counts.get(block_reason, 0) + 1
                        if block_reason == "EXIT_LOSS_BUDGET_EXHAUSTED":
                            halted = True
                            halt_reason = block_reason
                    else:
                        assert price is not None and qty is not None
                        last_exit_block_reason = None
                        intent = grid.propose_limit_exit_paper(
                            controller, {"price": price, "quantity": qty}, delivery=grid.delivery + 1
                        )
                        if intent is not None:
                            ok = submit_order_through_paper(
                                intent.client_order_id, purpose="RISK_EXIT", bar_idx=bar_idx
                            )
                            if ok:
                                dispatched_exits_this_gen += 1
                                exit_orders_submitted += 1
                                if (
                                    assumptions.exit_marketable_at_submit
                                    and not assumptions.exit_cannot_fill
                                    and assumptions.exit_fill_delay_bars == 0
                                ):
                                    order_snap = broker.state.orders[intent.client_order_id].snapshot
                                    budget -= match_order(intent.client_order_id, order_snap, book, budget)
                            else:
                                grid.withdraw_unsubmitted_paper(
                                    controller, [intent.client_order_id], delivery=grid.delivery + 1
                                )

            # 7. Check FLAT_CONFIRMED after breakout & F08 Automatic Rotation
            acct_end = grid.account or project_paper_account(controller, instrument_id)
            check_loss_halt()
            if (
                grid.closed_by_breakout
                and acct_end.inventory == ZERO
                and acct_end.reconciled
                and not controller.account().open_orders
                and not grid.active
                and pending_unknown_order_id is None
            ):
                if breakout_started_bar_idx is not None:
                    exit_latencies_bars.append(bar_idx - breakout_started_bar_idx)
                    breakout_started_bar_idx = None
                    flat_after_breakout_events += 1

                if guardrails.enable_f08 and bar_idx < len(bars) - 1:
                    if halted:
                        rotations_blocked.append({"bar_idx": bar_idx, "reason": halt_reason or "HALTED"})
                    else:
                        next_anchor = book["ask"]
                        next_cfg = _demo_grid_config(
                            next_anchor,
                            rules,
                            max_cash_committed=strategy_cap_usdt,
                            fee_rate=assumptions.fee_rate,
                            slippage=assumptions.slippage,
                            order_ttl=order_ttl,
                        )
                        entry_notional = rules.quantity_unit * D(str(next_cfg.lower))
                        upper_notional = rules.quantity_unit * max(D(str(next_cfg.upper)), next_anchor)
                        if guardrails.enforce_min_notional_on_exit and entry_notional < guardrails.min_notional:
                            rotations_blocked.append({"bar_idx": bar_idx, "reason": "ROTATION_MIN_NOTIONAL"})
                        elif guardrails.enforce_max_notional_on_exit and upper_notional > guardrails.max_notional:
                            rotations_blocked.append({"bar_idx": bar_idx, "reason": "ROTATION_MAX_NOTIONAL"})
                        elif broker.global_post_blocked(entry_notional):
                            rotations_blocked.append({"bar_idx": bar_idx, "reason": "ROTATION_GLOBAL_BUDGET_EXHAUSTED"})
                        else:
                            generation += 1
                            next_dep_id = grid_deployment_id(family_prefix, generation)
                            replacement = GridExecution(
                                next_cfg,
                                rules,
                                deployment_id=next_dep_id,
                                account_id=account_id,
                                market=Market.BINANCE_USDM,
                                instrument_id=instrument_id,
                                initial_equity=acct_end.equity,
                                fee_rate=assumptions.fee_rate,
                                continuous_capacity=True,
                            )
                            grid.rotate_paper(controller, replacement)
                            grid = replacement
                            rotations_completed += 1
                            dispatched_exits_this_gen = 0
                            current_breakout_recorded = False
                            last_exit_block_reason = None
                            rotated_on_this_bar = True
                            generations_log.append(
                                {
                                    "generation": generation,
                                    "deployment_id": next_dep_id,
                                    "anchor_price": float(next_anchor),
                                    "lower": next_cfg.lower,
                                    "upper": next_cfg.upper,
                                    "start_bar": bar_idx + 1,
                                    "start_time": bars[bar_idx + 1]["time"],
                                }
                            )

            # 8. Update the equity curve and drawdown on a strategy_cap_usdt basis.
            net_pnl_now = acct_end.equity - opening_equity
            strat_equity = strategy_cap_usdt + net_pnl_now
            strat_peak = max(strat_peak, strat_equity)
            if strat_peak > ZERO:
                dd_now = max(ZERO, (strat_peak - strat_equity) / strat_peak)
                max_drawdown = max(max_drawdown, min(D("1"), dd_now))
            if record_hourly_series:
                hourly_series.append(
                    {
                        "bar_idx": bar_idx,
                        "time": bar["time"],
                        "close": float(close),
                        "generation": generation,
                        "state": grid.state,
                        "reason": grid.reason,
                        "inventory": float(acct_end.inventory),
                        "account_equity": float(acct_end.equity),
                        "strategy_equity": float(strat_equity),
                        "net_pnl": float(net_pnl_now),
                        "cumulative_funding": float(broker.state.funding.get("USDT", ZERO)),
                        "halted": halted,
                        "rotated": rotated_on_this_bar,
                    }
                )

            prev_close = close
            prev_bar_close_ts = now[0]

        # Reconcile if simulation ended while an injected cancel fault was still pending
        if pending_unknown_order_id is not None:
            grid.reconcile_paper(controller, delivery=grid.delivery + 1)
            exit_reconcile_recoveries += 1
            pending_unknown_order_id = None

        # Authoritative FIFO attribution & zero-residual reconciliation via grid_fifo.attribute_events
        final_acct = project_paper_account(controller, instrument_id)
        final_close = D(str(bars[-1]["close"]))
        fill_events = [ev for _, ev in store.order_events() if ev.fill_price is not None]
        funding_events_all = [ev for _, ev in store.funding_events()]
        fifo_result_input = {
            "config": {"initial_inventory": 0, "initial_price": 0},
            "rules": {"quantity_unit": 1},
            "orders": {
                cid: {"purpose": order_meta.get(cid, {}).get("purpose", "GRID")}
                for cid in broker.state.orders
            },
            "instrument_id": instrument_id,
            "market": Market.BINANCE_USDM.value,
            "capital": opening_wallet,
            "final": {"close": final_close, "equity": final_acct.equity},
        }
        fifo = attribute_events(
            fifo_result_input,
            trade_rows,
            close_refs,
            fill_events,
            broker.state,
            funding_events=funding_events_all,
        )
        fifo_summary = fifo["summary"]
        residual_check = abs(fifo_summary["reconciliation_residual"])

        # Compute exit delay metrics (include censored ongoing breakout delay if still holding at end)
        still_stranded_breakout = grid.closed_by_breakout and final_acct.inventory > ZERO
        censored_delay = (
            (len(bars) - 1 - breakout_started_bar_idx)
            if (still_stranded_breakout and breakout_started_bar_idx is not None)
            else None
        )
        all_delays = list(exit_latencies_bars) + ([censored_delay] if censored_delay is not None else [])
        mean_exit_delay_hours = (
            float(sum(all_delays) / len(all_delays)) * (bar_seconds / 3600.0) if all_delays else 0.0
        )
        max_exit_delay_hours = float(max(all_delays)) * (bar_seconds / 3600.0) if all_delays else 0.0

        net_pnl = fifo_summary["strategy_net_pnl"]
        realized_funding = fifo_summary["realized_funding"]
        realized_trade_gross = fifo_summary["realized_trade_pnl"]
        trading_net = fifo_summary["trading_net"]
        fees_total = fifo_summary["actual_fees"]
        residual_inv = fifo_summary["residual_inventory"]
        loss_fraction = float(-net_pnl / strategy_cap_usdt)

        # Funding erosion ratio:
        # 1) As fraction of strategy capital basis
        # 2) As fraction of positive trading gross (when realized_trade_gross > 0), else None
        funding_erosion_of_cap_pct = float(abs(realized_funding) / strategy_cap_usdt * D("100"))
        funding_erosion_of_gross_pct = (
            float(abs(realized_funding) / realized_trade_gross * D("100"))
            if realized_trade_gross > ZERO
            else None
        )
        rotation_success_rate = (
            float(rotations_completed / breakout_events) if breakout_events > 0 else None
        )
        flat_rotation_success_rate = (
            float(rotations_completed / flat_after_breakout_events) if flat_after_breakout_events > 0 else None
        )

        return {
            "model_label": guardrails.model_label,
            "execution_profile": assumptions.profile_name,
            "evidence_label": assumptions.evidence_label,
            "funding_label": funding.label,
            "bars": len(bars),
            "interval": interval,
            "opening_wallet_usdt": str(opening_wallet),
            "strategy_cap_usdt": str(strategy_cap_usdt),
            "final_wallet_usdt": str(fifo_summary["final_wallet"]),
            "final_equity_usdt": str(fifo_summary["final_equity"]),
            "final_nav_account": float(fifo_summary["final_nav"]),
            "net_pnl_usdt": float(net_pnl),
            "net_return_pct": float(net_pnl / strategy_cap_usdt * D("100")),
            "loss_fraction": loss_fraction,
            "max_drawdown": float(max_drawdown),
            "max_drawdown_pct": float(max_drawdown * D("100")),
            "realized_trade_gross_usdt": float(realized_trade_gross),
            "trading_net_usdt": float(trading_net),
            "fees_usdt": float(fees_total),
            "slippage_usdt": float(total_slippage),
            "realized_funding_usdt": float(realized_funding),
            "funding_events_count": len(funding_events_all),
            "funding_erosion_of_cap_pct": funding_erosion_of_cap_pct,
            "funding_erosion_of_gross_pct": funding_erosion_of_gross_pct,
            "grid_cycles": int(fifo_summary["grid_cycles"]),
            "fills_total": len(fill_events),
            "orders_total": len(broker.state.orders),
            "posts_consumed": broker.campaign["posts"],
            "deletes_consumed": deletes_consumed,
            "submitted_notional_usdt": float(broker.campaign["notional"]),
            "holding_hours": float(holding_hours),
            "paused_with_inventory_hours": float(paused_with_inventory_hours),
            "residual_inventory": float(residual_inv),
            "residual_notional_usdt": float(residual_inv * final_close),
            "stranded_at_end": bool(residual_inv > ZERO and grid.closed_by_breakout),
            "breakout_events": breakout_events,
            "flat_after_breakout_events": flat_after_breakout_events,
            "exit_orders_submitted": exit_orders_submitted,
            "exit_orders_filled": exit_orders_filled,
            "exit_orders_partial_events": exit_orders_partial_events,
            "exit_reprices": exit_reprices,
            "exit_cancel_faults": exit_cancel_faults,
            "exit_reconcile_recoveries": exit_reconcile_recoveries,
            "dust_protections_triggered": dust_protections_triggered,
            "exit_block_counts": exit_block_counts,
            "last_exit_block_reason": last_exit_block_reason,
            "mean_exit_delay_hours": mean_exit_delay_hours,
            "max_exit_delay_hours": max_exit_delay_hours,
            "rotations_completed": rotations_completed,
            "rotations_blocked_count": len(rotations_blocked),
            "rotations_blocked": rotations_blocked,
            "rotation_success_rate": rotation_success_rate,
            "flat_rotation_success_rate": flat_rotation_success_rate,
            "generations_total": len(generations_log),
            "generations_log": generations_log,
            "halted": halted,
            "halt_reason": halt_reason,
            "final_state": grid.state,
            "final_reason": grid.reason,
            "reconciliation_residual_usdt": str(fifo_summary["reconciliation_residual"]),
            "reconciled_zero_residual": bool(residual_check <= D("1e-7")),
            "hourly_series": hourly_series if record_hourly_series else None,
        }
    finally:
        store.db.close()


def replay_official_demo_lifecycle(
    cloud_snapshot: Path,
    *,
    windows_snapshot: Path | None = None,
) -> dict[str, Any]:
    """Reconstruct and reconcile the observed official Demo Gen 1 -> F11 -> F08 -> Gen 2 lifecycle
    through the offline Paper/GridExecution/Funding/FIFO stack, plus conditional counterfactuals
    on the exact observed 1m bars.
    """
    from factorlab.experiments.demo_grid_performance import (
        analyze_empirical_performance,
        load_and_merge_snapshots,
    )

    merged = load_and_merge_snapshots(cloud_snapshot, windows_snapshot=windows_snapshot)
    empirical = analyze_empirical_performance(merged)

    g1_id = "fl-d7345f8795464a65-g-00001"
    g2_id = "fl-d7345f8795464a65-g-00002"
    g1_dep = merged["deployments"][g1_id]
    g2_dep = merged["deployments"][g2_id]
    g1_bars = merged["bars_by_deployment"][g1_id]
    g2_bars = merged["bars_by_deployment"][g2_id]
    acct_id = g1_dep["account_id"]

    # Opening wallet at Gen 1 start (post Pre-Grid smoke = 4999.97114760 USDT)
    opening_wallet = D(str(g1_dep["initial_equity"]))
    rules = GridRules(**{k: D(str(v)) for k, v in g1_dep["rules"].items()})
    g1_cfg = GridConfig(**g1_dep["config"])
    g2_cfg = GridConfig(**g2_dep["config"])

    # Extract exact observed fills and funding events for Gen 1
    g1_fills = [
        r for r in empirical["fills_and_funding"]
        if r["deployment_id"] == g1_id and r["record_type"] == "TRADE_FILL"
    ]
    g1_funding_events = sorted(
        list(merged["funding_events"].values()),
        key=lambda e: (_parse_utc(e.event_time), e.event_id),
    )
    g1_buy_fills = [f for f in g1_fills if f["side"] == "BUY"]
    g1_exit_fills = [f for f in g1_fills if f["purpose"] == "RISK_EXIT"]
    g2_buy_order = next(
        o for o in empirical["order_lifecycle"]
        if o["deployment_id"] == g2_id and o["side"] == "BUY"
    )

    # Part 1: Authoritative Observed Event Replay through GridExecution + PaperBroker + PaperStore
    now = [_parse_utc(g1_bars[0]["time"])]
    broker = PaperBroker(
        Market.BINANCE_USDM,
        acct_id,
        (Instrument("NVDAUSDT", "USDT"),),
        {"USDT": opening_wallet},
        fee_rate=ZERO,  # Official Gen 1 fills were Maker=True with 0.00000000 USDT fee
    )
    store = PaperStore(":memory:", continuous_capacity=True)
    limits = RiskLimits(D("7"), D("7"), D("7"), orders_per_minute=20)

    try:
        controller = TradingController(broker, store, clock=lambda: now[0], limits=limits)
        grid = GridExecution(
            g1_cfg,
            rules,
            deployment_id=g1_id,
            account_id=acct_id,
            market=Market.BINANCE_USDM,
            instrument_id="NVDAUSDT",
            initial_equity=opening_wallet,
            fee_rate=ZERO,
            continuous_capacity=True,
        )

        buy_idx = 0
        fund_idx = 0
        g1_buy_order_id: str | None = None
        g1_breakout_bar_utc: str | None = None
        order_meta: dict[str, dict[str, Any]] = {}
        trade_rows: list[dict[str, Any]] = []
        close_refs: dict[datetime, Decimal] = {}
        epoch_ref = datetime(2000, 1, 1)
        trade_seq = 0

        def record_obs_fill(ev: OrderEvent, snap_before: Any) -> None:
            nonlocal trade_seq
            dq = ev.cumulative_filled_quantity - snap_before.cumulative_filled_quantity
            df = ev.cumulative_fee - snap_before.cumulative_fee
            key_ts = epoch_ref + timedelta(microseconds=trade_seq)
            trade_seq += 1
            close_refs[key_ts] = ev.fill_price
            trade_rows.append(
                {
                    "event_id": ev.event_id,
                    "order_id": ev.client_order_id,
                    "side": snap_before.intent.side.value,
                    "event_time": ev.event_time.isoformat(),
                    "execution_date": key_ts.isoformat(),
                    "cumulative_filled_quantity": ev.cumulative_filled_quantity,
                    "cumulative_fee": ev.cumulative_fee,
                    "executed_shares": dq,
                    "execution_price": ev.fill_price,
                    "fee": df,
                    "trade_notional": dq * ev.fill_price,
                    "slippage": ZERO,
                    "transaction_cost": df,
                }
            )

        for idx, b in enumerate(g1_bars):
            b_time = _parse_utc(b["time"])

            # Apply any observed partial BUY fills that occurred <= b_time via confirm_paper
            while buy_idx < len(g1_buy_fills) and _parse_utc(g1_buy_fills[buy_idx]["timestamp_utc"]) <= b_time:
                bf = g1_buy_fills[buy_idx]
                now[0] = _parse_utc(bf["timestamp_utc"])
                assert g1_buy_order_id is not None
                snap_before = broker.state.orders[g1_buy_order_id].snapshot
                dq = D(bf["quantity"])
                cum_q = snap_before.cumulative_filled_quantity + dq
                cum_fee = snap_before.cumulative_fee + D(bf["fee_usdt"])
                status = (
                    OrderStatus.FILLED
                    if cum_q == snap_before.intent.quantity
                    else OrderStatus.PARTIALLY_FILLED
                )
                ev = OrderEvent(
                    client_order_id=g1_buy_order_id,
                    exchange_order_id=snap_before.exchange_order_id,
                    status=status,
                    cumulative_filled_quantity=cum_q,
                    fill_price=D(bf["price"]),
                    event_time=now[0],
                    source="official-demo-replay",
                    event_id=f"demo-replay:{bf['unique_id']}",
                    cumulative_fee=cum_fee,
                )
                grid.confirm_paper(controller, ev, delivery=grid.delivery + 1)
                record_obs_fill(ev, snap_before)
                buy_idx += 1

            # Apply any observed hourly FundingEvents that occurred <= b_time
            while fund_idx < len(g1_funding_events) and _parse_utc(g1_funding_events[fund_idx].event_time) <= b_time:
                fev = g1_funding_events[fund_idx]
                now[0] = _parse_utc(fev.event_time)
                grid.apply_funding_paper(controller, fev, delivery=grid.delivery + 1)
                fund_idx += 1

            now[0] = b_time
            obs = {
                "id": b["bar_id"],
                "time": b_time.replace(tzinfo=None).isoformat(),
                "price": b["close"],
                "high": b["high"],
                "low": b["low"],
                "close": b["close"],
                "volume": int(b["volume"]),
                "completed_bar": True,
            }
            grid.observe_paper(controller, obs, delivery=grid.delivery + 1, observed_at=now[0])
            if grid.closed_by_breakout and g1_breakout_bar_utc is None:
                g1_breakout_bar_utc = b_time.isoformat()

            # Submit Gen 1 initial BUY proposal on bar 0 (matching observed g-00001-00000001)
            if idx == 0:
                for key, o in list(grid.orders.items()):
                    if o.status == OrderStatus.NEW and o.intent.side == Side.BUY:
                        order_meta[key] = {"purpose": "GRID"}
                        grid.submit_paper(controller, key, delivery=grid.delivery + 1)
                        g1_buy_order_id = key
                        break
            else:
                # Withdraw unsubmitted reverse SELL proposals (mirroring official Demo where
                # reverse SELL proposals before breakout were withdrawn on offline gap/recovery)
                unsub = [
                    o.intent.client_order_id
                    for o in grid.active
                    if o.intent.client_order_id not in broker.state.orders
                ]
                if unsub and grid.closed_by_breakout:
                    grid.withdraw_unsubmitted_paper(controller, unsub, delivery=grid.delivery + 1)

        # Replay remaining funding settlements before F11 exit
        while fund_idx < len(g1_funding_events):
            fev = g1_funding_events[fund_idx]
            now[0] = _parse_utc(fev.event_time)
            grid.apply_funding_paper(controller, fev, delivery=grid.delivery + 1)
            fund_idx += 1

        # Withdraw any unsubmitted local proposals left before F11 exit
        unsub_all = [
            o.intent.client_order_id
            for o in grid.active
            if o.intent.client_order_id not in broker.state.orders
        ]
        if unsub_all:
            grid.withdraw_unsubmitted_paper(controller, unsub_all, delivery=grid.delivery + 1)

        # Replay F11 reduce-only LIMIT exit at exact observed submit & fill timestamps
        exit_fill = g1_exit_fills[0]
        exit_price = D(exit_fill["price"])
        exit_qty = D(exit_fill["quantity"])
        exit_fill_ts = _parse_utc(exit_fill["timestamp_utc"])
        now[0] = exit_fill_ts
        exit_intent = grid.propose_limit_exit_paper(
            controller,
            {"price": exit_price, "quantity": exit_qty},
            delivery=grid.delivery + 1,
            quote=Quote("NVDAUSDT", exit_price, now[0]),
        )
        assert exit_intent is not None
        order_meta[exit_intent.client_order_id] = {"purpose": "RISK_EXIT"}
        grid.submit_paper(controller, exit_intent.client_order_id, delivery=grid.delivery + 1)
        snap_before = broker.state.orders[exit_intent.client_order_id].snapshot
        exit_ev = grid.fill_paper(
            controller, exit_intent.client_order_id, exit_qty, exit_price, delivery=grid.delivery + 1
        )
        assert exit_ev is not None
        record_obs_fill(exit_ev, snap_before)

        g1_terminal_state = grid.state
        g1_terminal_reason = grid.reason
        g1_post_exit_acct = project_paper_account(controller, "NVDAUSDT")

        # Replay F08 automatic rotation to Gen 2 (fl-d7345f8795464a65-g-00002)
        g2_grid = GridExecution(
            g2_cfg,
            rules,
            deployment_id=g2_id,
            account_id=acct_id,
            market=Market.BINANCE_USDM,
            instrument_id="NVDAUSDT",
            initial_equity=g1_post_exit_acct.equity,
            fee_rate=ZERO,
            continuous_capacity=True,
        )
        grid.rotate_paper(controller, g2_grid)
        grid = g2_grid

        g2_buy_submitted_id: str | None = None
        for idx, b in enumerate(g2_bars):
            b_time = _parse_utc(b["time"])
            now[0] = b_time
            obs = {
                "id": b["bar_id"],
                "time": b_time.replace(tzinfo=None).isoformat(),
                "price": b["close"],
                "high": b["high"],
                "low": b["low"],
                "close": b["close"],
                "volume": int(b["volume"]),
                "completed_bar": True,
            }
            grid.observe_paper(controller, obs, delivery=grid.delivery + 1, observed_at=now[0])
            if idx == 0:
                for key, o in list(grid.orders.items()):
                    if o.status == OrderStatus.NEW and o.intent.side == Side.BUY:
                        order_meta[key] = {"purpose": "GRID"}
                        grid.submit_paper(controller, key, delivery=grid.delivery + 1)
                        g2_buy_submitted_id = key
                        break

        # Final authoritative FIFO check over the replayed shared PaperStore
        final_acct = project_paper_account(controller, "NVDAUSDT")
        final_close = g2_bars[-1]["close"] if g2_bars else exit_price
        fill_events = [ev for _, ev in store.order_events() if ev.fill_price is not None]
        funding_events_booked = [ev for _, ev in store.funding_events()]
        fifo = attribute_events(
            {
                "config": {"initial_inventory": 0, "initial_price": 0},
                "rules": {"quantity_unit": 1},
                "orders": order_meta,
                "instrument_id": "NVDAUSDT",
                "market": Market.BINANCE_USDM.value,
                "capital": opening_wallet,
                "final": {"close": final_close, "equity": final_acct.equity},
            },
            trade_rows,
            close_refs,
            fill_events,
            broker.state,
            funding_events=funding_events_booked,
        )["summary"]

        observed_final_equity = D(empirical["fifo_reconciliation"]["final_equity_usdt"])
        observed_grid_net = D(empirical["cohorts"]["all_grid"]["net_pnl_usdt"])
        observed_funding = D(empirical["cohorts"]["all_grid"]["realized_funding_usdt"])
        observed_gross = D(empirical["cohorts"]["all_grid"]["realized_trade_gross_usdt"])

        g2_order_snap = broker.state.orders[g2_buy_submitted_id].snapshot if g2_buy_submitted_id else None
        # Verify against the exact first breakout bar observed in official Demo controller.sqlite3
        # (2026-11-22T05:20:00Z, close=129.54 < lower=129.59, two minutes after the 05:18:48Z BUY fill)
        first_out_of_band_bar = next(
            (
                b["time"].isoformat()
                for b in g1_bars
                if b["close"] < D(str(g1_cfg.lower)) or b["close"] > D(str(g1_cfg.upper))
            ),
            None,
        )
        first_upside_breach_bar = next(
            (b["time"].isoformat() for b in g1_bars if b["close"] > D(str(g1_cfg.upper))),
            None,
        )
        reconciled_checks = {
            "breakout_bar_matches_observed": g1_breakout_bar_utc == first_out_of_band_bar == "2026-11-22T05:20:00+00:00",
            "partial_buy_fills_replayed": len(g1_buy_fills) == 2,
            "funding_count_matches_observed": len(funding_events_booked) == 8,
            "funding_total_matches_observed": fifo["realized_funding"] == observed_funding,
            "exit_gross_matches_observed": fifo["realized_trade_pnl"] == observed_gross,
            "net_pnl_matches_observed": fifo["strategy_net_pnl"] == observed_grid_net,
            "final_equity_matches_observed": fifo["final_equity"] == observed_final_equity,
            "fifo_reconciliation_residual_zero": fifo["reconciliation_residual"] == ZERO,
            "g1_terminal_exited_breakout": (g1_terminal_state == "EXITED" and g1_terminal_reason == "BREAKOUT"),
            "f08_rotated_to_g2": (
                g2_order_snap is not None
                and g2_order_snap.intent.client_order_id == g2_buy_order["client_order_id"]
                and g2_order_snap.intent.limit_price == D(g2_buy_order["limit_price"])
                and g2_order_snap.status == OrderStatus.SUBMITTED
            ),
        }
        authoritative_replay = {
            "status": "LIFECYCLE_REPLAY_VERIFIED" if all(reconciled_checks.values()) else "REPLAY_MISMATCH",
            "checks": reconciled_checks,
            "all_checks_passed": all(reconciled_checks.values()),
            "opening_wallet_usdt": str(opening_wallet),
            "replayed_final_equity_usdt": str(fifo["final_equity"]),
            "observed_final_equity_usdt": str(observed_final_equity),
            "equity_difference_usdt": str(fifo["final_equity"] - observed_final_equity),
            "replayed_grid_net_pnl_usdt": str(fifo["strategy_net_pnl"]),
            "observed_grid_net_pnl_usdt": str(observed_grid_net),
            "replayed_gross_pnl_usdt": str(fifo["realized_trade_pnl"]),
            "replayed_funding_usdt": str(fifo["realized_funding"]),
            "replayed_fees_usdt": str(fifo["actual_fees"]),
            "fifo_reconciliation_residual_usdt": str(fifo["reconciliation_residual"]),
            "breakout_bar_utc": g1_breakout_bar_utc,
            "first_upside_breach_utc": first_upside_breach_bar,
            "g1_terminal_state": g1_terminal_state,
            "g1_terminal_reason": g1_terminal_reason,
            "g1_buy_order_id": g1_buy_order_id,
            "g1_exit_order_id": exit_intent.client_order_id,
            "g2_buy_order_id": g2_buy_submitted_id,
            "g2_buy_limit_price": str(g2_order_snap.intent.limit_price) if g2_order_snap else None,
            "g2_buy_status": g2_order_snap.status.value if g2_order_snap else None,
            "g1_bars_replayed": len(g1_bars),
            "g2_bars_replayed": len(g2_bars),
            "rotation_history": [
                {k: str(v) for k, v in item.items()}
                for item in broker.state.runtime.get("grid_rotation_history", ())
            ],
        }
    finally:
        store.db.close()

    # Part 2: Conditional Counterfactuals on Exact Observed Gen 1 1m Bars
    # Using observed implied hourly funding events (~22.97 bps/h) on the 1,284 1m bars
    g1_1m_bars_input = [
        {
            "id": b["bar_id"],
            "time": b["time"].isoformat(),
            "open": float(b["open"]),
            "high": float(b["high"]),
            "low": float(b["low"]),
            "close": float(b["close"]),
            "volume": float(b["volume"]),
        }
        for b in g1_bars
    ]
    # Build observed funding schedule (timestamps + exact rates inferred from observed Demo settlements)
    obs_funding_tuples: list[tuple[datetime, Decimal, Decimal, str]] = []
    bar_by_ts = {_parse_utc(b["time"]): b["close"] for b in g1_bars}
    for fev in g1_funding_events:
        fts = _parse_utc(fev.event_time)
        mk = bar_by_ts.get(fts, D("132.80"))
        # observed amount = -0.04 * mk * rate  => rate = -amount / (0.04 * mk)
        rate = (-fev.amount) / (D("0.04") * mk)
        obs_funding_tuples.append((fts, mk, rate, "ObservedDemo"))
    obs_funding_sched = FundingSchedule(
        mode="historical_events",
        events=tuple(obs_funding_tuples),
        label="OBSERVED_DEMO_HOURLY_FUNDING",
    )

    counterfactuals: list[dict[str, Any]] = []
    for cf_label, gr, prof in [
        ("MODEL_A_BASELINE_NO_F11_NO_F08", DemoGuardrails(enable_f11=False, enable_f08=False), execution_profile("IDEAL_LIMIT_FILL")),
        ("MODEL_B_F11_F08_IDEAL_IMMEDIATE", DemoGuardrails(enable_f11=True, enable_f08=True), execution_profile("IDEAL_LIMIT_FILL")),
        ("MODEL_B_F11_F08_LIMITED_CAPACITY", DemoGuardrails(enable_f11=True, enable_f08=True), execution_profile("LIMITED_CAPACITY_PARTIAL")),
        ("MODEL_B_F11_F08_DELAYED_EXIT", DemoGuardrails(enable_f11=True, enable_f08=True), execution_profile("DELAYED_EXIT_FILL")),
        ("MODEL_B_F11_F08_UNFILLED_TIMEOUT", DemoGuardrails(enable_f11=True, enable_f08=True), execution_profile("UNFILLED_EXIT_TIMEOUT")),
    ]:
        # Match official Demo Gen 1 anchor (130.90) & 0 Maker fee for clean counterfactual on same bars
        prof_0fee = replace(prof, fee_rate=ZERO, slippage=ZERO)
        res = run_offline_lifecycle(
            g1_1m_bars_input,
            rules=rules,
            guardrails=gr,
            assumptions=prof_0fee,
            funding=obs_funding_sched,
            opening_wallet=opening_wallet,
            strategy_cap_usdt=DEFAULT_STRATEGY_CAP,
            interval="1m",
            instrument_id="NVDAUSDT",
            account_id=f"cf-{cf_label.lower()}",
            family_prefix="fl-cf",
            initial_anchor_override=D("130.90"),
            order_ttl=1500,
        )
        counterfactuals.append(
            {
                "scenario": cf_label,
                "evidence_label": "CONDITIONAL_COUNTERFACTUAL_ON_OBSERVED_1M_BARS",
                "model_label": res["model_label"],
                "execution_profile": res["execution_profile"],
                "net_pnl_usdt": round(res["net_pnl_usdt"], 8),
                "net_return_on_7u_pct": round(res["net_return_pct"], 4),
                "realized_trade_gross_usdt": round(res["realized_trade_gross_usdt"], 8),
                "realized_funding_usdt": round(res["realized_funding_usdt"], 8),
                "max_drawdown_pct": round(res["max_drawdown_pct"], 4),
                "holding_hours": round(res["holding_hours"], 4),
                "mean_exit_delay_hours": round(res["mean_exit_delay_hours"], 4),
                "breakout_events": res["breakout_events"],
                "rotations_completed": res["rotations_completed"],
                "generations_total": res["generations_total"],
                "residual_inventory": res["residual_inventory"],
                "final_state": res["final_state"],
                "reconciled_zero_residual": res["reconciled_zero_residual"],
            }
        )

    return {
        "authoritative_replay": authoritative_replay,
        "gen1_1m_counterfactuals": counterfactuals,
    }
