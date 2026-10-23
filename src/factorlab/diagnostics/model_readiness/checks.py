"""Independent assertions and adversarial scenarios around existing implementations."""

from contextlib import contextmanager, ExitStack
from dataclasses import replace
from unittest.mock import patch
import weakref

import numpy as np
import pandas as pd

from factorlab.metrics.robustness import (
    RobustnessError, RobustnessOptions, holm_adjust, mean_uncertainty,
)
from factorlab.validation import ExpandingWalkForwardSplitter
import factorlab.validation.walk_forward as wf

from .synthetic import FEATURES, TARGET, build_dataset, make_runner, split_options


def require(condition, reason):
    if not condition:
        raise AssertionError(reason)


def assert_labels(inputs, dataset):
    dates = inputs.calendar
    entry = pd.Series(dates, index=dates).shift(-1)
    exit_ = pd.Series(dates, index=dates).shift(-6)
    pd.testing.assert_series_equal(dataset.entry_date, dataset.date.map(entry), check_names=False)
    pd.testing.assert_series_equal(dataset.exit_date, dataset.date.map(exit_), check_names=False)
    # Independent price lookup rather than invoking the production label builder.
    prices = inputs.bars.pivot(index="date", columns="symbol", values="adj_open")
    benchmark = inputs.benchmark.set_index("date").adj_open
    expected = prices.shift(-6) / prices.shift(-1) - 1
    expected = expected.sub(benchmark.shift(-6) / benchmark.shift(-1) - 1, axis=0)
    expected.index.name, expected.columns.name = "date", "symbol"
    expected_values = expected.to_numpy()[
        expected.index.get_indexer(dataset.date), expected.columns.get_indexer(dataset.symbol)]
    np.testing.assert_allclose(dataset[TARGET], expected_values, rtol=1e-12, atol=1e-12, equal_nan=True)
    require(dataset.loc[dataset.date.isin(dates[-6:]), TARGET].isna().all(), "immature tail labels exist")


def assert_folds(dataset, calendar, size="small", **overrides):
    options = dict(split_options(size), **overrides)
    folds = list(ExpandingWalkForwardSplitter(**options).split(dataset, calendar=calendar))
    tests = set()
    for fold in folds:
        train, validation, test = [dataset.iloc[indices] for indices in fold.as_indices()]
        require(train.exit_date.max() < validation.date.min(), "train labels cross validation boundary")
        require(validation.exit_date.max() < test.date.min(), "validation labels cross test boundary")
        require(not test.exit_date.isna().any(), "immature label window in Test")
        groups = [set(frame.index) for frame in (train, validation, test)]
        require(not (groups[0] & groups[1] or groups[0] & groups[2] or groups[1] & groups[2]),
                "partition rows overlap")
        require(not tests.intersection(groups[2]), "Test repeated across folds")
        tests.update(groups[2])
        for frame in (train, validation, test):
            require((frame.date < frame.entry_date).all() and (frame.entry_date < frame.exit_date).all(),
                    "invalid label window")
    return folds


@contextmanager
def observe_model_calls(model, dataset, folds, recorder):
    """Assert actual model call cohorts, including candidates and selected diagnostics."""
    classes = {
        "factor_score": wf.FactorScoreModel, "ridge": wf.RidgeResearchModel,
        "lightgbm": wf.LightGBMResearchModel, "xgboost": wf.XGBoostResearchModel,
    }
    cls = classes[model]
    real_fit, real_predict = cls.fit, cls.predict
    states, counts = {}, {fold.fold: {"fit": 0, "validation": 0, "test": 0, "train": 0} for fold in folds}
    expected = {}
    for fold in folds:
        train = dataset.iloc[fold.train_indices]
        expected[fold.fold] = {
            "fit": train.loc[train[TARGET].notna()], "train": train,
            "validation": dataset.iloc[fold.validation_indices], "test": dataset.iloc[fold.test_indices],
        }

    def fit(instance, X, y=None):
        matching = [number for number, groups in expected.items() if X.index.equals(groups["fit"].index)]
        require(len(matching) == 1, "fit cohort is not exactly a mature Train")
        number = matching[0]
        pd.testing.assert_frame_equal(X, expected[number]["fit"].loc[:, FEATURES])
        pd.testing.assert_series_equal(y, expected[number]["fit"][TARGET])
        # Weak references avoid retaining discarded candidates in the resource
        # benchmark; identity is checked explicitly against possible id reuse.
        states[id(instance)] = (number, weakref.ref(instance))
        counts[number]["fit"] += 1
        with recorder.span("train", model, fold=number, rows=len(X)):
            return real_fit(instance, X, y)

    def predict(instance, X):
        number, reference = states[id(instance)]
        require(reference() is instance, "predict instance has no matching fit")
        roles = [role for role in ("train", "validation", "test")
                 if X.index.equals(expected[number][role].index)]
        # Fully labeled training cohorts have the same indices as fit; role is still Train diagnostic.
        require(len(roles) == 1, "predict cohort is unknown or crosses folds")
        role = roles[0]
        pd.testing.assert_frame_equal(X, expected[number][role].loc[:, FEATURES])
        counts[number][role] += 1
        with recorder.span("predict", model, fold=number, role=role, rows=len(X)):
            return real_predict(instance, X)

    with ExitStack() as stack:
        stack.enter_context(patch.object(cls, "fit", fit))
        stack.enter_context(patch.object(cls, "predict", predict))
        yield counts
    candidates = 1 if model == "factor_score" else 2
    for count in counts.values():
        require(count == {"fit": candidates, "validation": candidates, "test": 1, "train": 1},
                "candidate fit/selection or single Test predict contract violated: %s" % count)


def future_probes(inputs, dataset, baseline_results, size="small", seed=42):
    """Change future bars/labels and compare only predictions made by the cutoff.

    Historical labels whose exits occur after cutoff SHOULD change; they must
    never have entered historical fits or validation selection at that point.
    """
    folds = assert_folds(dataset, inputs.research_calendar, size)
    cutoff = folds[0].test_dates[-1]
    future = inputs.bars.date > cutoff
    altered_bars = inputs.bars.copy()
    numeric = [name for name in altered_bars if name not in ("date", "symbol")]
    altered_bars.loc[future, numeric] *= 7
    benchmark = inputs.benchmark.copy()
    benchmark.loc[benchmark.date > cutoff, ["adj_open", "adj_close"]] *= 3
    changed = build_dataset(replace(inputs, bars=altered_bars, benchmark=benchmark))
    historical = dataset.date <= cutoff
    feature_columns = ["date", "symbol"] + list(FEATURES) + ["raw_" + name for name in FEATURES] + [
        name for name in dataset if name.startswith("INDEX_") or name == "MARKET_REGIME"]
    pd.testing.assert_frame_equal(dataset.loc[historical, feature_columns], changed.loc[historical, feature_columns])
    require(not dataset[TARGET].equals(changed[TARGET]), "future price mutation did not change labels")
    altered_labels = dataset.copy()
    # Includes labels with historical signal dates but exits unavailable at cutoff.
    unknown = altered_labels.exit_date > cutoff
    altered_labels.loc[unknown, TARGET] = np.arange(unknown.sum()) * 1000.0
    evidence = {}
    for model, baseline in baseline_results.items():
        reference = baseline.predictions.loc[baseline.predictions.date <= cutoff, ["date", "symbol", "fold", "score", "selected_parameters"]].reset_index(drop=True)
        require(len(reference) > 0, "empty historical perturbation comparison")
        for scenario, frame in (("future_prices", changed), ("future_labels", altered_labels)):
            rerun = make_runner(model, size, seed).run(frame, calendar=inputs.research_calendar)
            actual = rerun.predictions.loc[rerun.predictions.date <= cutoff, reference.columns].reset_index(drop=True)
            pd.testing.assert_frame_equal(reference, actual, check_exact=True)
            evidence[model + "/" + scenario] = len(actual)
    return {"cutoff": cutoff.isoformat(), "unchanged_prediction_rows": evidence}


def statistical_probes():
    """Call existing estimators; no alternative inference implementation."""
    options = RobustnessOptions(bootstrap_repetitions=100)
    values = np.sin(np.arange(240) / 15.0) * .1 + .01
    result = mean_uncertainty(values, signal_dates=len(values), options=options)
    iid_se = values.std(ddof=0) / np.sqrt(len(values))
    require(result["hac_standard_error"] > iid_se * 2, "positive autocorrelation not reflected in HAC")
    repeated = mean_uncertainty(values, signal_dates=len(values), options=options)
    require(result == repeated, "Moving Block Bootstrap does not reproduce")
    missing = values.copy()
    missing[::2] = np.nan
    coverage = mean_uncertainty(missing, signal_dates=len(values), options=options)
    short = mean_uncertainty(values[:59], signal_dates=59, options=options)
    constant = mean_uncertainty(np.ones(240), signal_dates=240, options=options)
    require(coverage["status"] == "INSUFFICIENT_COVERAGE", "missing dates silently accepted")
    require(short["status"] == "INSUFFICIENT_DATES", "small sample silently accepted")
    require(constant["status"] == "DEGENERATE_VARIANCE", "constant series invented significance")
    for item in (coverage, short, constant):
        require(np.isnan(item["hac_p_value"]), "unavailable test fabricated p value")
    np.testing.assert_allclose(holm_adjust([.01, np.nan, .04]), [.03, np.nan, .08], equal_nan=True)
    for data in (np.full(240, np.inf), np.tile([1e308, -1e308], 120)):
        with np.errstate(over="ignore", invalid="ignore"):
            try:
                mean_uncertainty(data, signal_dates=240, options=options)
            except RobustnessError:
                continue
        raise AssertionError("non-finite or overflow data not rejected")
    return {"hac_to_iid_se_ratio": result["hac_standard_error"] / iid_se,
            "bootstrap_status": result["bootstrap_status"], "missing_status": coverage["status"],
            "short_status": short["status"], "constant_status": constant["status"],
            "fixed_holm_family": 3, "extreme_inputs_rejected": 2}
