"""Phase 2 model interface, leakage, and persistence tests."""

import numpy as np
import pandas as pd
import pytest

from qbot.research.models import (
    FactorScoreModel,
    ModelInputError,
    ModelNotFittedError,
    RidgeResearchModel,
)


def _training_data():
    index = pd.Index(["a", "b", "c", "d", "e"], name="observation")
    X = pd.DataFrame(
        {
            "REV1": [-2.0, -1.0, np.nan, 1.0, 2.0],
            "MOM20": [10.0, 12.0, 14.0, 16.0, 18.0],
        },
        index=index,
    )
    y = pd.Series([-1.2, -0.5, 0.1, 0.7, 1.5], index=index, name="EXCESS_RET5")
    return X, y


def test_factor_score_aligns_columns_by_name_and_returns_continuous_score():
    X = pd.DataFrame(
        {"unused": [99, 98], "MOM20": [0.5, -1.0], "REV1": [1.0, 2.0]},
        index=["first", "second"],
    )
    model = FactorScoreModel({"REV1": 0.75, "MOM20": -0.25}).fit(X)

    result = model.predict(X[["MOM20", "unused", "REV1"]])

    expected = pd.Series([0.625, 1.75], index=X.index, name="score")
    pd.testing.assert_series_equal(result, expected)


def test_factor_score_uses_zero_as_explicit_neutral_value_for_missing_zscore():
    X = pd.DataFrame({"REV1": [np.nan, 1.0], "MOM20": [2.0, np.nan]})
    model = FactorScoreModel({"REV1": 2.0, "MOM20": 3.0}).fit(X)
    assert model.predict(X).tolist() == [6.0, 2.0]


def test_factor_score_can_normalize_configured_weight_scale():
    X = pd.DataFrame({"REV1": [1.0], "MOM20": [2.0]})
    model = FactorScoreModel(
        {"REV1": 2.0, "MOM20": -1.0},
        normalize_by_absolute_weight=True,
    ).fit(X)

    assert model.predict(X).iloc[0] == pytest.approx(0.0)


def test_factor_score_rejects_missing_column_and_infinity():
    model = FactorScoreModel({"REV1": 1.0})
    with pytest.raises(ModelInputError, match="missing required features"):
        model.fit(pd.DataFrame({"MOM20": [1.0]}))
    with pytest.raises(ModelInputError, match="infinity"):
        model.fit(pd.DataFrame({"REV1": [np.inf]}))


def test_factor_score_persistence_round_trip(tmp_path):
    X = pd.DataFrame({"REV1": [-1.0, 0.0, 1.0]})
    model = FactorScoreModel({"REV1": -2.0}).fit(X)
    artifact = tmp_path / "models" / "factor_score.pkl"
    model.save(artifact)

    restored = FactorScoreModel.load(artifact)

    assert restored.weights == model.weights
    pd.testing.assert_series_equal(restored.predict(X), model.predict(X))


def test_unfitted_models_cannot_predict_or_save(tmp_path):
    with pytest.raises(ModelNotFittedError):
        FactorScoreModel({"REV1": 1.0}).predict(pd.DataFrame({"REV1": [1.0]}))
    with pytest.raises(ModelNotFittedError):
        RidgeResearchModel(feature_names=["REV1"]).save(tmp_path / "ridge.pkl")


def test_ridge_fits_imputer_and_scaler_on_training_data_only():
    X_train, y_train = _training_data()
    model = RidgeResearchModel(alpha=0.5, feature_names=["REV1", "MOM20"]).fit(
        X_train, y_train
    )
    training_median = model.imputer_.statistics_.copy()
    training_mean = model.scaler_.mean_.copy()
    training_scale = model.scaler_.scale_.copy()
    X_future = pd.DataFrame(
        {"MOM20": [1_000_000.0, np.nan], "REV1": [-1_000_000.0, np.nan]},
        index=["future-1", "future-2"],
    )

    prediction = model.predict(X_future)

    np.testing.assert_array_equal(model.imputer_.statistics_, training_median)
    np.testing.assert_array_equal(model.scaler_.mean_, training_mean)
    np.testing.assert_array_equal(model.scaler_.scale_, training_scale)
    assert prediction.index.equals(X_future.index)
    assert np.isfinite(prediction).all()


def test_ridge_exposes_named_coefficients_and_exports_them(tmp_path):
    X, y = _training_data()
    model = RidgeResearchModel(alpha=0.1, feature_names=["MOM20", "REV1"]).fit(X, y)

    assert model.feature_names == ("MOM20", "REV1")
    assert model.coefficients.index.tolist() == ["MOM20", "REV1"]
    assert model.coefficients.name == "coefficient"
    output = tmp_path / "coefficients.csv"
    model.export_coefficients(output)
    stored = pd.read_csv(output)
    assert stored.columns.tolist() == ["feature", "coefficient"]
    assert stored["feature"].tolist() == ["MOM20", "REV1"]
    np.testing.assert_allclose(stored["coefficient"], model.coefficients.to_numpy())


def test_ridge_persistence_preserves_predictions_and_training_state(tmp_path):
    X, y = _training_data()
    model = RidgeResearchModel(alpha=2.0, feature_names=["REV1", "MOM20"]).fit(X, y)
    expected = model.predict(X)
    artifact = tmp_path / "ridge.pkl"
    model.save(artifact)

    restored = RidgeResearchModel.load(artifact)

    assert restored.random_seed == 42
    assert restored.feature_names == model.feature_names
    np.testing.assert_allclose(restored.coefficients, model.coefficients)
    pd.testing.assert_series_equal(restored.predict(X), expected)


@pytest.mark.parametrize("invalid", [np.nan, np.inf, -np.inf])
def test_ridge_rejects_non_finite_training_labels(invalid):
    X, y = _training_data()
    y.iloc[-1] = invalid
    with pytest.raises(ModelInputError, match="finite labels"):
        RidgeResearchModel(feature_names=["REV1", "MOM20"]).fit(X, y)


def test_ridge_rejects_misaligned_target_and_all_missing_training_feature():
    X, y = _training_data()
    with pytest.raises(ModelInputError, match="indexes must be identical"):
        RidgeResearchModel(feature_names=["REV1", "MOM20"]).fit(
            X, y.sort_index(ascending=False)
        )
    X["REV1"] = np.nan
    with pytest.raises(ModelInputError, match="entirely missing"):
        RidgeResearchModel(feature_names=["REV1", "MOM20"]).fit(X, y)


def test_ridge_is_deterministic_for_same_seed_and_training_data():
    X, y = _training_data()
    first = RidgeResearchModel(alpha=1.0, random_seed=42).fit(X, y)
    second = RidgeResearchModel(alpha=1.0, random_seed=42).fit(X, y)
    np.testing.assert_allclose(first.predict(X), second.predict(X), rtol=0, atol=0)
    np.testing.assert_allclose(first.coefficients, second.coefficients, rtol=0, atol=0)


def test_ridge_consumes_solver_limits_and_rejects_invalid_public_inputs():
    X, y = _training_data()
    model = RidgeResearchModel(
        alpha=1.0,
        max_iter=321,
        tolerance=1e-5,
        feature_names=["REV1", "MOM20"],
    ).fit(X, y)
    assert model.estimator_.max_iter == 321
    assert model.estimator_.tol == pytest.approx(1e-5)

    with pytest.raises(ModelInputError, match="alpha"):
        RidgeResearchModel(alpha="bad")
    with pytest.raises(ModelInputError, match="fit_intercept"):
        RidgeResearchModel(fit_intercept="false")
    with pytest.raises(ModelInputError, match="Complex-valued features"):
        RidgeResearchModel().fit(
            pd.DataFrame({"factor": [1 + 2j, 2 + 3j]}),
            pd.Series([0.1, 0.2]),
        )


def test_load_rejects_artifact_of_different_model_class(tmp_path):
    X = pd.DataFrame({"REV1": [0.0, 1.0]})
    artifact = tmp_path / "factor_score.pkl"
    FactorScoreModel({"REV1": 1.0}).fit(X).save(artifact)
    with pytest.raises(TypeError, match="expected RidgeResearchModel"):
        RidgeResearchModel.load(artifact)
