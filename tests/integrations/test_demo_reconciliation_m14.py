"""Frozen offline recovery adversaries; no exchange writes or credentials."""
from dataclasses import replace
from decimal import Decimal as D

import pytest

from factorlab.errors import DataQualityError
from factorlab.integrations.binance_futures_trading.adapter import FuturesAdapter
from factorlab.integrations.binance_futures_trading.fake import FakeTransport, NOW, MS
from factorlab.integrations.binance_futures_trading.protocol import Credentials, FuturesClient, SafetyBlocked
from factorlab.trading import OrderSnapshot, OrderStatus as S
from factorlab.trading.state import OrderRecord, unknown
from factorlab.trading.storage import PaperStore
from test_binance_futures_trading import intent


def historical(adapter, fake, key='late', index=1):
    snapshot = replace(OrderSnapshot(intent(key, limit='100')), status=S.CANCELED,
                       exchange_order_id=str(index))
    with adapter.store.transaction():
        adapter._save(OrderRecord(snapshot, NOW, D('100'), S.CANCELED), D('0'))
    fake.orders[key] = dict(clientOrderId=key, orderId=index, symbol='NVDAUSDT',
        side='BUY', type='LIMIT', positionSide='BOTH', origQty='2', executedQty='0',
        cumQuote='0', status='CANCELED', updateTime=MS)


def adapter_at(path, fake, account='offline'):
    client = FuturesClient(Credentials('fixture-key', 'fixture-secret'), transport=fake,
                           clock=lambda: NOW.timestamp())
    return FuturesAdapter(client, PaperStore(path), account_id=account, clock=lambda: NOW)


@pytest.fixture
def recovery(tmp_path):
    fake = FakeTransport()
    adapter = adapter_at(tmp_path / 'protocol.sqlite', fake)
    historical(adapter, fake)
    yield adapter, fake
    adapter.store.close()


def test_canceled_late_fill_and_restart_preserve_fee(recovery, tmp_path):
    adapter, fake = recovery
    adapter.reconcile()
    fake.trade_update('late', '2', '100')
    adapter.reconcile()
    expected = adapter._load('late')
    assert expected[0].snapshot.status is S.FILLED
    assert expected[0].snapshot.cumulative_fee == D('.2')
    before = adapter.db.execute('SELECT count(*) FROM futures_events').fetchone()[0]
    adapter.reconcile()
    assert adapter._load('late') == expected
    assert adapter.db.execute('SELECT count(*) FROM futures_events').fetchone()[0] == before
    restored = adapter_at(tmp_path / 'protocol.sqlite', fake)
    try:
        restored.reconcile()
        assert restored._load('late') == expected
    finally:
        restored.store.close()
    assert all(method == 'GET' for method, _, _ in fake.calls)


@pytest.mark.parametrize('corruption', ['missing', 'duplicate', 'asset', 'quote', 'fee_correction', 'status'])
def test_history_conflict_closes_gate_without_overwriting_confirmation(recovery, corruption):
    adapter, fake = recovery
    fake.trade_update('late', '2')
    adapter.reconcile()
    expected = adapter._load('late')
    if corruption == 'missing':
        fake.trades.clear()
    elif corruption == 'duplicate':
        fake.trades.append(dict(fake.trades[0]))
    elif corruption == 'asset':
        fake.trades[0]['commissionAsset'] = 'BNB'
    elif corruption == 'quote':
        fake.trades[0]['quoteQty'] = '199'
    elif corruption == 'fee_correction':
        fake.trades[0]['commission'] = '.3'
    else:
        fake.orders['late']['status'] = 'CANCELED'
    with pytest.raises((DataQualityError, ValueError)):
        adapter.reconcile()
    assert not adapter.reconciled
    assert adapter._load('late') == (unknown(expected[0]), expected[1])
    with pytest.raises(SafetyBlocked):
        adapter.submit_order(intent('new-risk'))
    assert all(method == 'GET' for method, _, _ in fake.calls)


def test_partial_disconnect_and_trade_order_do_not_invent_missing_fills(recovery):
    adapter, fake = recovery
    fake.orders['late']['status'] = 'NEW'
    # The local canceled confirmation cannot regress to NEW.
    with pytest.raises(ValueError):
        adapter.reconcile()
    fake.trade_update('late', '1')
    fake.trade_update('late', '1')
    fake.trades.reverse()
    assert adapter.reconcile().reconciled
    assert adapter.order('late').cumulative_filled_quantity == D('2')
    assert adapter.order('late').cumulative_fee == D('.2')


def test_full_trade_page_is_not_evidence_of_completeness(recovery):
    adapter, fake = recovery
    fake.trade_update('late', '2')
    fake.trades = [dict(fake.trades[0], id=index) for index in range(1000)]
    with pytest.raises(DataQualityError, match='pagination'):
        adapter.reconcile()
    assert not adapter.reconciled
    assert adapter.order('late').cumulative_filled_quantity == 0


def test_restart_account_identity_cannot_refresh_history(recovery, tmp_path):
    adapter, fake = recovery
    adapter.reconcile()
    other = PaperStore(tmp_path / 'protocol.sqlite')
    try:
        with pytest.raises(ValueError, match='account mismatch'):
            FuturesAdapter(adapter.client, other, account_id='other')
    finally:
        other.close()
    assert adapter.order('late').status is S.CANCELED


def test_unexplained_income_does_not_reconcile_controller(recovery):
    from test_binance_futures_trading import gateway_setup
    adapter, fake = recovery
    gateway, controller = gateway_setup(adapter)
    try:
        assert gateway.recover(controller).reconciled
        fake.cash += D('1')
        assert not gateway.recover(controller).reconciled
        assert controller.account().assets[0].total == D('1000')
        assert all(method == 'GET' for method, _, _ in fake.calls)
    finally:
        controller.store.close()
