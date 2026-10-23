"""Strict, offline contracts for date-effective security status evidence."""

import json
from datetime import datetime, timezone

import pandas as pd
import numpy as np
import pytest

import qbot.research.universe.status as status_module
from qbot.research.errors import SecurityStatusDataError
from qbot.research.universe.status import (
    BAOSTOCK_SECURITY_SOURCE_VERSION,
    import_security_data,
    load_verified_security_data,
    publish_security_data,
    validate_security_data,
)
from qbot.research.universe.csi500 import dataframe_sha256, validate_daily_membership


def _universe_sha256(membership):
    return dataframe_sha256(
        validate_daily_membership(membership, expected_member_count=None)
    )


@pytest.fixture
def calendar():
    return pd.DatetimeIndex(pd.to_datetime(["2024-01-02", "2024-01-03", "2024-01-04"]))


@pytest.fixture
def membership(calendar):
    return pd.DataFrame(
        {
            "date": [calendar[0], calendar[1], calendar[1], calendar[2]],
            "symbol": ["600000.SH", "600000.SH", "000001.SZ", "000001.SZ"],
            "is_member": [True, True, True, True],
        }
    )


@pytest.fixture
def listings():
    return pd.DataFrame(
        {
            "symbol": ["600000.SH", "000001.SZ"],
            "listing_date": pd.to_datetime(["1999-11-10", "2024-01-03"]),
        }
    )


@pytest.fixture
def status(calendar):
    # 600000 was already listed at the start of the supplied calendar, while
    # 000001 starts on its listing session.  Membership entry is deliberately
    # later than the status coverage start for the first symbol.
    return pd.DataFrame(
        {
            "date": list(calendar) + list(calendar[1:]),
            "symbol": ["600000.SH"] * 3 + ["000001.SZ"] * 2,
            "trade_status": [1, 1, 0, 1, 1],
            "is_st": [0, 0, 0, 0, 1],
        }
    )


def test_validate_security_data_canonicalises_and_sorts(
    status, listings, calendar, membership
):
    clean_status, clean_listings = validate_security_data(
        status.sample(frac=1.0, random_state=42),
        listings.iloc[::-1],
        calendar=calendar,
        membership=membership,
    )

    assert clean_status.columns.tolist() == [
        "date",
        "symbol",
        "trade_status",
        "is_st",
    ]
    assert clean_listings.columns.tolist() == ["symbol", "listing_date"]
    assert clean_status.equals(clean_status.sort_values(["date", "symbol"]).reset_index(drop=True))
    assert clean_status["trade_status"].dtype.kind in "iu"
    assert clean_status["is_st"].dtype == bool
    assert clean_status["symbol"].drop_duplicates().tolist() == [
        "600000.SH",
        "000001.SZ",
    ]


def test_validate_accepts_nullable_boolean_values_from_baostock(
    status, listings, calendar, membership
):
    status = status.copy()
    status["is_st"] = pd.Series(
        [np.bool_(value) for value in status["is_st"]], dtype="boolean"
    )

    clean_status, _ = validate_security_data(
        status, listings, calendar=calendar, membership=membership
    )

    assert clean_status["is_st"].dtype == bool


@pytest.mark.parametrize(
    "target,mutate",
    [
        ("status", lambda frame: frame.assign(unexpected="x")),
        ("listings", lambda frame: frame.assign(unexpected="x")),
        ("status", lambda frame: frame.drop(columns="trade_status")),
        ("listings", lambda frame: frame.drop(columns="listing_date")),
    ],
)
def test_validate_requires_exact_columns(
    status, listings, calendar, membership, target, mutate
):
    if target == "status":
        status = mutate(status)
    else:
        listings = mutate(listings)
    with pytest.raises(SecurityStatusDataError, match="SECURITY STATUS DATA REQUIRED"):
        validate_security_data(
            status, listings, calendar=calendar, membership=membership
        )


@pytest.mark.parametrize(
    "target,column,value",
    [
        ("status", "date", "2024-01-02 12:00:00"),
        ("status", "date", 20240102),
        ("status", "date", datetime(2024, 1, 2, tzinfo=timezone.utc)),
        ("listings", "listing_date", "2024-01-02 12:00:00"),
        ("listings", "listing_date", 20240102),
        ("listings", "listing_date", datetime(2024, 1, 2, tzinfo=timezone.utc)),
    ],
)
def test_validate_rejects_non_date_values(
    status, listings, calendar, membership, target, column, value
):
    frame = status if target == "status" else listings
    frame = frame.copy()
    frame[column] = frame[column].astype(object)
    frame.loc[0, column] = value
    if target == "status":
        status = frame
    else:
        listings = frame
    with pytest.raises(SecurityStatusDataError, match="SECURITY STATUS DATA REQUIRED"):
        validate_security_data(
            status, listings, calendar=calendar, membership=membership
        )


@pytest.mark.parametrize("column,bad_value", [("trade_status", 2), ("is_st", -1), ("is_st", "yes")])
def test_validate_rejects_non_binary_flags(
    status, listings, calendar, membership, column, bad_value
):
    status = status.copy()
    status[column] = status[column].astype(object)
    status.loc[0, column] = bad_value
    with pytest.raises(SecurityStatusDataError, match="SECURITY STATUS DATA REQUIRED"):
        validate_security_data(
            status, listings, calendar=calendar, membership=membership
        )


@pytest.mark.parametrize("target", ["status", "listings"])
def test_validate_rejects_duplicate_keys(
    status, listings, calendar, membership, target
):
    if target == "status":
        status = pd.concat([status, status.iloc[[0]]], ignore_index=True)
    else:
        listings = pd.concat([listings, listings.iloc[[0]]], ignore_index=True)
    with pytest.raises(SecurityStatusDataError, match="SECURITY STATUS DATA REQUIRED"):
        validate_security_data(
            status, listings, calendar=calendar, membership=membership
        )


@pytest.mark.parametrize("bad_symbol", ["sh.600000", "600000", "AAPL"])
def test_validate_requires_canonical_symbols(
    status, listings, calendar, membership, bad_symbol
):
    status = status.copy()
    status.loc[status["symbol"] == "600000.SH", "symbol"] = bad_symbol
    with pytest.raises(SecurityStatusDataError, match="SECURITY STATUS DATA REQUIRED"):
        validate_security_data(
            status, listings, calendar=calendar, membership=membership
        )


def test_validate_requires_one_listing_row_for_every_pit_ever_symbol(
    status, listings, calendar, membership
):
    with pytest.raises(SecurityStatusDataError, match="SECURITY STATUS DATA REQUIRED"):
        validate_security_data(
            status,
            listings.iloc[[0]],
            calendar=calendar,
            membership=membership,
        )

    extra = pd.concat(
        [
            listings,
            pd.DataFrame(
                {"symbol": ["000002.SZ"], "listing_date": [pd.Timestamp("2000-01-01")]}
            ),
        ],
        ignore_index=True,
    )
    with pytest.raises(SecurityStatusDataError, match="SECURITY STATUS DATA REQUIRED"):
        validate_security_data(status, extra, calendar=calendar, membership=membership)


def test_validate_rejects_listing_after_first_membership(
    status, listings, calendar, membership
):
    listings = listings.copy()
    listings.loc[listings["symbol"] == "000001.SZ", "listing_date"] = calendar[2]
    with pytest.raises(SecurityStatusDataError, match="SECURITY STATUS DATA REQUIRED"):
        validate_security_data(
            status, listings, calendar=calendar, membership=membership
        )


def test_validate_requires_exact_status_coverage_without_inference(
    status, listings, calendar, membership
):
    missing = status.drop(index=status.index[1])
    with pytest.raises(SecurityStatusDataError, match="SECURITY STATUS DATA REQUIRED"):
        validate_security_data(
            missing, listings, calendar=calendar, membership=membership
        )

    non_session = status.copy()
    non_session.loc[0, "date"] = pd.Timestamp("2024-01-01")
    with pytest.raises(SecurityStatusDataError, match="SECURITY STATUS DATA REQUIRED"):
        validate_security_data(
            non_session, listings, calendar=calendar, membership=membership
        )

    extra_symbol = pd.concat(
        [
            status,
            pd.DataFrame(
                {
                    "date": [calendar[0]],
                    "symbol": ["000002.SZ"],
                    "trade_status": [1],
                    "is_st": [0],
                }
            ),
        ],
        ignore_index=True,
    )
    with pytest.raises(SecurityStatusDataError, match="SECURITY STATUS DATA REQUIRED"):
        validate_security_data(
            extra_symbol, listings, calendar=calendar, membership=membership
        )


def test_validate_requires_pit_keys_but_not_post_exit_rows(
    status, listings, calendar, membership
):
    pit_only = membership[["date", "symbol"]].merge(
        status,
        on=["date", "symbol"],
        how="left",
        validate="one_to_one",
    )

    clean_status, _ = validate_security_data(
        pit_only,
        listings,
        calendar=calendar,
        membership=membership,
    )

    assert len(clean_status) == len(membership)
    assert not (
        clean_status["date"].eq(calendar[2])
        & clean_status["symbol"].eq("600000.SH")
    ).any()


def test_publish_and_verified_load_bind_artifacts_to_pit_and_calendar(
    tmp_path, status, listings, calendar, membership
):
    manifest = publish_security_data(
        status,
        listings,
        output_dir=tmp_path,
        calendar=calendar,
        membership=membership,
        universe_sha256=_universe_sha256(membership),
        source="BaoStockPointInTimeProvider",
        source_version=BAOSTOCK_SECURITY_SOURCE_VERSION,
    )

    assert (tmp_path / "security_status.parquet").exists()
    assert (tmp_path / "listing_dates.parquet").exists()
    assert json.loads(
        (tmp_path / "security_data_manifest.json").read_text(encoding="utf-8")
    ) == manifest
    loaded_status, loaded_listings, loaded_manifest = load_verified_security_data(
        tmp_path,
        calendar=calendar,
        membership=membership,
        universe_sha256=_universe_sha256(membership),
        expected_provider="baostock",
    )
    pd.testing.assert_frame_equal(
        loaded_status,
        validate_security_data(
            status, listings, calendar=calendar, membership=membership
        )[0],
    )
    pd.testing.assert_frame_equal(
        loaded_listings,
        validate_security_data(
            status, listings, calendar=calendar, membership=membership
        )[1],
    )
    assert loaded_manifest == manifest

    with pytest.raises(SecurityStatusDataError, match="SECURITY STATUS DATA REQUIRED"):
        load_verified_security_data(
            tmp_path,
            calendar=calendar,
            membership=membership,
            universe_sha256="d" * 64,
            expected_provider="baostock",
        )
    with pytest.raises(SecurityStatusDataError, match="SECURITY STATUS DATA REQUIRED"):
        load_verified_security_data(
            tmp_path,
            calendar=calendar[:-1],
            membership=membership.loc[membership["date"].isin(calendar[:-1])],
            universe_sha256=_universe_sha256(membership),
            expected_provider="baostock",
        )


@pytest.mark.parametrize("artifact_key", ["status_file", "listings_file"])
def test_verified_load_rejects_tampered_parquet(
    tmp_path, status, listings, calendar, membership, artifact_key
):
    manifest = publish_security_data(
        status,
        listings,
        output_dir=tmp_path,
        calendar=calendar,
        membership=membership,
        universe_sha256=_universe_sha256(membership),
        source="BaoStockPointInTimeProvider",
        source_version=BAOSTOCK_SECURITY_SOURCE_VERSION,
    )
    path = tmp_path / manifest[artifact_key]
    path.write_bytes(path.read_bytes() + b"tampered")
    with pytest.raises(SecurityStatusDataError, match="SECURITY STATUS DATA REQUIRED"):
        load_verified_security_data(
            tmp_path,
            calendar=calendar,
            membership=membership,
            universe_sha256=_universe_sha256(membership),
        )


def test_verified_load_rejects_tampered_manifest(
    tmp_path, status, listings, calendar, membership
):
    publish_security_data(
        status,
        listings,
        output_dir=tmp_path,
        calendar=calendar,
        membership=membership,
        universe_sha256=_universe_sha256(membership),
        source="BaoStockPointInTimeProvider",
        source_version=BAOSTOCK_SECURITY_SOURCE_VERSION,
    )
    path = tmp_path / "security_data_manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["calendar_sha256"] = "0" * 64
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(SecurityStatusDataError, match="SECURITY STATUS DATA REQUIRED"):
        load_verified_security_data(
            tmp_path,
            calendar=calendar,
            membership=membership,
            universe_sha256=_universe_sha256(membership),
        )


def test_verified_load_rejects_tampered_local_input_hash(
    tmp_path, status, listings, calendar, membership
):
    status_path = tmp_path / "status.csv"
    listings_path = tmp_path / "listings.csv"
    status.assign(date=status["date"].dt.strftime("%Y-%m-%d")).to_csv(
        status_path, index=False
    )
    listings.assign(
        listing_date=listings["listing_date"].dt.strftime("%Y-%m-%d")
    ).to_csv(listings_path, index=False)
    output = tmp_path / "published"
    import_security_data(
        status_path,
        listings_path,
        output_dir=output,
        calendar=calendar,
        membership=membership,
        universe_sha256=_universe_sha256(membership),
    )
    manifest_path = output / "security_data_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["input_sha256"]["status"] = "f" * 64
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(SecurityStatusDataError, match="security_data_version"):
        load_verified_security_data(
            output,
            calendar=calendar,
            membership=membership,
            universe_sha256=_universe_sha256(membership),
            expected_provider="local",
        )


def test_failed_generation_publish_preserves_previous_verified_bundle(
    tmp_path, status, listings, calendar, membership, monkeypatch
):
    universe_sha256 = _universe_sha256(membership)
    previous = publish_security_data(
        status,
        listings,
        output_dir=tmp_path,
        calendar=calendar,
        membership=membership,
        universe_sha256=universe_sha256,
        source="BaoStockPointInTimeProvider",
        source_version=BAOSTOCK_SECURITY_SOURCE_VERSION,
    )
    real_atomic_parquet = status_module._atomic_parquet
    calls = {"count": 0}

    def fail_second_parquet(frame, destination):
        calls["count"] += 1
        if calls["count"] == 2:
            raise OSError("injected listing write failure")
        return real_atomic_parquet(frame, destination)

    monkeypatch.setattr(status_module, "_atomic_parquet", fail_second_parquet)
    changed = status.copy()
    changed.loc[0, "is_st"] = 1 - int(changed.loc[0, "is_st"])
    with pytest.raises(SecurityStatusDataError, match="cannot publish"):
        publish_security_data(
            changed,
            listings,
            output_dir=tmp_path,
            calendar=calendar,
            membership=membership,
            universe_sha256=universe_sha256,
            source="BaoStockPointInTimeProvider",
            source_version=BAOSTOCK_SECURITY_SOURCE_VERSION,
        )

    _, _, loaded_manifest = load_verified_security_data(
        tmp_path,
        calendar=calendar,
        membership=membership,
        universe_sha256=universe_sha256,
        expected_provider="baostock",
    )
    assert loaded_manifest["generation_id"] == previous["generation_id"]


def test_import_security_data_records_both_input_hashes_and_round_trips(
    tmp_path, status, listings, calendar, membership
):
    status_path = tmp_path / "status.csv"
    listings_path = tmp_path / "listings.csv"
    status.assign(date=status["date"].dt.strftime("%Y-%m-%d")).to_csv(
        status_path, index=False
    )
    listings.assign(
        listing_date=listings["listing_date"].dt.strftime("%Y-%m-%d")
    ).to_csv(listings_path, index=False)
    output = tmp_path / "published"

    manifest = import_security_data(
        status_path,
        listings_path,
        output_dir=output,
        calendar=calendar,
        membership=membership,
        universe_sha256=_universe_sha256(membership),
    )

    assert manifest["source"] == "local-import"
    assert manifest["source_version"] == "user-security-data-import-v1"
    assert set(manifest["input_sha256"]) == {"status", "listings"}
    assert manifest["input_files"] == {
        "status": str(status_path.resolve()),
        "listings": str(listings_path.resolve()),
    }
    loaded_status, loaded_listings, _ = load_verified_security_data(
        output,
        calendar=calendar,
        membership=membership,
        universe_sha256=_universe_sha256(membership),
        expected_provider="local",
    )
    assert len(loaded_status) == len(status)
    assert len(loaded_listings) == len(listings)
