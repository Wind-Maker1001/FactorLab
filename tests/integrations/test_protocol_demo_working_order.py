"""Resting order management through the persistent offline Demo session."""
from contextlib import closing
from datetime import timedelta
from decimal import Decimal as D

import pytest

from factorlab.integrations.binance_futures_trading.demo_cli import open_session
from factorlab.integrations.binance_futures_trading.demo_runtime import DemoRuntime
from factorlab.integrations.binance_futures_trading.demo_session import DemoSession
from factorlab.integrations.binance_futures_trading.protocol import SafetyBlocked
from factorlab.integrations.binance_futures_trading.unified_strategy import strategy_runtime
from factorlab.trading.contracts import OrderStatus, Side
from factorlab.trading.paper import Quote
from test_binance_demo_network import NetworkFixture, wired
from test_binance_demo_strategy_session import AT, deployment
from test_unified_strategy import manifest


@pytest.fixture
def ttl_strategy(tmp_path):
    return manifest(tmp_path / 'ttl', '''
from datetime import datetime, timedelta
from decimal import Decimal as D
from factorlab.strategies.protocol import Decision, StrategySpec
from factorlab.trading.contracts import OrderIntent, OrderType, Side


class RestingLimit:
    def describe(self):
        return StrategySpec('resting_limit', '1', 'orders', ['NVDAUSDT'], [60],
                            ['quote', 'order_event'])

    def decide(self, event, state):
        if event['kind'] != 'quote':
            return Decision(state=state)
        quote = event['quote']
        count = state.get('quotes', 0) + 1
        opened = state.get('opened_at', quote.observed_at.isoformat())
        state = {'quotes': count, 'opened_at': opened, 'inventory': str(event['account']['inventory'])}
        entry = event['deployment_id'] + '-entry'
        if quote.observed_at >= datetime.fromisoformat(opened) + timedelta(seconds=3):
            return Decision(cancellations=(entry, event['deployment_id'] + '-extra'), state=state)
        identity = entry if count == 1 else event['deployment_id'] + '-extra'
        quantity = D('.04') if count == 1 else D('.01')
        intent = OrderIntent(event['account']['market'], quote.instrument_id, Side.BUY,
                             OrderType.LIMIT, quantity, D(99), identity, position_side='BOTH')
        return Decision(orders=(intent,), state=state)
''', 'RestingLimit')


def runner_for(session, expect, campaign, strategy):
    deployment_id = campaign['prefix'] + '-ttl'
    return strategy_runtime(session, strategy=strategy, interval_seconds=60, deployment_id=deployment_id,
                            client_id_prefix=campaign['prefix'] + '-', expect=expect)


@pytest.mark.parametrize('lost_cancel_reply', [False, True])
def test_resting_quotes_and_ttl_cancel_survive_restart(deployment, ttl_strategy, monkeypatch, lost_cancel_reply):
    root, store, campaign, exchange = deployment
    counts = dict(GET=0, POST=0, DELETE=0)
    at = AT
    monkeypatch.setattr(DemoSession, '_now', lambda _: at)
    if lost_cancel_reply:
        dispatch = NetworkFixture.__call__

        def lose_cancel_reply(self, request, timeout):
            response = dispatch(self, request, timeout)
            if request.method == 'DELETE':
                raise TimeoutError('synthetic cancellation reply lost')
            return response

        monkeypatch.setattr(NetworkFixture, '__call__', lose_cancel_reply)

    with closing(DemoRuntime(root)) as runtime:
        with open_session(root, store, runtime, counts, campaign=campaign) as (session, expect):
            runner = runner_for(session, expect, campaign, ttl_strategy)
            assert runner.recover().reconciled
            opened = runner.quote(Quote('NVDAUSDT', D(100), at))
            key = opened.intent.client_order_id
            assert opened.status is OrderStatus.SUBMITTED and exchange.orders[key]['status'] == 'NEW'

            at += timedelta(seconds=1)
            assert runner.quote(Quote('NVDAUSDT', D(100), at)) is None
            assert runner.journal._latest()['quotes'] == 2
            assert counts['POST'] == 1 and counts['DELETE'] == 0

            exchange.trade_update(key, '.01', '99')
            assert runner.recover().reconciled
            partial = session.adapter.order(key)
            assert partial.status is OrderStatus.PARTIALLY_FILLED
            assert partial.cumulative_filled_quantity == D('.01')

            at += timedelta(seconds=1)
            assert runner.quote(Quote('NVDAUSDT', D(100), at)) is None
            state = runner.journal._latest()
            assert state['quotes'] == 3 and D(state['inventory']) == D('.01')
            assert counts['POST'] == 1 and counts['DELETE'] == 0

            at += timedelta(seconds=1)
            expiry = Quote('NVDAUSDT', D(100), at)
            canceled = runner.quote(expiry)
            assert canceled.status is (OrderStatus.UNKNOWN if lost_cancel_reply else OrderStatus.CANCELED)
            assert runner.journal._latest()['quotes'] == 4
            assert exchange.orders[key]['status'] == 'CANCELED'
            assert counts['POST'] == runtime.post_count(campaign['id']) == 1 and counts['DELETE'] == 1

    before = len(exchange.calls)
    with closing(DemoRuntime(root)) as runtime:
        with open_session(root, store, runtime, counts, campaign=campaign) as (session, expect):
            runner = runner_for(session, expect, campaign, ttl_strategy)
            account = runner.recover()
            assert account.reconciled and not account.open_orders
            assert account.positions[0].quantity == D('.01')
            terminal = session.adapter.order(key)
            assert terminal.status is OrderStatus.CANCELED
            assert terminal.cumulative_filled_quantity == partial.cumulative_filled_quantity
            assert terminal.cumulative_fee == partial.cumulative_fee
            assert runner.quote(expiry) is None
            assert runner.journal._latest()['quotes'] == 4
            assert counts['POST'] == runtime.post_count(campaign['id']) == 1 and counts['DELETE'] == 1
    assert all(method == 'GET' for method, _, _ in exchange.calls[before:])
    assert [params['origClientOrderId'] for method, _, params in exchange.calls if method == 'DELETE'] == [[key]]


def test_unknown_submission_recovers_the_original_order_without_replacement(deployment, ttl_strategy, monkeypatch):
    root, store, campaign, exchange = deployment
    counts = dict(GET=0, POST=0, DELETE=0)
    at = AT
    monkeypatch.setattr(DemoSession, '_now', lambda _: at)
    first = Quote('NVDAUSDT', D(100), at)
    with closing(DemoRuntime(root)) as runtime:
        with open_session(root, store, runtime, counts, campaign=campaign) as (session, expect):
            runner = runner_for(session, expect, campaign, ttl_strategy)
            assert runner.recover().reconciled
            exchange.timeout_next = True
            order = runner.quote(first)
            key = order.intent.client_order_id
            assert order.status is OrderStatus.UNKNOWN and order.reconcile_required
            assert not session.controller.account().reconciled
            at += timedelta(seconds=1)
            later = Quote('NVDAUSDT', D(100), at)
            assert runner.quote(later) is None
            assert runner.journal._latest()['quotes'] == 1
            assert counts['POST'] == runtime.post_count(campaign['id']) == 1 and counts['DELETE'] == 0

    before = len(exchange.calls)
    with closing(DemoRuntime(root)) as runtime:
        with open_session(root, store, runtime, counts, campaign=campaign) as (session, expect):
            runner = runner_for(session, expect, campaign, ttl_strategy)
            assert runner.recover().reconciled
            assert session.adapter.order(key).status is OrderStatus.SUBMITTED
            assert runner.quote(first) is None
            assert runner.quote(later) is None
            assert runner.journal._latest()['quotes'] == 2
            assert counts['POST'] == runtime.post_count(campaign['id']) == 1 and counts['DELETE'] == 0
    assert list(exchange.orders) == [key]
    assert any(method == 'GET' and path == '/fapi/v1/order' and params['origClientOrderId'] == [key]
               for method, path, params in exchange.calls[before:])


@pytest.mark.parametrize(('gate', 'reason'), [
    ('stale_book', 'STALE_QUOTE'),
    ('exhausted_campaign', 'plan total order limit|budget exhausted'),
    ('no_authorization', 'active Demo runtime authorization required'),
])
def test_new_intent_still_requires_demo_submission_admission(deployment, ttl_strategy, monkeypatch, gate, reason):
    root, store, campaign, exchange = deployment
    counts = dict(GET=0, POST=0, DELETE=0)
    at = AT
    monkeypatch.setattr(DemoSession, '_now', lambda _: at)
    with closing(DemoRuntime(root)) as runtime:
        authority = None if gate == 'no_authorization' else campaign
        with open_session(root, store, runtime, counts, campaign=authority) as (session, expect):
            runner = runner_for(session, expect, campaign, ttl_strategy)
            assert runner.recover().reconciled
            if gate == 'exhausted_campaign':
                for i in range(campaign['orders']):
                    key = f"{campaign['prefix']}-prior-{i}"
                    expect(key, Side.BUY, D('.04'))
                    assert session.submit_limit(key, Side.BUY, D('.04')).status is OrderStatus.SUBMITTED
                    assert session.cancel(key).status is OrderStatus.CANCELED
                assert counts['POST'] == campaign['orders']
            elif gate == 'stale_book':
                at += timedelta(seconds=6)

            writes = counts['POST'], counts['DELETE']
            with pytest.raises(SafetyBlocked, match=reason):
                runner.quote(Quote('NVDAUSDT', D(100), at))
            assert runner.journal._latest()['quotes'] == 1
            assert (counts['POST'], counts['DELETE']) == writes
            assert runtime.post_count(campaign['id']) == writes[0]
            assert runner.journal.deployment_id + '-entry' not in exchange.orders
