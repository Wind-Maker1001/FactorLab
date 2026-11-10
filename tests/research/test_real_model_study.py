"""Synthetic fixtures test causality contracts, not real financial performance."""
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from pandas.testing import assert_frame_equal

from factorlab.models.ridge import RidgeResearchModel
from factorlab.research.real_model_study import (
    KEYS, PRICES, TARGET, build_panel, require_raw, check_source_identity,
    make_runner, common_predictions, daily_metrics, hac_mean_se,
)
from factorlab.validation.factor_selection import TrainFactorSelector, FactorSelectionConfig
from factorlab.validation.splitter import ExpandingWalkForwardSplitter


@pytest.fixture
def fixture():
    config = json.loads((Path(__file__).resolve().parents[2] / 'configs/research/p8-02-real-models.json').read_text(encoding='utf-8'))
    config['minimum_cross_section'] = 4
    config['top_k'] = 2
    config['splitter'] = dict(initial_train_months=3, validation_months=1, test_months=2, step_months=12, purge_days=6, embargo_days=6)
    config['selection'].update(minimum_cross_section=4, minimum_dates=20, k=3)
    dates = pd.bdate_range('2020-01-01', periods=260)
    rng = np.random.default_rng(100)
    rows = []
    for i in range(8):
        price = (10 + i) * np.exp(np.cumsum(rng.normal(0, .012, len(dates))))
        for j, day in enumerate(dates):
            rows.append(dict(date=day, symbol=f'{i:06d}.SZ', raw_open=price[j]*.999,
                raw_high=price[j]*1.02, raw_low=price[j]*.98, raw_close=price[j],
                volume=float(rng.integers(100,10000)), suspended=0, st_status=0))
    bars = pd.DataFrame(rows)
    members = bars.loc[bars.date.ge(dates[65]), KEYS].sort_values(KEYS).reset_index(drop=True)
    return bars, members, dates, config


def panel(fixture):
    return build_panel(*fixture)


def test_future_ohlc_cannot_change_historical_features(fixture):
    bars, members, dates, config = fixture
    before, _, _, refs = panel(fixture)
    cutoff = dates[180]
    changed = bars.copy()
    changed.loc[changed.date.gt(cutoff), PRICES] *= 1.3
    after, _, _, _ = build_panel(changed, members, dates, config)
    columns = KEYS + [r.factor_id for r in refs]
    # Labels intentionally use future bars; only feature identity is invariant.
    assert_frame_equal(before.loc[before.date.le(cutoff), columns].reset_index(drop=True),
        after.loc[after.date.le(cutoff), columns].reset_index(drop=True))


def test_split_maturity_purge_and_no_test_in_train(fixture):
    data, _, _, _ = panel(fixture)
    calendar = fixture[2][fixture[2] >= data.date.min()]
    folds = list(ExpandingWalkForwardSplitter(**fixture[3]['splitter']).split(data, calendar=calendar))
    assert len(folds) == 1
    f = folds[0]
    train, validation, test = [data.iloc[x] for x in f.as_indices()]
    assert set(train.date).isdisjoint(test.date)
    assert train.exit_date.max() < validation.date.min()
    assert validation.exit_date.max() < test.date.min()
    assert len(f.purged_dates) == len(f.embargoed_dates) == 6


def test_future_labels_cannot_change_fitted_ridge(fixture):
    data, _, _, refs = panel(fixture)
    names = tuple(r.factor_id for r in refs)
    runner = make_runner(fixture[3], 'ridge', names)
    calendar = fixture[2][fixture[2] >= data.date.min()]
    first = runner.run(data, calendar=calendar)
    changed = data.copy()
    cutoff = first.predictions.date.min()
    changed.loc[changed.date.ge(cutoff), TARGET] *= -100
    second = runner.run(changed, calendar=calendar)
    np.testing.assert_allclose(first.predictions.score, second.predictions.score)
    assert_frame_equal(first.coefficients, second.coefficients)


def test_label_uses_next_open_and_matures_at_sixth_session(fixture):
    bars, _, dates, _ = fixture
    data, _, _, _ = panel(fixture)
    row = data.iloc[0]
    position = dates.get_loc(row.date)
    stock = bars.loc[bars.symbol.eq(row.symbol)].set_index('date')
    expected = stock.loc[dates[position + 6], 'raw_open'] / stock.loc[dates[position + 1], 'raw_open'] - 1
    assert row[TARGET] == pytest.approx(expected)
    assert row.entry_date == dates[position + 1]
    assert row.exit_date == dates[position + 6]


def test_train_only_scaler_and_imputer():
    train = pd.DataFrame({'x':[1., 2., np.nan, 4.], 'z':[4., 3., 2., 1.]})
    model = RidgeResearchModel().fit(train, pd.Series([1., 2., 3., 4.]))
    mean, medians = model.scaler_.mean_.copy(), model.imputer_.statistics_.copy()
    model.predict(pd.DataFrame({'x':[1000000., np.nan], 'z':[-1e8, 9e7]}))
    np.testing.assert_array_equal(mean, model.scaler_.mean_)
    np.testing.assert_array_equal(medians, model.imputer_.statistics_)
    assert medians[0] == 2.


def test_train_only_selection_with_future_label_mutation(fixture):
    data, _, snapshot, refs = panel(fixture)
    selector = TrainFactorSelector(snapshot, refs, FactorSelectionConfig(**fixture[3]['selection']))
    calendar = fixture[2][fixture[2] >= data.date.min()]
    runner = make_runner(fixture[3], 'factor_score', selector.feature_names, selector)
    first = runner.run(data, calendar=calendar)
    changed = data.copy()
    changed.loc[changed.date.ge(first.predictions.date.min()), TARGET] *= -100
    second = runner.run(changed, calendar=calendar)
    # Full dataset identity must change; the Train-only selection must not.
    stable = ['selected_features', 'directions', 'train_hash', 'model_feature_names']
    stable = [name for name in stable if name in first.factor_selection]
    assert_frame_equal(first.factor_selection[stable], second.factor_selection[stable])
    assert_frame_equal(first.factor_quality, second.factor_quality)
    np.testing.assert_allclose(first.predictions.score, second.predictions.score)


@pytest.mark.parametrize('mode', ['missing', 'suspended', 'absent'])
def test_missing_suspension_never_zero_filled(fixture, mode):
    bars, members, dates, config = fixture
    symbol, day = bars.symbol.iloc[0], dates[150]
    changed = bars.copy()
    mask = changed.symbol.eq(symbol) & changed.date.eq(day)
    if mode == 'missing':
        changed.loc[mask, 'raw_close'] = np.nan
    elif mode == 'suspended':
        changed.loc[mask, 'suspended'] = 1
    else:
        changed = changed.loc[~mask]
    data, _, _, _ = build_panel(changed, members, dates, config)
    affected = data.loc[data.symbol.eq(symbol) & data.date.between(dates[144], dates[210])]
    assert affected.empty


def test_model_comparison_rejects_changed_sample_and_label():
    a = pd.DataFrame({'fold':[0,0], 'date':pd.to_datetime(['2020-01-01']*2), 'symbol':['a','b'], TARGET:[.1,.2], 'score':[1.,2.]})
    assert common_predictions({'a':a,'b':a.copy()})
    with pytest.raises(ValueError, match='identical'):
        common_predictions({'a':a,'b':a.iloc[:1]})
    b = a.copy()
    b.loc[0,TARGET] = 8.
    with pytest.raises(ValueError, match='identical'):
        common_predictions({'a':a,'b':b})


def test_data_identity_drift_rejected():
    expected = [dict(archive='stock.zip', member='bars', actual_sha256='abc'), dict(asset='other')]
    check_source_identity(expected[:1], expected)
    with pytest.raises(ValueError, match='identity drift'):
        check_source_identity([dict(archive='stock.zip', member='bars', actual_sha256='xyz')], expected)


@pytest.mark.parametrize('basis', ['HFQ', 'RAW', 'HFQ_UNVERIFIED'])
def test_uncertified_hfq_not_accepted(basis):
    with pytest.raises(ValueError, match='HFQ_UNVERIFIED'):
        require_raw({'price_basis':basis})


def test_daily_correlations_not_security_day_iid():
    p = pd.DataFrame(dict(model_id=['a']*4, fold=[0]*4,
        signal_date=pd.to_datetime(['2020-01-01']*4), symbol=['a','b','c','d'], prediction=[1.,2.,3.,4.]))
    p[TARGET] = [.1,.2,.3,.4]
    d = daily_metrics(p, 1, 4)
    assert len(d) == 1
    assert d.rank_ic.iloc[0] == pytest.approx(1.)
    assert d.top_bottom_raw_price_change.iloc[0] == pytest.approx(.3)
    assert hac_mean_se([1.,1.,1.]) == 0.
