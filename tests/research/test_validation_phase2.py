"""Tests for deterministic, leakage-safe expanding walk-forward splits."""

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from factorlab.config import load_config
from factorlab.validation import ExpandingWalkForwardSplitter, WalkForwardError


def _cross_section(start="2020-01-02", end="2022-12-30", symbols=2):
    dates = pd.bdate_range(start, end)
    rows = []
    entry = pd.Series(dates, index=dates).shift(-1)
    exit_ = pd.Series(dates, index=dates).shift(-6)
    for date in dates:
        for number in range(symbols):
            rows.append(
                {
                    "date": date,
                    "symbol": "%06d.SZ" % (number + 1),
                    "entry_date": entry.loc[date],
                    "exit_date": exit_.loc[date],
                    "feature": float(number),
                }
            )
    return dates, pd.DataFrame(rows)


def _splitter():
    return ExpandingWalkForwardSplitter(
        initial_train_months=6,
        validation_months=2,
        test_months=2,
        step_months=2,
        purge_days=5,
        embargo_days=5,
    )


def test_fold_label_windows_are_strictly_before_later_periods():
    dates, frame = _cross_section()
    folds = list(_splitter().split(frame, calendar=dates))

    assert len(folds) > 2
    for fold in folds:
        train = frame.iloc[fold.train_indices]
        validation = frame.iloc[fold.validation_indices]
        assert train["exit_date"].max() < fold.validation_dates.min()
        assert validation["exit_date"].max() < fold.test_dates.min()
        assert fold.audit is not None
        assert fold.audit.train_max_exit_date < fold.audit.validation_first_date
        assert (
            fold.audit.validation_max_exit_date < fold.audit.test_first_date
        )


def test_multiple_folds_expand_training_and_keep_periods_disjoint():
    dates, frame = _cross_section()
    folds = list(_splitter().split(frame, calendar=dates))

    for previous, current in zip(folds, folds[1:]):
        assert set(previous.train_indices).issubset(set(current.train_indices))
        assert len(current.train_indices) > len(previous.train_indices)
        assert current.train_window[0] == previous.train_window[0]
        assert current.train_window[1] > previous.train_window[1]

    for fold in folds:
        assert fold.train_dates.is_monotonic_increasing
        assert fold.validation_dates.is_monotonic_increasing
        assert fold.test_dates.is_monotonic_increasing
        assert fold.train_dates.max() < fold.validation_dates.min()
        assert fold.validation_dates.max() < fold.test_dates.min()
        assert not set(fold.train_indices) & set(fold.validation_indices)
        assert not set(fold.train_indices) & set(fold.test_indices)
        assert not set(fold.validation_indices) & set(fold.test_indices)
        np.testing.assert_array_equal(
            fold.train_indices, np.sort(fold.train_indices)
        )


def test_purge_and_embargo_count_unique_trading_dates_not_rows_or_calendar_days():
    dates, frame = _cross_section(end="2021-06-30", symbols=3)
    splitter = ExpandingWalkForwardSplitter(
        initial_train_months=6,
        validation_months=3,
        test_months=3,
        step_months=3,
        purge_days=3,
        embargo_days=4,
    )
    fold = next(splitter.split(frame, calendar=dates))

    assert len(fold.purged_dates) == 3
    assert len(fold.embargoed_dates) == 4
    assert fold.purged_dates.isin(dates).all()
    assert fold.embargoed_dates.isin(dates).all()
    assert len(frame.iloc[fold.train_indices]) % 3 == 0


def test_short_history_raises_instead_of_emitting_partial_fold():
    dates, frame = _cross_section(end="2020-06-30")
    with pytest.raises(WalkForwardError, match="insufficient history"):
        list(_splitter().split(frame, calendar=dates))


def test_history_that_only_enters_test_window_does_not_emit_partial_fold():
    dates, frame = _cross_section(end="2020-09-15")

    with pytest.raises(WalkForwardError, match="insufficient history"):
        list(_splitter().split(frame, calendar=dates))


def test_checked_in_smoke_config_emits_one_complete_mature_fold():
    repository = Path(__file__).resolve().parents[2]
    config = load_config(
        "configs/research/csi500_smoke.yaml", repo_root=repository
    )
    dates = pd.bdate_range(config.data.start, config.data.end)
    frame = pd.DataFrame(
        {
            "date": dates,
            "entry_date": pd.Series(dates).shift(-1),
            "exit_date": pd.Series(dates).shift(-6),
        }
    )
    values = config.validation
    splitter = ExpandingWalkForwardSplitter(
        initial_train_months=values.initial_train_months,
        validation_months=values.validation_months,
        test_months=values.test_months,
        step_months=values.step_months,
        purge_days=values.purge_days,
        embargo_days=values.embargo_days,
    )

    folds = list(splitter.split(frame, calendar=dates))

    assert len(folds) == 1
    assert frame.iloc[folds[0].test_indices]["exit_date"].notna().all()


def test_incomplete_tail_labels_are_removed_and_indices_are_original_positions():
    dates, frame = _cross_section(end="2021-12-31")
    fold = list(_splitter().split(frame, calendar=dates))[-1]
    selected_test = frame.iloc[fold.test_indices]

    assert selected_test[["entry_date", "exit_date"]].notna().all().all()
    assert fold.audit is not None
    assert fold.audit.incomplete_label_rows_removed > 0
    assert all(frame.iloc[position]["date"] in fold.test_dates for position in fold.test_indices)


def test_unsorted_data_and_invalid_label_chronology_fail_fast():
    dates, frame = _cross_section(end="2021-12-31")
    unsorted = frame.iloc[::-1].reset_index(drop=True)
    with pytest.raises(WalkForwardError, match="sorted chronologically"):
        list(_splitter().split(unsorted, calendar=dates))

    invalid = frame.copy()
    invalid.loc[0, "entry_date"] = invalid.loc[0, "date"]
    with pytest.raises(WalkForwardError, match="date < entry_date < exit_date"):
        list(_splitter().split(invalid, calendar=dates))

    with pytest.raises(WalkForwardError, match="required for label-window audit"):
        list(
            _splitter().split(
                frame.drop(columns=["entry_date", "exit_date"]),
                calendar=dates,
            )
        )
