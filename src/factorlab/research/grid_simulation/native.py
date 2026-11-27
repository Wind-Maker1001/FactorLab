"""Compile the same path state machine for native CPU measurement."""
import ctypes
from functools import lru_cache
from hashlib import sha256
from importlib.metadata import version
from pathlib import Path
import platform
import subprocess
import sys
import tempfile
from time import perf_counter

import numpy as np


@lru_cache(maxsize=1)
def _library():
    source = Path(__file__).with_name('kernel.cu')
    compiler_version = version('ziglang')
    key = sha256(source.read_bytes() + (platform.system() + platform.machine()
                                       + compiler_version).encode()).hexdigest()[:24]
    root = Path(tempfile.gettempdir()) / 'factorlab-grid-native' / key
    root.mkdir(parents=True, exist_ok=True)
    library = root / ('grid.dll' if sys.platform == 'win32' else 'grid.so')
    if not library.exists():
        with tempfile.TemporaryDirectory(dir=root) as temporary:
            target = Path(temporary) / library.name
            subprocess.run([sys.executable, '-m', 'ziglang', 'c++', '-x', 'c++', str(source),
                '-DGRID_CPU', '-std=c++17', '-O3', '-ffp-contract=off', '-fno-fast-math',
                '-shared', '-o', str(target), *([] if sys.platform == 'win32' else ['-fPIC'])], check=True)
            target.replace(library)
    loaded = ctypes.CDLL(str(library))
    function = loaded.grid_cpu
    doubles = np.ctypeslib.ndpointer(dtype=np.float64, flags='C_CONTIGUOUS')
    integers = np.ctypeslib.ndpointer(dtype=np.int64, flags='C_CONTIGUOUS')
    errors = np.ctypeslib.ndpointer(dtype=np.int32, flags='C_CONTIGUOUS')
    function.argtypes = [integers, ctypes.c_int, ctypes.c_int, doubles, integers,
                         doubles, integers, ctypes.c_int, doubles, doubles, errors,
                         doubles, integers, integers, doubles, doubles, integers,
                         doubles, doubles, ctypes.c_int, ctypes.c_int, ctypes.c_int, doubles]
    function.restype = None
    return loaded


def run(quotes, targets, limits, params, integers, trace, market, workspace_width):
    from . import COLUMNS
    function = _library().grid_cpu
    output = np.empty((len(quotes), quotes.shape[1] if trace else 1, len(COLUMNS)))
    fills = np.zeros((len(quotes), quotes.shape[1], 128, 8) if trace else (1,))
    audit = np.zeros((len(quotes), quotes.shape[1], 512, 11) if trace else (1,))
    errors = np.zeros(len(quotes), dtype=np.int32)
    bars, times, event_times, rates, marks, coefficients, warmup = market
    workspace = np.empty((len(quotes), workspace_width), dtype=np.float64)
    started = perf_counter()
    function(quotes, len(quotes), quotes.shape[1], targets, limits,
             params, integers, trace, output, fills, errors, bars, times,
             event_times, rates, marks, coefficients, warmup, workspace,
             len(event_times), warmup.shape[1], workspace_width, audit)
    return output, fills, errors, perf_counter() - started, audit
