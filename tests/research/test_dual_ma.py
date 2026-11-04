"""Hand-calculated crossover, execution ledger, causality and admission tests."""
from dataclasses import replace
import hashlib
import json

import numpy as np
import pandas as pd
import pytest
from pandas.testing import assert_frame_equal

from factorlab.backtest.execution import PositionBook
from factorlab.backtest.fees import FeeSchedule
from factorlab.backtest.target import run_target_positions
from factorlab.errors import ConfigurationError, SchemaError
from factorlab.strategies.dual_ma import DualMAConfig, moving_average_signals, run_software_backtest
from factorlab.strategies.dual_ma_cli import main, metrics, round_trips


def case(close=None, **parameters):
    close = [10, 10, 10, 11, 12, 12, 10, 9, 9] if close is None else close
    dates = pd.bdate_range("2018-01-01", periods=len(close))
    config = DualMAConfig(start=str(dates[3].date()), end=str(dates[-1].date()),
                          short_window=2, long_window=3, initial_cash=10000)
    config = replace(config, **parameters)
    values = np.array(close, dtype=float)
    bars = pd.DataFrame(dict(date=dates, symbol=config.symbol, adj_close=values,
                             raw_open=10., raw_close=values, raw_high=np.maximum(10, values),
                             raw_low=np.minimum(10, values), volume=100000,
                             trade_status=True, is_st=False))
    return bars, dates, config


def zero_fees():
    return FeeSchedule.a_share_default(0, 0, 0)


def test_hand_calculated_golden_ledger_and_fees():
    bars, dates, config = case()
    result = run_software_backtest(bars, dates, config)
    signals, leg = result["signals"], result["strategy"]
    assert signals.signal.tolist() == ["buy", "hold", "hold", "sell", "hold", "hold"]
    assert signals.ma_short.iloc[0] == 10.5
    assert signals.ma_long.iloc[0] == pytest.approx(31 / 3)
    trades = leg["trades"]
    assert trades.side.tolist() == ["buy", "sell"]
    assert trades.signal_date.tolist() == [dates[3], dates[6]]
    assert trades.execution_date.tolist() == [dates[4], dates[7]]
    assert trades.executed_shares.tolist() == [900, 900]
    assert trades.commission.tolist() == [5, 5]
    assert trades.stamp_duty.tolist() == [0, 9]
    assert trades.slippage.tolist() == [4.5, 4.5]
    # 10,000 - 9,000 - 9.5 + 9,000 - 18.5 = 9,972.
    np.testing.assert_allclose(leg["nav"].cash, [10000, 990.5, 990.5, 990.5, 9972, 9972])
    assert leg["nav"].shares.tolist() == [0, 900, 900, 900, 0, 0]
    np.testing.assert_allclose(leg["nav"].portfolio_value,
                               [10000, 11790.5, 11790.5, 9990.5, 9972, 9972])
    assert leg["nav"].sellable_shares.tolist() == [0, 0, 900, 900, 0, 0]
    stats = metrics(leg)
    assert stats["total_fees"] == 19
    assert stats["total_slippage"] == 9
    assert stats["cumulative_return"] == pytest.approx(-0.0028)
    assert stats["annualized_return"] == pytest.approx(0.9972 ** (252 / 6) - 1)
    assert stats["trade_count"] == 2
    assert stats["trade_win_rate"] == 0
    assert round_trips(trades).net_pnl.tolist() == [-28]


def test_equality_previous_inclusive_current_strict():
    bars, dates, config = case([10, 10, 10, 11, 11, 10, 10, 11], short_window=1, long_window=2)
    assert moving_average_signals(bars, dates, config).signal.tolist() == ["buy", "hold", "sell", "hold", "buy"]


@pytest.mark.parametrize("values", [list(range(10, 20)), list(range(20, 10, -1)), [10] * 10])
def test_monotone_and_flat_start_do_not_manufacture_cross(values):
    bars, dates, config = case(values)
    result = run_software_backtest(bars, dates, config)
    assert result["strategy"]["trades"].empty
    assert result["strategy"]["nav"].cash.eq(10000).all()
    assert result["signals"].signal.eq("hold").all()


def test_oscillation_alternates_orders_and_preserves_cash():
    bars, dates, config = case([10, 10, 10, 11, 9, 11, 9, 11, 9, 11], short_window=1, long_window=2)
    leg = run_software_backtest(bars, dates, config)["strategy"]
    assert leg["trades"].side.tolist() == ["buy", "sell", "buy", "sell", "buy", "sell"]
    assert (leg["nav"].cash >= 0).all()
    assert leg["nav"].shares.ge(0).all()


def test_terminal_position_marked_not_liquidated_and_final_signal_unexecuted():
    bars, dates, config = case([10, 10, 10, 11, 12, 12, 10])
    result = run_software_backtest(bars, dates, config)
    assert result["signals"].signal.iloc[-1] == "sell"
    assert result["strategy"]["trades"].side.tolist() == ["buy"]
    last = result["strategy"]["nav"].iloc[-1]
    assert last.shares == 900
    assert last.portfolio_value == last.cash + 900 * 10
    assert metrics(result["strategy"])["trade_win_rate"] is None


def test_adjusted_signal_raw_execution_and_next_open():
    bars, dates, config = case()
    bars.adj_close *= 2
    bars.loc[4, "raw_open"] = 10.5
    leg = run_software_backtest(bars, dates, config)["strategy"]
    first = leg["trades"].iloc[0]
    assert first.execution_date == dates[4]
    assert first.signal_date == dates[3]
    assert first.execution_price == 10.5
    assert first.execution_price != bars.adj_close.iloc[4]


def test_warmup_not_carried_and_first_day_genuine_cross_allowed():
    bars, dates, config = case([9, 10, 11, 12, 13, 14])
    assert moving_average_signals(bars, dates, config).signal.iloc[0] == "hold"
    assert run_software_backtest(bars, dates, config)["strategy"]["trades"].empty
    bars, dates, config = case()
    assert moving_average_signals(bars, dates, config).signal.iloc[0] == "buy"


@pytest.mark.parametrize("mutator", [
    lambda b: b.drop(index=1),
    lambda b: b.assign(adj_close=np.nan),
    lambda b: b.assign(raw_open=0),
    lambda b: b.assign(raw_close=np.inf),
    lambda b: b.assign(volume=-1),
    lambda b: b.assign(raw_high=1),
    lambda b: b.assign(trade_status=None),
    lambda b: b.assign(is_st="unknown"),
    lambda b: b.assign(raw_close=True),
    lambda b: b.assign(symbol="600000.SH"),
    lambda b: b.assign(date=pd.NaT),
    lambda b: b.iloc[::-1],
])
def test_missing_invalid_and_mismatched_inputs_fail(mutator):
    bars, dates, config = case()
    with pytest.raises(SchemaError):
        run_software_backtest(mutator(bars), dates, config)


def test_warmup_insufficient_and_calendar_duplicate_fail():
    bars, dates, config = case()
    with pytest.raises(SchemaError, match="warmup"):
        moving_average_signals(bars, dates, replace(config, start=str(dates[2].date())))
    with pytest.raises(SchemaError, match="two sessions"):
        moving_average_signals(bars, dates, replace(config, start=str(dates[-1].date())))
    with pytest.raises(SchemaError, match="unique"):
        moving_average_signals(bars, dates.insert(0, dates[0]), config)


@pytest.mark.parametrize("kwargs", [dict(short_window=0), dict(long_window=True), dict(short_window=20),
                                   dict(initial_cash=np.nan), dict(max_weight=1.1), dict(board_lot=1.5),
                                   dict(allow_short=True), dict(signal_frequency="hourly"),
                                   dict(execution_timing="same_open"), dict(start="bad")])
def test_invalid_configuration_rejected(kwargs):
    with pytest.raises((ConfigurationError, SchemaError)):
        DualMAConfig(**kwargs)


@pytest.mark.parametrize("field,value,reason", [("trade_status", False, "suspended"),
                                               ("volume", 0, "suspended"),
                                               ("raw_open", 12.1, "buy_price_limit")])
def test_failed_entry_retried_at_later_open(field, value, reason):
    bars, dates, config = case()
    bars.loc[4, field] = value
    if field == "raw_open":
        bars.loc[4, "raw_high"] = 12.1
    leg = run_software_backtest(bars, dates, config)["strategy"]
    assert leg["rejections"].reason.iloc[0] == reason
    assert leg["trades"].execution_date.iloc[0] == dates[5]
    assert leg["trades"].signal_date.iloc[0] == dates[3]


def test_sell_limit_and_suspension_keep_position_until_valid_open():
    bars, dates, config = case([10, 10, 10, 11, 12, 12, 10, 9, 9, 9])
    bars.loc[7, ["raw_open", "raw_low"]] = 9.0  # day 6 close=10 => limit down
    bars.loc[8, "trade_status"] = False
    leg = run_software_backtest(bars, dates, config)["strategy"]
    assert leg["rejections"].reason.tolist()[-2:] == ["sell_price_limit", "suspended"]
    assert leg["trades"].execution_date.iloc[-1] == dates[9]
    assert leg["nav"].shares.iloc[8 - 3] == 900


def test_opposite_target_cancels_failed_entry_before_next_open():
    bars, dates, config = case([10, 10, 10, 11, 9, 9, 9], short_window=1, long_window=2)
    bars.loc[4, "trade_status"] = False
    leg = run_software_backtest(bars, dates, config)["strategy"]
    assert leg["rejections"].reason.tolist() == ["suspended"]
    assert leg["trades"].empty


@pytest.mark.parametrize("cash,weight,shares", [(100, 1., 0), (10000, .5, 400), (10000, .001, 0), (10000, 1., 900)])
def test_lots_cash_and_maximum_entry_budget(cash, weight, shares):
    bars, dates, config = case(initial_cash=cash, max_weight=weight)
    leg = run_software_backtest(bars, dates, config)["strategy"]
    assert leg["nav"].shares.iloc[1] == shares
    if shares:
        trade = leg["trades"].iloc[0]
        assert trade.trade_notional + trade.transaction_cost <= cash * weight
        assert trade.executed_shares % config.board_lot == 0
    else:
        assert leg["rejections"].reason.eq("cash_constraint").all()


def test_t_plus_one_position_book_refuses_same_day_sale():
    book = PositionBook()
    book.buy("601857.SH", "2018-01-02", 100)
    assert book.sellable_shares("601857.SH", "2018-01-02") == 0
    with pytest.raises(ConfigurationError, match="non-sellable"):
        book.sell("601857.SH", "2018-01-02", 100)
    book.sell("601857.SH", "2018-01-03", 100)
    assert book.total_shares("601857.SH") == 0


def test_historical_stamp_duty_and_custom_slippage():
    fees = FeeSchedule.a_share_default(0, 0, 7, .2)
    old, new = fees.calculate("2018-06-01", "sell", 10000), fees.calculate("2024-06-01", "sell", 10000)
    assert old.stamp_duty == 10
    assert new.stamp_duty == 5
    assert old.slippage == 7
    assert old.transfer_fee == pytest.approx(.2)
    assert fees.calculate("2018-06-01", "buy", 10000).stamp_duty == 0


def test_ledger_reconciliation_and_benchmark_common_interval():
    bars, dates, config = case()
    result = run_software_backtest(bars, dates, config)
    for name in ("strategy", "benchmark"):
        nav, trades = result[name]["nav"], result[name]["trades"]
        np.testing.assert_allclose(nav.portfolio_value, nav.cash + nav.shares * nav.mark_price)
        np.testing.assert_allclose(nav.strategy_nav, nav.portfolio_value / config.initial_cash)
        cash, shares = config.initial_cash, 0
        for day in nav.date:
            for trade in trades.loc[trades.execution_date.eq(day)].itertuples():
                direction = 1 if trade.side == "buy" else -1
                shares += direction * trade.executed_shares
                cash -= direction * trade.trade_notional + trade.transaction_cost
            assert nav.loc[nav.date.eq(day), "shares"].iloc[0] == shares
            assert nav.loc[nav.date.eq(day), "cash"].iloc[0] == pytest.approx(cash)
        assert nav.date.tolist() == dates[3:].tolist()
    assert result["benchmark"]["trades"].execution_date.tolist() == [dates[3]]
    assert result["benchmark"]["trades"].signal_date.isna().all()


def test_future_perturbation_and_determinism():
    bars, dates, config = case()
    first = run_software_backtest(bars, dates, config)
    second = run_software_backtest(bars.copy(), dates.copy(), config)
    for name in ("strategy", "benchmark"):
        for key in first[name]:
            assert_frame_equal(first[name][key], second[name][key])
    changed = bars.copy()
    changed.loc[7:, ["adj_close", "raw_close", "raw_high"]] = 15.
    future = run_software_backtest(changed, dates, config)
    assert_frame_equal(first["signals"].iloc[:4], future["signals"].iloc[:4])
    assert_frame_equal(first["strategy"]["nav"].iloc[:4], future["strategy"]["nav"].iloc[:4])


def test_actions_and_changing_adjustment_ratio_refused():
    bars, dates, config = case()
    with pytest.raises(SchemaError, match="corporate-action"):
        run_software_backtest(bars, dates, config, corporate_actions=[{"cash_dividend": .1}])
    bars.loc[4:, "adj_close"] *= 2
    with pytest.raises(SchemaError, match="execution ledger"):
        run_software_backtest(bars, dates, config)


def test_target_adapter_validates_weights_and_calendar():
    bars, dates, config = case()
    signals = moving_average_signals(bars, dates, config)
    for value in (-1, 2, np.inf, "bad"):
        invalid = signals.copy()
        invalid["target_weight"] = value
        with pytest.raises(SchemaError, match="weight"):
            run_target_positions(invalid, bars, dates[3:])


def test_cli_complete_artifacts_hashes_and_real_refusal(tmp_path, capsys):
    output = tmp_path / "synthetic"
    assert main(["--synthetic", "--start", "2018-01-01", "--end", "2018-03-31", "--output", str(output)]) == 0
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["synthetic"] is True
    assert manifest["formal_admission"] == "FORMAL_ADMISSION_BLOCKED"
    for name, digest in manifest["artifacts_sha256"].items():
        assert hashlib.sha256((output / name).read_bytes()).hexdigest() == digest
    assert (output / "nav.png").stat().st_size > 1000
    assert (output / "strategy_nav.csv").is_file()
    assert main(["--bars", str(tmp_path / "real.csv"), "--output", str(tmp_path / "real")]) == 2
    assert "FORMAL_ADMISSION_BLOCKED" in capsys.readouterr().err
    assert not (tmp_path / "real").exists()
    with pytest.raises(SystemExit):
        main(["--synthetic", "--bars", "real.csv", "--output", str(output)])


def test_cli_configuration_and_undefined_metrics(tmp_path):
    config = tmp_path / "config.json"
    config.write_text(json.dumps(dict(short_window=2, long_window=3, max_weight=.5)), encoding="utf-8")
    out = tmp_path / "run"
    assert main(["--synthetic", "--config", str(config), "--short-window", "1", "--start", "2018-01-01", "--end", "2018-01-31", "--output", str(out)]) == 0
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["config"]["short_window"] == 1
    assert manifest["config"]["long_window"] == 3
    assert manifest["fees"][-1]["effective_to"] is None
    assert manifest["package_root"]
    config.write_text("[]", encoding="utf-8")
    assert main(["--synthetic", "--config", str(config), "--output", str(tmp_path / "invalid")]) == 2
    bars, dates, cfg = case([10] * 10)
    stats = metrics(run_software_backtest(bars, dates, cfg)["strategy"])
    assert np.isnan(stats["sharpe_ratio"])
    assert stats["trade_win_rate"] is None
