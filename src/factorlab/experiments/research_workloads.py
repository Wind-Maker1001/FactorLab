"""Normalize registered workload dimensions before resource admission."""
from importlib.resources import files
import json

from factorlab.experiments.research_tasks import input_path


def benchmark_config(name):
    return json.loads(files('factorlab').joinpath('resources/' + name + '.json').read_text(encoding='utf-8'))


def benchmark_memory(cell, workers=1, device='cpu', *, free_gpu_bytes=None, gpu_limit_mb=None):
    """Shared per-cell admission for sequential benchmark seed waves."""
    mib = 1024**2
    assets, steps = cell['assets'], cell['n_steps']
    per_path = 8 * ((8 * assets + 16) * steps + 128) + 4096
    if cell['model'] == 'gbm':
        persistent = 8 * (4 * assets + 2 * assets**2) + 6 * 512 + 65536
    else:
        persistent = 8 * (2 * assets + 4096 * assets + 4077 + 4096 + 4096 + 1) + 7 * 512 + 65536
    batch = min(cell['batch_size'] or 4096, cell['n_paths'])
    host_batch = batch
    if device == 'gpu':
        pool = min(cell['gpu_memory_limit_mb'], gpu_limit_mb or cell['gpu_memory_limit_mb']) * mib
        host_batch = min(batch, (pool - persistent) // per_path)
        if free_gpu_bytes is not None:
            pool = min(pool, int(free_gpu_bytes * .8))
        batch = min(batch, (pool - persistent) // per_path)
        if batch < 1:
            raise RuntimeError('GPU budget cannot fit one benchmark path')
    working = host_batch * (per_path if device == 'cpu' else 32) + persistent
    return dict(estimated_in_memory_bytes=workers*(working + cell['n_paths']*128 + 256*mib),
        estimated_disk_working_bytes=workers*(working + 256*mib),
        disk_bytes_per_job=cell['n_paths']*40,
        estimated_gpu_bytes=256*mib + persistent + batch*per_path,
        batch_size=batch, persistent_bytes=persistent)


def admission_task(task, input_root):
    params = task['parameters']
    if task['entry'] == 'static_grid':
        definition = json.loads(input_path(input_root, task['inputs']['definition']).read_text(encoding='utf-8'))
        normalized = {'paths': 100000, 'batch_size': 4096, 'seed': 1729}
        normalized.update(params)
        normalized.update(steps=definition['experiment']['steps'], assets=1)
    elif task['entry'] == 'benchmark':
        config = benchmark_config(params['benchmark'])
        normalized = dict(cells=config['cells'], statistics=params.get('statistics', True),
            requested_workers=params.get('workers', 1),
            workers=min(params.get('workers', 1),
                1 if params.get('first_seed_only', False) else len(config['seeds'])))
    else:
        normalized = {'paths': 100000, 'steps': 252, 'assets': 1, 'batch_size': 1024, 'seed': 1729}
        normalized.update(params)
        if task['entry'] == 'bootstrap':
            normalized['history_rows'] = 4096
            if 'returns' in task['inputs']:
                from zipfile import ZipFile
                import numpy as np
                with ZipFile(input_path(input_root, task['inputs']['returns'])) as archive:
                    with archive.open('returns.npy') as stream:
                        version = np.lib.format.read_magic(stream)
                        if version != (1, 0):
                            raise ValueError('Historical returns must use NPY v1 arrays')
                        shape, _, dtype = np.lib.format.read_array_header_1_0(stream)
                        if len(shape) != 2 or shape[1] != normalized['assets'] or dtype.hasobject:
                            raise ValueError('Historical return dimensions/type differ from the task')
                        normalized['history_rows'] = shape[0]
    return dict(task, parameters=normalized)
