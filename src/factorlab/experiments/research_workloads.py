"""Normalize registered workload dimensions before resource admission."""
from importlib.resources import files
import json

from factorlab.experiments.research_tasks import input_path


def benchmark_config(name):
    return json.loads(files('factorlab').joinpath('resources/' + name + '.json').read_text(encoding='utf-8'))


def admission_task(task, input_root):
    params = task['parameters']
    if task['entry'] == 'static_grid':
        definition = json.loads(input_path(input_root, task['inputs']['definition']).read_text(encoding='utf-8'))
        normalized = {'paths': 100000, 'batch_size': 4096, 'seed': 1729}
        normalized.update(params)
        normalized.update(steps=definition['experiment']['steps'], assets=1)
    elif task['entry'] == 'benchmark':
        cells = benchmark_config(params['benchmark'])['cells']
        normalized = dict(paths=max(c['n_paths'] for c in cells), steps=max(c['n_steps'] for c in cells),
            assets=max(c['assets'] for c in cells), batch_size=max(c['batch_size'] or 4096 for c in cells),
            workers=params.get('workers', 1))
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
