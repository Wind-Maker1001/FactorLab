"""Strategy events through the persistent, HTTP-stubbed Demo session."""
from contextlib import closing
from datetime import timedelta
from decimal import Decimal as D
import json

import pytest

from test_binance_demo_network import NetworkFixture, wired
from test_binance_demo_runtime import MemoryCredentialStore
from factorlab.integrations.binance.client import Response
from factorlab.integrations.binance_futures_trading.demo_cli import open_session
from factorlab.integrations.binance_futures_trading.demo_runtime import DemoRuntime, bind_account, now
from factorlab.integrations.binance_futures_trading.demo_session import (
    DemoSession, demo_account_configuration, observe_demo_account,
)
from factorlab.integrations.binance_futures_trading.fake import NOW, MS
from factorlab.integrations.binance_futures_trading.protocol import Credentials, Environment, SafetyBlocked
from factorlab.integrations.binance_futures_trading.strategy_runtime import DemoStrategyRuntime
from factorlab.trading import OrderType, Side, OrderStatus
from factorlab.trading.control import RiskLimits
from factorlab.trading.paper import Quote
from factorlab.trading.strategy import ClosedBar, DualMAStrategy


AT = NOW + timedelta(milliseconds=2)


@pytest.fixture
def deployment(tmp_path, monkeypatch, wired):
    original = NetworkFixture.__call__

    def market(self, request, timeout):
        if request.path.endswith('bookTicker'):
            return Response(200, {}, b'{"symbol":"NVDAUSDT","bidPrice":"100","askPrice":"100","time":%d}' % (MS + 2))
        result = original(self, request, timeout)
        if request.path == '/fapi/v3/positionRisk':
            payload = json.loads(result.body)
            for position in payload:
                position.update(positionAmt=str(self.quantity), entryPrice=str(self.entry))
            return Response(200, {}, json.dumps(payload).encode())
        if request.path == '/fapi/v3/account':
            payload = json.loads(result.body)
            for position in payload['positions']:
                del position['entryPrice']
            return Response(200, {}, json.dumps(payload).encode())
        if request.path.endswith('exchangeInfo'):
            payload = json.loads(result.body)
            payload['symbols'][0]['timeInForce'] = ['GTC']
            for rule in payload['symbols'][0]['filters']:
                if rule['filterType'] == 'MIN_NOTIONAL':
                    rule['notional'] = '1'
            return Response(200, {}, json.dumps(payload).encode())
        return result

    monkeypatch.setattr(NetworkFixture, '__call__', market)
    monkeypatch.setattr(DemoSession, '_now', lambda self: AT)
    store = MemoryCredentialStore()
    credential = Credentials('demo-key', 'demo-secret', Environment.DEMO)
    facts, _ = observe_demo_account(credential)
    binding = bind_account(store, credential, facts, demo_account_configuration(facts))
    with closing(DemoRuntime(tmp_path)) as runtime:
        campaign = runtime.start(binding, seconds=900, orders=3, max_loss=D('1'),
            opening_equity=D('1000'), stop_epoch=runtime.stop_epoch(), started_at=now())
    return tmp_path, store, campaign, wired[0]


def runner_for(session, expect, campaign):
    runner = DemoStrategyRuntime(session, DualMAStrategy(max_weight=D('.004')),
        client_id_prefix=campaign['prefix'] + '-', expect=expect)
    runner.recover()
    return runner


def bars(runner):
    start = NOW - timedelta(hours=21)
    for i in range(21):
        price = D(100 if i < 20 else 110)
        opened = start + timedelta(hours=i)
        bar = ClosedBar('NVDAUSDT', opened, opened + timedelta(hours=1),
            price, price, price, price, D(100), 3600, 'BINANCE_USDM_PUBLIC_REST')
        runner.closed_bar(bar)
    return bar


@pytest.mark.parametrize('lost_reply', [False, True])
def test_persistent_session_strategy_restart(deployment, lost_reply):
    root, store, campaign, fake = deployment
    counts = dict(GET=0, POST=0, DELETE=0)
    with closing(DemoRuntime(root)) as runtime:
        with open_session(root, store, runtime, counts, campaign=campaign) as (session, expect):
            runner = runner_for(session, expect, campaign)
            bar = bars(runner)
            fake.timeout_next = lost_reply
            runner.quote(Quote('NVDAUSDT', D(100), AT))
            outcome = session.gateway.state.runtime['last_order']
            key = outcome['client_order_id']
            intent = session.adapter.order(key).intent
            assert intent.order_type is OrderType.LIMIT and intent.limit_price == D(100)
            assert intent.side is Side.BUY and not intent.reduce_only
            assert intent.quantity <= D('.04')
            assert runtime.post_count(campaign['id']) == counts['POST'] == 1
            posted = next(params for method, path, params in fake.calls if method == 'POST')
            assert posted['type'] == ['LIMIT'] and posted['timeInForce'] == ['GTC']
            assert session.adapter.order(key).reconcile_required is lost_reply
            assert runner.quote(Quote('NVDAUSDT', D(100), AT)) is None
            fake.trade_update(key, '.01')
            assert runner.recover().reconciled
            assert session.controller.account().positions[0].quantity == D('.01')
            assert not runner.closed_bar(bar)
            # An active partial order prevents a different signal from submitting.
            pending = dict(session.gateway.state.runtime['last_signal'], signal_id='while-partial')
            session.controller._atomic(lambda: session.gateway.state.runtime.update(pending=pending))
            assert runner.quote(Quote('NVDAUSDT', D(100), AT)) is None
            fake.trade_update(key, str(intent.quantity - D('.01')))
    with closing(DemoRuntime(root)) as runtime:
        with open_session(root, store, runtime, counts, campaign=campaign) as (session, expect):
            runner = runner_for(session, expect, campaign)
            assert session.controller.account().positions[0].quantity == intent.quantity
            assert session.controller.account().assets[0].total == fake.cash
            assert runtime.post_count(campaign['id']) == counts['POST'] == 1
            assert not runner.closed_bar(bar)
            # Redelivery of the original signal cannot allocate another identity.
            signal = session.gateway.state.runtime['last_signal']
            session.controller._atomic(lambda: session.gateway.state.runtime.update(pending=signal))
            assert runner.quote(Quote('NVDAUSDT', D(100), AT)) is None
            assert counts['POST'] == 1
            runner.pause()
            assert runner.recover().reconciled
            assert session.gateway.state.runtime['status'] == 'PAUSED'
            runner.resume()
            close = dict(signal, signal_id='close-long', direction='SELL', target_weight=D(0))
            session.controller._atomic(lambda: session.gateway.state.runtime.update(pending=close))
            if lost_reply:
                with pytest.raises(SafetyBlocked, match='stopped or expired'):
                    runner.quote(Quote('NVDAUSDT', D(100), AT))
                runner.stop()
                assert session.controller.account().positions[0].quantity == intent.quantity
                assert counts['POST'] == runtime.post_count(campaign['id']) == 1
                return
            runner.quote(Quote('NVDAUSDT', D(100), AT))
            exit_key = session.gateway.state.runtime['last_order']['client_order_id']
            sell = session.adapter.order(exit_key).intent
            assert sell.side is Side.SELL and sell.reduce_only and sell.quantity == intent.quantity
            fake.trade_update(exit_key, str(sell.quantity))
            assert runner.recover().reconciled
            assert not session.controller.account().positions
            assert counts['POST'] == runtime.post_count(campaign['id']) == 2
            runner.stop()
            assert runner.recover().reconciled
            assert session.gateway.state.runtime['status'] == 'STOPPED'
            with pytest.raises(SafetyBlocked, match='stopped'):
                runner.resume()


def test_decision_survives_failure_before_submission(deployment, monkeypatch):
    root, store, campaign, fake = deployment
    counts = dict(GET=0, POST=0, DELETE=0)
    with closing(DemoRuntime(root)) as runtime:
        with open_session(root, store, runtime, counts, campaign=campaign) as (session, expect):
            runner = runner_for(session, expect, campaign)
            bar = bars(runner)
            def interrupt(*args, **kwargs):
                assert not session.controller.store.db.in_transaction
                raise RuntimeError('interrupted after durable decision')
            monkeypatch.setattr(session, 'submit_limit', interrupt)
            with pytest.raises(RuntimeError, match='durable decision'):
                runner.quote(Quote('NVDAUSDT', D(100), AT))
            key = session.gateway.state.runtime['last_order']['client_order_id']
    with closing(DemoRuntime(root)) as runtime:
        with open_session(root, store, runtime, counts, campaign=campaign) as (session, expect):
            runner = runner_for(session, expect, campaign)
            assert not runner.closed_bar(bar)
            assert runner.quote(Quote('NVDAUSDT', D(100), AT)) is None
            assert session.gateway.state.runtime['last_order']['client_order_id'] == key
            assert counts['POST'] == runtime.post_count(campaign['id']) == 0


@pytest.mark.parametrize('block', ['stale', 'disconnect', 'pause', 'stop', 'risk', 'reject'])
def test_session_strategy_blocks_and_rejections(deployment, block):
    root, store, campaign, fake = deployment
    counts = dict(GET=0, POST=0, DELETE=0)
    with closing(DemoRuntime(root)) as runtime:
        with open_session(root, store, runtime, counts, campaign=campaign) as (session, expect):
            runner = runner_for(session, expect, campaign)
            bars(runner)
            quote = Quote('NVDAUSDT', D(100), AT)
            if block == 'stale':
                quote = Quote('NVDAUSDT', D(100), AT - timedelta(seconds=6))
            elif block == 'disconnect':
                runner.disconnect()
            elif block == 'pause':
                runner.pause()
            elif block == 'stop':
                runner.stop()
            elif block == 'risk':
                session.controller.limits = RiskLimits(D('1'), D('1'), D('1'), 10)
            elif block == 'reject':
                fake.response_next = Response(400, {}, b'{"code":-2010,"msg":"synthetic rejection"}')
            runner.quote(quote)
            if block == 'reject':
                key = session.gateway.state.runtime['last_order']['client_order_id']
                assert session.adapter.order(key).status is OrderStatus.REJECTED
                assert counts['POST'] == runtime.post_count(campaign['id']) == 1
                assert runner.quote(quote) is None
                assert counts['POST'] == 1
            else:
                assert counts['POST'] == runtime.post_count(campaign['id']) == 0
            if block == 'risk':
                assert session.gateway.state.runtime['last_order']['kind'] == 'TARGET_REJECTED'
            if block == 'disconnect':
                assert runner.recover().reconciled
                runner.quote(Quote('NVDAUSDT', D(100), AT))
                assert counts['POST'] == 1


def test_consumed_authorization_is_not_reset_by_strategy_restart(deployment):
    root, store, campaign, fake = deployment
    counts = dict(GET=0, POST=0, DELETE=0)
    for index in range(3):
        with closing(DemoRuntime(root)) as runtime:
            with open_session(root, store, runtime, counts, campaign=campaign) as (session, expect):
                runner = runner_for(session, expect, campaign)
                if index == 0:
                    bars(runner)
                else:
                    signal = dict(session.gateway.state.runtime['last_signal'], signal_id='budget-' + str(index),
                        target_weight=D(0) if index == 1 else D('.004'))
                    session.controller._atomic(lambda: session.gateway.state.runtime.update(pending=signal))
                runner.quote(Quote('NVDAUSDT', D(100), AT))
                key = session.gateway.state.runtime['last_order']['client_order_id']
                fake.trade_update(key, str(session.adapter.order(key).intent.quantity))
                assert runner.recover().reconciled
                assert runtime.post_count(campaign['id']) == counts['POST'] == index + 1
    with closing(DemoRuntime(root)) as runtime:
        with open_session(root, store, runtime, counts, campaign=campaign) as (session, expect):
            runner = runner_for(session, expect, campaign)
            signal = dict(session.gateway.state.runtime['last_signal'], signal_id='budget-exhausted', target_weight=D(0))
            session.controller._atomic(lambda: session.gateway.state.runtime.update(pending=signal))
            with pytest.raises(SafetyBlocked, match='plan total order limit|budget exhausted'):
                runner.quote(Quote('NVDAUSDT', D(100), AT))
            assert runtime.post_count(campaign['id']) == counts['POST'] == 3
            assert runner.quote(Quote('NVDAUSDT', D(100), AT)) is None
