"""Domain exceptions shared by the isolated FactorLab modules."""

from typing import Optional


POINT_IN_TIME_UNIVERSE_REQUIRED = "POINT-IN-TIME UNIVERSE REQUIRED"
SECURITY_STATUS_DATA_REQUIRED = "SECURITY STATUS DATA REQUIRED"


class FactorLabError(Exception):
    """Base class for expected, user-facing FactorLab failures."""


class ConfigurationError(FactorLabError):
    """Raised when a research configuration is missing or invalid."""


class SchemaError(FactorLabError):
    """Raised when market data violates the canonical schema."""


class ProviderError(FactorLabError):
    """Raised when an upstream data provider cannot satisfy a request."""


class ProviderNetworkError(ProviderError):
    """An explicitly identified recoverable provider transport/HTTP failure.

    Structural data failures and generic SDK/local errors must not use this
    class. Download orchestration can skip a failed security only when this
    classification is supported by the underlying exception.
    """


class CacheError(FactorLabError):
    """Raised when a cache artifact cannot be read or written safely."""


class DataQualityError(FactorLabError):
    """Raised when incomplete data would make a result unreliable."""


class SecurityStatusDataError(DataQualityError):
    """Raised when date-effective ST/trading/listing evidence is incomplete."""

    def __init__(self, detail: Optional[str] = None) -> None:
        message = SECURITY_STATUS_DATA_REQUIRED
        if detail:
            message = "{0}: {1}".format(message, detail)
        super().__init__(message)


class PointInTimeUniverseError(DataQualityError):
    """Base class for point-in-time universe integrity failures."""


class PointInTimeUniverseRequiredError(PointInTimeUniverseError):
    """Raised rather than silently substituting current constituents."""

    def __init__(self, detail: Optional[str] = None) -> None:
        message = POINT_IN_TIME_UNIVERSE_REQUIRED
        if detail:
            message = "{0}: {1}".format(message, detail)
        super().__init__(message)


class FactorError(FactorLabError):
    """Raised when factor computation or preprocessing is invalid."""


class LabelError(FactorLabError):
    """Raised when a forward-return label cannot be constructed safely."""


class EvaluationError(FactorLabError):
    """Raised when factor evaluation inputs are invalid."""


ConfigError = ConfigurationError


__all__ = [
    "POINT_IN_TIME_UNIVERSE_REQUIRED",
    "SECURITY_STATUS_DATA_REQUIRED",
    "CacheError",
    "ConfigError",
    "ConfigurationError",
    "DataQualityError",
    "EvaluationError",
    "FactorError",
    "FactorLabError",
    "LabelError",
    "PointInTimeUniverseError",
    "PointInTimeUniverseRequiredError",
    "ProviderError",
    "ProviderNetworkError",
    "SchemaError",
    "SecurityStatusDataError",
]
