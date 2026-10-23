"""CSI500 market-regime and trailing index features.

Every value emitted for signal date ``t`` is calculated from observations no
later than ``t``.  Missing prices are deliberately left missing; this module
never fills a market-data gap in either direction.
"""

from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd
from pandas.api.types import is_bool_dtype, is_complex_dtype, is_numeric_dtype

from qbot.research.data.schema import normalize_symbol
from qbot.research.errors import SchemaError


CSI500_SYMBOL = "000905.SH"
MARKET_REGIMES = ("BULL", "BEAR", "SIDEWAYS")
MARKET_FEATURE_COLUMNS = (
    "date",
    "MARKET_REGIME",
    "INDEX_RET5",
    "INDEX_RET20",
    "INDEX_RET60",
    "INDEX_VOL20",
)
_SUPPORTED_PRICE_COLUMNS = ("adj_close", "close")


def _normalise_and_validate_dates(values: pd.Series) -> pd.Series:
    try:
        dates = pd.to_datetime(values, errors="raise")
    except (TypeError, ValueError) as exc:
        raise SchemaError("Benchmark date contains an invalid value") from exc
    if dates.isna().any():
        raise SchemaError("Benchmark date must not contain missing values")
    if getattr(dates.dt, "tz", None) is not None:
        raise SchemaError("Benchmark date must be timezone-naive")
    normalized = dates.dt.normalize().astype("datetime64[ns]")
    if not dates.equals(normalized):
        raise SchemaError("Benchmark date must contain trading dates at midnight")
    if normalized.duplicated().any():
        raise SchemaError("Benchmark contains duplicate dates")
    if not normalized.is_monotonic_increasing:
        raise SchemaError("Benchmark dates must be strictly increasing")
    return normalized


def _select_price_column(
    benchmark: pd.DataFrame, price_column: Optional[str]
) -> str:
    if price_column is not None:
        selected = str(price_column)
        if selected not in _SUPPORTED_PRICE_COLUMNS:
            raise SchemaError(
                "price_column must be one of %s"
                % ", ".join(_SUPPORTED_PRICE_COLUMNS)
            )
        if selected not in benchmark.columns:
            raise SchemaError("Benchmark missing price column: %s" % selected)
        return selected
    if "adj_close" in benchmark.columns:
        return "adj_close"
    if "close" in benchmark.columns:
        return "close"
    raise SchemaError("Benchmark must contain adj_close or close")


def _validate_price(values: pd.Series, column: str) -> pd.Series:
    if (
        not is_numeric_dtype(values.dtype)
        or is_bool_dtype(values.dtype)
        or is_complex_dtype(values.dtype)
    ):
        raise SchemaError("Benchmark %s must be real numeric data" % column)
    price = values.astype(float)
    finite_or_missing = np.isfinite(price.to_numpy()) | price.isna().to_numpy()
    if not finite_or_missing.all():
        raise SchemaError("Benchmark %s must not contain infinity" % column)
    if (price.dropna() <= 0.0).any():
        raise SchemaError("Benchmark %s must be positive when present" % column)
    return price


def _validate_symbol(benchmark: pd.DataFrame) -> None:
    if "symbol" not in benchmark.columns:
        return
    if benchmark["symbol"].isna().any():
        raise SchemaError("Benchmark symbol must not be missing")
    try:
        symbols = {normalize_symbol(value) for value in benchmark["symbol"].unique()}
    except SchemaError:
        raise
    except Exception as exc:  # pragma: no cover - defensive pandas wrapper
        raise SchemaError("Benchmark symbol normalization failed") from exc
    if symbols != {CSI500_SYMBOL}:
        raise SchemaError(
            "Market regime requires CSI500 benchmark symbol %s" % CSI500_SYMBOL
        )


def build_market_regime_features(
    benchmark: pd.DataFrame,
    price_column: Optional[str] = None,
) -> pd.DataFrame:
    """Build trailing CSI500 features and the BULL/BEAR/SIDEWAYS state.

    ``adj_close`` is preferred when both supported price columns are present;
    callers may explicitly request ``close``.  The choice applies to the whole
    series, so a missing adjusted price is never patched with a raw price.

    The first observations without a complete 60-session moving average meet
    neither BULL nor BEAR and are therefore classified as SIDEWAYS, matching
    the frozen rule that all remaining cases are SIDEWAYS.  Their unavailable
    return/volatility features remain ``NaN``.
    """

    if not isinstance(benchmark, pd.DataFrame):
        raise SchemaError("Benchmark must be a pandas DataFrame")
    if benchmark.empty:
        raise SchemaError("Benchmark must not be empty")
    if "date" not in benchmark.columns:
        raise SchemaError("Benchmark missing required column: date")

    selected_price = _select_price_column(benchmark, price_column)
    dates = _normalise_and_validate_dates(benchmark["date"])
    _validate_symbol(benchmark)
    close = _validate_price(benchmark[selected_price], selected_price)

    ma20 = close.rolling(20, min_periods=20).mean()
    ma60 = close.rolling(60, min_periods=60).mean()
    bull = (close > ma60) & (ma20 > ma60)
    bear = (close < ma60) & (ma20 < ma60)
    regimes = np.select(
        [bull.to_numpy(), bear.to_numpy()],
        ["BULL", "BEAR"],
        default="SIDEWAYS",
    )

    daily_return = close.pct_change(fill_method=None)
    result = pd.DataFrame(
        {
            "date": dates.to_numpy(),
            "MARKET_REGIME": regimes,
            "INDEX_RET5": close / close.shift(5) - 1.0,
            "INDEX_RET20": close / close.shift(20) - 1.0,
            "INDEX_RET60": close / close.shift(60) - 1.0,
            "INDEX_VOL20": daily_return.rolling(20, min_periods=20).std(ddof=1),
        }
    )
    return result.loc[:, list(MARKET_FEATURE_COLUMNS)].reset_index(drop=True)


def compute_market_regime(
    benchmark: pd.DataFrame,
    price_column: Optional[str] = None,
) -> pd.DataFrame:
    """Compatibility-friendly name for :func:`build_market_regime_features`."""

    return build_market_regime_features(benchmark, price_column=price_column)


__all__ = [
    "CSI500_SYMBOL",
    "MARKET_FEATURE_COLUMNS",
    "MARKET_REGIMES",
    "build_market_regime_features",
    "compute_market_regime",
]
