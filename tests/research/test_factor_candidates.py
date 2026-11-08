"""Candidate contracts and temporal isolation on fixed-seed artificial observations."""

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from examples.research.dataset_registry_v2_demo import synthetic_inputs
from examples.research.factor_candidates_demo import experiment, prepare, splitter
from factorlab.dataset.identity import json_hash
from factorlab.exploratory.factor_discovery.contracts import DiscoveryError, Node
from factorlab.exploratory.factor_discovery.engine import compile_expression
from factorlab.factors.candidates import candidate_catalog, catalog_records
from factorlab.errors import SchemaError
from factorlab.validation.candidate_research import variation_diagnostics, summarize_candidates


def test_catalog_unique_registered_and_pinned():
    builder, baseline, selector = experiment()
    rows = candidate_catalog()
    assert len(rows) == len(selector.factor_refs) == 64
    assert len(baseline) == 14
    assert len({r["definition"].definition_hash for r in rows}) == 64
    assert len({r["family"] for r in rows}) == 16
    assert candidate_catalog() == rows
    for row in rows:
        definition = row["definition"]
        assert builder.snapshot.get(definition.factor_id, definition.version) == definition
        assert definition.lookback <= 60
        assert definition.availability == "SESSION_CLOSE"
    catalog = json.loads((Path(__file__).parents[2] / "docs/validation/p3-11/catalog.json").read_text())
    assert json_hash(catalog_records()) == json_hash(catalog)


@pytest.mark.parametrize("node", [Node("Eval"), Node("Field", value="future_close"),
    Node("Lag", (Node("Field", value="adj_close"),), param=-1)])
def test_illegal_expression_rejected(node):
    with pytest.raises(DiscoveryError):
        compile_expression(node)


def test_absent_fields_are_not_available():
    rows = catalog_records(["adj_close"])
    assert any(r["status"] == "NOT_AVAILABLE" for r in rows)
    assert all(r["status"] == "NOT_AVAILABLE" for r in rows if "turnover" in r["input_fields"])
    builder, _, selector = experiment()
    dates, bars, *_ = synthetic_inputs(sessions=80, symbols=24)
    ref = next(r for r in selector.factor_refs if r.factor_id == "C_turnover_level_5")
    with pytest.raises(SchemaError):
        builder.snapshot.compute(bars.drop(columns="turnover"), (ref,), calendar=dates)


def test_all_candidates_execute_causally_without_fill():
    builder, _, selector = experiment()
    dates, bars, *_ = synthetic_inputs(sessions=160, symbols=24)
    refs = selector.factor_refs
    original = builder.snapshot.compute(bars, refs, calendar=dates)
    assert original.loc[original.date.ge(dates[65]), list(selector.feature_names)].notna().any().all()
    future = bars.copy()
    future.loc[future.date.gt(dates[120]), "volume"] *= 10
    prices = [name for name in bars if name.startswith(("adj_", "raw_"))]
    future.loc[future.date.gt(dates[120]), prices] *= 1.5
    changed = builder.snapshot.compute(future, refs, calendar=dates)
    pd.testing.assert_frame_equal(original.loc[original.date.le(dates[120])],
                                  changed.loc[changed.date.le(dates[120])])
    missing = bars.copy()
    symbol = original.loc[original.date.eq(dates[100]) & original.C_momentum_5.notna()
                          & original.C_volume_surprise_5.notna(), "symbol"].iloc[0]
    missing.loc[missing.date.eq(dates[100]) & missing.symbol.eq(symbol), "volume"] = np.nan
    result = builder.snapshot.compute(missing, refs, calendar=dates)
    row = result.loc[result.date.eq(dates[100]) & result.symbol.eq(symbol)].iloc[0]
    assert np.isnan(row.C_volume_surprise_5)
    assert np.isnan(row.C_volume_dispersion_5)
    assert not np.isnan(row.C_momentum_5)


@pytest.fixture(scope="module")
def built():
    builder, _, selector = experiment()
    dates, bars, benchmark, members, status, listings = synthetic_inputs(sessions=300, symbols=24)
    return builder.build(bars, benchmark, members, status, listings, dates), members.date.drop_duplicates(), selector


def test_selection_is_train_only_and_deterministic(built):
    data, calendar, selector = built
    prepared, folds, selections, plans, *_ = prepare(data, calendar, selector)
    assert len(folds) >= 2
    fold = folds[0]
    train = prepared.iloc[fold.train_indices]
    assert train.exit_date.max() < fold.audit.validation_first_date
    actual = selector.select(train)
    assert actual.receipt == selections[fold.fold].receipt
    shuffled = selector.select(train.sample(frac=1, random_state=311))
    assert actual.receipt == shuffled.receipt
    assert len(actual.feature_names) <= 10
    modified = prepared.copy()
    modified.loc[modified.index[fold.test_indices], "EXCESS_RET5"] *= -100
    # Later folds may legitimately train on dates that were earlier Test.
    repeated = selector.select(modified.iloc[fold.train_indices])
    assert actual.receipt == repeated.receipt
    assert actual.receipt["eligible_count"] <= 64
    assert not actual.redundancy.empty


def test_train_variation_marks_constants_and_illegal_values():
    frame = pd.DataFrame({"date": np.repeat(pd.date_range("2020-01-01", periods=3), 200),
                          "constant": 1., "low": np.tile([0.] * 199 + [1.], 3), "bad": np.inf})
    diagnostic = variation_diagnostics(frame, ("constant", "low", "bad")).set_index("factor_id")
    assert diagnostic.loc["constant", "status"] == "CONSTANT"
    assert diagnostic.loc["low", "status"] == "LOW_VARIATION"
    assert diagnostic.loc["bad", "status"] == "NOT_DETERMINED"


def test_common_sample_mismatch_rejected():
    data = pd.DataFrame([{"fold": 0, "date": "2020-01-01", "group": group, "model": "ridge",
        "rows": 24, "paired_rows": 24, "keys_sha256": key, "labels_sha256": "label",
        "finite_pairs_sha256": "finite", "rank_ic": .1} for group, key in (("fixed64", "a"), ("selected", "b"))])
    with pytest.raises(ValueError, match="common samples"):
        summarize_candidates(data)
