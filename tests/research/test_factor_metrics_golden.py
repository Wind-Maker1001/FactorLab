"""Differential contract tests against an immutable pre-optimization evaluator."""

import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from qbot.research.metrics.factor import evaluate_factors
from tests.research.reference import factor_reference as reference
from tests.research.reference.compare import ATOL, RTOL, TABLES, assert_evaluation_equal


def golden_input(case="normal"):
    rng = np.random.default_rng(20261011)
    dates = pd.bdate_range("2024-01-02", periods=5)
    size = 27
    frame = pd.DataFrame({
        "date": np.repeat(dates.to_numpy(), size),
        "symbol": np.tile(["%06d.SZ" % i for i in range(size)], len(dates)),
        "F1": rng.normal(size=size * len(dates)),
        "F2": rng.normal(size=size * len(dates)),
        "EXCESS_RET5": rng.normal(scale=0.02, size=size * len(dates)),
        "REGIME": np.repeat(["BULL", "BEAR", "BULL", "SIDEWAYS", "BEAR"], size),
        "unrelated_audit": "must not be mutated",
    })
    if case == "missing":
        frame.loc[::3, "F1"] = np.nan
        frame.loc[::4, "F2"] = np.nan
        frame.loc[::5, "EXCESS_RET5"] = np.nan
    elif case == "light_missing":
        # Both pairs remain above the minimum, with equal counts but different
        # masks: target rank caching must use actual members, not just n.
        frame.loc[frame.symbol.eq("000001.SZ"), "F1"] = np.nan
        frame.loc[frame.symbol.eq("000002.SZ"), "F2"] = np.nan
        frame.loc[frame.symbol.eq("000003.SZ"), "EXCESS_RET5"] = np.nan
    elif case == "infinity":
        frame.loc[::3, "F1"] = np.inf
        frame.loc[1::4, "F2"] = -np.inf
        frame.loc[::7, "EXCESS_RET5"] = np.inf
    elif case == "constant":
        frame["F1"] = 0.0
        frame.loc[frame.date.eq(dates[2]), "F2"] = 7.0
    elif case == "ties":
        frame["F1"] = np.tile(np.repeat([0., 1., 2.], 9), len(dates))
        frame["F2"] = 0.0
    elif case == "pool_change":
        frame = frame.loc[~(frame.date.eq(dates[1]) & frame.symbol.isin(frame.symbol.unique()[:8]))]
        frame = frame.loc[~(frame.date.eq(dates[2]) & frame.symbol.isin(frame.symbol.unique()[-8:]))]
    elif case == "absent_session":
        frame = frame.loc[~frame.date.eq(dates[2])]
    elif case == "invalid_session":
        frame.loc[frame.date.eq(dates[2]), ["F1", "F2"]] = np.nan
    elif case == "unmatured_tail":
        frame.loc[frame.date.ge(dates[3]), "EXCESS_RET5"] = np.nan
    elif case == "missing_regime":
        frame.loc[frame.date.eq(dates[2]), "REGIME"] = None
    elif case == "partial_regime":
        frame.loc[(frame.date.eq(dates[2])) & (frame.symbol.eq("000001.SZ")), "REGIME"] = None
    elif case == "all_missing":
        frame[["F1", "F2", "EXCESS_RET5"]] = np.nan
    elif case == "offset":
        frame["F1"] = 1e12 + np.arange(len(frame)) * 0.001
    elif case == "extreme":
        frame["F1"] *= 1e300
        frame["EXCESS_RET5"] *= 1e300
    elif case == "cancellation":
        frame["EXCESS_RET5"] = np.resize([1e100, 1., -1e100], len(frame))
    elif case == "single_date":
        frame = frame.loc[frame.date.eq(dates[2])]
        dates = dates[2:3]
    elif case == "shuffled":
        frame = frame.sample(frac=1, random_state=42)
        frame.index = np.arange(len(frame)) // 2  # repeated index is not a repeated primary key
    elif case == "calendar_edges":
        dates = pd.bdate_range("2024-01-01", periods=7)
    elif case == "float32":
        frame[["F1", "F2", "EXCESS_RET5"]] = frame[["F1", "F2", "EXCESS_RET5"]].astype("float32")
    elif case == "nullable":
        frame["F1"] = frame.F1.astype("Float64")
        frame.loc[::5, "F1"] = pd.NA
    elif case == "null_symbol":
        frame.loc[frame.symbol.eq("000001.SZ"), "symbol"] = None
    elif case == "near_zero_std":
        x = np.linspace(-1.0, 1.0, size)
        frame["F1"] = np.tile(x, len(dates))
        frame["EXCESS_RET5"] = frame.F1 + rng.normal(scale=1e-7, size=len(frame))
    elif case == "categorical_regime":
        frame["REGIME"] = pd.Categorical(frame.REGIME, categories=["ALL", "BEAR", "BULL", "NEVER", "SIDEWAYS"])
    elif case == "literal_all_regime":
        frame["REGIME"] = "ALL"
    return frame, dates


CASES = ("normal", "missing", "light_missing", "infinity", "constant", "ties", "pool_change",
         "absent_session", "invalid_session", "unmatured_tail", "missing_regime",
         "partial_regime", "all_missing", "offset", "extreme", "cancellation",
         "single_date", "shuffled", "calendar_edges", "float32", "nullable", "null_symbol",
         "near_zero_std", "categorical_regime", "literal_all_regime")


def test_frozen_reference_checksum_and_comparison_policy():
    path = Path(reference.__file__)
    meta = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
    assert hashlib.sha256(path.read_bytes()).hexdigest() == meta["source_sha256"]
    assert meta["comparison_atol"] == ATOL and meta["comparison_rtol"] == RTOL


@pytest.mark.parametrize("case", CASES)
@pytest.mark.parametrize("grouped", [False, True])
def test_evaluation_matches_frozen_reference_and_does_not_mutate_input(case, grouped):
    frame, dates = golden_input(case)
    original = frame.copy(deep=True)
    options = dict(quantiles=5, minimum_cross_section=20, calendar=dates,
                   group_column="REGIME" if grouped else None)
    with np.errstate(over="ignore", invalid="ignore", divide="ignore"):
        expected = reference.evaluate_factors(frame, ["F2", "F1"], **options)
        actual = evaluate_factors(frame, ["F2", "F1"], **options)
    assert_evaluation_equal(actual, expected)
    pd.testing.assert_frame_equal(frame, original, check_exact=True)


@pytest.mark.parametrize("size", [4, 5, 6, 7, 19, 20, 21, 26, 27])
@pytest.mark.parametrize("quantiles", [2, 3, 5])
def test_cross_section_and_bucket_boundaries(size, quantiles):
    frame, dates = golden_input("ties")
    frame = frame.loc[frame.symbol.isin(frame.symbol.unique()[:size])]
    options = dict(quantiles=quantiles, minimum_cross_section=max(5, quantiles), calendar=dates)
    assert_evaluation_equal(evaluate_factors(frame, ["F1", "F2"], **options),
                            reference.evaluate_factors(frame, ["F1", "F2"], **options))


@pytest.mark.parametrize("mutation", [
    lambda f, d: (pd.concat([f, f.iloc[[0]]]), d),
    lambda f, d: (f.drop(columns="F1"), d),
    lambda f, d: (f.assign(date=None), d),
    lambda f, d: (f, d[::-1]),
    lambda f, d: (f, d[:-1]),
    lambda f, d: (f.assign(REGIME=np.where(np.arange(len(f)) == 0, "BAD", f.REGIME)), d),
])
def test_failure_contract_matches_reference(mutation):
    frame, dates = mutation(*golden_input())
    options = dict(group_column="REGIME", calendar=dates)
    with pytest.raises(Exception) as before:
        reference.evaluate_factors(frame, ["F1", "F2"], **options)
    with pytest.raises(type(before.value)) as after:
        evaluate_factors(frame, ["F1", "F2"], **options)
    assert str(after.value) == str(before.value)


@pytest.mark.parametrize("empty", ["rows", "factors"])
def test_empty_input_preserves_reference_exception(empty):
    frame, dates = golden_input()
    names = ["F1", "F2"]
    if empty == "rows":
        frame = frame.iloc[:0]
    else:
        names = []
    with pytest.raises(Exception) as before:
        reference.evaluate_factors(frame, names, calendar=dates)
    with pytest.raises(type(before.value)) as after:
        evaluate_factors(frame, names, calendar=dates)
    assert str(after.value) == str(before.value)


def test_labels_never_change_quantile_selection_or_temporal_metrics():
    frame, dates = golden_input("ties")
    changed = frame.copy()
    changed["EXCESS_RET5"] = np.nan
    before, after = (evaluate_factors(f, ["F1", "F2"], calendar=dates) for f in (frame, changed))
    pd.testing.assert_frame_equal(before.quantile_returns.drop(columns=["future_return", "labeled_observations", "label_coverage"]),
                                  after.quantile_returns.drop(columns=["future_return", "labeled_observations", "label_coverage"]), check_exact=True)
    pd.testing.assert_frame_equal(before.autocorrelation, after.autocorrelation, check_exact=True)
    pd.testing.assert_frame_equal(before.turnover, after.turnover, check_exact=True)


def test_stored_golden_tables_and_member_traces():
    root = Path(__file__).parent / "fixtures" / "factor_golden"
    meta = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    for relative, digest in meta["sha256"].items():
        assert hashlib.sha256((root / relative).read_bytes()).hexdigest() == digest
    for case in ("normal", "ties", "pool_change"):
        frame, dates = golden_input(case)
        expected = reference.FactorEvaluation(**{name: pd.read_parquet(root / case / (name + ".parquet")) for name in TABLES})
        assert_evaluation_equal(evaluate_factors(frame, ["F2", "F1"], calendar=dates, group_column="REGIME"), expected)
        from qbot.research.metrics._factor_arrays import _quantile_codes, _top_positions
        for trace in json.loads((root / case / "traces.json").read_text(encoding="utf-8")):
            cross = frame.loc[frame.date.eq(pd.Timestamp(trace["date"]))]
            values = cross[trace["factor"]].to_numpy()
            np.testing.assert_array_equal(_quantile_codes(values, 5, {}), trace["bucket_codes"])
            assert cross.iloc[_top_positions(values, 5)].symbol.tolist() == trace["turnover_symbols"]
            np.testing.assert_array_equal(cross[trace["factor"]].rank(method="average"), trace["average_ranks"])


@pytest.mark.parametrize("size,quantiles", [(n, q) for n in range(2, 65) for q in (2, 3, 5, 7) if n >= q])
def test_ordinal_qcut_and_stable_top_members_match_original_primitives(size, quantiles):
    from qbot.research.metrics._factor_arrays import _quantile_codes, _top_positions
    values = np.random.default_rng(size).integers(-3, 4, size=size).astype(float)
    series = pd.Series(values, index=np.arange(size) // 2)
    expected = pd.qcut(series.rank(method="first"), q=quantiles, labels=False)
    np.testing.assert_array_equal(_quantile_codes(values, quantiles, {}), expected)
    expected_top = pd.DataFrame({"position": np.arange(size), "factor": values}).nlargest(
        int(np.ceil(size / quantiles)), "factor", keep="first").position.to_numpy()
    np.testing.assert_array_equal(_top_positions(values, quantiles), expected_top)
