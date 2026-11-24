"""Grid cancellations distinguish local proposals from submitted orders."""
from contextlib import closing
from datetime import timedelta
from decimal import Decimal as D
import json

import pytest

from factorlab.integrations.binance_futures_trading.demo_cli import open_session
from factorlab.integrations.binance_futures_trading.demo_runtime import DemoRuntime
from factorlab.integrations.binance_futures_trading.demo_session import DemoSession
from factorlab.integrations.binance_futures_trading.unified_strategy import strategy_runtime
from factorlab.strategies.loader import LoadedStrategy
from factorlab.trading.contracts import Market, OrderStatus
from factorlab.trading.control import RiskLimits, TradingController
from factorlab.trading.paper import Instrument, PaperBroker, Quote
from factorlab.trading.storage import PaperStore, decode
from test_binance_demo_network import wired
from test_binance_demo_strategy_session import AT, deployment
from test_unified_strategy import ROOT, bar


@pytest.fixture
def grid_example():
    return LoadedStrategy(ROOT / 'examples/protocol_strategy/grid.json', trusted=True)


def paper_runner(store, strategy, clock):
    broker = PaperBroker(Market.BINANCE_USDM, 'grid-research',
                         (Instrument('NVDAUSDT', 'USDT'),), {'USDT': D(1000)})
    controller = TradingController(broker, store, clock=clock, limits=RiskLimits(D(500), D(500), D(800), 20))
    return strategy_runtime(controller, strategy=strategy, interval_seconds=60,
                            deployment_id='paper-grid', client_id_prefix='paper-grid-')


def demo_runner(session, expect, campaign, strategy):
    return strategy_runtime(session, strategy=strategy, interval_seconds=60,
                            deployment_id=campaign['prefix'] + '-grid',
                            client_id_prefix=campaign['prefix'] + '-', expect=expect)


BREAKOUT = Quote('NVDAUSDT', D(160), AT)
NEXT_QUOTE = Quote('NVDAUSDT', D(150), AT + timedelta(seconds=1))


def commands(runner):
    return decode(json.loads(runner.journal._latest()['commands']))


def assert_local_withdrawal(runner, before_quote):
    assert runner.recover().reconciled
    if before_quote:
        assert runner.closed_bar(bar(21, '150'))
    assert runner.quote(BREAKOUT) is None
    saved = commands(runner)
    assert [command['kind'] for command in saved] == (
        ['closed_bar', 'quote', 'withdraw'] if before_quote else ['quote', 'withdraw'])
    assert saved[-1]['value'] == [runner.journal.deployment_id + '-00000001']
    assert not runner.controller.broker.state.orders
    assert list(runner.controller.store.order_events()) == []
    return saved


def assert_persisted_withdrawal(runner, saved):
    assert runner.recover().reconciled
    assert runner.quote(BREAKOUT) is None
    assert commands(runner) == saved
    assert runner.quote(NEXT_QUOTE) is None
    replayed = commands(runner)
    assert replayed[:-1] == saved and replayed[-1]['kind'] == 'quote'
    assert not runner.controller.broker.state.orders
    assert list(runner.controller.store.order_events()) == []


@pytest.mark.parametrize('before_quote', [False, True])
def test_grid_withdraws_local_breakout_proposal_in_paper(tmp_path, grid_example, before_quote):
    path = tmp_path / 'paper.sqlite3'
    with closing(PaperStore(path)) as store:
        runner = paper_runner(store, grid_example, lambda: AT)
        saved = assert_local_withdrawal(runner, before_quote)
    with closing(PaperStore(path)) as store:
        runner = paper_runner(store, grid_example, lambda: NEXT_QUOTE.observed_at)
        assert_persisted_withdrawal(runner, saved)


@pytest.mark.parametrize('before_quote', [False, True])
def test_grid_withdraws_local_breakout_proposal_in_demo(deployment, grid_example, monkeypatch, before_quote):
    root, credentials, campaign, exchange = deployment
    counts = dict(GET=0, POST=0, DELETE=0)
    with closing(DemoRuntime(root)) as runtime:
        with open_session(root, credentials, runtime, counts, campaign=campaign) as (session, expect):
            saved = assert_local_withdrawal(demo_runner(session, expect, campaign, grid_example), before_quote)
    monkeypatch.setattr(DemoSession, '_now', lambda _: NEXT_QUOTE.observed_at)
    with closing(DemoRuntime(root)) as runtime:
        with open_session(root, credentials, runtime, counts, campaign=campaign) as (session, expect):
            assert_persisted_withdrawal(demo_runner(session, expect, campaign, grid_example), saved)
    assert counts['POST'] == counts['DELETE'] == 0
    assert not exchange.orders


def test_grid_cancels_submitted_partial_on_breakout_before_confirmation_delivery(tmp_path, grid_example):
    path = tmp_path / 'paper.sqlite3'
    with closing(PaperStore(path)) as store:
        runner = paper_runner(store, grid_example, lambda: AT)
        assert runner.recover().reconciled
        entry = runner.quote(Quote('NVDAUSDT', D(150), AT - timedelta(seconds=1)))
        assert entry.status is OrderStatus.SUBMITTED
        key = entry.client_order_id
        partial = runner.controller.fill(key, D('.02'), D(145))
        assert partial.status is OrderStatus.PARTIALLY_FILLED
        # The confirmed fill is in the controller, while Grid still sees its NEW proposal.
        assert [command['kind'] for command in commands(runner)] == ['quote']

        runner.quote(BREAKOUT)
        terminal = runner.controller.broker.state.orders[key].snapshot
        assert terminal.status is OrderStatus.CANCELED and terminal.cumulative_filled_quantity == D('.02')
        assert runner.controller.account().positions[0].quantity == D('.02')
        assert [command['kind'] for command in commands(runner)] == ['quote', 'quote']
        events = list(store.order_events())

    with closing(PaperStore(path)) as store:
        runner = paper_runner(store, grid_example, lambda: NEXT_QUOTE.observed_at)
        assert runner.recover().reconciled
        assert runner.quote(BREAKOUT) is None
        assert runner.quote(NEXT_QUOTE) is None
        assert list(store.order_events()) == events
        account = runner.controller.account()
        assert account.positions[0].quantity == D('.02') and not account.open_orders
