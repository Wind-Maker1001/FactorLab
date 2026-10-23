"""Strict YAML loading with repository-root-relative path resolution."""

from __future__ import annotations

from dataclasses import MISSING, fields, is_dataclass
from datetime import date, datetime
from pathlib import Path
from typing import (
    Any,
    Dict,
    Mapping,
    Optional,
    Tuple,
    Type,
    TypeVar,
    Union,
    get_args,
    get_origin,
    get_type_hints,
)

import yaml

from qbot.research.config.models import PathsConfig, ResearchConfig
from qbot.research.errors import ConfigurationError, FactorLabError


T = TypeVar("T")


def discover_repository_root(start: Optional[Path] = None) -> Path:
    """Locate the Qbot repository without relying on the process cwd."""

    candidate = (start or Path(__file__)).resolve()
    if candidate.is_file():
        candidate = candidate.parent
    for directory in (candidate,) + tuple(candidate.parents):
        if (directory / "qbot").is_dir() and (directory / "README.md").is_file():
            return directory
    raise ConfigurationError(
        "cannot locate repository root (expected qbot/ and README.md)"
    )


def load_config(
    path: Union[str, Path], repo_root: Optional[Union[str, Path]] = None
) -> ResearchConfig:
    """Load and validate a FactorLab YAML configuration.

    Unknown keys are rejected recursively. Relative configuration and artifact
    paths are interpreted from the repository root, so CLI results do not
    depend on the caller's current working directory.
    """

    root = (
        Path(repo_root).expanduser().resolve()
        if repo_root is not None
        else discover_repository_root()
    )
    config_path = Path(path).expanduser()
    if not config_path.is_absolute():
        config_path = root / config_path
    config_path = config_path.resolve()

    try:
        text = config_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigurationError(
            "cannot read configuration {0}: {1}".format(config_path, exc)
        ) from exc

    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigurationError(
            "invalid YAML in {0}: {1}".format(config_path, exc)
        ) from exc

    if document is None:
        raise ConfigurationError("configuration must not be empty")
    if not isinstance(document, Mapping):
        raise ConfigurationError("configuration root must be a mapping")

    try:
        config = _construct_dataclass(ResearchConfig, document, "config")
        resolved_paths = PathsConfig(
            data_root=_resolve_path(config.paths.data_root, root),
            results_root=_resolve_path(config.paths.results_root, root),
            market_input_root=(
                None if config.paths.market_input_root is None
                else _resolve_path(config.paths.market_input_root, root)
            ),
        )
        object.__setattr__(config, "paths", resolved_paths)
        object.__setattr__(config, "config_path", config_path)
        object.__setattr__(config, "repo_root", root)
        config.validate()
    except FactorLabError:
        raise
    except (TypeError, ValueError) as exc:
        raise ConfigurationError(str(exc)) from exc
    return config


def _resolve_path(value: Path, root: Path) -> Path:
    expanded = value.expanduser()
    if not expanded.is_absolute():
        expanded = root / expanded
    return expanded.resolve()


def _construct_dataclass(
    target: Type[T], mapping: Mapping[str, Any], location: str
) -> T:
    if not isinstance(mapping, Mapping):
        raise ConfigurationError("{0} must be a mapping".format(location))

    init_fields = {item.name: item for item in fields(target) if item.init}
    unknown = sorted(set(mapping) - set(init_fields))
    if unknown:
        raise ConfigurationError(
            "unknown field(s) at {0}: {1}".format(location, ", ".join(unknown))
        )

    type_hints = get_type_hints(target)
    values: Dict[str, Any] = {}
    missing = []
    for name, item in init_fields.items():
        if name not in mapping:
            if item.default is MISSING and item.default_factory is MISSING:
                missing.append(name)
            continue
        expected = type_hints[name]
        values[name] = _convert_value(
            mapping[name], expected, "{0}.{1}".format(location, name)
        )
    if missing:
        raise ConfigurationError(
            "missing required field(s) at {0}: {1}".format(
                location, ", ".join(sorted(missing))
            )
        )
    return target(**values)


def _convert_value(value: Any, expected: Any, location: str) -> Any:
    if is_dataclass(expected):
        return _construct_dataclass(expected, value, location)

    origin = get_origin(expected)
    arguments = get_args(expected)
    if origin is dict:
        if not isinstance(value, Mapping):
            raise ConfigurationError("{0} must be a mapping".format(location))
        key_type, value_type = arguments
        return {
            _convert_value(key, key_type, "{0}.<key>".format(location)): _convert_value(
                item, value_type, "{0}.{1}".format(location, key)
            )
            for key, item in value.items()
        }
    if origin in (tuple, Tuple):
        if not isinstance(value, (list, tuple)):
            raise ConfigurationError("{0} must be a sequence".format(location))
        if len(arguments) == 2 and arguments[1] is Ellipsis:
            return tuple(
                _convert_value(item, arguments[0], "{0}[]".format(location))
                for item in value
            )
        if len(arguments) != len(value):
            raise ConfigurationError("{0} has an invalid length".format(location))
        return tuple(
            _convert_value(item, item_type, "{0}[]".format(location))
            for item, item_type in zip(value, arguments)
        )
    if origin is Union:
        if type(None) in arguments and value is None:
            return None
        candidates = tuple(item for item in arguments if item is not type(None))
        for candidate in candidates:
            try:
                return _convert_value(value, candidate, location)
            except ConfigurationError:
                continue
        raise ConfigurationError("{0} has an invalid value".format(location))

    if expected is Path:
        if not isinstance(value, (str, Path)):
            raise ConfigurationError("{0} must be a path string".format(location))
        return Path(value)
    if expected is date:
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        if not isinstance(value, str):
            raise ConfigurationError("{0} must be an ISO date".format(location))
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise ConfigurationError("{0} must be an ISO date".format(location)) from exc
    if expected is bool:
        if type(value) is not bool:
            raise ConfigurationError("{0} must be a boolean".format(location))
        return value
    if expected is int:
        if type(value) is not int:
            raise ConfigurationError("{0} must be an integer".format(location))
        return value
    if expected is float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConfigurationError("{0} must be a number".format(location))
        return float(value)
    if expected is str:
        if not isinstance(value, str):
            raise ConfigurationError("{0} must be a string".format(location))
        return value
    return value


__all__ = ["discover_repository_root", "load_config"]
