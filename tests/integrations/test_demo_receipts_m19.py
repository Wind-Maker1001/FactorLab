"""Exchange receipt persistence shares the existing confirmation transaction."""
import json
from datetime import datetime, timedelta

from factorlab.integrations.binance_futures_trading.demo_session import DemoSession
from factorlab.integrations.binance_futures_trading.fake import NOW

import pytest

from factorlab.errors import DataQualityError
from test_demo_reconciliation_m14 import adapter_at, historical
from factorlab.integrations.binance_futures_trading.fake import FakeTransport
from factorlab.integrations.binance_futures_trading.demo_fill_acceptance import receipt_mapping, fifo_evidence, capture, compare_recovery, _unknown_dispatch
from test_binance_demo_grid_network import network_grid, observe
from test_binance_demo_network import NetworkFixture, wired
from factorlab.integrations.binance.client import Response
from factorlab.integrations.binance_futures_trading.demo_runtime import open_grid_session
from decimal import Decimal as D


@pytest.mark.parametrize(('status', 'body', 'unknown'), [
    (None, None, True), (408, {}, True), (500, {}, True), (503, {}, True),
    (400, {'code': -1006}, True), (400, {'code': -1007}, True),
    (400, {'code': -4116}, True), (200, b'not-json', True),
    (200, {}, True), (200, [], True), (200, b'null', True),
    (200, {'orderId': 1, 'clientOrderId': 'wrong', 'symbol': 'NVDAUSDT', 'status': 'NEW'}, True),
    (400, {'code': -1021}, False), (400, {'code': -2019}, False),
    (400, {}, False), (200, None, False),
])
def test_unknown_dispatch_requires_its_own_later_get(
        network_grid, monkeypatch, status, body, unknown):
    root, store, factory, enable, fake, requests = network_grid
    current_time = NOW
    monkeypatch.setattr(DemoSession, '_now', lambda _: current_time)
    enable()
    original = NetworkFixture.__call__
    post_seen, query_enabled = False, False

    def response(self, request, timeout):
        nonlocal post_seen
        if post_seen and unknown and not query_enabled and request.method == 'GET' and request.path == '/fapi/v1/order':
            raise TimeoutError('recovery not yet available')
        actual = original(self, request, timeout)
        if request.method != 'POST':
            return actual
        post_seen = True
        if status == 400 and not unknown:
            fake.orders.clear()
        if status is None:
            raise TimeoutError('lost ACK')
        if body is None:
            return actual
        return Response(status, {}, body if isinstance(body, bytes) else json.dumps(body).encode())

    monkeypatch.setattr(NetworkFixture, '__call__', response)
    with open_grid_session(root, store, factory) as bridge:
        bridge.recover()
        observe(bridge)
        campaign = bridge.runtime.campaign(bridge.runtime.db.execute('SELECT campaign FROM dispatches').fetchone()[0])
        receipt, = bridge.runtime.receipts(campaign['id'])
        assert receipt['outcome_unknown'] is unknown
        assert bridge.account().inventory == bridge.account().turnover == 0
        # Export without generating any new GET evidence.
        monkeypatch.setattr('factorlab.integrations.binance_futures_trading.demo_fill_acceptance.recover',
                            lambda session: session.status())
        before = capture(bridge.session, bridge.runtime, D('1000'))
        assert not before['lifecycle']['unknown_ack_queried']
        assert not before['statuses']['DEMO_LIFECYCLE_VERIFIED']
        key = receipt['client_order_id']
        query_enabled = True
        bridge.recover()
        assert not capture(bridge.session, bridge.runtime, D('1000'))['lifecycle']['unknown_ack_queried']
        if unknown:
            observed_at, payload = bridge.session.adapter.db.execute(
                'SELECT observed_at,payload FROM futures_receipts WHERE client_id=?', (key,)).fetchone()
            assert observed_at == receipt['observed_at'] == NOW.isoformat()
            assert json.loads(payload)['order']['clientOrderId'] == key
            assert json.loads(payload)['order']['symbol'] == receipt['request_parameters']['symbol']
        current_time += timedelta(milliseconds=1)
        bridge.recover()
        after = capture(bridge.session, bridge.runtime, D('1000'))
        assert after['lifecycle']['unknown_ack_queried'] is unknown
        assert not after['statuses']['DEMO_LIFECYCLE_VERIFIED']
        if unknown:
            observed_at, payload = bridge.session.adapter.db.execute(
                'SELECT observed_at,payload FROM futures_receipts WHERE client_id=?', (key,)).fetchone()
            assert datetime.fromisoformat(observed_at) == current_time > datetime.fromisoformat(receipt['observed_at'])
            wrong_symbol = json.loads(payload)
            wrong_symbol['order']['symbol'] = 'BTCUSDT'
            bridge.session.adapter.db.execute('UPDATE futures_receipts SET payload=? WHERE client_id=?',
                                             (json.dumps(wrong_symbol), key))
            assert not capture(bridge.session, bridge.runtime, D('1000'))['lifecycle']['unknown_ack_queried']
            bridge.session.adapter.db.execute('UPDATE futures_receipts SET payload=? WHERE client_id=?', (payload, key))
        events = list(bridge.controller.store.order_events())
        current_time += timedelta(milliseconds=1)
        bridge.recover()
        assert list(bridge.controller.store.order_events()) == events
        if unknown:
            assert bridge.session.adapter.db.execute(
                'SELECT observed_at,payload FROM futures_receipts WHERE client_id=?', (key,)).fetchall() == [
                    (current_time.isoformat(), payload)]
        assert len([r for r in requests if r.get_method() == 'POST']) == 1
        bridge.runtime.db.execute("UPDATE dispatches SET client_id='unrelated'")
        assert not capture(bridge.session, bridge.runtime, D('1000'))['lifecycle']['unknown_ack_queried']
        bridge.runtime.db.execute('UPDATE dispatches SET client_id=?', (key,))
        # Neither pre-dispatch GETs nor legacy receipts establish recovery.
        saved = dict(receipt)
        saved['observed_at'] = '2099-01-01T00:00:00+00:00'
        bridge.runtime.record_receipt(campaign, saved)
        assert not capture(bridge.session, bridge.runtime, D('1000'))['lifecycle']['unknown_ack_queried']
        saved.pop('observed_at')
        saved.pop('outcome_unknown')
        parameters = json.loads(bridge.runtime.db.execute('SELECT parameters FROM dispatches').fetchone()[0])
        assert _unknown_dispatch(dict(receipt=saved, parameters=parameters)) is unknown
        bridge.runtime.record_receipt(campaign, saved)
        assert not capture(bridge.session, bridge.runtime, D('1000'))['lifecycle']['unknown_ack_queried']


def test_experiment_excludes_historical_fills_and_requires_new_recovery(network_grid, monkeypatch):
    from factorlab.integrations.binance_futures_trading.demo_fill_acceptance import experiment_evidence

    root, store, factory, enable, fake, requests = network_grid
    enable()
    with open_grid_session(root, store, factory) as bridge:
        monkeypatch.setattr(type(bridge.session.adapter), 'income', lambda self, symbol=None: ())
        bridge.recover()
        baseline = capture(bridge.session, bridge.runtime, D('1000'))
        observe(bridge)
        key = bridge.grid.active[0].intent.client_order_id
        fake.trade_update(key, '.04', '146')
        bridge.recover()
        bridge.dispatch()
        fake.trade_update(bridge.grid.active[0].intent.client_order_id, '.04', '154')
        bridge.recover()
        completed = capture(bridge.session, bridge.runtime, D('1000'))
        fresh = experiment_evidence(baseline, completed)
        assert fresh['statuses']['NEW_DEMO_ACK_VERIFIED']
        assert fresh['statuses']['NEW_DEMO_FILL_VERIFIED']
        assert fresh['statuses']['NEW_DEMO_FEE_RECONCILED']
        assert fresh['statuses']['NEW_DEMO_ACCOUNT_RECONCILED']
        assert fresh['actual_fees'] == D('.012')
        assert fresh['wallet_change'] == D('.308')
        assert not fresh['statuses']['NEW_DEMO_RESTART_RECOVERY_VERIFIED']
        historical = experiment_evidence(completed, completed)
        assert historical['statuses']['HISTORICAL_DEMO_ACCOUNT_RECONCILED']
        assert not any(value for name, value in historical['statuses'].items() if name.startswith('NEW_'))
        assert not historical['statuses']['DEMO_LIFECYCLE_VERIFIED']
    with open_grid_session(root, store, factory) as bridge:
        restored = capture(bridge.session, bridge.runtime, D('1000'))
        same_process = experiment_evidence(baseline, restored, previous_recovery=completed)
        assert not same_process['statuses']['NEW_DEMO_RESTART_RECOVERY_VERIFIED']
        monkeypatch.setattr('factorlab.integrations.binance_futures_trading.demo_fill_acceptance.os.getpid',
                            lambda: completed['recovery_proof']['process_id'] + 1)
        restored = capture(bridge.session, bridge.runtime, D('1000'))
        result = experiment_evidence(baseline, restored, previous_recovery=completed)
        assert result['statuses']['NEW_DEMO_RESTART_RECOVERY_VERIFIED']
        wrong_boundary = experiment_evidence(baseline, restored, previous_recovery=baseline)
        assert not wrong_boundary['statuses']['NEW_DEMO_RESTART_RECOVERY_VERIFIED']
        assert not wrong_boundary['restart_comparison']['experiment_orders_present_before_restart']
        assert not result['statuses']['DEMO_LIFECYCLE_VERIFIED']
        wrong = json.loads(json.dumps(restored, default=str))
        wrong['recovery_proof']['account_id'] = 'wrong'
        with pytest.raises(DataQualityError, match='binding changed'):
            experiment_evidence(baseline, wrong)


def test_actual_trade_receipts_survive_restart_and_repeated_get(tmp_path):
    path = tmp_path / 'protocol.sqlite'
    fake = FakeTransport()
    adapter = adapter_at(path, fake)
    historical(adapter, fake)
    fake.trade_update('late', '2', '100')
    adapter.reconcile()
    receipts = adapter.db.execute('SELECT id,payload FROM futures_receipts').fetchall()
    payload = json.loads(receipts[-1][1])
    assert payload['order']['executedQty'] == fake.orders['late']['executedQty']
    assert payload['trades'] == fake.trades
    mapping, = receipt_mapping(adapter)
    assert mapping['trade_ids'] == [trade['id'] for trade in fake.trades]
    assert mapping['trade_quantity_sum'] == D('2')
    assert mapping['actual_fee'] == D('.2')
    adapter.reconcile()
    assert adapter.db.execute('SELECT id,payload FROM futures_receipts').fetchall() == receipts
    adapter.store.close()
    restored = adapter_at(path, fake)
    try:
        restored.reconcile()
        assert restored.db.execute('SELECT id,payload FROM futures_receipts').fetchall() == receipts
    finally:
        restored.store.close()
    assert all(method == 'GET' for method, _, _ in fake.calls)


def test_conflicting_fee_asset_does_not_persist_a_confirmed_receipt(tmp_path):
    fake = FakeTransport()
    adapter = adapter_at(tmp_path / 'protocol.sqlite', fake)
    try:
        historical(adapter, fake)
        fake.trade_update('late', '1', '100')
        fake.trades[0]['commissionAsset'] = 'BNB'
        with pytest.raises(DataQualityError, match='non-USDT'):
            adapter.reconcile()
        assert adapter.db.execute('SELECT count(*) FROM futures_receipts').fetchone()[0] == 0
        assert adapter.order('late').cumulative_filled_quantity == 0
    finally:
        adapter.store.close()


def test_partial_actual_receipts_project_existing_fifo_without_rebooking(network_grid):
    root, store, factory, enable, fake, requests = network_grid
    enable()
    with open_grid_session(root, store, factory) as bridge:
        bridge.recover()
        observe(bridge)
        receipt, = bridge.session.receipts
        assert receipt['request_parameters']['symbol'] == 'NVDAUSDT'
        assert receipt['request_parameters']['timeInForce'] == 'GTC'
        assert not {'signature', 'timestamp', 'recvWindow', 'X-MBX-APIKEY'} & receipt['request_parameters'].keys()
        assert receipt['acknowledgement']['orderId'] == int(receipt['order_id'])
        buy = bridge.grid.active[0].intent
        fake.trade_update(buy.client_order_id, '.01', '146')
        bridge.recover()
        before = list(bridge.controller.store.order_events())
        mapping, = receipt_mapping(bridge.session.adapter)
        assert mapping['trade_quantity_sum'] == D('.01')
        exported = fifo_evidence(bridge.session, D('1000'))
        summary = exported['attribution']['summary']
        assert summary['residual_inventory'] == D('.01')
        assert summary['actual_fees'] == D('.00146')
        assert summary['reconciliation_residual'] == 0
        assert list(bridge.controller.store.order_events()) == before
    with open_grid_session(root, store, factory) as bridge:
        bridge.recover()
        assert fifo_evidence(bridge.session, D('1000')) == exported


def test_complete_export_requires_actual_trades_and_reconciles_fee_nav(network_grid, monkeypatch):
    root, store, factory, enable, fake, requests = network_grid
    enable()
    with open_grid_session(root, store, factory) as bridge:
        bridge.recover()
        observe(bridge)
        empty = capture(bridge.session, bridge.runtime, D('1000'))
        assert empty['statuses']['DEMO_ORDER_ACK_VERIFIED']
        assert not empty['statuses']['DEMO_TRADE_RECEIPT_VERIFIED']
        assert not empty['statuses']['DEMO_ACCOUNT_RECONCILED']
        buy = bridge.grid.active[0].intent
        fake.trade_update(buy.client_order_id, '.04', '146')
        bridge.recover()
        bridge.dispatch()
        sell = bridge.grid.active[0].intent
        fake.trade_update(sell.client_order_id, '.04', '154')
        bridge.recover()
        funded = capture(bridge.session, bridge.runtime, D('1000'))
        assert funded['funding']['booking'] == 'NOT_SUPPORTED'
        assert not funded['statuses']['DEMO_ACCOUNT_RECONCILED']
        monkeypatch.setattr(type(bridge.session.adapter), 'income', lambda self, symbol=None: ())
        report = capture(bridge.session, bridge.runtime, D('1000'))
        assert report['statuses']['DEMO_TRADE_RECEIPT_VERIFIED']
        assert report['statuses']['DEMO_FEE_RECONCILED']
        assert report['statuses']['DEMO_POSITION_RECONCILED']
        assert report['statuses']['DEMO_ACCOUNT_RECONCILED']
        assert report['actual_fees'] == D('.012')
        assert report['equity'] == D('1000.308')
        assert report['fifo']['attribution']['summary']['normal_sell']['net'] == D('.308')
        assert len(report['order_trade_mapping']) == 2
        assert report['funding']['events'] == []
        assert not report['statuses']['DEMO_LIFECYCLE_VERIFIED']
        assert not report['statuses']['DEMO_RESTART_RECOVERY_VERIFIED']
    with open_grid_session(root, store, factory) as bridge:
        restored = capture(bridge.session, bridge.runtime, D('1000'))
        assert all(compare_recovery(report, restored).values())
        assert restored['statuses']['DEMO_RESTART_RECOVERY_VERIFIED']
        altered = json.loads(json.dumps(restored, default=str))
        altered['recovery_proof']['dispatch_count'] += 1
        assert not compare_recovery(report, altered)['no_new_dispatch']
        assert not altered['statuses']['DEMO_RESTART_RECOVERY_VERIFIED']
