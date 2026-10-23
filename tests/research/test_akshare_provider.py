import pandas as pd
import pytest

from qbot.research.data.providers.akshare import AKShareProvider, ProviderError
from qbot.research.errors import ProviderNetworkError


def _vendor_frame(multiplier=1.0, include_turnover=True):
    values = {
        "日期": ["2024-01-02", "2024-01-03"],
        "开盘": [10.0 * multiplier, 11.0 * multiplier],
        "最高": [11.0 * multiplier, 12.0 * multiplier],
        "最低": [9.0 * multiplier, 10.0 * multiplier],
        "收盘": [10.5 * multiplier, 11.5 * multiplier],
        "成交量": [12.0, 15.0],
        "成交额": [12_600.0, 17_250.0],
    }
    if include_turnover:
        values["换手率"] = [2.0, 2.5]
    return pd.DataFrame(values)


class FakeAKShare:
    __version__ = "test"

    def __init__(self):
        self.stock_calls = []

    def stock_zh_a_hist(self, **kwargs):
        self.stock_calls.append(kwargs)
        multiplier = 2.0 if kwargs["adjust"] == "hfq" else 1.0
        return _vendor_frame(multiplier=multiplier)

    def index_zh_a_hist(self, **kwargs):
        return _vendor_frame(include_turnover=False)

    def index_stock_cons(self, **kwargs):
        return pd.DataFrame({"品种代码": ["600000", "000001", "600000"]})


def test_stock_bars_join_raw_and_hfq_and_convert_units():
    fake = FakeAKShare()
    provider = AKShareProvider(
        akshare_module=fake, retry_count=0, request_interval=0
    )
    bars = provider.get_daily_bars("600000.SH", "2024-01-01", "2024-01-04")

    assert [call["adjust"] for call in fake.stock_calls] == ["", "hfq"]
    assert all(call["symbol"] == "600000" for call in fake.stock_calls)
    assert all(call["timeout"] == 30.0 for call in fake.stock_calls)
    assert bars["symbol"].unique().tolist() == ["600000.SH"]
    assert bars.loc[0, "open"] == bars.loc[0, "raw_open"] == 10.0
    assert bars.loc[0, "adj_open"] == 20.0
    assert bars.loc[0, "volume"] == 1_200.0
    assert bars.loc[0, "amount"] == 12_600.0
    assert bars.loc[0, "turnover"] == pytest.approx(0.02)
    assert bars.loc[0, "adj_factor"] == pytest.approx(2.0)


def test_stock_cache_identity_binds_adjustment_and_package_version():
    hfq = AKShareProvider(
        adjustment="hfq",
        akshare_module=FakeAKShare(),
        retry_count=0,
        request_interval=0,
    )
    qfq = AKShareProvider(
        adjustment="qfq",
        akshare_module=FakeAKShare(),
        retry_count=0,
        request_interval=0,
    )

    assert hfq.cache_source_version("600000.SH", False) == (
        "akshare=test;contract=stock_zh_a_hist-raw+hfq-v1"
    )
    assert qfq.cache_source_version("600000.SH", False) == (
        "akshare=test;contract=stock_zh_a_hist-raw+qfq-v1"
    )
    assert hfq.cache_source_version("600000.SH", False) != (
        qfq.cache_source_version("600000.SH", False)
    )


def test_index_cache_identity_binds_selected_endpoint_not_adjustment():
    hfq = AKShareProvider(
        adjustment="hfq",
        akshare_module=FakeAKShare(),
        retry_count=0,
        request_interval=0,
    )
    qfq = AKShareProvider(
        adjustment="qfq",
        akshare_module=FakeAKShare(),
        retry_count=0,
        request_interval=0,
    )

    expected = "akshare=test;contract=index_zh_a_hist-v1"
    assert hfq.cache_source_version("000905.SH", True) == expected
    assert qfq.cache_source_version("000905.SH", True) == expected


def test_index_bars_are_unadjusted_and_drive_calendar():
    provider = AKShareProvider(
        akshare_module=FakeAKShare(), retry_count=0, request_interval=0
    )
    bars = provider.get_index_bars("000905.SH", "2024-01-01", "2024-01-04")
    assert bars["adj_factor"].eq(1.0).all()
    assert bars["adj_close"].equals(bars["raw_close"])
    calendar = provider.get_trade_calendar("2024-01-01", "2024-01-04")
    assert calendar.tolist() == [
        pd.Timestamp("2024-01-02"),
        pd.Timestamp("2024-01-03"),
    ]


def test_historical_constituent_request_never_falls_back_to_current_snapshot():
    provider = AKShareProvider(
        akshare_module=FakeAKShare(), retry_count=0, request_interval=0
    )
    with pytest.raises(ProviderError, match="POINT-IN-TIME UNIVERSE REQUIRED"):
        provider.get_index_constituents("000905.SH", as_of="2022-01-01")


def test_current_constituent_snapshot_is_explicitly_marked():
    provider = AKShareProvider(
        akshare_module=FakeAKShare(), retry_count=0, request_interval=0
    )
    result = provider.get_index_constituents("000905.SH")
    assert result["symbol"].tolist() == ["000001.SZ", "600000.SH"]
    assert result["is_current_snapshot"].all()


def test_retry_uses_injected_backoff_without_network():
    sleeps = []

    class FlakyAK(FakeAKShare):
        def __init__(self):
            super().__init__()
            self.failures = 1

        def index_zh_a_hist(self, **kwargs):
            if self.failures:
                self.failures -= 1
                raise ConnectionError("temporary")
            return _vendor_frame(include_turnover=False)

    provider = AKShareProvider(
        akshare_module=FlakyAK(),
        retry_count=1,
        backoff_seconds=(0.5,),
        request_interval=0,
        sleep_fn=sleeps.append,
    )
    assert not provider.get_index_bars(
        "000905.SH", "2024-01-01", "2024-01-04"
    ).empty
    assert sleeps == [0.5]


def test_index_provider_uses_alternate_only_when_primary_attribute_is_absent():
    class AlternateOnlyAK:
        __version__ = "test"

        def __init__(self):
            self.fallback_calls = []

        def stock_zh_index_daily(self, **kwargs):
            self.fallback_calls.append(kwargs)
            return _vendor_frame(include_turnover=False)

    fake = AlternateOnlyAK()
    provider = AKShareProvider(
        akshare_module=fake,
        retry_count=0,
        request_interval=0,
    )

    bars = provider.get_index_bars(
        "000905.SH", "2024-01-01", "2024-01-04"
    )

    assert fake.fallback_calls == [{"symbol": "sh000905"}]
    assert bars["date"].tolist() == [
        pd.Timestamp("2024-01-02"),
        pd.Timestamp("2024-01-03"),
    ]
    assert bars.loc[0, "volume"] == 12.0


@pytest.mark.parametrize(
    "reported_volume,expected_volume",
    [
        ([1_200_000_000.0, 1_500_000_000.0], [1_200_000_000.0, 1_500_000_000.0]),
        ([120.0, 150.0], [120_000_000.0, 150_000_000.0]),
    ],
    ids=("shares", "ten-thousand-hands"),
)
def test_csi500_prefers_official_index_endpoint_and_normalises_units(
    reported_volume, expected_volume
):
    class OfficialCSI:
        __version__ = "test"

        def __init__(self):
            self.official_calls = []

        def stock_zh_index_hist_csindex(self, **kwargs):
            self.official_calls.append(kwargs)
            return pd.DataFrame(
                {
                    "日期": ["2024-01-02", "2024-01-03"],
                    "开盘": [10.0, 11.0],
                    "最高": [11.0, 12.0],
                    "最低": [9.0, 10.0],
                    "收盘": [10.5, 11.5],
                    "成交量": reported_volume,
                    "成交金额": [126.0, 172.5],
                }
            )

        def index_zh_a_hist(self, **kwargs):
            raise AssertionError("CSI500 must use the official index endpoint")

    fake = OfficialCSI()
    provider = AKShareProvider(
        akshare_module=fake, retry_count=0, request_interval=0
    )

    bars = provider.get_index_bars(
        "000905.SH", "2024-01-01", "2024-01-04"
    )

    assert fake.official_calls == [
        {
            "symbol": "000905",
            "start_date": "20240101",
            "end_date": "20240104",
        }
    ]
    assert bars["volume"].tolist() == expected_volume
    assert bars["amount"].tolist() == [12_600_000_000.0, 17_250_000_000.0]
    assert provider.cache_source_version("000905.SH", True) == (
        "akshare=test;contract=stock_zh_index_hist_csindex-v1"
    )


@pytest.mark.parametrize("timeout", [5.0, 7])
def test_configured_timeout_is_passed_to_both_native_stock_requests(timeout):
    class NativeSignatureAK(FakeAKShare):
        def stock_zh_a_hist(self, symbol, period, start_date, end_date, adjust, timeout):
            return super().stock_zh_a_hist(
                symbol=symbol, period=period, start_date=start_date,
                end_date=end_date, adjust=adjust, timeout=timeout,
            )

    fake = NativeSignatureAK()
    provider = AKShareProvider(
        akshare_module=fake, retry_count=0, request_interval=0,
        timeout_seconds=timeout,
    )
    bars = provider.get_daily_bars("600000.SH", "2024-01-01", "2024-01-04")
    assert not bars.empty
    assert provider.timeout_seconds == float(timeout)
    assert [call["timeout"] for call in fake.stock_calls] == [float(timeout)] * 2
    assert [call["adjust"] for call in fake.stock_calls] == ["", "hfq"]


@pytest.mark.parametrize(
    "timeout",
    [float("nan"), float("inf"), -float("inf"), 0, -1, "30", None, True, False, 10 ** 1000],
    ids=["nan", "inf", "negative-inf", "zero", "negative", "string", "none", "true", "false", "overflow"],
)
def test_invalid_timeout_is_rejected_as_provider_error_without_sdk_access(timeout):
    class UntouchableSDK:
        def __getattribute__(self, name):
            raise AssertionError("invalid configuration must not access the SDK")

    with pytest.raises(ProviderError, match="timeout_seconds must be a finite positive number"):
        AKShareProvider(akshare_module=UntouchableSDK(), timeout_seconds=timeout)


@pytest.mark.parametrize("failing_adjustment", ["", "hfq"], ids=["raw", "adjusted"])
def test_stock_timeouts_exhaust_exact_endpoint_retry_budget_without_partial_success(
    failing_adjustment,
):
    pauses = []
    error = TimeoutError("native stock request timed out")

    class TimeoutAK(FakeAKShare):
        def stock_zh_a_hist(self, **kwargs):
            if kwargs["adjust"] == failing_adjustment:
                self.stock_calls.append(kwargs)
                raise error
            return super().stock_zh_a_hist(**kwargs)

    fake = TimeoutAK()
    provider = AKShareProvider(
        akshare_module=fake, retry_count=2, backoff_seconds=(0.1, 0.2),
        request_interval=0, sleep_fn=pauses.append, timeout_seconds=5.0,
    )
    with pytest.raises(ProviderNetworkError, match="AKShare request failed after retries") as failure:
        provider.get_daily_bars("600000.SH", "2024-01-01", "2024-01-04")
    assert failure.value.__cause__ is error
    expected_adjustments = ([""] if failing_adjustment == "hfq" else []) + [failing_adjustment] * 3
    assert [call["adjust"] for call in fake.stock_calls] == expected_adjustments
    assert all(call["timeout"] == 5.0 for call in fake.stock_calls)
    assert pauses == [0.1, 0.2]


def test_stock_timeout_retries_keep_timeout_on_every_raw_and_hfq_attempt():
    pauses = []

    class OnceTimeoutAK(FakeAKShare):
        def __init__(self):
            super().__init__()
            self.seen = set()

        def stock_zh_a_hist(self, **kwargs):
            if kwargs["adjust"] not in self.seen:
                self.seen.add(kwargs["adjust"])
                self.stock_calls.append(kwargs)
                raise TimeoutError("transient native timeout")
            return super().stock_zh_a_hist(**kwargs)

    fake = OnceTimeoutAK()
    provider = AKShareProvider(
        akshare_module=fake, retry_count=1, backoff_seconds=(0.25,),
        request_interval=0, sleep_fn=pauses.append, timeout_seconds=2.5,
    )
    assert not provider.get_daily_bars("600000.SH", "2024-01-01", "2024-01-04").empty
    assert [call["adjust"] for call in fake.stock_calls] == ["", "", "hfq", "hfq"]
    assert [call["timeout"] for call in fake.stock_calls] == [2.5] * 4
    assert pauses == [0.25, 0.25]


def test_new_timeout_keyword_preserves_old_positional_constructor_api():
    fake = FakeAKShare()
    pauses = []
    provider = AKShareProvider("000905.SH", "hfq", 0, (1.0,), 0.0, fake, pauses.append, lambda: 0.0)
    assert not provider.get_daily_bars("600000.SH", "2024-01-01", "2024-01-04").empty
    assert [call["timeout"] for call in fake.stock_calls] == [30.0, 30.0]
    assert not pauses


def test_timeout_does_not_change_stock_or_index_cache_source_contract():
    first = AKShareProvider(akshare_module=FakeAKShare(), timeout_seconds=5.0)
    second = AKShareProvider(akshare_module=FakeAKShare(), timeout_seconds=30.0)
    assert first.cache_source_version("600000.SH", False) == second.cache_source_version("600000.SH", False)
    assert first.cache_source_version("000905.SH", True) == second.cache_source_version("000905.SH", True)


def test_benchmark_endpoint_without_native_timeout_is_called_without_fake_bound():
    class NativeIndexAK(FakeAKShare):
        def __init__(self):
            super().__init__()
            self.index_calls = []

        def index_zh_a_hist(self, symbol, period, start_date, end_date):
            self.index_calls.append((symbol, period, start_date, end_date))
            return _vendor_frame(include_turnover=False)

    fake = NativeIndexAK()
    provider = AKShareProvider(
        akshare_module=fake, retry_count=0, request_interval=0, timeout_seconds=5.0,
    )
    assert not provider.get_index_bars("000905.SH", "2024-01-01", "2024-01-04").empty
    assert fake.index_calls == [("000905", "daily", "20240101", "20240104")]
