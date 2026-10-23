"""Calendar-preserving HAC and overlapping moving-block inference.

Missing observations have zero estimating-equation score, not a zero return.
They retain their session position in HAC products and bootstrap blocks.
"""

import numpy as np
from scipy.stats import norm

from ..contract import ExploratoryContractError


STATES = ("BULL", "BEAR", "SIDEWAYS")
PAIRS = (("BULL", "BEAR"), ("BULL", "SIDEWAYS"), ("BEAR", "SIDEWAYS"))
LAGS = (5, 20)
BLOCK = 20
REPEATS = 2000
SEED = 20261014
MIN_DAYS = 60


def holm(pvalues):
    """Keep unavailable tests in the fixed family, conservatively as p=1."""
    p = np.asarray(pvalues, dtype=float)
    finite = np.isfinite(p)
    if ((p[finite] < 0) | (p[finite] > 1)).any():
        raise ExploratoryContractError("p-values outside [0, 1]")
    order = np.argsort(np.where(finite, p, 1), kind="stable")
    adjusted = np.maximum.accumulate(
        np.where(finite, p, 1)[order] * np.arange(len(p), 0, -1)
    ).clip(0, 1)
    result = np.empty(len(p))
    result[order] = adjusted
    result[~finite] = np.nan
    return result


def hac_fit(y, design, lag):
    """OLS sandwich with Bartlett weights and n/(n-k) correction.

    Uses asymptotic normal reference, not a Student t independence fiction.
    X rows must be defined on the complete session axis, including missing y.
    """
    y, x = np.asarray(y, float), np.asarray(design, float)
    if y.ndim != 1 or x.ndim != 2 or len(y) != len(x):
        raise ExploratoryContractError("HAC requires aligned daily y and design")
    if not np.isfinite(x).all() or np.isinf(y).any():
        raise ExploratoryContractError("nonfinite design or infinite daily RankIC")
    if type(lag) is not int or lag < 0 or lag >= len(y):
        raise ExploratoryContractError("HAC lag outside complete session axis")
    valid = np.isfinite(y)
    n, k = int(valid.sum()), x.shape[1]
    if n <= k or np.linalg.matrix_rank(x[valid]) < k:
        return None
    bread = np.linalg.inv(x[valid].T @ x[valid])
    beta = bread @ x[valid].T @ y[valid]
    score = np.zeros_like(x)
    score[valid] = x[valid] * (y[valid] - x[valid] @ beta)[:, None]
    meat = score.T @ score
    for offset in range(1, lag + 1):
        cross = score[offset:].T @ score[:-offset]
        meat += (1 - offset / float(lag + 1)) * (cross + cross.T)
    covariance = bread @ meat @ bread * n / float(n - k)
    return beta, (covariance + covariance.T) / 2


def contrast(fit, weights):
    if fit is None:
        return dict(
            estimate=np.nan,
            se=np.nan,
            ci_low=np.nan,
            ci_high=np.nan,
            p_two_sided=np.nan,
            numerical_status="UNAVAILABLE",
        )
    beta, covariance = fit
    w = np.asarray(weights, float)
    estimate = float(w @ beta)
    variance = float(w @ covariance @ w)
    # A constant/degenerate series cannot force a significance conclusion.
    if not np.isfinite(variance) or variance <= 1e-24:
        return dict(
            estimate=estimate,
            se=np.nan,
            ci_low=np.nan,
            ci_high=np.nan,
            p_two_sided=np.nan,
            numerical_status="DEGENERATE",
        )
    se = np.sqrt(variance)
    radius = norm.ppf(0.975) * se
    return dict(
        estimate=estimate,
        se=float(se),
        ci_low=estimate - radius,
        ci_high=estimate + radius,
        p_two_sided=float(2 * norm.sf(abs(estimate / se))),
        numerical_status="APPROXIMATE",
    )


def moving_block_indices(length, block=BLOCK, repeats=REPEATS, seed=SEED):
    """Non-circular, overlapping blocks; truncate only the last drawn block.

    Draw jointly for every factor and regime. Never stitch same-state dates
    before drawing. An index jump can occur only between sampled blocks.
    """
    if (
        type(length) is not int
        or type(block) is not int
        or type(repeats) is not int
        or length < block
        or block < 2
        or repeats < 2
    ):
        raise ExploratoryContractError("invalid moving block dimensions")
    starts = np.random.default_rng(seed).integers(
        0, length - block + 1, size=(repeats, int(np.ceil(length / block)))
    )
    return (starts[:, :, None] + np.arange(block)).reshape(repeats, -1)[:, :length]


def bootstrap_summary(draws, estimate):
    """Percentile CI and centered-null absolute-tail Monte Carlo test.

    p=(1+#{|theta* - theta_hat| >= |theta_hat|})/(B_valid+1).
    This is not a sign-count test. Percentile CI and this test are not dual.
    Non-circular block boundary bias is reported, not silently removed.
    """
    draws = np.asarray(draws, float)
    valid = draws[np.isfinite(draws)]
    result = dict(
        estimate=float(estimate),
        se=np.nan,
        ci_low=np.nan,
        ci_high=np.nan,
        p_two_sided=np.nan,
        valid_repeats=len(valid),
        bootstrap_bias=np.nan,
        numerical_status="UNAVAILABLE",
    )
    if len(valid) < max(100, int(0.95 * len(draws))) or not np.isfinite(estimate):
        return result
    if np.std(valid, ddof=1) <= 1e-12:
        result["numerical_status"] = "DEGENERATE"
        return result
    low, high = np.quantile(valid, [0.025, 0.975])
    result.update(
        se=float(np.std(valid, ddof=1)),
        ci_low=float(low),
        ci_high=float(high),
        p_two_sided=float(
            (1 + np.sum(abs(valid - estimate) >= abs(estimate))) / (len(valid) + 1)
        ),
        bootstrap_bias=float(np.mean(valid) - estimate),
        numerical_status="APPROXIMATE",
    )
    return result


def sample_means(y, indices, selection=None):
    y = np.asarray(y, float)
    keep = np.isfinite(y)
    if selection is not None:
        keep &= np.asarray(selection, bool)
    counts = keep[indices].sum(axis=1)
    total = np.where(keep, y, 0)[indices].sum(axis=1)
    return np.divide(total, counts, out=np.full(len(indices), np.nan), where=counts > 0)


def regime_design(regimes):
    r = np.asarray(regimes)
    if not np.isin(r, STATES).all():
        raise ExploratoryContractError("missing or unknown frozen market regime")
    # No intercept: three dummy coefficients are the three state means.
    return np.column_stack([r == state for state in STATES]).astype(float)


def run_count(mask):
    mask = np.asarray(mask, bool)
    return int(np.sum(mask & ~np.r_[False, mask[:-1]]))


def infer_panel(panel, regimes, factors):
    """Return raw descriptors, 14-test mean family, and 42-test state family."""
    descriptors, means, state_means, comparisons = [], [], [], []
    indices = moving_block_indices(len(panel))
    design = regime_design(regimes)
    r = np.asarray(regimes)
    for factor in factors:
        y = panel[factor].to_numpy(float)
        valid = np.isfinite(y)
        n = int(valid.sum())
        mean = float(np.mean(y[valid])) if n else np.nan
        sd = float(np.std(y[valid], ddof=1)) if n > 1 else np.nan
        descriptors.append(
            dict(
                factor=factor,
                mean=mean,
                std=sd,
                valid_dates=n,
                positive_ratio=float(np.mean(y[valid] > 0)) if n else np.nan,
                calendar_dates=len(y),
                valid_runs=run_count(valid),
                coverage=n / float(len(y)),
                iid_se_descriptive_only=sd / np.sqrt(n) if n else np.nan,
            )
        )
        for lag in LAGS:
            row = contrast(hac_fit(y, np.ones((len(y), 1)), lag), [1])
            means.append(
                dict(
                    factor=factor,
                    method="HAC%d" % lag,
                    valid_dates=n,
                    sample_insufficient=n < MIN_DAYS,
                    **row
                )
            )
        draws = sample_means(y, indices)
        means.append(
            dict(
                factor=factor,
                method="MBB20",
                valid_dates=n,
                sample_insufficient=n < MIN_DAYS,
                **bootstrap_summary(draws, mean)
            )
        )
        fits = {lag: hac_fit(y, design, lag) for lag in LAGS}
        state_draws = {}
        ns = {}
        for number, state in enumerate(STATES):
            selected = r == state
            observed = selected & valid
            ns[state] = int(observed.sum())
            theta = float(np.mean(y[observed])) if observed.any() else np.nan
            state_draws[state] = sample_means(y, indices, selected)
            counts = dict(
                factor=factor,
                state=state,
                mean=theta,
                signal_dates=int(selected.sum()),
                valid_dates=ns[state],
                signal_runs=run_count(selected),
                valid_runs=run_count(observed),
                coverage=(
                    ns[state] / float(selected.sum()) if selected.any() else np.nan
                ),
                sample_insufficient=ns[state] < MIN_DAYS,
            )
            for lag in LAGS:
                weights = np.eye(3)[number]
                row = contrast(fits[lag], weights)
                state_means.append(
                    dict(
                        **counts,
                        method="HAC%d" % lag,
                        **{k: v for k, v in row.items() if k != "estimate"}
                    )
                )
            row = bootstrap_summary(state_draws[state], theta)
            state_means.append(
                dict(
                    **counts,
                    method="MBB20",
                    **{k: v for k, v in row.items() if k != "estimate"}
                )
            )
        for left, right in PAIRS:
            weights = np.array([int(s == left) - int(s == right) for s in STATES])
            limited = min(ns[left], ns[right]) < MIN_DAYS
            counts = dict(
                factor=factor,
                comparison=left + "-" + right,
                left_valid_dates=ns[left],
                right_valid_dates=ns[right],
                sample_insufficient=limited,
            )
            for lag in LAGS:
                comparisons.append(
                    dict(**counts, method="HAC%d" % lag, **contrast(fits[lag], weights))
                )
            left_mean = np.mean(y[(r == left) & valid]) if ns[left] else np.nan
            right_mean = np.mean(y[(r == right) & valid]) if ns[right] else np.nan
            comparisons.append(
                dict(
                    **counts,
                    method="MBB20",
                    **bootstrap_summary(
                        state_draws[left] - state_draws[right], left_mean - right_mean
                    )
                )
            )
    import pandas as pd

    means, comparisons = pd.DataFrame(means), pd.DataFrame(comparisons)
    for table in (means, comparisons):
        table["p_holm"] = table.groupby("method")["p_two_sided"].transform(
            lambda p: holm(p.to_numpy())
        )
        table["conditional_support"] = (
            table.p_holm.lt(0.05)
            & ~table.sample_insufficient
            & table.numerical_status.eq("APPROXIMATE")
        )
    return pd.DataFrame(descriptors), means, pd.DataFrame(state_means), comparisons
