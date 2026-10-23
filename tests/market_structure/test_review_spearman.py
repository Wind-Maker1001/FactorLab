"""Pairwise ranks checked against a separate scalar reference, including ties."""

from dataclasses import replace
import itertools
import math

import numpy as np
import pandas as pd
import pytest

from factorlab.exploratory.market_structure.diagnostics import (
    RedundancyPolicy,
    diagnose_features,
)
from tests.market_structure.test_diagnostics import panel


def reference_correlation(x, y):
    if len(x) < 2:
        return math.nan
    # This reference uses scalar centering, without the old vectorized kernel.
    x = [float(v) for v in x]
    y = [float(v) for v in y]
    x = [v - math.fsum(x) / len(x) for v in x]
    y = [v - math.fsum(y) / len(y) for v in y]
    denominator = math.sqrt(math.fsum(v * v for v in x) * math.fsum(v * v for v in y))
    return (
        math.fsum(a * b for a, b in zip(x, y)) / denominator
        if denominator
        else math.nan
    )


def reference_average_ranks(values):
    return [
        sum(other < value for other in values)
        + (sum(other == value for other in values) + 1) / 2
        for value in values
    ]


def reference_pair(left, right, minimum):
    mask = np.isfinite(left) & np.isfinite(right)
    x, y = np.asarray(left)[mask], np.asarray(right)[mask]
    if mask.sum() < minimum:
        return int(mask.sum()), float(mask.mean()), math.nan, math.nan
    return (
        int(mask.sum()),
        float(mask.mean()),
        reference_correlation(x, y),
        reference_correlation(reference_average_ranks(x), reference_average_ranks(y)),
    )


def matrix_panel(columns, windows=6):
    base = panel(
        entities=len(next(iter(columns.values()))), windows=windows, sessions=3
    )
    pieces = []
    for name, values in columns.items():
        f = base[base.feature_id == "A"].copy()
        f["feature_id"] = name
        f["value"] = np.tile(values, windows)
        pieces.append(f)
    return pd.concat(pieces, ignore_index=True)


POLICY = RedundancyPolicy(
    minimum_security_entities=3,
    minimum_observation_windows=3,
    minimum_trading_sessions=3,
    minimum_pair_coverage=0.5,
)


@pytest.mark.parametrize(
    "columns",
    [
        dict(A=[1, 2, 3, np.nan], B=[1, 2, 100, 1.5]),
        dict(A=[1, 2, 3, 4, 5, 1e250, np.nan], B=[5, 4, 3, 2, 1, np.nan, -1e250]),
        dict(A=[1, 1, 3, 5, np.nan, 10], B=[2, 2, 1, 9, 100, np.nan]),
        dict(A=[1, 2, 3, 4, np.nan], B=[1, 2, 3, np.nan, 9]),
        dict(A=[1, 1, 1, np.nan], B=[2, 3, 4, 100]),
        dict(A=[1, 2, 3, 4], B=[1, 2, 3, 4]),
        dict(A=[1, 2, np.nan, np.nan], B=[4, 3, 2, 1]),
        dict(
            A=[1, 2, 3, np.nan, 7, 8],
            B=[4, 4, 9, 1, np.nan, 3],
            C=[1, 9, np.nan, 4, 2, 7],
        ),
    ],
)
def test_each_pair_window_and_verdict_match_independent_reference(columns):
    result = diagnose_features(matrix_panel(columns), POLICY)
    for left, right in itertools.combinations(sorted(columns), 2):
        n, coverage, pearson, spearman = reference_pair(
            columns[left], columns[right], POLICY.minimum_security_entities
        )
        windows = result["redundancy_windows"].query("left==@left and right==@right")
        assert len(windows) == 6
        assert windows.paired_entities.eq(n).all()
        assert windows.pair_coverage.eq(coverage).all()
        np.testing.assert_allclose(windows.pearson, pearson, atol=1e-12, equal_nan=True)
        np.testing.assert_allclose(
            windows.spearman, spearman, atol=1e-12, equal_nan=True
        )
        row = result["redundancy"].query("left==@left and right==@right").iloc[0]
        good = np.isfinite(pearson) and np.isfinite(spearman) and coverage >= 0.5
        assert row.valid_observation_windows == (6 if good else 0)
        assert row.total_observation_windows == 6
        assert row.distinct_trading_sessions == (3 if good else 0)
        assert row.mean_pair_coverage == pytest.approx(coverage)
        masks_same = np.array_equal(
            np.isfinite(columns[left]), np.isfinite(columns[right])
        )
        pair = np.isfinite(columns[left]) & np.isfinite(columns[right])
        same = masks_same and np.array_equal(
            np.asarray(columns[left])[pair], np.asarray(columns[right])[pair]
        )
        expected = (
            "UNDETERMINED"
            if not good
            else (
                "NUMERIC_DUPLICATE"
                if same
                else (
                    "HIGHLY_REDUNDANT"
                    if max(abs(pearson), abs(spearman)) >= 0.98
                    else "NON_REDUNDANT"
                )
            )
        )
        assert row.status == expected
        if good:
            assert row.mean_abs_spearman == pytest.approx(abs(spearman), abs=1e-12)


@pytest.mark.parametrize(
    "offset,expected",
    [
        (-1e-10, "HIGHLY_REDUNDANT"),
        (0.0, "HIGHLY_REDUNDANT"),
        (1e-10, "NON_REDUNDANT"),
    ],
)
def test_spearman_correlation_threshold_boundary(offset, expected):
    columns = dict(A=[1, 2, 3, 4, 5], B=[1, 2, 3, 100, 4])
    _, _, pearson, rho = reference_pair(columns["A"], columns["B"], 3)
    assert abs(pearson) < rho
    result = diagnose_features(
        matrix_panel(columns), replace(POLICY, correlation_threshold=rho + offset)
    )
    assert result["redundancy"].iloc[0].status == expected


def test_pairwise_mask_does_not_inflate_coverage_or_hide_failed_windows():
    f = matrix_panel(dict(A=[1, 2, 3, 4, np.nan], B=[1, 2, 100, np.nan, 5]))
    d = diagnose_features(f, replace(POLICY, minimum_pair_coverage=0.8))
    assert d["redundancy_windows"].pair_coverage.eq(3 / 5).all()
    assert d["redundancy_windows"].paired_entities.eq(3).all()
    assert d["redundancy"].iloc[0].total_observation_windows == 6
    assert d["redundancy"].iloc[0].valid_observation_windows == 0
    assert d["redundancy"].iloc[0].status == "UNDETERMINED"
