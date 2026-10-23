"""Failure classification must not let malformed research inputs be skipped."""

import errno
import http.client
import socket
import urllib.error

import numpy as np
import pandas as pd
import pytest
import requests
import urllib3

from qbot.research.data.providers.akshare import AKShareProvider
from qbot.research.errors import (
    CacheError,
    DataQualityError,
    ProviderError,
    ProviderNetworkError,
    SchemaError,
)


def _vendor_frame():
    return pd.DataFrame({
        "日期": ["2024-01-02", "2024-01-03"],
        "开盘": [10.0, 11.0],
        "最高": [11.0, 12.0],
        "最低": [9.0, 10.0],
        "收盘": [10.5, 11.5],
        "成交量": [12.0, 15.0],
        "成交额": [12_600.0, 17_250.0],
        "换手率": [2.0, 2.5],
    })


def _http_error(status):
    response = requests.Response()
    response.status_code = status
    return requests.HTTPError("HTTP failure", response=response)


class FailureSDK:
    __version__ = "test"

    def __init__(self, error):
        self.error = error
        self.calls = 0

    def index_zh_a_hist(self, **kwargs):
        self.calls += 1
        raise self.error


def _failure_provider(error):
    sdk = FailureSDK(error)
    delays = []
    provider = AKShareProvider(
        akshare_module=sdk, retry_count=2, request_interval=0,
        backoff_seconds=(0.1, 0.2), sleep_fn=delays.append,
    )
    return provider, sdk, delays


@pytest.mark.parametrize("error", [
    ConnectionError("connection lost"),
    TimeoutError("timed out"),
    socket.gaierror("DNS lookup failed"),
    socket.herror("host lookup failed"),
    http.client.RemoteDisconnected("remote closed connection"),
    http.client.IncompleteRead(b"partial", 20),
    http.client.BadStatusLine("invalid HTTP status"),
    requests.ConnectionError("connection lost"),
    requests.ConnectTimeout("connect timeout"),
    requests.ReadTimeout("read timeout"),
    requests.exceptions.SSLError("TLS transport failure"),
    requests.exceptions.ProxyError("proxy connection lost"),
    requests.exceptions.ChunkedEncodingError("incomplete chunk"),
    urllib.error.URLError(socket.gaierror("DNS lookup failed")),
    urllib.error.URLError(TimeoutError("timed out")),
    urllib3.exceptions.ReadTimeoutError(None, "/", "read timed out"),
    urllib3.exceptions.NewConnectionError(None, "connection refused"),
    urllib3.exceptions.ProtocolError("connection lost"),
    urllib3.exceptions.SSLError("TLS transport failure"),
    urllib3.exceptions.ProxyError("proxy failure", ConnectionError("connection lost")),
    urllib3.exceptions.MaxRetryError(
        None, "/", reason=urllib3.exceptions.NewConnectionError(None, "connection refused"),
    ),
], ids=lambda item: type(item).__name__)
def test_explicit_transport_failures_exhaust_exact_retry_budget(error):
    provider, sdk, delays = _failure_provider(error)
    with pytest.raises(ProviderNetworkError, match="failed after retries") as failure:
        provider.get_index_bars("000905.SH", "2024-01-01", "2024-01-04")
    assert failure.value.__cause__ is error
    assert isinstance(failure.value, ProviderError)
    assert sdk.calls == 3
    assert delays == [0.1, 0.2]


@pytest.mark.parametrize("status", [408, 425, 429, 500, 501, 502, 503, 504, 599])
@pytest.mark.parametrize("http_library", ["requests", "urllib"])
def test_transient_http_status_is_network_failure_with_exact_retries(status, http_library):
    error = (
        _http_error(status) if http_library == "requests"
        else urllib.error.HTTPError("https://example.invalid", status, "HTTP failure", {}, None)
    )
    provider, sdk, delays = _failure_provider(error)
    with pytest.raises(ProviderNetworkError) as failure:
        provider.get_index_bars("000905.SH", "2024-01-01", "2024-01-04")
    assert failure.value.__cause__ is error
    assert sdk.calls == 3
    assert delays == [0.1, 0.2]


@pytest.mark.parametrize("status", [200, 301, 400, 401, 403, 404, 410, 422, 600])
@pytest.mark.parametrize("http_library", ["requests", "urllib"])
def test_nontransient_http_status_is_fatal_without_retry(status, http_library):
    error = (
        _http_error(status) if http_library == "requests"
        else urllib.error.HTTPError("https://example.invalid", status, "HTTP failure", {}, None)
    )
    provider, sdk, delays = _failure_provider(error)
    with pytest.raises(ProviderError, match="without a recoverable network error") as failure:
        provider.get_index_bars("000905.SH", "2024-01-01", "2024-01-04")
    assert not isinstance(failure.value, ProviderNetworkError)
    assert failure.value.__cause__ is error
    assert sdk.calls == 1
    assert delays == []


@pytest.mark.parametrize("error", [
    RuntimeError("SDK programming failure"),
    TypeError("unsupported SDK argument"),
    ValueError("malformed payload"),
    PermissionError("permission denied"),
    FileNotFoundError("local file absent"),
    OSError(errno.EIO, "local disk failure"),
    requests.HTTPError("HTTP status not supplied"),
    requests.exceptions.InvalidURL("malformed URL"),
    requests.exceptions.MissingSchema("URL scheme missing"),
    requests.exceptions.InvalidSchema("unsupported scheme"),
    requests.exceptions.TooManyRedirects("redirect loop"),
    requests.exceptions.RequestException("unclassified request failure"),
    requests.exceptions.JSONDecodeError("malformed JSON", "{", 1),
    urllib.error.URLError(PermissionError("local permission denied")),
    urllib.error.URLError("unclassified reason"),
    http.client.InvalidURL("invalid port"),
    urllib3.exceptions.LocationParseError("malformed URL"),
    urllib3.exceptions.MaxRetryError(None, "/", reason=ValueError("SDK failure")),
], ids=lambda item: type(item).__name__)
def test_generic_sdk_configuration_format_and_local_failures_are_not_network(error):
    provider, sdk, delays = _failure_provider(error)
    with pytest.raises(ProviderError, match="without a recoverable network error") as failure:
        provider.get_index_bars("000905.SH", "2024-01-01", "2024-01-04")
    assert not isinstance(failure.value, ProviderNetworkError)
    assert failure.value.__cause__ is error
    assert sdk.calls == 1
    assert delays == []


def test_generic_wrapped_connection_exception_is_not_reclassified_as_network():
    error = RuntimeError("SDK wrapper failed")
    error.__cause__ = ConnectionError("connection lost")
    provider, sdk, delays = _failure_provider(error)
    with pytest.raises(ProviderError) as failure:
        provider.get_index_bars("000905.SH", "2024-01-01", "2024-01-04")
    assert not isinstance(failure.value, ProviderNetworkError)
    assert failure.value.__cause__ is error
    assert sdk.calls == 1
    assert not delays


@pytest.mark.parametrize("error", [
    SchemaError("invalid OHLC"), DataQualityError("misaligned data"),
    CacheError("cache corrupted"), ProviderError("unsupported SDK"),
])
def test_existing_domain_failures_are_preserved_without_retry(error):
    provider, sdk, delays = _failure_provider(error)
    with pytest.raises(type(error)) as failure:
        provider.get_index_bars("000905.SH", "2024-01-01", "2024-01-04")
    assert failure.value is error
    assert sdk.calls == 1
    assert not delays


class ResponseSDK:
    __version__ = "test"

    def __init__(self, raw, adjusted=None):
        self.raw = raw
        self.adjusted = adjusted if adjusted is not None else raw
        self.calls = []

    def stock_zh_a_hist(self, **kwargs):
        self.calls.append(kwargs)
        return self.adjusted if kwargs["adjust"] else self.raw


def _response_provider(raw, adjusted=None):
    sdk = ResponseSDK(raw, adjusted)
    delays = []
    provider = AKShareProvider(
        akshare_module=sdk, retry_count=3, request_interval=0,
        sleep_fn=delays.append,
    )
    return provider, sdk, delays


@pytest.mark.parametrize("response", [None, {}, [], "not a frame", pd.Series([1])])
def test_non_dataframe_payload_fails_before_adjusted_endpoint_without_retry(response):
    provider, sdk, delays = _response_provider(response)
    with pytest.raises(SchemaError, match="expected pandas DataFrame"):
        provider.get_daily_bars("600000.SH", "2024-01-01", "2024-01-04")
    assert len(sdk.calls) == 1
    assert not delays


@pytest.mark.parametrize("column", [
    "日期", "开盘", "最高", "最低", "收盘", "成交量", "成交额", "换手率",
])
def test_missing_raw_required_field_is_immediate_schema_failure(column):
    provider, sdk, delays = _response_provider(_vendor_frame().drop(columns=column))
    with pytest.raises(SchemaError, match="missing"):
        provider.get_daily_bars("600000.SH", "2024-01-01", "2024-01-04")
    assert len(sdk.calls) == 1
    assert not delays


@pytest.mark.parametrize("value", ["not-a-date", "2024-13-01", None, pd.NaT, 20240102, True])
def test_invalid_raw_date_is_immediate_schema_failure(value):
    raw = _vendor_frame()
    raw["日期"] = pd.Series([value, "2024-01-03"], dtype=object)
    provider, sdk, delays = _response_provider(raw)
    with pytest.raises(SchemaError, match="date"):
        provider.get_daily_bars("600000.SH", "2024-01-01", "2024-01-04")
    assert len(sdk.calls) == 1
    assert not delays


@pytest.mark.parametrize("damage", ["duplicate-date", "duplicate-normalized-date", "duplicate-column", "no-columns"])
def test_malformed_shape_and_duplicates_are_not_empty_success(damage):
    raw = _vendor_frame()
    if damage == "duplicate-date":
        raw.loc[1, "日期"] = raw.loc[0, "日期"]
    elif damage == "duplicate-normalized-date":
        raw["日期"] = ["2024-01-02 09:30", "2024-01-02 15:00"]
    elif damage == "duplicate-column":
        raw = pd.concat([raw, raw[["开盘"]]], axis=1)
    else:
        raw = pd.DataFrame(index=[0, 1])
    provider, sdk, delays = _response_provider(raw)
    with pytest.raises(SchemaError):
        provider.get_daily_bars("600000.SH", "2024-01-01", "2024-01-04")
    assert len(sdk.calls) == 1
    assert not delays


@pytest.mark.parametrize("column,value", [
    ("最高", 1.0), ("最低", 20.0), ("开盘", 0.0),
    ("收盘", None), ("成交量", -1.0), ("成交额", -1.0),
    ("换手率", -1.0), ("成交额", "malformed"),
    ("成交额", np.inf), ("成交量", "inf"), ("收盘", "-inf"),
])
def test_invalid_raw_ohlc_numeric_and_nonfinite_fields_fail_before_adjusted_call(column, value):
    raw = _vendor_frame()
    raw[column] = raw[column].astype(object)
    raw.loc[0, column] = value
    provider, sdk, delays = _response_provider(raw)
    with pytest.raises(SchemaError):
        provider.get_daily_bars("600000.SH", "2024-01-01", "2024-01-04")
    assert len(sdk.calls) == 1
    assert not delays


@pytest.mark.parametrize("bad_frame", ["raw", "adjusted"])
def test_empty_only_one_price_representation_is_fatal_data_quality_failure(bad_frame):
    raw, adjusted = _vendor_frame(), _vendor_frame()
    if bad_frame == "raw":
        raw = raw.iloc[:0]
    else:
        adjusted = adjusted.iloc[:0]
    provider, sdk, delays = _response_provider(raw, adjusted)
    with pytest.raises(DataQualityError, match="different trading dates"):
        provider.get_daily_bars("600000.SH", "2024-01-01", "2024-01-04")
    assert [call["adjust"] for call in sdk.calls] == ["", "hfq"]
    assert not delays


def test_raw_adjusted_date_mismatch_is_fatal_data_quality_failure_without_retry():
    raw, adjusted = _vendor_frame(), _vendor_frame()
    adjusted.loc[1, "日期"] = "2024-01-04"
    provider, sdk, delays = _response_provider(raw, adjusted)
    with pytest.raises(DataQualityError, match="different trading dates"):
        provider.get_daily_bars("600000.SH", "2024-01-01", "2024-01-04")
    assert [call["adjust"] for call in sdk.calls] == ["", "hfq"]
    assert not delays


def test_adjusted_ohlc_schema_failure_is_not_retried():
    raw, adjusted = _vendor_frame(), _vendor_frame()
    adjusted.loc[0, "最高"] = 1.0
    provider, sdk, delays = _response_provider(raw, adjusted)
    with pytest.raises(SchemaError, match="OHLC relationship"):
        provider.get_daily_bars("600000.SH", "2024-01-01", "2024-01-04")
    assert [call["adjust"] for call in sdk.calls] == ["", "hfq"]
    assert not delays


def test_adjusted_optional_turnover_and_amount_can_be_absent():
    raw = _vendor_frame()
    adjusted = raw.drop(columns=["换手率", "成交额"])
    provider, sdk, delays = _response_provider(raw, adjusted)
    bars = provider.get_daily_bars("600000.SH", "2024-01-01", "2024-01-04")
    assert bars["amount"].tolist() == raw["成交额"].tolist()
    assert len(sdk.calls) == 2
    assert not delays


def test_sina_index_fallback_keeps_known_absent_amount_as_missing():
    class SinaSDK:
        __version__ = "test"

        def stock_zh_index_daily(self, **kwargs):
            return _vendor_frame().drop(columns=["成交额", "换手率"])

    provider = AKShareProvider(akshare_module=SinaSDK(), retry_count=3, request_interval=0)
    bars = provider.get_index_bars("000905.SH", "2024-01-01", "2024-01-04")
    assert len(bars) == 2
    assert bars["amount"].isna().all()
    assert bars["turnover"].isna().all()


def test_primary_index_endpoint_missing_amount_is_schema_failure():
    class PrimarySDK:
        __version__ = "test"

        def index_zh_a_hist(self, **kwargs):
            return _vendor_frame().drop(columns=["成交额"])

    provider = AKShareProvider(akshare_module=PrimarySDK(), retry_count=3, request_interval=0)
    with pytest.raises(SchemaError, match="missing amount"):
        provider.get_index_bars("000905.SH", "2024-01-01", "2024-01-04")


def test_empty_benchmark_calendar_is_fatal_data_quality_failure():
    class EmptySDK:
        __version__ = "test"

        def index_zh_a_hist(self, **kwargs):
            return _vendor_frame().iloc[:0]

    provider = AKShareProvider(akshare_module=EmptySDK(), retry_count=3, request_interval=0)
    with pytest.raises(DataQualityError, match="trading calendar"):
        provider.get_trade_calendar("2024-01-01", "2024-01-04")


@pytest.mark.parametrize("damage", ["missing-amount", "unverifiable-units", "invalid-ohlc"])
def test_official_index_contract_failure_is_fatal_without_retry_or_endpoint_fallback(damage):
    delays = []

    class OfficialSDK:
        __version__ = "test"

        def __init__(self):
            self.calls = 0

        def stock_zh_index_hist_csindex(self, **kwargs):
            self.calls += 1
            frame = _vendor_frame()
            frame = frame.rename(columns={"成交额": "成交金额"})
            if damage == "missing-amount":
                return frame.drop(columns="成交金额")
            if damage == "unverifiable-units":
                frame["成交量"] = 1e20
                frame["成交金额"] = 1.0
            else:
                frame.loc[0, "最高"] = 1.0
            return frame

        def index_zh_a_hist(self, **kwargs):
            raise AssertionError("bad official data must not be hidden by fallback")

    sdk = OfficialSDK()
    provider = AKShareProvider(
        akshare_module=sdk, retry_count=3, request_interval=0,
        sleep_fn=delays.append,
    )
    expected = DataQualityError if damage == "unverifiable-units" else SchemaError
    with pytest.raises(expected):
        provider.get_index_bars("000905.SH", "2024-01-01", "2024-01-04")
    assert sdk.calls == 1
    assert not delays


@pytest.mark.parametrize("response", [
    pd.DataFrame({"not-symbol": ["600000"]}),
    pd.DataFrame([["600000", "000001"]], columns=["symbol", "symbol"]),
    pd.DataFrame({"symbol": ["not-a-symbol"]}),
])
def test_current_constituent_shape_symbol_contract_is_schema_failure_without_retry(response):
    delays = []

    class ConstituentSDK:
        __version__ = "test"

        def __init__(self):
            self.calls = 0

        def index_stock_cons(self, **kwargs):
            self.calls += 1
            return response

    sdk = ConstituentSDK()
    provider = AKShareProvider(
        akshare_module=sdk, retry_count=3, request_interval=0,
        sleep_fn=delays.append,
    )
    with pytest.raises(SchemaError):
        provider.get_index_constituents("000905.SH")
    assert sdk.calls == 1
    assert not delays
