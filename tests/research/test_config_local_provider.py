"""Strict configuration contracts for the completely local market provider."""

import json
from pathlib import Path

import pytest
import yaml

from qbot.research.config import load_config
from qbot.research.errors import ConfigurationError


def _document():
    return {
        "project": {"name": "synthetic-local-provider-config-test"},
        "data": {
            "start": "2024-01-02",
            "end": "2024-04-30",
            "provider": "local_parquet",
        },
        "universe": {"provider": "local", "status_provider": "local"},
        "paths": {
            "data_root": "data/research/local-cache",
            "results_root": "results/local-test",
            "market_input_root": "data/research/local-input",
        },
        "factors": ["REV1", "MOM20"],
    }


def _write(root, document):
    directory = root / "configs" / "research"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "local.yaml"
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return path


def test_local_bundle_path_is_repository_relative_not_cwd(tmp_path, monkeypatch):
    root = tmp_path / "repository"
    root.mkdir()
    _write(root, _document())
    other_cwd = tmp_path / "elsewhere"
    other_cwd.mkdir()
    monkeypatch.chdir(other_cwd)

    config = load_config("configs/research/local.yaml", repo_root=root)

    assert config.data.provider == "local_parquet"
    assert config.paths.market_input_root == (root / "data/research/local-input").resolve()
    assert config.paths.data_root == (root / "data/research/local-cache").resolve()
    assert config.paths.results_root == (root / "results/local-test").resolve()
    assert not config.paths.market_input_root.exists()
    assert config.live_trading is False
    assert config.universe.point_in_time is True
    snapshot = json.loads(json.dumps(config.to_dict()))
    assert snapshot["paths"]["market_input_root"] == str(config.paths.market_input_root)


def test_absolute_local_input_path_is_preserved(tmp_path):
    document = _document()
    source_root = tmp_path / "external-market-input"
    document["paths"]["market_input_root"] = str(source_root.resolve())

    config = load_config(_write(tmp_path, document), repo_root=tmp_path)

    assert config.paths.market_input_root == source_root.resolve()


@pytest.mark.parametrize("missing_value", [None, "omitted"])
def test_local_provider_requires_explicit_bundle_path(tmp_path, missing_value):
    document = _document()
    if missing_value == "omitted":
        document["paths"].pop("market_input_root")
    else:
        document["paths"]["market_input_root"] = missing_value

    with pytest.raises(ConfigurationError, match="local_parquet requires paths.market_input_root"):
        load_config(_write(tmp_path, document), repo_root=tmp_path)


@pytest.mark.parametrize("provider", ["local", "Local_Parquet", "akshare-current", ""])
def test_market_provider_name_is_not_silently_coerced(tmp_path, provider):
    document = _document()
    document["data"]["provider"] = provider

    with pytest.raises(ConfigurationError, match="data.provider must be akshare or local_parquet"):
        load_config(_write(tmp_path, document), repo_root=tmp_path)


@pytest.mark.parametrize("path_value", [False, 10, [], {}])
def test_local_input_root_requires_a_path_not_an_arbitrary_yaml_value(tmp_path, path_value):
    document = _document()
    document["paths"]["market_input_root"] = path_value

    with pytest.raises(ConfigurationError, match="market_input_root"):
        load_config(_write(tmp_path, document), repo_root=tmp_path)


def test_unknown_local_bundle_setting_fails_fast(tmp_path):
    document = _document()
    document["paths"]["market_input_rooot"] = "typo"

    with pytest.raises(ConfigurationError, match="unknown field.*market_input_rooot"):
        load_config(_write(tmp_path, document), repo_root=tmp_path)


def test_akshare_configuration_does_not_require_local_bundle_path(tmp_path):
    document = _document()
    document["data"]["provider"] = "akshare"
    document["paths"].pop("market_input_root")

    config = load_config(_write(tmp_path, document), repo_root=tmp_path)

    assert config.paths.market_input_root is None
    assert config.to_dict()["paths"]["market_input_root"] is None


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("-inf"), float("nan"), True, "5"])
def test_network_timeout_remains_strict_and_finite_for_local_configs(tmp_path, timeout):
    document = _document()
    document["network"] = {"timeout_seconds": timeout}

    with pytest.raises(ConfigurationError, match="timeout_seconds"):
        load_config(_write(tmp_path, document), repo_root=tmp_path)
