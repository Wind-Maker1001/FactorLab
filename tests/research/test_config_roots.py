"""Root contracts exercised independently of the actual checkout/cwd topology."""
from pathlib import Path

import pytest

from factorlab.config import loader
from factorlab.errors import ConfigurationError


def source_tree(root):
    module = root / "src/factorlab/config/loader.py"
    module.parent.mkdir(parents=True)
    module.write_text("# synthetic module location\n", encoding="utf-8")
    (root / "pyproject.toml").write_text("# synthetic source marker\n", encoding="utf-8")
    return module


def config_at(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("""project: {name: synthetic-root-contract}
data: {start: '2024-01-02', end: '2024-04-30'}
universe: {name: CSI500, index_symbol: '000905.SH', provider: baostock, point_in_time: true}
factors: [REV1, MOM20]
paths: {data_root: inputs, results_root: outputs, market_input_root: market}
""", encoding="utf-8")
    return path


def assert_root(config, root, path):
    assert config.repo_root == root.resolve()
    assert config.config_path == path.resolve()
    assert config.paths.data_root == (root / "inputs").resolve()
    assert config.paths.results_root == (root / "outputs").resolve()
    assert config.paths.market_input_root == (root / "market").resolve()


@pytest.mark.parametrize("cwd_kind", ["checkout", "subdirectory", "unrelated"])
def test_source_relative_config_uses_module_checkout(tmp_path, monkeypatch, cwd_kind):
    root = tmp_path / "source"
    monkeypatch.setattr(loader, "__file__", str(source_tree(root)))
    path = config_at(root / "configs/research/test.yaml")
    cwd = {"checkout": root, "subdirectory": path.parent,
           "unrelated": tmp_path / "outside"}[cwd_kind]
    cwd.mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(cwd)
    assert_root(loader.load_config("configs/research/test.yaml"), root, path)
    assert loader.discover_repository_root() == root


def test_external_absolute_config_preserves_source_artifact_base(tmp_path, monkeypatch):
    root = tmp_path / "source"
    monkeypatch.setattr(loader, "__file__", str(source_tree(root)))
    path = config_at(tmp_path / "external/test.yaml")
    monkeypatch.chdir(path.parent)
    assert_root(loader.load_config(path), root, path)
    relative = Path("configs/../..") / path.relative_to(tmp_path)
    assert_root(loader.load_config(relative), root, path)


def test_other_checkout_absolute_config_is_explicit(tmp_path, monkeypatch):
    first, second = tmp_path / "a", tmp_path / "b"
    monkeypatch.setattr(loader, "__file__", str(source_tree(first)))
    source_tree(second)
    path = config_at(second / "configs/test.yaml")
    monkeypatch.chdir(first)
    assert_root(loader.load_config(path), second, path)


def test_multiple_relative_source_roots_require_disambiguation(tmp_path, monkeypatch):
    first, second = tmp_path / "a", tmp_path / "b"
    monkeypatch.setattr(loader, "__file__", str(source_tree(first)))
    source_tree(second)
    config_at(first / "configs/test.yaml")
    path = config_at(second / "configs/test.yaml")
    monkeypatch.chdir(second)
    with pytest.raises(ConfigurationError, match="ambiguous configuration roots"):
        loader.load_config("configs/test.yaml")
    assert_root(loader.load_config("configs/test.yaml", repo_root=second), second, path)


@pytest.mark.parametrize("cwd_is_repository", [False, True])
def test_wheel_absolute_config_is_standalone(tmp_path, monkeypatch, cwd_is_repository):
    monkeypatch.setattr(loader, "__file__", str(tmp_path / "wheel/site-packages/factorlab/config/loader.py"))
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    if cwd_is_repository:
        source_tree(cwd)
    monkeypatch.chdir(cwd)
    path = config_at(tmp_path / "standalone/nested/test.yaml")
    assert_root(loader.load_config(path), path.parent, path)
    with pytest.raises(ConfigurationError, match="cannot locate"):
        loader.discover_repository_root()
    with pytest.raises(ConfigurationError, match="relative configuration requires"):
        loader.load_config("test.yaml")


@pytest.mark.parametrize("absolute_config", [False, True])
def test_explicit_root_wins_without_a_repository(tmp_path, monkeypatch, absolute_config):
    monkeypatch.setattr(loader, "__file__", str(tmp_path / "wheel/site-packages/factorlab/config/loader.py"))
    root = tmp_path / "explicit"
    root.mkdir()
    path = config_at((tmp_path / "external" if absolute_config else root) / "test.yaml")
    monkeypatch.chdir(tmp_path)
    assert_root(loader.load_config(path if absolute_config else "test.yaml", repo_root=root), root, path)


def test_explicit_root_must_be_absolute_and_exist(tmp_path):
    with pytest.raises(ConfigurationError, match="must be absolute"):
        loader.load_config("test.yaml", repo_root=".")
    with pytest.raises(ConfigurationError, match="existing directory"):
        loader.load_config("test.yaml", repo_root=tmp_path / "absent")


def test_missing_config_does_not_fall_back_to_cwd(tmp_path, monkeypatch):
    root = tmp_path / "source"
    monkeypatch.setattr(loader, "__file__", str(source_tree(root)))
    config_at(tmp_path / "test.yaml")
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ConfigurationError, match="cannot read configuration"):
        loader.load_config("test.yaml")
