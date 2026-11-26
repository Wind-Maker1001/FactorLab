from contextlib import closing
from decimal import Decimal as D
import json
from urllib.parse import parse_qs

from test_binance_demo_strategy_session import deployment
from test_binance_demo_network import NetworkFixture, wired
from factorlab.integrations.binance.client import Response
from factorlab.integrations.binance_futures_trading.demo_cli import open_session
from factorlab.integrations.binance_futures_trading.demo_portfolio_consumer import DemoPortfolioConsumer
from factorlab.integrations.binance_futures_trading.demo_runtime import DemoRuntime


def test_legacy_leverage_configuration_uses_symbol_capability_without_switching_session(deployment, monkeypatch):
    root, credentials, campaign, fake = deployment
    original = NetworkFixture.__call__
    leverage = [5]
    def symbols(self, request, timeout):
        params = parse_qs(request.query)
        if request.path == '/fapi/v1/leverage' and request.method == 'POST':
            assert params['symbol'] == ['QQQUSDT']
            leverage[0] = int(params['leverage'][0])
            return Response(200, {}, json.dumps(dict(symbol='QQQUSDT', leverage=leverage[0])).encode())
        result = original(self, request, timeout)
        payload = json.loads(result.body)
        if request.path == '/fapi/v1/symbolConfig':
            payload.append(dict(symbol='QQQUSDT', leverage=leverage[0], marginType='CROSSED'))
        elif request.path == '/fapi/v1/exchangeInfo':
            row = dict(payload['symbols'][0], symbol='QQQUSDT', contractType='TRADIFI_PERPETUAL')
            payload['symbols'].append(row)
        elif request.path == '/fapi/v1/leverageBracket' and params.get('symbol') == ['QQQUSDT']:
            payload = [dict(payload[0], symbol='QQQUSDT')]
        return Response(result.status, result.headers, json.dumps(payload).encode())
    monkeypatch.setattr(NetworkFixture, '__call__', symbols)
    with closing(DemoRuntime(root)) as runtime:
        runtime.start_continuous(credentials.read_config(), opening_equity=D(1000),
                                 stop_epoch=runtime.stop_epoch())
        with open_session(root, credentials, runtime, dict(GET=0, POST=0, DELETE=0), campaign=campaign) as (session, _):
            original_context = (session.symbol, session.configuration, session.gateway.rules,
                                session.gateway.leverage, session.client.transport, session._write_guard)
            from types import SimpleNamespace
            monkeypatch.setattr('factorlab.integrations.binance_futures_trading.network.datetime',
                                SimpleNamespace(now=lambda tz: session._now()))
            consumer = DemoPortfolioConsumer(session, runtime, root)
            with consumer.scope('configuration', 'QQQUSDT'):
                consumer.configure_leverage('QQQUSDT')
            assert (session.symbol, session.configuration, session.gateway.rules,
                    session.gateway.leverage, session.client.transport, session._write_guard) == original_context
            assert session.symbol_context('QQQUSDT').leverage == 1
            assert runtime.db.execute('SELECT client_id FROM dispatches WHERE method=\'POST\'').fetchall() == [('pf-leverage-QQQUSDT',)]
            assert runtime.consumption(runtime.continuous_authorization())['posts'] == 1


def test_two_symbol_legacy_task_restarts_without_context_switch_or_duplicate_post(deployment, monkeypatch):
    from types import SimpleNamespace
    root, credentials, campaign, fake = deployment
    original = NetworkFixture.__call__
    def symbols(self, request, timeout):
        params = parse_qs(request.query)
        result = original(self, request, timeout)
        payload = json.loads(result.body)
        symbol = params.get('symbol', ['NVDAUSDT'])[0]
        if request.path == '/fapi/v1/symbolConfig':
            payload.extend(dict(symbol=s, leverage=1, marginType='CROSSED') for s in ('QQQUSDT', 'SPYUSDT'))
        elif request.path == '/fapi/v1/exchangeInfo':
            payload['symbols'].extend(dict(payload['symbols'][0], symbol=s, contractType='TRADIFI_PERPETUAL')
                                      for s in ('QQQUSDT', 'SPYUSDT'))
        elif request.path == '/fapi/v1/leverageBracket':
            payload = [dict(payload[0], symbol=symbol)]
        elif request.path in ('/fapi/v1/ticker/bookTicker', '/fapi/v1/premiumIndex'):
            payload['symbol'] = symbol
        elif request.path == '/fapi/v1/apiTradingStatus':
            payload['indicators'].update({s: [{'indicator':'UFR', 'isLocked':False}]
                                          for s in ('QQQUSDT', 'SPYUSDT')})
        return Response(result.status, result.headers, json.dumps(payload).encode())
    monkeypatch.setattr(NetworkFixture, '__call__', symbols)
    with closing(DemoRuntime(root)) as runtime:
        runtime.start_continuous(credentials.read_config(), opening_equity=D(1000), stop_epoch=runtime.stop_epoch())
        for restart in (False, True):
            with open_session(root, credentials, runtime, dict(GET=0, POST=0, DELETE=0), campaign=campaign) as (session, _):
                monkeypatch.setattr('factorlab.integrations.binance_futures_trading.network.datetime',
                                    SimpleNamespace(now=lambda tz: session._now()))
                consumer = DemoPortfolioConsumer(session, runtime, root)
                if not restart:
                    (consumer.directory / 'task.json').write_text(json.dumps(dict(task_id='stable-original',
                        budget_usdt='20', weights={'QQQUSDT':'.5', 'SPYUSDT':'.5'})))
                original_context = (session.symbol, session.configuration, session.gateway.rules,
                                    session.gateway.leverage, session.client.transport, session._write_guard)
                consumer._poll()
                assert (session.symbol, session.configuration, session.gateway.rules,
                        session.gateway.leverage, session.client.transport, session._write_guard) == original_context
                assert len(fake.orders) == 2
                assert {row['symbol'] for row in fake.orders.values()} == {'QQQUSDT', 'SPYUSDT'}
                assert runtime.consumption(runtime.continuous_authorization())['posts'] == 2
                assert len(session.controller.store.db.execute('SELECT client_id FROM demo_order_owners').fetchall()) == 2
                if restart:
                    original_ids = set(fake.orders)
                    (consumer.directory / 'task.json').write_text(json.dumps(dict(task_id='stable-original',
                        budget_usdt='20', weights={'QQQUSDT':'.5', 'SPYUSDT':'.5'}, action='cancel_pending')))
                    consumer._poll()
                    assert set(fake.orders) == original_ids
                    assert all(order['status'] == 'CANCELED' for order in fake.orders.values())
                    assert runtime.consumption(runtime.continuous_authorization())['posts'] == 2
                    assert (session.symbol, session.configuration, session.gateway.rules,
                            session.gateway.leverage, session.client.transport, session._write_guard) == original_context
