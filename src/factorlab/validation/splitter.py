"""Deterministic expanding walk-forward splits with leakage guards.

The splitter works with signal dates, rather than individual cross-sectional
rows.  Calendar-month offsets define the nominal train, validation, and test
windows, while purge and embargo gaps are counted on the supplied trading
calendar.  Label ``entry_date``/``exit_date`` columns are mandatory: rows with
incomplete or boundary-overlapping label windows are removed using their
actual realised horizon, rather than trusting a purge count alone.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Iterator, Optional, Tuple

import numpy as np
import pandas as pd


class WalkForwardError(ValueError):
    """Raised when a safe walk-forward split cannot be constructed."""


def _positive_integer(name: str, value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, np.integer)):
        raise WalkForwardError("%s must be an integer" % name)
    if int(value) <= 0:
        raise WalkForwardError("%s must be positive" % name)
    return int(value)


def _non_negative_integer(name: str, value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, np.integer)):
        raise WalkForwardError("%s must be an integer" % name)
    if int(value) < 0:
        raise WalkForwardError("%s must be non-negative" % name)
    return int(value)


def _normalise_dates(values: Iterable[object], name: str) -> pd.DatetimeIndex:
    try:
        result = pd.DatetimeIndex(pd.to_datetime(list(values), errors="raise"))
    except (TypeError, ValueError) as exc:
        raise WalkForwardError("%s contains invalid dates" % name) from exc
    if result.tz is not None:
        result = result.tz_localize(None)
    result = result.normalize()
    if result.isna().any():
        raise WalkForwardError("%s contains missing dates" % name)
    return result


def _unique_calendar(values: Iterable[object]) -> pd.DatetimeIndex:
    dates = _normalise_dates(values, "calendar")
    if dates.empty:
        raise WalkForwardError("calendar must contain at least one trading date")
    if dates.has_duplicates:
        raise WalkForwardError("calendar trading dates must be unique")
    if not dates.is_monotonic_increasing:
        raise WalkForwardError("calendar trading dates must be sorted")
    return dates


def _window(
    calendar: pd.DatetimeIndex, start: pd.Timestamp, end: pd.Timestamp
) -> pd.DatetimeIndex:
    """Return trading dates in the half-open calendar interval [start, end)."""

    return calendar[(calendar >= start) & (calendar < end)]


def _trim_end(dates: pd.DatetimeIndex, count: int) -> Tuple[pd.DatetimeIndex, pd.DatetimeIndex]:
    if count == 0:
        return dates, pd.DatetimeIndex([])
    if len(dates) <= count:
        return pd.DatetimeIndex([]), dates
    return dates[:-count], dates[-count:]


def _dates_from_positions(
    row_dates: pd.Series, positions: np.ndarray
) -> pd.DatetimeIndex:
    if len(positions) == 0:
        return pd.DatetimeIndex([])
    values = row_dates.iloc[positions].drop_duplicates().sort_values()
    return pd.DatetimeIndex(values)


@dataclass(frozen=True)
class BoundaryAudit:
    """Evidence that label windows do not cross fold boundaries."""

    train_overlap_rows_removed: int
    validation_overlap_rows_removed: int
    incomplete_label_rows_removed: int
    train_max_exit_date: Optional[pd.Timestamp]
    validation_max_exit_date: Optional[pd.Timestamp]
    validation_first_date: pd.Timestamp
    test_first_date: pd.Timestamp

    @property
    def label_windows_checked(self) -> bool:
        """Whether entry/exit metadata was present and audited."""

        return self.incomplete_label_rows_removed >= 0


@dataclass(frozen=True)
class WalkForwardFold:
    """One immutable expanding train/validation/test partition.

    Indices are zero-based row positions into the DataFrame supplied to
    :meth:`ExpandingWalkForwardSplitter.split`; they are never shuffled.
    ``train_dates``/``validation_dates``/``test_dates`` contain exactly the
    unique signal dates represented by those positions after leakage checks.
    """

    fold: int
    train_indices: np.ndarray
    validation_indices: np.ndarray
    test_indices: np.ndarray
    train_dates: pd.DatetimeIndex
    validation_dates: pd.DatetimeIndex
    test_dates: pd.DatetimeIndex
    purged_dates: pd.DatetimeIndex
    embargoed_dates: pd.DatetimeIndex
    train_window: Tuple[pd.Timestamp, pd.Timestamp]
    validation_window: Tuple[pd.Timestamp, pd.Timestamp]
    test_window: Tuple[pd.Timestamp, pd.Timestamp]
    audit: BoundaryAudit

    def as_indices(self) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Return the three row-position arrays for model code."""

        return self.train_indices, self.validation_indices, self.test_indices


class ExpandingWalkForwardSplitter:
    """Create deterministic expanding walk-forward folds.

    Nominal windows are half-open calendar intervals.  For every fold the
    training origin remains fixed, while all three right-hand boundaries move
    by ``step_months``.  ``purge_days`` removes the final trading dates of the
    nominal training window.  ``embargo_days`` reserves the final trading
    dates of the nominal validation window as a gap before test evaluation.

    Label-window metadata is mandatory and authoritative: train rows must
    have ``exit_date < first validation signal date`` and validation rows must
    have ``exit_date < first test signal date``.  Missing label-window dates
    are excluded from all three partitions.
    """

    def __init__(
        self,
        initial_train_months: int = 24,
        validation_months: int = 3,
        test_months: int = 3,
        step_months: int = 3,
        purge_days: int = 5,
        embargo_days: int = 5,
    ) -> None:
        self.initial_train_months = _positive_integer(
            "initial_train_months", initial_train_months
        )
        self.validation_months = _positive_integer(
            "validation_months", validation_months
        )
        self.test_months = _positive_integer("test_months", test_months)
        self.step_months = _positive_integer("step_months", step_months)
        self.purge_days = _non_negative_integer("purge_days", purge_days)
        self.embargo_days = _non_negative_integer("embargo_days", embargo_days)

    def split(
        self,
        data: pd.DataFrame,
        *,
        date_col: str = "date",
        entry_date_col: str = "entry_date",
        exit_date_col: str = "exit_date",
        calendar: Optional[Iterable[object]] = None,
    ) -> Iterator[WalkForwardFold]:
        """Yield folds using original DataFrame row positions.

        Supplying an explicit market calendar is recommended when the input
        frame can omit all stocks on a session.  Otherwise the sorted unique
        values in ``date_col`` form the trading calendar.  Data must itself be
        chronologically sorted; silently sorting it would make returned row
        positions surprising.
        """

        if not isinstance(data, pd.DataFrame):
            raise WalkForwardError("data must be a pandas DataFrame")
        if date_col not in data.columns:
            raise WalkForwardError("data is missing date column %r" % date_col)
        if data.empty:
            raise WalkForwardError("data must not be empty")

        row_index = pd.RangeIndex(len(data))
        row_dates_index = _normalise_dates(data[date_col], date_col)
        row_dates = pd.Series(row_dates_index, index=row_index, name=date_col)
        if not row_dates.is_monotonic_increasing:
            raise WalkForwardError("data must be sorted chronologically by %s" % date_col)

        if calendar is None:
            trading_calendar = pd.DatetimeIndex(row_dates.drop_duplicates())
        else:
            trading_calendar = _unique_calendar(calendar)
            outside = pd.DatetimeIndex(row_dates.drop_duplicates()).difference(
                trading_calendar
            )
            if len(outside):
                raise WalkForwardError(
                    "data contains signal dates outside the supplied calendar"
                )
        trading_calendar = _unique_calendar(trading_calendar)

        missing_label_columns = [
            name
            for name in (entry_date_col, exit_date_col)
            if name not in data.columns
        ]
        if missing_label_columns:
            raise WalkForwardError(
                "entry_date and exit_date columns are required for label-window audit"
            )
        entry = self._optional_date_series(data[entry_date_col], entry_date_col)
        exit_ = self._optional_date_series(data[exit_date_col], exit_date_col)
        known = entry.notna() & exit_.notna()
        invalid = known & ((entry <= row_dates) | (exit_ <= entry))
        if invalid.any():
            raise WalkForwardError(
                "label audit requires date < entry_date < exit_date"
            )
        label_dates: Tuple[pd.Series, pd.Series] = entry, exit_

        produced = 0
        anchor = trading_calendar[0]
        fold_number = 0
        while True:
            train_end = anchor + pd.DateOffset(
                months=self.initial_train_months + fold_number * self.step_months
            )
            validation_end = train_end + pd.DateOffset(months=self.validation_months)
            test_end = validation_end + pd.DateOffset(months=self.test_months)

            nominal_train = _window(trading_calendar, anchor, train_end)
            nominal_validation = _window(trading_calendar, train_end, validation_end)
            nominal_test = _window(trading_calendar, validation_end, test_end)

            if not len(nominal_test):
                break
            # A test window is half-open.  Require the supplied calendar to
            # reach its final expected weekday; otherwise a few tail sessions
            # must not be presented as a full multi-month Test fold.  This is
            # intentionally conservative around exchange holidays.
            expected_test_tail = (test_end - pd.offsets.BDay(1)).normalize()
            if trading_calendar[-1] < expected_test_tail:
                break
            if not len(nominal_train) or not len(nominal_validation):
                break

            train_allowed, purged = _trim_end(nominal_train, self.purge_days)
            validation_allowed, embargoed = _trim_end(
                nominal_validation, self.embargo_days
            )
            if not len(train_allowed) or not len(validation_allowed):
                raise WalkForwardError(
                    "purge/embargo removes an entire train or validation window"
                )

            validation_first = validation_allowed[0]
            test_first = nominal_test[0]
            train_mask = row_dates.isin(train_allowed)
            validation_mask = row_dates.isin(validation_allowed)
            test_mask = row_dates.isin(nominal_test)

            entry, exit_ = label_dates
            complete = entry.notna() & exit_.notna()
            incomplete_removed = int(
                ((train_mask | validation_mask | test_mask) & ~complete).sum()
            )
            train_overlap = train_mask & complete & (exit_ >= validation_first)
            validation_overlap = validation_mask & complete & (exit_ >= test_first)
            train_mask &= complete & (exit_ < validation_first)
            validation_mask &= complete & (exit_ < test_first)
            test_mask &= complete
            train_max_exit = self._max_date(exit_[train_mask])
            validation_max_exit = self._max_date(exit_[validation_mask])
            audit = BoundaryAudit(
                train_overlap_rows_removed=int(train_overlap.sum()),
                validation_overlap_rows_removed=int(validation_overlap.sum()),
                incomplete_label_rows_removed=incomplete_removed,
                train_max_exit_date=train_max_exit,
                validation_max_exit_date=validation_max_exit,
                validation_first_date=validation_first,
                test_first_date=test_first,
            )

            train_positions = row_index[train_mask.to_numpy()].to_numpy(dtype=np.int64)
            validation_positions = row_index[
                validation_mask.to_numpy()
            ].to_numpy(dtype=np.int64)
            test_positions = row_index[test_mask.to_numpy()].to_numpy(dtype=np.int64)
            if not len(train_positions) or not len(validation_positions) or not len(test_positions):
                raise WalkForwardError(
                    "fold %d has no usable train, validation, or test rows" % fold_number
                )

            train_dates = _dates_from_positions(row_dates, train_positions)
            validation_dates = _dates_from_positions(row_dates, validation_positions)
            test_dates = _dates_from_positions(row_dates, test_positions)
            self._assert_partition_order(train_dates, validation_dates, test_dates)

            yield WalkForwardFold(
                fold=fold_number,
                train_indices=train_positions,
                validation_indices=validation_positions,
                test_indices=test_positions,
                train_dates=train_dates,
                validation_dates=validation_dates,
                test_dates=test_dates,
                purged_dates=purged,
                embargoed_dates=embargoed,
                train_window=(anchor, train_end),
                validation_window=(train_end, validation_end),
                test_window=(validation_end, test_end),
                audit=audit,
            )
            produced += 1
            fold_number += 1

        if produced == 0:
            required_months = (
                self.initial_train_months
                + self.validation_months
                + self.test_months
            )
            raise WalkForwardError(
                "insufficient history for the first fold; need approximately "
                "%d calendar months" % required_months
            )

    def split_indices(
        self, data: pd.DataFrame, **kwargs: object
    ) -> Iterator[Tuple[np.ndarray, np.ndarray, np.ndarray]]:
        """Yield sklearn-like positional triples without losing validation."""

        for fold in self.split(data, **kwargs):
            yield fold.as_indices()

    def get_n_splits(self, data: pd.DataFrame, **kwargs: object) -> int:
        """Return the number of usable folds for the supplied data."""

        return sum(1 for _ in self.split(data, **kwargs))

    @staticmethod
    def _optional_date_series(values: pd.Series, name: str) -> pd.Series:
        try:
            converted = pd.to_datetime(values, errors="raise")
        except (TypeError, ValueError) as exc:
            raise WalkForwardError("%s contains invalid dates" % name) from exc
        if getattr(converted.dt, "tz", None) is not None:
            converted = converted.dt.tz_localize(None)
        return converted.dt.normalize().reset_index(drop=True)

    @staticmethod
    def _max_date(values: pd.Series) -> Optional[pd.Timestamp]:
        result = values.max()
        return None if pd.isna(result) else pd.Timestamp(result)

    @staticmethod
    def _assert_partition_order(
        train: pd.DatetimeIndex,
        validation: pd.DatetimeIndex,
        test: pd.DatetimeIndex,
    ) -> None:
        if not (train.max() < validation.min() < test.min()):
            raise WalkForwardError(
                "walk-forward train, validation, and test periods must be ordered"
            )
        if len(train.intersection(validation)) or len(train.intersection(test)):
            raise WalkForwardError("walk-forward date partitions overlap")
        if len(validation.intersection(test)):
            raise WalkForwardError("walk-forward date partitions overlap")


__all__ = [
    "BoundaryAudit",
    "ExpandingWalkForwardSplitter",
    "WalkForwardError",
    "WalkForwardFold",
]
