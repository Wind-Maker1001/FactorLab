from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from factorlab.experiments.grid_execution_audit import _order_receipts
from factorlab.experiments.mc_grid_tail_f04c_cases import (
    CAPITAL, FEE_RATE, SYMBOL, _replay_receipts,
)
from factorlab.trading.contracts import Market, OrderIntent, OrderType, Side
from factorlab.trading.paper import Instrument, PaperBroker, Quote
from factorlab.trading.storage import PaperStore


D = Decimal
START = datetime(2026, 5, 15, tzinfo=timezone.utc)
STEP = timedelta(minutes=1)
END = START + 6 * STEP


def native_funding(broker):
    asset = broker.read_account().assets[0]
    position = broker.state.positions.get(SYMBOL)
    position_collateral = abs(position.quantity) * position.entry_price if position else D(0)
    return dict(wallet_cash=asset.total, reserved_collateral=asset.reserved,
                available_wallet_cash=asset.available, position_collateral=position_collateral,
                pending_reservations=asset.reserved - position_collateral, committed=asset.reserved)


def native_receipts(tmp_path, *, leave_open=False):
    broker = PaperBroker(Market.BINANCE_USDM, 'reservation-test',
        (Instrument(SYMBOL, 'USDT'),), {'USDT': CAPITAL}, fee_rate=FEE_RATE)
    store = PaperStore(tmp_path / 'paper.sqlite')
    broker.record_event = store.record_event
    now = START
    broker.clock = lambda: now
    orders = {}
    minute_index = {}
    references = {}
    snapshots = {}

    def quote(price):
        broker.set_quote(Quote(SYMBOL, D(price), now))
        minute_index[now.isoformat()] = dict(close=D(price))

    def submit(order_id, quantity, limit=None):
        order_type = OrderType.MARKET if limit is None else OrderType.LIMIT
        intent = OrderIntent(Market.BINANCE_USDM, SYMBOL, Side.BUY, order_type, D(quantity),
                            D(limit) if limit is not None else None, order_id)
        broker.submit_order(intent)
        orders[order_id] = dict(grid_level=0, purpose='test')

    def fill(quantity, price):
        event = broker.fill('low', D(quantity), D(price))
        references[event.event_id] = broker.state.quotes[SYMBOL].price

    try:
        with store.transaction():
            quote(100)
            submit('low', 1, 90)
            submit('high', '1.5', 120)
            submit('market', '.5')
            snapshots['submitted'] = native_funding(broker)
            now += STEP
            quote(85)
            fill('.25', 85)
            snapshots['partial'] = native_funding(broker)
            now += STEP
            quote(86)
            broker.reject('high')
            snapshots['rejected'] = native_funding(broker)
            now += STEP
            quote(87)
            if not leave_open:
                broker.cancel_order('market')
            snapshots['canceled'] = native_funding(broker)
            now += STEP
            quote(88)
            fill('.75', 88)
            snapshots['filled'] = native_funding(broker)
            now += STEP
            quote(89)
            store.save(broker.state)
        broker.state = store.load()
        receipts, _, _ = _order_receipts(broker.state, store.order_events(),
                                          dict(orders=orders), references)
        return dict(orders=receipts), minute_index, snapshots, native_funding(broker)
    finally:
        store.close()


@pytest.mark.parametrize('leave_open', [False, True], ids=['closed', 'working-market'])
def test_replay_projects_native_reservations_through_order_lifecycle(tmp_path, leave_open):
    account, minutes, native, native_terminal = native_receipts(tmp_path, leave_open=leave_open)
    assert {key: order['reservation_price'] for key, order in account['orders'].items()} == {
        'low': D(100), 'high': D(120), 'market': D(100),
    }
    assert native['submitted']['reserved_collateral'] == D('330.1320')
    assert native['partial']['pending_reservations'] == D('305.1220')
    assert native['partial']['reserved_collateral'] - native['rejected']['reserved_collateral'] == D('180.072')
    assert native['rejected']['reserved_collateral'] - native['canceled']['reserved_collateral'] == (
        D(0) if leave_open else D('50.020'))
    assert native_terminal['pending_reservations'] == (D('50.020') if leave_open else D(0))

    for offset, before, after in ((1, 'submitted', 'partial'), (2, 'partial', 'rejected'),
                                  (3, 'rejected', 'canceled'), (4, 'canceled', 'filled')):
        _, snapshots, terminal = _replay_receipts(account, minutes, {}, START, END,
                                                  START + offset * STEP)
        for boundary, expected in (('before', native[before]), ('after', native[after])):
            assert {key: snapshots[boundary][key] for key in expected} == expected
            assert all(order['reservation_price'] == account['orders'][order['order_id']]['reservation_price']
                       for order in snapshots[boundary]['working_orders'])
        assert dict(wallet_cash=terminal['cash'], reserved_collateral=terminal['committed'],
                    available_wallet_cash=terminal['available'],
                    position_collateral=terminal['position_collateral'],
                    pending_reservations=terminal['pending_reservations'],
                    committed=terminal['committed']) == native_terminal


@pytest.mark.parametrize('bad_price', [None, 'not a price', 'NaN', 'Infinity', '0', '-1', '89'])
def test_replay_rejects_missing_or_invalid_native_reservation(tmp_path, bad_price):
    account, minutes, _, _ = native_receipts(tmp_path)
    low = account['orders']['low']
    if bad_price is None:
        del low['reservation_price']
    else:
        low['reservation_price'] = bad_price
    with pytest.raises(ValueError, match='reservation_price.*low'):
        _replay_receipts(account, minutes, {}, START, END, START + STEP)
