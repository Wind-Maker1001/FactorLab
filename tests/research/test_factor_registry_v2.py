"""Software-only identities, compatibility, and temporal isolation for V2."""

from dataclasses import FrozenInstanceError, replace
import json

import numpy as np
import pandas as pd
import pytest

from factorlab.errors import ConfigurationError, SchemaError
from factorlab.exploratory.factor_discovery.contracts import DiscoveryConfig, DiscoveryError, Node
from factorlab.exploratory.factor_discovery.engine import ExpressionEngine, compile_expression
from factorlab.exploratory.factor_discovery.synthetic import make_synthetic_data
from factorlab.factors.base import Factor
from factorlab.factors.registry import FACTOR_REGISTRY, compute_factors, get_factor
from factorlab.factors.registry_v2 import (
    FactorDataContext, FactorDefinition, FactorRef, FactorRegistryV2,
)


@pytest.fixture
def data():
    bars, _, dates = make_synthetic_data(stock_count=3, sessions=85)
    return bars, dates


def expression(field="turnover", lag=1):
    return compile_expression(Node("Lag", (Node("Field", value=field),), param=lag),
                              require_dimensionless=False)


def test_legacy_fourteen_public_interfaces_and_exact_v2_results(data, monkeypatch):
    bars, dates = data
    bars = bars.sample(frac=1, random_state=42)
    bars.index = np.repeat(np.arange(len(bars) // 3), 3)  # V2 uses keys, not caller index.
    original = bars.copy(deep=True)
    legacy = compute_factors(bars.reset_index(drop=True), list(FACTOR_REGISTRY))
    registry = FactorRegistryV2.with_builtins()
    refs = [registry.get(name).ref for name in FACTOR_REGISTRY]

    def no_per_factor_validation(*args):
        raise AssertionError("V2 must prepare a shared panel once")

    monkeypatch.setattr(Factor, "validate", no_per_factor_validation)
    actual = registry.compute(bars, refs, calendar=dates)
    pd.testing.assert_frame_equal(actual, legacy[actual.columns], check_exact=True)
    pd.testing.assert_frame_equal(bars, original, check_exact=True)
    assert list(FACTOR_REGISTRY) == [ref.factor_id for ref in refs]
    assert get_factor("mom20") is FACTOR_REGISTRY["MOM20"]
    assert get_factor("MOM20").lookback == 20
    assert tuple(registry.get("MOM20").input_fields) == ("adj_close",)
    assert registry.get("VOL20").lookback == 20
    assert registry.get("TURN20").lookback == 19


def test_stable_hash_content_version_and_atomic_conflict():
    a = FactorDefinition.builtin("MOM20")
    same = FactorDefinition.builtin("MOM20", version="2", factor_id="momentum")
    assert a.definition_hash == same.definition_hash
    assert a.ref != same.ref
    assert a.definition_hash != FactorDefinition.builtin("MOM60").definition_hash
    with pytest.raises(FrozenInstanceError):
        a.version = "2"
    registry = FactorRegistryV2()
    registry.register(a)
    old = registry.freeze()
    with pytest.raises(ConfigurationError, match="Duplicate"):
        registry.register(a)
    conflict = FactorDefinition.builtin("MOM60", factor_id="MOM20")
    with pytest.raises(ConfigurationError, match="version conflict"):
        registry.register_many((same, conflict))
    assert registry.freeze() == old
    registry.register(replace(a, version="2"))
    with pytest.raises(ConfigurationError, match="ambiguous"):
        registry.get("MOM20")
    assert registry.get("MOM20", "1") == a
    assert len(old.definitions) == 1
    with pytest.raises(FrozenInstanceError):
        old.definitions = ()
    reverse = FactorRegistryV2()
    reverse.register_many(reversed(registry.freeze().definitions))
    assert reverse.freeze() == registry.freeze()


def test_dependency_order_pins_missing_cycles_and_output_selection(data):
    bars, dates = data
    base = FactorDefinition.builtin("MOM20")
    alias = FactorDefinition.alias("momentum_alias", "1", base)
    outer = FactorDefinition.alias("outer", "1", alias)
    registry = FactorRegistryV2()
    with pytest.raises(ConfigurationError, match="Missing dependency"):
        registry.register(alias)
    assert registry.freeze().definitions == ()
    registry.register_many((outer, alias, base))
    assert registry.resolve((outer.ref,)) == (base, alias, outer)
    result = registry.compute(bars, (outer.ref,), calendar=dates)
    assert list(result) == ["date", "symbol", "outer"]
    np.testing.assert_allclose(result["outer"], compute_factors(bars, ["MOM20"])["MOM20"],
                               equal_nan=True)
    with pytest.raises(ConfigurationError, match="hash conflict"):
        registry.resolve((replace(base.ref, definition_hash="0" * 64),))
    wrong_alias = replace(alias, dependencies=(replace(base.ref, definition_hash="0" * 64),))
    with pytest.raises(ConfigurationError, match="hash conflict"):
        FactorRegistryV2().register_many((base, wrong_alias))
    a = replace(alias, factor_id="a", dependencies=(FactorRef("b", "1", "0" * 64),))
    b = replace(alias, factor_id="b", dependencies=(FactorRef("a", "1", "0" * 64),))
    with pytest.raises(ConfigurationError, match="cycle"):
        FactorRegistryV2().register_many((a, b))
    self_cycle = replace(a, dependencies=(FactorRef("a", "1", "0" * 64),))
    with pytest.raises(ConfigurationError, match="cycle"):
        FactorRegistryV2().register(self_cycle)
    with pytest.raises(ConfigurationError, match="Duplicate output"):
        registry.compute(bars, (base.ref, base.ref), calendar=dates)


@pytest.mark.parametrize("change", [
    {"input_fields": ("label",)}, {"input_fields": ("RET1",)},
    {"input_fields": ("adj_close", "adj_close")}, {"input_fields": ["adj_close"]},
    {"lookback": -1}, {"lookback": True}, {"frequency": "1min"},
    {"price_basis": ("QFQ",)}, {"availability": "FUTURE"},
    {"factor_id": "date"}, {"factor_id": "adj_close"}, {"factor_id": "__import__"},
    {"version": ""}, {"dependencies": []},
])
def test_illegal_metadata_rejected(change):
    with pytest.raises(ConfigurationError):
        replace(FactorDefinition.builtin("MOM20"), **change)


@pytest.mark.parametrize("change", [
    {"lookback": 0}, {"input_fields": ("volume",)}, {"price_basis": ("RAW",)},
    {"implementation": "os:system"}, {"expression_json": "print('unsafe')"},
])
def test_metadata_cannot_override_executable_semantics(change):
    with pytest.raises(ConfigurationError):
        FactorRegistryV2().register(replace(FactorDefinition.builtin("MOM20"), **change))


def test_controlled_adapter_revalidates_all_compiled_metadata_and_defends_mutation(data):
    bars, dates = data
    compiled = expression()
    definition = FactorDefinition.from_expression("discovered", "1", compiled)
    registry = FactorRegistryV2()
    registry.register(definition)
    expected = ExpressionEngine(bars, dates).compute(compiled).values
    actual = registry.compute(bars, (definition.ref,), calendar=dates)
    np.testing.assert_allclose(actual["discovered"], expected, equal_nan=True)
    for change in ({"expression_sha256": "0" * 64}, {"lookback": 0},
                   {"canonical_ast": {}}, {"canonical_json": "{}"},
                   {"expression": "__import__('os')"}, {"complexity": {}}):
        with pytest.raises(ConfigurationError):
            registry.register_expression("bad", "1", replace(compiled, **change))
    for value in ("__import__('os')", lambda frame: frame, Node("Field", value="turnover")):
        with pytest.raises(ConfigurationError):
            registry.register_expression("bad", "1", value)
    compiled.canonical_ast["op"] = "Call"
    again = registry.compute(bars, (definition.ref,), calendar=dates)
    pd.testing.assert_frame_equal(actual, again, check_exact=True)
    with pytest.raises((ConfigurationError, DiscoveryError)):
        registry.register(replace(definition, expression_json='{"op":"Call","args":[]}'))
    # RET1 always comes from historical close; supplied RET1/labels are ignored.
    ret = FactorDefinition.from_expression("derived_return", "1", compile_expression(
        Node("Field", value="RET1")))
    registry.register(ret)
    bars["RET1"], bars["label"] = -999, "do not read"
    result = registry.compute(bars, (ret.ref,), calendar=dates)
    np.testing.assert_allclose(result["derived_return"],
                               bars.groupby("symbol")["adj_close"].pct_change(fill_method=None),
                               equal_nan=True)


def test_missing_suspended_rows_and_warmup_do_not_compress_calendar(data):
    bars, dates = data
    registry = FactorRegistryV2.with_builtins()
    # Delete a session entirely for one security; keep an explicit NaN for another.
    bars = bars.loc[~(bars["symbol"].eq("000001.SZ") & bars["date"].eq(dates[5]))].copy()
    bars.loc[bars["symbol"].eq("000002.SZ") & bars["date"].eq(dates[5]), "turnover"] = np.nan
    bars.loc[bars["symbol"].eq("000003.SZ") & bars["date"].eq(dates[5]),
             ["volume", "amount", "turnover"]] = 0.0
    refs = [registry.get(name).ref for name in ("REV1", "TURN5", "VOLUME_RATIO", "AMIHU20")]
    lag = registry.register_expression("lag_turnover", "1", expression())
    result = registry.compute(bars.sample(frac=1, random_state=10), refs + [lag], calendar=dates)
    assert len(result) == len(bars)
    for symbol in ("000001.SZ", "000002.SZ"):
        selected = result.set_index(["symbol", "date"]).loc[symbol]
        assert pd.isna(selected.loc[dates[6], "lag_turnover"])
        assert selected.loc[dates[6:10], "TURN5"].isna().all()
        assert pd.notna(selected.loc[dates[10], "TURN5"])
        assert selected.loc[dates[:4], "TURN5"].isna().all()
        assert pd.notna(selected.loc[dates[4], "TURN5"])
    assert pd.isna(result.set_index(["symbol", "date"]).loc[("000001.SZ", dates[6]), "REV1"])
    zero = result.set_index(["symbol", "date"]).loc["000003.SZ"]
    assert zero.loc[dates[20:25], "AMIHU20"].isna().all()


def test_future_prefix_and_cross_security_isolation_for_every_builtin_and_expression(data):
    bars, dates = data
    registry = FactorRegistryV2.with_builtins()
    discovered = registry.register_expression("discovered", "1", expression())
    refs = [d.ref for d in registry.freeze().definitions]
    assert discovered in refs
    result = registry.compute(bars, refs, calendar=dates)
    changed = bars.copy()
    mask = changed["symbol"].eq("000002.SZ") & changed["date"].gt(dates[65])
    for name in ("adj_open", "adj_high", "adj_low", "adj_close", "raw_open", "raw_high",
                 "raw_low", "raw_close", "volume", "amount", "turnover"):
        changed.loc[mask, name] *= 7
    altered = registry.compute(changed, refs, calendar=dates)
    unchanged = result["date"].le(dates[65]) | result["symbol"].ne("000002.SZ")
    pd.testing.assert_frame_equal(result.loc[unchanged], altered.loc[unchanged], check_exact=True)
    prefix = registry.compute(bars.loc[bars["date"].le(dates[65])], refs, calendar=dates[:66])
    pd.testing.assert_frame_equal(result.loc[result["date"].le(dates[65])], prefix, check_exact=True)
    for symbol in bars["symbol"].unique():
        isolated = registry.compute(bars.loc[bars["symbol"].eq(symbol)], refs, calendar=dates)
        expected = result.loc[result["symbol"].eq(symbol)].reset_index(drop=True)
        pd.testing.assert_frame_equal(expected, isolated, check_exact=True)


@pytest.mark.parametrize("case", ["duplicate", "null_key", "bad_date", "outside", "intraday",
                                 "columns", "numeric_field", "inf_field", "missing_field"])
def test_bad_inputs_fail_closed(data, case):
    bars, dates = data
    if case == "duplicate":
        bars = pd.concat([bars, bars.iloc[:1]])
    elif case == "null_key":
        bars.loc[0, "symbol"] = None
    elif case == "bad_date":
        bars.loc[0, "date"] = pd.NaT
    elif case == "outside":
        dates = dates[1:]
    elif case == "intraday":
        bars.loc[0, "date"] += pd.Timedelta(hours=1)
    elif case == "columns":
        bars.columns = ["symbol" if c == "amount" else c for c in bars.columns]
    elif case == "numeric_field":
        bars["adj_close"] = "10"
    elif case == "inf_field":
        bars.loc[0, "adj_close"] = np.inf
    else:
        bars = bars.drop(columns="adj_close")
    registry = FactorRegistryV2.with_builtins()
    with pytest.raises(SchemaError):
        registry.compute(bars, (registry.get("MOM20").ref,), calendar=dates)


def test_budget_failure_is_explicit_and_run_evidence_is_not_definition_identity(data):
    bars, dates = data
    registry = FactorRegistryV2()
    ref = registry.register_expression("lagged", "1", expression())
    with pytest.raises(ConfigurationError, match="BUDGET_EXCEEDED"):
        registry.compute(bars, (ref,), calendar=dates,
                         expression_config=replace(DiscoveryConfig(), max_cell_operations=1))
    first = registry.compute(bars, (ref,), calendar=dates,
                             context=FactorDataContext("synthetic-a", "caller claim"))
    second = registry.compute(bars, (ref,), calendar=dates,
                              context=FactorDataContext("synthetic-b", "different evidence"))
    a, b = first.attrs["factor_registry_v2"], second.attrs["factor_registry_v2"]
    assert a["factors"] == b["factors"]
    assert a["snapshot_hash"] == b["snapshot_hash"]
    assert a["data_context"] != b["data_context"]
    for record in (a, b):
        context = record["data_context"]
        assert context["no_live_money"] is True
        assert context["pit_status"] == "PIT_UNVERIFIED"
        assert context["hfq_status"] == "HFQ_UNVERIFIED"
        assert context["admission_status"] == "FORMAL_ADMISSION_BLOCKED"
    assert json.loads(json.dumps(a)) == a


@pytest.mark.parametrize("case", ["reversed", "duplicate", "null", "numeric", "timezone"])
def test_invalid_calendar_rejected(data, case):
    bars, dates = data
    if case == "reversed":
        dates = dates[::-1]
    elif case == "duplicate":
        dates = dates.append(dates[:1])
    elif case == "null":
        dates = dates.insert(0, pd.NaT)
    elif case == "numeric":
        bars["date"], dates = 0, [0]
    else:
        dates = dates.tz_localize("UTC")
    registry = FactorRegistryV2.with_builtins()
    with pytest.raises(SchemaError):
        registry.compute(bars, (registry.get("MOM20").ref,), calendar=dates)


def test_hundred_approved_definitions_batch_and_receipt():
    from examples.research.factor_registry_v2_demo import run_benchmark

    receipt = run_benchmark(stocks=3, sessions=65)
    assert receipt["definitions_registered"] == receipt["unique_definition_hashes"] == 100
    assert receipt["factor_columns"] == 100 and receipt["output_rows"] == 195
    assert receipt["deterministic_reordered_input_exact"] is True
    assert all(value > 0 for value in receipt["finite_values_per_factor"].values())
    assert receipt["process_peak_rss_bytes"] > 0
    assert receipt["data_context"]["admission_status"] == "FORMAL_ADMISSION_BLOCKED"


def test_nullable_numeric_missing_values_remain_numeric_and_unfilled(data):
    bars, dates = data
    bars["turnover"] = bars["turnover"].astype("Float64")
    bars.loc[0, "turnover"] = pd.NA
    registry = FactorRegistryV2.with_builtins()
    result = registry.compute(bars, (registry.get("TURN5").ref,), calendar=dates)
    selected = result.loc[result["symbol"].eq("000001.SZ"), "TURN5"].reset_index(drop=True)
    assert selected.iloc[:5].isna().all()
    assert pd.notna(selected.iloc[5])
    assert result["TURN5"].dtype == np.dtype("float64")
