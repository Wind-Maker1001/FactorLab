"""Hand arithmetic, causal intents and shared execution integration."""
from dataclasses import replace
import json

import numpy as np
import pandas as pd
from pandas.testing import assert_frame_equal
import pytest

from factorlab.backtest.fees import FeeSchedule
from factorlab.errors import ConfigurationError, SchemaError
from factorlab.strategies.technical import (
    BollingerConfig, RSIConfig, MACDConfig, CTAConfig, seeded_ema,
    wilder_rsi, wilder_average, technical_signals,
)
from factorlab.strategies.technical_research import run_technical_backtest
from factorlab.strategies.technical_cli import main
from factorlab.experiments.tracker import ExperimentTracker


def fixture(config, prices, warmup):
    dates = pd.bdate_range("2018-01-01", periods=len(prices))
    config = replace(config, start=str(dates[warmup].date()), end=str(dates[-1].date()), initial_cash=10000)
    close = np.array(prices, dtype=float)
    bars = pd.DataFrame(dict(date=dates, symbol=config.symbol, adj_close=close, raw_close=close,
        raw_open=10., raw_high=np.maximum(10., close), raw_low=np.minimum(10., close),
        volume=1000000, trade_status=True, is_st=False))
    return bars, dates, config


def test_wilder_hand_arithmetic_and_gap_reset():
    rsi = wilder_rsi([10, 11, 10, 12, 11], 2)
    assert rsi.iloc[:2].isna().all()
    np.testing.assert_allclose(rsi.iloc[2:], [50, 100 * 1.25 / 1.5, 50])
    for prices, expected in (([1, 2, 3, 4], 100), ([4, 3, 2, 1], 0), ([3] * 4, 50)):
        assert wilder_rsi(prices, 2).iloc[-1] == expected
    rsi = wilder_rsi([1, 2, 3, np.nan, 5, 6, 7], 2)
    assert rsi.iloc[3:6].isna().all()
    assert rsi.iloc[6] == 100
    np.testing.assert_allclose(wilder_average([2, 4, 8], 2).iloc[1:], [3, 5.5])


def test_ema_sma_seed_and_macd_hand_golden():
    np.testing.assert_allclose(seeded_ema([1, 2, 3, 4], 2).iloc[1:], [1.5, 2.5, 3.5])
    bars, dates, c = fixture(MACDConfig(fast=2, slow=3, signal_window=2), [10, 10, 10, 10, 12, 10, 8, 12], 4)
    s = technical_signals(bars, dates, c)
    # At first close EMA2=34/3, EMA3=11; DIF=1/3, DEA=2/9.
    assert s.ema_fast.iloc[0] == pytest.approx(34 / 3)
    assert s.ema_slow.iloc[0] == 11
    assert s.dif.iloc[0] == pytest.approx(1 / 3)
    assert s.dea.iloc[0] == pytest.approx(2 / 9)
    assert s.histogram.iloc[0] == pytest.approx(2 / 9)
    assert s.signal.iloc[:2].tolist() == ["buy", "sell"]


def test_bollinger_modes_and_population_std():
    bars, dates, c = fixture(BollingerConfig(window=3, deviations=1), [10, 10, 10, 8, 8.5, 10, 12, 9], 3)
    s = technical_signals(bars, dates, c)
    assert s.middle.iloc[0] == pytest.approx(28 / 3)
    assert s.lower.iloc[0] == pytest.approx(28 / 3 - np.sqrt(8 / 9))
    assert s.lower_breakout.iloc[0]
    assert s.lower_reentry.iloc[1]
    assert s.signal.iloc[1] == "buy"
    breakout = technical_signals(bars, dates, replace(c, mode="breakout"))
    assert breakout.signal.iloc[2] == "buy"
    assert breakout.signal.iloc[-1] == "sell"


def test_rsi_threshold_cross_and_exits():
    bars, dates, c = fixture(RSIConfig(window=2), [10, 9, 8, 9, 10, 9, 8], 3)
    s = technical_signals(bars, dates, c)
    np.testing.assert_allclose(s.rsi.iloc[:3], [50, 75, 37.5])
    assert s.signal.iloc[:3].tolist() == ["buy", "hold", "sell"]
    midline = technical_signals(bars, dates, replace(c, exit_rule="midline"))
    assert midline.signal.iloc[2] == "sell"


def test_cta_prior_channel_atr_risk_budget_and_next_open_stop():
    bars, dates, c = fixture(CTAConfig(entry_window=3, exit_window=3, atr_window=2,
        trend_window=3, atr_multiple=1, risk_fraction=0.01), [10, 10, 10, 12, 13, 11, 10, 10], 3)
    # Explicit OHLC in signal units: 2-wide ranges, closing gap on entry.
    bars["raw_open"] = bars.raw_close
    bars["raw_high"] = bars.raw_close + 1
    bars["raw_low"] = bars.raw_close - 1
    s = technical_signals(bars, dates, c)
    assert s.channel_upper.iloc[0] == 11  # not today's 13
    assert s.atr.iloc[0] == 2.5  # seed TR=2,2; next TR=3
    assert s.signal.iloc[0] == "buy"
    assert s.target_weight.iloc[0] == pytest.approx(.01 * 12 / 2.5)
    assert s.stop_level.iloc[0] == 9.5
    assert s.stop_level.iloc[1] == 10.75
    # Breach the old stop without a trend/channel exit; sell only next open.
    c = replace(c, trend_window=5, atr_multiple=0.5, risk_fraction=0.05)
    bars, dates, c = fixture(c, [10, 10, 10, 10, 10, 12, 13, 11.5, 11, 11], 5)
    bars["raw_open"], bars["raw_high"], bars["raw_low"] = bars.raw_close, bars.raw_close + 1, bars.raw_close - 1
    result = run_technical_backtest(bars, dates, c)
    trades = result["strategy"]["trades"]
    assert result["signals"].stop_level.iloc[1] == 11.875
    assert result["signals"].exit_reason.iloc[2] == "atr_stop"
    assert result["signals"].adj_close.iloc[2] > result["signals"].trend.iloc[2]
    assert trades.side.tolist() == ["buy", "sell"]
    assert trades.execution_date.tolist() == [dates[6], dates[8]]
    assert trades.execution_price.tolist() == [13, 11]
    assert result["strategy"]["nav"].shares.iloc[2] > 0
    assert result["signals"].target_weight.dropna().between(0, c.max_weight).all()


CONFIGS = [BollingerConfig(window=3, deviations=1), RSIConfig(window=2),
           MACDConfig(fast=2, slow=3, signal_window=2),
           CTAConfig(entry_window=3, exit_window=2, atr_window=2, trend_window=3)]


@pytest.mark.parametrize("c", CONFIGS)
def test_prefix_causality_determinism_and_reconciliation(c):
    bars, dates, c = fixture(c, [10] * 6 + [8, 9, 12, 11, 9, 8, 12, 10, 11, 9], 6)
    first = run_technical_backtest(bars, dates, c)
    second = run_technical_backtest(bars, dates, c)
    for name in ("strategy", "benchmark"):
        for key in ("nav", "trades", "rejections"):
            assert_frame_equal(first[name][key], second[name][key])
        nav = first[name]["nav"]
        np.testing.assert_allclose(nav.portfolio_value, nav.cash + nav.shares * nav.mark_price)
        assert nav.cash.ge(0).all()
        assert nav.shares.mod(c.board_lot).eq(0).all()
        assert first["evaluations"][name].metrics["total_transaction_cost"].status == "OK"
    changed = bars.copy()
    changed.loc[12:, ["adj_close", "raw_close", "raw_high", "raw_open", "raw_low"]] *= 1.7
    altered = run_technical_backtest(changed, dates, c)
    assert_frame_equal(first["signals"].iloc[:6], altered["signals"].iloc[:6])
    assert_frame_equal(first["strategy"]["nav"].iloc[:6], altered["strategy"]["nav"].iloc[:6])
    prefix = technical_signals(bars.iloc[:12], dates[:12], replace(c, end=str(dates[11].date())))
    assert_frame_equal(first["signals"].iloc[:6], prefix)


@pytest.mark.parametrize("c", CONFIGS)
def test_warmup_missing_and_adjustment_contracts(c):
    bars, dates, c = fixture(c, [10] * 12, 6)
    with pytest.raises(SchemaError, match="warmup"):
        technical_signals(bars, dates, replace(c, start=str(dates[1].date())))
    missing = bars.copy()
    missing.loc[7, "adj_close"] = np.nan
    with pytest.raises(SchemaError):
        technical_signals(missing, dates, c)
    bars.loc[9:, "adj_close"] *= 2
    with pytest.raises(SchemaError, match="adjustment"):
        run_technical_backtest(bars, dates, c)
    with pytest.raises(SchemaError, match="corporate"):
        run_technical_backtest(bars, dates, c, corporate_actions=[1])


@pytest.mark.parametrize("cls,kwargs", [(BollingerConfig, {"window": True}), (BollingerConfig, {"deviations": 0}),
    (BollingerConfig, {"mode": "unknown"}), (RSIConfig, {"window": 1}), (RSIConfig, {"oversold": 70}),
    (RSIConfig, {"exit_rule": "unknown"}), (MACDConfig, {"fast": 26}),
    (MACDConfig, {"zero_filter": 1}), (CTAConfig, {"risk_fraction": 2}), (CTAConfig, {"atr_multiple": 0}),
    (CTAConfig, {"allow_short": True}), (CTAConfig, {"execution_timing": "same_close"})])
def test_bad_parameters(cls, kwargs):
    with pytest.raises(ConfigurationError):
        cls(**kwargs)


@pytest.mark.parametrize("event", ["suspended", "limit_up", "cash", "limit_down"])
def test_actual_strategy_intents_share_a_share_rejections(event):
    bars, dates, c = fixture(RSIConfig(window=2), [10, 9, 8, 9, 10, 9, 8, 9], 3)
    if event == "suspended":
        bars.loc[4, "trade_status"] = False
    elif event == "limit_up":
        bars.loc[4, "raw_open"] = 11
        bars.loc[4, "raw_high"] = 11
    elif event == "cash":
        c = replace(c, initial_cash=1)
    else:
        bars.loc[6, "raw_open"] = 8
        bars.loc[6, "raw_low"] = 8
    leg = run_technical_backtest(bars, dates, c)["strategy"]
    assert not leg["rejections"].empty
    assert leg["nav"].cash.ge(0).all()
    assert (leg["trades"].execution_date > leg["trades"].signal_date).all()


@pytest.mark.parametrize("c", CONFIGS)
@pytest.mark.parametrize("prices", [[10] * 20, list(range(10, 30)), list(range(30, 10, -1))])
def test_flat_and_one_way_markets(c, prices):
    bars, dates, c = fixture(c, prices, 6)
    result = run_technical_backtest(bars, dates, c)
    assert result["strategy"]["nav"].portfolio_value.gt(0).all()
    if isinstance(c, CTAConfig) and prices == list(range(10, 30)):
        assert result["signals"].signal.eq("buy").sum() == 1
    if not isinstance(c, CTAConfig):
        assert not result["signals"].signal.eq("buy").any()


def test_macd_zero_filter_blocks_negative_axis_cross():
    bars, dates, c = fixture(MACDConfig(fast=2, slow=3, signal_window=2),
        [20, 19, 18, 17, 16, 15, 14, 14, 14, 14, 16, 18], 6)
    unfiltered = technical_signals(bars, dates, c)
    filtered = technical_signals(bars, dates, replace(c, zero_filter=True))
    assert unfiltered.signal.eq("buy").any()
    assert (unfiltered.loc[unfiltered.signal.eq("buy"), "dif"] < 0).all()
    assert not filtered.signal.eq("buy").any()


def test_gap_stop_rejection_keeps_position_until_executable_open():
    c = CTAConfig(entry_window=3, exit_window=3, atr_window=2, trend_window=5,
                  atr_multiple=.5, risk_fraction=.05)
    bars, dates, c = fixture(c, [10, 10, 10, 10, 10, 12, 13, 11.5, 9, 9], 5)
    bars["raw_open"], bars["raw_high"], bars["raw_low"] = bars.raw_close, bars.raw_close + 1, bars.raw_close - 1
    leg = run_technical_backtest(bars, dates, c)["strategy"]
    assert leg["trades"].side.tolist() == ["buy", "sell"]
    assert leg["trades"].execution_date.iloc[-1] == dates[9]
    assert leg["nav"].shares.iloc[3] > 0
    assert leg["rejections"].execution_date.eq(dates[8]).any()


@pytest.mark.parametrize("name", ["bollinger", "rsi", "macd", "cta"])
def test_cli_synthetic_tracking_and_admission(name, tmp_path):
    output = tmp_path / name
    assert main([name, "--synthetic", "--output", str(output)]) == 0
    report = json.loads((output / "evaluation.json").read_text(encoding="utf-8"))
    assert report["synthetic"] is True
    assert "FORMAL_ADMISSION_BLOCKED" in report["safety"]
    tracker = ExperimentTracker(output / "tracker")
    run = tracker.get_run("synthetic")["payload"]["run"]
    assert run["qualification"] == "SYNTHETIC"
    assert run["status"] == "SUCCEEDED"
    assert main([name, "--output", str(tmp_path / "real")]) == 2
    assert not (tmp_path / "real").exists()
