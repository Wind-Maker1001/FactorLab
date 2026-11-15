"""Device policy shared with the existing research configuration loader."""

from dataclasses import dataclass
from typing import Literal, Optional

from factorlab.errors import ConfigurationError


@dataclass(frozen=True)
class ComputeConfig:
    device: Literal["cpu", "gpu", "auto"] = "cpu"
    allow_fallback: bool = False
    gpu_memory_limit_mb: Optional[int] = None

    def __post_init__(self) -> None:
        if self.device not in ("cpu", "gpu", "auto"):
            raise ConfigurationError("compute.device must be cpu, gpu or auto")
        if type(self.allow_fallback) is not bool:
            raise ConfigurationError("compute.allow_fallback must be a boolean")
        limit = self.gpu_memory_limit_mb
        if limit is not None and (type(limit) is not int or limit <= 0):
            raise ConfigurationError("compute.gpu_memory_limit_mb must be a positive integer or null")
