"""Shared multi-process execution helpers for offline FactorLab research.

Prevents BLAS/OpenMP thread oversubscription across worker processes using both
environment variables (for libraries imported lazily) and ``threadpoolctl``
runtime threadpool limiting (for libraries already initialized during Windows
``spawn`` bootstrap), computes balanced map chunksizes, and provides
conservative worker-count recommendations based on workload size and CPU cores.
"""
from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor
from multiprocessing import get_context
import os
from typing import Any

_BLAS_THREAD_VARS = (
    "OMP_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "MKL_NUM_THREADS",
    "VECLIB_MAXIMUM_THREADS",
    "NUMEXPR_NUM_THREADS",
    "BLIS_NUM_THREADS",
)

# Keeps any threadpoolctl limiter handle alive for the worker process lifetime.
_WORKER_THREADPOOL_LIMITER: Any = None


def set_single_thread_blas_env() -> None:
    """Pin numerical library thread env vars to 1 for libraries imported later."""
    for name in _BLAS_THREAD_VARS:
        os.environ[name] = "1"


def inspect_threadpools() -> list[dict[str, Any]]:
    """Return actual loaded BLAS/OpenMP threadpools reported by threadpoolctl."""
    try:
        from threadpoolctl import threadpool_info
    except ImportError:
        return []
    return [dict(entry) for entry in threadpool_info()]


def apply_single_thread_blas_limits() -> dict[str, Any]:
    """Enforce single-thread BLAS/OpenMP limits in the current process and report status.

    Sets thread env vars for any lazily imported libraries and applies
    ``threadpoolctl.threadpool_limits(limits=1)`` to already-loaded BLAS/OpenMP
    runtimes (critical under Windows ``spawn`` where NumPy/SciPy may initialize
    before the pool initializer runs). Reports actual post-limit thread counts
    without claiming success for any runtime that could not be limited.
    """
    global _WORKER_THREADPOOL_LIMITER
    set_single_thread_blas_env()
    try:
        from threadpoolctl import threadpool_info, threadpool_limits
    except ImportError:
        return {
            "threadpoolctl_available": False,
            "all_limited_to_one": False,
            "pools": [],
            "uncontrolled_pools": [],
        }

    _WORKER_THREADPOOL_LIMITER = threadpool_limits(limits=1)
    pools = [dict(entry) for entry in threadpool_info()]
    uncontrolled = [p for p in pools if int(p.get("num_threads", 0)) != 1]
    return {
        "threadpoolctl_available": True,
        "all_limited_to_one": len(uncontrolled) == 0,
        "pools": pools,
        "uncontrolled_pools": uncontrolled,
    }


def single_thread_blas_initializer() -> None:
    """Top-level ProcessPoolExecutor initializer for Windows spawn & Linux."""
    apply_single_thread_blas_limits()


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
    """Create a ProcessPoolExecutor with optional single-thread BLAS/OpenMP limiting.

    Does not mutate the parent process's environment variables or threadpools.
    """
    kwargs: dict[str, object] = {"max_workers": max(1, int(max_workers))}
    if mp_start_method is not None:
        kwargs["mp_context"] = get_context(mp_start_method)
    if single_thread_blas:
        kwargs["initializer"] = single_thread_blas_initializer
    return ProcessPoolExecutor(**kwargs)  # type: ignore[arg-type]


__all__ = [
    "apply_single_thread_blas_limits",
    "chunksize_for_tasks",
    "create_process_pool",
    "inspect_threadpools",
    "recommended_workers",
    "set_single_thread_blas_env",
    "single_thread_blas_initializer",
]
