from contextlib import closing
from decimal import Decimal
import json
import os
import subprocess
import sys
from uuid import uuid4

import pytest

from test_binance_demo_network import NetworkFixture, wired
from factorlab.integrations.binance.client import Response
from factorlab.integrations.binance_futures_trading.demo_cli import open_session, smoke_lifecycle
from factorlab.integrations.binance_futures_trading.demo_credentials import DemoCredentialStore
from factorlab.integrations.binance_futures_trading.demo_runtime import SMOKE_CAMPAIGN, DemoRuntime, bind_account, now
from factorlab.integrations.binance_futures_trading.demo_session import demo_account_configuration, observe_demo_account
from factorlab.integrations.binance_futures_trading.fake import MS
from factorlab.integrations.binance_futures_trading.protocol import Credentials, Environment, SafetyBlocked
from factorlab.trading import Side


def test_current_user_credential_available_in_independent_process():
    if os.name != 'nt':
        pytest.skip('Windows Credential Manager')
    namespace = 'FactorLab-test-' + uuid4().hex
    store = DemoCredentialStore(namespace=namespace)
    try:
        store.write(Credentials('synthetic-demo-key', 'synthetic-demo-secret', Environment.DEMO))
        store.write_config({'account_id': 'test-binding'})
        subprocess.run([sys.executable, '-c', '''
import sys
from factorlab.integrations.binance_futures_trading.demo_credentials import DemoCredentialStore
from factorlab.integrations.binance_futures_trading.protocol import Credentials, Environment
store = DemoCredentialStore(namespace=sys.argv[1])
assert store.read() == Credentials('synthetic-demo-key', 'synthetic-demo-secret', Environment.DEMO)
assert store.read_config() == {'account_id': 'test-binding'}
''', namespace], check=True)
    finally:
        store.delete()
        store.delete_config()


class MemoryCredentialStore:
    reference = 'synthetic'

    def __init__(self):
        self.credential = None
        self.config = None

    def read(self):
        return self.credential

    def write(self, credential):
        self.credential = credential

    def read_config(self):
        return self.config

    def write_config(self, config):
        self.config = config


@pytest.mark.parametrize(('lose_entry_reply', 'order_budget'), [(False, 1), (False, 2), (True, 2)])
def test_demo_smoke_lifecycle_and_reopen(tmp_path, monkeypatch, wired, lose_entry_reply, order_budget):
    original = NetworkFixture.__call__

    def filled_exchange(self, request, timeout):
        if request.path.endswith('bookTicker'):
            return Response(200, {}, b'{"symbol":"NVDAUSDT","bidPrice":"160","askPrice":"160","time":%d}' % MS)
        if request.path.endswith('premiumIndex'):
            return Response(200, {}, b'{"symbol":"NVDAUSDT","markPrice":"160","time":%d}' % MS)
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
        if request.method == 'POST' and response.status == 200:
            order = json.loads(response.body)
            client_id = order['clientOrderId']
            self.trade_update(client_id, order['origQty'], '160')
            if lose_entry_reply and order['side'] == 'BUY':
                raise TimeoutError('synthetic lost reply')
            return Response(200, {}, json.dumps(self.orders[client_id]).encode())
        return response

    monkeypatch.setattr(NetworkFixture, '__call__', filled_exchange)
    store = MemoryCredentialStore()
    credential = Credentials('demo-key', 'demo-secret', Environment.DEMO)
    facts, _ = observe_demo_account(credential)
    binding = bind_account(store, credential, facts, demo_account_configuration(facts))
    counts = dict(GET=0, POST=0, DELETE=0)
    with closing(DemoRuntime(tmp_path)) as runtime:
        campaign = runtime.start(binding, seconds=900, orders=order_budget, max_loss=Decimal('1'),
            opening_equity=Decimal('1000'), stop_epoch=runtime.stop_epoch(), started_at=now(), smoke_id=SMOKE_CAMPAIGN)
        with open_session(tmp_path, store, runtime, counts, campaign=campaign) as (session, expect):
            if lose_entry_reply:
                with pytest.raises(SafetyBlocked, match='response was uncertain'):
                    smoke_lifecycle(session, expect, campaign)
            else:
                result = smoke_lifecycle(session, expect, campaign)
                assert result['status'] == 'DEMO_FILL_RECONCILED'
                assert bool(result['account']['positions']) is (order_budget == 1)
        runtime.stop()

    with closing(DemoRuntime(tmp_path)) as runtime:
        assert runtime.active_campaign() is None
        with pytest.raises(SafetyBlocked, match='already started'):
            runtime.start(binding, seconds=900, orders=2, max_loss=Decimal('1'),
                opening_equity=Decimal('1000'), stop_epoch=runtime.stop_epoch(), started_at=now(), smoke_id=SMOKE_CAMPAIGN)
        assert runtime.post_count(campaign['id']) == (1 if lose_entry_reply else order_budget)
        before = counts['POST']
        with open_session(tmp_path, store, runtime, counts) as (session, _):
            restored = session.recover()
            assert restored['reconciled']
            assert sum(order['fee_usdt'] for order in restored['orders'].values()) == Decimal('0.0064') * before
            assert restored['orders'][campaign['prefix'] + '-01']['average_fill_price'] == Decimal('160')
            assert bool(restored['positions']) is (lose_entry_reply or order_budget == 1)
        assert counts['POST'] == before
        store.credential = Credentials('different-key', 'demo-secret', Environment.DEMO)
        with pytest.raises(SafetyBlocked, match='account binding mismatch'):
            with open_session(tmp_path, store, runtime, counts):
                pytest.fail('different account opened its predecessor’s Demo journals')
        store.credential = None
        with pytest.raises(SafetyBlocked, match='credential missing'):
            with open_session(tmp_path, store, runtime, counts):
                pytest.fail('missing credentials opened a Demo session')
    assert counts['POST'] == before


def test_stopped_campaign_cannot_dispatch_with_a_running_capability(tmp_path, monkeypatch, wired):
    original = NetworkFixture.__call__

    def market(self, request, timeout):
        if request.path.endswith('bookTicker'):
            return Response(200, {}, b'{"symbol":"NVDAUSDT","bidPrice":"100","askPrice":"100","time":%d}' % MS)
        response = original(self, request, timeout)
        if request.path.endswith('exchangeInfo'):
            payload = json.loads(response.body)
            payload['symbols'][0]['timeInForce'] = ['GTC']
            for rule in payload['symbols'][0]['filters']:
                if rule['filterType'] == 'MIN_NOTIONAL':
                    rule['notional'] = '1'
            return Response(200, {}, json.dumps(payload).encode())
        return response

    monkeypatch.setattr(NetworkFixture, '__call__', market)
    store = MemoryCredentialStore()
    credential = Credentials('demo-key', 'demo-secret', Environment.DEMO)
    facts, _ = observe_demo_account(credential)
    binding = bind_account(store, credential, facts, demo_account_configuration(facts))
    counts = dict(GET=0, POST=0, DELETE=0)
    with closing(DemoRuntime(tmp_path)) as origin, closing(DemoRuntime(tmp_path)) as separate:
        stale_epoch = origin.stop_epoch()
        separate.stop()
        args = dict(seconds=900, orders=2, max_loss=Decimal('1'), opening_equity=Decimal('1000'), started_at=now())
        with pytest.raises(SafetyBlocked, match='stopped during'):
            origin.start(binding, stop_epoch=stale_epoch, **args)
        campaign = origin.start(binding, stop_epoch=origin.stop_epoch(), **args)
    with closing(DemoRuntime(tmp_path)) as resumed, closing(DemoRuntime(tmp_path)) as separate:
        restored = resumed.active_campaign()
        assert resumed.authority(binding, restored).plan.expires_at.isoformat() == campaign['expires_at']
        with open_session(tmp_path, store, resumed, counts, campaign=restored) as (session, expect):
            session.recover()
            separate.stop()
            client_id, quantity = restored['prefix'] + '-01', Decimal('0.04')
            expect(client_id, Side.BUY, quantity)
            with pytest.raises(SafetyBlocked, match='stopped or expired'):
                session.submit_limit(client_id, Side.BUY, quantity)
            assert counts['POST'] == resumed.post_count(restored['id']) == 0
            assert session.status()['orders'] == {}


def test_separate_smoke_approval_preserves_consumed_budget(tmp_path):
    binding = dict(account_id='synthetic-account', configuration={})
    with closing(DemoRuntime(tmp_path)) as runtime:
        def start(approval):
            return runtime.start(binding, seconds=900, orders=2, max_loss=Decimal('1'),
                opening_equity=Decimal('1000'), stop_epoch=runtime.stop_epoch(), started_at=now(), smoke_id=approval)

        first = start(SMOKE_CAMPAIGN)
        runtime.stop()
        next_approval = 'smoke-' + uuid4().hex
        second = start(next_approval)
        runtime.stop()
        assert first['prefix'] != second['prefix']
        for approval in (SMOKE_CAMPAIGN, next_approval):
            with pytest.raises(SafetyBlocked, match='already started'):
                start(approval)
