"""Train-only selection evidence, feature plumbing, and deterministic receipts."""

from dataclasses import replace
import json

import numpy as np
import pandas as pd
import pytest

from factorlab.dataset.identity import IDENTITY_ATTR, frame_hash, seal
from factorlab.factors.registry_v2 import FactorDefinition, FactorRegistryV2
from factorlab.models.base import ResearchModel
from factorlab.validation import (
    ExpandingWalkForwardSplitter, FactorSelectionConfig, FactorSelectionError,
    TrainFactorSelector, WalkForwardRunner, WalkForwardRunError,
)


def panel():
    dates = pd.bdate_range("2020-01-01", "2020-06-30")
    rng = np.random.default_rng(207)
    frame = pd.MultiIndex.from_product([dates, range(12)], names=["date", "symbol"]).to_frame(index=False)
    frame["symbol"] = frame.symbol.map(lambda n: "S%02d" % n)
    frame["REV1"] = rng.normal(size=len(frame))
    frame["MOM20"] = rng.normal(size=len(frame))
    # First fold learns REV1; a later expanding fold learns MOM20.
    frame["EXCESS_RET5"] = np.where(frame.date < "2020-02-01", frame.REV1, -frame.MOM20)
    frame["entry_date"] = frame.date + pd.offsets.BDay(1)
    frame["exit_date"] = frame.date + pd.offsets.BDay(3)
    return dates, frame


def selector(names=("REV1", "MOM20"), **options):
    registry = FactorRegistryV2.with_builtins()
    registry.register(FactorDefinition.builtin("REV1", factor_id="COPY"))
    registry.register(FactorDefinition.alias("ALIAS", "1", registry.get("REV1", "1")))
    snapshot = registry.freeze()
    config = FactorSelectionConfig(
        k=1, minimum_cross_section=4, minimum_dates=5, minimum_observations=20,
        minimum_abs_rank_ic=0.01, minimum_direction_consistency=0.0,
    )
    return TrainFactorSelector(snapshot, tuple(snapshot.get(n, "1").ref for n in names),
                               replace(config, **options))


def splitter():
    return ExpandingWalkForwardSplitter(initial_train_months=1, validation_months=1,
        test_months=1, step_months=2, purge_days=1, embargo_days=1)


def runner(model="ridge", selection=None, **options):
    selection = selector() if selection is None else selection
    extra = {"factor_score": {"dynamic_factor_score_weighting": "equal"},
             "ridge": {"ridge_alphas": (0.1, 1.0)},
             "lightgbm": {"lightgbm_candidates": [{"n_estimators": 5, "min_child_samples": 4}]},
             "xgboost": {"xgboost_candidates": [{"n_estimators": 5, "max_depth": 2}]}}[model]
    return WalkForwardRunner(splitter(), model_name=model, feature_names=selection.feature_names,
        factor_selector=selection, top_k=2, minimum_cross_section=4, **extra, **options)


def test_each_fold_independent_and_shortfall_explicit():
    dates, frame = panel()
    result = runner().run(frame, calendar=dates)
    assert result.factor_selection.selected_features.tolist() == [["REV1"], ["MOM20"]]
    assert result.factor_selection.directions.tolist() == [{"REV1": 1}, {"MOM20": -1}]
    assert (result.fold_summary.train_max_exit_date < result.fold_summary.audit_validation_first_date).all()
    assert (result.fold_summary.validation_max_exit_date < result.fold_summary.audit_test_first_date).all()
    # Expansion is intentional: earlier Test labels may belong to later Train.
    selected = selector(k=3).select(frame)
    assert len(selected.feature_names) == 2 and selected.receipt["shortfall"]
    with pytest.raises(FactorSelectionError, match="require 3"):
        selector(k=3, shortfall_policy="error").select(frame)


@pytest.mark.parametrize("partition", ["train", "validation", "test", "test_features"])
def test_perturbation_boundaries(partition):
    dates, frame = panel()
    baseline = runner().run(frame, calendar=dates)
    fold = list(splitter().split(frame, calendar=dates))[-1]
    changed = frame.copy()
    positions = getattr(fold, ("test" if partition == "test_features" else partition) + "_indices")
    if partition == "test_features":
        changed.loc[positions, ["REV1", "MOM20"]] *= -3
    else:
        changed.loc[positions, "EXCESS_RET5"] = changed.loc[positions, "REV1"]
    rerun = runner().run(changed, calendar=dates)
    before, after = baseline.factor_selection.iloc[-1], rerun.factor_selection.iloc[-1]
    if partition == "train":
        assert before.selected_features != after.selected_features
        assert before.selection_hash != after.selection_hash
    else:
        assert before.selection_hash == after.selection_hash
        assert before.fit_source_sha256 == after.fit_source_sha256
        if partition != "validation":
            pd.testing.assert_frame_equal(baseline.coefficients, rerun.coefficients)
        if partition == "test_features":
            assert before.prediction_sha256 != after.prediction_sha256
        elif partition == "test":
            np.testing.assert_array_equal(baseline.predictions.score, rerun.predictions.score)


def test_selector_sees_only_mature_unpurged_rows(monkeypatch):
    dates, frame = panel()
    frame.loc[0, "exit_date"] = pd.Timestamp("2020-02-05")
    frame.loc[12, "exit_date"] = pd.NaT
    expected = list(splitter().split(frame, calendar=dates))
    seen = []
    original = TrainFactorSelector.select

    def spy(self, train, **kwargs):
        fold = expected[len(seen)]
        assert train.index.tolist() == fold.train_indices.tolist()
        assert train.exit_date.max() < fold.audit.validation_first_date
        assert not train.date.isin(fold.purged_dates).any()
        assert not train.date.isin(fold.embargoed_dates).any()
        seen.append(len(train))
        return original(self, train, **kwargs)

    monkeypatch.setattr(TrainFactorSelector, "select", spy)
    result = runner().run(frame, calendar=dates)
    assert len(seen) == 2
    assert result.fold_summary.train_overlap_rows_removed.iloc[0] > 0
    assert result.fold_summary.incomplete_label_rows_removed.iloc[0] > 0


def test_missing_constant_nonfinite_and_insufficient_evidence():
    _, frame = panel()
    frame["VOL5"], frame["VOL20"], frame["VOL60"] = np.nan, 1.0, np.inf
    frame["TURN5"] = np.where(frame.date < "2020-01-03", frame.REV1, np.nan)
    result = selector(("REV1", "VOL5", "VOL20", "VOL60", "TURN5"), k=5).select(frame)
    quality = result.quality.set_index("factor_id")
    assert result.feature_names == ("REV1",)
    assert "coverage" in quality.loc["VOL5", "reason"]
    assert "undefined_rank_ic" in quality.loc["VOL20", "reason"]
    assert "nonfinite_ratio" in quality.loc["VOL60", "reason"]
    assert "insufficient_valid_dates" in quality.loc["TURN5", "reason"]
    with pytest.raises(FactorSelectionError, match="Only 0 eligible") as error:
        selector(("VOL5",)).select(frame)
    assert "undefined_rank_ic" in error.value.quality.reason.iloc[0]


def test_definition_alias_numeric_duplicate_and_high_correlation():
    _, frame = panel()
    frame["COPY"] = frame["ALIAS"] = frame["VOL5"] = frame.REV1
    frame["VOL20"] = frame.REV1 * -2 + 0.00001 * frame.MOM20
    result = selector(("REV1", "COPY", "ALIAS", "VOL5", "VOL20"), k=5).select(frame)
    assert len(result.feature_names) == 1
    reasons = result.quality.reason.tolist()
    assert reasons.count("duplicate_definition_or_alias") == 2
    assert "numeric_duplicate" in reasons
    assert "highly_redundant" in reasons
    assert {"NUMERIC_DUPLICATE", "HIGHLY_REDUNDANT"}.issubset(set(result.redundancy.status))
    frame["REV1"] *= -1
    conflicted = selector(("COPY", "REV1"), k=2).select(frame).quality.set_index("factor_id")
    assert conflicted.loc["REV1", "reason"] == "identity_value_conflict"
    assert pd.isna(conflicted.loc["REV1", "rank_ic_mean"])


def test_insufficient_pair_evidence_is_not_nonredundant():
    _, frame = panel()
    # Both pass marginal coverage, but disjoint missing masks fail pair coverage.
    frame.loc[frame.index % 12 < 2, "REV1"] = np.nan
    frame.loc[frame.index % 12 >= 10, "MOM20"] = np.nan
    result = selector(k=2, minimum_pair_coverage=0.8).select(frame)
    assert result.redundancy.status.tolist() == ["UNDETERMINED"]
    assert len(result.feature_names) == 1
    assert "undetermined" in result.quality.reason.tolist()


@pytest.mark.parametrize("model", ["factor_score", "ridge", "lightgbm", "xgboost"])
def test_four_models_receive_and_persist_selected_signed_features(model, tmp_path, monkeypatch):
    import factorlab.validation.walk_forward as module
    dates, frame = panel()
    fits = []
    cls = {"factor_score": module.FactorScoreModel, "ridge": module.RidgeResearchModel,
           "lightgbm": module.LightGBMResearchModel, "xgboost": module.XGBoostResearchModel}[model]
    original_fit = cls.fit

    def fit(self, X, y=None):
        fits.append((list(X.columns), X.copy(), y.copy()))
        return original_fit(self, X, y)

    monkeypatch.setattr(cls, "fit", fit)
    result = runner(model, model_directory=tmp_path).run(frame, calendar=dates)
    assert fits and {tuple(f[0]) for f in fits} == {("REV1",), ("MOM20",)}
    for fold, path in zip(list(splitter().split(frame, calendar=dates)), result.model_paths):
        receipt = result.factor_selection.loc[result.factor_selection.fold.eq(fold.fold)].iloc[0]
        sidecar = json.loads(path.with_suffix(".selection.json").read_text(encoding="utf-8"))
        assert sidecar["selection_hash"] == receipt.selection_hash
        assert sidecar["directions"] == receipt.directions
        assert sidecar["model_prediction_identity"] == receipt.model_prediction_identity
        fitted = ResearchModel.load(path)
        assert list(fitted.feature_names) == receipt.selected_features == receipt.model_feature_names
        test = frame.iloc[fold.test_indices].copy()
        for name, sign in receipt.directions.items():
            test[name] *= sign
        scores = fitted.predict(test[receipt.selected_features])
        predicted = result.predictions.loc[result.predictions.fold.eq(fold.fold)].set_index(["date", "symbol"])
        actual = pd.Series(scores.to_numpy(), index=pd.MultiIndex.from_frame(test[["date", "symbol"]]))
        np.testing.assert_array_equal(actual.sort_index(), predicted.score.sort_index())
        if model == "factor_score":
            assert dict(fitted.weights) == dict.fromkeys(receipt.selected_features, 1.0)


@pytest.mark.parametrize("model", ["factor_score", "ridge", "lightgbm", "xgboost"])
def test_row_and_candidate_order_reproducible_and_runner_reusable(model):
    dates, frame = panel()
    configured = runner(model)
    baseline = configured.run(frame, calendar=dates)
    repeated = configured.run(frame.sample(frac=1, random_state=10), calendar=dates)
    reversed_refs = runner(model, selection=selector(("MOM20", "REV1"))).run(frame, calendar=dates)
    for result in (repeated, reversed_refs):
        pd.testing.assert_frame_equal(baseline.predictions, result.predictions)
        pd.testing.assert_frame_equal(baseline.factor_selection, result.factor_selection)
        pd.testing.assert_frame_equal(baseline.factor_quality, result.factor_quality)
    assert configured.feature_names == selector().feature_names


def test_weight_contract_and_fixed_default():
    dates, frame = panel()
    with pytest.raises(WalkForwardRunError, match="Explicit dynamic"):
        WalkForwardRunner(splitter(), model_name="factor_score", feature_names=selector().feature_names,
                          factor_selector=selector(), factor_weights={"REV1": 1, "MOM20": 1})
    options = dict(model_name="factor_score", feature_names=["REV1", "MOM20"],
                   factor_weights={"REV1": 0.3, "MOM20": -0.7}, minimum_cross_section=4, top_k=2)
    fixed = WalkForwardRunner(splitter(), **options).run(frame, calendar=dates)
    disabled = WalkForwardRunner(splitter(), factor_selector=None, **options).run(frame, calendar=dates)
    assert fixed.factor_selection.empty
    pd.testing.assert_frame_equal(fixed.predictions, disabled.predictions)
    dynamic = WalkForwardRunner(splitter(), factor_selector=selector(),
        dynamic_factor_score_weighting="configured_subset", **options).run(frame, calendar=dates)
    assert dynamic.factor_selection.model_parameters.iloc[-1]["weights"] == {"MOM20": -0.7}


def test_dataset_identity_checked_and_bound():
    dates, frame = panel()
    sel = selector()
    from dataclasses import asdict
    frame.attrs[IDENTITY_ATTR] = seal({"dataset_content_hash": frame_hash(frame),
        "snapshot_hash": sel.snapshot.snapshot_hash, "factors": [asdict(r) for r in sel.factor_refs]})
    result = runner().run(frame, calendar=dates)
    assert result.factor_selection.dataset_identity_hash.eq(frame.attrs[IDENTITY_ATTR]["identity_hash"]).all()
    frame.loc[0, "REV1"] += 1
    with pytest.raises(WalkForwardRunError, match="content mismatch"):
        runner().run(frame, calendar=dates)


def test_summary_only_redundancy_preserves_existing_math():
    from factorlab.exploratory.factor_discovery.redundancy import diagnose_redundancy
    from factorlab.exploratory.factor_discovery.contracts import DiscoveryConfig
    _, frame = panel()
    config = DiscoveryConfig(development_start="2020-01-01", development_end="2020-06-30",
                             minimum_cross_section=4, minimum_evaluation_dates=5)
    original, daily = diagnose_redundancy(frame, ["REV1", "MOM20"], [], config)
    summary, omitted = diagnose_redundancy(frame, ["REV1", "MOM20"], [], config, summary_only=True)
    assert not daily.empty and omitted.empty
    pd.testing.assert_frame_equal(original, summary, check_exact=True)


def test_semantic_column_names_and_undefined_label_evidence():
    dates, frame = panel()
    renamed = frame.rename(columns={"date": "session", "symbol": "asset", "EXCESS_RET5": "target"})
    result = runner(date_col="session", symbol_col="asset", target_col="target").run(renamed, calendar=dates)
    assert len(result.factor_selection) == 2
    frame["EXCESS_RET5"] = 0.0
    with pytest.raises(FactorSelectionError, match="undefined_rank_ic"):
        selector().select(frame)


@pytest.mark.parametrize("overrides", [{"k": True}, {"minimum_dates": 1},
    {"minimum_coverage": np.nan}, {"minimum_selected": 20}, {"shortfall_policy": "pad"},
    {"orient_by_train_ic": 1}])
def test_invalid_config(overrides):
    with pytest.raises(FactorSelectionError):
        FactorSelectionConfig(**overrides)
