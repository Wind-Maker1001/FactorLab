"""AKShare-backed implementation of the FactorLab market-data interface."""

import importlib
import http.client
import logging
import math
from numbers import Real
import socket
import time
from typing import Any, Callable, Dict, Iterable, Optional, Sequence, Tuple
import urllib.error

import numpy as np
import pandas as pd

from factorlab.errors import (
    DataQualityError,
    FactorLabError,
    ProviderError,
    ProviderNetworkError,
    SchemaError,
)

from factorlab.data.providers.base import DateLike, MarketDataProvider
from factorlab.data.schema import (
    canonicalize_bars,
    normalize_symbol,
    vendor_code,
)


LOGGER = logging.getLogger(__name__)

_COLUMN_ALIASES: Dict[str, Tuple[str, ...]] = {
    "date": ("date", "日期", "交易日期"),
    "open": ("open", "开盘"),
    "high": ("high", "最高"),
    "low": ("low", "最低"),
    "close": ("close", "收盘"),
    "volume": ("volume", "成交量"),
    "amount": ("amount", "成交额", "成交金额"),
    "turnover": ("turnover", "换手率"),
}


def _timestamp(value: DateLike, label: str) -> pd.Timestamp:
    try:
        result = pd.Timestamp(value)
    except (TypeError, ValueError) as exc:
        raise ProviderError("invalid %s date: %r" % (label, value)) from exc
    if result.tzinfo is not None:
        result = result.tz_localize(None)
    return result.normalize()


def _format_api_date(value: pd.Timestamp) -> str:
    return value.strftime("%Y%m%d")


def _find_column(frame: pd.DataFrame, logical_name: str) -> Optional[str]:
    for candidate in _COLUMN_ALIASES[logical_name]:
        if candidate in frame.columns:
            return candidate
    return None


def _extract_series(
    frame: pd.DataFrame,
    logical_name: str,
    required: bool = True,
) -> pd.Series:
    column = _find_column(frame, logical_name)
    if column is None:
        if required:
            raise SchemaError(
                "AKShare response is missing %s (columns=%s)"
                % (logical_name, list(frame.columns))
            )
        return pd.Series(np.nan, index=frame.index, dtype="float64")
    return frame[column]


def _retryable_http_status(status: Any) -> bool:
    """Retry only transient HTTP responses, not invalid/authenticated requests."""

    return (
        isinstance(status, int)
        and not isinstance(status, bool)
        and (status in {408, 425, 429} or 500 <= status <= 599)
    )


def _is_network_failure(error: Exception) -> bool:
    """Recognize transport failures without treating every SDK/OSError as one.

    The optional HTTP packages are resolved only on an SDK failure so merely
    importing the research namespace does not require AKShare or requests.
    Do not follow arbitrary ``__cause__`` chains: a programming error that
    happens to wrap a connection failure is still a fatal programming error.
    """

    if isinstance(error, urllib.error.HTTPError):
        return _retryable_http_status(error.code)
    if isinstance(error, urllib.error.URLError):
        return isinstance(error.reason, Exception) and _is_network_failure(error.reason)
    if isinstance(error, (
        ConnectionError,
        TimeoutError,
        socket.gaierror,
        socket.herror,
        http.client.IncompleteRead,
        http.client.BadStatusLine,
    )):
        return True
    try:
        requests_errors = importlib.import_module("requests.exceptions")
    except ImportError:
        requests_errors = None
    if requests_errors is not None:
        if isinstance(error, requests_errors.HTTPError):
            response = getattr(error, "response", None)
            return response is not None and _retryable_http_status(
                getattr(response, "status_code", None)
            )
        if isinstance(error, (
            requests_errors.ConnectionError,
            requests_errors.Timeout,
            requests_errors.ChunkedEncodingError,
        )):
            return True
    try:
        urllib3_errors = importlib.import_module("urllib3.exceptions")
    except ImportError:
        return False
    # requests usually translates these; recognize direct SDK usage as well.
    if isinstance(error, urllib3_errors.MaxRetryError):
        reason = getattr(error, "reason", None)
        return isinstance(reason, Exception) and _is_network_failure(reason)
    return isinstance(error, (
        urllib3_errors.TimeoutError,
        urllib3_errors.NewConnectionError,
        urllib3_errors.ProtocolError,
        urllib3_errors.SSLError,
        urllib3_errors.ProxyError,
    ))


def _extract_numeric_series(
    frame: pd.DataFrame, logical_name: str, required: bool = True,
) -> pd.Series:
    values = _extract_series(frame, logical_name, required=required)
    if (
        pd.api.types.is_bool_dtype(values.dtype)
        or pd.api.types.is_datetime64_any_dtype(values.dtype)
        or pd.api.types.is_timedelta64_dtype(values.dtype)
    ):
        raise SchemaError("AKShare %s must contain real numeric values" % logical_name)
    try:
        parsed = pd.to_numeric(values, errors="coerce")
    except (TypeError, ValueError) as exc:
        raise SchemaError("AKShare %s contains an invalid numeric value" % logical_name) from exc
    if (values.notna() & parsed.isna()).any():
        raise SchemaError("AKShare %s contains an invalid numeric value" % logical_name)
    return parsed


class AKShareProvider(MarketDataProvider):
    """Fetch raw and adjusted A-share bars through AKShare.

    Imports AKShare lazily so importing ``factorlab`` never requires the
    optional network dependency.  Tests and offline callers may inject a small
    AKShare-compatible object through ``akshare_module``.

    ``timeout_seconds`` sets native ``stock_zh_a_hist`` connection/read timeouts,
    not a wall-clock deadline for the whole download. The
    current benchmark and constituent SDK endpoints do not expose a native
    timeout; this provider does not change global requests/socket settings or
    claim those endpoints have the same timeout bound.
    """

    def __init__(
        self,
        benchmark_symbol: str = "000905.SH",
        adjustment: str = "hfq",
        retry_count: int = 3,
        backoff_seconds: Sequence[float] = (1.0, 2.0, 4.0),
        request_interval: float = 0.25,
        akshare_module: Optional[Any] = None,
        sleep_fn: Callable[[float], None] = time.sleep,
        monotonic_fn: Callable[[], float] = time.monotonic,
        *,
        timeout_seconds: float = 30.0,
    ) -> None:
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, Real):
            raise ProviderError("timeout_seconds must be a finite positive number")
        try:
            timeout = float(timeout_seconds)
        except (OverflowError, TypeError, ValueError) as exc:
            raise ProviderError("timeout_seconds must be a finite positive number") from exc
        if not math.isfinite(timeout) or timeout <= 0:
            raise ProviderError("timeout_seconds must be a finite positive number")
        if adjustment not in {"hfq", "qfq"}:
            raise ProviderError("adjustment must be 'hfq' or 'qfq'")
        if retry_count < 0:
            raise ProviderError("retry_count must be non-negative")
        if request_interval < 0:
            raise ProviderError("request_interval must be non-negative")
        if not backoff_seconds:
            raise ProviderError("backoff_seconds must contain at least one value")
        if any(float(item) < 0 for item in backoff_seconds):
            raise ProviderError("backoff_seconds values must be non-negative")

        self.benchmark_symbol = normalize_symbol(benchmark_symbol)
        self.adjustment = adjustment
        self.retry_count = int(retry_count)
        self.backoff_seconds = tuple(float(item) for item in backoff_seconds)
        self.request_interval = float(request_interval)
        self.timeout_seconds = timeout
        self._akshare = akshare_module
        self._sleep = sleep_fn
        self._monotonic = monotonic_fn
        self._last_request_at: Optional[float] = None

    @property
    def akshare(self) -> Any:
        if self._akshare is None:
            try:
                self._akshare = importlib.import_module("akshare")
            except ImportError as exc:
                raise ProviderError(
                    "AKShare is not installed; install dev/requirements-research.txt"
                ) from exc
        return self._akshare

    @property
    def source_version(self) -> str:
        return str(getattr(self.akshare, "__version__", "unknown"))

    def cache_source_version(self, symbol: str, is_index: bool) -> str:
        """Return the exact vendor contract that produced a cache artifact."""

        canonical_symbol = normalize_symbol(symbol)
        if not is_index:
            contract = "stock_zh_a_hist-raw+%s-v1" % self.adjustment
        elif (
            canonical_symbol == "000905.SH"
            and hasattr(self.akshare, "stock_zh_index_hist_csindex")
        ):
            contract = "stock_zh_index_hist_csindex-v1"
        elif hasattr(self.akshare, "index_zh_a_hist"):
            contract = "index_zh_a_hist-v1"
        elif hasattr(self.akshare, "stock_zh_index_daily"):
            contract = "stock_zh_index_daily-v1"
        else:
            raise ProviderError("AKShare has no supported mainland index endpoint")
        return "akshare=%s;contract=%s" % (self.source_version, contract)

    def _throttle(self) -> None:
        now = self._monotonic()
        if self._last_request_at is not None:
            remaining = self.request_interval - (now - self._last_request_at)
            if remaining > 0:
                self._sleep(remaining)
                now = self._monotonic()
        self._last_request_at = now

    def _call(self, function: Callable[..., Any], **kwargs: Any) -> pd.DataFrame:
        last_error: Optional[Exception] = None
        for attempt in range(self.retry_count + 1):
            self._throttle()
            try:
                result = function(**kwargs)
            except FactorLabError:
                raise
            except Exception as exc:
                if not _is_network_failure(exc):
                    raise ProviderError(
                        "AKShare SDK failed without a recoverable network error (%s): %s"
                        % (type(exc).__name__, exc)
                    ) from exc
                last_error = exc
                if attempt >= self.retry_count:
                    break
                delay = self.backoff_seconds[
                    min(attempt, len(self.backoff_seconds) - 1)
                ]
                LOGGER.warning(
                    "AKShare request failed (attempt %d/%d): %s; retrying in %.2fs",
                    attempt + 1,
                    self.retry_count + 1,
                    exc,
                    delay,
                )
                if delay:
                    self._sleep(delay)
            else:
                if not isinstance(result, pd.DataFrame):
                    raise SchemaError(
                        "AKShare provider returned %s, expected pandas DataFrame"
                        % type(result).__name__
                    )
                return result
        raise ProviderNetworkError("AKShare request failed after retries") from last_error

    @staticmethod
    def _date_range(start: DateLike, end: DateLike) -> Tuple[pd.Timestamp, pd.Timestamp]:
        start_ts = _timestamp(start, "start")
        end_ts = _timestamp(end, "end")
        if start_ts > end_ts:
            raise ProviderError("start date must be on or before end date")
        return start_ts, end_ts

    @staticmethod
    def _normalise_vendor_frame(
        frame: pd.DataFrame,
        start: pd.Timestamp,
        end: pd.Timestamp,
        require_turnover: bool,
        symbol: str,
        require_amount: bool = True,
    ) -> pd.DataFrame:
        if frame.columns.duplicated().any():
            raise SchemaError("AKShare response contains duplicate columns")
        if len(frame) == 0:
            return pd.DataFrame(
                columns=["date", "open", "high", "low", "close", "volume", "amount", "turnover"]
            )
        result = pd.DataFrame(index=frame.index)
        date_values = _extract_series(frame, "date")
        if pd.api.types.infer_dtype(date_values, skipna=True) in {
            "integer", "floating", "mixed-integer", "mixed-integer-float", "boolean",
        }:
            raise SchemaError("AKShare response contains a numeric/invalid date")
        try:
            dates = pd.to_datetime(date_values, errors="raise", format="mixed")
            result["date"] = dates.dt.tz_localize(None).dt.normalize()
        except (TypeError, ValueError, AttributeError) as exc:
            raise SchemaError("AKShare response contains an invalid date") from exc
        for field in ("open", "high", "low", "close", "volume"):
            result[field] = _extract_numeric_series(frame, field)
        result["amount"] = _extract_numeric_series(
            frame, "amount", required=require_amount,
        )
        result["turnover"] = _extract_numeric_series(
            frame, "turnover", required=require_turnover,
        )
        if result["date"].isna().any():
            raise SchemaError("AKShare response contains an invalid date")
        if result["date"].duplicated().any():
            raise SchemaError("AKShare response contains duplicate dates")
        # Validate before requesting the adjusted endpoint or discarding rows.
        # Missing optional amount/turnover remains NaN; malformed supplied
        # values and invalid OHLC are never silently turned into missing data.
        canonicalize_bars(result, symbol=symbol)
        result = result.loc[result["date"].between(start, end)].copy()
        return result.sort_values("date", kind="mergesort").reset_index(drop=True)

    def get_daily_bars(
        self, symbol: str, start: DateLike, end: DateLike
    ) -> pd.DataFrame:
        canonical_symbol = normalize_symbol(symbol)
        start_ts, end_ts = self._date_range(start, end)
        kwargs = {
            "symbol": vendor_code(canonical_symbol),
            "period": "daily",
            "start_date": _format_api_date(start_ts),
            "end_date": _format_api_date(end_ts),
            "timeout": self.timeout_seconds,
        }
        raw_vendor = self._call(
            self.akshare.stock_zh_a_hist, adjust="", **kwargs
        )
        raw = self._normalise_vendor_frame(
            raw_vendor, start_ts, end_ts, require_turnover=True,
            require_amount=True, symbol=canonical_symbol,
        )
        adjusted_vendor = self._call(
            self.akshare.stock_zh_a_hist, adjust=self.adjustment, **kwargs
        )
        adjusted = self._normalise_vendor_frame(
            adjusted_vendor, start_ts, end_ts, require_turnover=False,
            require_amount=False, symbol=canonical_symbol,
        )
        if raw.empty and adjusted.empty:
            from factorlab.data.schema import empty_bars

            return empty_bars()
        raw_dates = pd.Index(raw["date"])
        adjusted_dates = pd.Index(adjusted["date"])
        if not raw_dates.equals(adjusted_dates):
            raise DataQualityError(
                "raw and %s AKShare bars have different trading dates"
                % self.adjustment
            )

        result = pd.DataFrame(
            {
                "date": raw["date"],
                "symbol": canonical_symbol,
                "open": raw["open"],
                "high": raw["high"],
                "low": raw["low"],
                "close": raw["close"],
                # AKShare stock_zh_a_hist reports volume in hands.
                "volume": raw["volume"] * 100.0,
                "amount": raw["amount"],
                # AKShare reports turnover as percent; internal units are decimal.
                "turnover": raw["turnover"] / 100.0,
                "raw_open": raw["open"],
                "raw_high": raw["high"],
                "raw_low": raw["low"],
                "raw_close": raw["close"],
                "adj_open": adjusted["open"],
                "adj_high": adjusted["high"],
                "adj_low": adjusted["low"],
                "adj_close": adjusted["close"],
            }
        )
        result["adj_factor"] = result["adj_close"] / result["raw_close"].replace(
            0.0, np.nan
        )
        return canonicalize_bars(result, symbol=canonical_symbol)

    def get_index_bars(
        self, symbol: str, start: DateLike, end: DateLike
    ) -> pd.DataFrame:
        canonical_symbol = normalize_symbol(symbol)
        start_ts, end_ts = self._date_range(start, end)
        code = vendor_code(canonical_symbol)

        volume_multiplier = 100.0
        amount_multiplier = 1.0
        require_amount = True
        csindex_official = (
            canonical_symbol == "000905.SH"
            and hasattr(self.akshare, "stock_zh_index_hist_csindex")
        )
        if csindex_official:
            vendor_frame = self._call(
                self.akshare.stock_zh_index_hist_csindex,
                symbol=code,
                start_date=_format_api_date(start_ts),
                end_date=_format_api_date(end_ts),
            )
            # The CSI endpoint documents volume as ten-thousand hands and
            # amount as CNY 100m.  Some endpoint revisions already return
            # volume in shares, so the scale is resolved below from the
            # amount/volume ratio and then validated conservatively.
            volume_multiplier = 1.0
            amount_multiplier = 100_000_000.0
        elif hasattr(self.akshare, "index_zh_a_hist"):
            vendor_frame = self._call(
                self.akshare.index_zh_a_hist,
                symbol=code,
                period="daily",
                start_date=_format_api_date(start_ts),
                end_date=_format_api_date(end_ts),
            )
        elif hasattr(self.akshare, "stock_zh_index_daily"):
            exchange = canonical_symbol.split(".", 1)[1].lower()
            vendor_frame = self._call(
                self.akshare.stock_zh_index_daily,
                symbol="%s%s" % (exchange, code),
            )
            # The Sina fallback reports index volume in shares, whereas the
            # Eastmoney primary reports hands.
            volume_multiplier = 1.0
            require_amount = False
        else:
            raise ProviderError("AKShare has no supported mainland index endpoint")

        normalised = self._normalise_vendor_frame(
            vendor_frame, start_ts, end_ts, require_turnover=False,
            symbol=canonical_symbol, require_amount=require_amount,
        )
        if normalised.empty:
            from factorlab.data.schema import empty_bars

            return empty_bars()
        volume = normalised["volume"] * volume_multiplier
        amount = normalised["amount"] * amount_multiplier
        if csindex_official:
            observed = pd.DataFrame({"volume": volume, "amount": amount}).dropna()
            observed = observed.loc[
                observed["volume"].gt(0.0) & observed["amount"].gt(0.0)
            ]
            if not observed.empty:
                raw_implied_price = float(
                    (observed["amount"] / observed["volume"]).median()
                )
                if raw_implied_price > 100_000.0:
                    volume = volume * 1_000_000.0
                    raw_implied_price /= 1_000_000.0
                if not 0.01 <= raw_implied_price <= 100_000.0:
                    raise DataQualityError(
                        "CSI index volume/amount units cannot be validated"
                    )
        result = pd.DataFrame(
            {
                "date": normalised["date"],
                "symbol": canonical_symbol,
                "open": normalised["open"],
                "high": normalised["high"],
                "low": normalised["low"],
                "close": normalised["close"],
                "volume": volume,
                "amount": amount,
                "turnover": normalised["turnover"],
            }
        )
        for field in ("open", "high", "low", "close"):
            result["raw_%s" % field] = result[field]
            result["adj_%s" % field] = result[field]
        result["adj_factor"] = 1.0
        return canonicalize_bars(
            result, symbol=canonical_symbol, allow_missing_ohlc=False
        )

    def get_trade_calendar(
        self, start: DateLike, end: DateLike
    ) -> pd.DatetimeIndex:
        bars = self.get_index_bars(self.benchmark_symbol, start, end)
        if bars.empty:
            raise DataQualityError(
                "benchmark returned no dates; a trading calendar cannot be built"
            )
        from factorlab.data.calendar import TradingCalendar

        return TradingCalendar.from_benchmark_bars(bars).dates

    def get_index_constituents(
        self, symbol: str, as_of: Optional[DateLike] = None
    ) -> pd.DataFrame:
        if as_of is not None:
            raise ProviderError(
                "AKShare exposes only a current constituent snapshot; "
                "POINT-IN-TIME UNIVERSE REQUIRED for historical dates"
            )
        canonical_index = normalize_symbol(symbol)
        if not hasattr(self.akshare, "index_stock_cons"):
            raise ProviderError("AKShare has no supported constituent endpoint")
        vendor_frame = self._call(
            self.akshare.index_stock_cons, symbol=vendor_code(canonical_index)
        )
        if vendor_frame.columns.duplicated().any():
            raise SchemaError("AKShare constituent response contains duplicate columns")
        symbol_column = next(
            (
                candidate
                for candidate in ("品种代码", "成分券代码", "股票代码", "代码", "symbol")
                if candidate in vendor_frame.columns
            ),
            None,
        )
        if symbol_column is None:
            raise SchemaError("AKShare constituent response has no symbol column")
        output = pd.DataFrame(
            {
                "index_symbol": canonical_index,
                "symbol": vendor_frame[symbol_column].map(normalize_symbol),
                "is_current_snapshot": True,
            }
        )
        return output.drop_duplicates("symbol").sort_values("symbol").reset_index(
            drop=True
        )


__all__ = ["AKShareProvider"]
