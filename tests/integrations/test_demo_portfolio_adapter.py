from decimal import Decimal as D
from types import SimpleNamespace

import pytest

from test_binance_demo_execution import setup
from factorlab.integrations.binance_futures_trading.demo_portfolio_adapter import DemoPortfolioAdapter
from factorlab.integrations.binance_futures_trading.protocol import SafetyBlocked
from factorlab.trading.portfolio_execution import SubmissionNotAttempted


def test_only_original_owned_symbol_is_used_for_absence_recovery():
    adapter, gateway, controller, _ = setup()
    looked_up = []
    session = SimpleNamespace(controller=controller, gateway=gateway, adapter=adapter,
        _protocol_ids=lambda: (),
        _require_absent=lambda client_id, **kwargs: looked_up.append((client_id, kwargs)))
    bridge = DemoPortfolioAdapter(session, capability=lambda *args: pytest.fail('no writes'))
    controller.store.db.execute('INSERT INTO demo_order_owners VALUES (?,?,?,?)',
        ('stable-original', 'instance-one', 'allocation-one', 'ETHUSDT'))
    try:
        with pytest.raises(SubmissionNotAttempted):
            bridge.query_order('stable-original')
        assert looked_up == [('stable-original', dict(symbol='ETHUSDT'))]
        with pytest.raises(SafetyBlocked, match='ownership'):
            bridge.query_order('foreign')
        assert not adapter.db.execute('SELECT 1 FROM futures_orders').fetchone()
    finally:
        controller.store.close()
        adapter.store.close()


def test_restart_cancellation_restores_original_order_capability():
    adapter, gateway, controller, _ = setup()
    authority, guard = object(), object()
    intent = SimpleNamespace(instrument_id='ETHUSDT')
    cancelled = []
    session = SimpleNamespace(controller=controller, gateway=gateway,
        adapter=SimpleNamespace(order=lambda client_id: SimpleNamespace(intent=intent)),
        client=SimpleNamespace(transport=SimpleNamespace(authorizations={})),
        _instance_guards={}, cancel=lambda client_id: cancelled.append(client_id))
    observed = []
    bridge = DemoPortfolioAdapter(session, capability=lambda offered, owner:
        observed.append((offered, owner)) or (authority, guard))
    bridge._confirmed = lambda client_id: 'confirmed-cancel'
    controller.store.db.execute('INSERT INTO demo_order_owners VALUES (?,?,?,?)',
        ('stable-exit', 'one', 'funded-one', 'ETHUSDT'))
    try:
        assert bridge.cancel_order('stable-exit') == 'confirmed-cancel'
        assert observed == [(intent, ('one', 'funded-one', 'ETHUSDT'))]
        assert session.client.transport.authorizations == {'stable-exit': authority}
        assert session._instance_guards == {'stable-exit': guard}
        assert cancelled == ['stable-exit']
    finally:
        controller.store.close()
        adapter.store.close()


def test_bridge_preserves_cumulative_execution_notional():
    from datetime import datetime, timezone
    from factorlab.trading import OrderEvent, OrderStatus
    event = OrderEvent('original', 'remote', OrderStatus.FILLED, D(2), None,
                       datetime.now(timezone.utc), 'rest', 'confirmed', D('.02'))
    adapter, gateway, controller, _ = setup()
    session = SimpleNamespace(controller=controller, gateway=gateway, adapter=SimpleNamespace(
        _load=lambda client_id: (SimpleNamespace(snapshot=SimpleNamespace(reconcile_required=False)), D('201.5'))))
    gateway.state.confirmations['original'] = event
    bridge = DemoPortfolioAdapter(session, capability=lambda *args: pytest.fail('no writes'))
    try:
        recovered = bridge._confirmed('original')
        assert recovered.cumulative_filled_notional == D('201.5')
        assert recovered.cumulative_fee == D('.02')
        assert recovered.fill_price is None
    finally:
        controller.store.close()
        adapter.store.close()
