"""CSI500 membership diagnostics and strictly imported point-in-time files.

Current constituents are deliberately never used as a historical fallback.
BaoStock query-date snapshots are diagnostic only until effective membership
semantics can be independently established.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, time as datetime_time, timezone
import hashlib
import io
import json
import logging
from numbers import Real
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

import pandas as pd

from factorlab.errors import PointInTimeUniverseError, ProviderError

from .base import PointInTimeUniverse, PointInTimeUniverseProvider

LOGGER = logging.getLogger(__name__)
CANONICAL_SYMBOL = re.compile(r"^(?:60|68|00|30|83|87|43)\d{4}\.(?:SH|SZ|BJ)$")
PIT_REQUIRED = "POINT-IN-TIME UNIVERSE REQUIRED"
# v2 checks server query-date evidence, named code fields and terminal SDK
# error codes. It proves neither effective membership dates nor successful
# pagination completion when the SDK silently stops after an empty response.
BAOSTOCK_PIT_SOURCE_VERSION = "baostock-query_zz500_stocks-daily-v2"
BAOSTOCK_PIT_UNVERIFIED_REASON = (
    PIT_REQUIRED + ": EFFECTIVE MEMBERSHIP SEMANTICS UNVERIFIED"
)
PIT_GENERATION_SCHEMA_VERSION = 3
PIT_DAILY_ALIAS = "csi500_daily.parquet"
PIT_INTERVALS_ALIAS = "csi500_intervals.parquet"


def canonical_symbol(value: Any) -> str:
    """Convert BaoStock/Chinese exchange symbols to the project convention."""

    raw = str(value).strip().upper()
    if CANONICAL_SYMBOL.match(raw):
        return raw
    if raw.startswith(("SH.", "SZ.", "BJ.")):
        exchange, code = raw.split(".", 1)
        result = "%s.%s" % (code, exchange)
    elif raw.endswith((".SH", ".SZ", ".BJ")):
        result = raw
    elif raw.isdigit() and len(raw) == 6:
        if raw.startswith(("6", "9")):
            exchange = "SH"
        elif raw.startswith(("4", "8")):
            exchange = "BJ"
        else:
            exchange = "SZ"
        result = "%s.%s" % (raw, exchange)
    else:
        raise PointInTimeUniverseError(
            "%s: invalid constituent symbol %r" % (PIT_REQUIRED, value)
        )
    if not CANONICAL_SYMBOL.match(result):
        raise PointInTimeUniverseError(
            "%s: invalid canonical symbol %r" % (PIT_REQUIRED, result)
        )
    return result


def _normalise_dates(values: Iterable[Any]) -> pd.DatetimeIndex:
    dates = pd.to_datetime(list(values), errors="raise")
    if getattr(dates, "tz", None) is not None:
        dates = dates.tz_localize(None)
    return pd.DatetimeIndex(dates).normalize()


def _membership_flag(value: Any) -> bool:
    """Parse an explicit membership flag without truthiness coercion.

    In particular, ``bool("false")`` and ``bool("0")`` are both true in
    Python.  Accepting those values silently would add non-members to a PIT
    universe, so imported flags are restricted to unambiguous encodings.
    """

    if value is True or value is False:
        return bool(value)
    if pd.isna(value):
        raise PointInTimeUniverseError(
            "%s: is_member must not be null" % PIT_REQUIRED
        )
    if isinstance(value, Real) and float(value) in {0.0, 1.0}:
        return bool(value)
    text = str(value).strip().lower()
    if text in {"true", "1"}:
        return True
    if text in {"false", "0"}:
        return False
    raise PointInTimeUniverseError(
        "%s: invalid is_member value %r" % (PIT_REQUIRED, value)
    )


def validate_daily_membership(
    frame: pd.DataFrame,
    requested_dates: Optional[Iterable[Any]] = None,
    expected_member_count: Optional[int] = 500,
) -> pd.DataFrame:
    """Validate exact daily PIT snapshots and return a canonical copy."""

    required = {"date", "symbol"}
    missing = required.difference(frame.columns)
    if missing:
        raise PointInTimeUniverseError(
            "%s: missing columns %s" % (PIT_REQUIRED, sorted(missing))
        )
    result = frame.copy()
    result["date"] = pd.to_datetime(result["date"], errors="raise").dt.tz_localize(None).dt.normalize()
    result["symbol"] = result["symbol"].map(canonical_symbol)
    if "is_member" not in result:
        result["is_member"] = True
    result["is_member"] = result["is_member"].map(_membership_flag).astype(bool)
    result = result.loc[result["is_member"], ["date", "symbol", "is_member"]]
    if result.duplicated(["date", "symbol"]).any():
        dupes = result.loc[result.duplicated(["date", "symbol"], keep=False), ["date", "symbol"]]
        raise PointInTimeUniverseError(
            "%s: duplicate memberships: %s" % (PIT_REQUIRED, dupes.head().to_dict("records"))
        )
    if requested_dates is not None:
        expected_dates = _normalise_dates(requested_dates).unique().sort_values()
        actual_dates = pd.DatetimeIndex(result["date"].unique()).sort_values()
        missing_dates = expected_dates.difference(actual_dates)
        extra_dates = actual_dates.difference(expected_dates)
        if len(missing_dates) or len(extra_dates):
            raise PointInTimeUniverseError(
                "%s: snapshot coverage mismatch; missing=%s extra=%s"
                % (
                    PIT_REQUIRED,
                    [d.strftime("%Y-%m-%d") for d in missing_dates[:10]],
                    [d.strftime("%Y-%m-%d") for d in extra_dates[:10]],
                )
            )
    if expected_member_count is not None:
        counts = result.groupby("date", sort=True)["symbol"].nunique()
        invalid = counts[counts != int(expected_member_count)]
        if not invalid.empty:
            details = {d.strftime("%Y-%m-%d"): int(v) for d, v in invalid.head(10).items()}
            raise PointInTimeUniverseError(
                "%s: expected %d unique members per date, got %s"
                % (PIT_REQUIRED, expected_member_count, details)
            )
    return result.sort_values(["date", "symbol"], kind="mergesort").reset_index(drop=True)


def compress_membership_intervals(daily: pd.DataFrame) -> pd.DataFrame:
    """Compress consecutive observed membership dates into inclusive intervals.

    Consecutive means consecutive dates in the supplied market calendar, not
    consecutive civil days. Re-entry creates another interval for the symbol.
    """

    clean = validate_daily_membership(daily, expected_member_count=None)
    calendar = pd.DatetimeIndex(clean["date"].drop_duplicates().sort_values())
    date_position = pd.Series(range(len(calendar)), index=calendar)
    rows: List[Dict[str, Any]] = []
    for symbol, group in clean.groupby("symbol", sort=True):
        dates = pd.DatetimeIndex(group["date"].sort_values().unique())
        positions = date_position.loc[dates].to_numpy()
        starts = [0]
        starts.extend(((positions[1:] - positions[:-1] != 1).nonzero()[0] + 1).tolist())
        ends = [value - 1 for value in starts[1:]] + [len(dates) - 1]
        for interval_number, (start_idx, end_idx) in enumerate(zip(starts, ends), start=1):
            rows.append(
                {
                    "symbol": symbol,
                    "entry_date": dates[start_idx],
                    "exit_date": dates[end_idx],
                    "interval": interval_number,
                }
            )
    columns = ["symbol", "entry_date", "exit_date", "interval"]
    return pd.DataFrame(rows, columns=columns).sort_values(
        ["symbol", "entry_date"], kind="mergesort"
    ).reset_index(drop=True)


def strict_import_date(value: Any, name: str) -> pd.Timestamp:
    """Parse a local trading date without numeric or timezone coercion."""

    if value is None or value is pd.NaT or value is pd.NA or (
        isinstance(value, float) and pd.isna(value)
    ):
        raise PointInTimeUniverseError(
            "%s: %s must not be missing" % (PIT_REQUIRED, name)
        )
    if isinstance(value, (bool, Real)) and not isinstance(value, (date, datetime)):
        raise PointInTimeUniverseError(
            "%s: %s must not be a numeric timestamp" % (PIT_REQUIRED, name)
        )
    if isinstance(value, str):
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise PointInTimeUniverseError(
                "%s: %s must use canonical YYYY-MM-DD form"
                % (PIT_REQUIRED, name)
            )
        try:
            return pd.Timestamp(date.fromisoformat(value))
        except ValueError as exc:
            raise PointInTimeUniverseError(
                "%s: %s is not a valid calendar date" % (PIT_REQUIRED, name)
            ) from exc
    # ``pandas.Timestamp`` subclasses ``datetime``.  Handle it first so
    # nanosecond precision cannot slip through ``datetime.time()`` unnoticed.
    if isinstance(value, pd.Timestamp):
        if pd.isna(value):
            raise PointInTimeUniverseError(
                "%s: %s must not be missing" % (PIT_REQUIRED, name)
            )
        if value.tzinfo is not None:
            raise PointInTimeUniverseError(
                "%s: %s must be timezone-naive" % (PIT_REQUIRED, name)
            )
        if value != value.normalize():
            raise PointInTimeUniverseError(
                "%s: %s must not contain a time-of-day" % (PIT_REQUIRED, name)
            )
        return value.normalize()
    if isinstance(value, datetime):
        if value.tzinfo is not None or value.utcoffset() is not None:
            raise PointInTimeUniverseError(
                "%s: %s must be timezone-naive" % (PIT_REQUIRED, name)
            )
        if value.time() != datetime_time.min:
            raise PointInTimeUniverseError(
                "%s: %s must not contain a time-of-day" % (PIT_REQUIRED, name)
            )
        return pd.Timestamp(value)
    if isinstance(value, date):
        return pd.Timestamp(value)
    raise PointInTimeUniverseError(
        "%s: %s must be a canonical trading date" % (PIT_REQUIRED, name)
    )


def expand_membership_intervals(
    intervals: pd.DataFrame,
    calendar: Iterable[Any],
    expected_member_count: Optional[int] = 500,
) -> pd.DataFrame:
    """Expand inclusive membership intervals over an exact trading calendar.

    Input rows mean ``entry_date <= trading_date <= exit_date``.  Both bounds
    are mandatory effective dates.  No inference is made from announcement
    dates, adjustment-base dates, or an ``opt-out`` convention; callers must
    convert official source material to these explicit semantics first.
    """

    required = {"symbol", "entry_date", "exit_date"}
    missing = sorted(required.difference(intervals.columns))
    if missing:
        raise PointInTimeUniverseError(
            "%s: interval import missing columns %s" % (PIT_REQUIRED, missing)
        )
    if intervals.empty:
        raise PointInTimeUniverseError("%s: interval import is empty" % PIT_REQUIRED)
    result = intervals.copy()
    result["symbol"] = result["symbol"].map(canonical_symbol)
    if "is_member" in result.columns:
        flags = result["is_member"].map(_membership_flag)
        if not flags.all():
            raise PointInTimeUniverseError(
                "%s: interval rows must all represent active membership"
                % PIT_REQUIRED
            )
    result["entry_date"] = pd.Series(
        [
            strict_import_date(value, "entry_date")
            for value in result["entry_date"].tolist()
        ],
        index=result.index,
        dtype="datetime64[ns]",
    )
    result["exit_date"] = pd.Series(
        [
            strict_import_date(value, "exit_date")
            for value in result["exit_date"].tolist()
        ],
        index=result.index,
        dtype="datetime64[ns]",
    )
    invalid_order = result["exit_date"].notna() & (
        result["exit_date"] < result["entry_date"]
    )
    if invalid_order.any():
        raise PointInTimeUniverseError(
            "%s: exit_date must be on or after entry_date" % PIT_REQUIRED
        )
    if result.duplicated(["symbol", "entry_date", "exit_date"]).any():
        raise PointInTimeUniverseError(
            "%s: duplicate membership intervals" % PIT_REQUIRED
        )
    ordered = result.sort_values(
        ["symbol", "entry_date", "exit_date"], kind="mergesort"
    )
    previous_exit = ordered.groupby("symbol", sort=False)["exit_date"].shift()
    overlaps = previous_exit.notna() & ordered["entry_date"].le(previous_exit)
    if overlaps.any():
        examples = ordered.loc[
            overlaps, ["symbol", "entry_date", "exit_date"]
        ].head(5)
        raise PointInTimeUniverseError(
            "%s: overlapping inclusive intervals: %s"
            % (PIT_REQUIRED, examples.to_dict("records"))
        )

    dates = _normalise_dates(calendar).unique().sort_values()
    if dates.empty:
        raise PointInTimeUniverseError(
            "%s: requested trading calendar is empty" % PIT_REQUIRED
        )
    calendar_values = set(dates)
    for column in ("entry_date", "exit_date"):
        boundary = ordered[column].dropna()
        in_research_span = boundary.between(dates.min(), dates.max())
        invalid = boundary.loc[in_research_span & ~boundary.isin(calendar_values)]
        if not invalid.empty:
            raise PointInTimeUniverseError(
                "%s: %s inside the requested span must be a benchmark "
                "trading date: %s"
                % (
                    PIT_REQUIRED,
                    column,
                    [value.strftime("%Y-%m-%d") for value in invalid.head(5)],
                )
            )
    date_values = dates.to_numpy(dtype="datetime64[ns]")
    entry_values = ordered["entry_date"].to_numpy(dtype="datetime64[ns]")
    exit_values = ordered["exit_date"].to_numpy(dtype="datetime64[ns]")
    active = entry_values[:, None] <= date_values[None, :]
    active &= date_values[None, :] <= exit_values[:, None]
    interval_positions, date_positions = active.nonzero()
    daily = pd.DataFrame(
        {
            "date": dates.take(date_positions),
            "symbol": ordered["symbol"].to_numpy()[interval_positions],
            "is_member": True,
        }
    )
    return validate_daily_membership(daily, dates, expected_member_count)


class DataFramePointInTimeUniverse(PointInTimeUniverse):
    """Exact-date universe backed by validated daily snapshots."""

    def __init__(self, daily: pd.DataFrame, version: Optional[str] = None) -> None:
        self._daily = validate_daily_membership(daily, expected_member_count=None)
        self._version = version or dataframe_sha256(self._daily)

    def get_members(self, date: pd.Timestamp) -> List[str]:
        as_of = pd.Timestamp(date)
        if as_of.tzinfo is not None:
            as_of = as_of.tz_localize(None)
        as_of = as_of.normalize()
        selected = self._daily.loc[self._daily["date"] == as_of, "symbol"]
        if selected.empty:
            raise PointInTimeUniverseError(
                "%s: no snapshot for %s" % (PIT_REQUIRED, as_of.strftime("%Y-%m-%d"))
            )
        return selected.sort_values().tolist()

    def version(self) -> Optional[str]:
        return self._version

    @property
    def daily(self) -> pd.DataFrame:
        return self._daily.copy()


class BaoStockPointInTimeProvider(PointInTimeUniverseProvider):
    """Date-aware BaoStock diagnostics, not independently verified PIT data.

    The endpoint's response query date and periodically updated constituent
    records do not establish actual index-adjustment effective dates. Keep the
    diagnostic interface, but never publish its snapshots as a formal universe.
    """

    SUPPORTED_INDEXES = {"000905.SH", "CSI500", "ZZ500"}

    def __init__(
        self,
        client: Any = None,
        retry_count: int = 3,
        backoff_seconds: Sequence[float] = (1.0, 2.0, 4.0),
        request_interval: float = 0.25,
        sleep_fn: Callable[[float], None] = time.sleep,
        monotonic_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        if retry_count < 0:
            raise ProviderError("retry_count must be non-negative")
        if retry_count and not backoff_seconds:
            raise ProviderError("backoff_seconds must not be empty when retries are enabled")
        if any(float(value) < 0 for value in backoff_seconds):
            raise ProviderError("backoff_seconds values must be non-negative")
        if request_interval < 0:
            raise ProviderError("request_interval must be non-negative")
        self._client = client
        self._owns_session = False
        self.retry_count = int(retry_count)
        self.backoff_seconds = tuple(float(value) for value in backoff_seconds)
        self.request_interval = float(request_interval)
        self._sleep = sleep_fn
        self._monotonic = monotonic_fn
        self._last_request_at: Optional[float] = None

    def __enter__(self) -> "BaoStockPointInTimeProvider":
        client = self._get_client()

        def login() -> Any:
            result = client.login()
            if getattr(result, "error_code", "0") != "0":
                raise ProviderError(
                    "BaoStock login failed: %s"
                    % getattr(result, "error_msg", "unknown")
                )
            return result

        self._with_retry(login, "login")
        self._owns_session = True
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if self._owns_session:
            self._client.logout()
            self._owns_session = False

    def _get_client(self) -> Any:
        if self._client is None:
            try:
                import baostock as bs
            except ImportError as exc:
                raise ProviderError(
                    "BaoStock is required for PIT CSI500 data; install dev/requirements-research.txt"
                ) from exc
            self._client = bs
        return self._client

    def _throttle(self) -> None:
        now = self._monotonic()
        if self._last_request_at is not None:
            remaining = self.request_interval - (now - self._last_request_at)
            if remaining > 0:
                self._sleep(remaining)
                now = self._monotonic()
        self._last_request_at = now

    def _with_retry(self, operation: Callable[[], Any], description: str) -> Any:
        last_error: Optional[BaseException] = None
        for attempt in range(self.retry_count + 1):
            self._throttle()
            try:
                return operation()
            except Exception as exc:  # BaoStock exposes several exception types
                last_error = exc
                if attempt >= self.retry_count:
                    break
                delay = self.backoff_seconds[
                    min(attempt, len(self.backoff_seconds) - 1)
                ]
                LOGGER.warning(
                    "BaoStock %s failed (attempt %d/%d): %s; retrying in %.2fs",
                    description,
                    attempt + 1,
                    self.retry_count + 1,
                    exc,
                    delay,
                )
                if delay:
                    self._sleep(delay)
        raise ProviderError(
            "BaoStock %s failed after %d attempt(s): %s"
            % (description, self.retry_count + 1, last_error)
        ) from last_error

    def _query_rows(
        self,
        method_name: str,
        description: str,
        *args: Any,
        required_fields: Sequence[str] = (),
        require_rows: bool = True,
        expected_response_date: Optional[str] = None,
        **kwargs: Any
    ) -> Tuple[List[str], List[List[str]]]:
        """Run and fully consume one BaoStock result inside the retry boundary."""

        client = self._get_client()

        def operation() -> Tuple[List[str], List[List[str]]]:
            query = getattr(client, method_name)(*args, **kwargs)
            if getattr(query, "error_code", "0") != "0":
                raise ProviderError(
                    "%s: %s"
                    % (description, getattr(query, "error_msg", "unknown error"))
                )
            if expected_response_date is not None:
                response_date = str(getattr(query, "date", "")).strip()
                if response_date != expected_response_date:
                    raise ProviderError(
                        "%s response date mismatch: expected %s, got %s"
                        % (
                            description,
                            expected_response_date,
                            response_date or "<missing>",
                        )
                    )
            fields = list(getattr(query, "fields", []))
            missing = sorted(set(required_fields).difference(fields))
            if missing:
                raise ProviderError(
                    "%s response missing fields %s" % (description, missing)
                )
            rows: List[List[str]] = []
            while query.next():
                rows.append(query.get_row_data())
            if getattr(query, "error_code", "0") != "0":
                raise ProviderError(
                    "%s iteration: %s"
                    % (description, getattr(query, "error_msg", "unknown error"))
                )
            if require_rows and not rows:
                raise ProviderError("%s returned no rows" % description)
            return fields, rows

        return self._with_retry(operation, description)

    def get_constituents(
        self, index_symbol: str, date: pd.Timestamp
    ) -> pd.DataFrame:
        if str(index_symbol).upper() not in self.SUPPORTED_INDEXES:
            raise ProviderError("BaoStock provider only supports CSI500/000905.SH")
        as_of = pd.Timestamp(date).strftime("%Y-%m-%d")
        description = "BaoStock CSI500 query for %s" % as_of
        fields, rows = self._query_rows(
            "query_zz500_stocks",
            description,
            required_fields=("code",),
            expected_response_date=as_of,
            date=as_of,
        )
        data = pd.DataFrame(rows, columns=fields)
        try:
            symbols = data["code"].map(canonical_symbol)
        except PointInTimeUniverseError as exc:
            raise ProviderError(
                "%s returned an invalid constituent code: %s"
                % (description, exc)
            ) from exc
        duplicated = symbols.duplicated(keep=False)
        if duplicated.any():
            raise ProviderError(
                "%s returned duplicate constituent codes %s"
                % (description, sorted(symbols.loc[duplicated].unique().tolist()))
            )
        result = pd.DataFrame(
            {
                "date": pd.Timestamp(as_of),
                "symbol": symbols,
                "is_member": True,
            }
        )
        result = result.sort_values("symbol").reset_index(drop=True)
        result.attrs["baostock_evidence"] = {
            "response_query_date": as_of,
            "reported_update_dates": (
                tuple(sorted(set(data["updateDate"].astype(str))))
                if "updateDate" in data.columns else None
            ),
            "membership_semantics": "unverified-query-date-snapshot",
        }
        return result

    def get_security_history(
        self, symbols: Sequence[str], start: Any, end: Any
    ) -> Tuple[pd.DataFrame, pd.DataFrame, List[str]]:
        """Download daily ST/trading status and point-in-time listing dates."""

        status_parts: List[pd.DataFrame] = []
        listing_rows: List[Dict[str, Any]] = []
        failures: List[str] = []
        for symbol in sorted(set(symbols)):
            bs_symbol = "%s.%s" % (symbol[-2:].lower(), symbol[:6])
            try:
                basic_fields, basic_rows = self._query_rows(
                    "query_stock_basic",
                    "stock basic %s" % symbol,
                    code=bs_symbol,
                    required_fields=("code", "ipoDate"),
                )
                basic_data = pd.DataFrame(basic_rows, columns=basic_fields)
                if len(basic_data) != 1:
                    raise ProviderError(
                        "stock basic %s returned %d rows"
                        % (symbol, len(basic_data))
                    )
                basic_symbols = {
                    canonical_symbol(value) for value in basic_data["code"]
                }
                if basic_symbols != {symbol}:
                    raise ProviderError(
                        "stock basic %s returned mismatched code(s) %s"
                        % (symbol, sorted(basic_symbols))
                    )
                listing_date = strict_import_date(
                    basic_data.iloc[0]["ipoDate"], "ipoDate"
                )
                history_fields, rows = self._query_rows(
                    "query_history_k_data_plus",
                    "security status %s" % symbol,
                    bs_symbol,
                    "date,code,tradestatus,isST",
                    start_date=pd.Timestamp(start).strftime("%Y-%m-%d"),
                    end_date=pd.Timestamp(end).strftime("%Y-%m-%d"),
                    frequency="d",
                    adjustflag="3",
                    required_fields=("date", "code", "tradestatus", "isST"),
                )
                part = pd.DataFrame(rows, columns=history_fields)
                history_symbols = {
                    canonical_symbol(value) for value in part["code"]
                }
                if history_symbols != {symbol}:
                    raise ProviderError(
                        "security status %s returned mismatched code(s) %s"
                        % (symbol, sorted(history_symbols))
                    )
                history_dates = [
                    strict_import_date(value, "date")
                    for value in part["date"].tolist()
                ]
                status_parts.append(
                    pd.DataFrame(
                        {
                            "date": history_dates,
                            "symbol": symbol,
                            "trade_status": pd.to_numeric(
                                part["tradestatus"], errors="coerce"
                            ),
                            "is_st": pd.to_numeric(
                                part["isST"], errors="coerce"
                            ).astype("boolean"),
                        }
                    )
                )
                listing_rows.append(
                    {"symbol": symbol, "listing_date": listing_date}
                )
            except Exception as exc:  # one security must not abort a batch
                LOGGER.warning("BaoStock status failed for %s: %s", symbol, exc)
                failures.append(symbol)
        status = pd.concat(status_parts, ignore_index=True) if status_parts else pd.DataFrame(
            columns=["date", "symbol", "trade_status", "is_st"]
        )
        listings = pd.DataFrame(listing_rows, columns=["symbol", "listing_date"])
        return status, listings, failures


def dataframe_sha256(frame: pd.DataFrame) -> str:
    """Stable content hash independent of Parquet metadata."""

    ordered = frame.sort_values(list(frame.columns), kind="mergesort").reset_index(drop=True)
    values = ordered.to_csv(index=False, date_format="%Y-%m-%d", lineterminator="\n")
    return hashlib.sha256(values.encode("utf-8")).hexdigest()


def _atomic_parquet(frame: pd.DataFrame, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(
        ".%s.tmp-%s" % (destination.name, uuid.uuid4().hex)
    )
    try:
        frame.to_parquet(temporary, index=False)
        os.replace(str(temporary), str(destination))
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _atomic_json(payload: Dict[str, Any], destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(
        ".%s.tmp-%s" % (destination.name, uuid.uuid4().hex)
    )
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(str(temporary), str(destination))
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _publish_pit_generation(
    daily: pd.DataFrame,
    intervals: pd.DataFrame,
    manifest: Dict[str, Any],
    output_dir: Path,
) -> Dict[str, Any]:
    """Publish immutable artifacts and atomically switch the manifest pointer.

    Canonical names are convenience aliases.  An interrupted replacement of a
    generation bundle cannot modify either artifact referenced by its previous
    manifest.  Alias refreshes happen only after the manifest is committed, so
    failed legacy upgrades also leave the old alias-bound bundle readable.
    """

    output = Path(output_dir)
    generation_id = uuid.uuid4().hex
    daily_filename = "csi500_daily.%s.parquet" % generation_id
    intervals_filename = "csi500_intervals.%s.parquet" % generation_id
    published = {
        **manifest,
        "schema_version": PIT_GENERATION_SCHEMA_VERSION,
        "generation_id": generation_id,
        "daily_file": daily_filename,
        "intervals_file": intervals_filename,
        "daily_alias": PIT_DAILY_ALIAS,
        "intervals_alias": PIT_INTERVALS_ALIAS,
    }
    manifest_path = output / "manifest.json"
    try:
        with _universe_write_lock(output):
            daily_path = output / daily_filename
            intervals_path = output / intervals_filename
            if daily_path.exists() or intervals_path.exists() or daily_path.is_symlink() or intervals_path.is_symlink():
                raise PointInTimeUniverseError("%s: new PIT generation already exists" % PIT_REQUIRED)
            _atomic_parquet(daily, daily_path)
            _atomic_parquet(intervals, intervals_path)
            published["daily_file_sha256"] = hashlib.sha256(
                daily_path.read_bytes()
            ).hexdigest()
            published["intervals_file_sha256"] = hashlib.sha256(
                intervals_path.read_bytes()
            ).hexdigest()
            _atomic_json(published, manifest_path)
            # The verified generation is already committed.  Failure to
            # refresh a convenience alias must not turn that commit into
            # an apparent failure or make a caller retry the publication.
            for frame, alias in (
                (daily, PIT_DAILY_ALIAS), (intervals, PIT_INTERVALS_ALIAS)
            ):
                try:
                    _atomic_parquet(frame, output / alias)
                except Exception as exc:
                    LOGGER.warning("PIT generation committed; alias %s not refreshed: %s", alias, exc)
    except PointInTimeUniverseError:
        raise
    except Exception as exc:
        raise PointInTimeUniverseError(
            "%s: cannot publish PIT generation: %s: %s"
            % (PIT_REQUIRED, type(exc).__name__, exc)
        ) from exc
    return published


@contextmanager
def _universe_write_lock(output_dir: Path) -> Iterator[Path]:
    """Serialize publication of the three PIT universe artifacts.

    An abandoned lock is deliberately not deleted automatically: guessing that
    a writer is dead could let two generations interleave.  The error names the
    exact lock so recovery remains an explicit operator action.
    """

    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    lock_path = output / ".universe-write.lock"
    descriptor: Optional[int] = None
    owned_identity: Optional[Tuple[int, int]] = None
    token = uuid.uuid4().hex
    payload = (
        "token=%s pid=%d generated_at=%s\n"
        % (token, os.getpid(), datetime.now(timezone.utc).isoformat())
    ).encode("ascii")
    try:
        descriptor = os.open(
            str(lock_path),
            os.O_CREAT
            | os.O_EXCL
            | os.O_WRONLY
            | getattr(os, "O_BINARY", 0),
        )
        os.write(descriptor, payload)
        # Use the same path-based stat API here and during cleanup.  On some
        # Windows/Python builds ``fstat`` and path ``stat`` expose different
        # inode representations for the same file.
        lock_stat = os.stat(str(lock_path), follow_symlinks=False)
        owned_identity = (lock_stat.st_dev, lock_stat.st_ino)
    except FileExistsError as exc:
        raise PointInTimeUniverseError(
            "%s: universe writer lock already exists: %s"
            % (PIT_REQUIRED, lock_path)
        ) from exc
    except Exception:
        if descriptor is not None:
            os.close(descriptor)
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass
        raise
    try:
        yield lock_path
    finally:
        if descriptor is not None:
            os.close(descriptor)
            descriptor = None
        try:
            current_stat = os.stat(str(lock_path), follow_symlinks=False)
            current_identity = (current_stat.st_dev, current_stat.st_ino)
            if (
                current_identity == owned_identity
                and lock_path.read_bytes() == payload
            ):
                lock_path.unlink()
        except (FileNotFoundError, OSError):
            pass


def _snapshot_metadata(
    provider: PointInTimeUniverseProvider,
    index_symbol: str,
    date: pd.Timestamp,
    expected_member_count: int,
    snapshot: pd.DataFrame,
) -> Dict[str, Any]:
    return {
        "schema_version": 1,
        "source": provider.__class__.__name__,
        "source_version": BAOSTOCK_PIT_SOURCE_VERSION,
        "index_symbol": index_symbol,
        "date": pd.Timestamp(date).strftime("%Y-%m-%d"),
        "expected_member_count": int(expected_member_count),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "sha256": dataframe_sha256(snapshot),
    }


def _load_cached_snapshot(
    snapshot_path: Path,
    metadata_path: Path,
    provider: PointInTimeUniverseProvider,
    index_symbol: str,
    date: pd.Timestamp,
    expected_member_count: int,
) -> pd.DataFrame:
    """Load only a snapshot whose source contract and content are auditable."""

    if not snapshot_path.exists() or not metadata_path.exists():
        raise PointInTimeUniverseError("cached snapshot provenance is missing")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    if not isinstance(metadata, dict):
        raise PointInTimeUniverseError("cached snapshot metadata is not a mapping")
    expected = {
        "schema_version": 1,
        "source": provider.__class__.__name__,
        "source_version": BAOSTOCK_PIT_SOURCE_VERSION,
        "index_symbol": index_symbol,
        "date": pd.Timestamp(date).strftime("%Y-%m-%d"),
        "expected_member_count": int(expected_member_count),
    }
    mismatches = [
        name for name, value in expected.items() if metadata.get(name) != value
    ]
    if mismatches:
        raise PointInTimeUniverseError(
            "cached snapshot metadata mismatch: %s" % ", ".join(mismatches)
        )
    snapshot = validate_daily_membership(
        pd.read_parquet(snapshot_path), [date], expected_member_count
    )
    digest = metadata.get("sha256")
    if (
        not isinstance(digest, str)
        or len(digest) != 64
        or any(character not in "0123456789abcdefABCDEF" for character in digest)
        or digest.lower() != dataframe_sha256(snapshot)
    ):
        raise PointInTimeUniverseError("cached snapshot content hash mismatch")
    return snapshot


def collect_point_in_time_universe(
    provider: PointInTimeUniverseProvider,
    index_symbol: str,
    calendar: Iterable[Any],
    output_dir: Path,
    expected_member_count: int = 500,
    retries: int = 3,
    backoff_seconds: Sequence[float] = (1.0, 2.0, 4.0),
    request_interval_seconds: float = 0.25,
) -> Dict[str, Any]:
    """Collect resumable daily snapshots and materialise validated PIT files.

    Individual failures are recorded and collection continues. Materialisation
    fails after the loop unless every requested market date is valid.

    BaoStock's periodic source updates have not been proved to reconstruct
    exact effective membership. Its instances and subclasses are rejected
    before queries, filesystem access or publication, even with old caches.
    """

    if isinstance(provider, BaoStockPointInTimeProvider):
        raise PointInTimeUniverseError(BAOSTOCK_PIT_UNVERIFIED_REASON)
    output = Path(output_dir)
    if isinstance(retries, bool) or not isinstance(retries, int) or retries < 0:
        raise PointInTimeUniverseError("retries must be a non-negative integer")
    if retries and not backoff_seconds:
        raise PointInTimeUniverseError(
            "backoff_seconds must not be empty when retries are enabled"
        )
    snapshots_dir = output / "snapshots"
    snapshots_dir.mkdir(parents=True, exist_ok=True)
    dates = _normalise_dates(calendar).unique().sort_values()
    failures: Dict[str, str] = {}
    parts: List[pd.DataFrame] = []
    for number, date in enumerate(dates):
        snapshot_path = snapshots_dir / (date.strftime("%Y-%m-%d") + ".parquet")
        metadata_path = snapshots_dir / (date.strftime("%Y-%m-%d") + ".json")
        snapshot: Optional[pd.DataFrame] = None
        if snapshot_path.exists() or metadata_path.exists():
            try:
                snapshot = _load_cached_snapshot(
                    snapshot_path,
                    metadata_path,
                    provider,
                    index_symbol,
                    date,
                    expected_member_count,
                )
            except Exception as exc:
                LOGGER.warning("Ignoring invalid cached PIT snapshot %s: %s", snapshot_path, exc)
        if snapshot is None:
            last_error: Optional[Exception] = None
            for attempt in range(retries + 1):
                try:
                    candidate = provider.get_constituents(index_symbol, date)
                    validated_candidate = validate_daily_membership(
                        candidate, [date], expected_member_count
                    )
                    _atomic_parquet(validated_candidate, snapshot_path)
                    _atomic_json(
                        _snapshot_metadata(
                            provider,
                            index_symbol,
                            date,
                            expected_member_count,
                            validated_candidate,
                        ),
                        metadata_path,
                    )
                    snapshot = validated_candidate
                    break
                except Exception as exc:
                    last_error = exc
                    if attempt < retries:
                        delay = backoff_seconds[min(attempt, len(backoff_seconds) - 1)] if backoff_seconds else 0
                        time.sleep(max(0.0, float(delay)))
            if snapshot is None:
                failures[date.strftime("%Y-%m-%d")] = str(last_error)
                LOGGER.error("PIT snapshot failed for %s: %s", date.date(), last_error)
            if request_interval_seconds > 0 and number + 1 < len(dates):
                time.sleep(request_interval_seconds)
        if snapshot is not None:
            parts.append(snapshot)
    if failures or len(parts) != len(dates):
        failure_path = output / "failures.json"
        _atomic_json({"failed_dates": failures}, failure_path)
        raise PointInTimeUniverseError(
            "%s: %d/%d daily snapshots unavailable; see %s"
            % (PIT_REQUIRED, len(failures), len(dates), failure_path)
        )
    daily = validate_daily_membership(
        pd.concat(parts, ignore_index=True), dates, expected_member_count
    )
    intervals = compress_membership_intervals(daily)
    content_hash = dataframe_sha256(daily)
    intervals_hash = dataframe_sha256(intervals)
    manifest = {
        "schema_version": 1,
        "source": provider.__class__.__name__,
        "index_symbol": index_symbol,
        "source_version": BAOSTOCK_PIT_SOURCE_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "start": dates.min().strftime("%Y-%m-%d"),
        "end": dates.max().strftime("%Y-%m-%d"),
        "trading_dates": int(len(dates)),
        "expected_member_count": int(expected_member_count),
        "sha256": content_hash,
        "daily_sha256": content_hash,
        "intervals_sha256": intervals_hash,
    }
    return _publish_pit_generation(daily, intervals, manifest, output)


def import_point_in_time_universe(
    source: Path,
    output_dir: Path,
    calendar: Iterable[Any],
    expected_member_count: int = 500,
    index_symbol: str = "000905.SH",
) -> Dict[str, Any]:
    """Validate and materialise a user-supplied PIT membership artifact.

    Accepted layouts are exact daily snapshots (``date,symbol,is_member``) or
    explicit inclusive intervals (``symbol,entry_date,exit_date``).  Interval
    endpoints must already be effective trading dates.  Ambiguous announcement
    or ``opt-in``/``opt-out`` conventions are deliberately not inferred.
    """

    path = Path(source)
    try:
        source_bytes = path.read_bytes()
    except OSError as exc:
        raise PointInTimeUniverseError(
            "%s: cannot read import file %s: %s" % (PIT_REQUIRED, path, exc)
        ) from exc
    source_file_sha256 = hashlib.sha256(source_bytes).hexdigest()
    if path.suffix.lower() in {".parquet", ".pq"}:
        frame = pd.read_parquet(io.BytesIO(source_bytes))
    elif path.suffix.lower() == ".csv":
        frame = pd.read_csv(io.BytesIO(source_bytes))
    else:
        raise PointInTimeUniverseError(
            "%s: import must be CSV or Parquet" % PIT_REQUIRED
        )
    dates = _normalise_dates(calendar).unique().sort_values()
    if dates.empty:
        raise PointInTimeUniverseError(
            "%s: requested trading calendar is empty" % PIT_REQUIRED
        )
    columns = set(frame.columns)
    lower_columns = {str(column).strip().lower() for column in columns}
    if lower_columns.intersection({"opt-in", "opt-out", "opt_in", "opt_out"}):
        raise PointInTimeUniverseError(
            "%s: opt-in/opt-out dates are ambiguous; convert audited "
            "effective dates to inclusive entry_date/exit_date first"
            % PIT_REQUIRED
        )
    daily_required = {"date", "symbol"}
    interval_required = {"symbol", "entry_date", "exit_date"}
    daily_allowed = daily_required | {"is_member"}
    interval_allowed = interval_required | {"is_member", "interval"}
    daily_layout = daily_required.issubset(columns)
    interval_layout = interval_required.issubset(columns)
    has_interval_column = bool({"entry_date", "exit_date"}.intersection(columns))
    if daily_layout and has_interval_column:
        raise PointInTimeUniverseError(
            "%s: import layout is ambiguous; provide either daily snapshots "
            "or inclusive intervals" % PIT_REQUIRED
        )
    if daily_layout:
        unexpected = sorted(columns.difference(daily_allowed))
        if unexpected:
            raise PointInTimeUniverseError(
                "%s: unsupported daily import columns %s"
                % (PIT_REQUIRED, unexpected)
            )
        input_layout = "daily"
        membership_semantics = "exact-trading-date-snapshot"
        strict_daily = frame.copy()
        strict_daily["date"] = pd.Series(
            [
                strict_import_date(value, "date")
                for value in strict_daily["date"].tolist()
            ],
            index=strict_daily.index,
            dtype="datetime64[ns]",
        )
        daily = validate_daily_membership(
            strict_daily, dates, expected_member_count
        )
    elif interval_layout:
        unexpected = sorted(columns.difference(interval_allowed))
        if unexpected:
            raise PointInTimeUniverseError(
                "%s: unsupported interval import columns %s"
                % (PIT_REQUIRED, unexpected)
            )
        input_layout = "inclusive-intervals"
        membership_semantics = "entry_date <= trading_date <= exit_date"
        daily = expand_membership_intervals(
            frame, dates, expected_member_count
        )
    else:
        raise PointInTimeUniverseError(
            "%s: import requires date,symbol or "
            "symbol,entry_date,exit_date columns" % PIT_REQUIRED
        )
    intervals = compress_membership_intervals(daily)
    digest = dataframe_sha256(daily)
    intervals_digest = dataframe_sha256(intervals)
    manifest = {
        "schema_version": 2,
        "source": "local-import",
        "source_version": "user-pit-import-v2",
        "input_layout": input_layout,
        "membership_semantics": membership_semantics,
        "source_file": str(path.resolve()),
        "source_file_sha256": source_file_sha256,
        "index_symbol": index_symbol,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "start": dates.min().strftime("%Y-%m-%d"),
        "end": dates.max().strftime("%Y-%m-%d"),
        "trading_dates": int(len(dates.unique())),
        "expected_member_count": int(expected_member_count),
        "sha256": digest,
        "daily_sha256": digest,
        "intervals_sha256": intervals_digest,
    }
    return _publish_pit_generation(daily, intervals, manifest, Path(output_dir))
