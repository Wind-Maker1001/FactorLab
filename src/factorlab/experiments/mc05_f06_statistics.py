"""Exact all-path F06 summaries and shared-prefix simulation uncertainty."""
import math

import numpy as np
from scipy.stats import norm

from factorlab.research.grid_simulation import COLUMNS, REASONS, STATES
from factorlab.research.monte_carlo import _loss_tail
from factorlab.research.monte_carlo.reliability import quantile_interval, wilson_interval


_Z = float(norm.ppf(.975))
_QUANTILES = (.05, .5, .95, .99)


def _column(terminal, name):
    return terminal[:, COLUMNS.index(name)]


def _masks(terminal, auxiliary):
    return dict(unconditional=np.ones(len(terminal), dtype=bool),
                traded=_column(terminal, 'fills') > 0,
                ever_inventory=np.asarray(auxiliary['ever_inventory'], dtype=bool),
                terminal_inventory=_column(terminal, 'inventory') != 0)


def _interval(estimate, influence, method='iid_path_influence_normal'):
    n = len(influence)
    se = float(np.std(influence, ddof=1) / math.sqrt(n)) if n > 1 else None
    return dict(estimate=float(estimate), lower=float(estimate - _Z*se) if se is not None else None,
                upper=float(estimate + _Z*se) if se is not None else None,
                standard_error=se, coverage=.95, method=method)


def _mean(values):
    return _interval(np.mean(values), values - np.mean(values))


def _distribution(values):
    return dict(mean=_mean(values), quantiles={str(q): float(np.quantile(values, q))
                                               for q in _QUANTILES},
                minimum=float(np.min(values)), maximum=float(np.max(values)))


def _tail(values, confidence):
    n = len(values)
    var, cvar = _loss_tail(values, confidence)
    mass = (1-confidence)*n
    # The generalized inverse, rather than interpolated VaR, gives the ES influence.
    cutoff = float(np.partition(values, min(n-1, math.ceil(confidence*n)-1))[
        min(n-1, math.ceil(confidence*n)-1)])
    excess = np.maximum(values-cutoff, 0)/(1-confidence)
    influence = excess - np.mean(excess)
    interval = _interval(cvar, influence)
    rank = quantile_interval(values, confidence)
    ties = int(np.count_nonzero(values == cutoff))
    warnings = []
    if mass < 20:
        warnings.append('FEWER_THAN_20_EXPECTED_TAIL_PATHS')
    if mass < 1:
        warnings.append('CVAR_EQUALS_SAMPLE_MAXIMUM')
    atom_unresolved = ties > 1 and not (rank['lower'] == cutoff == rank['upper'])
    if atom_unresolved:
        warnings.append('UNRESOLVED_QUANTILE_ATOM')
    if np.ptp(values) == 0:
        warnings.append('DEGENERATE_OBSERVED_SUPPORT')
    if rank['upper'] is None:
        warnings.append('UNBOUNDED_UPPER_QUANTILE_INTERVAL')
    unresolved = atom_unresolved or mass < 20 or np.ptp(values) == 0
    if unresolved:
        interval.update(lower=None, upper=None, standard_error=None)
    return dict(var=float(var), cvar=float(cvar), var_interval=rank, cvar_interval=interval,
                expected_tail_paths=float(mass),
                tail_paths_with_nonzero_weight=int(math.ceil(mass)),
                largest_path_tail_weight=float(min(1., 1/mass)),
                cutoff=cutoff, cutoff_ties=ties, distinct_losses=int(len(np.unique(values))),
                warnings=warnings, uncertainty_resolved=not unresolved), influence


def _event(event, mask):
    n, count = len(mask), int(np.count_nonzero(mask))
    hits = int(np.count_nonzero(event & mask))
    return dict(events=hits, population_n=n, group_n=count,
                joint=wilson_interval(hits, n),
                conditional=wilson_interval(hits, count) if count else None)


def _events(terminal, capital, threshold):
    returns = _column(terminal, 'equity') / capital - 1
    return dict(profit=returns > 0, loss=returns < 0, zero_return=returns == 0,
                zero_fill=_column(terminal, 'fills') == 0,
                drawdown_breach=_column(terminal, 'maximum_drawdown') > threshold,
                stopped=_column(terminal, 'state') != 0)


def summarize(terminal, auxiliary, capital=1000, threshold=.2):
    """Summarize aligned terminal rows and trace reductions, without batch averaging.

    ``ever_inventory`` is required. Exposure and committed arrays are USDT;
    capital-return ratios normalize them by initial capital. Lifetime reductions
    are optional and never inferred from terminal values. All intervals are pointwise 95% conditional
    simulation uncertainty, not market prediction or convergence certification.
    """
    terminal = np.asarray(terminal)
    n = len(terminal)
    if terminal.ndim != 2 or terminal.shape[1] != len(COLUMNS) or n == 0:
        raise ValueError('nonempty terminal rows in grid_simulation COLUMNS order required')
    if capital <= 0 or not 0 <= threshold <= 1:
        raise ValueError('positive capital and threshold in [0, 1] required')
    if any(np.asarray(v).shape != (n,) for v in auxiliary.values()):
        raise ValueError('auxiliary arrays must align with terminal rows')
    returns = _column(terminal, 'equity') / capital - 1
    events = _events(terminal, capital, threshold)
    groups = {}
    for name, mask in _masks(terminal, auxiliary).items():
        count = int(np.count_nonzero(mask))
        group = dict(n=count, population_n=n, participation=wilson_interval(count, n),
                     events={key: _event(value, mask) for key, value in events.items()})
        groups[name] = group
        if not count:
            group.update(net_return=None, tails=None, maximum_drawdown_quantiles=None,
                         distributions=None, states=None, reasons=None, capital_return=None)
            continue
        r = returns[mask]
        group['net_return'] = dict(mean=_mean(r), median=quantile_interval(r, .5))
        group['tails'] = {str(c): _tail(-r, c)[0] for c in (.95, .99)}
        group['maximum_drawdown_quantiles'] = {
            str(q): quantile_interval(_column(terminal, 'maximum_drawdown')[mask], q)
            for q in (.5, .95, .99)}
        values = {key: _column(terminal, key) for key in
                  ('fills', 'total_fees', 'total_slippage', 'inventory', 'committed', 'available')}
        values.update({key: np.asarray(value) for key, value in auxiliary.items()
                       if key != 'ever_inventory'})
        group['distributions'] = {key: _distribution(value[mask]) for key, value in values.items()}
        group['states'] = {label: _event(_column(terminal, 'state') == i, mask)
                           for i, label in enumerate(STATES)}
        group['reasons'] = {str(label): _event(_column(terminal, 'reason') == i, mask)
                            for i, label in enumerate(REASONS)}
        relationships = {}
        capital_values = {'terminal_committed': _column(terminal, 'committed')}
        capital_values.update(auxiliary)
        for key, value in capital_values.items():
            if 'exposure' not in key and 'committed' not in key:
                continue
            x = np.asarray(value)[mask] / capital
            positive = x > 0
            relationships[key] = dict(
                return_correlation=float(np.corrcoef(x, r)[0, 1])
                    if count > 1 and np.ptp(x) > 0 and np.ptp(r) > 0 else None,
                positive_denominator_n=int(np.count_nonzero(positive)),
                mean_path_return_per_exposure=float(np.mean(r[positive]/x[positive]))
                    if positive.any() else None,
                ratio_mean_return_to_mean_exposure=float(np.mean(r)/np.mean(x))
                    if np.mean(x) > 0 else None)
        group['capital_return'] = relationships
    return dict(n=n, capital=float(capital), drawdown_threshold=threshold, groups=groups,
                interval_meaning='pointwise conditional simulation uncertainty; not market prediction',
                statistical_status='PRECISION_REPORTED_NOT_CERTIFIED')


def _paired_interval(lower, upper, lower_if, upper_if):
    nl, nu = len(lower_if), len(upper_if)
    if min(nl, nu) < 2:
        return dict(estimate=float(upper-lower), lower=None, upper=None,
                    standard_error=None, coverage=.95, method='shared_prefix_influence_normal')
    covariance = float(np.cov(lower_if, upper_if[:nl], ddof=1)[0, 1])
    variance = (np.var(lower_if, ddof=1)/nl + np.var(upper_if, ddof=1)/nu
                - 2*covariance/nu)
    se = math.sqrt(max(0., float(variance)))
    estimate = float(upper-lower)
    return dict(estimate=estimate, lower=estimate-_Z*se, upper=estimate+_Z*se,
                standard_error=se, coverage=.95, method='shared_prefix_influence_normal')


def _conditional_influence(values, mask):
    mean = float(np.mean(values[mask]))
    return mean, np.where(mask, values-mean, 0) / np.mean(mask)


def _quantile_difference(lower, upper, q):
    lo, hi = (quantile_interval(v, q, coverage=.975) for v in (lower, upper))
    return dict(estimate=hi['estimate']-lo['estimate'],
                lower=hi['lower']-lo['upper'] if hi['lower'] is not None and lo['upper'] is not None else None,
                upper=hi['upper']-lo['lower'] if hi['upper'] is not None and lo['lower'] is not None else None,
                coverage=.95, method='bonferroni_quantile_endpoint_difference')


def nested_difference(lower_terminal, lower_auxiliary, upper_terminal, upper_auxiliary,
                      capital=1000, threshold=.2):
    """Upper-minus-lower estimates for an exactly shared lower prefix.

    Conditional influence vectors are embedded in full population indices before
    shared-prefix covariance is computed. Quantile bounds allocate error across
    the two marginal intervals and require no independence assumption.
    """
    lt, ut = np.asarray(lower_terminal), np.asarray(upper_terminal)
    nl, nu = len(lt), len(ut)
    if nl > nu or not np.array_equal(lt, ut[:nl]) or any(
            not np.array_equal(v, np.asarray(upper_auxiliary[k])[:nl])
            for k, v in lower_auxiliary.items()):
        raise ValueError('lower sample must be the exact shared prefix of upper')
    masks = (_masks(lt, lower_auxiliary), _masks(ut, upper_auxiliary))
    events = (_events(lt, capital, threshold), _events(ut, capital, threshold))
    returns = (_column(lt, 'equity')/capital-1, _column(ut, 'equity')/capital-1)
    groups = {}
    for name in masks[0]:
        lm, um = masks[0][name], masks[1][name]
        group = dict(lower_n=int(lm.sum()), upper_n=int(um.sum()),
                     participation=_paired_interval(lm.mean(), um.mean(), lm-lm.mean(), um-um.mean()))
        groups[name] = group
        group['events'] = {}
        for key in events[0]:
            lv, uv = events[0][key] & lm, events[1][key] & um
            joint = _paired_interval(lv.mean(), uv.mean(), lv-lv.mean(), uv-uv.mean())
            conditional = None
            if lm.any() and um.any():
                le, li = _conditional_influence(events[0][key], lm)
                ue, ui = _conditional_influence(events[1][key], um)
                conditional = _paired_interval(le, ue, li, ui)
                if min(lm.sum(), um.sum()) < 2:
                    conditional.update(lower=None, upper=None, standard_error=None,
                        diagnostic='INSUFFICIENT_CONDITIONAL_PATHS')
            group['events'][key] = dict(joint=joint, conditional=conditional)
        if not lm.any() or not um.any():
            group.update(mean_net_return=None, median_net_return=None, tails=None,
                         maximum_drawdown_quantiles=None)
            continue
        le, li = _conditional_influence(returns[0], lm)
        ue, ui = _conditional_influence(returns[1], um)
        group['mean_net_return'] = _paired_interval(le, ue, li, ui)
        if min(lm.sum(), um.sum()) < 2:
            group['mean_net_return'].update(lower=None, upper=None, standard_error=None,
                diagnostic='INSUFFICIENT_CONDITIONAL_PATHS')
        group['median_net_return'] = _quantile_difference(returns[0][lm], returns[1][um], .5)
        group['maximum_drawdown_quantiles'] = {str(q): _quantile_difference(
            _column(lt, 'maximum_drawdown')[lm], _column(ut, 'maximum_drawdown')[um], q)
            for q in (.5, .95, .99)}
        group['tails'] = {}
        for c in (.95, .99):
            low, lif = _tail(-returns[0][lm], c)
            high, uif = _tail(-returns[1][um], c)
            lv, uv = np.zeros(nl), np.zeros(nu)
            lv[lm], uv[um] = lif/lm.mean(), uif/um.mean()
            interval = _paired_interval(low['cvar'], high['cvar'], lv, uv)
            resolved = low['uncertainty_resolved'] and high['uncertainty_resolved']
            if not resolved:
                interval.update(lower=None, upper=None, standard_error=None)
            group['tails'][str(c)] = dict(
                var=_quantile_difference(-returns[0][lm], -returns[1][um], c),
                cvar=interval, uncertainty_resolved=resolved)
    return dict(lower_n=nl, upper_n=nu, direction='upper_minus_lower', groups=groups)


def seed_summary(cells):
    """Descriptive seed dispersion for cells from the same model/window/size."""
    metrics = {}
    def collect(value, path=''):
        if isinstance(value, dict):
            if 'estimate' in value:
                estimate = value['estimate']
                if estimate is not None:
                    metrics.setdefault(path, []).append(float(estimate))
                return
            for key, item in value.items():
                estimand = (key in ('mean', 'median', 'var', 'cvar', 'return_correlation',
                    'mean_path_return_per_exposure', 'ratio_mean_return_to_mean_exposure')
                    or path.endswith('.quantiles'))
                if estimand and isinstance(item, (int, float)):
                    metrics.setdefault(f'{path}.{key}', []).append(float(item))
                elif isinstance(item, dict):
                    collect(item, f'{path}.{key}' if path else key)
    for cell in cells:
        collect(cell.get('statistics', cell).get('groups', {}))
    return dict(repeats=len(cells),
                meaning='descriptive independent-seed dispersion; not a confidence interval',
                metrics={key: dict(n=len(v), mean=float(np.mean(v)),
                    seed_sd=float(np.std(v, ddof=1)) if len(v) > 1 else None,
                    minimum=float(min(v)), maximum=float(max(v))) for key, v in metrics.items()})
