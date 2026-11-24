from decimal import Decimal as D
from types import SimpleNamespace
from datetime import datetime, timezone

import pytest

from factorlab.trading.contracts import AccountSnapshot, AssetBalance, Market
from factorlab.trading.portfolio_runtime import PortfolioRuntime
from factorlab.trading.storage import PaperStore


def runtime(path):
    store = PaperStore(str(path))
    account = AccountSnapshot(Market.BINANCE_USDM, 'a', (AssetBalance('USDT', D(100)),),
                              (), (), datetime.now(timezone.utc), True)
    gateway = SimpleNamespace(state=SimpleNamespace(orders={}, fee_rate=D(0)))
    controller = SimpleNamespace(store=store, broker=gateway)
    return PortfolioRuntime(controller, allocation_id='test', owned_symbols={'QQQUSDT', 'SPYUSDT'},
                            recover=lambda: account)


def test_restart_after_lost_post_never_resubmits(tmp_path):
    path = tmp_path / 'controller.sqlite'
    first = runtime(path)
    first.begin('2026-W48', weights={'QQQUSDT': D('.5'), 'SPYUSDT': D('.5')}, equity=D(100))
    calls = []
    def lost(intent):
        calls.append(intent)
        raise TimeoutError('POST response lost')
    arguments = dict(prices={'QQQUSDT': D(10), 'SPYUSDT': D(10)},
                     steps={'QQQUSDT': D(1), 'SPYUSDT': D(1)})
    with pytest.raises(TimeoutError):
        first.advance('2026-W48', submit=lost, **arguments)
    first.controller.store.close()
    second = runtime(path)
    assert not second.begin('2026-W48', weights={'QQQUSDT': D('.5'), 'SPYUSDT': D('.5')}, equity=D(100))
    assert second.advance('2026-W48', submit=calls.append, **arguments) == 'RECONCILE_REQUIRED'
    assert len(calls) == 1
    second.controller.store.close()


def test_two_symbols_one_wallet(tmp_path):
    instance = runtime(tmp_path / 'a.sqlite')
    instance.begin('week', weights={'QQQUSDT': D('.5'), 'SPYUSDT': D('.5')}, equity=D(100))
    calls = []
    assert instance.advance('week', prices={'QQQUSDT': D(10), 'SPYUSDT': D(10)},
        steps={'QQQUSDT': D(1), 'SPYUSDT': D(1)}, submit=calls.append) == 'SUBMITTED'
    assert {i.instrument_id for i in calls} == {'QQQUSDT', 'SPYUSDT'}
    assert sum(i.quantity * i.limit_price for i in calls) == 100
    instance.controller.store.close()

@pytest.mark.parametrize('crash_at', range(3))
@pytest.mark.parametrize('after_dispatch', [False, True])
def test_resume_each_member_from_protocol_boundary(tmp_path, crash_at, after_dispatch):
    import sqlite3
    from factorlab.trading.contracts import OrderSnapshot, OrderStatus, Position
    from factorlab.trading.storage import decode, dumps
    import json
    path = tmp_path / 'controller.sqlite'
    protocol = sqlite3.connect(tmp_path / 'protocol.sqlite', isolation_level=None)
    protocol.execute('CREATE TABLE futures_orders (id TEXT PRIMARY KEY, payload TEXT)')
    calls = []
    symbols = ('QQQUSDT', 'SPYUSDT', 'TSLAUSDT')
    arguments = dict(prices={s:D(10) for s in symbols}, steps={s:D(1) for s in symbols})
    def open_runtime():
        value = runtime(path)
        value.owned_symbols = frozenset(symbols)
        value.gateway.adapter = SimpleNamespace(db=protocol)
        def recover():
            positions = []
            for key, raw in protocol.execute('SELECT id,payload FROM futures_orders'):
                intent = decode(json.loads(raw))
                positions.append(Position(intent.instrument_id, intent.quantity, D(10), D(10), 'USDT'))
                value.gateway.state.orders[key] = SimpleNamespace(
                    snapshot=OrderSnapshot(intent, status=OrderStatus.FILLED))
            return AccountSnapshot(Market.BINANCE_USDM, 'a',
                (AssetBalance('USDT', D(100)),), tuple(positions), (), datetime.now(timezone.utc), True)
        value.recover_account = recover
        return value
    first = open_runtime()
    first.begin('week', weights={s:D('.3') for s in symbols}, equity=D(100))
    attempt = 0
    def submit(intent):
        nonlocal attempt
        current = attempt
        attempt += 1
        if current == crash_at and not after_dispatch:
            raise RuntimeError('crash before adapter journal')
        protocol.execute('INSERT INTO futures_orders VALUES (?,?)',
                         (intent.client_order_id, dumps(intent)))
        calls.append(intent.client_order_id)
        if current == crash_at:
            raise RuntimeError('crash after adapter journal')
    with pytest.raises(RuntimeError):
        first.advance('week', submit=submit, **arguments)
    first.controller.store.close()
    second = open_runtime()
    def continue_submit(intent):
        protocol.execute('INSERT INTO futures_orders VALUES (?,?)',
                         (intent.client_order_id, dumps(intent)))
        calls.append(intent.client_order_id)
    result = second.advance('week', submit=continue_submit, **arguments)
    assert result == ('NO_EXECUTABLE_DELTA' if after_dispatch and crash_at == 2 else 'SUBMITTED')
    assert len(calls) == len(set(calls)) == 3
    second.controller.store.close()
    protocol.close()
