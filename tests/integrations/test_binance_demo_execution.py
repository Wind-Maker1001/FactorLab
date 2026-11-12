from dataclasses import replace
from decimal import Decimal as D
import json
import pickle
from urllib.parse import parse_qs

import pytest

from factorlab.errors import DataQualityError
from factorlab.integrations.binance.client import Response
from factorlab.integrations.binance_futures_trading.execution import (
    DemoExecutionAuthorization, DemoExecutionTransport, DemoExecutionGateway,
    DemoFuturesAdapter, observe_demo_rules,
)
from factorlab.integrations.binance_futures_trading.fake import FakeTransport, NOW, synthetic_rules
from factorlab.integrations.binance_futures_trading.protocol import (
    Credentials, Environment as E, FuturesClient, ReadOnlyNetworkTransport, SafetyBlocked, OutcomeUnknown,
)
from factorlab.integrations.binance_futures_trading.stream import UserDataEvents
from factorlab.trading import Market, OrderIntent, OrderType, Side, OrderStatus as S
from factorlab.trading.control import TradingController, RiskLimits, RiskRejected
from factorlab.trading.paper import Instrument, Quote
from factorlab.trading.storage import PaperStore


class DemoFixture(FakeTransport):
    def __call__(self, request, timeout):
        if request.path == '/fapi/v1/exchangeInfo':
            self.calls.append((request.method, request.path, {}))
            rules = synthetic_rules()
            return Response(200, {}, json.dumps({'symbols': [dict(symbol='NVDAUSDT', status='TRADING',
                quoteAsset='USDT', marginAsset='USDT', orderTypes=['MARKET', 'LIMIT'],
                filters=list(rules.filters.values()))]}).encode())
        return super().__call__(request, timeout)


def intent(key='one', side=Side.BUY, limit=None, quantity='2'):
    return OrderIntent(Market.BINANCE_USDM, 'NVDAUSDT', side,
        OrderType.MARKET if limit is None else OrderType.LIMIT, D(quantity),
        None if limit is None else D(limit), key)


def setup(protocol=':memory:', paper=':memory:', fake=None, authorize=True, now=NOW):
    fake = DemoFixture() if fake is None else fake
    transport = DemoExecutionTransport(delegate=fake,
        authorization=DemoExecutionAuthorization() if authorize else None)
    client = FuturesClient(Credentials('demo-key', 'demo-secret', E.DEMO),
        environment=E.DEMO, transport=transport, clock=lambda: now.timestamp())
    adapter = DemoFuturesAdapter(client, PaperStore(protocol), clock=lambda: now)
    observation = observe_demo_rules(adapter, source_version='OFFLINE_DEMO_FIXTURE')
    gateway = DemoExecutionGateway(adapter, observation, (Instrument('NVDAUSDT', 'USDT'),), {'USDT': D('1000')})
    controller = TradingController(gateway, PaperStore(paper), clock=lambda: now,
        limits=RiskLimits(D('500'), D('500'), D('800'), 10))
    controller.quote(Quote('NVDAUSDT', D('100'), now))
    gateway.recover(controller)
    return adapter, gateway, controller, fake


def test_environment_authorization_and_no_network(monkeypatch):
    monkeypatch.setattr('factorlab.integrations.binance_futures_trading.protocol.build_opener',
        lambda *a: pytest.fail('no network connection permitted'))
    delegate = DemoFixture()
    transport = DemoExecutionTransport(delegate=delegate, authorization=DemoExecutionAuthorization())
    assert transport.host == 'https://demo-fapi.binance.com'
    for env in (None, E.PRODUCTION):
        with pytest.raises(SafetyBlocked):
            FuturesClient(Credentials('k', 's', env), environment=env, transport=transport)
    with pytest.raises(SafetyBlocked):
        DemoExecutionTransport(delegate=ReadOnlyNetworkTransport())
    with pytest.raises(TypeError):
        pickle.dumps(transport.authorization)
    production = FuturesClient(Credentials('k', 's', E.PRODUCTION), environment=E.PRODUCTION)
    with pytest.raises(SafetyBlocked):
        transport(production.build('POST', '/fapi/v1/order'), 1)
    a, g, c, fake = setup(authorize=False)
    with pytest.raises(SafetyBlocked):
        c.submit(intent())
    assert not [call for call in fake.calls if call[0] != 'GET']
    with pytest.raises(SafetyBlocked):
        a.submit_order(intent())


@pytest.mark.parametrize('side', [Side.BUY, Side.SELL])
@pytest.mark.parametrize('limit', [None, '100.01'])
def test_order_contract(side, limit):
    a, g, c, fake = setup()
    event = c.submit(intent(side=side, limit=limit))
    assert event.status == S.SUBMITTED
    params = next(p for method, _, p in fake.calls if method == 'POST')
    assert params['side'] == [side.value] and params['quantity'] == ['2']
    assert params['positionSide'] == ['BOTH']
    if limit is not None:
        assert params['price'] == [limit] and params['timeInForce'] == ['GTC']
    assert len(a.open_orders('NVDAUSDT')) == 1
    assert a.query_order('one').status == S.SUBMITTED
    assert c.cancel('one').status == S.CANCELED


@pytest.mark.parametrize('quantity,limit,reason', [
    ('0.001', None, 'QUANTITY_FILTER'), ('0.01', None, 'MIN_NOTIONAL'),
    ('2', '100.001', 'PRICE_FILTER')])
def test_exchange_filters(quantity, limit, reason):
    a, g, c, fake = setup()
    with pytest.raises(RiskRejected, match=reason):
        c.submit(intent(quantity=quantity, limit=limit))
    assert not [call for call in fake.calls if call[0] == 'POST']


@pytest.mark.parametrize('condition', ['mode', 'margin', 'config', 'bracket', 'risk'])
def test_unknown_and_risk_admission(condition):
    a, g, c, fake = setup()
    if condition == 'mode':
        a.facts = replace(a.facts, hedge_mode=None)
    elif condition == 'margin':
        a.facts = replace(a.facts, assets=({'asset': 'USDT', 'walletBalance': '1000'},))
    elif condition == 'config':
        a.facts = replace(a.facts, symbol_config=())
    elif condition == 'bracket':
        g.rules = replace(g.rules, brackets=())
    else:
        c.set_kill_switch()
    with pytest.raises((SafetyBlocked, RiskRejected)):
        c.submit(intent())
    assert not [call for call in fake.calls if call[0] == 'POST']


def test_submit_rejection_and_unknown_recovery():
    a, g, c, fake = setup()
    fake.response_next = Response(400, {}, b'{"code":-2019}')
    assert c.submit(intent('reject')).status == S.REJECTED
    fake.timeout_next = True
    assert c.submit(intent()) is None
    assert a.order('one').status == S.UNKNOWN
    with pytest.raises(ValueError, match='RECONCILE_REQUIRED'):
        c.submit(intent())
    g.recover(c)
    assert a.order('one').status == S.SUBMITTED
    assert len([call for call in fake.calls if call[0] == 'POST']) == 2


@pytest.mark.parametrize('response', [Response(400, {}, b'{"code":-2011}'),
                                     Response(503, {}, b'{"code":-1007}')])
def test_cancel_failure_requires_query(response):
    a, g, c, fake = setup()
    c.submit(intent())
    fake.response_next = response
    assert c.cancel('one') is None
    assert a.order('one').status == S.UNKNOWN
    g.recover(c)
    assert a.order('one').status == S.SUBMITTED
    assert len([call for call in fake.calls if call[0] == 'DELETE']) == 1


def test_partial_duplicate_terminal_and_restart(tmp_path):
    protocol, paper = tmp_path / 'protocol.sqlite', tmp_path / 'paper.sqlite'
    a, g, c, fake = setup(protocol, paper)
    c.submit(intent())
    message = fake.trade_update('one', '0.5')
    event = UserDataEvents(a).parse(message)
    c.replay(event)
    c.replay(UserDataEvents(a).parse(message))
    assert a.order('one').cumulative_filled_quantity == D('0.5')
    fake.trade_update('one', '1.5')
    a.disconnect()
    a.store.close()
    c.store.close()
    a, g, c, fake = setup(protocol, paper, fake)
    assert a.order('one').status == S.FILLED
    assert c.account().positions[0].quantity == D('2')
    before = c.account().assets
    assert g.recover(c).assets == before
    assert len([call for call in fake.calls if call[0] == 'POST']) == 1


def test_cross_process_identity_and_unknown_query(tmp_path):
    import os
    import subprocess
    import sys
    a, g, c, fake = setup(tmp_path / 'protocol.sqlite')
    c.submit(intent())
    a.store.close()
    script = '''
import json, sys
from factorlab.integrations.binance_futures_trading.execution import DemoExecutionTransport, DemoFuturesAdapter
from factorlab.integrations.binance_futures_trading.protocol import Credentials, Environment, FuturesClient, SafetyBlocked
from factorlab.integrations.binance_futures_trading.fake import FakeTransport, NOW
from factorlab.trading.storage import PaperStore
fake = FakeTransport()
fake.orders = json.loads(sys.stdin.read())
client = FuturesClient(Credentials('demo-key', 'demo-secret', Environment.DEMO), environment=Environment.DEMO, transport=DemoExecutionTransport(delegate=fake))
a = DemoFuturesAdapter(client, PaperStore(sys.argv[1]), clock=lambda: NOW)
assert a.order('one').reconcile_required
assert not a.reconciled
assert a.query_order('one').status.value == 'SUBMITTED'
assert all(method == 'GET' for method, _, _ in fake.calls)
print('RESTORED_QUERY_ONLY')
'''
    result = subprocess.run([sys.executable, '-c', script, str(tmp_path / 'protocol.sqlite')],
        input=json.dumps(fake.orders), text=True, capture_output=True, timeout=20, env=os.environ.copy())
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'RESTORED_QUERY_ONLY'


def test_persist_before_dispatch_and_lost_cancel_reply():
    class InspectDispatch(DemoFixture):
        adapter = None
        def __call__(self, request, timeout):
            if request.method == 'POST':
                key = parse_qs(request.query)['newClientOrderId'][0]
                assert self.adapter.order(key).status == S.UNKNOWN
            result = super().__call__(request, timeout)
            if request.method == 'DELETE':
                raise TimeoutError('offline lost cancel reply')
            return result
    fake = InspectDispatch()
    a, g, c, fake = setup(fake=fake)
    fake.adapter = a
    c.submit(intent())
    assert c.cancel('one') is None
    assert a.order('one').reconcile_required
    g.recover(c)
    assert a.order('one').status == S.CANCELED
    assert len([call for call in fake.calls if call[0] == 'DELETE']) == 1


def test_stale_duplicate_does_not_regress_terminal_state():
    a, g, c, fake = setup()
    old = c.submit(intent())
    c.replay(UserDataEvents(a).parse(fake.trade_update('one', '2')))
    a._apply(old, D('0'))
    assert a.order('one').status == S.FILLED
    assert a.order('one').cumulative_filled_quantity == D('2')


def test_credentials_cannot_be_swapped_after_client_construction():
    a, g, c, fake = setup()
    a.client.credentials = Credentials('production', 'secret', E.PRODUCTION)
    with pytest.raises(SafetyBlocked):
        a.client.build('POST', '/fapi/v1/order', {})


def test_partial_cancel_and_regressing_fill():
    a, g, c, fake = setup()
    c.submit(intent())
    c.replay(UserDataEvents(a).parse(fake.trade_update('one', '0.5')))
    assert c.cancel('one').status == S.CANCELED
    assert a.order('one').cumulative_filled_quantity == D('0.5')
    fake.orders['one']['executedQty'] = '0.1'
    with pytest.raises(DataQualityError, match='regression'):
        a.query_order('one')
    assert a.order('one').reconcile_required
