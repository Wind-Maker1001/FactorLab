"""Execution-aligned forward-label tests."""

import numpy as np
import pandas as pd
import pytest

from factorlab.labels.forward_return import build_forward_excess_return_labels


def _label_inputs(periods=10):
    dates = pd.bdate_range("2024-01-02", periods=periods)
    stock = pd.DataFrame(
        {
            "date": dates,
            "symbol": "600000.SH",
            "adj_open": [10.0 * (1.02 ** value) for value in range(periods)],
        }
    )
    benchmark = pd.DataFrame(
        {
            "date": dates,
            "adj_open": [100.0 * (1.01 ** value) for value in range(periods)],
        }
    )
    return dates, stock, benchmark


def test_default_label_uses_t_plus_1_entry_and_t_plus_6_exit():
    dates, stock, benchmark = _label_inputs()
    result = build_forward_excess_return_labels(stock, benchmark, holding_days=5)
    row = result.loc[result["date"] == dates[0]].iloc[0]
    assert row["entry_date"] == dates[1]
    assert row["exit_date"] == dates[6]
    assert row["STOCK_FWD_RET5"] == pytest.approx(stock.loc[6, "adj_open"] / stock.loc[1, "adj_open"] - 1)
    assert row["BENCHMARK_FWD_RET5"] == pytest.approx(
        benchmark.loc[6, "adj_open"] / benchmark.loc[1, "adj_open"] - 1
    )
    assert row["EXCESS_RET5"] == pytest.approx(
        (1.02 ** 5 - 1.0) - (1.01 ** 5 - 1.0)
    )


def test_signal_day_close_or_t_plus_5_open_are_not_used():
    dates, stock, benchmark = _label_inputs()
    baseline = build_forward_excess_return_labels(stock, benchmark, holding_days=5)
    mutated = stock.copy()
    mutated.loc[0, "adj_open"] = 99999.0  # signal-day open is irrelevant
    mutated.loc[5, "adj_open"] = 88888.0  # t+5 is not the exit
    unchanged = build_forward_excess_return_labels(mutated, benchmark, holding_days=5)
    assert unchanged.loc[0, "EXCESS_RET5"] == pytest.approx(baseline.loc[0, "EXCESS_RET5"])

    mutated.loc[6, "adj_open"] *= 2.0
    changed = build_forward_excess_return_labels(mutated, benchmark, holding_days=5)
    assert changed.loc[0, "STOCK_FWD_RET5"] != baseline.loc[0, "STOCK_FWD_RET5"]


@pytest.mark.parametrize("missing_position", [1, 6])
def test_missing_entry_or_exit_open_produces_missing_label(missing_position):
    _, stock, benchmark = _label_inputs()
    stock.loc[missing_position, "adj_open"] = np.nan
    result = build_forward_excess_return_labels(stock, benchmark, holding_days=5)
    assert pd.isna(result.loc[0, "STOCK_FWD_RET5"])
    assert pd.isna(result.loc[0, "EXCESS_RET5"])


def test_unlabelled_tail_is_preserved_with_missing_values():
    dates, stock, benchmark = _label_inputs()
    result = build_forward_excess_return_labels(stock, benchmark, holding_days=5)
    assert result["date"].tolist() == list(dates)
    assert result.tail(6)["EXCESS_RET5"].isna().all()
