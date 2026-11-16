"""Independent references for conditional path uncertainty and experiment records."""
from dataclasses import replace
import importlib.util
import json
import math
from pathlib import Path

import numpy as np
import pytest
from scipy.integrate import quad
from scipy.stats import binomtest, norm

from factorlab.experiments import mc_tail_reliability as study
from factorlab.research.monte_carlo import MonteCarloConfig, MonteCarloInput, MonteCarloRunner
from factorlab.research.monte_carlo.reliability import (
    quantile_interval, repeat_summary, summarize_paths, tail_statistics, wilson_interval)


@pytest.mark.parametrize('events,n', [(0,16),(16,16),(5,100),(500,1000)])
def test_wilson_independent_reference(events, n):
    actual = wilson_interval(events,n)
    expected = binomtest(events,n).proportion_ci(method='wilson')
    assert actual['lower'] == pytest.approx(expected.low,abs=1e-15)
    assert actual['upper'] == pytest.approx(expected.high,abs=1e-15)


@pytest.mark.parametrize('n,q', [(16,.95),(100,.95),(1000,.99), (20,.5)])
def test_rank_interval_coverage_by_binomial_sum(n,q):
    result = quantile_interval(np.arange(n),q)
    low, high = result['lower_rank'], result['upper_rank']
    coverage = sum(math.comb(n,k)*q**k*(1-q)**(n-k) for k in range(low,high))
    assert coverage >= .95-1e-12
    assert result['continuous_population_rank_coverage'] == pytest.approx(coverage)
    assert sum(math.comb(n,k)*q**k*(1-q)**(n-k) for k in range(low)) <= .025+1e-12
    assert sum(math.comb(n,k)*q**k*(1-q)**(n-k) for k in range(high,n+1)) <= .025+1e-12
    if n==16:
        assert result['upper'] is None and result['upper_unbounded']


def test_normal_quantile_var_es_against_analytic_and_integrated_reference():
    n=100000
    sample=norm.ppf((np.arange(n)+.5)/n)*.2+.03
    result=tail_statistics(sample, resamples=5, seed=31)
    for c in (.95,.99):
        z=norm.ppf(c)
        expected=.03+.2*norm.pdf(z)/(1-c)
        integrated=quad(lambda x:x*norm.pdf(x,loc=.03,scale=.2), .03+.2*z, np.inf)[0]/(1-c)
        assert expected == pytest.approx(integrated,abs=1e-10)
        assert result[str(c)]['var'] == pytest.approx(.03+.2*z,abs=1e-4)
        assert result[str(c)]['cvar'] == pytest.approx(expected,abs=1e-4)


def test_fractional_tail_and_extreme_sample_degeneracy():
    losses=np.array([-.4,-.1,.2,.8])
    result=tail_statistics(losses, confidences=(.625,.95),resamples=199,seed=12)
    assert result['0.625']['var'] == pytest.approx(.1625)
    assert result['0.625']['cvar'] == pytest.approx((.8+.5*.2)/1.5)
    assert result['0.95']['cvar'] == .8
    assert result['0.95']['largest_path_tail_weight'] == 1
    assert result['0.95']['statistical_status']=='STATISTICALLY_UNSTABLE'
    identical=tail_statistics(np.zeros(100),resamples=19)['0.95']
    assert 'DEGENERATE_EMPIRICAL_BOOTSTRAP' in identical['warnings']


def test_bootstrap_against_scalar_independent_resampling_reference():
    losses=np.array([-.2,.1,.3,.7,.9])
    result=tail_statistics(losses,confidences=(.7,),resamples=99,seed=77)['0.7']
    rng=np.random.RandomState(77)
    values=[]
    for _ in range(99):
        sample=sorted(losses[rng.randint(0,5,5)],reverse=True)
        values.append((sample[0]+.5*sample[1])/1.5)
    expected=np.quantile(values,[.025,.975])
    assert [result['cvar_interval']['lower'],result['cvar_interval']['upper']] == pytest.approx(expected)


def test_fixed_seed_and_independent_statistics_rng():
    inputs=MonteCarloInput(weights=[1.],drift=[.001],volatility=[.02])
    cfg=MonteCarloConfig(n_paths=100,n_steps=12,seed=1729,batch_size=16,dt=1.)
    first=MonteCarloRunner(cfg).run(inputs)
    a=summarize_paths(1-first.terminal_values,first.maximum_drawdowns,resamples=19,bootstrap_seed=42)
    b=summarize_paths(1-first.terminal_values,first.maximum_drawdowns,resamples=19,bootstrap_seed=42)
    second=MonteCarloRunner(cfg).run(inputs)
    assert a==b
    np.testing.assert_array_equal(first.terminal_values,second.terminal_values)


def test_strict_event_thresholds_and_quantile_ties():
    result=summarize_paths([0,.2,-.2],[0,.2,.3],threshold=.2,resamples=19)
    assert result['loss_probability']['events']==1
    assert result['drawdown_breach_probability']['events']==1
    tied=quantile_interval(np.zeros(1000),.95)
    assert tied['lower']==tied['upper']==0


def historical():
    times=np.arange('2026-01-01T00','2026-04-01T00',dtype='datetime64[h]')
    returns=(.001*np.sin(np.arange(len(times))*.2))[:,None]
    return MonteCarloInput(weights=[1.],historical_returns=returns,timestamps=times,
        available_at=times,as_of=np.datetime64('2026-04-02'),assets=('TEST',),initial_prices=[100.])


def test_window_calibration_and_no_future_availability():
    inputs=historical()
    actual=study.calibrate_window(inputs,720)
    logs=np.log1p(inputs.historical_returns[-720:])
    assert actual.volatility[0]==pytest.approx(logs.std(ddof=1))
    assert actual.drift[0]==pytest.approx(logs.mean()+.5*logs.var(ddof=1))
    bad=replace(inputs,available_at=np.full(len(inputs.timestamps),np.datetime64('2026-04-03')))
    with pytest.raises(ValueError,match='unavailable'):
        study.calibrate_window(bad,720)
    assert study.oos_feasibility(inputs)['observed_coverage'] is None


def test_all_sizes_seeds_recorded_and_compact_output(tmp_path,monkeypatch):
    inputs=historical()
    monkeypatch.setattr(study,'historical_input',lambda *args:(inputs,{'as_of':'2026-04-02T00:00:00Z'}))
    monkeypatch.setattr(study,'capture_provenance',lambda root:{'code':{'commit':'test'}})
    result=study.run_reliability(tmp_path/'run',snapshot='unused',as_of='2026-04-02T00:00:00Z',
        start='2026-04-03T00:00:00Z',seeds=(7,11),sizes=(20,40),resamples=9,steps=4,grid_paths=0)
    primary=[r for r in result['rows'] if r['phase']=='primary']
    assert {(r['model'],r['n_paths'],r['seed']) for r in primary} == {
        (model,n,seed) for model in study.MODELS for n in (20,40) for seed in (7,11)}
    for model in study.MODELS:
        values=[r['statistics']['tails']['0.95']['cvar'] for r in primary if r['model']==model]
        assert len(set(values))>1
    encoded=(tmp_path/'run/results.json').read_text()
    assert 'asset_paths' not in encoded and 'portfolio_paths' not in encoded
    assert json.loads(encoded)['financial_status']=='FINANCIAL_NOT_ESTABLISHED'
    assert all(r['repeats']==2 for r in repeat_summary(primary))


def test_budget_skips_are_explicit(tmp_path,monkeypatch):
    inputs=historical()
    monkeypatch.setattr(study,'historical_input',lambda *args:(inputs,{}))
    monkeypatch.setattr(study,'capture_provenance',lambda root:{'code':{}})
    result=study.run_reliability(tmp_path/'run',snapshot='unused',as_of='2026-04-02T00:00:00Z',
        start='2026-04-03T00:00:00Z',budget_seconds=1e-9,seeds=(7,),sizes=(20,),resamples=9,grid_paths=0)
    assert not result['rows']
    assert any(r['reason']=='TIME_BUDGET' for r in result['skipped'])
    assert result['engineering_status']=='NOT_ESTABLISHED'


def test_cli_arguments(monkeypatch,capsys):
    path=Path(__file__).resolve().parents[2]/'tools/research/run_mc_tail_reliability.py'
    spec=importlib.util.spec_from_file_location('mc02_cli',path)
    cli=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    received={}
    def run(**kwargs):
        received.update(kwargs)
        return dict(status='COMPLETED',rows=[],skipped=[],elapsed_seconds=0,
                    engineering_status='NOT_ESTABLISHED',financial_status='FINANCIAL_NOT_ESTABLISHED')
    monkeypatch.setattr(cli,'run_reliability',run)
    cli.main(['--snapshot','s','--as-of','a','--start','b','--output','o','--budget-seconds','15'])
    assert received['budget_seconds']==15
    assert json.loads(capsys.readouterr().out)['status']=='COMPLETED'
