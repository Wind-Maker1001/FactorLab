import importlib.util
import json
from decimal import Decimal
from pathlib import Path

import pytest

from factorlab.strategies.package import install_package
from factorlab.strategies.loader import LoadedStrategy

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('prepare_p10_12_alpaca', ROOT / 'tools' / 'prepare_p10_12_alpaca.py')
preparation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(preparation)


def test_preparation_builds_frozen_packages_without_training_or_promoting_v69(tmp_path):
    artifact = json.loads((ROOT / 'examples/v179_v182_alpaca/artifacts/v179_v182_ridge_target.json').read_text())
    output = tmp_path / 'prepared'
    identities = preparation.prepare(account_id='paper-acceptance', revision='a' * 40,
        artifact=artifact, destination=output, releases=Path('/existing/releases'),
        benchmark=Path('/private/sp500tr.parquet'), account_budget=Decimal(100000), fee_rate=Decimal(0),
        symbol_limits={symbol: Decimal(99900) for symbol in preparation.ASSETS})
    config = json.loads((output / 'proposed-instances.json').read_text())
    assert not (output / 'instances.json').exists()
    assert config['capital_policy']['symbol_limits'] == {symbol: '99900' for symbol in preparation.ASSETS}
    assert sum(Decimal(row['funding']['capital']) for row in config['instances']) == Decimal(100000)
    assert config['instances'][0]['native_v69'] is True
    assert config['instances'][0]['binding']['allocation_id'] == 'v69-original'
    assert [row['funding']['capital'] for row in config['instances'][1:]] == ['50', '50']
    for variant, identity in identities.items():
        release, manifest = install_package(output / (variant + '.zip'), tmp_path / 'releases')
        loaded = LoadedStrategy.from_strategy_package(release, trusted=True)
        assert loaded.spec.capital['requested'] == Decimal(50)
        assert manifest['package_id'] == identity
        assert manifest['evidence']['champion_promotion'] is False
        assert manifest['model_artifact']['artifact_sha256'] == artifact['artifact_sha256']
    assert not (output / 'v69.zip').exists()


@pytest.mark.parametrize('invalid', ['missing', 'excess', 'nonfinite'])
def test_preparation_rejects_incomplete_or_invalid_aggregate_limits_before_build(tmp_path, invalid):
    limits = {symbol: Decimal(100000) for symbol in preparation.ASSETS}
    symbol = next(iter(limits))
    if invalid == 'missing':
        del limits[symbol]
    else:
        limits[symbol] = Decimal(100001) if invalid == 'excess' else Decimal('NaN')
    with pytest.raises(ValueError):
        preparation.prepare(account_id='paper-acceptance', revision='a' * 40,
            artifact={}, destination=tmp_path / 'prepared', releases=Path('/existing/releases'),
            benchmark=Path('/private/sp500tr.json'), account_budget=Decimal(100000),
            fee_rate=Decimal(0), symbol_limits=limits)
    assert not (tmp_path / 'prepared').exists()
