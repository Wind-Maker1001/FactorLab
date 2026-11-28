from decimal import Decimal
import importlib.util
import json
from pathlib import Path

from factorlab.strategies.package import install_package


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_packages_enter_then_exit_and_keep_zero_targets_after_restart(tmp_path):
    prepared = tmp_path / 'prepared'
    tool = load_module('prepare_p10_12_binance',
        Path(__file__).parents[2] / 'tools' / 'prepare_p10_12_binance.py')
    tool.prepare(prepared, account_id='demo-test', symbols=['DOGEUSDT', 'XRPUSDT'], revision='a' * 40)
    for identity, symbol, exit_after in zip(json.loads((prepared / 'packages.json').read_text()),
                                          ['DOGEUSDT', 'XRPUSDT'], [3, 5]):
        release, manifest = install_package(prepared / identity['archive'], tmp_path / 'releases')
        strategy_class = load_module('bounded_' + symbol, release / 'strategy.py').Strategy
        strategy = strategy_class()
        assert strategy.describe().schedule['interval_seconds'] == 60
        assert manifest['evidence']['champion_promotion'] is False
        state = {}
        for count in range(1, exit_after + 3):
            strategy = strategy_class()
            decision = strategy.decide({}, state)
            assert decision.targets == {symbol: Decimal('.6') if count <= exit_after else Decimal(0)}
            state = decision.state
