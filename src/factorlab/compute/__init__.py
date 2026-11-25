"""Lightweight compute policy; importing this package initializes no GPU runtime."""

from .config import ComputeConfig
from .backend import (
    BackendCapability,
    BackendStatus,
    ComputeMemoryError,
    ComputeUnavailableError,
    Device,
    ResolvedCompute,
    probe_runtime,
    resolve_compute,
)
from .parallel import (
    apply_single_thread_blas_limits,
    chunksize_for_tasks,
    create_process_pool,
    inspect_threadpools,
    recommended_workers,
    set_single_thread_blas_env,
    single_thread_blas_initializer,
)

__all__ = [
    "BackendCapability", "BackendStatus", "ComputeConfig", "ComputeMemoryError",
    "ComputeUnavailableError", "Device", "ResolvedCompute", "probe_runtime",
    "resolve_compute", "apply_single_thread_blas_limits", "chunksize_for_tasks",
    "create_process_pool", "inspect_threadpools", "recommended_workers",
    "set_single_thread_blas_env", "single_thread_blas_initializer",
]
