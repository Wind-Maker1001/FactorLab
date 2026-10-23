"""Rule-aware A-share daily execution tests."""

from datetime import date

import numpy as np
import pandas as pd
import pytest

from qbot.research.backtest import (
    ExecutionBacktestConfig,
    FeeSchedule,
    PositionBook,
    PriceLimitSchedule,
    classify_board,
    run_execution_backtest,
)
from qbot.research.errors import ConfigurationError, SchemaError


def _bars(dates, symbols, *, overrides=None):
    overrides = overrides or {}
    rows = []
    for date_number, trading_date in enumerate(dates):
        for symbol_number, symbol in enumerate(symbols):
            values = {
                "date": trading_date,
                "symbol": symbol,
                "raw_open": 10.0 + symbol_number,
                "raw_close": 10.0 + symbol_number,
                "volume": 100_000.0,
                "trade_status": 1,
                "is_st": False,
            }
            values.update(overrides.get((trading_date, symbol), {}))
            rows.append(values)
    return pd.DataFrame(rows)


def _benchmark(dates):
    return pd.DataFrame(
        {
            "date": dates,
            "adj_open": 4999.5 + np.arange(len(dates), dtype=float),
            "adj_close": 5000.0 + np.arange(len(dates), dtype=float),
        }
    )


def _scores(dates, symbols):
    rows = []
    for signal_number, signal_date in enumerate((dates[0], dates[2])):
        preferred = symbols[signal_number]
        for symbol in symbols:
            rows.append(
                {
                    "date": signal_date,
                    "symbol": symbol,
                    "score": 1.0 if symbol == preferred else 0.0,
                }
            )
    return pd.DataFrame(rows)


def _config(initial_cash=10_000.0):
    return ExecutionBacktestConfig(
        top_k=1,
        rebalance_days=2,
        minimum_cross_section=2,
        initial_cash=initial_cash,
        board_lot=100,
        price_limit_tolerance_bps=5.0,
    )


def test_position_book_enforces_same_day_t_plus_one():
    book = PositionBook()
    day = pd.Timestamp("2024-01-03")
    book.buy("000001.SZ", day, 100)
    assert book.total_shares("000001.SZ") == 100
    assert book.sellable_shares("000001.SZ", day, t_plus_one=True) == 0
    assert book.sellable_shares(
        "000001.SZ", day + pd.offsets.BDay(1), t_plus_one=True
    ) == 100
    with pytest.raises(ConfigurationError, match="non-sellable"):
        book.sell("000001.SZ", day, 100, t_plus_one=True)


def test_execution_uses_next_open_board_lots_cash_and_t_plus_one():
    dates = pd.bdate_range("2024-01-02", periods=6)
    symbols = ["000001.SZ", "000002.SZ"]
    result = run_execution_backtest(
        _scores(dates, symbols),
        _bars(dates, symbols),
        _benchmark(dates),
        config=_config(),
        fee_schedule=FeeSchedule.a_share_default(slippage_bps=0.0),
    )

    assert not result.trades.empty
    assert (result.trades["execution_date"] > result.trades["signal_date"]).all()
    assert (result.trades["executed_shares"] % 100 == 0).all()
    assert (result.nav["cash"] >= -1e-8).all()
    assert result.nav.iloc[0]["benchmark_return"] == pytest.approx(
        result.nav.iloc[0]["benchmark_nav"] - 1.0
    )
    first_buy = result.trades.iloc[0]
    assert first_buy["side"] == "buy"
    first_day_positions = result.positions.loc[
        result.positions["date"] == first_buy["execution_date"]
    ]
    assert first_day_positions["sellable_shares"].eq(0).all()
    assert {"buy", "sell"}.issubset(set(result.trades["side"]))
    assert result.nav["holdings"].max() == 1


def test_execution_fails_fast_when_an_actual_order_lacks_status_evidence():
    dates = pd.bdate_range("2024-01-02", periods=4)
    symbols = ["000001.SZ", "000002.SZ"]
    bars = _bars(
        dates,
        symbols,
        overrides={(dates[1], "000001.SZ"): {"trade_status": pd.NA}},
    )

    with pytest.raises(SchemaError, match="execution status is missing"):
        run_execution_backtest(
            _scores(dates, symbols),
            bars,
            _benchmark(dates),
            config=_config(),
        )


@pytest.mark.parametrize("column", ["trade_status", "is_st"])
def test_execution_requires_explicit_status_columns(column):
    dates = pd.bdate_range("2024-01-02", periods=4)
    symbols = ["000001.SZ", "000002.SZ"]

    with pytest.raises(SchemaError, match="execution bars missing columns"):
        run_execution_backtest(
            _scores(dates, symbols),
            _bars(dates, symbols).drop(columns=column),
            _benchmark(dates),
            config=_config(),
        )


def test_suspension_and_opening_price_limits_create_explicit_rejections():
    dates = pd.bdate_range("2024-01-02", periods=4)
    symbols = ["000001.SZ", "000002.SZ"]
    overrides = {
        (dates[1], "000001.SZ"): {
            "raw_open": 11.0,
            "raw_close": 11.0,
        },
        (dates[1], "000002.SZ"): {
            "volume": 0.0,
            "trade_status": 0,
        },
    }
    scores = pd.DataFrame(
        [
            {"date": dates[0], "symbol": "000001.SZ", "score": 2.0},
            {"date": dates[0], "symbol": "000002.SZ", "score": 1.0},
        ]
    )
    limit_result = run_execution_backtest(
        scores,
        _bars(dates, symbols, overrides=overrides),
        _benchmark(dates),
        config=_config(),
    )
    assert limit_result.trades.empty
    assert set(limit_result.rejections["reason"]) == {"buy_price_limit"}

    suspended_scores = scores.copy()
    suspended_scores.loc[
        suspended_scores["symbol"] == "000002.SZ", "score"
    ] = 3.0
    suspended_result = run_execution_backtest(
        suspended_scores,
        _bars(dates, symbols, overrides=overrides),
        _benchmark(dates),
        config=_config(),
    )
    assert suspended_result.trades.empty
    assert set(suspended_result.rejections["reason"]) == {"suspended"}


def test_cash_constraint_never_creates_fractional_or_negative_cash():
    dates = pd.bdate_range("2024-01-02", periods=3)
    symbols = ["000001.SZ", "000002.SZ"]
    scores = pd.DataFrame(
        [
            {"date": dates[0], "symbol": "000001.SZ", "score": 2.0},
            {"date": dates[0], "symbol": "000002.SZ", "score": 1.0},
        ]
    )
    result = run_execution_backtest(
        scores,
        _bars(dates, symbols),
        _benchmark(dates),
        config=_config(initial_cash=1_000.0),
    )
    assert result.trades.empty
    assert "cash_constraint" in set(result.rejections["reason"])
    assert (result.nav["cash"] >= 0.0).all()


def test_historical_board_and_st_price_limit_schedule():
    schedule = PriceLimitSchedule.a_share_default()
    assert classify_board("000001.SZ") == "main"
    assert classify_board("300001.SZ") == "chinext"
    assert classify_board("688001.SH") == "star"
    assert schedule.bounds(
        "000001.SZ", date(2024, 1, 2), 10.0, is_st=False
    ).upper == pytest.approx(11.0)
    assert schedule.bounds(
        "000001.SZ", date(2024, 1, 2), 10.0, is_st=True
    ).upper == pytest.approx(10.5)
    assert schedule.bounds(
        "300001.SZ", date(2020, 8, 21), 10.0, is_st=False
    ).upper == pytest.approx(11.0)
    assert schedule.bounds(
        "300001.SZ", date(2020, 8, 24), 10.0, is_st=False
    ).upper == pytest.approx(12.0)
