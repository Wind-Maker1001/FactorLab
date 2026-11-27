"""MC-COLAB-02 contracts, exact storage, and actual backend probes."""
from dataclasses import replace
import json
from pathlib import Path

import numpy as np
import pytest

from factorlab.compute import ComputeConfig
from factorlab.experiments.mc_colab_benchmark import (
    analytic_gbm, compare_studies, cu_cost, make_inputs, memory_plan,
    performance_comparison, point_statistics, run_study, seed_summary, simulation_config, validate_config,
)
from factorlab.research.monte_carlo import MonteCarloConfig, MonteCarloInput, MonteCarloRunner, _loss_tail
from factorlab.research.monte_carlo.storage import allocate_outputs, disk_summary


ROOT=Path(__file__).resolve().parents[2]


def configuration():
    c=json.loads((ROOT/'configs/research/mc_colab_02.json').read_text())
    c['cells']=c['cells'][:1]
    c['cells'][0].update(n_paths=257,n_steps=7,batch_size=31)
    c['timing_repeats']=1
    c['bootstrap_resamples']=9
    return c


@pytest.mark.parametrize('key,value',[
    ('seeds',[1,1,2,3,4]),('seeds',[1,2,3,4]),('timing_repeats',0),
    ('bootstrap_resamples',1),('budget_seconds',float('nan'))])
def test_configuration_rejects_invalid_design(key,value):
    c=configuration()
    c[key]=value
    with pytest.raises(ValueError):
        validate_config(c)


@pytest.mark.parametrize('model',['gbm','moving_block_bootstrap'])
def test_exact_disk_backed_outputs_and_profile(tmp_path,model):
    cell=configuration()['cells'][0]
    cell['model']=model
    c=simulation_config(cell,13,'cpu')
    inputs=make_inputs(cell)
    a=MonteCarloRunner(c).run(inputs)
    b=MonteCarloRunner(c).run(inputs,output_directory=tmp_path/'samples',profile=True)
    np.testing.assert_array_equal(a.terminal_values,b.terminal_values)
    np.testing.assert_array_equal(a.maximum_drawdowns,b.maximum_drawdowns)
    for name in ('var','cvar','loss_probability','drawdown_breach_probability'):
        assert getattr(b,name)==pytest.approx(getattr(a,name),abs=1e-14)
    assert b.terminal_quantiles==pytest.approx(a.terminal_quantiles)
    assert b.drawdown_quantiles==pytest.approx(a.drawdown_quantiles)
    assert 'result_statistics' in b.metadata['phase_seconds']
    assert 'phase_seconds' not in a.metadata
    assert all(v>=0 for v in b.metadata['phase_seconds'].values())
    assert b.metadata['compute']['execution']['batch_size']==31
    with pytest.raises(FileExistsError):
        allocate_outputs(tmp_path/'samples',257)


def test_fractional_tail_crosses_batch_boundary(tmp_path):
    terminal,dd=allocate_outputs(tmp_path/'tail',13)
    losses=np.array([-.5,.1,.9,.2,.2,.4,.8,0.,-.1,.3,.7,.5,.6])
    terminal[:]=1-losses
    dd[:]=np.linspace(0,.7,13)
    r=disk_summary(terminal,dd,tmp_path/'tail',initial=1.,confidence=.81,
        quantiles=(0.,.51,1.),threshold=.35)
    var,cvar=_loss_tail(losses,.81)
    assert r['var']==pytest.approx(var)
    assert r['cvar']==pytest.approx(cvar)
    assert r['drawdown_breach_probability']==pytest.approx(np.mean(dd>.35))


def test_fixed_initial_peak_and_bootstrap():
    times=np.array(['2020-01-01','2020-01-02'],dtype='datetime64[D]')
    inputs=MonteCarloInput(weights=[1.],historical_returns=[[-.1],[-.1]],
        timestamps=times,available_at=times,as_of='2020-01-02')
    c=MonteCarloConfig(model='moving_block_bootstrap',n_paths=5,n_steps=3,block_size=1)
    result=MonteCarloRunner(c).run(inputs)
    np.testing.assert_allclose(result.terminal_values,.9**3,rtol=1e-10,atol=1e-12)
    np.testing.assert_allclose(result.maximum_drawdowns,1-.9**3,rtol=1e-10,atol=1e-12)


def test_analytic_gbm_matches_large_cpu_sample():
    cell=dict(configuration()['cells'][0],n_paths=100000,n_steps=1,batch_size=4096)
    result=MonteCarloRunner(simulation_config(cell,104729,'cpu')).run(make_inputs(cell))
    observed=point_statistics(result)
    expected=analytic_gbm(1)
    # Sampling tolerance fixed before hardware experiments; not an equivalence margin.
    for metric in ('var_0.95','cvar_0.95','var_0.99','cvar_0.99'):
        assert abs(observed[metric]-expected[metric])<.0008
    assert abs(observed['loss_probability']-expected['loss_probability'])<.008


def test_seed_summary_does_not_count_timing_repeats():
    cell=configuration()['cells'][0]
    result=MonteCarloRunner(simulation_config(cell,3,'cpu')).run(make_inputs(cell))
    metrics=point_statistics(result)
    rows=[dict(cell=cell,device='cpu',seed=s,repeat=r,timing_kind='cold' if r==0 else 'steady',
        engine=result.metadata, metrics={k:v+s*1e-5 for k,v in metrics.items()}) for s in range(5) for r in range(3)]
    summary=seed_summary(rows)
    assert summary[0]['n']==5
    assert summary[0]['metrics']['var_0.95']['seed_sd']>0
    gpu=[dict(r,device='gpu',metrics={k:v+.001 for k,v in r['metrics'].items()}) for r in rows]
    comparisons=compare_studies(dict(device='cpu',workers=1,independent_seed_summary=summary),
        dict(device='gpu',workers=1,independent_seed_summary=seed_summary(gpu)))
    assert comparisons[0]['contrasts']['var_0.95']['gpu_minus_cpu']==pytest.approx(.001)
    assert comparisons[0]['equivalence_status']=='NO_BUSINESS_MARGIN_SPECIFIED'


def test_performance_comparison_separates_cold_and_steady_and_inputs():
    cell=configuration()['cells'][0]
    def study(device, seconds, cold):
        row=dict(cell=cell,seed=1,engine=dict(sample_hash='input'),runner_wall_seconds=seconds,
            launch_to_first_runner_completion_seconds=cold)
        return dict(workers=1,device=device,
            independent_seed_summary=[dict(cell=cell,input_sample_hash='input')],
            rows=[dict(row,timing_kind='cold'),dict(row,timing_kind='steady')])
    a,b=study('cpu',1.,2.),study('gpu',.1,3.)
    result=performance_comparison(a,b)[0]
    assert result['observed_speedup']==10
    assert result['measured_cold_choice']=='cpu'
    assert result['measured_steady_choice']=='gpu'
    b['rows'][0]['engine']['sample_hash']='other'
    with pytest.raises(ValueError,match='identical inputs'):
        performance_comparison(a,b)


def test_cu_cost_counts_every_execution_and_rejects_incomplete_window():
    study=dict(elapsed_seconds=20,rows=[dict(cell=dict(n_paths=100,n_steps=7))]*3)
    result=cu_cost(study,cu_before=10,cu_after=9.5,allocation_seconds=60,setup_seconds=30)
    assert result['paths_per_cu']==600
    assert result['path_steps_per_cu']==4200
    assert result['cu_per_hour']==30
    with pytest.raises(ValueError):
        cu_cost(study,cu_before=10,cu_after=10,allocation_seconds=60,setup_seconds=30)
    with pytest.raises(ValueError):
        cu_cost(study,cu_before=10,cu_after=9.5,allocation_seconds=40,setup_seconds=30)


def test_subprocess_cpu_study_and_raw_replay(tmp_path):
    pytest.importorskip('psutil')
    c=configuration()
    report=run_study(c,tmp_path/'study',first_seed_only=True,statistics=False)
    assert report['status']=='COMPLETED',report['failures']
    assert len(report['rows'])==2
    row=report['rows'][1]
    assert row['path_steps_per_second']==pytest.approx(c['cells'][0]['n_steps']*row['paths_per_second'])
    assert row['asset_path_steps_per_second']==pytest.approx(row['path_steps_per_second'])
    assert row['resources']['rss_peak_bytes']>0
    assert row['engine']['compute']['execution']['backend']=='numpy'
    replay=json.loads((tmp_path/'study/study.json').read_text())
    assert replay['rows'][0]['metrics']==report['rows'][0]['metrics']
    assert replay['independent_seed_summary'][0]['reliability_status']=='PILOT_ONLY'


def test_multiprocess_study_counts_independent_jobs(tmp_path):
    pytest.importorskip('psutil')
    c=configuration()
    report=run_study(c,tmp_path/'parallel',workers=2,statistics=False)
    assert report['status']=='COMPLETED',report['failures']
    assert len(report['rows'])==10
    assert report['independent_seed_summary'][0]['n']==5
    waves=[json.loads(p.read_text()) for p in (tmp_path/'parallel').glob('wave-*.json')]
    assert sum(w['completed_paths'] for w in waves)==10*c['cells'][0]['n_paths']
    assert all(w['child_inclusive_memory']['rss_peak_bytes']>0 for w in waves)


def test_memory_admission_includes_concurrent_workers():
    cell=configuration()['cells'][0]
    assert memory_plan(cell,2)['estimated_in_memory_bytes']==2*memory_plan(cell)['estimated_in_memory_bytes']


def test_cuda_fixed_input_when_available():
    cp=pytest.importorskip('cupy')
    if cp.cuda.runtime.getDeviceCount()==0:
        pytest.skip('CUDA device unavailable')
    c=MonteCarloConfig(n_paths=13,n_steps=7,batch_size=5)
    inputs=MonteCarloInput(weights=[.4,.6],drift=[.03,.06],volatility=[0.,0.])
    a=MonteCarloRunner(c).run(inputs)
    b=MonteCarloRunner(replace(c,compute=ComputeConfig(device='gpu'))).run(inputs,profile=True)
    np.testing.assert_allclose(a.terminal_values,b.terminal_values,rtol=1e-10,atol=1e-12)
    np.testing.assert_allclose(a.maximum_drawdowns,b.maximum_drawdowns,rtol=1e-10,atol=1e-12)
    assert b.metadata['compute']['execution']['backend']=='cupy-cuda'
