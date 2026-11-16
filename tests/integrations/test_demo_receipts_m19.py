"""Exchange receipt persistence shares the existing confirmation transaction."""
import json

import pytest

from factorlab.errors import DataQualityError
from test_demo_reconciliation_m14 import adapter_at, historical
from factorlab.integrations.binance_futures_trading.fake import FakeTransport
from factorlab.integrations.binance_futures_trading.demo_fill_acceptance import receipt_mapping, fifo_evidence, capture, compare_recovery
from test_binance_demo_grid_network import network_grid, observe
from test_binance_demo_network import wired
from factorlab.integrations.binance_futures_trading.demo_runtime import open_grid_session
from decimal import Decimal as D


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
