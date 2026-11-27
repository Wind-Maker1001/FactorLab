"""Phase 2: FactorLab independent historical replay engine and 7-layer differential verification.

This module NEVER invokes Qlib's backtest/matching engine to produce FactorLab results.
All order planning, sell-before-buy matching, volume capping, fee/slippage/delay/partial-fill
execution, cash/position accounting, daily NAV, per-ticker attribution, and benchmark metrics
are computed exclusively by FactorLab's own independent backtester.
"""
from __future__ import annotations

from dataclasses import dataclass
import importlib
import json
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
from typing import Any, Protocol

import numpy as np
import pandas as pd

from factorlab.strategies.v69_native import NativeV69

from .config import DEFAULT_TICKERS, StressScenarioSpec, WindowSpec


@dataclass(frozen=True)
class MarketPanel:
    """Aligned OHLCV market data and S&P 500 Total Return index for one fixture."""

    tickers: tuple[str, ...]
    close: pd.DataFrame
    opening: pd.DataFrame
    high: pd.DataFrame
    low: pd.DataFrame
    volume: pd.DataFrame
    benchmark_close: pd.Series
    source_dir: str
    benchmark_path: str


def load_market_panel(package_dir: Path, window: WindowSpec, tickers: tuple[str, ...] = DEFAULT_TICKERS) -> MarketPanel:
    """Load frozen OHLCV parquet files and benchmark series strictly up to window.end."""
    data_dir = package_dir / window.market_subdir
    bars: dict[str, pd.DataFrame] = {}
    for sym in tickers:
        p = data_dir / f"{sym}.parquet"
        if p.is_file():
            bars[sym] = pd.read_parquet(p).loc[: window.end]
    close = pd.DataFrame({s: q["close"] for s, q in bars.items()}).reindex(columns=list(tickers))
    opening = pd.DataFrame({s: q["open"] for s, q in bars.items()}).reindex(columns=list(tickers))
    high = pd.DataFrame({s: q["high"] for s, q in bars.items()}).reindex(columns=list(tickers))
    low = pd.DataFrame({s: q["low"] for s, q in bars.items()}).reindex(columns=list(tickers))
    volume = pd.DataFrame({s: q["volume"] for s, q in bars.items()}).reindex(columns=list(tickers))
    bench_path = package_dir / window.benchmark_file
    benchmark = pd.read_parquet(bench_path)["close"].sort_index()
    return MarketPanel(
        tickers=tickers,
        close=close,
        opening=opening,
        high=high,
        low=low,
        volume=volume,
        benchmark_close=benchmark,
        source_dir=str(data_dir),
        benchmark_path=str(bench_path),
    )


class SignalGenerator(Protocol):
    """Protocol for any strategy signal generator plugged into FactorLab independent replay."""

    strategy_name: str
    signal_source: str

    def targets(
        self,
        previous: pd.Timestamp,
        prices: pd.Series,
        valid_prices: pd.Series,
        held: pd.Series,
    ) -> pd.Series: ...

    def indicator_snapshot(self, previous: pd.Timestamp) -> dict[str, Any]: ...


class NativeV69SignalAdapter:
    """Adapter around FactorLab's native V69 implementation (zero external source dependency)."""

    strategy_name = "v69"
    signal_source = "NATIVE_FL"

    def __init__(
        self,
        close: pd.DataFrame,
        volume: pd.DataFrame,
        opening: pd.DataFrame,
        *,
        omit_sndk: bool = False,
        omit_symbols: tuple[str, ...] = (),
        market_index: pd.Series | None = None,
    ) -> None:
        self._omit_symbols = tuple(sorted(set(omit_symbols) | ({"SNDK"} if omit_sndk else set())))
        self._engine = NativeV69(close, volume, opening, omit_sndk=omit_sndk)

    def targets(
        self,
        previous: pd.Timestamp,
        prices: pd.Series,
        valid_prices: pd.Series,
        held: pd.Series,
    ) -> pd.Series:
        w = self._engine.targets(previous, prices, valid_prices, held).copy()
        for sym in self._omit_symbols:
            if sym in w.index:
                w.loc[sym] = 0.0
        return w

    def indicator_snapshot(self, previous: pd.Timestamp) -> dict[str, Any]:
        return {
            "breadth": float(self._engine.breadth.at[previous]) if pd.notna(self._engine.breadth.at[previous]) else None,
            "qqq_ma63": float(self._engine.qqq_ma63.at[previous]) if pd.notna(self._engine.qqq_ma63.at[previous]) else None,
            "ma28": {k: (float(v) if pd.notna(v) else None) for k, v in self._engine.ma28.loc[previous].items()},
            "vol21": {k: (float(v) if pd.notna(v) else None) for k, v in self._engine.vol.loc[previous].items()},
        }


def _install_inert_qlib_shims() -> dict[str, ModuleType | None]:
    """Install inert Qlib base-class shims solely to import read-only strategy target math without Qlib."""
    saved: dict[str, ModuleType | None] = {}
    if "qlib" in sys.modules and not getattr(sys.modules["qlib"], "_factorlab_inert_shim", False):
        return saved

    class _InertBaseStrategy:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

    class _InertOrder:
        SELL = 0
        BUY = 1

        def __init__(self, stock_id: str, amount: float, direction: int, start_time: Any = None, end_time: Any = None) -> None:
            self.stock_id = stock_id
            self.amount = amount
            self.direction = direction
            self.start_time = start_time
            self.end_time = end_time

    class _InertTradeDecisionWO:
        def __init__(self, order_list: list[Any], strategy: Any) -> None:
            self.order_list = order_list
            self.strategy = strategy

    def _blocked_backtest(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("Qlib backtest engine is strictly forbidden in FactorLab independent replay")

    modules = {
        "qlib": ModuleType("qlib"),
        "qlib.backtest": ModuleType("qlib.backtest"),
        "qlib.backtest.decision": ModuleType("qlib.backtest.decision"),
        "qlib.strategy": ModuleType("qlib.strategy"),
        "qlib.strategy.base": ModuleType("qlib.strategy.base"),
    }
    modules["qlib"]._factorlab_inert_shim = True  # type: ignore[attr-defined]
    modules["qlib"].init = _blocked_backtest  # type: ignore[attr-defined]
    modules["qlib.backtest"].backtest = _blocked_backtest  # type: ignore[attr-defined]
    modules["qlib.backtest.decision"].Order = _InertOrder  # type: ignore[attr-defined]
    modules["qlib.backtest.decision"].TradeDecisionWO = _InertTradeDecisionWO  # type: ignore[attr-defined]
    modules["qlib.strategy.base"].BaseStrategy = _InertBaseStrategy  # type: ignore[attr-defined]

    for name, mod in modules.items():
        saved[name] = sys.modules.get(name)
        sys.modules[name] = mod
    return saved


def _restore_modules(saved: dict[str, ModuleType | None]) -> None:
    for name, old in saved.items():
        if old is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = old


class ExternalPackageSignalAdapter:
    """Read-only signal adapter loading frozen external V179/V182 targets() without porting or Qlib matching.

    Used while P10-03 native V179/V182 is pending. All execution, matching, fees, capacity, cash,
    and daily accounting run exclusively in FactorLab's independent backtester.
    """

    def __init__(
        self,
        version: str,
        package_dir: Path,
        close: pd.DataFrame,
        volume: pd.DataFrame,
        opening: pd.DataFrame,
        *,
        market_index: pd.Series,
        omit_sndk: bool = False,
        omit_symbols: tuple[str, ...] = (),
        blend_override: tuple[float, float] | None = None,
    ) -> None:
        self.strategy_name = version
        self.signal_source = "EXTERNAL_PACKAGE_FROZEN_SIGNAL_PENDING_P10_03"
        self._omit_symbols = tuple(sorted(set(omit_symbols) | ({"SNDK"} if omit_sndk else set())))
        self._blend_override = blend_override

        pkg_str = str(package_dir.resolve())
        saved_qlib = _install_inert_qlib_shims()
        added_path = False
        if pkg_str not in sys.path:
            sys.path.insert(0, pkg_str)
            added_path = True
        try:
            mod = importlib.import_module(f"{version}.strategy")
            self._strategy = mod.Strategy(
                close,
                volume,
                opening,
                market_index=market_index,
                omit_sndk=omit_sndk,
            )
        finally:
            if added_path and pkg_str in sys.path:
                sys.path.remove(pkg_str)
            _restore_modules(saved_qlib)

    def targets(
        self,
        previous: pd.Timestamp,
        prices: pd.Series,
        valid_prices: pd.Series,
        held: pd.Series,
    ) -> pd.Series:
        if self._blend_override is not None:
            # Research-only sensitivity perturbation of blend shares (never alters frozen strategy source)
            from v125.strategy import Strategy as Growth  # type: ignore[import-not-found]

            high_share, low_share = self._blend_override
            growth = Growth.targets(self._strategy, previous, prices, valid_prices, held)
            defensive = super(type(self._strategy).__mro__[1], self._strategy).targets(previous, prices, valid_prices, held)
            rule = self._strategy.blend_rule
            if rule == "trend":
                cond = self._strategy.index.at[previous] > self._strategy.index_long.at[previous] and self._strategy.breadth.at[previous] >= 0.5
            else:
                cond = self._strategy.breadth_change.at[previous] > 0.10 and self._strategy.index.at[previous] > self._strategy.market_fast.at[previous]
            share = high_share if cond else low_share
            w = (growth * share + defensive * (1.0 - share)).copy()
        else:
            w = self._strategy.targets(previous, prices, valid_prices, held).copy()
        for sym in self._omit_symbols:
            if sym in w.index:
                w.loc[sym] = 0.0
        return w

    def indicator_snapshot(self, previous: pd.Timestamp) -> dict[str, Any]:
        s = self._strategy
        return {
            "breadth": float(s.breadth.at[previous]) if pd.notna(s.breadth.at[previous]) else None,
            "index": float(s.index.at[previous]) if hasattr(s, "index") and pd.notna(s.index.at[previous]) else None,
            "index_long_200": float(s.index_long.at[previous]) if hasattr(s, "index_long") and pd.notna(s.index_long.at[previous]) else None,
            "market_fast_63": float(s.market_fast.at[previous]) if hasattr(s, "market_fast") and pd.notna(s.market_fast.at[previous]) else None,
            "breadth_change_10": float(s.breadth_change.at[previous]) if hasattr(s, "breadth_change") and pd.notna(s.breadth_change.at[previous]) else None,
        }


def build_signal_provider(
    version: str,
    market: MarketPanel,
    *,
    package_dir: Path,
    omit_sndk: bool = False,
    omit_symbols: tuple[str, ...] = (),
    blend_override: tuple[float, float] | None = None,
) -> SignalGenerator:
    """Resolve native FactorLab strategy when available; otherwise use read-only frozen signal adapter."""
    if version == "v69":
        return NativeV69SignalAdapter(
            market.close,
            market.volume,
            market.opening,
            omit_sndk=omit_sndk,
            omit_symbols=omit_symbols,
            market_index=market.benchmark_close,
        )
    # Check if P10-03 native module exists in factorlab.strategies
    native_mod_name = f"factorlab.strategies.{version}_native"
    try:
        native_mod = importlib.import_module(native_mod_name)
        factory = getattr(native_mod, "build_signal_provider", None)
        if callable(factory):
            return factory(
                market.close,
                market.volume,
                market.opening,
                market_index=market.benchmark_close,
                omit_sndk=omit_sndk,
                omit_symbols=omit_symbols,
            )
    except ImportError:
        pass
    if version in ("v179", "v182"):
        return ExternalPackageSignalAdapter(
            version,
            package_dir,
            market.close,
            market.volume,
            market.opening,
            market_index=market.benchmark_close,
            omit_sndk=omit_sndk,
            omit_symbols=omit_symbols,
            blend_override=blend_override,
        )
    raise ValueError(f"Unsupported strategy version: {version}")


def compute_risk_metrics(returns: pd.Series | np.ndarray, benchmark_returns: pd.Series | np.ndarray) -> dict[str, float | None]:
    """Compute CAGR, MaxDD, Sharpe, Sortino, Calmar, Beta, Alpha (matching exact benchmark convention)."""
    r = np.asarray(returns, dtype=np.float64)
    m = np.asarray(benchmark_returns, dtype=np.float64)
    nav = np.r_[1.0, np.cumprod(1.0 + r)]
    dd = float(np.min(nav / np.maximum.accumulate(nav) - 1.0))
    net_ret = float(nav[-1] - 1.0)
    bench_ret = float(np.prod(1.0 + m) - 1.0) if len(m) else 0.0
    if len(r) < 30:
        return {
            "net_return": net_ret,
            "max_dd": dd,
            "cagr": None,
            "sortino": None,
            "calmar": None,
            "sharpe": None,
            "beta": None,
            "alpha": None,
            "benchmark_return": bench_ret,
        }
    cagr = float(nav[-1] ** (252.0 / len(r)) - 1.0)
    down = float(np.sqrt(np.mean(np.minimum(r, 0.0) ** 2)))
    sd = float(r.std(ddof=1))
    m_var = float(m.var(ddof=1))
    beta = float(np.cov(r, m, ddof=1)[0, 1] / m_var) if m_var > 0 else float("nan")
    return {
        "net_return": net_ret,
        "max_dd": dd,
        "cagr": cagr,
        "sortino": float(np.sqrt(252.0) * r.mean() / down) if down > 0 else None,
        "calmar": float(cagr / abs(dd)) if dd < 0 else None,
        "sharpe": float(np.sqrt(252.0) * r.mean() / sd) if sd > 0 else None,
        "beta": beta if np.isfinite(beta) else None,
        "alpha": float((r.mean() - beta * m.mean()) * 252.0) if np.isfinite(beta) else None,
        "benchmark_return": bench_ret,
    }


@dataclass(frozen=True)
class IndependentReplayResult:
    """Complete artifacts from one FactorLab independent historical replay."""

    version: str
    window: str
    case: str
    signal_source: str
    execution_engine: str
    open_price_mode: str
    daily: pd.DataFrame
    fills: pd.DataFrame
    targets: pd.DataFrame
    attribution: pd.Series
    orders: pd.DataFrame
    sp500_metrics: dict[str, Any]
    qqq_metrics: dict[str, Any]
    accounting_audit: dict[str, float]


class FactorLabIndependentBacktester:
    """Pure-FactorLab daily-bar portfolio backtester with reference-compatible and stress modes."""

    def __init__(
        self,
        *,
        initial_capital: float = 300_000.0,
        fee_rate: float = 0.001,
        slippage_bps: float = 0.0,
        rebalance_deadband: float = 0.005,
        volume_cap_ratio: float = 0.001,
        fill_ratio: float = 1.0,
        execution_delay_days: int = 0,
        open_price_mode: str = "float64",
        gross_cap: float = 0.90,
        tickers: tuple[str, ...] = DEFAULT_TICKERS,
    ) -> None:
        self.initial_capital = float(initial_capital)
        self.fee_rate = float(fee_rate)
        self.slippage_bps = float(slippage_bps)
        self.rebalance_deadband = float(rebalance_deadband)
        self.volume_cap_ratio = float(volume_cap_ratio)
        self.fill_ratio = float(fill_ratio)
        self.execution_delay_days = int(execution_delay_days)
        self.open_price_mode = open_price_mode
        self.gross_cap = float(gross_cap)
        self.tickers = tickers

    def _execution_price(
        self,
        market: MarketPanel,
        day: pd.Timestamp,
        symbol: str,
        side: str,
    ) -> float:
        raw_open = float(market.opening.at[day, symbol])
        if self.open_price_mode == "qlib_float32":
            base = float(np.float32(raw_open))
        elif self.open_price_mode == "open_high_mid":
            if side == "buy":
                base = 0.5 * (raw_open + float(market.high.at[day, symbol]))
            else:
                base = 0.5 * (raw_open + float(market.low.at[day, symbol]))
        else:
            base = raw_open
        slip = self.slippage_bps / 10_000.0
        return base * (1.0 + slip) if side == "buy" else base * (1.0 - slip)

    def run(
        self,
        signal_provider: SignalGenerator,
        market: MarketPanel,
        *,
        window_name: str,
        start: str,
        end: str,
        case: str = "normal",
    ) -> IndependentReplayResult:
        """Execute a complete causal daily backtest in FactorLab's independent accounting engine."""
        full_index = market.close.index
        sessions = full_index[(full_index >= pd.Timestamp(start)) & (full_index <= pd.Timestamp(end))]
        if len(sessions) == 0:
            raise ValueError(f"Empty trading calendar for {window_name} [{start}, {end}]")

        cash = self.initial_capital
        held = pd.Series(0.0, index=list(self.tickers), dtype=np.float64)
        last_week: tuple[int, int] | None = None

        pending_orders: dict[pd.Timestamp, list[dict[str, Any]]] = {}
        fill_rows: list[dict[str, Any]] = []
        order_rows: list[dict[str, Any]] = []
        target_rows: list[dict[str, Any]] = []

        nav_list: list[float] = []
        cash_list: list[float] = []
        val_list: list[float] = []
        fee_list: list[float] = []
        qqq_list: list[float] = []
        etf_equiv_list: list[float] = []
        qty_history = pd.DataFrame(0.0, index=sessions, columns=list(self.tickers), dtype=np.float64)
        trade_pnl = pd.DataFrame(0.0, index=sessions, columns=list(self.tickers), dtype=np.float64)

        for idx_in_window, day in enumerate(sessions):
            iso = day.isocalendar()
            week = (int(iso.year), int(iso.week))
            i_full = int(full_index.get_loc(day))
            if i_full == 0:
                raise ValueError("At least one prior warmup session is required before backtest start")
            previous = full_index[i_full - 1]

            # 1. Generate weekly decision at first session of each ISO week (strictly using previous close)
            if week != last_week:
                prices = market.close.loc[previous, list(self.tickers)]
                volumes = market.volume.loc[previous, list(self.tickers)]
                valid = np.isfinite(prices) & prices.gt(0.0)
                tradable = valid & np.isfinite(volumes) & volumes.ge(0.0)
                invalid_held = held.ne(0.0) & ~tradable
                if invalid_held.any():
                    raise ValueError(f"Missing valid prior quote for held symbols: {list(held.index[invalid_held])}")

                weights = signal_provider.targets(previous, prices, valid, held)
                if not np.isfinite(weights).all() or weights.lt(-1e-12).any():
                    raise ValueError("Non-finite or negative target weights")
                if float(weights.sum()) > self.gross_cap + 1e-8:
                    raise ValueError(f"Target weight sum {weights.sum()} exceeds gross_cap {self.gross_cap}")

                held_value = (held * prices.where(tradable)).fillna(0.0)
                prev_nav = float(cash + held_value.sum())
                sells: list[dict[str, Any]] = []
                buys: list[dict[str, Any]] = []
                for sym in self.tickers:
                    if not tradable[sym]:
                        continue
                    change = float(weights[sym] - held_value[sym] / prev_nav)
                    if weights[sym] == 0.0:
                        amount = float(-held[sym])
                    elif abs(change) >= self.rebalance_deadband:
                        amount = float(change * prev_nav / prices[sym])
                    else:
                        continue
                    size = min(abs(amount), self.volume_cap_ratio * float(volumes[sym]))
                    if size <= 1e-8:
                        continue
                    side = "sell" if amount < 0 else "buy"
                    rec = {
                        "signal_date": previous,
                        "decision_date": day,
                        "ticker": sym,
                        "side": side,
                        "requested": float(size),
                    }
                    (sells if side == "sell" else buys).append(rec)

                ordered_intents = sells + buys
                exec_idx = idx_in_window + self.execution_delay_days
                if exec_idx < len(sessions):
                    exec_day = sessions[exec_idx]
                    pending_orders.setdefault(exec_day, []).extend(ordered_intents)
                    for rec in ordered_intents:
                        order_rows.append({**rec, "scheduled_exec_date": exec_day})
                target_rows.append({"date": day, "signal_date": previous, **{k: float(weights[k]) for k in self.tickers}})
                last_week = week

            # 2. Execute scheduled orders at today's open (sells first, then buys; strict cash constraint)
            day_fee = 0.0
            day_orders = pending_orders.pop(day, [])
            if day_orders:
                # Guarantee sell-before-buy even if delayed orders overlap
                day_orders = [o for o in day_orders if o["side"] == "sell"] + [o for o in day_orders if o["side"] == "buy"]
                for order in day_orders:
                    sym = order["ticker"]
                    side = order["side"]
                    req = float(order["requested"])
                    px = self._execution_price(market, day, sym, side)
                    if not np.isfinite(px) or px <= 0.0:
                        continue
                    # Apply partial fill ratio (independent stress)
                    eff_req = req * self.fill_ratio
                    if side == "sell":
                        qty = min(eff_req, float(held[sym]))
                        if qty <= 0.0:
                            continue
                        turnover = qty * px
                        fee = turnover * self.fee_rate
                        held[sym] -= qty
                        if abs(held[sym]) < 1e-12:
                            held[sym] = 0.0
                        cash += turnover - fee
                        side_sign = -1.0
                    else:
                        # Strict cash-funded long-only matching: never borrow cash
                        max_affordable = max(0.0, cash / (px * (1.0 + self.fee_rate)))
                        qty = min(eff_req, max_affordable)
                        if qty <= 0.0:
                            continue
                        turnover = qty * px
                        fee = turnover * self.fee_rate
                        held[sym] += qty
                        cash -= turnover + fee
                        side_sign = 1.0

                    close_today = float(market.close.at[day, sym])
                    trade_pnl.at[day, sym] += side_sign * qty * (close_today - px) - fee
                    day_fee += fee
                    fill_rows.append(
                        {
                            "date": day,
                            "ticker": sym,
                            "side": side,
                            "quantity": float(qty),
                            "requested": float(req),
                            "price": float(px),
                            "turnover": float(turnover),
                            "fee": float(fee),
                        }
                    )

            # 3. End-of-day mark-to-market valuation
            close_row = market.close.loc[day, list(self.tickers)]
            if (held.abs().gt(1e-8) & close_row.isna()).any():
                raise ValueError(f"Held symbol lacks valid close mark on {day}")
            mark_close = (
                close_row.astype(np.float32).astype(np.float64)
                if self.open_price_mode == "qlib_float32"
                else close_row
            )
            marks = (held * mark_close).fillna(0.0)
            equity_val = float(marks.sum())
            nav = float(cash + equity_val)
            nav_list.append(nav)
            cash_list.append(float(cash))
            val_list.append(float((held * close_row).fillna(0.0).sum()))
            fee_list.append(float(day_fee))
            qqq_list.append(float(close_row["QQQ"]))
            etf_equiv_list.append(
                float(((held * close_row).fillna(0.0)["QQQ"] + 3.0 * (held * close_row).fillna(0.0)["TQQQ"]) / nav)
                if nav > 0
                else 0.0
            )
            qty_history.loc[day] = held

        # 4. Build ledgers, attribution, and dual-benchmark metrics
        daily = pd.DataFrame(
            {
                "nav": nav_list,
                "fee": fee_list,
                "value": val_list,
                "QQQ": qqq_list,
                "cash": cash_list,
            },
            index=sessions,
        )
        daily["weight"] = daily["value"] / daily["nav"]
        daily["index_equivalent_etf_weight"] = etf_equiv_list
        net_daily = daily["nav"].pct_change()
        net_daily.iloc[0] = daily["nav"].iloc[0] / self.initial_capital - 1.0
        qqq_daily = market.close["QQQ"].pct_change(fill_method=None).reindex(sessions)
        sp500_daily = market.benchmark_close.pct_change(fill_method=None).reindex(sessions)
        daily["net_daily"] = net_daily
        daily["qqq_daily"] = qqq_daily
        daily["sp500_daily"] = sp500_daily

        close_win = market.close.reindex(sessions)
        prev_win = market.close.shift(1).reindex(sessions)
        by_symbol = (qty_history.shift(1, fill_value=0.0) * (close_win - prev_win)).fillna(0.0) + trade_pnl
        attribution = by_symbol.sum().rename("net_usd")

        # Independent accounting reconciliation residual
        pnl_recon = self.initial_capital + by_symbol.sum(axis=1).cumsum()
        pnl_error = float((pnl_recon - daily["nav"]).abs().max())
        min_cash = float(daily["cash"].min())
        min_qty = float(qty_history.min().min())

        fills_cols = ["date", "ticker", "side", "quantity", "requested", "price", "turnover", "fee"]
        fills_df = pd.DataFrame(fill_rows, columns=fills_cols)
        orders_df = pd.DataFrame(order_rows)
        targets_df = pd.DataFrame(target_rows).set_index("date") if target_rows else pd.DataFrame()

        sp500_m = compute_risk_metrics(net_daily, sp500_daily)
        sp500_m.update(
            days=int(len(sessions)),
            fills=int((fills_df["quantity"] > 0).sum()) if len(fills_df) else 0,
            fees=float(fills_df["fee"].sum()) if len(fills_df) else 0.0,
            turnover_usd=float(fills_df["turnover"].sum()) if len(fills_df) else 0.0,
            exposure=float(daily["weight"].mean()),
            net_usd=float(daily["nav"].iloc[-1] - self.initial_capital),
            final_nav=float(daily["nav"].iloc[-1]),
            benchmark_name="S&P 500 Total Return (^SP500TR)",
        )
        qqq_m = compute_risk_metrics(net_daily, qqq_daily)
        qqq_m.update(
            days=int(len(sessions)),
            fills=sp500_m["fills"],
            fees=sp500_m["fees"],
            exposure=sp500_m["exposure"],
            net_usd=sp500_m["net_usd"],
            benchmark_name="QQQ (internal reference)",
        )

        return IndependentReplayResult(
            version=signal_provider.strategy_name,
            window=window_name,
            case=case,
            signal_source=signal_provider.signal_source,
            execution_engine="FACTORLAB_INDEPENDENT_BACKTESTER",
            open_price_mode=self.open_price_mode,
            daily=daily,
            fills=fills_df,
            targets=targets_df,
            attribution=attribution,
            orders=orders_df,
            sp500_metrics=sp500_m,
            qqq_metrics=qqq_m,
            accounting_audit={
                "max_pnl_nav_residual_usd": pnl_error,
                "min_cash_usd": min_cash,
                "min_position_qty": min_qty,
                "zero_borrowing": bool(min_cash >= -1e-6 and min_qty >= -1e-8),
            },
        )


def compare_against_reference(
    fl_result: IndependentReplayResult,
    ref_dir: Path,
    *,
    is_v69_fixture: bool = False,
) -> dict[str, Any]:
    """Perform 7-layer differential verification of FL independent replay against archived reference."""
    orig_daily = pd.read_parquet(ref_dir / "daily.parquet")
    orig_fills = pd.read_csv(ref_dir / "fills.csv", parse_dates=["date"])
    orig_targets = pd.read_csv(ref_dir / "targets.csv", parse_dates=["date", "signal_date"]).set_index("date")
    orig_attr = pd.read_csv(ref_dir / "attribution.csv", index_col=0)["net_usd"]
    orig_result = json.loads((ref_dir / "result.json").read_text(encoding="utf-8"))
    if is_v69_fixture:
        sp_df = pd.read_csv(ref_dir / "sp500_report.csv")
        orig_sp500 = sp_df.loc[sp_df["case"] == "normal"].iloc[0].to_dict()
    else:
        orig_sp500 = json.loads((ref_dir / "sp500_metrics.json").read_text(encoding="utf-8"))

    # Layer 2: Targets diff
    tickers = list(DEFAULT_TICKERS)
    fl_t = fl_result.targets.reindex(index=orig_targets.index, columns=tickers)
    or_t = orig_targets[tickers]
    t_diff = (fl_t - or_t).abs()
    max_target_diff = float(t_diff.max().max())
    t_bad_days = t_diff.index[t_diff.max(axis=1) > 1e-10]
    first_target_div = str(t_bad_days[0].date()) if len(t_bad_days) else None

    # Layer 3 & 4: Orders & Fills diff
    fl_f = fl_result.fills.reset_index(drop=True)
    or_f = orig_fills.reset_index(drop=True)
    fills_count_match = len(fl_f) == len(or_f)
    if fills_count_match and len(fl_f) > 0:
        same_keys = bool(
            (fl_f["date"].to_numpy() == or_f["date"].to_numpy()).all()
            and (fl_f["ticker"].to_numpy() == or_f["ticker"].to_numpy()).all()
            and (fl_f["side"].to_numpy() == or_f["side"].to_numpy()).all()
        )
        max_req_diff = float((fl_f["requested"] - or_f["requested"]).abs().max()) if same_keys else float("nan")
        max_qty_diff = float((fl_f["quantity"] - or_f["quantity"]).abs().max()) if same_keys else float("nan")
        max_px_diff = float((fl_f["price"] - or_f["price"]).abs().max()) if same_keys else float("nan")
        max_fee_diff = float((fl_f["fee"] - or_f["fee"]).abs().max()) if same_keys else float("nan")
        px_bad = fl_f.loc[(fl_f["price"] - or_f["price"]).abs() > 1e-9, "date"] if same_keys else pd.Series(dtype="datetime64[ns]")
        first_fill_px_div = str(px_bad.iloc[0].date()) if len(px_bad) else None
    else:
        same_keys = False
        max_req_diff = max_qty_diff = max_px_diff = max_fee_diff = float("nan")
        first_fill_px_div = "ROW_COUNT_MISMATCH"

    # Layer 5: Daily NAV & exposure diff
    fl_d = fl_result.daily.reindex(orig_daily.index)
    nav_diff = (fl_d["nav"] - orig_daily["nav"]).abs()
    max_nav_diff_usd = float(nav_diff.max())
    max_nav_rel_diff = float((nav_diff / orig_daily["nav"].abs()).max())
    max_weight_diff = float((fl_d["weight"] - orig_daily["weight"]).abs().max())
    nav_bad = nav_diff.index[nav_diff > 1e-6]
    first_nav_div = str(nav_bad[0].date()) if len(nav_bad) else None

    # Layer 6: Per-ticker attribution diff
    attr_diff = (fl_result.attribution.reindex(orig_attr.index) - orig_attr).abs()
    max_attr_diff_usd = float(attr_diff.max())

    # Layer 7: S&P 500 TR & QQQ metrics diff
    sp_keys = ("net_return", "max_dd", "cagr", "sharpe", "sortino", "calmar", "beta", "alpha")
    sp_diffs = {
        k: abs(float(fl_result.sp500_metrics[k]) - float(orig_sp500[k]))
        for k in sp_keys
        if k in orig_sp500 and fl_result.sp500_metrics.get(k) is not None
    }
    qqq_ref = orig_result["metrics"]
    qqq_diffs = {
        k: abs(float(fl_result.qqq_metrics[k]) - float(qqq_ref[k]))
        for k in sp_keys
        if k in qqq_ref and qqq_ref[k] is not None and fl_result.qqq_metrics.get(k) is not None
    }

    exact_match = (
        max_target_diff < 1e-10
        and fills_count_match
        and same_keys
        and max_nav_diff_usd < 1e-4
        and max_attr_diff_usd < 1e-4
        and max(sp_diffs.values(), default=0.0) < 1e-7
    )

    return {
        "version": fl_result.version,
        "window": fl_result.window,
        "case": fl_result.case,
        "signal_source": fl_result.signal_source,
        "execution_engine": fl_result.execution_engine,
        "open_price_mode": fl_result.open_price_mode,
        "exact_reference_match": exact_match,
        "layer2_targets": {
            "rows": int(len(orig_targets)),
            "max_abs_weight_diff": max_target_diff,
            "first_divergence_date": first_target_div,
        },
        "layer3_4_orders_and_fills": {
            "orig_fills": int(len(or_f)),
            "fl_fills": int(len(fl_f)),
            "count_and_keys_match": bool(fills_count_match and same_keys),
            "max_requested_qty_diff": max_req_diff,
            "max_filled_qty_diff": max_qty_diff,
            "max_fill_price_diff_usd": max_px_diff,
            "max_fee_diff_usd": max_fee_diff,
            "first_price_divergence_date": first_fill_px_div,
        },
        "layer5_daily_ledger": {
            "days": int(len(orig_daily)),
            "max_abs_nav_diff_usd": max_nav_diff_usd,
            "max_rel_nav_diff": max_nav_rel_diff,
            "max_abs_weight_diff": max_weight_diff,
            "first_nav_divergence_date": first_nav_div,
            "accounting_residual_usd": fl_result.accounting_audit["max_pnl_nav_residual_usd"],
        },
        "layer6_attribution": {
            "max_abs_ticker_pnl_diff_usd": max_attr_diff_usd,
            "per_ticker_diff_usd": {k: float(v) for k, v in attr_diff.items()},
        },
        "layer7_metrics": {
            "max_sp500_metric_diff": max(sp_diffs.values(), default=0.0),
            "sp500_diffs": sp_diffs,
            "max_qqq_metric_diff": max(qqq_diffs.values(), default=0.0),
            "qqq_diffs": qqq_diffs,
        },
    }
