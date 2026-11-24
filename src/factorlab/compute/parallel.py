"""Shared multi-process execution helpers for offline FactorLab research.

Prevents BLAS/OpenMP thread oversubscription across worker processes, computes
sensible map chunksizes to amortize Windows spawn IPC overhead, and provides
conservative worker-count recommendations based on workload size and CPU cores.
"""
from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor
from multiprocessing import get_context
import os

_BLAS_THREAD_VARS = (
    "OMP_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "MKL_NUM_THREADS",
    "VECLIB_MAXIMUM_THREADS",
    "NUMEXPR_NUM_THREADS",
)


def set_single_thread_blas_env() -> None:
    """Pin numerical libraries to one thread per process to prevent oversubscription."""
    for name in _BLAS_THREAD_VARS:
        os.environ[name] = "1"


def single_thread_blas_initializer() -> None:
    """Top-level ProcessPoolExecutor initializer for Windows spawn compatibility."""
    set_single_thread_blas_env()


def chunksize_for_tasks(n_tasks: int, max_workers: int, *, min_chunk: int = 1, max_chunk: int = 16) -> int:
    """Choose a balanced chunksize (~4 waves per worker) to amortize IPC without stragglers."""
    if n_tasks <= 0 or max_workers <= 1:
        return 1
    target = max(min_chunk, n_tasks // (max_workers * 4))
    return min(max_chunk, target)


def recommended_workers(
    n_tasks: int,
    *,
    io_heavy: bool = False,
    cpu_count: int | None = None,
) -> int:
    """Recommend worker count for heterogeneous P/E-core workstations and CI hosts.

    Small task batches should not pay full process-pool spawn overhead.
    CPU-bound research scales well up to physical core count (capped at 16 by
    default to avoid Windows spawn and hybrid E-core straggler drag). File-heavy
    synchronous SQLite workloads saturate disk flushes much earlier.
    """
    if n_tasks <= 2:
        return 1
    logical = cpu_count if cpu_count is not None else (os.cpu_count() or 2)
    cap = 8 if io_heavy else min(16, max(1, logical - 4))
    if n_tasks < 16:
        cap = min(cap, max(1, n_tasks // 2))
    return max(1, min(cap, n_tasks))


def create_process_pool(
    max_workers: int,
    *,
    single_thread_blas: bool = True,
    mp_start_method: str | None = "spawn",
) -> ProcessPoolExecutor:
    """Create a ProcessPoolExecutor with optional BLAS single-thread pinning."""
    kwargs: dict[str, object] = {"max_workers": max(1, int(max_workers))}
    if mp_start_method is not None:
        kwargs["mp_context"] = get_context(mp_start_method)
    if single_thread_blas:
        kwargs["initializer"] = single_thread_blas_initializer
    return ProcessPoolExecutor(**kwargs)  # type: ignore[arg-type]


__all__ = [
    "chunksize_for_tasks",
    "create_process_pool",
    "recommended_workers",
    "set_single_thread_blas_env",
    "single_thread_blas_initializer",
]
