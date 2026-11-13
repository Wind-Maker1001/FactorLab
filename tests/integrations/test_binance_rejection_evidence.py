import json
from decimal import Decimal

import pytest

from factorlab.integrations.binance.client import Response
from factorlab.integrations.binance_futures_trading.adapter import FuturesAdapter
from factorlab.integrations.binance_futures_trading.fake import FakeTransport, NOW
from factorlab.integrations.binance_futures_trading.order_evidence import exact_demo_order
from factorlab.integrations.binance_futures_trading.protocol import (
    Credentials, Environment, ExchangeError, FuturesClient, OfflineTransport, rejection_message,
)
from factorlab.trading import Market, OrderIntent, OrderType, OrderStatus, Side
from factorlab.trading.storage import PaperStore


class RejectingTransport(FakeTransport):
    def __call__(self, request, timeout):
        if request.method == 'POST':
            self.calls.append(('POST', request.path, {}))
            return Response(400, {}, b'{"code":-2019,"msg":"Margin is insufficient."}')
        return super().__call__(request, timeout)


def test_rejection_diagnostic_survives_restart_without_changing_recovery(tmp_path):
    path = str(tmp_path / 'protocol.sqlite3')
    client = FuturesClient(Credentials('test-key', 'test-secret'), transport=RejectingTransport())
    store = PaperStore(path)
    adapter = FuturesAdapter(client, store, clock=lambda: NOW)
    adapter.reconcile()
    intent = OrderIntent(Market.BINANCE_USDM, 'NVDAUSDT', Side.BUY, OrderType.LIMIT,
                         Decimal('2'), Decimal('100'), 'rejection-test')
    event = adapter.submit_order(intent)
    assert event.status is OrderStatus.REJECTED
    diagnostic = json.loads(store.db.execute('SELECT payload FROM futures_rejections').fetchone()[0])
    assert diagnostic == dict(client_order_id='rejection-test',event_id='reject:rejection-test',
                             http_status=400,code=-2019,message='Margin is insufficient.',category='INSUFFICIENT_MARGIN')
    store.close()
    store = PaperStore(path)
    restored = FuturesAdapter(client, store, clock=lambda: NOW)
    calls_before = len(client.transport.calls)
    restored.reconcile()
    assert not any(path == '/fapi/v1/order' for _, path, _ in client.transport.calls[calls_before:])
    assert sum(method == 'POST' for method, _, _ in client.transport.calls) == 1
    assert not any(method == 'DELETE' for method, _, _ in client.transport.calls)
    assert restored.order('rejection-test').status is OrderStatus.REJECTED
    assert restored.order('rejection-test').cumulative_filled_quantity == 0
    assert json.loads(store.db.execute('SELECT payload FROM futures_rejections').fetchone()[0]) == diagnostic
    store.close()


@pytest.mark.parametrize('message', ['https://host.invalid/?x=1', 'signature=abc', 'key-value',
    'secret-value', 'a'*513, 'x\nheader', 'b'*64])
def test_message_suppression(message):
    assert rejection_message({'msg':message},credentials=Credentials('key-value','secret-value')) == '[SUPPRESSED_UNSAFE_EXCHANGE_MESSAGE]'


def test_signature_echo_and_exception_string_are_not_exposed():
    client = FuturesClient(Credentials('key-value','secret-value'),transport=FakeTransport())
    request = client.build('GET','/fapi/v1/order',{'symbol':'NVDAUSDT','origClientOrderId':'one'})
    signature = request.query.rsplit('=',1)[1]
    assert rejection_message({'msg':signature},credentials=client.credentials,request=request) == '[SUPPRESSED_UNSAFE_EXCHANGE_MESSAGE]'
    assert 'Margin' not in str(ExchangeError(-2019,400,'INSUFFICIENT_MARGIN',message='Margin is insufficient.'))


@pytest.mark.parametrize('code,status,state', [(-2013,400,'ABSENT'),(-2015,401,'UNKNOWN'),(-1000,500,'UNKNOWN')])
def test_exact_query_once_without_any_writes(code,status,state):
    calls=[]
    class Transport(OfflineTransport):
        def __call__(self,request,timeout):
            calls.append(request)
            return Response(status,{},json.dumps(dict(code=code,msg='Order query result.')).encode())
    client=FuturesClient(Credentials('key','secret',Environment.DEMO),environment=Environment.DEMO,transport=Transport())
    result=exact_demo_order(client,'original-id')
    assert result['state']==state and result['code']==code and result['http_status']==status
    assert len(calls)==1 and calls[0].method=='GET'
    assert 'origClientOrderId=original-id' in calls[0].query


@pytest.mark.parametrize('returned_id,state',[('original-id','FOUND'),('other','UNKNOWN')])
def test_exact_query_success_requires_matching_identity(returned_id,state):
    class Transport(OfflineTransport):
        def __call__(self,request,timeout):
            return Response(200,{},json.dumps(dict(symbol='NVDAUSDT',clientOrderId=returned_id,status='NEW')).encode())
    client=FuturesClient(Credentials('key','secret',Environment.DEMO),environment=Environment.DEMO,transport=Transport())
    assert exact_demo_order(client,'original-id')['state']==state
