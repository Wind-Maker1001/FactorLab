"""P2-06 integration boundaries; no changes to historical financial oracles."""

from dataclasses import replace
import json

import numpy as np
import pandas as pd
import pytest

from examples.research.dataset_registry_v2_demo import run_demo, synthetic_inputs
from factorlab.dataset import DatasetBuildOptions, DatasetBuilder
from factorlab.dataset.identity import IDENTITY_ATTR, seal
from factorlab.errors import ConfigurationError, SchemaError, PointInTimeUniverseError
from factorlab.exploratory.factor_discovery.contracts import Node
from factorlab.exploratory.factor_discovery.engine import compile_expression
from factorlab.factors.registry import FACTOR_REGISTRY
from factorlab.factors.registry_v2 import FactorDataContext, FactorDefinition, FactorRegistryV2
from factorlab.universe.filters import UniverseFilterConfig


@pytest.fixture
def inputs():
    return synthetic_inputs(sessions=100, symbols=4)


def builder(names=("REV1", "MOM20"), *, registry=None, refs=None, **kwargs):
    snapshot = (registry or FactorRegistryV2.with_builtins()).freeze()
    return DatasetBuilder(
        DatasetBuildOptions(
            expected_member_count=4, minimum_cross_section=2,
            filter_config=UniverseFilterConfig(min_listed_days=1, zero_volume_window=1,
                                               quality_window=1, min_quality_ratio=0.0),
        ), snapshot=snapshot,
        factor_refs=refs if refs is not None else [snapshot.get(name, "1").ref for name in names],
        data_context=FactorDataContext("synthetic", "close-known"),
        data_version="seed-v1", calendar_id="synthetic-bday-v1", **kwargs,
    )


def build(config, inputs):
    dates, *frames = inputs
    return config.build(*frames, dates)


def test_all_14_builtins_exact_legacy_dataset_equivalence(inputs):
    config = builder(tuple(FACTOR_REGISTRY))
    legacy = DatasetBuilder(replace(config.options, factor_names=tuple(FACTOR_REGISTRY)))
    old, new = build(legacy, inputs), build(config, inputs)
    pd.testing.assert_frame_equal(new, old, check_exact=True)
    assert [n for n in new if n in FACTOR_REGISTRY] == list(FACTOR_REGISTRY)
    assert new.filter(like="raw_")["raw_MOM60"].notna().all()


def test_discovered_expression_and_pinned_dependency_enter_dataset(inputs):
    registry = FactorRegistryV2.with_builtins()
    expression = registry.register_expression("lag_return", "2", compile_expression(
        Node("Lag", (Node("Field", value="RET1"),), param=1),
    ))
    alias = registry.register(FactorDefinition.alias("alias_rev", "7", registry.get("REV1", "1")))
    config = builder(registry=registry, refs=[expression, alias])
    result = build(config, inputs)
    assert [n for n in result if n in config.factor_names] == ["lag_return", "alias_rev"]
    dates, bars, *_ = inputs
    prices = bars.pivot(index="date", columns="symbol", values="adj_close").reindex(dates)
    expected = prices.pct_change(fill_method=None).shift(1).stack().rename("expected")
    actual = result.set_index(["date", "symbol"])["raw_lag_return"]
    pd.testing.assert_series_equal(actual, expected.reindex(actual.index), check_names=False, check_exact=True)
    assert result.lag_return.notna().all()
    entered = result.dropna(subset=["entry_date"])
    assert entered.entry_date.gt(entered.date).all()
    mature = result.dropna(subset=["exit_date"])
    assert mature.exit_date.gt(mature.entry_date).all()
    assert result.attrs[IDENTITY_ATTR]["factors"][0]["version"] == "2"


def test_shuffle_warmup_missing_sessions_and_security_isolation(inputs):
    dates, bars, benchmark, membership, status, listings = inputs
    missing = (bars.date == dates[70]) & (bars.symbol == "000001.SZ")
    sparse = bars.loc[~missing].sample(frac=1, random_state=5)
    config = builder()
    result = build(config, (dates, sparse, benchmark, membership, status, listings))
    affected = result[result.symbol.eq("000001.SZ")].set_index("date")
    # Missing-volume day itself is removed by the existing eligibility filter.
    assert dates[70] not in affected.index
    assert pd.isna(affected.loc[dates[71], "raw_REV1"])
    assert pd.isna(affected.loc[dates[90], "raw_MOM20"])
    assert np.isfinite(affected.loc[dates[65], "raw_MOM20"])
    ordered = build(config, (dates, sparse.sort_values(["date", "symbol"]), benchmark, membership, status, listings))
    pd.testing.assert_frame_equal(result, ordered, check_exact=True)
    changed = sparse.copy()
    changed.loc[changed.symbol.eq("000001.SZ"), "adj_close"] *= 17
    altered = build(config, (dates, changed, benchmark, membership, status, listings))
    other = result.symbol.ne("000001.SZ")
    pd.testing.assert_frame_equal(result.loc[other, ["raw_REV1", "raw_MOM20"]],
                                  altered.loc[other, ["raw_REV1", "raw_MOM20"]], check_exact=True)


def test_future_changes_do_not_change_past_factors_or_eligibility(inputs):
    dates, bars, benchmark, membership, status, listings = inputs
    config = builder()
    baseline = build(config, inputs)
    changed = bars.copy()
    changed.loc[changed.date > dates[80], ["adj_close", "adj_open", "volume"]] *= 1.5
    result = build(config, (dates, changed, benchmark, membership, status, listings))
    columns = ["date", "symbol", *config.factor_names, "raw_REV1", "raw_MOM20"]
    past = baseline.date <= dates[80]
    pd.testing.assert_frame_equal(baseline.loc[past, columns], result.loc[past, columns], check_exact=True)
    # Labels deliberately change when their future exit price changes.
    assert not baseline.EXCESS_RET5.equals(result.EXCESS_RET5)
    assert result.groupby("symbol").tail(6).EXCESS_RET5.isna().all()


@pytest.mark.parametrize("name", ["entry_date", "exit_date", "EXCESS_RET5", "EXCESS_RET99",
                                  "STOCK_FWD_RET5", "BENCHMARK_FWD_RET5", "raw_REV1",
                                  "INDEX_RET5", "MARKET_REGIME", "score", "rank", "fold"])
def test_protected_factor_ids_rejected(name):
    registry = FactorRegistryV2()
    ref = registry.register(FactorDefinition.builtin("REV1", factor_id=name))
    with pytest.raises(ConfigurationError, match="protected"):
        builder(registry=registry, refs=[ref])


@pytest.mark.parametrize("kind", ["hash", "version", "duplicate", "unversioned"])
def test_invalid_pins_rejected(kind):
    registry = FactorRegistryV2.with_builtins()
    ref = registry.get("REV1", "1").ref
    refs = {"hash": [replace(ref, definition_hash="0" * 64)],
            "version": [replace(ref, version="404")], "duplicate": [ref, ref],
            "unversioned": ["REV1"]}[kind]
    with pytest.raises(ConfigurationError):
        builder(registry=registry, refs=refs)


def test_missing_dependency_rejected():
    target = FactorDefinition.builtin("REV1")
    with pytest.raises(ConfigurationError, match="Missing dependency"):
        FactorRegistryV2().register(FactorDefinition.alias("alias", "1", target))


@pytest.mark.parametrize("kind", ["no_snapshot", "mutable_registry", "no_refs", "empty_refs", "bad_refs",
                                  "no_context", "no_version", "no_calendar_id", "name_mismatch", "same_open"])
def test_v2_configuration_must_be_explicit(kind):
    registry = FactorRegistryV2.with_builtins()
    options = DatasetBuildOptions()
    kwargs = dict(snapshot=registry.freeze(), factor_refs=[registry.get("REV1", "1").ref],
                  data_context=FactorDataContext(), data_version="v1", calendar_id="v1")
    if kind == "no_snapshot":
        kwargs.pop("snapshot")
    elif kind == "mutable_registry":
        kwargs["snapshot"] = registry
    elif kind == "no_refs":
        kwargs.pop("factor_refs")
    elif kind == "empty_refs":
        kwargs["factor_refs"] = []
    elif kind == "bad_refs":
        kwargs["factor_refs"] = 123
    elif kind == "no_context":
        kwargs.pop("data_context")
    elif kind == "no_version":
        kwargs.pop("data_version")
    elif kind == "no_calendar_id":
        kwargs.pop("calendar_id")
    elif kind == "name_mismatch":
        options = replace(options, factor_names=["MOM20"])
    else:
        options = replace(options, entry_price="same_open")
    with pytest.raises(ConfigurationError):
        DatasetBuilder(options, **kwargs)


@pytest.mark.parametrize("kind", ["duplicate_columns", "duplicate_keys", "numeric_dates", "off_calendar",
                                  "input_collision", "raw_collision", "input_label", "bad_symbol", "infinite", "string_values"])
def test_invalid_inputs_rejected(inputs, kind):
    dates, bars, *rest = inputs
    bars = bars.copy()
    if kind == "duplicate_columns":
        bars = pd.concat([bars, bars[["adj_close"]]], axis=1)
    elif kind == "duplicate_keys":
        bars = pd.concat([bars, bars.iloc[:1]])
    elif kind == "numeric_dates":
        bars["date"] = 1
    elif kind == "off_calendar":
        bars.loc[0, "date"] = dates[-1] + pd.Timedelta(days=10)
    elif kind == "input_collision":
        bars["REV1"] = 1
    elif kind == "raw_collision":
        bars["raw_REV1"] = 1
    elif kind == "input_label":
        bars["EXCESS_RET5"] = 1
    elif kind == "bad_symbol":
        bars.loc[0, "symbol"] = None
    elif kind == "infinite":
        bars.loc[0, "adj_close"] = np.inf
    else:
        bars["adj_close"] = "12"
    with pytest.raises((SchemaError, ConfigurationError)):
        build(builder(), (dates, bars, *rest))


@pytest.mark.parametrize("kind", ["empty", "numeric", "duplicate", "unordered", "intraday"])
def test_invalid_calendar_rejected(inputs, kind):
    dates, *rest = inputs
    calendar = {"empty": [], "numeric": [1, 2], "duplicate": dates.append(dates[:1]),
                "unordered": dates[::-1], "intraday": dates + pd.Timedelta(hours=1)}[kind]
    with pytest.raises(SchemaError):
        build(builder(), (calendar, *rest))


@pytest.mark.parametrize("kind", ["missing_members", "missing_status", "all_st"])
def test_existing_pit_and_status_gates_preserved(inputs, kind):
    dates, bars, benchmark, membership, status, listings = inputs
    if kind == "missing_members":
        membership = membership[membership.date.ne(dates[80])]
    elif kind == "missing_status":
        status = status[status.date.ne(dates[80])]
    else:
        status = status.assign(is_st=True)
    if kind == "missing_status":
        result = build(builder(), (dates, bars, benchmark, membership, status, listings))
        assert dates[80] not in result.date.values
    else:
        with pytest.raises((SchemaError, PointInTimeUniverseError)):
            build(builder(), (dates, bars, benchmark, membership, status, listings))


def test_sidecar_round_trip_and_boundaries(inputs, tmp_path):
    config = builder()
    dataset = build(config, inputs)
    path, features = tmp_path / "dataset.parquet", tmp_path / "features"
    sidecar = config.write_artifacts(dataset, path, features)
    identity = json.loads(sidecar.read_text())
    loaded = config.read_artifacts(path, features, calendar=inputs[0],
                                   expected_identity_hash=identity["identity_hash"],
                                   expected_input_hashes=dataset.attrs[IDENTITY_ATTR]["input_hashes"])
    pd.testing.assert_frame_equal(loaded, dataset, check_exact=True)
    assert identity["data_context"]["pit_status"] == "PIT_UNVERIFIED"
    assert identity["data_context"]["hfq_status"] == "HFQ_UNVERIFIED"
    assert identity["data_context"]["admission_status"] == "FORMAL_ADMISSION_BLOCKED"
    assert identity["data_context"]["no_live_money"] is True
    assert set(identity["artifacts"]) == {"dataset", "raw_features", "processed_features"}


@pytest.mark.parametrize("kind", ["missing", "definition", "version", "order", "calendar", "calendar_id",
                                  "data_version", "input_hash", "status", "dataset", "raw", "processed", "anchor",
                                  "checksum", "invalid_json"])
def test_sidecar_tamper_or_mismatch_rejected(inputs, tmp_path, kind):
    config = builder()
    dataset = build(config, inputs)
    path, features = tmp_path / "dataset.parquet", tmp_path / "features"
    sidecar = config.write_artifacts(dataset, path, features)
    identity = json.loads(sidecar.read_text())
    calendar = inputs[0]
    extra = {}
    if kind == "missing":
        sidecar.unlink()
    elif kind == "invalid_json":
        sidecar.write_text("{invalid")
    elif kind == "checksum":
        identity["data_version"] = "changed-without-sealing"
        sidecar.write_text(json.dumps(identity))
    elif kind in ("dataset", "raw", "processed"):
        target = {"dataset": path, "raw": features / "raw_factors.parquet",
                  "processed": features / "processed_factors.parquet"}[kind]
        frame = pd.read_parquet(target)
        frame.iloc[0, -1] = 12345.0
        frame.to_parquet(target, index=False)
    elif kind == "calendar":
        calendar = calendar.delete(4)
    elif kind == "calendar_id":
        config.calendar_id = "different-calendar"
    elif kind == "data_version":
        config.data_version = "v2"
    elif kind == "anchor":
        extra["expected_identity_hash"] = "0" * 64
    elif kind == "input_hash":
        extra["expected_input_hashes"] = {"bars": "0" * 64}
    else:
        identity.pop("identity_hash")
        if kind == "order":
            identity["factors"].reverse()
        elif kind == "status":
            identity["data_context"]["pit_status"] = "VERIFIED"
        elif kind == "definition":
            identity["factors"][0]["definition_hash"] = "0" * 64
        else:
            identity["factors"][0]["version"] = "999"
        sidecar.write_text(json.dumps(seal(identity)))
    with pytest.raises(SchemaError):
        config.read_artifacts(path, features, calendar=calendar, **extra)


def test_stale_build_receipt_cannot_be_written(inputs, tmp_path):
    config = builder()
    dataset = build(config, inputs)
    dataset.loc[0, "REV1"] = 999.0
    with pytest.raises(SchemaError, match="changed"):
        config.write_artifacts(dataset, tmp_path / "dataset.parquet", tmp_path / "features")


def test_four_models_train_on_v2_dataset_and_walk_forward(tmp_path):
    manifest = run_demo(tmp_path)
    assert manifest["sidecar_verified"]
    for name, model in manifest["models"].items():
        assert model["prediction_rows"] > 0
        assert model["folds"] >= 2
        assert model["feature_names"] == ["MOM20", "FD_001", "pinned_reversal"]
        assert model["fit_scope"] == ["train_only"]
        assert np.isfinite(model["prediction_rank_ic_mean"])
        assert len(model["model_sha256"]) == model["folds"]
        assert model["discovered_feature_score_delta"] > 0
        assert (tmp_path / name / "predictions.csv").exists()
