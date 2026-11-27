"""Colab-only concentrated MC-COLAB-02 study, invoked by the existing controller."""
import argparse
import importlib.metadata as metadata
import json
import os
from pathlib import Path
import subprocess
import sys
from time import monotonic


def command(*args, cwd=None, timeout=None):
    subprocess.run(list(args), cwd=cwd, check=True, timeout=timeout)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--commit', required=True)
    parser.add_argument('--output', default='/content/factorlab-result')
    # Existing controller passes these; workload definitions remain registered JSON.
    parser.add_argument('--paths', type=int, default=1000000)
    parser.add_argument('--steps', type=int, default=252)
    parser.add_argument('--seed', type=int, default=0)
    args = parser.parse_args()
    if not Path('/content').is_dir():
        raise RuntimeError('Run this study in Colab, not on the control host')
    output = Path(args.output)
    output.mkdir(exist_ok=False)
    started = monotonic()
    source = Path('/content/mc-colab-02-source')
    command('git', 'clone', '--no-checkout', 'https://github.com/Wind-Maker1001/FactorLab.git', str(source))
    command('git', 'checkout', '--detach', args.commit, cwd=source)
    command(sys.executable, '-m', 'pip', 'install', '-e', str(source)+'[monte-carlo-cuda,monte-carlo-benchmark]')
    # A new interpreter imports only after installation and verifies loaded versions.
    try:
        command(sys.executable, str(source/'tools/research/colab_mc_colab_02.py'),
            '--execute', str(output), '--source', str(source), '--source-commit', args.commit)
    finally:
        (output/'setup-and-study.json').write_text(json.dumps(dict(
            commit=args.commit, deployment_and_study_seconds=monotonic()-started,
            cost_scope='controller allocation lifecycle includes initial wheel installation and release'), indent=2))
        # TODO: Deploy the controller failure-download extension from ac2b91c before another cloud run.
        bundle_results(output, args.commit)


def bundle_results(output, commit):
    """Encode artifacts as actual NPZ uint8 arrays for the existing two-file download."""
    import numpy as np
    files = sorted(path for path in output.rglob('*') if path.is_file())
    manifest = {f'artifact_{i}': path.relative_to(output).as_posix()
        for i, path in enumerate(files)}
    arrays = {key: np.frombuffer((output / relative).read_bytes(), dtype=np.uint8)
        for key, relative in manifest.items()}
    np.savez_compressed(output / 'samples.npz', **arrays)
    report = dict(schema='MC_COLAB_02_ARTIFACT_NPZ_V1', commit=commit,
        archive='samples.npz', encoding='uint8 arrays containing original file bytes',
        manifest=manifest,
        stages=json.loads((output / 'stages.json').read_text()) if (output/'stages.json').exists() else [],
        environment=json.loads((output / 'environment.json').read_text()) if (output/'environment.json').exists() else None)
    (output / 'report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')


def execute(output, source, commit):
    from packaging.requirements import Requirement
    import numpy as np
    import cupy as cp
    import psutil
    import scipy
    import pynvml
    from factorlab.experiments.mc_colab_benchmark import run_study, write_json
    loaded = {'numpy':np.__version__, 'cupy-cuda13x':cp.__version__,
        'scipy':scipy.__version__, 'psutil':psutil.__version__}
    for name, actual in loaded.items():
        if metadata.version(name) != actual:
            raise RuntimeError(f'Loaded {name} differs from installed metadata')
    for text in metadata.requires('factorlab'):
        requirement = Requirement(text)
        for extra in ('monte-carlo-cuda','monte-carlo-benchmark'):
            if requirement.marker is None or requirement.marker.evaluate({'extra':extra}):
                if metadata.version(requirement.name) not in requirement.specifier:
                    raise RuntimeError(f'Unsatisfied dependency: {text}')
    write_json(output/'environment.json', dict(commit=commit, executable=sys.executable,
        loaded_versions=loaded, pid=os.getpid(),
        installed=subprocess.check_output([sys.executable,'-m','pip','freeze'],text=True),
        gpu=subprocess.check_output(['nvidia-smi','--query-gpu=name,memory.total,driver_version',
            '--format=csv,noheader'],text=True),
        dependency_versions_verified=True))
    started=monotonic()
    # Prioritize formal five-seed GPU evidence, then matching cloud CPU and scale.
    stages=[('gpu-reliability','mc_colab_02_reliability.json','gpu',False,False),
        ('gpu-scale','mc_colab_02_scale.json','gpu',True,True),
        ('cpu-reliability','mc_colab_02_reliability.json','cpu',False,False)]
    outcomes=[]
    for name, filename, device, first, points in stages:
        remaining=900-(monotonic()-started)
        if remaining<60:
            outcomes.append(dict(stage=name,status='NOT_STARTED_TIME_RESERVE'))
            continue
        config=json.loads((source/'configs/research'/filename).read_text())
        config['budget_seconds']=remaining-30
        try:
            result=run_study(config,output/name,device=device,
                first_seed_only=first,statistics=not points)
        except Exception as error:
            outcomes.append(dict(stage=name,status='FAILED',error=f'{type(error).__name__}: {error}'))
            write_json(output/'stages.json',outcomes)
            raise
        outcomes.append(dict(stage=name,status=result['status'],rows=len(result['rows'])))
        if result['status']=='FAILED':
            write_json(output/'stages.json',outcomes)
            raise RuntimeError(f'Cloud stage failed: {name}; inspect retained artifacts')
    write_json(output/'stages.json',outcomes)


if __name__=='__main__':
    if '--execute' in sys.argv:
        parser=argparse.ArgumentParser()
        parser.add_argument('--execute',type=Path,required=True)
        parser.add_argument('--source',type=Path,required=True)
        parser.add_argument('--source-commit',required=True)
        args=parser.parse_args()
        execute(args.execute,args.source,args.source_commit)
    else:
        main()
