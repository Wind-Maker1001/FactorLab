from contextlib import closing
from dataclasses import replace
from decimal import Decimal as D
import json
from urllib.parse import parse_qs, urlsplit

import pytest

from test_binance_demo_network import NetworkFixture, wired
from test_binance_demo_runtime import MemoryCredentialStore
from factorlab.integrations.binance.client import Response
from factorlab.integrations.binance_futures_trading.demo_runtime import (
    DemoRuntime, bind_account, now, open_grid_session,
)
from factorlab.integrations.binance_futures_trading.demo_session import (
    demo_account_configuration, observe_demo_account,
)
from factorlab.integrations.binance_futures_trading.fake import MS, NOW
from factorlab.integrations.binance_futures_trading.protocol import Credentials, Environment, SafetyBlocked
from factorlab.strategies.grid import GridConfig
from factorlab.strategies.grid_execution import GridExecution, GridRules
from factorlab.trading import Market, OrderIntent, OrderType, Side, OrderStatus as S
from factorlab.trading.control import RiskRejected
from factorlab.trading.paper import Quote


@pytest.fixture
def network_grid(tmp_path, monkeypatch, wired):
    original = NetworkFixture.__call__

    def market(self, request, timeout):
        if request.path.endswith('bookTicker'):
            return Response(200, {}, b'{"symbol":"NVDAUSDT","bidPrice":"150","askPrice":"150","time":%d}' % MS)
        if request.path.endswith('premiumIndex'):
            return Response(200, {}, b'{"symbol":"NVDAUSDT","markPrice":"150","time":%d}' % MS)
        response = original(self, request, timeout)
        if request.path.endswith('exchangeInfo'):
            payload = json.loads(response.body)
            payload['symbols'][0]['timeInForce'] = ['GTC']
            return Response(200, {}, json.dumps(payload).encode())
        if request.path == '/fapi/v3/positionRisk':
            payload = json.loads(response.body)
            for position in payload:
                position['positionAmt'] = str(self.quantity)
                position['entryPrice'] = str(self.entry)
            return Response(200, {}, json.dumps(payload).encode())
        if request.path == '/fapi/v3/account':
            payload = json.loads(response.body)
            for position in payload['positions']:
                del position['entryPrice']
            return Response(200, {}, json.dumps(payload).encode())
        return response

    monkeypatch.setattr(NetworkFixture, '__call__', market)
    store = MemoryCredentialStore()
    credential = Credentials('demo-key', 'demo-secret', Environment.DEMO)
    facts, _ = observe_demo_account(credential)
    binding = bind_account(store, credential, facts, demo_account_configuration(facts))

    def enable(orders=3):
        with closing(DemoRuntime(tmp_path)) as runtime:
            return runtime.start(binding, seconds=900, orders=orders, max_loss=D('1'),
                opening_equity=D('1000'), stop_epoch=runtime.stop_epoch(), started_at=now())

    def factory(session, deployment_id, **config):
        return GridExecution(GridConfig(lower=146, upper=154, levels=1, initial_price=150,
            initial_inventory=0, quantity=1, max_inventory=1, max_cash_committed=7,
            breakout=config.pop('breakout', 'pause'), **config), GridRules(D('.04'), D('.01'), D('.01'), D('.01'), D('5')),
            deployment_id=deployment_id, account_id=session.adapter.account_id,
            market=Market.BINANCE_USDM, instrument_id='NVDAUSDT', initial_equity=D('1000'))

    return tmp_path, store, factory, enable, wired[0], wired[1]


def observe(bridge, **changes):
    return bridge.observe(dict(id='quote-1', time='2026-11-11T12:00:00', price='150',
        volume=1, **changes))


def writes(requests, method='POST'):
    return [parse_qs(urlsplit(request.full_url).query) for request in requests
            if request.get_method() == method]


def test_original_limit_and_confirmed_reduce_only_round_trip(network_grid):
    root, store, factory, enable, fake, requests = network_grid
    enable()
    with open_grid_session(root, store, factory) as bridge:
        bridge.recover()
        assert observe(bridge).status is S.SUBMITTED
        buy = bridge.grid.active[0].intent
        post = writes(requests)[0]
        assert D(post['price'][0]) == buy.limit_price == D('146')
        assert post['symbol'] == ['NVDAUSDT'] and post['side'] == ['BUY']
        assert post['type'] == ['LIMIT'] and post['timeInForce'] == ['GTC']
        assert post['quantity'] == ['0.04'] and post['reduceOnly'] == ['false']
        assert post['positionSide'] == ['BOTH']
        assert bridge.dispatch() is None
        fake.trade_update(buy.client_order_id, '.04', '146')
        bridge.recover()
        assert bridge.account().inventory == D('.04')
        assert bridge.dispatch().status is S.SUBMITTED
        sell = bridge.grid.active[0].intent
        assert sell.reduce_only and sell.limit_price == D('154')
        sell_post = writes(requests)[1]
        assert sell_post['symbol'] == ['NVDAUSDT'] and sell_post['side'] == ['SELL']
        assert sell_post['reduceOnly'] == ['true'] and sell_post['positionSide'] == ['BOTH']
        assert sell_post['type'] == ['LIMIT'] and sell_post['timeInForce'] == ['GTC']
        assert sell_post['quantity'] == ['0.04']
        assert D(sell_post['price'][0]) == sell.limit_price
        fake.trade_update(sell.client_order_id, '.04', '154')
        bridge.recover()
        assert bridge.account().inventory == 0
        turnover = bridge.account().turnover
        bridge.recover()
        assert bridge.account().turnover == turnover == D('12')
        assert sum(o['fee_usdt'] for o in bridge.session.status()['orders'].values()) == D('.012')
    with open_grid_session(root, store, factory) as bridge:
        bridge.recover()
        assert bridge.account().inventory == 0
        assert bridge.account().turnover == D('12')
    assert len(writes(requests)) == 2


def test_partial_fill_remainder_and_no_duplicate_after_restart(network_grid):
    root, store, factory, enable, fake, requests = network_grid
    enable()
    with open_grid_session(root, store, factory) as bridge:
        bridge.recover()
        observe(bridge)
        key = bridge.grid.active[0].intent.client_order_id
        fake.trade_update(key, '.02', '146')
        bridge.recover()
        assert bridge.account().inventory == D('.02')
        assert bridge.grid.reverse_remainders == {'SELL:1': D('.02')}
        assert bridge.dispatch() is None
    with open_grid_session(root, store, factory) as bridge:
        bridge.recover()
        assert bridge.dispatch() is None
        assert len(writes(requests)) == 1
        fake.trade_update(key, '.02', '146')
        bridge.recover()
        assert not bridge.grid.reverse_remainders
        assert bridge.dispatch().status is S.SUBMITTED
        assert bridge.grid.active[0].intent.quantity == D('.04')
    with open_grid_session(root, store, factory) as bridge:
        bridge.recover()
        assert bridge.dispatch() is None
        assert bridge.account().inventory == D('.04')
    assert len(writes(requests)) == 2


def test_cancel_over_budget_after_post_budget_consumed(network_grid):
    root, store, factory, enable, fake, requests = network_grid
    campaign = enable(orders=1)
    with open_grid_session(root, store, factory) as bridge:
        bridge.recover()
        observe(bridge)
        key = bridge.grid.active[0].intent.client_order_id
        bridge.controller.quote(Quote('NVDAUSDT', D('180'), NOW))
        assert bridge.account().committed == D('7.2')
        bridge.observe(dict(id='breakout', time='2026-11-11T12:00:01', price='180', volume=1))
        assert bridge.gateway.adapter.order(key).status is S.CANCELED
        assert bridge.account().committed == 0
    assert len(writes(requests)) == len(writes(requests, 'DELETE')) == 1
    with closing(DemoRuntime(root)) as runtime:
        assert runtime.post_count(campaign['id']) == 1


def test_unknown_reply_recovery_never_resubmits(network_grid):
    root, store, factory, enable, fake, requests = network_grid
    campaign = enable()
    with open_grid_session(root, store, factory) as bridge:
        bridge.recover()
        fake.timeout_next = True
        unknown = observe(bridge)
        assert unknown.status is S.UNKNOWN and unknown.reconcile_required
        with pytest.raises(SafetyBlocked):
            bridge.dispatch()
        bridge.recover()
        assert bridge.dispatch() is None
        assert len(bridge.grid.active) == 1
        assert not bridge.gateway.adapter.order(unknown.intent.client_order_id).reconcile_required
    with pytest.raises(SafetyBlocked, match='no persistent Demo authorization'):
        with open_grid_session(root, store, factory):
            pytest.fail('uncertain response retained network write authority')
    assert len(writes(requests)) == 1
    with closing(DemoRuntime(root)) as runtime:
        assert runtime.active_campaign() is None
        assert runtime.post_count(campaign['id']) == 1


def test_exhausted_post_budget_blocks_reverse_order_on_restart(network_grid):
    root, store, factory, enable, fake, requests = network_grid
    campaign = enable(orders=1)
    with open_grid_session(root, store, factory) as bridge:
        bridge.recover()
        observe(bridge)
        fake.trade_update(bridge.grid.active[0].intent.client_order_id, '.04', '146')
    with open_grid_session(root, store, factory) as bridge:
        bridge.recover()
        assert bridge.account().inventory == D('.04')
        with pytest.raises(SafetyBlocked, match='plan total order limit|POST budget exhausted'):
            bridge.dispatch()
    assert len(writes(requests)) == 1
    with closing(DemoRuntime(root)) as runtime:
        assert runtime.post_count(campaign['id']) == 1


def test_grid_context_retains_cross_session_operator_lock(network_grid):
    root, store, factory, enable, fake, requests = network_grid
    enable()
    with open_grid_session(root, store, factory):
        with pytest.raises(SafetyBlocked, match='another Demo session is active'):
            with open_grid_session(root, store, factory):
                pytest.fail('second grid session bypassed the operator lock')
    assert not writes(requests)


def test_production_credential_cannot_open_grid_network(network_grid):
    root, store, factory, enable, fake, requests = network_grid
    enable()
    store.credential = Credentials('demo-key', 'demo-secret', Environment.PRODUCTION)
    before = len(requests)
    with pytest.raises(SafetyBlocked, match='Demo credentials required'):
        with open_grid_session(root, store, factory):
            pytest.fail('Production credentials opened a Demo Grid session')
    assert len(requests) == before


@pytest.mark.parametrize(('price', 'quantity', 'reason'), [
    ('146.001', '.04', 'PRICE_FILTER'),
    ('146', '.035', 'QUANTITY_FILTER'),
    ('146', '.03', 'MIN_NOTIONAL'),
    ('145', '.04', 'price band'),
    ('154.51', '.04', 'price band'),
])
def test_original_invalid_limit_is_rejected_not_adjusted(network_grid, price, quantity, reason):
    root, store, factory, enable, fake, requests = network_grid
    enable()
    with open_grid_session(root, store, factory) as bridge:
        bridge.recover()
        intent = OrderIntent(Market.BINANCE_USDM, 'NVDAUSDT', Side.BUY, OrderType.LIMIT,
            D(quantity), D(price), bridge.grid.deployment_id + '-00000001')
        bridge.expect(intent)
        with pytest.raises((SafetyBlocked, RiskRejected), match=reason):
            bridge.session.submit_intent(intent)
    assert not writes(requests)


def test_dispatch_guard_binds_original_price(network_grid):
    root, store, factory, enable, fake, requests = network_grid
    campaign = enable()
    with open_grid_session(root, store, factory) as bridge:
        bridge.recover()
        intent = OrderIntent(Market.BINANCE_USDM, 'NVDAUSDT', Side.BUY, OrderType.LIMIT,
            D('.04'), D('146'), bridge.grid.deployment_id + '-00000001')
        bridge.expect(intent)
        with pytest.raises(SafetyBlocked, match='price|intent'):
            bridge.session.submit_intent(replace(intent, limit_price=D('147')))
    assert not writes(requests)
    with closing(DemoRuntime(root)) as runtime:
        assert runtime.post_count(campaign['id']) == 0


def test_geometric_identity_and_deployment_binding(network_grid):
    root, store, factory, enable, fake, requests = network_grid
    campaign = enable()

    def geometric(session, deployment_id):
        return factory(session, deployment_id, mode='geometric')

    with open_grid_session(root, store, geometric) as bridge:
        bridge.recover()
        observe(bridge)
        key = bridge.grid.active[0].intent.client_order_id
        assert key.startswith(campaign['prefix'] + '-') and len(key) <= 36
    with open_grid_session(root, store, geometric) as bridge:
        bridge.recover()
        assert bridge.grid.active[0].intent.client_order_id == key
        assert bridge.dispatch() is None
    with pytest.raises(SafetyBlocked, match='deployment'):
        with open_grid_session(root, store, factory):
            pytest.fail('different grid configuration adopted the same deployment')
    assert len(writes(requests)) == 1


def test_network_atr_pending_bar_is_replayed_without_submission(network_grid):
    root, store, factory, enable, fake, requests = network_grid
    enable()

    def atr(session, deployment_id):
        return factory(session, deployment_id, mode='atr', atr_window=2, atr_multiplier=1)

    with open_grid_session(root, store, atr) as bridge:
        bridge.recover()
        bridge._command('observe', dict(id='bar-1', time='2026-11-11T12:00:00', price='150',
            volume=1, completed_bar=True, high=153, low=147, close=150))
        assert bridge.grid.pending_bar is not None
    with open_grid_session(root, store, atr) as bridge:
        bridge.recover()
        assert bridge.grid.pending_bar is None
        assert list(bridge.grid.true_ranges) == [6]
        delivery = bridge.grid.delivery
        bridge.recover()
        assert bridge.grid.delivery == delivery
    assert not writes(requests)


def test_market_risk_exit_pauses_without_market_write(network_grid):
    root, store, factory, enable, fake, requests = network_grid
    enable()

    def exiting(session, deployment_id):
        return factory(session, deployment_id, breakout='exit')

    with open_grid_session(root, store, exiting) as bridge:
        bridge.recover()
        observe(bridge)
        fake.trade_update(bridge.grid.active[0].intent.client_order_id, '.04', '146')
        bridge.recover()
        with pytest.raises(SafetyBlocked, match='LIMIT orders; delivery paused'):
            bridge.observe(dict(id='exit', time='2026-11-11T12:00:01', price='160', volume=1))
            bridge.dispatch()
        assert not bridge.ready
        assert bridge.account().inventory == D('.04')
        assert all(post['type'] == ['LIMIT'] for post in writes(requests))
    assert len(writes(requests)) == 1


def test_cancel_partial_order_retains_below_minimum_inventory(network_grid):
    root, store, factory, enable, fake, requests = network_grid
    enable()
    with open_grid_session(root, store, factory) as bridge:
        bridge.recover()
        observe(bridge)
        key = bridge.grid.active[0].intent.client_order_id
        fake.trade_update(key, '.02', '146')
        bridge.recover()
        bridge.observe(dict(id='withdraw', time='2026-11-11T12:00:01', price='150',
            volume=1, action='cancel', order_id=key))
        assert bridge.account().inventory == D('.02')
        assert bridge.gateway.adapter.order(key).status is S.CANCELED
        assert bridge.dispatch() is None
    with open_grid_session(root, store, factory) as bridge:
        bridge.recover()
        assert bridge.account().inventory == D('.02')
        assert bridge.dispatch() is None
    assert len(writes(requests)) == len(writes(requests, 'DELETE')) == 1


def acceptance_tool():
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location('grid_acceptance',
        Path(__file__).parents[2] / 'tools' / 'demo_grid_acceptance.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_acceptance_restart_changed_book_and_readonly_after_stop(network_grid, monkeypatch):
    tool = acceptance_tool()
    root, store, factory, enable, fake, requests = network_grid
    enable()
    with open_grid_session(root, store, factory) as bridge:
        deployment = bridge.grid.deployment()
        tool.execute(bridge, 'submit')
        key = bridge.grid.active[0].intent.client_order_id
        intent = bridge.grid.active[0].intent
        observation = bridge.controller.store.event_payload(bridge.prefix + 'initial-observation')
    # The factory must restore rather than derive a new deployment from a new book.
    with open_grid_session(root, store, tool.factory) as bridge:
        assert bridge.grid.deployment() == deployment
        original = bridge.session._fresh_book
        monkeypatch.setattr(bridge.session, '_fresh_book', lambda: dict(original(), askPrice=D('151')))
        tool.execute(bridge, 'submit')
        assert bridge.grid.active[0].intent.client_order_id == key
        assert bridge.controller.store.event_payload(bridge.prefix + 'initial-observation') == observation
        tool.execute(bridge, 'cancel', key)
        tool.execute(bridge, 'submit')
    assert len(writes(requests)) == len(writes(requests, 'DELETE')) == 1
    with closing(DemoRuntime(root)) as runtime:
        runtime.stop()
    before = len(requests)
    with open_grid_session(root, store, read_only=True) as bridge:
        tool.execute(bridge, 'status')
        assert bridge.grid.deployment() == deployment
        assert not bridge.controller.account().open_orders
        with pytest.raises(SafetyBlocked, match='read-only'):
            bridge.dispatch()
        with pytest.raises(SafetyBlocked, match='authorization'):
            bridge.session.submit_intent(intent)
        with pytest.raises(SafetyBlocked, match='authorization'):
            bridge.session.cancel(key)
    assert all(request.get_method() == 'GET' for request in requests[before:])


def test_acceptance_utc_conversion_reaches_grid_observe(network_grid):
    from datetime import datetime, timedelta, timezone
    tool = acceptance_tool()
    root, store, factory, enable, fake, requests = network_grid
    enable()
    with open_grid_session(root, store, factory) as bridge:
        bridge.recover()
        event = tool.initial_observation(dict(askPrice=D('150'),
            time_utc=datetime(2026, 11, 11, 20, tzinfo=timezone(timedelta(hours=8)))))
        assert event['time'] == '2026-11-11T12:00:00'
        assert bridge.observe(event).status is S.SUBMITTED
        assert bridge.grid.timestamp.isoformat() == event['time']


@pytest.mark.parametrize('reserved', [False, True])
def test_acceptance_resumes_only_unattempted_initial_proposal(network_grid, monkeypatch, reserved):
    tool = acceptance_tool()
    root, store, factory, enable, fake, requests = network_grid
    campaign = enable()
    with open_grid_session(root, store, factory) as bridge:
        bridge.recover()
        with monkeypatch.context() as patch:
            patch.setattr(bridge, 'dispatch', lambda: (_ for _ in ()).throw(SafetyBlocked('temporary quote failure')))
            with pytest.raises(SafetyBlocked, match='temporary quote'):
                tool.submit_initial(bridge)
        identity = bridge.grid.active[0].intent.client_order_id
        original_price = bridge.grid.active[0].intent.limit_price
        if reserved:
            with bridge.runtime.db:
                bridge.runtime.db.execute('INSERT INTO dispatches VALUES (?, ?, ?, ?, NULL)',
                    (campaign['id'], 'POST', identity, '{}'))
        else:
            tool.execute(bridge, 'recover')
            assert not writes(requests)
    with open_grid_session(root, store, tool.factory) as bridge:
        original = bridge.session._fresh_book
        monkeypatch.setattr(bridge.session, '_fresh_book', lambda: dict(original(), askPrice=D('151')))
        tool.execute(bridge, 'submit')
        tool.execute(bridge, 'submit')
        assert bridge.grid.active[0].intent.client_order_id == identity
        assert bridge.grid.active[0].intent.limit_price == original_price
    assert len(writes(requests)) == (0 if reserved else 1)


def test_acceptance_repeated_submit_does_not_dispatch_reverse(network_grid):
    tool = acceptance_tool()
    root, store, factory, enable, fake, requests = network_grid
    enable()
    with open_grid_session(root, store, factory) as bridge:
        tool.execute(bridge, 'submit')
        key = bridge.grid.active[0].intent.client_order_id
        fake.trade_update(key, '.04', '146')
    with open_grid_session(root, store, tool.factory) as bridge:
        tool.execute(bridge, 'submit')
        assert bridge.account().inventory == D('.04')
        assert bridge.grid.active[0].intent.side is Side.SELL
    assert len(writes(requests)) == 1


def test_explicit_new_campaign_deployment_preserves_previous_journal(network_grid):
    root, store, factory, enable, fake, requests = network_grid
    first = enable()
    with open_grid_session(root, store, factory) as bridge:
        bridge.recover()
        old_prefix = bridge.prefix
        old_binding = bridge.controller.store.event_payload(old_prefix + 'deployment')
    with closing(DemoRuntime(root)) as runtime:
        runtime.stop()
    second = enable()
    with pytest.raises(SafetyBlocked, match='campaign differs'):
        with open_grid_session(root, store, factory):
            pytest.fail('silently adopted a replacement campaign')
    with open_grid_session(root, store, factory, new_deployment=True) as bridge:
        assert bridge.grid.deployment_id == second['prefix'] + '-g'
        assert bridge.controller.store.event_payload(old_prefix + 'deployment') == old_binding
        assert bridge.grid.deployment_id != first['prefix'] + '-g'
    with pytest.raises(SafetyBlocked, match='already has'):
        with open_grid_session(root, store, factory, new_deployment=True):
            pytest.fail('replaced the same campaign deployment')
    assert not writes(requests)
    assert not writes(requests, 'DELETE')
