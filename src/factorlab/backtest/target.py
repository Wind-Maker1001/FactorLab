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
    _prepare_bars, _trade_frame, _REJECTION_COLUMNS,
)
from factorlab.errors import SchemaError


def run_target_positions(targets, bars, calendar, config=None, fee_schedule=None,
                         price_limit_schedule=None, *, buy_at_first_open=False,
                         dividend_plan=None):
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
    actions = None
    if dividend_plan is not None:
        from factorlab.backtest.corporate_actions import DividendExecutionLedger
        if config.open_price_column != "raw_open" or config.close_price_column != "raw_close":
            raise SchemaError("dividend execution requires raw_open/raw_close; adjusted prices double-count")
        actions = DividendExecutionLedger(dividend_plan, symbol, dates)
    trades, rejections, ledger = [], [], []
    indexed = prepared.set_index("date")
    pending = (float(weights.iloc[0]), pd.NaT) if buy_at_first_open else None
    for offset, day in enumerate(dates):
        row = indexed.loc[day]
        prior_cash = cash
        if actions is not None:
            cash += actions.before_open(day)
        opening_cash = cash
        # The benchmark's first target is consumed before its first open.
        if offset > 0 and not (buy_at_first_open and offset == 1):
            target = weights.iloc[offset - 1]
            if pd.notna(target):
                pending = (float(target), dates[offset - 1])
        trade_cash_start = cash
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
        trade_flow = cash - trade_cash_start
        if actions is not None:
            cash += actions.after_orders(day, book, symbol)
        mark = row[config.close_price_column]
        if pd.isna(mark) or not np.isfinite(mark) or mark <= 0:
            raise SchemaError("daily close mark must be finite and positive, including suspended sessions")
        value = shares * float(mark)
        equity = cash + value
        if actions is not None:
            balances = actions.balances()
            equity += balances["restricted_cash"] + balances["recognized_receivables"]
        if cash < -1e-8 or shares < 0 or equity <= 0:
            raise SchemaError("invalid target cash/position/equity ledger")
        ledger.append(dict(date=day, symbol=symbol, shares=shares,
                           sellable_shares=book.sellable_shares(symbol, day, t_plus_one=config.t_plus_one),
                           mark_price=float(mark), market_value=value, cash=cash,
                           portfolio_value=equity, strategy_nav=equity / config.initial_cash,
                           pending_target=np.nan if pending is None else pending[0]))
        if actions is not None:
            ledger[-1].update(balances, opening_available_cash=opening_cash,
                              available_cash=cash, cash=cash + balances["restricted_cash"],
                              equity_net=None if actions.unknown_tax else equity,
                              equity_before_tax_charges=equity + balances["tax_accrued"],
                              trade_cash_flow=trade_flow, dividend_cash_flow=cash - prior_cash - trade_flow,
                              lots=book.lot_snapshot(symbol))
    nav = pd.DataFrame(ledger)
    nav["strategy_return"] = nav.strategy_nav.pct_change()
    nav.loc[0, "strategy_return"] = nav.strategy_nav.iloc[0] - 1.0
    result = dict(nav=nav, trades=_trade_frame(trades),
                  rejections=pd.DataFrame(rejections, columns=_REJECTION_COLUMNS))
    if actions is not None:
        result.update(entitlements=pd.DataFrame(actions.entitlements), payments=pd.DataFrame(actions.audit),
                      recognitions=pd.DataFrame(actions.recognitions),
                      scope="SOFTWARE_ONLY", financial_admission="FORMAL_ADMISSION_BLOCKED",
                      payment_policy="DATE_ONLY_AFTER_OPEN_ORDERS; AVAILABLE_NEXT_OBSERVED_OPEN",
                      net_status="UNKNOWN" if actions.unknown_tax else "SYNTHETIC_EXPLICIT_OR_NO_ENTITLEMENT")
        from factorlab.backtest.corporate_actions import dividend_return_accounting
        result["return_accounting"] = dividend_return_accounting(
            nav, result["payments"], result["recognitions"], initial_cash=config.initial_cash)
    return result
