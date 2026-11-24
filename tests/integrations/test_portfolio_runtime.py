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
