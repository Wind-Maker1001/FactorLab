from dataclasses import asdict, replace
import json

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("statsmodels", reason="install factorlab[pairs] for cointegration research")

from factorlab.strategies.pairs_statistics import SelectionConfig, fit_pair, select_pairs


def cointegrated(n=600):
    rng = np.random.default_rng(810)
    x = 100 + np.cumsum(rng.normal(size=n))
    noise = rng.normal(scale=0.3, size=n)
    return pd.DataFrame({"Y": 7 + 1.6 * x + noise, "X": x}, index=pd.date_range("2020-01-01", periods=n))


def config(prices, **kwargs):
    return SelectionConfig(train_end=str(prices.index[-1].date()), **kwargs)


def test_cointegration_ols_and_spread():
    prices = cointegrated()
    model = fit_pair(prices, config(prices))
    expected = np.linalg.lstsq(np.column_stack([np.ones(len(prices)), prices.X]), prices.Y, rcond=None)[0]
    assert model.intercept == pytest.approx(expected[0])
    assert model.hedge_ratio == pytest.approx(expected[1])
    spread = prices.Y - expected[0] - expected[1] * prices.X
    np.testing.assert_allclose(model.spread(prices), spread)
    assert model.spread_mean == pytest.approx(spread.mean())
    assert model.spread_std == pytest.approx(spread.std(ddof=1))
    assert model.coint_pvalue < 0.05
    assert model.adf_pvalue < 0.05
    assert model.hedge_ratio_drift < 0.5
    json.dumps(asdict(model), allow_nan=False)


def test_log_transform_is_explicit():
    log_prices = cointegrated() / 100
    prices = np.exp(log_prices)
    model = fit_pair(prices, config(prices, price_transform="log"))
    expected = fit_pair(log_prices, config(log_prices))
    assert model.intercept == pytest.approx(expected.intercept)
    assert model.hedge_ratio == pytest.approx(expected.hedge_ratio)
    np.testing.assert_allclose(model.spread(prices), expected.spread(log_prices), atol=1e-14)
    prices.iloc[0, 0] = 0
    with pytest.raises(ValueError, match="positive"):
        fit_pair(prices, config(prices, price_transform="log"))


def test_future_values_and_missingness_cannot_change_selection():
    prices = cointegrated()
    cfg = replace(config(prices), train_end=str(prices.index[399].date()))
    before, diagnostics = select_pairs(prices, cfg)
    changed = prices.copy()
    changed.iloc[400:, 0] = np.nan
    changed.iloc[400:, 1] = -1e20
    after, changed_diagnostics = select_pairs(changed, cfg)
    assert before == after
    pd.testing.assert_frame_equal(diagnostics, changed_diagnostics)
    assert before[0].n_observations == 400


def test_high_correlation_without_cointegration_is_rejected():
    rng = np.random.default_rng(121)
    n = 800
    x = np.arange(n, dtype=float) + rng.normal(size=n)
    independent_walk = np.cumsum(rng.normal(size=n))
    prices = pd.DataFrame({"Y": 100 + 2 * x + independent_walk, "X": 100 + x}, index=pd.date_range("2020-01-01", periods=n))
    assert prices.corr().iloc[0, 1] > 0.99
    models, diagnostics = select_pairs(prices, config(prices))
    assert models == []
    assert "Engle-Granger" in diagnostics.iloc[0].reason


def test_completeness_overlap_and_candidate_counts():
    prices = cointegrated(300)
    prices["SHORT"] = np.nan
    prices.loc[prices.index[:30], "SHORT"] = np.arange(30)
    models, diagnostics = select_pairs(prices, config(prices))
    assert len(models) == 1
    assert len(diagnostics) == 3
    assert diagnostics.attrs == {"candidate_count": 3, "eligible_candidate_count": 1}
    missing = prices[["Y", "X"]].copy()
    missing.iloc[:60, 0] = np.nan
    missing.iloc[240:, 1] = np.nan
    with pytest.raises(ValueError, match="completeness"):
        fit_pair(missing, config(missing))
    with pytest.raises(ValueError, match="overlap"):
        fit_pair(missing, config(missing, min_completeness=0.7, min_overlap=0.8))
    with pytest.raises(ValueError, match="observations"):
        fit_pair(prices.iloc[:30, :2], config(prices))


def test_zscore_uses_previous_window_and_future_is_irrelevant():
    prices = cointegrated()
    model = fit_pair(prices.iloc[:300], config(prices.iloc[:300]))
    actual = model.zscore(prices, 20)
    spread = model.spread(prices)
    assert actual.iloc[:20].isna().all()
    assert actual.iloc[20] == pytest.approx((spread.iloc[20] - spread.iloc[:20].mean()) / spread.iloc[:20].std())
    changed = prices.copy()
    changed.iloc[401:] *= 100
    pd.testing.assert_series_equal(actual.iloc[:401], model.zscore(changed, 20).iloc[:401])
    shocked = prices.copy()
    shocked.iloc[400, 0] += 100
    expected = (model.spread(shocked).iloc[400] - spread.iloc[380:400].mean()) / spread.iloc[380:400].std()
    assert model.zscore(shocked, 20).iloc[400] == pytest.approx(expected)


def test_positive_and_stable_hedges_are_required():
    prices = cointegrated()
    prices.Y = 400 - prices.Y
    with pytest.raises(ValueError, match="nonpositive"):
        fit_pair(prices, config(prices))
    prices = cointegrated()
    prices.loc[prices.index[300:], "Y"] += 2 * (prices.X.iloc[300:] - prices.X.iloc[300:].mean())
    with pytest.raises(ValueError, match="unstable"):
        fit_pair(prices, config(prices))


def test_hand_calculated_hedge_and_spread():
    from factorlab.strategies.pairs_statistics import _ols

    # x mean 20, y mean 45; centered cross product 400 / x squares 200.
    prices = pd.DataFrame({"Y": [26., 43., 66.], "X": [10., 20., 30.]})
    intercept, hedge = _ols(prices.to_numpy())
    assert intercept == pytest.approx(5)
    assert hedge == pytest.approx(2)
    training = cointegrated()
    model = replace(fit_pair(training, config(training)), intercept=intercept, hedge_ratio=hedge)
    np.testing.assert_allclose(model.spread(prices), [1., -2., 1.])
    assert model.spread(prices).std() == pytest.approx(np.sqrt(3))


def test_exactly_two_columns_and_valid_training_window():
    prices = cointegrated()
    with pytest.raises(ValueError, match="exactly two"):
        fit_pair(prices[["Y"]], config(prices))
    with pytest.raises(ValueError, match="boundaries"):
        SelectionConfig(train_end="2020-01-01", train_start="2021-01-01")
