import json

import pandas as pd
import pytest

from factorlab.data.cache import ParquetBarCache
from factorlab.data.schema import canonicalize_bars, empty_bars
from factorlab.errors import CacheError


def _bars(dates, closes=None):
    closes = closes or [10.0 + offset for offset in range(len(dates))]
    frame = pd.DataFrame(
        {
            "date": dates,
            "symbol": "600000.SH",
            "open": closes,
            "high": [value + 1 for value in closes],
            "low": [value - 1 for value in closes],
            "close": closes,
            "volume": 1000.0,
            "amount": 10_000.0,
            "turnover": 0.01,
        }
    )
    return canonicalize_bars(frame)


def test_cache_tracks_covered_ranges_and_incremental_gaps(tmp_path):
    cache = ParquetBarCache(tmp_path)
    cache.update(
        "600000.SH",
        _bars(["2024-01-02", "2024-01-03"]),
        "2024-01-01",
        "2024-01-05",
        source="fixture",
    )
    assert cache.missing_ranges("600000.SH", "2024-01-01", "2024-01-10") == [
        (pd.Timestamp("2024-01-06"), pd.Timestamp("2024-01-10"))
    ]
    cache.update(
        "600000.SH",
        _bars(["2024-01-08", "2024-01-09"]),
        "2024-01-08",
        "2024-01-10",
        source="fixture",
    )
    assert cache.missing_ranges("600000.SH", "2024-01-01", "2024-01-10") == [
        (pd.Timestamp("2024-01-06"), pd.Timestamp("2024-01-07"))
    ]
    metadata = json.loads(cache.metadata_path("600000.SH").read_text("utf-8"))
    assert metadata["row_count"] == 4
    assert len(metadata["content_sha256"]) == 64
    assert not list(tmp_path.glob("*.tmp-*"))


def test_later_cache_update_replaces_an_overlapping_observation(tmp_path):
    cache = ParquetBarCache(tmp_path)
    cache.update(
        "600000.SH",
        _bars(["2024-01-02"], closes=[10.0]),
        "2024-01-02",
        "2024-01-02",
        source="fixture",
    )
    cache.update(
        "600000.SH",
        _bars(["2024-01-02"], closes=[12.0]),
        "2024-01-02",
        "2024-01-02",
        source="fixture",
    )
    loaded = cache.load("600000.SH")
    assert len(loaded) == 1
    assert loaded.loc[0, "close"] == 12.0


def test_empty_successful_response_still_marks_range_covered(tmp_path):
    cache = ParquetBarCache(tmp_path)
    cache.update(
        "600000.SH",
        empty_bars(),
        "2024-01-06",
        "2024-01-07",
        source="fixture",
    )
    assert cache.missing_ranges("600000.SH", "2024-01-06", "2024-01-07") == []
    assert cache.load("600000.SH").empty


def test_update_from_provider_only_fetches_uncovered_ranges(tmp_path):
    cache = ParquetBarCache(tmp_path)

    class Provider:
        source_version = "fixture"

        def __init__(self):
            self.calls = []

        def get_daily_bars(self, symbol, start, end):
            self.calls.append((symbol, pd.Timestamp(start), pd.Timestamp(end)))
            return _bars([pd.Timestamp(start)])

    provider = Provider()
    cache.update_from_provider(
        provider, "600000.SH", "2024-01-01", "2024-01-05"
    )
    cache.update_from_provider(
        provider, "600000.SH", "2024-01-01", "2024-01-08"
    )
    assert provider.calls == [
        (
            "600000.SH",
            pd.Timestamp("2024-01-01"),
            pd.Timestamp("2024-01-05"),
        ),
        (
            "600000.SH",
            pd.Timestamp("2024-01-06"),
            pd.Timestamp("2024-01-08"),
        ),
    ]


def test_fully_covered_cache_rejects_changed_provider_contract(tmp_path):
    cache = ParquetBarCache(tmp_path)

    class Provider:
        def __init__(self, version):
            self.version = version
            self.calls = []

        def cache_source_version(self, symbol, is_index):
            return self.version

        def get_daily_bars(self, symbol, start, end):
            self.calls.append((symbol, start, end))
            return _bars([pd.Timestamp(start)])

    original = Provider("raw+hfq-v1")
    cache.update_from_provider(
        original, "600000.SH", "2024-01-01", "2024-01-05"
    )
    changed = Provider("raw+qfq-v1")

    with pytest.raises(CacheError, match="cache source identity mismatch"):
        cache.update_from_provider(
            changed, "600000.SH", "2024-01-01", "2024-01-05"
        )

    assert changed.calls == []


def test_direct_update_never_mixes_different_sources(tmp_path):
    cache = ParquetBarCache(tmp_path)
    cache.update(
        "600000.SH",
        _bars(["2024-01-02"]),
        "2024-01-02",
        "2024-01-02",
        source="old-provider",
        source_version="v1",
    )

    with pytest.raises(CacheError, match="cache source identity mismatch"):
        cache.update(
            "600000.SH",
            _bars(["2024-01-03"]),
            "2024-01-03",
            "2024-01-03",
            source="new-provider",
            source_version="v2",
        )

    assert cache.load("600000.SH")["date"].tolist() == [
        pd.Timestamp("2024-01-02")
    ]


@pytest.mark.parametrize("missing", ["metadata", "parquet"])
def test_cache_rejects_incomplete_artifact_pair(tmp_path, missing):
    cache = ParquetBarCache(tmp_path)
    symbol = "600000.SH"
    if missing == "metadata":
        _bars(["2024-01-02"]).to_parquet(cache.data_path(symbol), index=False)
    else:
        cache.metadata_path(symbol).write_text(
            json.dumps(
                {
                    "metadata_version": cache.metadata_version,
                    "generation_id": "a" * 32,
                    "data_file": symbol + "." + "a" * 32 + ".parquet",
                    "symbol": symbol,
                    "source": "fixture",
                    "source_version": "v1",
                    "content_sha256": "0" * 64,
                    "covered_ranges": [],
                }
            ),
            encoding="utf-8",
        )

    with pytest.raises(CacheError, match="incomplete cache artifact"):
        cache.load(symbol)


def test_load_requires_complete_expected_identity(tmp_path):
    cache = ParquetBarCache(tmp_path)
    with pytest.raises(CacheError, match="must be provided together"):
        cache.load("600000.SH", expected_source="fixture")


@pytest.mark.parametrize(
    "field,value,match",
    [
        ("metadata_version", 1, "unsupported cache metadata version"),
        ("row_count", 99, "row_count mismatch"),
        ("data_end", "2024-01-03", "data bounds mismatch"),
        ("content_sha256", None, "no valid content_sha256"),
    ],
)
def test_load_rejects_unverifiable_metadata(tmp_path, field, value, match):
    cache = ParquetBarCache(tmp_path)
    symbol = "600000.SH"
    cache.update(
        symbol,
        _bars(["2024-01-02"]),
        "2024-01-02",
        "2024-01-02",
        source="fixture",
        source_version="v1",
    )
    metadata = json.loads(cache.metadata_path(symbol).read_text(encoding="utf-8"))
    if value is None:
        metadata.pop(field)
    else:
        metadata[field] = value
    cache.metadata_path(symbol).write_text(
        json.dumps(metadata), encoding="utf-8"
    )

    with pytest.raises(CacheError, match=match):
        cache.load(symbol)
