"""Pointwise conditional simulation uncertainty; not market prediction intervals."""
from collections import defaultdict
import math

import numpy as np
from scipy.stats import binom, norm

from factorlab.research.monte_carlo import _loss_tail


def _sample(values):
    values = np.asarray(values, dtype=float)
    if values.ndim != 1 or not len(values) or not np.isfinite(values).all():
        raise ValueError('a nonempty finite one-dimensional sample is required')
    return values


def _probability(value):
    if not 0 < value < 1:
        raise ValueError('probability must lie strictly between zero and one')


def wilson_interval(events, n, coverage=.95):
    """Wilson score interval for independent Bernoulli trials, including 0/n."""
    _probability(coverage)
    if n <= 0 or not 0 <= events <= n or int(events) != events or int(n) != n:
        raise ValueError('integer event count in [0, n] and positive integer n required')
    p = events / n
    z = float(norm.ppf((1 + coverage) / 2))
    denominator = 1 + z*z/n
    center = (p + z*z/(2*n)) / denominator
    half = z * math.sqrt(p*(1-p)/n + z*z/(4*n*n)) / denominator
    return dict(estimate=p, lower=max(0., center-half), upper=min(1., center+half),
                coverage=coverage, method='wilson_score', events=int(events), n=int(n))


def quantile_interval(values, q, coverage=.95):
    """Binomial-rank interval; null endpoints mean unbounded, not sample extrema.

    Ranks are one-based with X_(0)=-infinity and X_(n+1)=+infinity.
    Coverage is at least nominal for a continuous population; ties are conservative.
    The point estimate remains the engine's linear interpolated quantile.
    """
    values = np.sort(_sample(values))
    _probability(q)
    _probability(coverage)
    n = len(values)
    alpha = (1-coverage)/2
    lower_rank = int(binom.ppf(alpha, n, q))
    upper_rank = int(binom.ppf(1-alpha, n, q)) + 1
    achieved = float(binom.cdf(upper_rank-1, n, q) - binom.cdf(lower_rank-1, n, q))
    return dict(estimate=float(np.quantile(values, q)),
        lower=float(values[lower_rank-1]) if lower_rank else None,
        upper=float(values[upper_rank-1]) if upper_rank <= n else None,
        lower_unbounded=lower_rank == 0, upper_unbounded=upper_rank > n,
        lower_rank=lower_rank, upper_rank=upper_rank,
        coverage=coverage, continuous_population_rank_coverage=achieved,
        method='binomial_order_statistics', quantile=q)


def tail_statistics(losses, confidences=(.95, .99), *, coverage=.95,
                    resamples=999, seed=0):
    """Fractional-tail CVaR bootstrap, resampling independent path-level losses.

    The empirical bootstrap cannot generate losses outside observed support.
    Low tail mass is explicitly flagged even when bootstrap spread is nonzero.
    """
    losses = _sample(losses)
    _probability(coverage)
    if type(resamples) is not int or resamples < 2:
        raise ValueError('at least two bootstrap resamples are required')
    for c in confidences:
        _probability(c)
    n = len(losses)
    rng = np.random.RandomState(seed)
    estimates = np.empty((resamples, len(confidences)))
    # Bound temporary index/value arrays, independently of the simulation batch plan.
    chunk = max(1, min(32, 262144 // n))
    max_needed = min(n, max(int(np.floor((1 - c) * n)) + 1 for c in confidences))
    use_partition = max_needed < n
    kth = n - max_needed
    for begin in range(0, resamples, chunk):
        count = min(chunk, resamples-begin)
        sample = losses[rng.randint(0, n, size=(count, n))]
        if use_partition:
            top = np.partition(sample, kth, axis=1)[:, kth:]
            descending = np.sort(top, axis=1)[:, ::-1]
        else:
            descending = np.sort(sample, axis=1)[:, ::-1]
        for j, c in enumerate(confidences):
            mass = (1-c)*n
            whole = int(np.floor(mass))
            fraction = mass-whole
            if fraction:
                estimates[begin:begin+count, j] = (descending[:, :whole].sum(axis=1)
                    + fraction*descending[:, whole]) / mass
            else:
                estimates[begin:begin+count, j] = descending[:, :whole].sum(axis=1) / mass
    results = {}
    for j, c in enumerate(confidences):
        var, cvar = _loss_tail(losses, c)
        mass = (1-c)*n
        fraction_of_tail = min(1., 1/mass)
        lower, upper = np.quantile(estimates[:, j], [(1-coverage)/2, (1+coverage)/2])
        degenerate = bool(lower == upper)
        quantile = quantile_interval(losses, c, coverage)
        warnings = []
        if mass < 1:
            warnings.append('CVAR_EQUALS_SAMPLE_MAXIMUM')
        if mass < 20:
            warnings.append('FEWER_THAN_20_EXPECTED_TAIL_PATHS_DIAGNOSTIC_NOT_ACCEPTANCE_RULE')
        if degenerate:
            warnings.append('DEGENERATE_EMPIRICAL_BOOTSTRAP')
        if quantile['upper_unbounded']:
            warnings.append('NO_FINITE_DISTRIBUTION_FREE_UPPER_QUANTILE_BOUND')
        results[str(c)] = dict(var=var, cvar=cvar, var_interval=quantile,
            cvar_interval=dict(lower=float(lower), upper=float(upper), coverage=coverage,
                method='iid_path_percentile_bootstrap', resamples=resamples,
                conditional_on_observed_support=True, bootstrap_seed=seed),
            expected_tail_paths=mass, tail_paths_with_nonzero_weight=int(math.ceil(mass)),
            largest_path_tail_weight=fraction_of_tail,
            largest_path_cvar_contribution=float(losses.max()*fraction_of_tail),
            distinct_losses=int(len(np.unique(losses))), warnings=warnings,
            statistical_status='STATISTICALLY_UNSTABLE' if warnings else 'PRECISION_REPORTED_NOT_CERTIFIED')
    return results


def summarize_paths(losses, drawdowns, *, threshold=.2, confidences=(.95, .99),
                    resamples=999, bootstrap_seed=0):
    """Summarize independent paths, retaining no full histories or sample vectors."""
    losses, drawdowns = _sample(losses), _sample(drawdowns)
    if len(losses) != len(drawdowns) or np.any((drawdowns < 0) | (drawdowns > 1)):
        raise ValueError('aligned losses and drawdowns in [0, 1] required')
    if not 0 <= threshold <= 1:
        raise ValueError('drawdown threshold must be in [0, 1]')
    return dict(n=len(losses), tails=tail_statistics(losses, confidences,
            resamples=resamples, seed=bootstrap_seed),
        loss_probability=wilson_interval(int(np.sum(losses > 0)), len(losses)),
        drawdown_breach_probability=wilson_interval(int(np.sum(drawdowns > threshold)), len(losses)),
        drawdown_threshold=threshold,
        maximum_drawdown_quantiles={str(q): quantile_interval(drawdowns, q)
                                   for q in (.5, .95, .99)},
        interval_meaning='pointwise conditional simulation uncertainty; not financial prediction accuracy')


def repeat_summary(rows):
    """Descriptive seed dispersion, not confidence intervals or paired contrasts."""
    groups = defaultdict(list)
    for row in rows:
        groups[(row['model'], row['window'], row['block_size'], row['n_paths'])].append(row)
    output = []
    for (model, window, block, n), samples in groups.items():
        metrics = defaultdict(list)
        for row in samples:
            result = row['statistics']
            for c, tail in result['tails'].items():
                for name in ('var', 'cvar'):
                    metrics[f'{name}_{c}'].append(tail[name])
            for name in ('loss_probability', 'drawdown_breach_probability'):
                metrics[name].append(result[name]['estimate'])
            for q, value in result['maximum_drawdown_quantiles'].items():
                metrics[f'drawdown_{q}'].append(value['estimate'])
        output.append(dict(model=model, window=window, block_size=block, n_paths=n,
            seeds=[s['seed'] for s in samples], repeats=len(samples),
            meaning='descriptive independent-seed range and sample SD; not market prediction interval',
            metrics={key: dict(mean=float(np.mean(v)), minimum=float(min(v)), maximum=float(max(v)),
                seed_sd=float(np.std(v, ddof=1)) if len(v)>1 else None)
                     for key, v in metrics.items()}))
    return output
