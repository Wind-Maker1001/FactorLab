"""Task-scoped backend declarations and lazy, implementation-specific resolution."""

from dataclasses import asdict, dataclass, replace
from enum import StrEnum
from importlib import import_module
from types import ModuleType
from typing import Callable

from factorlab.errors import FactorLabError

from .config import ComputeConfig


class Device(StrEnum):
    CPU = "cpu"
    GPU = "gpu"


class ComputeUnavailableError(FactorLabError):
    """No declared implementation satisfies the requested device policy."""


class ComputeMemoryError(ComputeUnavailableError):
    """An implementation cannot execute within its GPU memory budget.

    Implementations translate only their native allocation/budget failures into
    this error. V1 does not allocate memory or retry an operation on the CPU.
    """


@dataclass(frozen=True)
class BackendStatus:
    """Probe evidence, not an assertion that the research task has executed.

    ``driver_visible=None`` means not inspected or not applicable. A visible
    driver alone never establishes runtime or implementation availability.
    Failure reasons should identify missing libraries, incompatible versions,
    unsupported capabilities, or insufficient memory as appropriate.
    """

    available: bool
    reason: str | None = None
    version: str | None = None
    driver_visible: bool | None = None
    runtime_available: bool = False

    def __post_init__(self) -> None:
        if type(self.available) is not bool or type(self.runtime_available) is not bool:
            raise ValueError("backend availability fields must be booleans")
        if self.driver_visible is not None and type(self.driver_visible) is not bool:
            raise ValueError("driver_visible must be a boolean or null")
        if self.available and (not self.runtime_available or self.driver_visible is False):
            raise ValueError("available backend requires a usable runtime and no known driver failure")
        if self.available and self.reason is not None:
            raise ValueError("available backend must not have a failure reason")
        if not self.available and (not isinstance(self.reason, str) or not self.reason.strip()):
            raise ValueError("unavailable backend requires a reason")
        if self.version is not None and (not isinstance(self.version, str) or not self.version.strip()):
            raise ValueError("backend version must be a non-empty string or null")


def probe_runtime(
    module_name: str, check: Callable[[ModuleType], BackendStatus],
) -> BackendStatus:
    """Import an optional runtime lazily, then check the target implementation.

    The trusted ``check`` must test the requested backend, including required
    features/version support, and translate expected native failures to status.
    Import success or device enumeration alone must not yield availability.
    Unexpected exceptions propagate rather than silently selecting another device.
    """
    try:
        runtime = import_module(module_name)
    except (ImportError, OSError) as exc:
        return BackendStatus(False, f"runtime {module_name} unavailable: {exc}")
    status = check(runtime)
    version = getattr(runtime, "__version__", None)
    if status.version is None and isinstance(version, str) and version.strip():
        status = replace(status, version=version)
    return status


@dataclass(frozen=True)
class BackendCapability:
    """One implemented task path. Declaration order is selection priority.

    Backend names identify concrete paths, e.g. ``numpy``, ``xgboost-cuda`` or
    ``lightgbm-opencl``; device type alone does not identify the runtime.
    Probes are trusted Python callables, never configuration import strings.
    They receive the budget so a task can reject an infeasible allocation.
    """

    device: Device
    backend: str
    probe: Callable[[ComputeConfig], BackendStatus]

    def __post_init__(self) -> None:
        object.__setattr__(self, "device", Device(self.device))
        if not isinstance(self.backend, str) or not self.backend.strip():
            raise ValueError("backend must be a non-empty name")
        if not callable(self.probe):
            raise ValueError("backend requires an implementation-specific probe")


@dataclass(frozen=True)
class ResolvedCompute:
    requested: ComputeConfig
    device: Device
    backend: str
    status: BackendStatus
    fallback: bool
    fallback_reason: str | None

    def to_dict(self) -> dict[str, object]:
        """JSON-compatible selection evidence for ExperimentTracker.environment.

        A successful probe is readiness evidence, not proof of task execution.
        Persist this with a successful run only after dispatching that exact path.
        """
        return {
            "requested": asdict(self.requested),
            "device": self.device.value,
            "backend": self.backend,
            "backend_version": self.status.version,
            "probe": asdict(self.status),
            "fallback": self.fallback,
            "fallback_reason": self.fallback_reason,
            "gpu_memory_limit_mb": self.requested.gpu_memory_limit_mb,
            # TODO: GPU implementations must enforce budgets before claiming a limit.
            "gpu_memory_limit_enforced": False,
        }


def resolve_compute(
    capabilities: tuple[BackendCapability, ...],
    config: ComputeConfig = ComputeConfig(),
) -> ResolvedCompute:
    """Choose only among declared task implementations; never discover plugins.

    CPU never probes GPU. Auto tries GPU then CPU, independently of the explicit
    GPU fallback flag. Only reported unavailability permits trying another path;
    unexpected probe exceptions propagate. No capability means no implementation.
    """
    capabilities = tuple(capabilities)
    identities = [(item.device, item.backend) for item in capabilities]
    if len(set(identities)) != len(identities):
        raise ValueError("duplicate backend capability")
    failures = []
    devices = (Device.CPU,) if config.device == "cpu" else (Device.GPU, Device.CPU)
    for device in devices:
        if device == Device.CPU and config.device == "gpu" and not config.allow_fallback:
            break
        candidates = [item for item in capabilities if item.device == device]
        if not candidates:
            failures.append(f"{device.value}: task declares no implementation")
        for capability in candidates:
            status = capability.probe(config)
            if status.available:
                fallback = config.device == "gpu" and device == Device.CPU
                return ResolvedCompute(
                    config, device, capability.backend, status, fallback,
                    "; ".join(failures) if fallback else None,
                )
            failures.append(f"{capability.backend}: {status.reason}")
    raise ComputeUnavailableError(
        f"compute.device={config.device}: " + "; ".join(failures)
    )
