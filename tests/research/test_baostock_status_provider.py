"""Deterministic BaoStock status/listing retry and pacing contracts."""

import pandas as pd
import pytest

from factorlab.errors import PointInTimeUniverseError, ProviderError
from factorlab.universe.csi500 import (
    BaoStockPointInTimeProvider,
    collect_point_in_time_universe,
)


class FakeQuery:
    def __init__(
        self,
        fields=(),
        rows=(),
        error_code="0",
        error_msg="",
        response_date="",
        terminal_error=None,
    ):
        self.fields = list(fields)
        self._rows = [list(row) for row in rows]
        self.error_code = error_code
        self.error_msg = error_msg
        self.date = response_date
        self._terminal_error = terminal_error
        self._position = -1

    def next(self):
        self._position += 1
        has_row = self._position < len(self._rows)
        if not has_row and self._terminal_error is not None:
            self.error_code, self.error_msg = self._terminal_error
        return has_row

    def get_row_data(self):
        return self._rows[self._position]


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(float(seconds))
        self.now += float(seconds)


def _basic_success(code):
    return FakeQuery(
        fields=("code", "ipoDate"),
        rows=((code, "1999-11-10"),),
    )


def _history_success(code):
    return FakeQuery(
        fields=("date", "code", "tradestatus", "isST"),
        rows=(
            ("2024-01-02", code, "1", "0"),
            ("2024-01-03", code, "0", "1"),
        ),
    )


def _constituents_success(*codes, response_date="2024-01-02"):
    return FakeQuery(
        fields=("updateDate", "code", "code_name"),
        rows=tuple((response_date, code, "fixture") for code in codes),
        response_date=response_date,
    )


def test_constituent_query_uses_strict_fields_and_returns_canonical_snapshot():
    class Client:
        def query_zz500_stocks(self, date):
            assert date == "2024-01-02"
            return _constituents_success("sh.600000", "sz.000001")

    provider = BaoStockPointInTimeProvider(
        client=Client(), retry_count=0, request_interval=0.0
    )

    result = provider.get_constituents("000905.SH", pd.Timestamp("2024-01-02"))

    assert result.to_dict("records") == [
        {
            "date": pd.Timestamp("2024-01-02"),
            "symbol": "000001.SZ",
            "is_member": True,
        },
        {
            "date": pd.Timestamp("2024-01-02"),
            "symbol": "600000.SH",
            "is_member": True,
        },
    ]


def test_constituent_query_rejects_error_reported_only_after_iteration():
    class Client:
        def query_zz500_stocks(self, date):
            return FakeQuery(
                fields=("date", "code"),
                rows=((date, "sh.600000"),),
                response_date=date,
                terminal_error=("100", "next page failed"),
            )

    provider = BaoStockPointInTimeProvider(
        client=Client(), retry_count=0, request_interval=0.0
    )

    with pytest.raises(ProviderError, match="iteration: next page failed"):
        provider.get_constituents("000905.SH", "2024-01-02")


def test_constituent_query_requires_named_code_field():
    class Client:
        def query_zz500_stocks(self, date):
            return FakeQuery(
                fields=("date", "security"),
                rows=((date, "sh.600000"),),
                response_date=date,
            )

    provider = BaoStockPointInTimeProvider(
        client=Client(), retry_count=0, request_interval=0.0
    )

    with pytest.raises(ProviderError, match=r"missing fields \['code'\]"):
        provider.get_constituents("000905.SH", "2024-01-02")


@pytest.mark.parametrize(
    "query,error_pattern",
    [
        (
            _constituents_success("sh.600000", response_date="2024-01-03"),
            "response date mismatch",
        ),
        (
            _constituents_success("sh.600000", "600000.SH"),
            "duplicate constituent codes",
        ),
        (
            _constituents_success("not-a-security-code"),
            "invalid constituent code",
        ),
    ],
    ids=("response-date-mismatch", "duplicate-canonical-code", "invalid-code"),
)
def test_constituent_query_rejects_invalid_response_identity(query, error_pattern):
    class Client:
        def query_zz500_stocks(self, date):
            return query

    provider = BaoStockPointInTimeProvider(
        client=Client(), retry_count=0, request_interval=0.0
    )

    with pytest.raises(ProviderError, match=error_pattern):
        provider.get_constituents("000905.SH", "2024-01-02")


def test_single_retry_layer_limits_three_retries_to_four_endpoint_calls(tmp_path):
    class AlwaysFailingClient:
        def __init__(self):
            self.calls = 0

        def query_zz500_stocks(self, date):
            self.calls += 1
            return FakeQuery(error_code="100", error_msg="temporary outage")

    client = AlwaysFailingClient()
    provider = BaoStockPointInTimeProvider(
        client=client,
        retry_count=3,
        backoff_seconds=(0.0, 0.0, 0.0),
        request_interval=0.0,
        sleep_fn=lambda seconds: None,
    )

    # The diagnostic endpoint still owns one retry layer; formal publication
    # rejects BaoStock before any request while effective semantics are unknown.
    with pytest.raises(ProviderError, match="failed after 4 attempt"):
        provider.get_constituents("000905.SH", pd.Timestamp("2024-01-02"))

    assert client.calls == 4


def test_login_uses_the_same_retry_contract_as_data_queries():
    class LoginResult:
        def __init__(self, code, message=""):
            self.error_code = code
            self.error_msg = message

    class FlakyLoginClient:
        def __init__(self):
            self.login_calls = 0
            self.logout_calls = 0

        def login(self):
            self.login_calls += 1
            if self.login_calls == 1:
                return LoginResult("100", "temporary login failure")
            return LoginResult("0")

        def logout(self):
            self.logout_calls += 1

    client = FlakyLoginClient()
    clock = FakeClock()
    provider = BaoStockPointInTimeProvider(
        client=client,
        retry_count=1,
        backoff_seconds=(1.0,),
        request_interval=0.25,
        sleep_fn=clock.sleep,
        monotonic_fn=clock.monotonic,
    )

    with provider as connected:
        assert connected is provider

    assert client.login_calls == 2
    assert client.logout_calls == 1
    assert clock.sleeps == [1.0]


def test_status_and_listing_queries_retry_and_share_one_rate_limiter():
    class FlakyClient:
        def __init__(self):
            self.basic_calls = 0
            self.history_calls = 0

        def query_stock_basic(self, code):
            self.basic_calls += 1
            if self.basic_calls == 1:
                return FakeQuery(error_code="100", error_msg="temporary basic failure")
            return _basic_success(code)

        def query_history_k_data_plus(self, code, fields, **kwargs):
            self.history_calls += 1
            if self.history_calls == 1:
                return FakeQuery(error_code="200", error_msg="temporary status failure")
            return _history_success(code)

    client = FlakyClient()
    clock = FakeClock()
    provider = BaoStockPointInTimeProvider(
        client=client,
        retry_count=1,
        backoff_seconds=(1.0,),
        request_interval=0.25,
        sleep_fn=clock.sleep,
        monotonic_fn=clock.monotonic,
    )

    status, listings, failures = provider.get_security_history(
        ["600000.SH"], "2024-01-02", "2024-01-03"
    )

    assert client.basic_calls == 2
    assert client.history_calls == 2
    assert clock.sleeps == [1.0, 0.25, 1.0]
    assert failures == []
    assert listings.to_dict("records") == [
        {"symbol": "600000.SH", "listing_date": pd.Timestamp("1999-11-10")}
    ]
    assert status["trade_status"].tolist() == [1, 0]
    assert status["is_st"].tolist() == [False, True]


def test_exhausted_symbol_failure_does_not_abort_remaining_symbols():
    class PartlyBrokenClient:
        def __init__(self):
            self.history_calls = {}

        def query_stock_basic(self, code):
            return _basic_success(code)

        def query_history_k_data_plus(self, code, fields, **kwargs):
            self.history_calls[code] = self.history_calls.get(code, 0) + 1
            if code == "sz.000001":
                return FakeQuery(error_code="300", error_msg="persistent failure")
            return _history_success(code)

    client = PartlyBrokenClient()
    provider = BaoStockPointInTimeProvider(
        client=client,
        retry_count=1,
        backoff_seconds=(0.0,),
        request_interval=0.0,
        sleep_fn=lambda seconds: None,
    )

    status, listings, failures = provider.get_security_history(
        ["600000.SH", "000001.SZ"], "2024-01-02", "2024-01-03"
    )

    assert client.history_calls == {"sz.000001": 2, "sh.600000": 1}
    assert failures == ["000001.SZ"]
    assert status["symbol"].unique().tolist() == ["600000.SH"]
    assert listings["symbol"].tolist() == ["600000.SH"]


@pytest.mark.parametrize(
    "basic_query,history_query",
    [
        (
            _basic_success("sz.000001"),
            _history_success("sh.600000"),
        ),
        (
            FakeQuery(
                fields=("code", "ipoDate"),
                rows=(
                    ("sh.600000", "1999-11-10"),
                    ("sh.600000", "1999-11-10"),
                ),
            ),
            _history_success("sh.600000"),
        ),
        (
            _basic_success("sh.600000"),
            _history_success("sz.000001"),
        ),
    ],
    ids=("basic-code-mismatch", "multiple-basic-rows", "history-code-mismatch"),
)
def test_status_history_rejects_mismatched_or_ambiguous_response_codes(
    basic_query, history_query
):
    class InvalidResponseClient:
        def query_stock_basic(self, code):
            return basic_query

        def query_history_k_data_plus(self, code, fields, **kwargs):
            return history_query

    provider = BaoStockPointInTimeProvider(
        client=InvalidResponseClient(),
        retry_count=0,
        request_interval=0.0,
        sleep_fn=lambda seconds: None,
    )

    status, listings, failures = provider.get_security_history(
        ["600000.SH"], "2024-01-02", "2024-01-03"
    )

    assert status.empty
    assert listings.empty
    assert failures == ["600000.SH"]
