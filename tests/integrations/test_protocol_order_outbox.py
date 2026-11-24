"""Orders offered once survive Demo admission and keep their original lifetime."""
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal as D
import json

import pytest

from factorlab.integrations.binance.client import Response
from factorlab.integrations.binance_futures_trading.demo_cli import open_session
from factorlab.integrations.binance_futures_trading.demo_preflight import BOOK
from factorlab.integrations.binance_futures_trading.demo_runtime import DemoRuntime
from factorlab.integrations.binance_futures_trading.demo_session import DemoSession
from factorlab.integrations.binance_futures_trading.unified_strategy import strategy_runtime
from factorlab.trading.contracts import OrderStatus
from factorlab.trading.paper import Quote
from factorlab.trading.storage import PaperStore, decode
from test_binance_demo_network import NetworkFixture, wired
from test_binance_demo_strategy_session import AT, deployment
from test_protocol_research_outstanding import paper_controller
from test_unified_strategy import manifest


@pytest.fixture
def batch_strategy(tmp_path):
    return manifest(tmp_path / 'batch', '''
from decimal import Decimal as D

from factorlab.strategies.protocol import Decision, StrategySpec
from factorlab.trading.contracts import OrderIntent, OrderType, Side


class OfferBatchOnce:
    def describe(self):
        return StrategySpec('offer_batch_once', '1', 'orders', ['NVDAUSDT'], [60], ['quote'])

    def decide(self, event, state):
        prefix = event['deployment_id']
        quote = event['quote']
        if state.get('offered'):
            if quote.price == D(101):
                return Decision(cancellations=(prefix + '-second',), state=state)
            return Decision(state=state)
        market = event['account']['market']
        first = OrderIntent(market, quote.instrument_id, Side.BUY, OrderType.LIMIT,
            D('.02'), D(99), prefix + '-first', position_side='BOTH')
        second = OrderIntent(market, quote.instrument_id, Side.BUY, OrderType.LIMIT,
            D('.02'), D(98), prefix + '-second', position_side='BOTH')
        return Decision(orders=(first, second), state={'offered': True})
''', 'OfferBatchOnce')


@dataclass
class QuoteClock:
    at: datetime = AT

    def quote(self, price=D(100)):
        return Quote('NVDAUSDT', price, self.at)


@pytest.fixture
def demo_clock(deployment, monkeypatch):
    clock = QuoteClock()
    dispatch = NetworkFixture.__call__

    def market(self, request, timeout):
        if request.path == BOOK:
            book = dict(symbol='NVDAUSDT', bidPrice='100', askPrice='100',
                time=round(clock.at.timestamp() * 1000))
            return Response(200, {}, json.dumps(book).encode())
        return dispatch(self, request, timeout)

    monkeypatch.setattr(NetworkFixture, '__call__', market)
    monkeypatch.setattr(DemoSession, '_now', lambda _: clock.at)
    return clock


@contextmanager
def open_batch(deployment, strategy, counts, *, max_quantity=None):
    root, store, campaign, _ = deployment
    prefix = campaign['prefix'] + '-'
    with closing(DemoRuntime(root)) as runtime:
        with open_session(root, store, runtime, counts, campaign=campaign) as (session, expect):
            runner = strategy_runtime(session, strategy=strategy, interval_seconds=60,
                deployment_id=prefix + 'batch', client_id_prefix=prefix, expect=expect,
                max_quantity=max_quantity)
            assert runner.recover().reconciled
            yield session, runner, runtime


def order_writes(exchange):
    id_param = {'POST': 'newClientOrderId', 'DELETE': 'origClientOrderId'}
    return [(method, params[id_param[method]][0]) for method, _, params in exchange.calls if method in id_param]


def outcome(runner, key):
    raw = runner.controller.store.event_payload(runner.execution.prefix + key + ':outcome')
    return decode(json.loads(raw))


@pytest.mark.parametrize('terminal_status', [OrderStatus.FILLED, OrderStatus.CANCELED])
def test_demo_deferred_order_survives_partial_fill_and_restart(deployment, demo_clock, batch_strategy,
                                                             terminal_status):
    _, _, campaign, exchange = deployment
    counts = dict(GET=0, POST=0, DELETE=0)
    with open_batch(deployment, batch_strategy, counts) as (session, runner, runtime):
        first = runner.journal.deployment_id + '-first'
        second = runner.journal.deployment_id + '-second'
        opening = demo_clock.quote()
        assert runner.quote(opening).intent.client_order_id == first
        assert exchange.orders[first]['status'] == 'NEW'
        assert order_writes(exchange) == [('POST', first)]

        exchange.trade_update(first, '.01', '99')
        assert runner.recover().reconciled
        assert session.adapter.order(first).status is OrderStatus.PARTIALLY_FILLED
        demo_clock.at += timedelta(seconds=1)
        working = demo_clock.quote()
        assert runner.quote(working) is None
        assert counts['POST'] == runtime.post_count(campaign['id']) == 1
        offer, = runner.gateway.state.runtime['pending']['orders']
        assert offer['intent'].client_order_id == second and offer['status'] == 'DEFERRED'
        assert offer['expires_at'] == opening.observed_at + timedelta(seconds=60)
        runner.disconnect()
        assert runner.quote(working) is None

    with open_batch(deployment, batch_strategy, counts) as (session, runner, runtime):
        assert runner.quote(working) is None
        assert order_writes(exchange) == [('POST', first)]
        if terminal_status is OrderStatus.FILLED:
            exchange.trade_update(first, '.01', '99')
        else:
            assert session.cancel(first).status is OrderStatus.CANCELED
        assert runner.recover().reconciled
        assert session.adapter.order(first).status is terminal_status

        demo_clock.at += timedelta(seconds=1)
        trigger = demo_clock.quote()
        submitted = runner.quote(trigger)
        assert submitted.status is OrderStatus.SUBMITTED
        assert submitted.intent.client_order_id == second
        assert (submitted.intent.quantity, submitted.intent.limit_price) == (D('.02'), D(98))
        assert runner.quote(trigger) is None
        assert runner.journal._latest() == {'offered': True}
        assert counts['POST'] == runtime.post_count(campaign['id']) == 2
    expected = [('POST', first), ('POST', second)]
    if terminal_status is OrderStatus.CANCELED:
        expected.insert(1, ('DELETE', first))
    assert order_writes(exchange) == expected


def test_demo_unknown_first_order_does_not_release_deferred_batch(deployment, demo_clock, batch_strategy):
    _, _, campaign, exchange = deployment
    counts = dict(GET=0, POST=0, DELETE=0)
    with open_batch(deployment, batch_strategy, counts) as (session, runner, runtime):
        first = runner.journal.deployment_id + '-first'
        exchange.timeout_next = True
        submitted = runner.quote(demo_clock.quote())
        assert submitted.status is OrderStatus.UNKNOWN and submitted.intent.client_order_id == first
        assert not session.controller.account().reconciled
        demo_clock.at += timedelta(seconds=1)
        assert runner.quote(demo_clock.quote()) is None
        assert runtime.post_count(campaign['id']) == 1

    recovery_start = len(exchange.calls)
    with open_batch(deployment, batch_strategy, counts) as (session, runner, runtime):
        assert session.adapter.order(first).status is OrderStatus.SUBMITTED
        assert runner.quote(demo_clock.quote()) is None
        assert runtime.post_count(campaign['id']) == 1
    assert any(method == 'GET' and path == '/fapi/v1/order' and params['origClientOrderId'] == [first]
        for method, path, params in exchange.calls[recovery_start:])
    assert order_writes(exchange) == [('POST', first)]


def test_demo_deferred_order_expires_from_original_offer_even_after_replay(deployment, demo_clock, batch_strategy):
    _, _, _, exchange = deployment
    counts = dict(GET=0, POST=0, DELETE=0)
    with open_batch(deployment, batch_strategy, counts) as (session, runner, _):
        first = runner.journal.deployment_id + '-first'
        opening = demo_clock.quote()
        runner.quote(opening)
        demo_clock.at += timedelta(seconds=4)
        assert runner.quote(opening) is None
        demo_clock.at = opening.observed_at + timedelta(seconds=59)
        assert runner.quote(demo_clock.quote()) is None
        exchange.trade_update(first, '.02', '99')
        assert runner.recover().reconciled
        assert session.adapter.order(first).status is OrderStatus.FILLED

        demo_clock.at += timedelta(seconds=1)
        deadline = demo_clock.quote()
        assert runner.quote(deadline) is None
        assert runner.gateway.state.runtime['pending'] is None
        assert outcome(runner, runner.journal.deployment_id + '-second')['status'] == 'EXPIRED'
        assert runner.quote(deadline) is None
    assert order_writes(exchange) == [('POST', first)]


def test_demo_deferred_cancel_is_local_and_survives_pause_and_restart(deployment, demo_clock, batch_strategy):
    _, _, _, exchange = deployment
    counts = dict(GET=0, POST=0, DELETE=0)
    with open_batch(deployment, batch_strategy, counts) as (_, runner, _):
        first = runner.journal.deployment_id + '-first'
        runner.quote(demo_clock.quote())
        demo_clock.at += timedelta(seconds=1)
        withdraw = demo_clock.quote(D(101))
        assert runner.quote(withdraw) is None
        assert runner.gateway.state.runtime['pending'] is None
        assert outcome(runner, runner.journal.deployment_id + '-second')['status'] == 'WITHDRAWN'
        assert order_writes(exchange) == [('POST', first)]
        runner.pause()
        assert runner.resume().reconciled
        assert runner.quote(withdraw) is None
        exchange.trade_update(first, '.02', '99')

    with open_batch(deployment, batch_strategy, counts) as (session, runner, _):
        assert session.adapter.order(first).status is OrderStatus.FILLED
        assert runner.quote(withdraw) is None
        demo_clock.at += timedelta(seconds=1)
        assert runner.quote(demo_clock.quote()) is None
        assert runner.gateway.state.runtime['pending'] is None
    assert order_writes(exchange) == [('POST', first)]


def test_demo_deferred_order_rechecks_budget_and_retires_rejected_intent(deployment, demo_clock, batch_strategy):
    _, _, _, exchange = deployment
    counts = dict(GET=0, POST=0, DELETE=0)
    with open_batch(deployment, batch_strategy, counts, max_quantity=D('.03')) as (_, runner, _):
        first = runner.journal.deployment_id + '-first'
        second = runner.journal.deployment_id + '-second'
        runner.quote(demo_clock.quote())
        exchange.trade_update(first, '.02', '99')
        assert runner.recover().reconciled

        demo_clock.at += timedelta(seconds=1)
        trigger = demo_clock.quote()
        with pytest.raises(ValueError, match='execution budget'):
            runner.quote(trigger)
        receipt = outcome(runner, second)
        assert receipt['status'] == 'RISK_REJECTED' and receipt['reason'] == 'EXECUTION_BUDGET'
        assert receipt['intent'].client_order_id == second
        assert runner.quote(trigger) is None
        assert runner.gateway.state.runtime['pending'] is None
    assert order_writes(exchange) == [('POST', first)]


def test_paper_submits_both_offers_once_under_existing_admission(batch_strategy):
    with closing(PaperStore(':memory:')) as store:
        controller = paper_controller(store)
        runner = strategy_runtime(controller, strategy=batch_strategy, interval_seconds=60,
            deployment_id='batch', client_id_prefix='batch-')
        assert runner.recover().reconciled
        opening = Quote('NVDAUSDT', D(100), controller.clock())
        runner.quote(opening)
        assert [order.intent.client_order_id for order in controller.account().open_orders] == [
            'batch-first', 'batch-second',
        ]
        assert runner.quote(opening) is None
        assert [(event.client_order_id, event.status) for _, event in store.order_events()] == [
            ('batch-first', OrderStatus.SUBMITTED), ('batch-second', OrderStatus.SUBMITTED),
        ]
