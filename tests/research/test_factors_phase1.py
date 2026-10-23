"""Formula, preprocessing, and lookahead tests for all Phase-1 factors."""

import numpy as np
import pandas as pd
import pytest

from qbot.research.factors.preprocess import preprocess_cross_sections
from qbot.research.factors.registry import FACTOR_REGISTRY, compute_factors


FACTOR_NAMES = [
    "REV1",
    "REV5",
    "MOM20",
    "MOM60",
    "VOL5",
    "VOL20",
    "VOL60",
    "TURN5",
    "TURN20",
    "VOLUME_RATIO",
    "AMIHU20",
    "PRICE_MA20",
    "MA5_MA20",
    "MA20_MA60",
]


def _bars(periods=75, symbols=("600000.SH",)):
    dates = pd.bdate_range("2023-01-02", periods=periods)
    rows = []
    for symbol_number, symbol in enumerate(symbols):
        for position, date in enumerate(dates):
            close = (100.0 + symbol_number) * (1.01 ** position)
            rows.append(
                {
                    "date": date,
                    "symbol": symbol,
                    "adj_close": close,
                    "turnover": 0.01 + position * 0.0001 + symbol_number * 0.001,
                    "volume": 1000.0 + 10.0 * position + symbol_number * 100.0,
                    "amount": 50_000_000.0 + position * 100_000.0,
                }
            )
    return pd.DataFrame(rows)


def test_registry_contains_exactly_the_fourteen_phase1_factors():
    assert set(FACTOR_REGISTRY) == set(FACTOR_NAMES)


def test_all_factor_formulas_match_independent_pandas_calculations():
    bars = _bars()
    calculated = compute_factors(bars, FACTOR_NAMES).set_index("date")
    close = bars.set_index("date")["adj_close"]
    turnover = bars.set_index("date")["turnover"]
    volume = bars.set_index("date")["volume"]
    amount = bars.set_index("date")["amount"]
    returns = close.pct_change(fill_method=None)
    expected = {
        "REV1": -(close / close.shift(1) - 1.0),
        "REV5": -(close / close.shift(5) - 1.0),
        "MOM20": close / close.shift(20) - 1.0,
        "MOM60": close / close.shift(60) - 1.0,
        "VOL5": returns.rolling(5, min_periods=5).std(ddof=1),
        "VOL20": returns.rolling(20, min_periods=20).std(ddof=1),
        "VOL60": returns.rolling(60, min_periods=60).std(ddof=1),
        "TURN5": turnover.rolling(5, min_periods=5).mean(),
        "TURN20": turnover.rolling(20, min_periods=20).mean(),
        "VOLUME_RATIO": volume.rolling(5, min_periods=5).mean()
        / volume.rolling(20, min_periods=20).mean(),
        "AMIHU20": (returns.abs() / amount * 1.0e8).rolling(
            20, min_periods=20
        ).mean(),
        "PRICE_MA20": close / close.rolling(20, min_periods=20).mean() - 1.0,
        "MA5_MA20": close.rolling(5, min_periods=5).mean()
        / close.rolling(20, min_periods=20).mean()
        - 1.0,
        "MA20_MA60": close.rolling(20, min_periods=20).mean()
        / close.rolling(60, min_periods=60).mean()
        - 1.0,
    }
    for name in FACTOR_NAMES:
        pd.testing.assert_series_equal(
            calculated[name], expected[name], check_names=False, rtol=1e-12, atol=1e-12
        )


def test_amihud_nonpositive_amount_and_zero_volume_ratio_are_missing():
    bars = _bars(periods=25)
    bars.loc[5, "amount"] = 0.0
    bars["volume"] = 0.0
    result = compute_factors(bars, ["AMIHU20", "VOLUME_RATIO"])
    assert result["AMIHU20"].isna().all()
    assert result["VOLUME_RATIO"].isna().all()


@pytest.mark.parametrize("factor", FACTOR_NAMES)
def test_future_price_volume_mutation_cannot_change_past_factor(factor):
    bars = _bars(periods=75, symbols=("600000.SH", "000001.SZ"))
    cutoff = pd.Timestamp("2023-03-31")
    before = compute_factors(bars, [factor])
    mutated = bars.copy()
    future = mutated["date"] > cutoff
    mutated.loc[future, "adj_close"] *= 100.0
    mutated.loc[future, "volume"] *= 20.0
    mutated.loc[future, "turnover"] *= 5.0
    mutated.loc[future, "amount"] *= 50.0
    after = compute_factors(mutated, [factor])
    mask = before["date"] <= cutoff
    pd.testing.assert_series_equal(
        before.loc[mask, factor].reset_index(drop=True),
        after.loc[mask, factor].reset_index(drop=True),
    )


def test_cross_section_preprocessing_is_daily_and_preserves_raw_values():
    dates = pd.to_datetime(["2024-01-02"] * 5 + ["2024-01-03"] * 5)
    values = [0.0, 1.0, 2.0, 3.0, 1000.0, 100.0, 110.0, 120.0, 130.0, 10000.0]
    frame = pd.DataFrame(
        {
            "date": dates,
            "symbol": ["%06d.SZ" % value for value in range(10)],
            "F": values,
            "CONSTANT": [7.0] * 10,
        }
    )
    result = preprocess_cross_sections(
        frame, ["F", "CONSTANT"], mad_multiplier=1.0, minimum_count=5
    )
    assert result["raw_F"].tolist() == values
    for _, cross_section in result.groupby("date"):
        assert cross_section["F"].mean() == pytest.approx(0.0, abs=1e-12)
        assert cross_section["F"].std(ddof=0) == pytest.approx(1.0)
        assert cross_section["CONSTANT"].eq(0.0).all()
    # Different daily scales have no influence on each other's standardized ranks.
    np.testing.assert_allclose(
        result.loc[result["date"] == dates[0], "F"].to_numpy(),
        result.loc[result["date"] == dates[-1], "F"].to_numpy(),
    )


def test_cross_section_missing_values_are_never_filled():
    frame = pd.DataFrame(
        {
            "date": pd.to_datetime(["2024-01-02"] * 3),
            "symbol": ["000001.SZ", "000002.SZ", "000003.SZ"],
            "F": [1.0, np.nan, 3.0],
        }
    )
    result = preprocess_cross_sections(frame, ["F"], minimum_count=2)
    assert pd.isna(result.loc[1, "F"])
    assert pd.isna(result.loc[1, "raw_F"])
