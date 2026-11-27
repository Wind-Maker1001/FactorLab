"""Exact disk-backed summaries without retaining full histories in RAM."""
from pathlib import Path

import numpy as np


CHUNK = 262144


def allocate_outputs(directory, n):
    """Create outputs exclusively; existing experiment files must not be replaced."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=False)
    return tuple(np.lib.format.open_memmap(directory / name, mode='w+',
        dtype=np.float64, shape=(n,)) for name in ('terminal.npy', 'drawdown.npy'))


def sorted_quantile(values, q):
    index = (len(values) - 1) * q
    lo = int(np.floor(index))
    hi = int(np.ceil(index))
    return float(values[lo] + (values[hi] - values[lo]) * (index - lo))


def disk_summary(terminal, drawdown, directory, *, initial, confidence, quantiles, threshold):
    """Preserve original samples; heapsort has constant auxiliary array workspace.

    OS page cache is reclaimable, not a hard RSS bound. Sorting can be I/O bound.
    Temporary sorted arrays remain artifacts to support exact rank intervals.
    """
    directory = Path(directory)
    n = len(terminal)
    arrays = [np.lib.format.open_memmap(directory / name, mode='w+',
        dtype=np.float64, shape=(n,)) for name in
        ('sorted-terminal.npy', 'sorted-drawdown.npy', 'sorted-loss.npy')]
    st, sd, sl = arrays
    loss_events = breach_events = 0
    for begin in range(0, n, CHUNK):
        end = min(n, begin + CHUNK)
        t, d = terminal[begin:end], drawdown[begin:end]
        if not np.isfinite(t).all() or not np.isfinite(d).all():
            raise FloatingPointError('simulation produced nonfinite wealth')
        st[begin:end], sd[begin:end] = t, d
        sl[begin:end] = 1 - t / initial
        loss_events += int(np.count_nonzero(t < initial))
        breach_events += int(np.count_nonzero(d > threshold))
    for a in arrays:
        a.sort(kind='heapsort')
        a.flush()
    mass = (1 - confidence) * n
    whole = int(np.floor(mass))
    fraction = mass - whole
    tail_sum = 0.
    for begin in range(n - whole, n, CHUNK):
        tail_sum += float(np.sum(sl[begin:min(n, begin + CHUNK)]))
    if fraction:
        tail_sum += fraction * sl[n - whole - 1]
    terminal.flush()
    drawdown.flush()
    return dict(var=sorted_quantile(sl, confidence), cvar=float(tail_sum / mass),
        terminal_quantiles={q: sorted_quantile(st, q) for q in quantiles},
        drawdown_quantiles={q: sorted_quantile(sd, q) for q in quantiles},
        loss_probability=loss_events/n, drawdown_breach_probability=breach_events/n)
