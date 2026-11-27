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

        # Independent accounting reconciliation residual (double-entry close mark vs cumulated PnL)
        pnl_recon = self.initial_capital + by_symbol.sum(axis=1).cumsum()
        float64_nav = daily["cash"] + daily["value"]
        pnl_error = float((pnl_recon - float64_nav).abs().max())
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


# Pre-determined seven-layer numerical tolerances (frozen engineering contract; never relaxed post-hoc)
REFERENCE_REPLAY_TOLERANCES: dict[str, dict[str, float]] = {
    "target_weight": {"abs": 1e-10, "rel": 1e-10},
    "requested_qty": {"abs": 1e-6, "rel": 1e-9},
    "filled_qty": {"abs": 1e-6, "rel": 1e-9},
    "fill_price_usd": {"abs": 1e-6, "rel": 1e-9},
    "fill_turnover_usd": {"abs": 1e-4, "rel": 1e-9},
    "fill_fee_usd": {"abs": 1e-6, "rel": 1e-9},
    "daily_nav_usd": {"abs": 1e-4, "rel": 1e-10},
    "daily_weight": {"abs": 1e-8, "rel": 1e-8},
    "daily_value_usd": {"abs": 1e-4, "rel": 1e-10},
    "daily_fee_usd": {"abs": 1e-6, "rel": 1e-9},
    "attribution_usd": {"abs": 1e-4, "rel": 1e-10},
    "sp500_metric": {"abs": 1e-7, "rel": 1e-7},
    "qqq_metric": {"abs": 1e-7, "rel": 1e-7},
    "accounting_residual_usd": {"abs": 1e-4, "rel": 0.0},
}

REQUIRED_BENCHMARK_METRIC_KEYS: tuple[str, ...] = (
    "net_return",
    "max_dd",
    "cagr",
    "sharpe",
    "sortino",
    "calmar",
    "beta",
    "alpha",
)


def _compare_numeric_arrays(
    fl_vals: np.ndarray,
    ref_vals: np.ndarray,
    tol: dict[str, float],
    *,
    allow_empty: bool = False,
) -> dict[str, Any]:
    """Fail-closed elementwise comparison requiring finiteness and |fl - ref| <= abs_tol + rel_tol * |ref|."""
    a = np.asarray(fl_vals, dtype=np.float64)
    b = np.asarray(ref_vals, dtype=np.float64)
    if a.shape != b.shape:
        return {
            "passed": False,
            "max_abs_diff": float("nan"),
            "max_rel_diff": float("nan"),
            "has_non_finite": True,
            "reason": f"shape_mismatch:{a.shape}!={b.shape}",
        }
    if a.size == 0:
        return {
            "passed": bool(allow_empty),
            "max_abs_diff": 0.0 if allow_empty else float("nan"),
            "max_rel_diff": 0.0 if allow_empty else float("nan"),
            "has_non_finite": not allow_empty,
            "reason": None if allow_empty else "empty_array",
        }
    finite_ok = bool(np.isfinite(a).all() and np.isfinite(b).all())
    if not finite_ok:
        return {
            "passed": False,
            "max_abs_diff": float("nan"),
            "max_rel_diff": float("nan"),
            "has_non_finite": True,
            "reason": "non_finite_values",
        }
    abs_err = np.abs(a - b)
    denom = np.abs(b)
    with np.errstate(divide="ignore", invalid="ignore"):
        rel_err = np.where(denom > 0.0, abs_err / denom, np.where(abs_err == 0.0, 0.0, np.inf))
    bound = float(tol["abs"]) + float(tol["rel"]) * denom
    passed = bool(np.all(abs_err <= bound))
    return {
        "passed": passed,
        "max_abs_diff": float(np.max(abs_err)),
        "max_rel_diff": float(np.max(rel_err)),
        "has_non_finite": False,
        "reason": None if passed else "tolerance_exceeded",
    }


def _compare_metric_dicts(
    fl_metrics: dict[str, Any],
    ref_metrics: dict[str, Any],
    keys: tuple[str, ...],
    tol: dict[str, float],
) -> dict[str, Any]:
    """Fail-closed benchmark metric comparison across all required keys (no silent skipping of missing/NaN keys)."""
    abs_diffs: dict[str, float] = {}
    rel_diffs: dict[str, float] = {}
    missing_or_invalid: list[str] = []
    failed_keys: list[str] = []

    for k in keys:
        if k not in fl_metrics or k not in ref_metrics:
            missing_or_invalid.append(f"{k}:missing")
            continue
        fv = fl_metrics[k]
        rv = ref_metrics[k]
        # Treat pandas NA / numpy nan consistently
        fv_is_none = fv is None
        rv_is_none = rv is None
        if fv_is_none and rv_is_none:
            # Degenerate null allowed ONLY for derived ratios (e.g. calmar when max_dd==0), never for net_return/max_dd
            if k in ("net_return", "max_dd"):
                missing_or_invalid.append(f"{k}:both_none_disallowed")
            continue
        if fv_is_none != rv_is_none:
            missing_or_invalid.append(f"{k}:one_sided_none")
            continue
        try:
            ff = float(fv)
            rf = float(rv)
        except (TypeError, ValueError):
            missing_or_invalid.append(f"{k}:non_numeric")
            continue
        if not (np.isfinite(ff) and np.isfinite(rf)):
            missing_or_invalid.append(f"{k}:non_finite")
            continue
        ad = abs(ff - rf)
        rd = ad / abs(rf) if rf != 0.0 else (0.0 if ad == 0.0 else float("inf"))
        abs_diffs[k] = ad
        rel_diffs[k] = rd
        if ad > float(tol["abs"]) + float(tol["rel"]) * abs(rf):
            failed_keys.append(k)

    passed = bool(len(missing_or_invalid) == 0 and len(failed_keys) == 0 and len(abs_diffs) > 0)
    return {
        "passed": passed,
        "max_abs_diff": max(abs_diffs.values()) if (abs_diffs and not missing_or_invalid) else float("nan"),
        "max_rel_diff": max(rel_diffs.values()) if (rel_diffs and not missing_or_invalid) else float("nan"),
        "diffs": abs_diffs,
        "rel_diffs": rel_diffs,
        "missing_or_invalid_keys": missing_or_invalid,
        "failed_keys": failed_keys,
    }


def compare_against_reference(
    fl_result: IndependentReplayResult,
    ref_dir: Path,
    *,
    is_v69_fixture: bool = False,
    tolerances: dict[str, dict[str, float]] | None = None,
) -> dict[str, Any]:
    """Perform fail-closed 7-layer differential verification of FL independent replay against archived reference."""
    tols = tolerances if tolerances is not None else REFERENCE_REPLAY_TOLERANCES

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

    # ------------------------------------------------------------------
    # Layer 2: Targets diff (exact dates, zero duplicates, zero NaN, strict tolerance)
    # ------------------------------------------------------------------
    tickers = list(DEFAULT_TICKERS)
    fl_t_raw = fl_result.targets
    t_cols_present = all(c in fl_t_raw.columns for c in tickers) and all(c in orig_targets.columns for c in tickers)
    t_no_dups = bool(not fl_t_raw.index.duplicated().any() and not orig_targets.index.duplicated().any())
    t_index_exact = bool(
        len(fl_t_raw) == len(orig_targets)
        and len(orig_targets) > 0
        and t_no_dups
        and fl_t_raw.index.equals(orig_targets.index)
    )
    if t_cols_present and t_index_exact:
        fl_t = fl_t_raw[tickers].to_numpy(dtype=np.float64)
        or_t = orig_targets[tickers].to_numpy(dtype=np.float64)
        t_cmp = _compare_numeric_arrays(fl_t, or_t, tols["target_weight"])
        if not t_cmp["has_non_finite"]:
            t_abs = np.abs(fl_t - or_t)
            t_bound = tols["target_weight"]["abs"] + tols["target_weight"]["rel"] * np.abs(or_t)
            bad_mask = (t_abs > t_bound).any(axis=1)
            first_target_div = str(orig_targets.index[bad_mask][0].date()) if bad_mask.any() else None
        else:
            first_target_div = "NON_FINITE_TARGETS"
        layer2_targets_exact = bool(t_cmp["passed"])
    else:
        t_cmp = {"passed": False, "max_abs_diff": float("nan"), "max_rel_diff": float("nan"), "has_non_finite": True}
        first_target_div = "INDEX_OR_COLUMN_MISMATCH"
        layer2_targets_exact = False

    # ------------------------------------------------------------------
    # Layer 3 & 4: Orders & Fills diff (identity, duplicates, requested/filled qty, price, turnover, fee)
    # ------------------------------------------------------------------
    fl_f = fl_result.fills.reset_index(drop=True)
    or_f = orig_fills.reset_index(drop=True)
    req_fill_cols = ("date", "ticker", "side", "requested", "quantity", "price", "fee")
    fill_cols_present = all(c in fl_f.columns for c in req_fill_cols) and all(c in or_f.columns for c in req_fill_cols)
    has_turnover_col = ("turnover" in fl_f.columns) and ("turnover" in or_f.columns)
    fills_count_match = len(fl_f) == len(or_f)

    if fill_cols_present:
        fl_dup_keys = int(fl_f.duplicated(["date", "ticker", "side"]).sum()) if len(fl_f) else 0
        or_dup_keys = int(or_f.duplicated(["date", "ticker", "side"]).sum()) if len(or_f) else 0
    else:
        fl_dup_keys = or_dup_keys = -1

    if fill_cols_present and fills_count_match and fl_dup_keys == 0 and or_dup_keys == 0:
        if len(fl_f) == 0:
            same_keys = True
            req_cmp = _compare_numeric_arrays(np.array([]), np.array([]), tols["requested_qty"], allow_empty=True)
            qty_cmp = _compare_numeric_arrays(np.array([]), np.array([]), tols["filled_qty"], allow_empty=True)
            px_cmp = _compare_numeric_arrays(np.array([]), np.array([]), tols["fill_price_usd"], allow_empty=True)
            fee_cmp = _compare_numeric_arrays(np.array([]), np.array([]), tols["fill_fee_usd"], allow_empty=True)
            to_cmp = _compare_numeric_arrays(np.array([]), np.array([]), tols["fill_turnover_usd"], allow_empty=True)
            first_fill_px_div = None
        else:
            same_keys = bool(
                (pd.to_datetime(fl_f["date"]).to_numpy() == pd.to_datetime(or_f["date"]).to_numpy()).all()
                and (fl_f["ticker"].astype(str).to_numpy() == or_f["ticker"].astype(str).to_numpy()).all()
                and (fl_f["side"].astype(str).to_numpy() == or_f["side"].astype(str).to_numpy()).all()
            )
            if same_keys:
                req_cmp = _compare_numeric_arrays(fl_f["requested"].to_numpy(), or_f["requested"].to_numpy(), tols["requested_qty"])
                qty_cmp = _compare_numeric_arrays(fl_f["quantity"].to_numpy(), or_f["quantity"].to_numpy(), tols["filled_qty"])
                px_cmp = _compare_numeric_arrays(fl_f["price"].to_numpy(), or_f["price"].to_numpy(), tols["fill_price_usd"])
                fee_cmp = _compare_numeric_arrays(fl_f["fee"].to_numpy(), or_f["fee"].to_numpy(), tols["fill_fee_usd"])
                if has_turnover_col:
                    to_cmp = _compare_numeric_arrays(fl_f["turnover"].to_numpy(), or_f["turnover"].to_numpy(), tols["fill_turnover_usd"])
                else:
                    # Recompute implied turnover qty*price when reference omits explicit turnover column
                    to_cmp = _compare_numeric_arrays(
                        fl_f["quantity"].to_numpy() * fl_f["price"].to_numpy(),
                        or_f["quantity"].to_numpy() * or_f["price"].to_numpy(),
                        tols["fill_turnover_usd"],
                    )
                if not px_cmp["has_non_finite"]:
                    px_abs = np.abs(fl_f["price"].to_numpy(dtype=np.float64) - or_f["price"].to_numpy(dtype=np.float64))
                    px_bound = tols["fill_price_usd"]["abs"] + tols["fill_price_usd"]["rel"] * np.abs(or_f["price"].to_numpy(dtype=np.float64))
                    px_bad = fl_f.loc[px_abs > px_bound, "date"]
                    first_fill_px_div = str(pd.Timestamp(px_bad.iloc[0]).date()) if len(px_bad) else None
                else:
                    first_fill_px_div = "NON_FINITE_PRICE"
            else:
                req_cmp = qty_cmp = px_cmp = fee_cmp = to_cmp = {
                    "passed": False,
                    "max_abs_diff": float("nan"),
                    "max_rel_diff": float("nan"),
                    "has_non_finite": True,
                }
                first_fill_px_div = "ORDER_IDENTITY_MISMATCH"
    else:
        same_keys = False
        req_cmp = qty_cmp = px_cmp = fee_cmp = to_cmp = {
            "passed": False,
            "max_abs_diff": float("nan"),
            "max_rel_diff": float("nan"),
            "has_non_finite": True,
        }
        first_fill_px_div = "DUPLICATE_ORDER_IDENTITY" if (fl_dup_keys > 0 or or_dup_keys > 0) else "ROW_COUNT_OR_SCHEMA_MISMATCH"

    order_identity_exact = bool(fill_cols_present and fills_count_match and fl_dup_keys == 0 and or_dup_keys == 0 and same_keys)

    # ------------------------------------------------------------------
    # Layer 5: Daily ledger (exact index, zero duplicates, NAV, weight, value, fee, accounting audit)
    # ------------------------------------------------------------------
    fl_d_raw = fl_result.daily
    d_no_dups = bool(not fl_d_raw.index.duplicated().any() and not orig_daily.index.duplicated().any())
    d_index_exact = bool(
        len(fl_d_raw) == len(orig_daily)
        and len(orig_daily) > 0
        and d_no_dups
        and fl_d_raw.index.equals(orig_daily.index)
    )
    d_cols_present = all(c in fl_d_raw.columns for c in ("nav", "weight")) and all(c in orig_daily.columns for c in ("nav", "weight"))
    if d_index_exact and d_cols_present:
        nav_cmp = _compare_numeric_arrays(fl_d_raw["nav"].to_numpy(), orig_daily["nav"].to_numpy(), tols["daily_nav_usd"])
        wt_cmp = _compare_numeric_arrays(fl_d_raw["weight"].to_numpy(), orig_daily["weight"].to_numpy(), tols["daily_weight"])
        if "value" in orig_daily.columns and "value" in fl_d_raw.columns:
            val_cmp = _compare_numeric_arrays(fl_d_raw["value"].to_numpy(), orig_daily["value"].to_numpy(), tols["daily_value_usd"])
        else:
            val_cmp = {"passed": False, "max_abs_diff": float("nan"), "max_rel_diff": float("nan"), "has_non_finite": True}
        if "fee" in orig_daily.columns and "fee" in fl_d_raw.columns:
            dfee_cmp = _compare_numeric_arrays(fl_d_raw["fee"].to_numpy(), orig_daily["fee"].to_numpy(), tols["daily_fee_usd"])
        else:
            dfee_cmp = {"passed": False, "max_abs_diff": float("nan"), "max_rel_diff": float("nan"), "has_non_finite": True}
        if not nav_cmp["has_non_finite"]:
            nav_abs = np.abs(fl_d_raw["nav"].to_numpy(dtype=np.float64) - orig_daily["nav"].to_numpy(dtype=np.float64))
            nav_bound = tols["daily_nav_usd"]["abs"] + tols["daily_nav_usd"]["rel"] * np.abs(orig_daily["nav"].to_numpy(dtype=np.float64))
            nav_bad = orig_daily.index[nav_abs > nav_bound]
            first_nav_div = str(nav_bad[0].date()) if len(nav_bad) else None
        else:
            first_nav_div = "NON_FINITE_NAV"
    else:
        nav_cmp = wt_cmp = val_cmp = dfee_cmp = {
            "passed": False,
            "max_abs_diff": float("nan"),
            "max_rel_diff": float("nan"),
            "has_non_finite": True,
        }
        first_nav_div = "INDEX_OR_COLUMN_MISMATCH"

    acct_res = float(fl_result.accounting_audit.get("max_pnl_nav_residual_usd", float("nan")))
    zero_borrowing = bool(fl_result.accounting_audit.get("zero_borrowing", False))
    acct_audit_exact = bool(zero_borrowing and np.isfinite(acct_res) and acct_res <= tols["accounting_residual_usd"]["abs"])

    # ------------------------------------------------------------------
    # Layer 6: Per-ticker attribution (exact ticker set, zero duplicates, zero NaN)
    # ------------------------------------------------------------------
    fl_attr = fl_result.attribution
    attr_no_dups = bool(not fl_attr.index.duplicated().any() and not orig_attr.index.duplicated().any())
    attr_index_exact = bool(
        len(fl_attr) == len(orig_attr)
        and len(orig_attr) > 0
        and attr_no_dups
        and set(fl_attr.index) == set(orig_attr.index)
    )
    if attr_index_exact:
        fl_attr_aligned = fl_attr.reindex(orig_attr.index)
        attr_cmp = _compare_numeric_arrays(fl_attr_aligned.to_numpy(), orig_attr.to_numpy(), tols["attribution_usd"])
        per_ticker_diff = {k: float(abs(fl_attr_aligned.at[k] - orig_attr.at[k])) for k in orig_attr.index}
    else:
        attr_cmp = {"passed": False, "max_abs_diff": float("nan"), "max_rel_diff": float("nan"), "has_non_finite": True}
        per_ticker_diff = {}

    # ------------------------------------------------------------------
    # Layer 7: Dual-benchmark metrics (S&P 500 TR AND QQQ; zero silent skipping)
    # ------------------------------------------------------------------
    sp_cmp = _compare_metric_dicts(fl_result.sp500_metrics, orig_sp500, REQUIRED_BENCHMARK_METRIC_KEYS, tols["sp500_metric"])
    qqq_ref = orig_result.get("metrics", {}) if isinstance(orig_result, dict) else {}
    qqq_cmp = _compare_metric_dicts(fl_result.qqq_metrics, qqq_ref, REQUIRED_BENCHMARK_METRIC_KEYS, tols["qqq_metric"])

    # ------------------------------------------------------------------
    # Final fail-closed 7-layer exact reference match contract
    # ------------------------------------------------------------------
    layer_checks = {
        "layer2_targets_exact": layer2_targets_exact,
        "layer3_4_order_identity_exact": order_identity_exact,
        "layer3_4_requested_qty_exact": bool(req_cmp["passed"]),
        "layer3_4_filled_qty_exact": bool(qty_cmp["passed"]),
        "layer3_4_fill_price_exact": bool(px_cmp["passed"]),
        "layer3_4_fill_turnover_exact": bool(to_cmp["passed"]),
        "layer3_4_fill_fee_exact": bool(fee_cmp["passed"]),
        "layer5_daily_index_exact": d_index_exact,
        "layer5_daily_nav_exact": bool(nav_cmp["passed"]),
        "layer5_daily_weight_exact": bool(wt_cmp["passed"]),
        "layer5_daily_value_exact": bool(val_cmp["passed"]),
        "layer5_daily_fee_exact": bool(dfee_cmp["passed"]),
        "layer5_accounting_audit_exact": acct_audit_exact,
        "layer6_attribution_exact": bool(attr_index_exact and attr_cmp["passed"]),
        "layer7_sp500_metrics_exact": bool(sp_cmp["passed"]),
        "layer7_qqq_metrics_exact": bool(qqq_cmp["passed"]),
    }
    failed_checks = [name for name, ok in layer_checks.items() if not ok]
    exact_match = bool(len(failed_checks) == 0)

    return {
        "version": fl_result.version,
        "window": fl_result.window,
        "case": fl_result.case,
        "signal_source": fl_result.signal_source,
        "execution_engine": fl_result.execution_engine,
        "open_price_mode": fl_result.open_price_mode,
        "exact_reference_match": exact_match,
        "failed_checks": failed_checks,
        "layer_checks": layer_checks,
        "tolerances": tols,
        "layer2_targets": {
            "rows": int(len(orig_targets)),
            "fl_rows": int(len(fl_t_raw)),
            "index_exact": t_index_exact,
            "max_abs_weight_diff": float(t_cmp["max_abs_diff"]),
            "max_rel_weight_diff": float(t_cmp["max_rel_diff"]),
            "first_divergence_date": first_target_div,
        },
        "layer3_4_orders_and_fills": {
            "orig_fills": int(len(or_f)),
            "fl_fills": int(len(fl_f)),
            "orig_duplicate_keys": int(or_dup_keys),
            "fl_duplicate_keys": int(fl_dup_keys),
            "count_and_keys_match": order_identity_exact,
            "max_requested_qty_diff": float(req_cmp["max_abs_diff"]),
            "max_requested_qty_rel_diff": float(req_cmp["max_rel_diff"]),
            "max_filled_qty_diff": float(qty_cmp["max_abs_diff"]),
            "max_filled_qty_rel_diff": float(qty_cmp["max_rel_diff"]),
            "max_fill_price_diff_usd": float(px_cmp["max_abs_diff"]),
            "max_fill_price_rel_diff": float(px_cmp["max_rel_diff"]),
            "max_turnover_diff_usd": float(to_cmp["max_abs_diff"]),
            "max_fee_diff_usd": float(fee_cmp["max_abs_diff"]),
            "max_fee_rel_diff": float(fee_cmp["max_rel_diff"]),
            "first_price_divergence_date": first_fill_px_div,
        },
        "layer5_daily_ledger": {
            "days": int(len(orig_daily)),
            "fl_days": int(len(fl_d_raw)),
            "index_exact": d_index_exact,
            "max_abs_nav_diff_usd": float(nav_cmp["max_abs_diff"]),
            "max_rel_nav_diff": float(nav_cmp["max_rel_diff"]),
            "max_abs_weight_diff": float(wt_cmp["max_abs_diff"]),
            "max_abs_value_diff_usd": float(val_cmp["max_abs_diff"]),
            "max_abs_daily_fee_diff_usd": float(dfee_cmp["max_abs_diff"]),
            "first_nav_divergence_date": first_nav_div,
            "accounting_residual_usd": acct_res,
            "zero_borrowing": zero_borrowing,
        },
        "layer6_attribution": {
            "index_exact": attr_index_exact,
            "max_abs_ticker_pnl_diff_usd": float(attr_cmp["max_abs_diff"]),
            "max_rel_ticker_pnl_diff": float(attr_cmp["max_rel_diff"]),
            "per_ticker_diff_usd": per_ticker_diff,
        },
        "layer7_metrics": {
            "max_sp500_metric_diff": float(sp_cmp["max_abs_diff"]),
            "max_sp500_metric_rel_diff": float(sp_cmp["max_rel_diff"]),
            "sp500_diffs": sp_cmp["diffs"],
            "sp500_missing_or_invalid_keys": sp_cmp["missing_or_invalid_keys"],
            "sp500_failed_keys": sp_cmp["failed_keys"],
            "max_qqq_metric_diff": float(qqq_cmp["max_abs_diff"]),
            "max_qqq_metric_rel_diff": float(qqq_cmp["max_rel_diff"]),
            "qqq_diffs": qqq_cmp["diffs"],
            "qqq_missing_or_invalid_keys": qqq_cmp["missing_or_invalid_keys"],
            "qqq_failed_keys": qqq_cmp["failed_keys"],
        },
    }
