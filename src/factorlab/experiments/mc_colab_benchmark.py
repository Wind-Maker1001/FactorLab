"""Reproducible heterogeneous Monte Carlo measurements; no alternate simulator."""
from concurrent.futures import ThreadPoolExecutor
import json
import math
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
from threading import Event
from time import perf_counter

import numpy as np
from scipy.stats import norm, t

from factorlab.compute import ComputeConfig
from factorlab.research.monte_carlo import MonteCarloConfig, MonteCarloInput, MonteCarloRunner, _loss_tail
from factorlab.research.monte_carlo.reliability import summarize_paths


PROTOCOL = 'MC-COLAB-02-v1'
METRICS = ('var_0.95', 'cvar_0.95', 'var_0.99', 'cvar_0.99',
    'loss_probability', 'drawdown_breach_probability', 'drawdown_0.5',
    'drawdown_0.95', 'drawdown_0.99')


def write_json(path, value):
    with Path(path).open('x', encoding='utf-8') as stream:
        json.dump(value, stream, indent=2, allow_nan=False)
        stream.write('\n')


def validate_config(config):
    if config['protocol'] != PROTOCOL:
        raise ValueError('unsupported protocol')
    seeds = config['seeds']
    if len(seeds) < 5 or len(set(seeds)) != len(seeds):
        raise ValueError('at least five distinct seeds required')
    for key in ('timing_repeats', 'bootstrap_resamples', 'host_memory_budget_mb', 'blas_threads'):
        if type(config[key]) is not int or config[key] < 1:
            raise ValueError(key + ' must be a positive integer')
    if config['bootstrap_resamples'] < 2:
        raise ValueError('at least two bootstrap resamples required')
    if not math.isfinite(config['budget_seconds']) or config['budget_seconds'] <= 0:
        raise ValueError('positive finite budget_seconds required')
    if not config['cells'] or len({c['name'] for c in config['cells']}) != len(config['cells']):
        raise ValueError('nonempty cells with unique names required')
    for seed in (*seeds, config['input_seed']):
        MonteCarloConfig(seed=seed)
    for cell in config['cells']:
        if not cell['name'] or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_' for c in cell['name']):
            raise ValueError('cell name must be a filesystem-safe identifier')
        if type(cell['assets']) is not int or cell['assets'] < 1:
            raise ValueError('positive asset count required')
        simulation_config(cell, seeds[0], 'cpu')
        simulation_config(cell, seeds[0], 'gpu')
    return config


def simulation_config(cell, seed, device):
    return MonteCarloConfig(model=cell['model'], n_paths=cell['n_paths'],
        n_steps=cell['n_steps'], seed=seed, batch_size=cell['batch_size'],
        compute=ComputeConfig(device=device, allow_fallback=False,
            gpu_memory_limit_mb=cell['gpu_memory_limit_mb'] if device == 'gpu' else None))


def make_inputs(cell, input_seed=1729):
    n = cell['assets']
    correlation = np.full((n, n), .3)
    np.fill_diagonal(correlation, 1.)
    if cell['model'] == 'gbm':
        return MonteCarloInput(weights=np.full(n, 1/n), drift=np.full(n, .05),
            volatility=np.full(n, .2), correlation=correlation)
    history = np.random.RandomState(input_seed).standard_normal((4096, n))
    history = history @ np.linalg.cholesky(correlation).T
    history = np.expm1((.05-.5*.2**2)/252 + .2/np.sqrt(252)*history)
    timestamps = np.datetime64('2000-01-01') + np.arange(len(history)).astype('timedelta64[D]')
    return MonteCarloInput(weights=np.full(n, 1/n), historical_returns=history,
        timestamps=timestamps, available_at=timestamps, as_of=str(timestamps[-1]))


def analytic_gbm(n_steps, dt=1/252, drift=.05, volatility=.2):
    horizon = n_steps*dt
    m, s = (drift-volatility**2/2)*horizon, volatility*np.sqrt(horizon)
    return {**{f'{metric}_{c}': float(value) for c in (.95, .99)
        for metric, value in (
            ('var', 1-np.exp(m+s*norm.ppf(1-c))),
            ('cvar', 1-np.exp(m+s*s/2)*norm.cdf(norm.ppf(1-c)-s)/(1-c)))},
        'loss_probability': float(norm.cdf(-m/s))}


def point_statistics(result):
    losses = 1-result.terminal_values
    metrics = {}
    for c in (.95, .99):
        metrics[f'var_{c}'], metrics[f'cvar_{c}'] = _loss_tail(losses, c)
    metrics.update(loss_probability=result.loss_probability,
        drawdown_breach_probability=result.drawdown_breach_probability)
    metrics.update({f'drawdown_{q}': float(np.quantile(result.maximum_drawdowns, q))
        for q in (.5, .95, .99)})
    return metrics


class Resources:
    """Sample this worker's RSS/CPU and whole-device memory, not allocator estimates."""
    def __init__(self, device, interval=.05, include_children=False):
        import psutil
        self.psutil = psutil
        self.process = psutil.Process()
        self.include_children = include_children
        self.interval = interval
        self.stop = Event()
        self.samples = 0
        self.peak_rss = 0
        self.peak_cpu_cores = 0.
        self.device_peak = None
        self.nvml = None
        self.gpu_identity = None
        if device == 'gpu':
            import cupy as cp
            import pynvml
            pynvml.nvmlInit()
            self.nvml = pynvml
            # Bind NVML to the actual CUDA device, including CUDA_VISIBLE_DEVICES remapping.
            pci = cp.cuda.runtime.deviceGetPCIBusId(cp.cuda.Device().id)
            self.handle = pynvml.nvmlDeviceGetHandleByPciBusId(pci)
            self.gpu_identity = dict(name=pynvml.nvmlDeviceGetName(self.handle),
                uuid=pynvml.nvmlDeviceGetUUID(self.handle), pci_bus_id=pci,
                driver=pynvml.nvmlSystemGetDriverVersion(),
                total_memory_bytes=pynvml.nvmlDeviceGetMemoryInfo(self.handle).total)
        self.pool = ThreadPoolExecutor(max_workers=1)

    def _sample(self):
        times = self.process.cpu_times()
        cpu = times.user+times.system
        now = perf_counter()
        if self.samples:
            self.peak_cpu_cores = max(self.peak_cpu_cores, (cpu-self.previous_cpu)/(now-self.previous_time))
        self.previous_cpu, self.previous_time = cpu, now
        rss = self.process.memory_info().rss
        if self.include_children:
            for child in self.process.children(recursive=True):
                try:
                    rss += child.memory_info().rss
                except self.psutil.NoSuchProcess:
                    pass  # A worker may exit between enumeration and sampling.
        self.peak_rss = max(self.peak_rss, rss)
        if self.nvml:
            used = self.nvml.nvmlDeviceGetMemoryInfo(self.handle).used
            self.device_peak = max(self.device_peak or 0, used)
        self.samples += 1

    def _run(self):
        while not self.stop.wait(self.interval):
            self._sample()

    def __enter__(self):
        self.started = perf_counter()
        self._sample()
        self.initial_cpu = self.previous_cpu
        self.future = self.pool.submit(self._run)
        return self

    def __exit__(self, *exc):
        self.stop.set()
        self.future.result()
        self.pool.shutdown()
        self._sample()
        self.record = dict(sample_interval_seconds=self.interval, samples=self.samples,
            rss_peak_bytes=self.peak_rss, cpu_peak_core_equivalents=self.peak_cpu_cores,
            cpu_seconds=self.previous_cpu-self.initial_cpu,
            device_used_peak_bytes=self.device_peak,
            device_memory_scope='whole CUDA device, includes other processes',
            gpu=self.gpu_identity, peak_meaning='sampled lower bound on instantaneous peak')
        if self.nvml:
            self.nvml.nvmlShutdown()


def disk_point_statistics(result, directory):
    from factorlab.research.monte_carlo.storage import CHUNK, sorted_quantile
    losses = np.load(Path(directory)/'sorted-loss.npy', mmap_mode='r')
    dd = np.load(Path(directory)/'sorted-drawdown.npy', mmap_mode='r')
    metrics = {}
    for c in (.95, .99):
        mass = (1-c)*len(losses)
        whole = int(np.floor(mass))
        total = sum(float(np.sum(losses[b:min(len(losses), b+CHUNK)]))
            for b in range(len(losses)-whole, len(losses), CHUNK))
        fraction = mass-whole
        if fraction:
            total += fraction*losses[len(losses)-whole-1]
        metrics[f'var_{c}'], metrics[f'cvar_{c}'] = sorted_quantile(losses,c), float(total/mass)
    metrics.update(loss_probability=result.loss_probability,
        drawdown_breach_probability=result.drawdown_breach_probability)
    metrics.update({f'drawdown_{q}': sorted_quantile(dd,q) for q in (.5,.95,.99)})
    return metrics


def worker(job):
    with Resources(job['device']) as resources:
        rows = _worker(job)
    write_json(Path(job['output'])/'worker-resources.json', resources.record)
    return rows


def _worker(job):
    from threadpoolctl import threadpool_limits, threadpool_info
    cell, config = job['cell'], job['config']
    output = Path(job['output'])
    output.mkdir(parents=True, exist_ok=False)
    input_started = perf_counter()
    inputs = make_inputs(cell, config['input_seed'])
    input_seconds = perf_counter()-input_started
    rows = []
    with threadpool_limits(limits=config['blas_threads']):
        for repeat in range(config['timing_repeats']+1):
            c = simulation_config(cell, job['seed'], job['device'])
            sample_dir = output/f'samples-{repeat}' if job['disk'] else None
            with Resources(job['device']) as resources:
                started = perf_counter()
                result = MonteCarloRunner(c).run(inputs, output_directory=sample_dir, profile=job['profile'])
                seconds = perf_counter()-started
            first_completed = perf_counter()
            analysis_started = perf_counter()
            metrics = disk_point_statistics(result, sample_dir) if job['disk'] else point_statistics(result)
            intervals = None
            if repeat == 0 and job['statistics']:
                if job['disk']:
                    raise ValueError('path bootstrap requires explicitly admitted in-memory samples')
                intervals = summarize_paths(1-result.terminal_values, result.maximum_drawdowns,
                    resamples=config['bootstrap_resamples'], bootstrap_seed=job['seed'])
            analysis_seconds = perf_counter()-analysis_started
            row = dict(cell=cell, seed=job['seed'], device=job['device'], repeat=repeat,
                timing_kind='profile' if job['profile'] else ('cold' if repeat==0 else 'steady'),
                runner_wall_seconds=seconds, external_statistics_seconds=analysis_seconds,
                paths_per_second=cell['n_paths']/seconds,
                path_steps_per_second=cell['n_paths']*cell['n_steps']/seconds,
                asset_path_steps_per_second=cell['n_paths']*cell['n_steps']*cell['assets']/seconds,
                input_preparation_seconds=input_seconds if repeat==0 else 0.,
                launch_to_first_runner_completion_seconds=(first_completed-job['launch_started']) if repeat==0 else None,
                metrics=metrics, intervals=intervals, resources=resources.record,
                engine=result.metadata, threadpools=threadpool_info())
            if cell['model']=='gbm' and cell['assets']==1:
                row['analytic_reference'] = analytic_gbm(cell['n_steps'])
                row['analytic_error'] = {k:metrics[k]-v for k,v in row['analytic_reference'].items()}
            rows.append(row)
            write_json(output/f'run-{repeat}.json', row)
            del result
    write_json(output/'completed.json', dict(rows=rows))
    return rows


def memory_plan(cell, workers=1, device='cpu'):
    # Conservative admission estimate, not a measured RSS bound.
    per_path = 8*((8*cell['assets']+16)*cell['n_steps']+128)+4096
    working = min(cell['batch_size'] or 4096,cell['n_paths'])*(per_path if device=='cpu' else 32)
    samples_and_statistics = cell['n_paths']*128
    return dict(estimated_in_memory_bytes=workers*(working+samples_and_statistics+256*1024**2),
        estimated_disk_working_bytes=workers*(working+256*1024**2),
        disk_bytes_per_job=cell['n_paths']*40)


def launch_job(job):
    job = dict(job, launch_started=perf_counter())
    path = Path(job['output']).with_suffix('.job.json')
    write_json(path,job)
    env = dict(os.environ)
    for key in ('OMP_NUM_THREADS','OPENBLAS_NUM_THREADS','MKL_NUM_THREADS','NUMEXPR_NUM_THREADS'):
        env[key] = str(job['config']['blas_threads'])
    with Path(job['output']).with_suffix('.log').open('x',encoding='utf-8') as log:
        process = subprocess.run([sys.executable,'-m','factorlab.experiments.mc_colab_benchmark',
            '--worker',str(path)],env=env,stdout=log,stderr=subprocess.STDOUT)
    if process.returncode:
        rows=[json.loads(p.read_text(encoding='utf-8')) for p in sorted(Path(job['output']).glob('run-*.json'))]
        return dict(status='FAILED', job=str(path), exit_code=process.returncode, rows=rows)
    return dict(status='SUCCEEDED', job=str(path),
        resources=json.loads((Path(job['output'])/'worker-resources.json').read_text(encoding='utf-8')),
        rows=json.loads((Path(job['output'])/'completed.json').read_text(encoding='utf-8'))['rows'])


def host_identity():
    import psutil
    name=platform.processor()
    if sys.platform=='win32':
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,r'HARDWARE\DESCRIPTION\System\CentralProcessor\0') as key:
            name=winreg.QueryValueEx(key,'ProcessorNameString')[0].strip()
    return dict(cpu=name,logical_cpus=psutil.cpu_count(),physical_cores=psutil.cpu_count(logical=False),
        total_ram_bytes=psutil.virtual_memory().total)


def run_study(config, output, *, device='cpu', workers=1, statistics=True, profile=False, first_seed_only=False):
    validate_config(config)
    if device not in ('cpu','gpu') or type(workers) is not int or workers<1:
        raise ValueError('explicit cpu/gpu device and positive workers required')
    if device=='gpu' and workers!=1:
        raise ValueError('one worker per GPU study; CPU multiprocess is a separate mode')
    output = Path(output).resolve()
    output.mkdir(parents=True,exist_ok=False)
    write_json(output/'configuration.json',config)
    root=Path(__file__).resolve().parents[3]
    source=output/'source'
    source.mkdir()
    for relative in ('src/factorlab/experiments/mc_colab_benchmark.py',
        'src/factorlab/research/monte_carlo/__init__.py',
        'src/factorlab/research/monte_carlo/storage.py',
        'src/factorlab/research/monte_carlo/reliability.py'):
        destination=source/relative
        destination.parent.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(root/relative,destination)
    write_json(output/'provenance.json',dict(
        baseline_commit=subprocess.check_output(['git','-C',str(root),'rev-parse','HEAD'],text=True).strip(),
        working_tree_status=subprocess.check_output(['git','-C',str(root),'status','--porcelain'],text=True),
        source_snapshot='source/',command=sys.argv))
    started = perf_counter()
    outcomes = []
    skipped = []
    seeds = config['seeds'][:1] if first_seed_only else config['seeds']
    for cell in config['cells']:
        memory = memory_plan(cell,workers,device)
        budget = config['host_memory_budget_mb']*1024**2
        disk = memory['estimated_in_memory_bytes'] > budget
        if disk and (statistics or memory['estimated_disk_working_bytes'] > budget):
            skipped.append(dict(cell=cell, reason='HOST_MEMORY_ADMISSION',memory=memory))
            continue
        for begin in range(0,len(seeds),workers):
            if perf_counter()-started >= config['budget_seconds']:
                skipped.extend(dict(cell=cell,seed=s,reason='WALL_BUDGET') for s in seeds[begin:])
                break
            jobs=[dict(config=config,cell=cell,seed=seed,device=device,
                output=str(output/f'{cell["name"]}-{seed}'),disk=disk,
                statistics=statistics,profile=profile) for seed in seeds[begin:begin+workers]]
            wave_started=perf_counter()
            with Resources('cpu', include_children=True) as aggregate_resources:
                with ThreadPoolExecutor(max_workers=workers) as pool:
                    wave=list(pool.map(launch_job,jobs))
            wave_seconds=perf_counter()-wave_started
            outcomes.extend(wave)
            write_json(output/f'wave-{cell["name"]}-{begin}.json',dict(outcomes=wave,
                workers=workers,wall_seconds=wave_seconds,
                child_inclusive_memory=aggregate_resources.record,
                child_cpu_seconds=sum(r['resources']['cpu_seconds'] for o in wave for r in o.get('rows',[])),
                completed_paths=sum(len(o.get('rows',[]))*cell['n_paths'] for o in wave),
                scope='aggregate child jobs including startup, sampling and external statistics'))
            if any(o['status']=='FAILED' for o in wave):
                break
    elapsed=perf_counter()-started
    rows=[r for o in outcomes for r in o.get('rows',[])]
    report=dict(protocol=PROTOCOL,device=device,workers=workers,
        status='FAILED' if any(o['status']=='FAILED' for o in outcomes) else ('PARTIAL' if skipped else 'COMPLETED'),
        elapsed_seconds=elapsed,rows=rows,skipped=skipped,
        failures=[o for o in outcomes if o['status']=='FAILED'],
        environment=dict(python=sys.version,platform=platform.platform(),numpy=np.__version__,host=host_identity()),
        independent_seed_summary=seed_summary(rows))
    write_json(output/'study.json',report)
    return report


def seed_summary(rows):
    groups={}
    for row in rows:
        if row['repeat'] != 0 or row['timing_kind']=='profile':
            continue
        key=json.dumps([row['device'],row['cell']],sort_keys=True)
        samples=groups.setdefault(key,{})
        if row['seed'] in samples:
            raise ValueError('duplicate independent-seed observation')
        samples[row['seed']]=row
    summaries=[]
    for samples in groups.values():
        first=next(iter(samples.values()))
        n=len(samples)
        metrics={}
        for name in METRICS:
            values=np.array([r['metrics'][name] for r in samples.values()])
            sd=float(np.std(values,ddof=1)) if n>1 else None
            half=float(t.ppf(.975,n-1)*sd/np.sqrt(n)) if n>1 else None
            mean=float(values.mean())
            metrics[name]=dict(mean=mean,seed_sd=sd,minimum=float(values.min()),maximum=float(values.max()),
                mean_interval_95=[mean-half,mean+half] if half is not None else None)
        identities={r['engine']['sample_hash'] for r in samples.values()}
        if len(identities)!=1:
            raise ValueError('seed group contains different inputs')
        summaries.append(dict(device=first['device'],cell=first['cell'],seeds=list(samples),n=n,
            input_sample_hash=identities.pop(),
            reliability_status='FIVE_SEED_EVIDENCE' if n>=5 else 'PILOT_ONLY',metrics=metrics,
            interval_method='Student t across independent seed estimates; not path bootstrap'))
    return summaries


def compare_studies(cpu,gpu):
    if cpu['device']!='cpu' or gpu['device']!='gpu' or cpu['workers']!=1 or gpu['workers']!=1:
        raise ValueError('comparison requires single-process CPU and single-GPU studies')
    comparisons=[]
    for a in cpu['independent_seed_summary']:
        matches=[b for b in gpu['independent_seed_summary'] if b['cell']==a['cell']]
        if not matches:
            continue
        b=matches[0]
        if a['input_sample_hash']!=b['input_sample_hash']:
            raise ValueError('CPU/GPU input identity mismatch')
        if min(a['n'],b['n'])<5:
            continue
        contrasts={}
        for name in METRICS:
            x,y=a['metrics'][name],b['metrics'][name]
            vx,vy=x['seed_sd']**2/a['n'],y['seed_sd']**2/b['n']
            se=np.sqrt(vx+vy)
            df=(vx+vy)**2/(vx**2/(a['n']-1)+vy**2/(b['n']-1)) if se else None
            half=float(t.ppf(1-.05/(2*len(METRICS)),df)*se) if se else 0.
            difference=y['mean']-x['mean']
            contrasts[name]=dict(gpu_minus_cpu=difference,
                family_interval_95=[difference-half,difference+half])
        comparisons.append(dict(cell=a['cell'],contrasts=contrasts,
            method='unpaired Welch, Bonferroni across nine metrics within cell',
            equivalence_status='NO_BUSINESS_MARGIN_SPECIFIED'))
    return comparisons


def performance_comparison(cpu, gpu):
    """Matched measured cells only; no extrapolated device crossover."""
    output=[]
    for summary in cpu['independent_seed_summary']:
        cell=summary['cell']
        a=[r for r in cpu['rows'] if r['cell']==cell and r['timing_kind']=='steady']
        b=[r for r in gpu['rows'] if r['cell']==cell and r['timing_kind']=='steady']
        if not a or not b:
            continue
        if {r['engine']['sample_hash'] for r in a+b}!={summary['input_sample_hash']}:
            raise ValueError('performance comparison requires identical inputs')
        if {r['seed'] for r in a}!={r['seed'] for r in b}:
            raise ValueError('performance comparison requires matching seed design')
        cpu_seconds=float(np.median([r['runner_wall_seconds'] for r in a]))
        gpu_seconds=float(np.median([r['runner_wall_seconds'] for r in b]))
        cold_cpu=[r['launch_to_first_runner_completion_seconds'] for r in cpu['rows']
            if r['cell']==cell and r['timing_kind']=='cold']
        cold_gpu=[r['launch_to_first_runner_completion_seconds'] for r in gpu['rows']
            if r['cell']==cell and r['timing_kind']=='cold']
        cold_a=float(np.median(cold_cpu)) if cold_cpu else None
        cold_b=float(np.median(cold_gpu)) if cold_gpu else None
        output.append(dict(cell=cell,cpu_workers=cpu['workers'],gpu_workers=gpu['workers'],
            cold_cpu_median_seconds=cold_a,cold_gpu_median_seconds=cold_b,
            measured_cold_choice=('gpu' if cold_b<cold_a else 'cpu') if cold_a is not None and cold_b is not None else None,
            cpu_median_seconds=cpu_seconds,gpu_median_seconds=gpu_seconds,
            observed_speedup=cpu_seconds/gpu_seconds,
            measured_steady_choice='gpu' if gpu_seconds<cpu_seconds else 'cpu',
            scope='runner including exact summary; excludes external reliability analysis'))
    return output


def cu_cost(study, *, cu_before, cu_after, allocation_seconds, setup_seconds):
    values=(cu_before,cu_after,allocation_seconds,setup_seconds)
    if any(not math.isfinite(v) for v in values) or cu_after<0 or cu_before<=cu_after or allocation_seconds<=0 or setup_seconds<0:
        raise ValueError('positive observed CU consumption and allocation duration required')
    if allocation_seconds < study['elapsed_seconds']+setup_seconds:
        raise ValueError('CU allocation window must cover setup and study')
    cu=cu_before-cu_after
    paths=sum(r['cell']['n_paths'] for r in study['rows'])
    steps=sum(r['cell']['n_paths']*r['cell']['n_steps'] for r in study['rows'])
    return dict(source='observed_CU_balance_difference',cu=cu,cu_per_hour=cu*3600/allocation_seconds,
        paths_per_cu=paths/cu,path_steps_per_cu=steps/cu,completed_paths=paths,
        allocation_seconds=allocation_seconds,setup_seconds=setup_seconds,
        scope='entire observed allocation including setup; repetitions counted once per execution')


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser()
    parser.add_argument('--worker',required=True)
    args=parser.parse_args()
    worker(json.loads(Path(args.worker).read_text(encoding='utf-8')))
