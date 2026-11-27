import numpy as np
import pytest

from factorlab.experiments.mc05_f06_statistics import (
    nested_difference, seed_summary, summarize,
)
from factorlab.research.grid_simulation import COLUMNS
from factorlab.research.monte_carlo import _loss_tail


def sample(returns, fills=None, inventory=None):
    returns = np.asarray(returns, dtype=float)
    n = len(returns)
    terminal = np.zeros((n, len(COLUMNS)))
    terminal[:, COLUMNS.index('equity')] = 1000*(1+returns)
    terminal[:, COLUMNS.index('fills')] = np.ones(n) if fills is None else fills
    terminal[:, COLUMNS.index('inventory')] = np.ones(n) if inventory is None else inventory
    terminal[:, COLUMNS.index('maximum_drawdown')] = np.maximum(0, -returns)
    auxiliary = dict(ever_inventory=terminal[:, COLUMNS.index('fills')] > 0,
                     terminal_exposure=np.full(n, 200.))
    return terminal, auxiliary


def test_exact_fractional_tail_not_average_of_batches():
    t, a = sample([0, 0, 0, 0, 0, 0, -.1, -.2, -.3, -.9, -.99])
    result = summarize(t, a)['groups']['unconditional']
    losses = 1-t[:, COLUMNS.index('equity')]/1000
    for confidence in (.95, .99):
        var, cvar = _loss_tail(losses, confidence)
        tail = result['tails'][str(confidence)]
        assert tail['var'] == pytest.approx(var)
        assert tail['cvar'] == pytest.approx(cvar)
        assert tail['cvar_interval']['lower'] is None
    assert result['net_return']['mean']['estimate'] == pytest.approx(-2.49/11)


def test_zero_fill_and_empty_conditional_groups_keep_denominators():
    t, a = sample([0, 0, 0], fills=[0, 0, 0], inventory=[0, 0, 0])
    groups = summarize(t, a)['groups']
    zero = groups['unconditional']['events']['zero_fill']
    assert zero['joint']['estimate'] == 1
    assert zero['conditional']['n'] == 3
    for name in ('traded', 'ever_inventory', 'terminal_inventory'):
        group = groups[name]
        assert group['n'] == 0
        assert group['tails'] is None
        assert group['net_return'] is None
        assert group['events']['loss']['conditional'] is None
        assert group['events']['loss']['joint']['n'] == 3
    tail = groups['unconditional']['tails']['0.95']
    assert tail['cvar'] == 0
    assert not tail['uncertainty_resolved']


def test_joint_conditional_and_exposure_units():
    t, a = sample([.1, -.2, 0, 0], fills=[1, 1, 0, 0], inventory=[1, 0, 0, 0])
    group = summarize(t, a)['groups']['traded']
    loss = group['events']['loss']
    assert loss['joint']['estimate'] == .25
    assert loss['conditional']['estimate'] == .5
    assert group['participation']['estimate'] == .5
    assert group['capital_return']['terminal_exposure']['ratio_mean_return_to_mean_exposure'] == pytest.approx(-.25)
    assert 'peak_exposure' not in group['distributions']


def test_nested_mean_and_conditional_influence_share_prefix():
    ut, ua = sample([.1, -.2, .3, -.4, .5, -.6], fills=[1, 0, 1, 0, 1, 1])
    lt, la = ut[:4], {k: v[:4] for k, v in ua.items()}
    delta = nested_difference(lt, la, ut, ua)
    r = ut[:, COLUMNS.index('equity')]/1000-1
    lm, um = la['ever_inventory'], ua['ever_inventory']
    lif = np.where(lm, r[:4]-r[:4][lm].mean(), 0)/lm.mean()
    uif = np.where(um, r-r[um].mean(), 0)/um.mean()
    expected_variance = (np.var(lif, ddof=1)/4 + np.var(uif, ddof=1)/6
                         - 2*np.cov(lif, uif[:4], ddof=1)[0, 1]/6)
    actual = delta['groups']['traded']['mean_net_return']
    assert actual['estimate'] == pytest.approx(r[um].mean()-r[:4][lm].mean())
    assert actual['standard_error'] == pytest.approx(np.sqrt(expected_variance))
    same = nested_difference(ut, ua, ut, ua)
    assert same['groups']['unconditional']['mean_net_return']['standard_error'] == 0
    bad = ut.copy()
    bad[0, 0] = 1
    with pytest.raises(ValueError, match='shared prefix'):
        nested_difference(lt, la, bad, ua)


def test_regular_cvar_influence_and_quantile_difference():
    ut, ua = sample(-np.linspace(0, .9, 5000))
    lt, la = ut[:4000], {k: v[:4000] for k, v in ua.items()}
    result = summarize(ut, ua)['groups']['unconditional']['tails']['0.95']
    assert result['uncertainty_resolved']
    assert result['cvar_interval']['standard_error'] > 0
    delta = nested_difference(lt, la, ut, ua)['groups']['unconditional']['tails']['0.95']
    assert delta['uncertainty_resolved']
    assert delta['cvar']['standard_error'] > 0
    assert delta['var']['lower'] <= delta['var']['estimate'] <= delta['var']['upper']


def test_tied_cutoff_with_unresolved_atom_has_no_cvar_interval():
    t, a = sample(-np.r_[np.zeros(950), np.ones(50)*.5])
    tail = summarize(t, a)['groups']['unconditional']['tails']['0.95']
    assert tail['cvar'] == pytest.approx(.5)
    assert 'UNRESOLVED_QUANTILE_ATOM' in tail['warnings']
    assert tail['cvar_interval']['standard_error'] is None


def test_singleton_conditional_nested_uncertainty_is_not_zero_width():
    terminal, aux = sample([-.1, 0, 0], fills=[1, 0, 0], inventory=[1, 0, 0])
    result = nested_difference(terminal[:2], {k: v[:2] for k, v in aux.items()}, terminal, aux)
    group = result['groups']['traded']
    assert group['mean_net_return']['estimate'] == 0
    assert group['mean_net_return']['standard_error'] is None
    assert group['events']['loss']['conditional']['standard_error'] is None


def test_seed_dispersion_is_descriptive():
    first = summarize(*sample([.1, -.1]))
    second = summarize(*sample([.2, 0]))
    summary = seed_summary([{'statistics': first}, {'statistics': second}])
    metric = summary['metrics']['unconditional.net_return.mean']
    assert summary['repeats'] == 2
    assert metric['seed_sd'] == pytest.approx(np.std([0, .1], ddof=1))
    assert metric['minimum'] == pytest.approx(0)
    assert metric['maximum'] == pytest.approx(.1)
    assert 'unconditional.distributions.terminal_exposure.quantiles.0.95' in summary['metrics']
    assert 'unconditional.capital_return.terminal_exposure.ratio_mean_return_to_mean_exposure' in summary['metrics']
