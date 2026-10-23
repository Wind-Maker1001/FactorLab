"""Canonical daily-bar schema and validation utilities."""

import re
from typing import Iterable, Optional, Sequence

import numpy as np
import pandas as pd

try:
    from qbot.research.errors import SchemaError
except ImportError:  # pragma: no cover - only useful during isolated bootstrapping
    class SchemaError(ValueError):
        """Fallback used only when the package foundation is unavailable."""


CANONICAL_BAR_COLUMNS = (
    "date",
    "symbol",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "amount",
    "turnover",
    "adj_factor",
    "raw_open",
    "raw_high",
    "raw_low",
    "raw_close",
    "adj_open",
    "adj_high",
    "adj_low",
    "adj_close",
)

PRICE_COLUMNS = (
    "open",
    "high",
    "low",
    "close",
    "raw_open",
    "raw_high",
    "raw_low",
    "raw_close",
    "adj_open",
    "adj_high",
    "adj_low",
    "adj_close",
)

NUMERIC_BAR_COLUMNS = PRICE_COLUMNS + (
    "volume",
    "amount",
    "turnover",
    "adj_factor",
)

# Compare the close-based adjustment factor, allowing only floating-point
# roundoff. HFQ may involve affine/rounded OHLC transformations, so this does
# not assert a shared multiplier for adjusted open/high/low.
ADJ_FACTOR_RTOL = 1e-8
ADJ_FACTOR_ATOL = 1e-10

_SYMBOL_RE = re.compile(r"^(?P<code>\d{6})\.(?P<exchange>SH|SZ)$")


def normalize_symbol(value: object) -> str:
    """Convert common mainland symbol spellings to ``000001.SZ`` form.

    Six-digit values without an exchange use the A-share listing convention:
    codes beginning with 5, 6, or 9 are Shanghai; all others are Shenzhen.
    Index callers should pass an explicit suffix because an index code is not
    necessarily governed by the equity-code convention (for example
    ``000905.SH``).
    """

    if value is None or (isinstance(value, float) and np.isnan(value)):
        raise SchemaError("symbol must not be null")
    text = str(value).strip().upper()
    text = text.replace("XSHG", "SH").replace("XSHE", "SZ")
    text = text.replace("SHSE", "SH").replace("SZSE", "SZ")

    # Accept vendor forms such as sh600000 and SH.600000.
    match = re.fullmatch(r"(SH|SZ)[.]?(\d{6})", text)
    if match:
        text = "%s.%s" % (match.group(2), match.group(1))
    else:
        match = re.fullmatch(r"(\d{6})[._-]?(SH|SZ)", text)
        if match:
            text = "%s.%s" % (match.group(1), match.group(2))
        elif re.fullmatch(r"\d{6}", text):
            exchange = "SH" if text[0] in {"5", "6", "9"} else "SZ"
            text = "%s.%s" % (text, exchange)

    if not _SYMBOL_RE.fullmatch(text):
        raise SchemaError("invalid canonical symbol: %r" % value)
    return text


def vendor_code(symbol: object) -> str:
    """Return the six-digit code expected by AKShare-style endpoints."""

    return normalize_symbol(symbol).split(".", 1)[0]


def empty_bars(extra_columns: Optional[Iterable[str]] = None) -> pd.DataFrame:
    """Create an empty frame with stable canonical column order and dtypes."""

    columns = list(CANONICAL_BAR_COLUMNS)
    if extra_columns:
        columns.extend(column for column in extra_columns if column not in columns)
    frame = pd.DataFrame(columns=columns)
    frame["date"] = pd.to_datetime(frame["date"])
    for column in NUMERIC_BAR_COLUMNS:
        frame[column] = pd.to_numeric(frame[column])
    return frame


def _normalise_dates(values: pd.Series) -> pd.Series:
    try:
        parsed = pd.to_datetime(values, errors="raise")
    except (TypeError, ValueError) as exc:
        raise SchemaError("date contains an invalid value") from exc
    if isinstance(parsed.dtype, pd.DatetimeTZDtype):
        parsed = parsed.dt.tz_localize(None)
    return parsed.dt.normalize().astype("datetime64[ns]")


def canonicalize_bars(
    bars: pd.DataFrame,
    symbol: Optional[str] = None,
    allow_missing_ohlc: bool = False,
) -> pd.DataFrame:
    """Return a sorted canonical frame and validate all invariants.

    The helper accepts frames where the canonical OHLC fields are present but
    the explicit ``raw_*`` or ``adj_*`` copies are absent.  In that case those
    copies are derived without changing the price values.  It never fills
    observations across dates.
    """

    if not isinstance(bars, pd.DataFrame):
        raise SchemaError("bars must be a pandas DataFrame")
    frame = bars.copy()
    if "date" not in frame.columns:
        raise SchemaError("missing required column: date")
    if "symbol" not in frame.columns:
        if symbol is None:
            raise SchemaError("missing required column: symbol")
        frame["symbol"] = normalize_symbol(symbol)
    elif symbol is not None:
        expected = normalize_symbol(symbol)
        non_null = frame["symbol"].dropna()
        if not non_null.empty:
            actual = {normalize_symbol(item) for item in non_null.unique()}
            if actual != {expected}:
                raise SchemaError(
                    "bar symbols %s do not match requested symbol %s"
                    % (sorted(actual), expected)
                )
        frame["symbol"] = expected

    frame["date"] = _normalise_dates(frame["date"])
    try:
        frame["symbol"] = frame["symbol"].map(normalize_symbol)
    except SchemaError:
        raise
    except Exception as exc:  # pragma: no cover - defensive pandas wrapper
        raise SchemaError("symbol normalization failed") from exc

    canonical_ohlc = ("open", "high", "low", "close")
    for field in canonical_ohlc:
        raw_field = "raw_%s" % field
        adj_field = "adj_%s" % field
        if field not in frame.columns and raw_field in frame.columns:
            frame[field] = frame[raw_field]
        if raw_field not in frame.columns and field in frame.columns:
            frame[raw_field] = frame[field]
        if adj_field not in frame.columns and field in frame.columns:
            frame[adj_field] = frame[field]

    required_without_factor = set(CANONICAL_BAR_COLUMNS) - {"adj_factor"}
    missing = sorted(required_without_factor - set(frame.columns))
    if missing:
        raise SchemaError("missing required columns: %s" % ", ".join(missing))

    for column in NUMERIC_BAR_COLUMNS:
        if column == "adj_factor" and column not in frame.columns:
            continue
        frame[column] = pd.to_numeric(frame[column], errors="coerce")

    if "adj_factor" not in frame.columns:
        raw_close = frame["raw_close"].replace(0.0, np.nan)
        frame["adj_factor"] = frame["adj_close"] / raw_close

    frame = frame.sort_values(["date", "symbol"], kind="mergesort").reset_index(
        drop=True
    )
    extras = [column for column in frame.columns if column not in CANONICAL_BAR_COLUMNS]
    frame = frame[list(CANONICAL_BAR_COLUMNS) + extras]
    validate_bars(frame, allow_missing_ohlc=allow_missing_ohlc)
    return frame


def _validate_ohlc_block(
    frame: pd.DataFrame,
    columns: Sequence[str],
    allow_missing_ohlc: bool,
) -> None:
    block = frame[list(columns)]
    missing_count = block.isna().sum(axis=1)
    if allow_missing_ohlc:
        partial = (missing_count > 0) & (missing_count < len(columns))
        if partial.any():
            raise SchemaError("OHLC values must be either all present or all missing")
    elif missing_count.any():
        raise SchemaError("OHLC values must not be missing")

    complete = block.notna().all(axis=1)
    if not complete.any():
        return
    selected = block.loc[complete]
    open_col, high_col, low_col, close_col = columns
    invalid = (
        (selected <= 0).any(axis=1)
        | (selected[high_col] < selected[[open_col, close_col]].max(axis=1))
        | (selected[low_col] > selected[[open_col, close_col]].min(axis=1))
        | (selected[high_col] < selected[low_col])
    )
    if invalid.any():
        raise SchemaError("OHLC relationship is invalid")


def validate_bars(
    bars: pd.DataFrame,
    allow_missing_ohlc: bool = False,
    require_sorted: bool = True,
) -> None:
    """Validate canonical symbols, dates, uniqueness, and market fields."""

    missing = sorted(set(CANONICAL_BAR_COLUMNS) - set(bars.columns))
    if missing:
        raise SchemaError("missing required columns: %s" % ", ".join(missing))
    if not pd.api.types.is_datetime64_ns_dtype(bars["date"].dtype):
        raise SchemaError("date must use tz-naive datetime64[ns]")
    if getattr(bars["date"].dt, "tz", None) is not None:
        raise SchemaError("date must be timezone-naive")
    if bars[["date", "symbol"]].isna().any().any():
        raise SchemaError("date and symbol must not be null")
    if bars.duplicated(["date", "symbol"]).any():
        raise SchemaError("duplicate (date, symbol) rows detected")
    if require_sorted:
        expected = bars.sort_values(["date", "symbol"], kind="mergesort").index
        if not expected.equals(bars.index):
            raise SchemaError("bars must be sorted by date, symbol")
    invalid_symbols = [
        item for item in bars["symbol"].unique() if not _SYMBOL_RE.fullmatch(str(item))
    ]
    if invalid_symbols:
        raise SchemaError("non-canonical symbols detected: %s" % invalid_symbols[:3])

    for column in NUMERIC_BAR_COLUMNS:
        if (
            not pd.api.types.is_numeric_dtype(bars[column])
            or pd.api.types.is_complex_dtype(bars[column])
        ):
            raise SchemaError("%s must contain real numeric values" % column)
        # Nullable pandas numeric columns preserve their missing observations.
        # Infinity is never a valid market field, including a vendor string
        # such as "inf" that numeric coercion converted to a float.
        values = bars[column].to_numpy(dtype=np.float64, na_value=np.nan)
        if np.isinf(values).any():
            raise SchemaError("%s must be finite when present" % column)

    _validate_ohlc_block(
        bars, ("open", "high", "low", "close"), allow_missing_ohlc
    )
    _validate_ohlc_block(
        bars,
        ("raw_open", "raw_high", "raw_low", "raw_close"),
        allow_missing_ohlc,
    )
    _validate_ohlc_block(
        bars,
        ("adj_open", "adj_high", "adj_low", "adj_close"),
        allow_missing_ohlc,
    )

    for column in ("volume", "amount", "turnover"):
        negative = bars[column].notna() & (bars[column] < 0)
        if negative.any():
            raise SchemaError("%s must be non-negative" % column)
    invalid_factor = bars["adj_factor"].notna() & (bars["adj_factor"] <= 0)
    if invalid_factor.any():
        raise SchemaError("adj_factor must be positive when present")
    comparable_factor = bars[
        ["adj_factor", "adj_close", "raw_close"]
    ].notna().all(axis=1)
    if comparable_factor.any():
        declared_factor = bars.loc[comparable_factor, "adj_factor"].to_numpy(
            dtype=np.float64
        )
        with np.errstate(over="ignore", divide="ignore", invalid="ignore"):
            observed_factor = (
                bars.loc[comparable_factor, "adj_close"].to_numpy(dtype=np.float64)
                / bars.loc[comparable_factor, "raw_close"].to_numpy(dtype=np.float64)
            )
        if not np.isclose(
            declared_factor, observed_factor,
            rtol=ADJ_FACTOR_RTOL, atol=ADJ_FACTOR_ATOL,
        ).all():
            raise SchemaError("adj_factor must match adj_close / raw_close")

    for field in ("open", "high", "low", "close"):
        canonical = bars[field]
        raw = bars["raw_%s" % field]
        if not canonical.isna().eq(raw.isna()).all():
            raise SchemaError(
                "canonical and raw OHLC must have identical missing observations"
            )
        comparable = canonical.notna() & raw.notna()
        if comparable.any() and not np.allclose(
            canonical.loc[comparable], raw.loc[comparable], rtol=1e-12, atol=1e-12
        ):
            raise SchemaError("canonical OHLC must represent raw prices")


__all__ = [
    "ADJ_FACTOR_RTOL",
    "ADJ_FACTOR_ATOL",
    "CANONICAL_BAR_COLUMNS",
    "NUMERIC_BAR_COLUMNS",
    "PRICE_COLUMNS",
    "canonicalize_bars",
    "empty_bars",
    "normalize_symbol",
    "validate_bars",
    "vendor_code",
]
