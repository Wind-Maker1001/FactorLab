"""Associate frozen forward predictions with execution-aligned realised labels.

This is an in-memory calculation, not a signal recorder or a provenance gate.
Callers must select the predeclared cohort/version and obtain predictions from
``load_prediction_record(require_completion=True)`` before calling it. Neither
this function nor a non-empty IC proves PIT membership or timely publication.
No model is fitted, stock is rescored, price is filled, or artifact is written.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
import re
from typing import Optional

import numpy as np
import pandas as pd
from pandas.api.types import is_bool_dtype, is_complex_dtype, is_numeric_dtype

from factorlab.data.calendar import TradingCalendar, reindex_bars_to_calendar
from factorlab.data.schema import canonicalize_bars, normalize_symbol
from factorlab.errors import EvaluationError, SchemaError
from factorlab.labels.forward_return import build_forward_excess_return_labels
from factorlab.market.regime import MARKET_REGIMES, build_market_regime_features
from factorlab.metrics.prediction import evaluate_predictions


LABEL_STATUSES = ("MATURE", "MATURE_MISSING_PRICE", "IMMATURE")
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_LABEL_COLUMN = re.compile(r"^(STOCK_FWD_RET|BENCHMARK_FWD_RET|EXCESS_RET)\d+$")
_GENERATED_COLUMNS = {
    "entry_date", "exit_date", "holding_days", "as_of", "label_status",
    "EVALUATION_MARKET_REGIME",
}


class ForwardEvaluationError(EvaluationError):
    """An input cannot be evaluated without changing its research meaning."""


@dataclass(frozen=True)
class ForwardEvaluation:
    """Original predictions, labelled observations, and coverage-aware metrics."""

    predictions: pd.DataFrame
    daily: pd.DataFrame
    summary: pd.DataFrame
    as_of: str


def _trading_date(value: object, field: str) -> pd.Timestamp:
    if isinstance(value, str):
        if not _ISO_DATE.fullmatch(value):
            raise ForwardEvaluationError("%s must be a canonical ISO trading date" % field)
    elif not isinstance(value, (date, datetime, pd.Timestamp, np.datetime64)):
        raise ForwardEvaluationError("%s must be a trading date, not a numeric/boolean value" % field)
    try:
        parsed = pd.Timestamp(value)
    except (ValueError, TypeError, OverflowError) as exc:
        raise ForwardEvaluationError("%s contains an invalid trading date" % field) from exc
    if pd.isna(parsed) or parsed.tzinfo is not None or parsed != parsed.normalize():
        raise ForwardEvaluationError("%s must be a non-missing timezone-naive midnight date" % field)
    try:
        return parsed.as_unit("ns")
    except (ValueError, OverflowError) as exc:
        raise ForwardEvaluationError("%s is outside the supported trading-date range" % field) from exc


def _trading_dates(values: pd.Series, field: str) -> pd.Series:
    # Validate distinct dates, not every stock-day row. Forward metadata may
    # retain ISO strings; the separate research key always uses datetime64[ns].
    try:
        lookup = {value: _trading_date(value, field) for value in values.unique()}
        return pd.to_datetime(values.map(lookup)).astype("datetime64[ns]")
    except (TypeError, ValueError) as exc:
        raise ForwardEvaluationError("%s contains invalid trading dates" % field) from exc


def _integer(value: object, field: str, minimum: int = 1) -> int:
    if isinstance(value, (bool, np.bool_)) or not isinstance(value, (int, np.integer)) or int(value) < minimum:
        raise ForwardEvaluationError("%s must be an integer >= %d" % (field, minimum))
    return int(value)


def _finite_numeric(values: pd.Series, field: str) -> np.ndarray:
    if not is_numeric_dtype(values.dtype) or is_bool_dtype(values.dtype) or is_complex_dtype(values.dtype):
        raise ForwardEvaluationError("%s must contain real numeric values, not bool/string" % field)
    array = values.to_numpy(dtype=float, na_value=np.nan)
    if not np.isfinite(array).all():
        raise ForwardEvaluationError("%s must contain only finite values" % field)
    return array


def _predictions(frame: pd.DataFrame, cutoff: pd.Timestamp) -> pd.DataFrame:
    if not isinstance(frame, pd.DataFrame) or frame.empty:
        raise ForwardEvaluationError("predictions must be a non-empty DataFrame")
    if frame.columns.has_duplicates or not all(isinstance(column, str) for column in frame.columns):
        raise ForwardEvaluationError("prediction column names must be unique strings")
    required = {"signal_date", "symbol", "score", "rank"}
    missing = sorted(required.difference(frame.columns))
    if missing:
        raise ForwardEvaluationError("predictions missing required columns: %s" % missing)
    collisions = [column for column in frame.columns if column in _GENERATED_COLUMNS or _LABEL_COLUMN.fullmatch(column)]
    if collisions:
        raise ForwardEvaluationError("predictions already contain evaluation/label columns: %s" % collisions)
    result = frame.copy(deep=True)
    dates = _trading_dates(result["signal_date"], "signal_date")
    if (dates > cutoff).any():
        raise ForwardEvaluationError("signal_date cannot be later than as_of")
    if "date" in result and not _trading_dates(result["date"], "date").equals(dates):
        raise ForwardEvaluationError("date must equal signal_date")
    result["date"] = dates
    try:
        symbols = {value: normalize_symbol(value) for value in result["symbol"].unique()}
    except (TypeError, SchemaError) as exc:
        raise ForwardEvaluationError("prediction symbols must be canonical") from exc
    if any(not isinstance(value, str) or value != canonical for value, canonical in symbols.items()):
        raise ForwardEvaluationError("prediction symbols must already be canonical")
    if result.duplicated(["date", "symbol"]).any():
        raise ForwardEvaluationError("duplicate prediction (date, symbol) observations")
    _finite_numeric(result["score"], "score")
    ranks = _finite_numeric(result["rank"], "rank")
    if ((ranks < 1) | (ranks != np.floor(ranks))).any():
        raise ForwardEvaluationError("rank must contain positive integers")
    ordered = result.sort_values(["date", "score", "symbol"], ascending=[True, False, True], kind="mergesort")
    expected_ranks = ordered.groupby("date", sort=False).cumcount().to_numpy() + 1
    if not np.array_equal(ordered["rank"].to_numpy(), expected_ranks):
        raise ForwardEvaluationError("ranks must be contiguous 1..N in score-descending/symbol-ascending order")
    return result


def _bars(frame: pd.DataFrame, *, role: str, symbol: Optional[str] = None) -> pd.DataFrame:
    if not isinstance(frame, pd.DataFrame) or frame.columns.has_duplicates:
        raise SchemaError("%s bars must be a DataFrame with unique columns" % role)
    if "adj_open" not in frame:
        raise SchemaError("%s bars require explicit adj_open; raw-price fallback is forbidden" % role)
    if "date" in frame:
        _trading_dates(frame["date"], "%s bar date" % role)
    return canonicalize_bars(frame, symbol=symbol, allow_missing_ohlc=(role == "stock"))


def evaluate_forward_predictions(
    predictions: pd.DataFrame,
    stock_bars: pd.DataFrame,
    benchmark_bars: pd.DataFrame,
    *,
    as_of: object,
    holding_days: int = 5,
    top_k: int = 20,
    minimum_cross_section: int = 20,
    benchmark_symbol: str = "000905.SH",
    regime_price_column: Optional[str] = None,
    include_regime: bool = True,
) -> ForwardEvaluation:
    """Evaluate a frozen cross-section using bars observed through ``as_of``.

    Signal ``t`` enters at session ``t+1`` and exits at ``t+holding_days+1``.
    Calendar maturity and observed-price availability are distinct: a missing
    stock/bar does not shorten the holding period or replace its ranked stock.
    Immature predictions remain in daily and summary coverage denominators.

    ``as_of`` is an observed benchmark session, not an assertion about when
    this evaluation was performed. The production caller enforces the actual
    clock, completion evidence, PIT/source hashes, and immutable result storage.
    All bars are schema-validated before cutting off future observations.
    ``include_regime`` uses a saved MARKET_REGIME if provided; otherwise it
    adds EVALUATION_MARKET_REGIME using the unchanged production MA20/MA60
    rule. Adjusted close is required by default; there is no raw-price fallback.
    """

    cutoff = _trading_date(as_of, "as_of")
    holding_days = _integer(holding_days, "holding_days")
    top_k = _integer(top_k, "top_k")
    minimum_cross_section = _integer(minimum_cross_section, "minimum_cross_section", 2)
    if not isinstance(include_regime, bool):
        raise ForwardEvaluationError("include_regime must be bool")
    if regime_price_column is not None and regime_price_column not in {"adj_close", "close"}:
        raise ForwardEvaluationError("regime_price_column must be adj_close or close")
    if not isinstance(benchmark_symbol, str) or normalize_symbol(benchmark_symbol) != benchmark_symbol:
        raise ForwardEvaluationError("benchmark_symbol must already be canonical")
    original = _predictions(predictions, cutoff)
    stock = _bars(stock_bars, role="stock")
    benchmark = _bars(benchmark_bars, role="benchmark", symbol=benchmark_symbol)
    if include_regime and "MARKET_REGIME" not in original:
        selected_price = regime_price_column or "adj_close"
        if selected_price not in benchmark_bars:
            raise SchemaError("Benchmark requires explicit %s for regime evaluation" % selected_price)
    benchmark = benchmark.loc[benchmark["date"] <= cutoff].copy()
    calendar = TradingCalendar.from_benchmark_bars(benchmark)
    if not calendar.contains(cutoff):
        raise ForwardEvaluationError("as_of must be an observed benchmark trading date")
    if not original["date"].isin(calendar.dates).all():
        raise ForwardEvaluationError("every signal_date must be an observed benchmark trading date")
    symbols = original["symbol"].unique()
    stock = stock.loc[(stock["date"] <= cutoff) & stock["symbol"].isin(symbols)].copy()
    stock = reindex_bars_to_calendar(stock, calendar, symbols=symbols)
    labels = build_forward_excess_return_labels(stock, benchmark, holding_days=holding_days, calendar=calendar.dates)
    linked = original.merge(labels, on=["date", "symbol"], how="left", sort=False, validate="one_to_one")
    mature = linked["exit_date"].notna() & (linked["exit_date"] <= cutoff)
    target_col = "EXCESS_RET%d" % holding_days
    linked["label_status"] = np.select(
        [mature & linked[target_col].notna(), mature],
        ["MATURE", "MATURE_MISSING_PRICE"], default="IMMATURE",
    )
    linked["holding_days"] = holding_days
    linked["as_of"] = cutoff.date().isoformat()
    regime_col = None
    if include_regime:
        if "MARKET_REGIME" in linked:
            if not linked["MARKET_REGIME"].isin(MARKET_REGIMES).all():
                raise ForwardEvaluationError("saved MARKET_REGIME contains missing or unknown states")
            regime_col = "MARKET_REGIME"
        else:
            regime_col = "EVALUATION_MARKET_REGIME"
            regime = build_market_regime_features(benchmark, price_column=regime_price_column or "adj_close")
            linked[regime_col] = linked["date"].map(regime.set_index("date")["MARKET_REGIME"])
        if (linked.groupby("date", sort=False)[regime_col].nunique() != 1).any():
            raise ForwardEvaluationError("MARKET_REGIME must be constant within each signal_date")
    metrics = evaluate_predictions(linked, target_col=target_col, top_k=top_k,
                                   minimum_cross_section=minimum_cross_section,
                                   fold_col=None, regime_col=regime_col)
    return ForwardEvaluation(linked, metrics.daily, metrics.summary, cutoff.date().isoformat())


__all__ = ["ForwardEvaluation", "ForwardEvaluationError", "LABEL_STATUSES", "evaluate_forward_predictions"]
