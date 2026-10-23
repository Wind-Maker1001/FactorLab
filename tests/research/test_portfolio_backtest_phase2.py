"""Offline Phase-2 tests for Top-K construction and fast research backtests."""

from datetime import date

import numpy as np
import pandas as pd
import pytest

from qbot.research.backtest.fast import FastBacktestConfig, run_fast_backtest
from qbot.research.backtest.fees import FeeRule, FeeSchedule
from qbot.research.config import FeesConfig, StampDutyPeriod
from qbot.research.errors import ConfigurationError, SchemaError
from qbot.research.metrics.portfolio import calculate_portfolio_metrics
from qbot.research.portfolio.topk import rebalance_dates, select_top_k
from qbot.research.portfolio.weights import equal_weights


SYMBOLS = ["000001.SZ", "600000.SH", "600001.SH"]


def _zero_fee_schedule():
    return FeeSchedule(
        [
            FeeRule(
                effective_from=date(2000, 1, 1),
                commission_bps=0.0,
                minimum_commission_cny=0.0,
                slippage_bps=0.0,
            )
        ]
    )


def test_topk_is_deterministic_equal_weight_and_uses_trading_day_schedule():
    dates = pd.bdate_range("2024-01-05", periods=4)
    rows = []
    for trading_date in dates:
        # Deliberately reverse input order and tie the two highest scores.
        rows.extend(
            [
                {"date": trading_date, "symbol": "600001.SH", "score": 2.0},
                {"date": trading_date, "symbol": "000001.SZ", "score": 2.0},
                {"date": trading_date, "symbol": "600000.SH", "score": 1.0},
            ]
        )
    selected = select_top_k(
        pd.DataFrame(rows), top_k=2, rebalance_every=2, trading_dates=dates
    )

    assert selected["date"].drop_duplicates().tolist() == [dates[0], dates[2]]
    for _, cross_section in selected.groupby("date", sort=True):
        assert cross_section["symbol"].tolist() == ["000001.SZ", "600001.SH"]
        assert cross_section["rank"].tolist() == [1, 2]
        assert cross_section["target_weight"].tolist() == pytest.approx([0.5, 0.5])

    # Friday + two trading sessions lands on Tuesday, not Sunday.
    assert rebalance_dates(dates, every_n_days=2).tolist() == [dates[0], dates[2]]
    weights = equal_weights(SYMBOLS)
    assert weights.sum() == pytest.approx(1.0)
    assert weights.tolist() == pytest.approx([1 / 3.0] * 3)


@pytest.mark.parametrize(
    ("top_k", "minimum_cross_section", "expected_required"),
    [(3, 2, 3), (2, 3, 3)],
)
def test_topk_fails_fast_on_a_sparse_scheduled_rebalance(
    top_k, minimum_cross_section, expected_required
):
    dates = pd.bdate_range("2024-01-02", periods=2)
    scores = pd.DataFrame(
        [
            {"date": dates[0], "symbol": "000001.SZ", "score": 2.0},
            {"date": dates[0], "symbol": "600000.SH", "score": 1.0},
            {"date": dates[0], "symbol": "600001.SH", "score": 0.5},
            {"date": dates[1], "symbol": "000001.SZ", "score": 2.0},
            {"date": dates[1], "symbol": "600000.SH", "score": np.nan},
            {"date": dates[1], "symbol": "600001.SH", "score": np.nan},
        ]
    )

    with pytest.raises(
        SchemaError,
        match=(
            r"Top-K rebalance on 2024-01-03 has finite-score count 1; at least %d "
            r"are required" % expected_required
        ),
    ):
        select_top_k(
            scores,
            top_k=top_k,
            rebalance_every=1,
            trading_dates=dates,
            minimum_cross_section=minimum_cross_section,
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("top_k", True),
        ("top_k", 2.5),
        ("rebalance_days", False),
        ("rebalance_days", 2.5),
    ],
)
def test_fast_backtest_config_rejects_non_integer_schedule_values(field, value):
    options = {field: value}
    with pytest.raises(ConfigurationError, match="positive integer"):
        FastBacktestConfig(**options)


@pytest.mark.parametrize("value", [True, np.nan, np.inf, -np.inf, 0.0])
def test_fast_backtest_config_rejects_invalid_initial_cash(value):
    with pytest.raises(ConfigurationError, match="finite and positive"):
        FastBacktestConfig(initial_cash=value)


def test_topk_rejects_non_numeric_score_instead_of_treating_it_as_missing():
    scores = pd.DataFrame(
        {
            "date": [pd.Timestamp("2024-01-02")],
            "symbol": ["000001.SZ"],
            "score": ["CORRUPT"],
        }
    )

    with pytest.raises(SchemaError, match="non-numeric"):
        select_top_k(scores, top_k=1)


def test_fee_schedule_changes_stamp_duty_on_2023_08_28_and_keeps_components_separate():
    schedule = FeeSchedule.a_share_default(
        commission_bps=3.0,
        minimum_commission_cny=5.0,
        slippage_bps=5.0,
        transfer_fee_bps=0.1,
    )
    old = schedule.calculate("2023-08-27", "sell", 100_000.0)
    new = schedule.calculate("2023-08-28", "sell", 100_000.0)
    buy = schedule.calculate("2023-08-28", "buy", 1_000.0)

    assert old.stamp_duty == pytest.approx(100.0)
    assert new.stamp_duty == pytest.approx(50.0)
    assert old.total - new.total == pytest.approx(50.0)
    assert new.commission == pytest.approx(30.0)
    assert new.transfer_fee == pytest.approx(1.0)
    assert new.slippage == pytest.approx(50.0)
    assert buy.stamp_duty == 0.0
    assert buy.commission == pytest.approx(5.0)


def test_fee_schedule_is_constructed_from_typed_config():
    config = FeesConfig(
        commission_bps=2.0,
        minimum_commission_cny=1.0,
        slippage_bps=4.0,
        transfer_fee_bps=0.2,
        buy_fee_bps=0.1,
        sell_fee_bps=0.3,
        stamp_duty=(
            StampDutyPeriod(date(2023, 1, 1), date(2023, 8, 27), 10.0),
            StampDutyPeriod(date(2023, 8, 28), None, 5.0),
        ),
    )
    schedule = FeeSchedule.from_config(config)

    old = schedule.calculate("2023-08-27", "sell", 100_000.0)
    new = schedule.calculate("2023-08-28", "sell", 100_000.0)
    assert old.stamp_duty == pytest.approx(100.0)
    assert new.stamp_duty == pytest.approx(50.0)
    assert new.side_fee == pytest.approx(3.0)


@pytest.mark.parametrize("invalid", [np.nan, np.inf, -1.0, "bad", True])
def test_fee_schedule_rejects_invalid_trade_notional(invalid):
    with pytest.raises(ConfigurationError, match="notional"):
        FeeSchedule.a_share_default().calculate("2024-01-02", "buy", invalid)


def _fast_inputs():
    dates = pd.bdate_range("2024-01-02", periods=7)
    price_a = [100.0, 100.0, 110.0, 121.0, 121.0, 121.0, 121.0]
    price_b = [100.0, 100.0, 100.0, 100.0, 110.0, 121.0, 133.1]
    bars = pd.DataFrame(
        [
            {"date": trading_date, "symbol": symbol, "adj_open": prices[index]}
            for symbol, prices in (("000001.SZ", price_a), ("600000.SH", price_b))
            for index, trading_date in enumerate(dates)
        ]
    )
    scores = pd.DataFrame(
        [
            {
                "date": trading_date,
                "symbol": symbol,
                "score": (
                    2.0
                    if (index in {0, 1, 4, 5} and symbol == "000001.SZ")
                    or (index in {2, 3, 6} and symbol == "600000.SH")
                    else 1.0
                ),
            }
            for index, trading_date in enumerate(dates[:-1])
            for symbol in ("000001.SZ", "600000.SH")
        ]
    )
    benchmark = pd.DataFrame({"date": dates, "adj_open": 100.0})
    return dates, scores, bars, benchmark


def test_fast_backtest_executes_close_signals_only_at_next_open_and_rebalances_every_n_days():
    dates, scores, bars, benchmark = _fast_inputs()
    result = run_fast_backtest(
        scores,
        bars,
        benchmark,
        config=FastBacktestConfig(
            top_k=1, rebalance_days=2, initial_cash=1_000_000.0
        ),
        fee_schedule=_zero_fee_schedule(),
    )

    assert result.signals["date"].drop_duplicates().tolist() == [dates[0], dates[2], dates[4]]
    assert result.signals["execution_date"].drop_duplicates().tolist() == [
        dates[1],
        dates[3],
        dates[5],
    ]
    assert (result.trades["signal_date"] < result.trades["execution_date"]).all()
    assert result.trades["execution_date"].drop_duplicates().tolist() == [
        dates[1],
        dates[3],
        dates[5],
    ]
    assert result.nav.iloc[0]["date"] == dates[1]
    assert result.nav.iloc[0]["strategy_nav"] == pytest.approx(1.0)
    assert result.nav.loc[result.nav["date"] == dates[3], "strategy_nav"].iloc[0] == pytest.approx(1.21)
    assert result.nav["benchmark_nav"].eq(1.0).all()
    assert result.nav["excess_nav"].equals(result.nav["strategy_nav"])
    assert result.positions.groupby("date")["symbol"].nunique().eq(1).all()
    assert result.nav.loc[result.nav["date"].isin([dates[1], dates[3], dates[5]]), "turnover"].eq(1.0).all()


def test_fast_backtest_carries_last_valid_mark_across_suspension():
    dates = pd.bdate_range("2024-01-02", periods=4)
    scores = pd.DataFrame(
        {
            "date": dates,
            "symbol": "000001.SZ",
            "score": 1.0,
        }
    )
    bars = pd.DataFrame(
        {
            "date": dates,
            "symbol": "000001.SZ",
            "adj_open": [95.0, 100.0, np.nan, 110.0],
        }
    )
    benchmark = pd.DataFrame({"date": dates, "adj_open": 100.0})

    result = run_fast_backtest(
        scores,
        bars,
        benchmark,
        config=FastBacktestConfig(
            top_k=1,
            rebalance_days=10,
            initial_cash=100.0,
        ),
        fee_schedule=_zero_fee_schedule(),
    )

    assert result.trades.iloc[0]["execution_price"] == pytest.approx(100.0)
    assert result.nav["strategy_return"].tolist() == pytest.approx([0.0, 0.0, 0.10])
    assert result.nav["strategy_nav"].tolist() == pytest.approx([1.0, 1.0, 1.10])
    assert result.positions["mark_price"].tolist() == pytest.approx([100.0, 100.0, 110.0])
    assert result.positions["market_value"].tolist() == pytest.approx([100.0, 100.0, 110.0])


def test_fast_backtest_rejects_corrupt_price_instead_of_treating_it_as_suspension():
    dates, scores, bars, benchmark = _fast_inputs()
    broken = bars.astype({"adj_open": object}).copy()
    broken.loc[
        (broken["date"] == dates[2]) & (broken["symbol"] == "000001.SZ"),
        "adj_open",
    ] = "CORRUPT"

    with pytest.raises(SchemaError, match="non-numeric"):
        run_fast_backtest(
            scores,
            broken,
            benchmark,
            config=FastBacktestConfig(top_k=1, rebalance_days=10),
            fee_schedule=_zero_fee_schedule(),
        )


def test_fast_backtest_rejects_signal_without_a_following_trading_date():
    dates = pd.bdate_range("2024-01-02", periods=2)
    scores = pd.DataFrame(
        {
            "date": dates,
            "symbol": "000001.SZ",
            "score": 1.0,
        }
    )
    bars = pd.DataFrame(
        {
            "date": dates,
            "symbol": "000001.SZ",
            "adj_open": 10.0,
        }
    )
    benchmark = pd.DataFrame({"date": dates, "adj_open": 100.0})

    with pytest.raises(SchemaError, match="no following benchmark trading date"):
        run_fast_backtest(
            scores,
            bars,
            benchmark,
            config=FastBacktestConfig(top_k=1, rebalance_days=1),
            fee_schedule=_zero_fee_schedule(),
        )


def test_fast_backtest_deducts_fees_and_refuses_missing_execution_open():
    dates, scores, bars, benchmark = _fast_inputs()
    result = run_fast_backtest(
        scores,
        bars,
        benchmark,
        config=FastBacktestConfig(top_k=1, rebalance_days=2, initial_cash=1_000_000.0),
        fee_schedule=FeeSchedule.a_share_default(),
    )
    first = result.nav.iloc[0]
    assert first["transaction_cost"] > 0
    assert first["strategy_nav"] < 1.0
    assert result.trades["transaction_cost"].sum() == pytest.approx(
        result.nav["transaction_cost"].sum()
    )

    broken = bars.copy()
    broken.loc[
        (broken["date"] == dates[1]) & (broken["symbol"] == "000001.SZ"),
        "adj_open",
    ] = np.nan
    with pytest.raises(SchemaError, match="execution open is missing"):
        run_fast_backtest(
            scores,
            broken,
            benchmark,
            config=FastBacktestConfig(top_k=1, rebalance_days=2),
            fee_schedule=_zero_fee_schedule(),
        )


def test_portfolio_metrics_cover_returns_risk_benchmark_costs_and_holdings():
    dates = pd.bdate_range("2024-01-02", periods=4)
    strategy_returns = pd.Series([0.0, 0.10, -0.05, 0.02])
    benchmark_returns = pd.Series([0.0, 0.05, -0.02, 0.01])
    nav = pd.DataFrame(
        {
            "date": dates,
            "strategy_return": strategy_returns,
            "benchmark_return": benchmark_returns,
            "strategy_nav": (1.0 + strategy_returns).cumprod(),
            "benchmark_nav": (1.0 + benchmark_returns).cumprod(),
            "turnover": [1.0, 0.0, 0.5, 0.0],
            "transaction_cost": [1.0, 0.0, 2.0, 0.0],
            "holdings": [2, 2, 3, 3],
        }
    )
    trades = pd.DataFrame({"trade_notional": [100.0, 50.0, 0.0]})
    metrics = calculate_portfolio_metrics(nav, trades=trades, periods_per_year=252)

    expected_strategy = float((1.0 + strategy_returns).prod() - 1.0)
    expected_benchmark = float((1.0 + benchmark_returns).prod() - 1.0)
    assert metrics["cumulative_return"] == pytest.approx(expected_strategy)
    assert metrics["benchmark_return"] == pytest.approx(expected_benchmark)
    assert metrics["excess_return"] == pytest.approx(expected_strategy - expected_benchmark)
    assert metrics["maximum_drawdown"] == pytest.approx(-0.05)
    assert metrics["portfolio_turnover"] == pytest.approx(1.5)
    assert metrics["total_transaction_cost"] == pytest.approx(3.0)
    assert metrics["number_of_trades"] == 2
    assert metrics["average_holdings"] == pytest.approx(2.5)
    assert metrics["win_rate"] == pytest.approx(0.5)
    for name in (
        "annualized_return",
        "annualized_volatility",
        "sharpe_ratio",
        "sortino_ratio",
        "calmar_ratio",
        "tracking_error",
        "information_ratio",
    ):
        assert np.isfinite(metrics[name])


def test_maximum_drawdown_includes_loss_from_initial_entry_cost():
    dates = pd.bdate_range("2024-01-02", periods=2)
    nav = pd.DataFrame(
        {
            "date": dates,
            "strategy_return": [-0.10, 0.0],
            "benchmark_return": [0.0, 0.0],
            "strategy_nav": [0.90, 0.90],
            "benchmark_nav": [1.0, 1.0],
        }
    )

    metrics = calculate_portfolio_metrics(nav)

    assert metrics["cumulative_return"] == pytest.approx(-0.10)
    assert metrics["maximum_drawdown"] == pytest.approx(-0.10)
    assert np.isfinite(metrics["calmar_ratio"])
