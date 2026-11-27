"""Versioned, allowlisted research submissions; placement is not a numerical backend."""
import json
import math
from pathlib import Path, PurePosixPath
import re

ENTRIES = ('gbm', 'bootstrap', 'static_grid', 'benchmark')
DEVICES = ('cpu', 'local_gpu', 'colab', 'auto')
BENCHMARKS = ('mc_colab_02_reliability', 'mc_colab_02_scale')
PARAMETERS = {
    'gbm': {'paths', 'steps', 'assets', 'batch_size', 'seed'},
    'bootstrap': {'paths', 'steps', 'assets', 'batch_size', 'seed'},
    'static_grid': {'paths', 'batch_size', 'seed'},
    'benchmark': {'benchmark', 'first_seed_only', 'statistics', 'workers'},
}


def relative_path(value):
    if not isinstance(value, str) or not value or '\\' in value or ':' in value:
        raise ValueError('A nonempty portable relative path is required')
    path = PurePosixPath(value)
    if path.is_absolute() or any(p in ('', '.', '..') for p in value.split('/')):
        raise ValueError('Absolute paths and traversal are not allowed')
    return path


def input_path(root, value):
    path = relative_path(value)
    if path.suffix.lower() not in ('.json', '.csv', '.npy', '.npz', '.parquet'):
        raise ValueError('Only explicit research data formats may be uploaded')
    if re.search(r'credential|secret|token|password|private|service.account|^\.?env', path.name, re.I):
        raise ValueError('Credential-like inputs are not research data')
    root = Path(root).resolve()
    candidate = root
    for part in path.parts:
        candidate = candidate / part
        if candidate.is_symlink():
            raise ValueError('Symbolic links are not valid research inputs')
    if not candidate.resolve().is_relative_to(root) or not candidate.is_file():
        raise ValueError('Input must be an existing file inside the manifest directory')
    return candidate


def positive(value, name, integer=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValueError(name + ' must be positive and finite')
    if integer and type(value) is not int:
        raise ValueError(name + ' must be an integer')


def validate_manifest(manifest, root=None):
    allowed = {'schema_version', 'commit', 'timeout_seconds', 'budget_cu', 'tasks'}
    if set(manifest) != allowed or manifest['schema_version'] != 1:
        raise ValueError('Unsupported research manifest fields/schema')
    if not isinstance(manifest['commit'], str) or not re.fullmatch('[a-f0-9]{40}', manifest['commit']):
        raise ValueError('A full Git commit is required')
    positive(manifest['timeout_seconds'], 'batch timeout')
    budget = manifest['budget_cu']
    if isinstance(budget, bool) or not isinstance(budget, (int, float)) or not math.isfinite(budget) or budget < 0:
        raise ValueError('CU budget must be finite and nonnegative')
    if not isinstance(manifest['tasks'], list) or not manifest['tasks']:
        raise ValueError('At least one task is required')
    ids = set()
    for task in manifest['tasks']:
        required = {'id', 'entry', 'parameters', 'inputs', 'device', 'memory_budget_mb',
                    'gpu_memory_limit_mb', 'timeout_seconds', 'outputs'}
        if set(task) != required:
            raise ValueError('Unsupported or missing task fields')
        name = task['id']
        if not isinstance(name, str) or not re.fullmatch('[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}', name) or name.casefold() in ids:
            raise ValueError('Unique filesystem-safe task IDs are required')
        ids.add(name.casefold())
        if task['entry'] not in ENTRIES or task['device'] not in DEVICES:
            raise ValueError('Unregistered research entry or placement')
        if task['device'] == 'colab' and budget <= 0:
            raise ValueError('Colab requires a positive CU budget')
        positive(task['memory_budget_mb'], 'host memory budget', True)
        if task['gpu_memory_limit_mb'] is not None:
            positive(task['gpu_memory_limit_mb'], 'GPU memory budget', True)
        positive(task['timeout_seconds'], 'task timeout')
        params = task['parameters']
        if not isinstance(params, dict) or set(params) - PARAMETERS[task['entry']]:
            raise ValueError('Unregistered research parameters')
        for key in ('paths', 'steps', 'assets', 'batch_size', 'workers'):
            if key in params:
                positive(params[key], key, True)
        if 'seed' in params and (type(params['seed']) is not int or not 0 <= params['seed'] < 2**32):
            raise ValueError('Seed must be a uint32')
        if task['entry'] == 'benchmark':
            if params.get('benchmark') not in BENCHMARKS:
                raise ValueError('Unregistered benchmark configuration')
            for key in ('first_seed_only', 'statistics'):
                if key in params and type(params[key]) is not bool:
                    raise ValueError(key + ' must be boolean')
        inputs = task['inputs']
        if not isinstance(inputs, dict):
            raise ValueError('Inputs must be a named mapping')
        expected = {'definition'} if task['entry'] == 'static_grid' else ({'returns'} if task['entry'] == 'bootstrap' else set())
        if set(inputs) - expected or (task['entry'] == 'static_grid' and set(inputs) != expected):
            raise ValueError('Unexpected or missing registered input')
        for value in inputs.values():
            relative_path(value)
            if root is not None:
                input_path(root, value)
        outputs = task['outputs']
        if not isinstance(outputs, list) or not outputs or len(set(outputs)) != len(outputs):
            raise ValueError('Unique declared outputs are required')
        for value in outputs:
            relative_path(value)
    return manifest


def load_manifest(path):
    path = Path(path).resolve()
    return validate_manifest(json.loads(path.read_text(encoding='utf-8')), path.parent)
