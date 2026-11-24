"""Target signals size only against confirmed inventory after working orders end."""
from contextlib import closing
from datetime import timedelta
from decimal import Decimal as D

import pytest

from factorlab.integrations.binance_futures_trading.fake import NOW
from factorlab.integrations.binance_futures_trading.strategy_runtime import DemoStrategyRuntime
from factorlab.research.protocol_strategy import ResearchTargetRuntime
from factorlab.strategies.decision_runtime import DecisionJournal, SignalAdapter
from factorlab.strategies.protocol import Decision, StrategySpec
from factorlab.trading.contracts import Market, OrderStatus, Side
from factorlab.trading.control import RiskLimits, TradingController
from factorlab.trading.paper import Instrument, PaperBroker, Quote
from factorlab.trading.storage import PaperStore
from factorlab.trading.strategy import ClosedBar
from test_binance_demo_execution import DemoFixture, setup


SYMBOL = 'NVDAUSDT'


class FixedTarget:
    def __init__(self, *, weight=D('.1'), interval=60):
        self.spec = StrategySpec('fixed_target', '1', 'target', [SYMBOL], [interval], ['closed_bar'])
        self.binding = {'strategy': self.spec.strategy_id, 'weight': str(weight)}
        self.weight = weight

    def decide(self, event, state):
        weight = self.weight if event['bars'][-1]['close'] >= D(1) else D(0)
        return Decision(target_weight=weight, state=state)


def paper_controller(store, *, fee_rate=D(0)):
    broker = PaperBroker(Market.BINANCE_USDM, 'research', (Instrument(SYMBOL, 'USDT'),),
        {'USDT': D(1000)}, fee_rate=fee_rate)
    return TradingController(broker, store, clock=lambda: NOW,
        limits=RiskLimits(D(500), D(500), D(800), 10))


def target_journal(strategy, controller):
    return DecisionJournal(strategy, controller, deployment_id='target', symbol=SYMBOL,
        interval_seconds=strategy.spec.intervals[0], data_types=['closed_bar'])


@pytest.fixture
def paper_target(tmp_path):
    with closing(PaperStore(tmp_path / 'account.db')) as store:
        yield ResearchTargetRuntime(target_journal(FixedTarget(), paper_controller(store)))


def signal(runner, index, *, flat=False, price=D(1), interval=60):
    period = timedelta(seconds=interval)
    closed = NOW + index * period
    quoted = closed + timedelta(milliseconds=1)
    bar_price = D('.99') if flat else price
    controller = runner.controller
    controller.clock = controller.broker.clock = lambda: quoted
    runner.closed_bar(ClosedBar(SYMBOL, closed - period, closed, bar_price, bar_price,
        bar_price, bar_price, D(100), interval, 'SYNTHETIC'))
    return runner.quote(Quote(SYMBOL, price, quoted))


def test_repeated_target_waits_for_resting_buy(paper_target):
    controller = paper_target.controller
    entry = signal(paper_target, 0)
    assert controller.account().open_orders[0].intent.quantity == D(100)
    assert signal(paper_target, 1) is None
    assert tuple(controller.broker.state.orders) == (entry.client_order_id,)

    controller.fill(entry.client_order_id, D(100))
    assert controller.account().positions[0].quantity == D(100)
    assert signal(paper_target, 2) is None
    assert tuple(controller.broker.state.orders) == (entry.client_order_id,)


def test_partial_target_buy_waits_then_sizes_from_actual_inventory(paper_target):
    controller = paper_target.controller
    entry = signal(paper_target, 0)
    controller.fill(entry.client_order_id, D(40))
    working, = controller.account().open_orders
    assert (working.status, working.cumulative_filled_quantity) == (OrderStatus.PARTIALLY_FILLED, D(40))
    assert signal(paper_target, 1) is None
    assert tuple(controller.broker.state.orders) == (entry.client_order_id,)

    controller.cancel(entry.client_order_id)
    replacement = signal(paper_target, 2)
    remaining, = controller.account().open_orders
    assert (remaining.intent.client_order_id, remaining.intent.quantity) == (replacement.client_order_id, D(60))
    assert replacement.client_order_id != entry.client_order_id
    controller.fill(replacement.client_order_id, D(60))
    assert controller.account().positions[0].quantity == D(100)


def test_target_can_submit_again_after_unfilled_buy_is_canceled(paper_target):
    controller = paper_target.controller
    entry = signal(paper_target, 0)
    assert controller.cancel(entry.client_order_id).status is OrderStatus.CANCELED
    assert not controller.account().positions

    replacement = signal(paper_target, 1)
    working, = controller.account().open_orders
    assert working.intent.quantity == D(100)
    assert working.intent.client_order_id == replacement.client_order_id != entry.client_order_id


def test_restarted_target_waits_for_unknown_order_and_original_reconciliation(tmp_path):
    path = tmp_path / 'account.db'
    strategy = FixedTarget()
    with closing(PaperStore(path)) as store:
        controller = paper_controller(store)
        entry = signal(ResearchTargetRuntime(target_journal(strategy, controller)), 0)

    with closing(PaperStore(path)) as store:
        controller = paper_controller(store)
        runner = ResearchTargetRuntime(target_journal(strategy, controller))
        working, = controller.account().open_orders
        assert working.status is OrderStatus.UNKNOWN
        assert working.intent.client_order_id == entry.client_order_id
        assert signal(runner, 1) is None

        reconciled = controller.reconcile()
        assert reconciled.reconciled
        confirmed, = reconciled.open_orders
        assert confirmed.status is OrderStatus.SUBMITTED
        assert confirmed.intent.client_order_id == entry.client_order_id
        assert signal(runner, 2) is None

        controller.fill(entry.client_order_id, D(100))
        assert signal(runner, 3) is None
        assert controller.account().positions[0].quantity == D(100)
        assert [(event.client_order_id, event.status) for _, event in store.order_events()] == [
            (entry.client_order_id, OrderStatus.SUBMITTED), (entry.client_order_id, OrderStatus.FILLED),
        ]


def test_zero_target_waits_for_partial_buy_then_reduces_confirmed_inventory(paper_target):
    controller = paper_target.controller
    entry = signal(paper_target, 0)
    controller.fill(entry.client_order_id, D(40))
    assert signal(paper_target, 1, flat=True) is None
    assert tuple(controller.broker.state.orders) == (entry.client_order_id,)

    controller.fill(entry.client_order_id, D(60))
    exit_order = signal(paper_target, 2, flat=True)
    working, = controller.account().open_orders
    assert working.intent.side is Side.SELL and working.intent.reduce_only
    assert working.intent.quantity == D(100)
    controller.fill(exit_order.client_order_id, D(100))
    assert not controller.account().positions


def test_same_target_defers_during_a_working_order_in_research_and_demo():
    strategy = FixedTarget(weight=D('.1002'), interval=1)
    fake = DemoFixture()
    fake.income = []
    adapter, _, demo_controller, _ = setup(fake=fake)
    with closing(adapter.store), closing(demo_controller.store), closing(PaperStore(':memory:')) as store:
        paper = paper_controller(store, fee_rate=D('.001'))
        research = ResearchTargetRuntime(target_journal(strategy, paper))
        demo = DemoStrategyRuntime(demo_controller, SignalAdapter(target_journal(strategy, demo_controller)))
        assert demo.recover().reconciled
        paper_entry = signal(research, 0, price=D(100), interval=1)
        demo_entry = signal(demo, 0, price=D(100), interval=1)
        assert paper.account().open_orders[0].intent.quantity == D(1)
        assert demo_controller.account().open_orders[0].intent.quantity == D(1)
        assert signal(research, 1, price=D(100), interval=1) is None
        assert signal(demo, 1, price=D(100), interval=1) is None

        paper.fill(paper_entry.client_order_id, D(1))
        fake.trade_update(demo_entry.client_order_id)
        assert demo.recover().reconciled
        assert paper.account().positions[0].quantity == demo_controller.account().positions[0].quantity == D(1)
        assert tuple(paper.broker.state.orders) == (paper_entry.client_order_id,)
        assert tuple(fake.orders) == (demo_entry.client_order_id,)
