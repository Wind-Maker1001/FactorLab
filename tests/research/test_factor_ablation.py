"""P2-08 isolation, negative direction, model provenance and paired support."""

from dataclasses import asdict
import json

import numpy as np
import pandas as pd
import pytest

from factorlab.validation import AblationConfig, FactorAblationRunner, WalkForwardRunner, FactorSelectionError
from factorlab.validation.ablation_statistics import daily_receipts, summarize_ablation, selection_stability
from factorlab.dataset.identity import IDENTITY_ATTR, frame_hash, seal
from tests.research.test_walk_forward_factor_selection import panel, selector, splitter


OPTIONS = {"factor_score": {}, "ridge": {"ridge_alphas": (0.1, 1.0)},
    "lightgbm": {"lightgbm_candidates": [{"n_estimators": 5, "min_child_samples": 4}]},
    "xgboost": {"xgboost_candidates": [{"n_estimators": 5, "max_depth": 2}]}}


def configured(models=None, **kwargs):
    return FactorAblationRunner(splitter(), selector(),
        AblationConfig(("REV1",), "unit fixture fixed before label construction", ("REV1", "MOM20")),
        model_options=OPTIONS if models is None else {m: OPTIONS[m] for m in models},
        minimum_cross_section=4, top_k=2, **kwargs)


def test_prepared_pair_order_signs_and_train_isolation():
    dates, frame = panel()
    runner = configured()
    prepared, folds, plans, _, _ = runner.prepare(frame, calendar=dates)
    for fold in folds:
        p = plans.loc[plans.fold.eq(fold.fold)].set_index("group")
        assert p.loc["A", "selected_features"] == p.loc["B", "selected_features"]
        assert p.loc["C", "selected_features"] == p.loc["D", "selected_features"]
        assert p.loc["C", "dynamic_selection_hash"] == p.loc["D", "dynamic_selection_hash"]
        for g in ("A", "C", "E"):
            assert set(p.loc[g, "directions"].values()) == {1}
        changed = frame.copy()
        # Earlier Test can enter a later expanding Train: isolate each fold.
        not_train = ~changed.index.isin(fold.train_indices)
        changed.loc[not_train, "EXCESS_RET5"] *= -100
        other = runner.prepare(changed, calendar=dates)[2]
        pd.testing.assert_frame_equal(plans.loc[plans.fold.eq(fold.fold)].reset_index(drop=True),
            other.loc[other.fold.eq(fold.fold)].reset_index(drop=True))
        assert prepared.iloc[fold.train_indices].exit_date.max() < fold.audit.validation_first_date
    changed = frame.copy()
    changed.loc[folds[0].train_indices, "EXCESS_RET5"] *= -1
    other = runner.prepare(changed, calendar=dates)[2]
    assert plans.iloc[1].directions != other.iloc[1].directions


@pytest.mark.parametrize("model", list(OPTIONS))
def test_all_arms_actual_fit_inputs_reload_and_fixed_parity(model, tmp_path, monkeypatch):
    import factorlab.validation.walk_forward as module
    dates, frame = panel()
    fits = []
    cls = {"factor_score": module.FactorScoreModel, "ridge": module.RidgeResearchModel,
           "lightgbm": module.LightGBMResearchModel, "xgboost": module.XGBoostResearchModel}[model]
    original = cls.fit

    def fit(self, X, y=None):
        fits.append((tuple(X.columns), X.copy(), y.copy()))
        return original(self, X, y)

    monkeypatch.setattr(cls, "fit", fit)
    result = configured([model]).run(frame, calendar=dates, model_directory=tmp_path / "models")
    assert len(result.audit) == 5 * 2 * 3
    assert result.audit.restored_test_predictions_exact.all()
    for position, plan in result.plans.sort_values(["group", "fold"]).reset_index(drop=True).iterrows():
        # Orchestrator loops group/fold in this one-model run. Every candidate
        # must fit exactly the corresponding Train inputs with frozen signs.
        fold = list(splitter().split(frame, calendar=dates))[plan.fold]
        train = frame.iloc[fold.train_indices]
        count = 2 if model == "ridge" else 1
        for _, X, y in fits[position * count:(position + 1) * count]:
            assert tuple(X.columns) == tuple(plan.selected_features)
            for name, sign in plan.directions.items():
                np.testing.assert_array_equal(X[name], train[name] * sign)
            np.testing.assert_array_equal(y, train.EXCESS_RET5)
    for group, names in (("A", ("REV1",)), ("E", ("REV1", "MOM20"))):
        extra = {"factor_weights": dict.fromkeys(names, 1 / len(names))} if model == "factor_score" else OPTIONS[model]
        control = WalkForwardRunner(splitter(), model_name=model, feature_names=names,
            minimum_cross_section=4, top_k=2, **extra).run(frame, calendar=dates)
        expected = daily_receipts(control.predictions, group=group, model=model, minimum_cross_section=4)
        pd.testing.assert_frame_equal(expected.reset_index(drop=True),
            result.daily.loc[result.daily.group.eq(group)].reset_index(drop=True), check_exact=True)
    result.write(tmp_path / "summary")
    restored_daily = pd.read_csv(tmp_path / "summary/daily.csv")
    for actual, expected in zip(summarize_ablation(restored_daily),
                                (result.metrics, result.contrasts, result.volatility)):
        pd.testing.assert_frame_equal(actual, expected, check_exact=False, atol=1e-12)
    plans = pd.DataFrame(json.loads((tmp_path / "summary/plans.json").read_text()))
    for actual, expected in zip(selection_stability(plans, selector().feature_names),
                               (result.frequency, result.jaccard)):
        pd.testing.assert_frame_equal(actual, expected)


def test_negative_factor_counterexample(tmp_path):
    dates, frame = panel()
    frame["EXCESS_RET5"] = -frame.REV1 + 0.01 * frame.MOM20
    result = configured(["factor_score"]).run(frame, calendar=dates, model_directory=tmp_path)
    stats = result.metrics.loc[result.metrics.fold.eq("ALL")].set_index("group")
    assert stats.loc["A", "rank_ic"] < -0.95
    assert stats.loc["B", "rank_ic"] > 0.95
    assert stats.loc["C", "rank_ic"] < -0.95
    assert stats.loc["D", "rank_ic"] > 0.95
    assert result.plans.query("group == 'B'").directions.tolist() == [{"REV1": -1}] * 2
    assert result.audit.query("group == 'B'").model_parameters.map(
        lambda s: json.loads(s)["weights"] == {"REV1": 1.0}).all()


def test_independent_contrast_arithmetic_and_missing_support():
    rows = []
    for group, offset in zip("ABCD", (0, 0.1, 0.2, 0.4)):
        for date, value in enumerate((0.1, 0.3)):
            rows.append(dict(model="ridge", group=group, fold=0, date=str(date),
                keys_sha256="same", labels_sha256="same", finite_pairs_sha256="same",
                ic=value + offset, rank_ic=value + offset))
    daily = pd.DataFrame(rows)
    _, comparisons, _ = summarize_ablation(daily)
    comparison = comparisons.set_index("contrast")
    for contrast, value in {"B-A": 0.1, "C-A": 0.2, "D-C": 0.2, "D-C-B+A": 0.1}.items():
        assert comparison.loc[contrast, "rank_ic_delta"] == pytest.approx(value)
    broken = daily.copy()
    broken.loc[broken.group.eq("D") & broken.date.eq("0"), "keys_sha256"] = "missing security"
    _, comparisons, _ = summarize_ablation(broken)
    row = comparisons.set_index("contrast").loc["D-C"]
    assert row.rank_ic_status == "OBSERVATION_MISMATCH" and np.isnan(row.rank_ic_delta)
    broken = daily.drop(daily.index[-1])
    assert summarize_ablation(broken)[1].set_index("contrast").loc["D-C", "rank_ic_status"] == "OBSERVATION_MISMATCH"
    broken = daily.copy()
    broken.loc[broken.group.eq("D"), "rank_ic"] = np.nan
    row = summarize_ablation(broken)[1].set_index("contrast").loc["D-C"]
    assert row.rank_ic_status == "UNDEFINED_DAILY_METRIC" and np.isnan(row.rank_ic_delta)


def test_stability_independent_set_arithmetic():
    plans = pd.DataFrame([dict(group="C", fold=i, selected_features=names)
                          for i, names in enumerate((["x", "y"], ["y", "z"], ["y", "z"]))])
    frequency, pairs = selection_stability(plans, ["x", "y", "z", "never"])
    assert frequency.set_index("factor").loc["never", "frequency"] == 0
    assert frequency.set_index("factor").loc["y", "frequency"] == 1
    assert pairs.jaccard.tolist() == [1 / 3, 1 / 3, 1]


def test_ablation_identity_content_and_pins_fail_closed():
    dates, frame = panel()
    sel = selector()
    payload = {"dataset_content_hash": frame_hash(frame), "snapshot_hash": sel.snapshot.snapshot_hash,
               "factors": [asdict(r) for r in sel.factor_refs]}
    frame.attrs[IDENTITY_ATTR] = seal(payload)
    configured().prepare(frame, calendar=dates)
    frame.loc[0, "REV1"] += 1
    with pytest.raises(ValueError, match="content mismatch"):
        configured().prepare(frame, calendar=dates)
    payload["dataset_content_hash"] = frame_hash(frame)
    payload["factors"] = []
    frame.attrs[IDENTITY_ATTR] = seal(payload)
    with pytest.raises(ValueError, match="pins mismatch"):
        configured().prepare(frame, calendar=dates)


def test_undefined_icir_and_e_exclusion():
    rows = [dict(model="ridge", group=g, fold=0, date=str(i), keys_sha256="same",
                 labels_sha256="same", finite_pairs_sha256="same", ic=0.2, rank_ic=0.2)
            for g in "ABCDE" for i in range(2)]
    metrics, contrasts, _ = summarize_ablation(pd.DataFrame(rows))
    assert metrics.icir.isna().all()
    assert contrasts.icir_status.eq("UNDEFINED_ICIR").all()
    assert contrasts.ic_delta.eq(0).all()
    assert not contrasts.contrast.str.contains("E").any()


def test_shortfall_rejected_instead_of_changing_k():
    dates, frame = panel()
    sel = selector(k=2)
    frame["MOM20"] = frame.REV1
    runner = FactorAblationRunner(splitter(), sel,
        AblationConfig(("REV1", "MOM20"), "predeclared"), model_options={"ridge": {}})
    with pytest.raises(FactorSelectionError, match="require 2"):
        runner.prepare(frame, calendar=dates)


@pytest.mark.parametrize("options", [{"factor_score": {"factor_weights": {"REV1": -2}}},
    {"ridge": {"factor_selector": None}}, {"ridge": {"target_col": "future"}}])
def test_control_override_rejected(options):
    with pytest.raises(ValueError, match="override"):
        FactorAblationRunner(splitter(), selector(), AblationConfig(("REV1",), "predeclared"), model_options=options)
