"""Single-security entry/exit targets using the existing A-share order executor.

NaN means hold; zero means liquidate; a positive weight means enter if flat.
Entry weights size a new position, not continuous daily rebalancing. Failed
orders retry on later opens, until filled or superseded by a later close target.
The close target can only affect the following session. No terminal liquidation.
"""
import math

import numpy as np
import pandas as pd

from factorlab.backtest.execution import (
    AShareExecutionBacktester, ExecutionBacktestConfig, PositionBook,
    _prepare_bars, _TRADE_COLUMNS, _REJECTION_COLUMNS,
)
from factorlab.errors import SchemaError


def run_target_positions(targets, bars, calendar, config=None, fee_schedule=None,
                         price_limit_schedule=None, *, buy_at_first_open=False):
    """SOFTWARE_ONLY entry/exit adapter; never invokes TopKPortfolio."""
    config = config or ExecutionBacktestConfig()
    engine = AShareExecutionBacktester(config, fee_schedule, price_limit_schedule)
    prepared = _prepare_bars(bars, config)
    dates = pd.DatetimeIndex(calendar)
    if (dates.empty or dates.hasnans or dates.tz is not None or dates.has_duplicates
            or not dates.is_monotonic_increasing or not dates.equals(dates.normalize())):
        raise SchemaError("target calendar must contain ordered unique daily dates")
    if not {"date", "symbol", "target_weight"}.issubset(targets):
        raise SchemaError("targets require date, symbol, target_weight")
    if not pd.DatetimeIndex(targets.date).equals(dates) or targets.symbol.nunique() != 1:
        raise SchemaError("targets must contain one security and one row per session")
    symbol = targets.symbol.iloc[0]
    if set(prepared.symbol) != {symbol} or len(dates.difference(prepared.date)):
        raise SchemaError("target execution bars must cover the security/calendar")
    weights = pd.to_numeric(targets.target_weight, errors="coerce")
    if ((targets.target_weight.notna() & weights.isna()).any()
            or (weights.notna() & (~np.isfinite(weights) | (weights < 0) | (weights > 1))).any()):
        raise SchemaError("target weight must be NaN (hold) or within [0, 1]")
    if buy_at_first_open and (pd.isna(weights.iloc[0]) or weights.iloc[0] <= 0):
        raise SchemaError("first-open mandate requires a positive initial target")
    book, cash = PositionBook(), float(config.initial_cash)
    trades, rejections, ledger = [], [], []
    indexed = prepared.set_index("date")
    pending = (float(weights.iloc[0]), pd.NaT) if buy_at_first_open else None
    for offset, day in enumerate(dates):
        row = indexed.loc[day]
        # The benchmark's first target is consumed before its first open.
        if offset > 0 and not (buy_at_first_open and offset == 1):
            target = weights.iloc[offset - 1]
            if pd.notna(target):
                pending = (float(target), dates[offset - 1])
        shares = book.total_shares(symbol)
        if pending is not None:
            weight, signal_day = pending
            side = "sell" if weight == 0 else "buy"
            if (weight == 0 and shares == 0) or (weight > 0 and shares > 0):
                pending = None
            else:
                price = row[config.open_price_column]
                if weight == 0:
                    requested = shares
                elif pd.isna(price):
                    requested = config.board_lot
                else:
                    requested = math.floor(cash * weight / float(price) / config.board_lot) * config.board_lot
                    if requested == 0:
                        engine._record_rejection(rejections, signal_day, day, symbol, side,
                                                 np.nan, np.nan, config.board_lot,
                                                 config.board_lot, "cash_constraint")
                budget = cash * weight if side == "buy" else cash
                filled = None if requested == 0 else engine._execute_order(
                    book=book, day_bars=pd.DataFrame([row]).set_index("symbol", drop=False),
                    trade_rows=trades, rejection_rows=rejections, signal_date=signal_day,
                    execution_date=day, symbol=symbol, side=side,
                    requested_shares=requested, cash=budget, metadata=None,
                )
                if filled is not None:
                    cash += filled[3] - budget
                    trades[-1]["cash_after"] = cash
                    pending = None
        shares = book.total_shares(symbol)
        mark = row[config.close_price_column]
        if pd.isna(mark) or not np.isfinite(mark) or mark <= 0:
            raise SchemaError("daily close mark must be finite and positive, including suspended sessions")
        value = shares * float(mark)
        equity = cash + value
        if cash < -1e-8 or shares < 0 or equity <= 0:
            raise SchemaError("invalid target cash/position/equity ledger")
        ledger.append(dict(date=day, symbol=symbol, shares=shares,
                           sellable_shares=book.sellable_shares(symbol, day, t_plus_one=config.t_plus_one),
                           mark_price=float(mark), market_value=value, cash=cash,
                           portfolio_value=equity, strategy_nav=equity / config.initial_cash,
                           pending_target=np.nan if pending is None else pending[0]))
    nav = pd.DataFrame(ledger)
    nav["strategy_return"] = nav.strategy_nav.pct_change()
    nav.loc[0, "strategy_return"] = nav.strategy_nav.iloc[0] - 1.0
    return dict(nav=nav, trades=pd.DataFrame(trades, columns=_TRADE_COLUMNS),
                rejections=pd.DataFrame(rejections, columns=_REJECTION_COLUMNS))
