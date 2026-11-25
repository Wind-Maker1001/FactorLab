from decimal import Decimal as D
import sqlite3

import pytest

from factorlab.trading.portfolio_execution import PortfolioExecutor, PortfolioStore
from test_portfolio_execution import Broker, NOW, run


def allocate(executor, owner, capital, cash=None):
    executor.allocate(account_id='paper', strategy_id=owner, allocation_id=owner + '-cash',
                      capital=D(capital), initial_cash=D(capital if cash is None else cash),
                      official_equity=D(100))


def plan(executor, owner, **kwargs):
    return executor.create(account_id='paper', strategy_id=owner, strategy_version='1',
        trading_session='2026-11-24', weights={'AAPL': D(1)}, owned_symbols=['AAPL'], now=NOW, **kwargs)


def test_shared_symbol_preserves_cash_inventory_and_restart_identity(tmp_path):
    broker = Broker()
    path = tmp_path / 'portfolio.sqlite3'
    store = PortfolioStore(path)
    executor = PortfolioExecutor(broker, store, clock=lambda: NOW)
    allocate(executor, 'one', '40')
    allocate(executor, 'two', '60')
    first, second = plan(executor, 'one'), plan(executor, 'two')
    run(executor, first)
    run(executor, first)
    run(executor, second)
    run(executor, second)
    assert store.holdings('paper', 'one') == {'AAPL': D(4)}
    assert store.holdings('paper', 'two') == {'AAPL': D(6)}
    assert broker.positions['AAPL'] == D(10)
    assert store.cash('paper', 'one') == store.cash('paper', 'two') == 0
    assert executor.report(first, official_equity=D(100)).positions[0].owned_quantity == D(4)
    store.close()
    store = PortfolioStore(path)
    executor = PortfolioExecutor(broker, store, clock=lambda: NOW)
    allocate(executor, 'one', '40')
    assert plan(executor, 'one') == first
    assert run(executor, first).phase == 'COMPLETE'
    assert len(broker.posts) == 2
    with pytest.raises(ValueError, match='owned inventory'):
        plan(executor, 'one', rebalance_id='sell-too-much', quantity_deltas={'AAPL': D(-5)})
    store.close()


def test_partial_fill_reserves_only_its_sleeve_and_blocks_overlapping_broker_order(tmp_path):
    broker = Broker()
    store = PortfolioStore(tmp_path / 'portfolio.sqlite3')
    executor = PortfolioExecutor(broker, store, clock=lambda: NOW)
    allocate(executor, 'one', '40')
    allocate(executor, 'two', '60')
    first, second = plan(executor, 'one'), plan(executor, 'two')
    broker.mode = 'partial'
    run(executor, first)
    assert store.cash('paper', 'one') == 0
    assert store.cash('paper', 'two') == D(60)
    projection = executor.projection(broker.read_account(), 'one')
    asset = next(asset for asset in projection.assets if asset.currency == 'USD')
    assert asset.total == asset.reserved == D(20)
    assert executor.report(first, official_equity=D(100), readonly=True).available_cash == 0
    assert run(executor, second).reasons['AAPL'] == 'FOREIGN_ORDER_CONFLICT'
    executor.cancel(first, broker.posts[0].client_order_id)
    assert store.cash('paper', 'one') == D(20)
    broker.mode = 'fill'
    run(executor, second)
    assert store.holdings('paper', 'one')['AAPL'] == D(2)
    assert store.holdings('paper', 'two')['AAPL'] == D(6)
    store.close()


def test_split_and_dividend_are_atomic_across_same_symbol_sleeves(tmp_path):
    broker = Broker()
    store = PortfolioStore(tmp_path / 'portfolio.sqlite3')
    executor = PortfolioExecutor(broker, store, clock=lambda: NOW)
    allocate(executor, 'one', '40')
    allocate(executor, 'two', '60')
    for owner in ('one', 'two'):
        identity = plan(executor, owner)
        run(executor, identity)
        run(executor, identity)
    broker.positions['AAPL'] *= 2
    def action(cash):
        return dict(id='official-split', symbol='AAPL', quantity_ratio=D(2),
                    basis_ratio=D(1), cash_flow=D(cash), evidence={'activity': 'SSP'})
    args = dict(account_id='paper', by_strategy={'one': [action(0)], 'two': [action(0)]})
    executor.record_account_corporate_actions(**args)
    executor.record_account_corporate_actions(**args)
    assert store.holdings('paper') == {'AAPL': D(20)}
    assert store.holdings('paper', 'one') == {'AAPL': D(8)}
    for owner, cash in (('one', '4'), ('two', '6')):
        executor.record_corporate_actions(account_id='paper', strategy_id=owner,
            actions=[dict(action(cash), id='official-dividend', quantity_ratio=D(1))])
    assert store.cash('paper', 'one') == D(4)
    assert store.cash('paper', 'two') == D(6)
    assert store.db.execute('SELECT count(*) FROM portfolio_corporate_actions').fetchone()[0] == 4
    store.close()


def test_direct_order_keeps_original_identity_and_reservation_across_restart(tmp_path):
    from factorlab.trading.contracts import Market, OrderIntent, OrderType, Side
    broker = Broker()
    path = tmp_path / 'direct.sqlite3'
    store = PortfolioStore(path)
    executor = PortfolioExecutor(broker, store, clock=lambda: NOW)
    allocate(executor, 'one', '40')
    intent = OrderIntent(Market.BINANCE_EQUITY, 'AAPL', Side.BUY, OrderType.LIMIT,
                         D(3), D(10), 'original-direct-id', time_in_force='GTC')
    args = dict(account_id='paper', strategy_id='one', strategy_version='1',
                trading_session='2026-11-24', weights={}, owned_symbols=['AAPL'], now=NOW,
                rebalance_id='event-1', direct_orders=[(intent, D(10))])
    identity = executor.create(**args)
    broker.mode = 'timeout'
    run(executor, identity)
    store.close()
    store = PortfolioStore(path)
    executor = PortfolioExecutor(broker, store, clock=lambda: NOW)
    assert executor.create(**args) == identity
    assert run(executor, identity).phase == 'COMPLETE'
    assert [p.client_order_id for p in broker.posts] == ['original-direct-id']
    assert store.cash('paper', 'one') == D(10)
    assert store.holdings('paper', 'one') == {'AAPL': D(3)}
    store.close()


def test_direct_order_cannot_spend_another_sleeves_cash(tmp_path):
    from factorlab.trading.contracts import Market, OrderIntent, OrderType, Side
    broker = Broker()
    store = PortfolioStore(tmp_path / 'direct.sqlite3')
    executor = PortfolioExecutor(broker, store, clock=lambda: NOW)
    allocate(executor, 'one', '40')
    allocate(executor, 'two', '60')
    intent = OrderIntent(Market.BINANCE_EQUITY, 'AAPL', Side.BUY, OrderType.LIMIT,
                         D(5), D(10), 'overspend', time_in_force='GTC')
    with pytest.raises(ValueError, match='allocated cash'):
        executor.create(account_id='paper', strategy_id='one', strategy_version='1',
            trading_session='2026-11-24', weights={}, owned_symbols=['AAPL'], now=NOW,
            direct_orders=[(intent, D(10))])
    assert store.db.execute('SELECT count(*) FROM portfolio_orders').fetchone()[0] == 0
    assert broker.posts == []
    assert store.cash('paper', 'two') == D(60)
    store.close()


@pytest.mark.parametrize('quantities', [(D(7), D(7)), (D(4), D(6))])
def test_direct_sell_batch_reserves_only_owned_total_atomically(tmp_path, quantities):
    from factorlab.trading.contracts import Market, OrderIntent, OrderType, Side
    broker = Broker(cash=D(0), positions={'AAPL': D(10)})
    store = PortfolioStore(tmp_path / 'sell.sqlite3')
    executor = PortfolioExecutor(broker, store, clock=lambda: NOW)
    initial = executor.create(account_id='paper', strategy_id='one', strategy_version='1',
        trading_session='2026-11-24', weights={'AAPL': D(1)}, owned_symbols=['AAPL'],
        initial_inventory={'AAPL': (D(10), D(10))}, now=NOW)
    run(executor, initial)
    run(executor, initial)
    orders = [(OrderIntent(Market.BINANCE_EQUITY, 'AAPL', Side.SELL, OrderType.LIMIT,
        quantity, D(10), 'sell-' + str(index), time_in_force='GTC'), D(10))
        for index, quantity in enumerate(quantities)]
    args = dict(account_id='paper', strategy_id='one', strategy_version='1',
        trading_session='2026-11-24', weights={}, owned_symbols=['AAPL'], now=NOW,
        rebalance_id='sell-event', direct_orders=orders)
    if sum(quantities) > 10:
        with pytest.raises(ValueError, match='owned inventory'):
            executor.create(**args)
        assert store.db.execute('SELECT count(*) FROM portfolio_plans').fetchone()[0] == 1
        assert store.db.execute('SELECT count(*) FROM portfolio_orders').fetchone()[0] == 0
    else:
        identity = executor.create(**args)
        assert executor.create(**args) == identity
        assert len(store.orders(identity)) == 2
    assert broker.posts == []
    store.close()


def test_legacy_claim_migration_retains_rows_and_does_not_grant_sharing(tmp_path):
    path = tmp_path / 'portfolio.sqlite3'
    db = sqlite3.connect(path)
    db.execute('CREATE TABLE portfolio_claims (account TEXT NOT NULL, symbol TEXT NOT NULL, '
               'strategy TEXT NOT NULL, quantity TEXT NOT NULL, initial_quantity TEXT NOT NULL, '
               'entry_price TEXT NOT NULL, PRIMARY KEY(account,symbol))')
    db.execute("INSERT INTO portfolio_claims VALUES ('paper','AAPL','old','2','2','10')")
    db.commit()
    db.close()
    store = PortfolioStore(path)
    executor = PortfolioExecutor(Broker(), store, clock=lambda: NOW)
    assert store.holdings('paper', 'old') == {'AAPL': D(2)}
    with pytest.raises(ValueError, match='ownership conflict'):
        plan(executor, 'new')
    with pytest.raises(ValueError, match='existing strategy capital'):
        allocate(executor, 'new', '20')
    store.close()
