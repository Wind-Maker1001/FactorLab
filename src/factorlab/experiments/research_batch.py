"""Sequential, isolated registered research tasks, locally or in one Colab runtime."""
import argparse
import importlib.metadata as metadata
from importlib.resources import files
import json
import os
from pathlib import Path
import subprocess
import sys
from time import monotonic

from factorlab.experiments.research_tasks import load_manifest, relative_path, input_path
from factorlab.experiments.research_process import run_process

BUNDLE_RESERVE_SECONDS = 30


def save(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, indent=2, allow_nan=False) + '\n', encoding='utf-8')
    temporary.replace(path)


def installed_commit(commit):
    provenance = json.loads(files('factorlab').joinpath('resources/research-build.json').read_text(encoding='utf-8'))
    if provenance['commit'] != commit:
        raise ValueError('Installed wheel commit differs from submitted research commit')
    return provenance


def environment(device):
    import numpy as np
    import scipy
    from packaging.requirements import Requirement
    from packaging.specifiers import SpecifierSet
    import platform
    loaded = {'numpy': np.__version__, 'scipy': scipy.__version__}
    if device == 'gpu':
        import cupy as cp
        loaded['cupy-cuda13x'] = cp.__version__
    if platform.python_version() not in SpecifierSet(metadata.metadata('factorlab')['Requires-Python']):
        raise RuntimeError('Interpreter violates installed package requirements')
    extras = ('monte-carlo-benchmark', 'monte-carlo-native') + (('monte-carlo-cuda',) if device == 'gpu' else ())
    for text in metadata.requires('factorlab'):
        requirement = Requirement(text)
        if requirement.marker is None or any(requirement.marker.evaluate({'extra': extra}) for extra in extras):
            actual = loaded.get(requirement.name, metadata.version(requirement.name))
            if actual not in requirement.specifier:
                raise RuntimeError('Unsatisfied installed requirement: ' + text)
    for name, actual in loaded.items():
        if metadata.version(name) != actual:
            raise RuntimeError('Loaded dependency differs from installed distribution: ' + name)
    import factorlab
    return dict(pid=os.getpid(), executable=sys.executable, python=sys.version,
                factorlab=str(files('factorlab')), loaded_versions=loaded,
                dependency_constraints_validated=True)


def execute_task(task, device, output, input_root, commit):
    import numpy as np
    from factorlab.compute import ComputeConfig
    from factorlab.experiments.mc_colab_benchmark import make_inputs, point_statistics
    from factorlab.research.monte_carlo import MonteCarloConfig, MonteCarloInput, MonteCarloRunner
    params = task['parameters']
    started = monotonic()
    if task['entry'] in ('gbm', 'bootstrap'):
        cell = dict(model='gbm' if task['entry'] == 'gbm' else 'moving_block_bootstrap',
                    assets=params.get('assets', 1))
        inputs = make_inputs(cell)
        if 'returns' in task['inputs']:
            with np.load(input_path(input_root, task['inputs']['returns']), allow_pickle=False) as data:
                history = data['returns']
                dates = data['timestamps']
                inputs = MonteCarloInput(weights=np.full(cell['assets'], 1/cell['assets']),
                    historical_returns=history, timestamps=dates, available_at=dates, as_of=str(dates[-1]))
        config = MonteCarloConfig(model=cell['model'], n_paths=params.get('paths', 100000),
            n_steps=params.get('steps', 252), batch_size=params.get('batch_size', 1024),
            seed=params.get('seed', 1729), compute=ComputeConfig(device=device,
                gpu_memory_limit_mb=task['gpu_memory_limit_mb'] if device == 'gpu' else None))
        result = MonteCarloRunner(config).run(inputs)
        np.savez_compressed(output/'samples.npz', terminal_values=result.terminal_values,
                            maximum_drawdowns=result.maximum_drawdowns)
        report = dict(entry=task['entry'], metrics=point_statistics(result), engine=result.metadata)
    elif task['entry'] == 'static_grid':
        from factorlab.experiments.grid_batch import batches, configuration
        from factorlab.research.grid_simulation import BatchGridResult, COLUMNS, simulate
        definition, config = configuration(input_path(input_root, task['inputs']['definition']))
        size, batch_size = params.get('paths', 100000), params.get('batch_size', 4096)
        terminal = np.lib.format.open_memmap(output/'terminal.npy', mode='w+', dtype=np.float64,
                                             shape=(size, len(COLUMNS)))
        parts = []
        for begin, prices in batches(definition, size, batch_size, params.get('seed', 1729)):
            result = simulate(prices, config, backend='cuda' if device == 'gpu' else 'cpu', batch_size=batch_size)
            terminal[begin:begin+len(prices)] = result.terminal
            terminal.flush()
            parts.append(result.metadata)
        report = dict(entry='static_grid', paths=size, definition=definition,
                      risk=BatchGridResult(terminal, None, {}).risk(config.capital), batches=parts)
    else:
        from factorlab.experiments.mc_colab_benchmark import run_study
        config = json.loads(files('factorlab').joinpath(
            'resources/' + params['benchmark'] + '.json').read_text(encoding='utf-8'))
        config['host_memory_budget_mb'] = task['memory_budget_mb']
        config['budget_seconds'] = task['timeout_seconds']
        if task['gpu_memory_limit_mb'] is not None:
            for cell in config['cells']:
                cell['gpu_memory_limit_mb'] = min(cell['gpu_memory_limit_mb'], task['gpu_memory_limit_mb'])
        report = run_study(config, output/'study', device=device,
            workers=params.get('workers', 1), statistics=params.get('statistics', True),
            first_seed_only=params.get('first_seed_only', False), source_commit=commit)
        if report['status'] != 'COMPLETED':
            save(output/'report.json', report)
            raise RuntimeError('Registered benchmark incomplete; retained study contains outcomes')
    report['elapsed_seconds'] = monotonic()-started
    save(output/'report.json', report)


def worker(spec_path):
    spec = json.loads(Path(spec_path).read_text(encoding='utf-8'))
    output = Path(spec['output'])
    installed_commit(spec['commit'])
    save(output/'environment.json', environment(spec['compute_device']))
    execute_task(spec['task'], spec['compute_device'], output, Path(spec['input_root']), spec['commit'])
    for declared in spec['task']['outputs']:
        if not (output / declared).is_file():
            raise RuntimeError('Declared output was not produced: ' + declared)


def bundle_results(output, report):
    import numpy as np
    from zipfile import ZipFile, ZIP_DEFLATED
    manifest = {}
    with ZipFile(output/'samples.npz', 'x', compression=ZIP_DEFLATED, allowZip64=True) as archive:
        for path in sorted(output.rglob('*')):
            if path.is_symlink():
                raise ValueError('Research artifacts cannot be symbolic links')
            if not path.is_file() or path in (output/'report.json', output/'samples.npz'):
                continue
            key = 'artifact_' + str(len(manifest))
            manifest[key] = path.relative_to(output).as_posix()
            size = path.stat().st_size
            array = np.memmap(path, dtype=np.uint8, mode='r') if size else np.empty(0, dtype=np.uint8)
            with archive.open(key+'.npy', 'w', force_zip64=True) as stream:
                np.lib.format.write_array(stream, array, allow_pickle=False)
            del array
    save(output/'report.json', dict(report, artifact_manifest=manifest, artifact_encoding='uint8-npz-v1'))


def unpack_results(source, destination, *, max_bytes=8*1024**3, max_files=10000):
    import numpy as np
    source, destination = Path(source), Path(destination)
    report = json.loads((source/'report.json').read_text(encoding='utf-8'))
    names = list(report['artifact_manifest'].values())
    if len(names) != len(set(names)):
        raise ValueError('Duplicate artifact destinations')
    for name in names:
        relative_path(name)
    from zipfile import ZipFile
    import shutil
    if len(names) > max_files:
        raise ValueError('Artifact file-count limit exceeded')
    destination.mkdir(parents=True, exist_ok=False)
    with ZipFile(source/'samples.npz') as archive:
        entries = archive.infolist()
        if (len(entries) != len(names) or {e.filename for e in entries} !=
                {key+'.npy' for key in report['artifact_manifest']}):
            raise ValueError('Artifact index differs from bundle')
        if sum(e.file_size for e in entries) > max_bytes:
            raise ValueError('Artifact byte limit exceeded')
        for key, name in report['artifact_manifest'].items():
            with archive.open(key+'.npy') as data:
                if np.lib.format.read_magic(data) != (1, 0):
                    raise ValueError('Unsupported artifact array version')
                shape, fortran, dtype = np.lib.format.read_array_header_1_0(data)
                if dtype != np.uint8 or len(shape) != 1 or fortran:
                    raise ValueError('Artifact bytes must be a uint8 vector')
                if archive.getinfo(key+'.npy').file_size != data.tell()+shape[0]:
                    raise ValueError('Artifact length differs from array header')
                path = destination/name
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open('xb') as stream:
                    shutil.copyfileobj(data, stream, length=1024**2)
    return report


def run_batch(manifest_path, output, *, remote=False, timeout=None, cu_rate_per_hour=0., cu_balance=0.):
    from factorlab.experiments.research_selection import probe_resources, select_task
    manifest_path = Path(manifest_path).resolve()
    manifest = load_manifest(manifest_path)
    provenance = installed_commit(manifest['commit'])
    if remote and (cu_rate_per_hour <= 0 or cu_balance <= 0 or manifest['budget_cu'] <= 0):
        raise ValueError('Remote batches require observed rate, balance and approved budget')
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    save(output/'manifest.json', manifest)
    started = monotonic()
    limit = min(manifest['timeout_seconds'], timeout if timeout is not None else manifest['timeout_seconds'])
    if remote:
        limit = min(limit, min(cu_balance, manifest['budget_cu'])*3600/cu_rate_per_hour)
    bundle_reserve = min(BUNDLE_RESERVE_SECONDS, limit/4)
    report = dict(schema_version=1, commit=manifest['commit'], provenance=provenance,
                  remote=remote, outcomes=[], state='RUNNING')
    try:
        for task in manifest['tasks']:
            elapsed = monotonic()-started
            remaining = limit-elapsed-bundle_reserve
            outcome = dict(id=task['id'], state='NOT_STARTED')
            if remaining <= 0:
                outcome['reason'] = 'BATCH_TIME_OR_CU_BUDGET'
                report['outcomes'].append(outcome)
                save(output/'state.json', report)
                continue
            directory = output/task['id']
            directory.mkdir()
            work = directory/'work'
            work.mkdir()
            try:
                from factorlab.experiments.research_workloads import admission_task
                admitted = admission_task(task, manifest_path.parent)
                if remote:
                    if task['device'] not in ('colab', 'auto'):
                        raise ValueError('A remote batch cannot silently relocate an explicit local task')
                    resources = probe_resources(include_gpu=True)
                    local_task = dict(admitted, device='local_gpu')
                    selection = select_task(local_task, resources)
                    selection = dict(selection, device='colab')
                else:
                    resources = probe_resources(include_gpu=task['device'] != 'cpu')
                    selection = select_task(admitted, resources)
                    if selection['device'] == 'colab':
                        raise ValueError('Cloud placement requires colab_job submit --manifest')
                outcome['selection'] = selection
                remaining = limit-(monotonic()-started)-bundle_reserve
                if remaining <= 0:
                    outcome.update(reason='BATCH_TIME_OR_CU_BUDGET')
                    report['outcomes'].append(outcome)
                    save(directory/'status.json', outcome)
                    save(output/'state.json', report)
                    continue
                task = dict(task, timeout_seconds=min(task['timeout_seconds'], remaining))
                spec = dict(task=task, compute_device=selection['compute_device'],
                    input_root=str(manifest_path.parent), output=str(directory), commit=manifest['commit'])
                save(directory/'worker.json', spec)
                task_started = monotonic()
                from contextlib import nullcontext
                from factorlab.experiments.research_process import gpu_window
                gpu_index = selection.get('gpu_index')
                env = dict(os.environ)
                for name in ('OMP_NUM_THREADS', 'OPENBLAS_NUM_THREADS', 'MKL_NUM_THREADS', 'NUMEXPR_NUM_THREADS'):
                    env[name] = '1'
                if gpu_index is not None:
                    env['CUDA_VISIBLE_DEVICES'] = str(gpu_index)
                window = gpu_window(gpu_index) if gpu_index is not None else nullcontext()
                try:
                    with window, (directory/'worker.log').open('x', encoding='utf-8') as log:
                        code = run_process([sys.executable, '-I', '-m', 'factorlab.experiments.research_batch',
                            'worker', '--spec', str(directory/'worker.json')],
                            timeout=task['timeout_seconds'], cwd=work, stdout=log, stderr=subprocess.STDOUT, env=env)
                    outcome.update(state='SUCCEEDED' if code == 0 else 'FAILED', exit_code=code)
                except subprocess.TimeoutExpired:
                    outcome.update(state='TIMED_OUT')
                outcome['elapsed_seconds'] = monotonic()-task_started
            except Exception as exc:
                outcome.update(state='FAILED', error=f'{type(exc).__name__}: {exc}')
            report['outcomes'].append(outcome)
            save(directory/'status.json', outcome)
            save(output/'state.json', report)
    finally:
        states = [o['state'] for o in report['outcomes']]
        report['state'] = ('SUCCEEDED' if len(states) == len(manifest['tasks']) and all(s == 'SUCCEEDED' for s in states)
                           else 'PARTIAL' if 'SUCCEEDED' in states else 'FAILED')
        report['elapsed_seconds'] = monotonic()-started
        report['estimated_execution_cu'] = report['elapsed_seconds']*cu_rate_per_hour/3600
        save(output/'state.json', report)
        with (output/'bundle.log').open('x', encoding='utf-8') as log:
            code = run_process([sys.executable, '-I', '-m', 'factorlab.experiments.research_batch',
                'bundle', '--source', str(output)], timeout=max(.001, limit-(monotonic()-started)),
                stdout=log, stderr=subprocess.STDOUT)
        if code:
            raise RuntimeError('Artifact bundling failed; original task directories are retained')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('dispatch', 'local', 'remote', 'worker', 'bundle', 'unpack', 'validate', 'plan'))
    parser.add_argument('--manifest', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--spec', type=Path)
    parser.add_argument('--source', type=Path)
    parser.add_argument('--commit')
    parser.add_argument('--timeout', type=float)
    parser.add_argument('--cu-rate-per-hour', type=float, default=0.)
    parser.add_argument('--cu-balance', type=float, default=0.)
    parser.add_argument('--wheel', type=Path)
    parser.add_argument('--cloud-state', type=Path, help='Observed cloud capacity/rate/authorization JSON')
    parser.add_argument('--wsl-distro')
    parser.add_argument('--controller-python')
    parser.add_argument('--colab', default='colab')
    parser.add_argument('--gpu', default='L4', choices=('T4', 'L4', 'G4', 'H100', 'A100'))
    args = parser.parse_args()
    cloud = json.loads(args.cloud_state.read_text(encoding='utf-8')) if args.cloud_state else None
    if args.action == 'dispatch':
        from factorlab.experiments.research_dispatch import dispatch
        report = dispatch(args.manifest, args.output, wheel=args.wheel, cloud=cloud,
            wsl_distro=args.wsl_distro, controller_python=args.controller_python,
            colab_executable=args.colab, gpu=args.gpu)
        print(json.dumps(dict(state=report['state'], elapsed_seconds=report['elapsed_seconds'])))
        if report['state'] != 'SUCCEEDED':
            raise SystemExit(1)
    elif args.action == 'worker':
        worker(args.spec)
    elif args.action == 'bundle':
        bundle_results(args.source, json.loads((args.source/'state.json').read_text(encoding='utf-8')))
    elif args.action == 'unpack':
        unpack_results(args.source, args.output)
    elif args.action == 'validate':
        print(json.dumps(load_manifest(args.manifest), indent=2))
    elif args.action == 'plan':
        from factorlab.experiments.research_selection import probe_resources, select_task
        manifest = load_manifest(args.manifest)
        from factorlab.experiments.research_workloads import admission_task
        print(json.dumps([dict(id=t['id'], selection=select_task(
            admission_task(t, args.manifest.resolve().parent), probe_resources(t['device'] != 'cpu'), cloud))
            for t in manifest['tasks']], indent=2))
    else:
        manifest = load_manifest(args.manifest)
        if args.commit is not None and args.commit != manifest['commit']:
            parser.error('Controller and manifest commit differ')
        report = run_batch(args.manifest, args.output, remote=args.action == 'remote',
            timeout=args.timeout, cu_rate_per_hour=args.cu_rate_per_hour, cu_balance=args.cu_balance)
        print(json.dumps(dict(state=report['state'], elapsed_seconds=report['elapsed_seconds'])))
        if report['state'] != 'SUCCEEDED':
            raise SystemExit(1)


if __name__ == '__main__':
    main()
