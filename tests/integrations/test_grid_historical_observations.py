"""Historical Grid observations advance state without replaying entries."""
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D
import json

import pytest

from factorlab.strategies.adapters import GridAdapter
from factorlab.strategies.decision_runtime import OrderStrategyRuntime
from factorlab.strategies.protocol import StrategySpec
from factorlab.trading.contracts import Market
from factorlab.trading.control import TradingController, RiskLimits
from factorlab.trading.paper import PaperBroker, Instrument, Quote
from factorlab.trading.storage import PaperStore, decode
from test_unified_strategy import bar, grid_strategy, journal, manifest


def start(path, strategy, at):
    broker = PaperBroker(Market.BINANCE_USDM, 'research',
        (Instrument('NVDAUSDT', 'USDT'),), {'USDT': D(1000)})
    controller = TradingController(broker, PaperStore(path), clock=lambda: at,
        limits=RiskLimits(D(500), D(500), D(800), 10))
    runner = OrderStrategyRuntime(journal(strategy, controller, 'grid-demo'))
    assert runner.recover().reconciled
    return runner, controller


def engine(runner):
    parameters = runner.journal.strategy.manifest['parameters']
    adapter = GridAdapter(**parameters)
    commands = decode(json.loads(runner.journal._latest()['commands']))
    grid = adapter._engine(dict(deployment_id='grid-demo', account={'account_id': 'research'}))
    for command in commands:
        adapter._apply(grid, command)
    return grid


def test_default_orders_strategy_history_does_not_decide(tmp_path):
    strategy = manifest(tmp_path / 'strategy', '''
from factorlab.strategies.protocol import StrategySpec, Decision
class Passive:
    def describe(self):
        return StrategySpec('passive', '1', 'orders', ['NVDAUSDT'], [60], ['closed_bar'])
    def decide(self, event, state):
        raise AssertionError('historical decide is not opted in')
''', 'Passive')
    assert not strategy.spec.historical_observations
    assert 'historical_observations' not in strategy.binding['spec']
    runner, controller = start(tmp_path / 'paper.db', strategy, bar(21, 150).closed_at)
    assert runner.historical_bar(bar(19, 150))
    assert runner.journal._latest() == {}
    assert not controller.broker.state.orders
    binding = dict(strategy.binding)
    controller.store.close()
    from factorlab.strategies.loader import LoadedStrategy
    restored = LoadedStrategy(tmp_path / 'strategy', trusted=True)
    assert restored.binding == binding
    runner, controller = start(tmp_path / 'paper.db', restored, bar(21, 150).closed_at)
    assert runner.journal._latest() == {}
    controller.store.close()


def test_old_spec_payload_defaults_to_no_history():
    spec = StrategySpec(**dict(strategy_id='old', version='1', decision_kind='orders',
        symbols=['NVDAUSDT'], intervals=[60], data_types=['closed_bar']))
    assert spec.historical_observations is False


@pytest.mark.parametrize('mode', ['arithmetic', 'atr'])
def test_grid_history_warmup_survives_restart_without_orders(tmp_path, mode):
    strategy = grid_strategy(tmp_path / 'strategy', 'research', mode=mode)
    assert strategy.spec.historical_observations is True
    assert strategy.binding['spec']['historical_observations'] is True
    at = bar(21, 150).closed_at + timedelta(milliseconds=1)
    path = tmp_path / 'paper.db'
    runner, controller = start(path, strategy, at)
    for i in (18, 19, 20):
        assert runner.historical_bar(replace(bar(i, 150), high=D(153), low=D(147)))
        assert not controller.broker.state.orders
        assert runner._runtime()['pending'] is None
        assert not engine(runner).active
    state = runner.journal._latest()
    if mode == 'atr':
        assert len(engine(runner).true_ranges) >= 2
    controller.store.close()
    runner, controller = start(path, strategy, at)
    assert runner.journal._latest() == state
    assert not runner.historical_bar(bar(19, 150))
    assert runner.closed_bar(replace(bar(21, 150), high=D(153), low=D(147)))
    runner.quote(Quote('NVDAUSDT', D(150), at))
    assert len(controller.broker.state.orders) == 1
    controller.store.close()


def test_historical_breakout_persists_when_price_returns_inside(tmp_path):
    strategy = grid_strategy(tmp_path / 'strategy', 'research')
    path = tmp_path / 'paper.db'
    at = bar(21, 150).closed_at + timedelta(milliseconds=1)
    runner, controller = start(path, strategy, at)
    assert runner.historical_bar(bar(18, 150))
    assert runner.historical_bar(bar(19, 160))
    assert runner.historical_bar(bar(20, 150))
    assert engine(runner).state == 'PAUSED'
    controller.store.close()
    runner, controller = start(path, strategy, at)
    assert engine(runner).state == 'PAUSED'
    assert runner.closed_bar(bar(21, 150))
    runner.quote(Quote('NVDAUSDT', D(150), at))
    assert not controller.broker.state.orders
    controller.store.close()


def test_history_preserves_real_order_and_durable_cancellation(tmp_path):
    strategy = grid_strategy(tmp_path / 'strategy', 'research')
    at = bar(18, 150).closed_at + timedelta(milliseconds=1)
    path = tmp_path / 'paper.db'
    runner, controller = start(path, strategy, at)
    assert runner.closed_bar(bar(18, 150))
    runner.quote(Quote('NVDAUSDT', D(150), at))
    runner.sync_reconciled()
    key, = controller.broker.state.orders
    at = bar(21, 150).closed_at + timedelta(milliseconds=1)
    controller.clock = lambda: at
    assert runner.historical_bar(bar(19, 150))
    assert key in engine(runner).orders
    runner._runtime()['status'] = 'PAUSED'
    assert runner.historical_bar(bar(20, 160))
    assert key in engine(runner).cancellations
    assert [offer['key'] for offer in runner._runtime()['pending']['cancellations']] == [key]
    assert len(controller.broker.state.orders) == 1
    controller.store.close()
    runner, controller = start(path, strategy, at)
    assert [offer['key'] for offer in runner._runtime()['pending']['cancellations']] == [key]
    assert key in engine(runner).orders
    controller.store.close()
