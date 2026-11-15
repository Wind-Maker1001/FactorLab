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

__all__ = [
    "BackendCapability", "BackendStatus", "ComputeConfig", "ComputeMemoryError",
    "ComputeUnavailableError", "Device", "ResolvedCompute", "probe_runtime",
    "resolve_compute",
]
