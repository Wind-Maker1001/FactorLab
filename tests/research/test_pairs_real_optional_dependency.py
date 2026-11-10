import builtins
import importlib

import pytest


def test_real_research_import_without_statsmodels(monkeypatch, tmp_path):
    original_import = builtins.__import__

    def without_statsmodels(name, *args, **kwargs):
        if name == "statsmodels" or name.startswith("statsmodels."):
            raise ModuleNotFoundError("No module named 'statsmodels'", name="statsmodels")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_statsmodels)
    from factorlab.strategies import pairs_real_cli

    importlib.reload(pairs_real_cli)
    output = tmp_path / "research"
    with pytest.raises(ModuleNotFoundError, match=r"pip install .*factorlab\[pairs\]") as error:
        pairs_real_cli.execute({}, candidate=tmp_path / "missing", output=output)
    assert error.value.name == "statsmodels"
    assert not output.exists()
