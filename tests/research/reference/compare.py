"""Fixed comparison policy shared by Golden tests and offline benchmarks."""

import numpy as np
import pandas as pd

TABLES = ("daily_ic", "summary", "quantile_returns", "autocorrelation", "turnover", "correlation")
ATOL = RTOL = 1e-12


def assert_evaluation_equal(actual, expected):
    for name in TABLES:
        left, right = getattr(actual, name), getattr(expected, name)
        pd.testing.assert_index_equal(left.index, right.index, exact=True)
        pd.testing.assert_index_equal(left.columns, right.columns, exact=True)
        pd.testing.assert_series_equal(left.dtypes, right.dtypes)
        pd.testing.assert_frame_equal(left.isna(), right.isna(), check_exact=True)
        for column in left:
            if pd.api.types.is_float_dtype(left[column]):
                a, b = left[column].to_numpy(), right[column].to_numpy()
                np.testing.assert_array_equal(np.isposinf(a), np.isposinf(b))
                np.testing.assert_array_equal(np.isneginf(a), np.isneginf(b))
                valid = np.isfinite(b)
                np.testing.assert_allclose(a[valid], b[valid], rtol=RTOL, atol=ATOL)
            else:
                pd.testing.assert_series_equal(left[column], right[column], check_exact=True)
