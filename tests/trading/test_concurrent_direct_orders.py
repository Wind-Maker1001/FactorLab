from dataclasses import replace
from decimal import Decimal as D

import pytest

from factorlab.trading import Market, OrderIntent, OrderType, Side
from test_capital_policy import setup
from test_portfolio_execution import NOW


def create(executor, name, amount='1', *, concurrent=True):
    order = OrderIntent(Market.BINANCE_EQUITY, 'AAPL', Side.BUY, OrderType.LIMIT,
        D(amount), D(10), name, time_in_force='GTC')
    return executor.create(account_id='paper', strategy_id='one', strategy_version='1',
        trading_session=name, weights={}, owned_symbols=['AAPL'], now=NOW,
        direct_orders=[(order, D(10))], concurrent_direct_orders=concurrent)


def test_explicit_direct_stream_reserves_all_working_plans_atomically(tmp_path):
    _, store, executor = setup(tmp_path)
    try:
        first = create(executor, 'first')
        second = create(executor, 'second', '2')
        assert first != second
        assert executor.capital_policy.snapshot('paper', 'one').reserved == D(30)
        with pytest.raises(ValueError, match='allocated cash'):
            create(executor, 'over-budget', '2')
        assert executor.capital_policy.snapshot('paper', 'one').reserved == D(30)
        assert len(store.db.execute('SELECT id FROM portfolio_plans').fetchall()) == 2
        assert create(executor, 'first') == first
        with pytest.raises(ValueError, match='previous rebalance'):
            create(executor, 'serial', concurrent=False)
        with pytest.raises(ValueError, match='previous rebalance'):
            executor.create(account_id='paper', strategy_id='one', strategy_version='1',
                trading_session='target', weights={'AAPL': D('.1')}, owned_symbols=['AAPL'], now=NOW)
    finally:
        store.close()


def test_stream_cannot_join_a_serial_active_plan(tmp_path):
    _, store, executor = setup(tmp_path)
    try:
        create(executor, 'serial', concurrent=False)
        with pytest.raises(ValueError, match='previous rebalance'):
            create(executor, 'stream')
    finally:
        store.close()
