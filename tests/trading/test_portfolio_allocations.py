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
    assert run(executor, second).reasons['AAPL'] == 'SYMBOL_ORDER_PENDING'
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


def shared_executor(tmp_path, *, broker=None, fee_rate=D(0)):
    from types import SimpleNamespace
    from factorlab.trading.capital import CapitalPolicyMode
    broker = Broker() if broker is None else broker
    store = PortfolioStore(tmp_path / 'shared.sqlite3')
    executor = PortfolioExecutor(broker, store, clock=lambda: NOW)
    policy = executor.capital_policy
    policy.configure(account_id='paper', currency='USD', budget=D(100), fee_rate=fee_rate,
                     mode=CapitalPolicyMode.SHARED_SAME_DIRECTION)
    for owner, amount in [('one', '40'), ('two', '60')]:
        allocate(executor, owner, amount)
        spec = SimpleNamespace(account_id='paper', market='BINANCE_EQUITY', strategy_id='package',
            capital={'currency': 'USD', 'requested': D(amount)}, symbols=['AAPL'])
        policy.qualify(spec, dict(account_id='paper', instance_id=owner, strategy_id='package',
            market='BINANCE_EQUITY', allocation_id=owner + '-cash'))
    return broker, store, executor


def direct_sell(executor, name, quantity):
    from factorlab.trading.contracts import Market, OrderIntent, OrderType, Side
    intent = OrderIntent(Market.BINANCE_EQUITY, 'AAPL', Side.SELL, OrderType.LIMIT,
        D(quantity), D(10), name, time_in_force='GTC')
    return executor.create(account_id='paper', strategy_id='one', strategy_version='1',
        trading_session='2026-11-24', rebalance_id=name, weights={}, owned_symbols=['AAPL'],
        now=NOW, direct_orders=[(intent, D(10))], concurrent_direct_orders=True)


def test_released_flat_history_does_not_block_exclusive_create_or_corporate_child(tmp_path):
    from factorlab.trading.capital import CapitalPolicyMode
    broker, store, executor = shared_executor(tmp_path)
    for owner in ('one', 'two'):
        identity = executor.create(account_id='paper', strategy_id=owner, strategy_version='1',
            trading_session='2026-11-24', weights={}, owned_symbols=['AAPL', 'CHILD'],
            quantity_deltas={'AAPL': D(0), 'CHILD': D(0)}, now=NOW)
        assert run(executor, identity).phase == 'COMPLETE'
    with store.transaction():
        executor.capital_policy.release('paper', 'one', 'AAPL')
        executor.capital_policy.release('paper', 'one', 'CHILD')
    executor.capital_policy.configure(account_id='paper', currency='USD', budget=D(100),
        fee_rate=D(0), mode=CapitalPolicyMode.EXCLUSIVE)
    identity = plan(executor, 'two', rebalance_id='exclusive-buy', quantity_deltas={'AAPL': D(2)})
    run(executor, identity)
    assert run(executor, identity).phase == 'COMPLETE'
    broker.positions['CHILD'] = D(1)
    executor.record_corporate_actions(account_id='paper', strategy_id='two', actions=[
        dict(id='history-child', symbol='AAPL', quantity_ratio=D(1), basis_ratio=D(1),
             cash_flow=D(0), evidence={'activity': 'official'},
             allocations=[dict(symbol='CHILD', quantity=D(1), basis_fraction=D(0))])])
    assert store.holdings('paper', 'two') == {'AAPL': D(2), 'CHILD': D(1)}
    assert store.db.execute('SELECT quantity FROM portfolio_claims WHERE account=? AND strategy=? AND symbol=?',
        ('paper', 'one', 'AAPL')).fetchone() == ('0',)
    store.close()


def test_shared_create_cannot_adopt_inventory_without_migration_receipt(tmp_path):
    _, store, executor = shared_executor(tmp_path)
    with pytest.raises(ValueError, match='explicit migration receipt'):
        plan(executor, 'one', initial_inventory={'AAPL': (D(2), D(10))})
    assert store.holdings('paper') == {}
    assert store.db.execute('SELECT count(*) FROM portfolio_plans').fetchone()[0] == 0
    store.close()


def test_shared_policy_sells_and_flat_switch_do_not_change_other_instance(tmp_path):
    broker, store, executor = shared_executor(tmp_path)
    first = plan(executor, 'one', quantity_deltas={'AAPL': D(2)})
    second = plan(executor, 'two', quantity_deltas={'AAPL': D(2)})
    for identity in [first, second]:
        run(executor, identity)
        run(executor, identity)
    other_orders = store.orders(second)
    other_cash = store.cash('paper', 'two')
    partial = plan(executor, 'one', rebalance_id='reduce', quantity_deltas={'AAPL': D(-1)})
    run(executor, partial)
    run(executor, partial)
    assert store.holdings('paper', 'one')['AAPL'] == D(1)
    assert store.holdings('paper', 'two')['AAPL'] == D(2)
    flat = plan(executor, 'one', rebalance_id='flat', quantity_deltas={'AAPL': D(-1)})
    run(executor, flat)
    run(executor, flat)
    executor.capital_policy.pause('paper', 'one')
    executor.transfer_flat_ownership(account_id='paper', from_strategy='one', to_strategy='two', symbols=['AAPL'])
    executor.transfer_flat_ownership(account_id='paper', from_strategy='one', to_strategy='two', symbols=['AAPL'])
    assert store.holdings('paper', 'two') == {'AAPL': D(2)}
    assert store.orders(second) == other_orders
    assert store.cash('paper', 'two') == other_cash
    assert not executor.capital_policy.snapshot('paper', 'two').paused
    assert broker.positions['AAPL'] == D(2)
    store.close()


def test_exit_reservations_span_plans_and_release_only_unfilled_quantity(tmp_path):
    broker, store, executor = shared_executor(tmp_path)
    initial = plan(executor, 'one')
    run(executor, initial)
    run(executor, initial)
    first = direct_sell(executor, 'sell-one', '3')
    with pytest.raises(ValueError, match='owned inventory'):
        direct_sell(executor, 'over-reserved', '2')
    assert store.available_inventory('paper', 'one', 'AAPL') == D(1)
    broker.mode = 'partial'
    run(executor, first)
    assert store.holdings('paper', 'one')['AAPL'] == D('2.5')
    assert store.available_inventory('paper', 'one', 'AAPL') == D(1)
    second = direct_sell(executor, 'sell-two', '1')
    assert run(executor, second).reasons['AAPL'] == 'SYMBOL_ORDER_PENDING'
    executor.cancel(first, 'sell-one')
    assert store.available_inventory('paper', 'one', 'AAPL') == D('1.5')
    broker.mode = 'fill'
    run(executor, second)
    assert store.holdings('paper', 'one')['AAPL'] == D('1.5')
    assert store.available_inventory('paper', 'one', 'AAPL') == D('1.5')
    store.close()


@pytest.mark.parametrize('external_symbol', ['AAPL', 'QQQ'])
def test_unknown_external_order_never_authorizes_more_shared_risk(tmp_path, external_symbol):
    from factorlab.trading.contracts import Market, OrderIntent, OrderSnapshot, OrderStatus, OrderType, Side
    broker, store, executor = shared_executor(tmp_path)
    unknown = OrderIntent(Market.BINANCE_EQUITY, external_symbol, Side.BUY, OrderType.LIMIT,
        D(1), D(10), 'unknown-external', time_in_force='GTC')
    broker.foreign = (OrderSnapshot(unknown, OrderStatus.SUBMITTED),)
    identity = plan(executor, 'one', quantity_deltas={'AAPL': D(1)})
    if external_symbol == 'AAPL':
        assert run(executor, identity).reasons['AAPL'] == 'FOREIGN_ORDER_CONFLICT'
    else:
        with pytest.raises(ValueError, match='unknown external orders'):
            run(executor, identity)
    assert broker.posts == []
    assert store.holdings('paper', 'one')['AAPL'] == 0
    store.close()


def test_known_uncertain_symbol_does_not_stop_another_symbol(tmp_path):
    from factorlab.trading.portfolio_execution import OrderAbsent
    broker, store, executor = shared_executor(tmp_path)
    first = plan(executor, 'one', quantity_deltas={'AAPL': D(2)})
    broker.mode = 'timeout'
    run(executor, first)
    original = broker.query_order
    lost = broker.posts[0].client_order_id

    def query(identity):
        if identity == lost:
            raise OrderAbsent(identity)
        return original(identity)

    broker.query_order = query
    broker.mode = 'fill'
    second = executor.create(account_id='paper', strategy_id='two', strategy_version='1',
        trading_session='2026-11-24', weights={}, owned_symbols=['QQQ'], now=NOW,
        quantity_deltas={'QQQ': D(1)})
    run(executor, second)
    assert [order.instrument_id for order in broker.posts] == ['AAPL', 'QQQ']
    assert store.holdings('paper', 'two') == {'QQQ': D(1)}
    assert store.orders(first)[0]['uncertain']
    assert store.cash('paper', 'one') == D(20)
    store.close()


def test_registered_market_buy_uses_its_original_reservation_price(tmp_path):
    from factorlab.trading.contracts import Market, OrderEvent, OrderIntent, OrderStatus, OrderType, Side

    class MarketBroker(Broker):
        def submit_order(self, intent):
            self.posts.append(intent)
            self.orders[intent.client_order_id] = intent
            event = OrderEvent(intent.client_order_id, 'broker-' + intent.client_order_id,
                OrderStatus.SUBMITTED, D(0), None, NOW, 'broker', intent.client_order_id + '-accepted')
            self.events[intent.client_order_id] = event
            return event

    broker, store, executor = shared_executor(tmp_path, broker=MarketBroker())
    first = OrderIntent(Market.BINANCE_EQUITY, 'AAPL', Side.BUY, OrderType.MARKET,
        D(1), None, 'market-buy', time_in_force='GTC')
    identity = executor.create(account_id='paper', strategy_id='one', strategy_version='1',
        trading_session='2026-11-24', weights={}, owned_symbols=['AAPL'], now=NOW,
        direct_orders=[(first, D(10))])
    run(executor, identity)
    second = OrderIntent(Market.BINANCE_EQUITY, 'QQQ', Side.BUY, OrderType.LIMIT,
        D(1), D(10), 'other-buy', time_in_force='GTC')
    other = executor.create(account_id='paper', strategy_id='two', strategy_version='1',
        trading_session='2026-11-24', weights={}, owned_symbols=['QQQ'], now=NOW,
        direct_orders=[(second, D(10))])
    assert store.plan(other) is not None
    assert store.cash('paper', 'one') == D(30)
    assert store.cash('paper', 'two') == D(50)
    store.close()


def test_shared_rebalance_keeps_its_reduction_barrier(tmp_path):
    broker, store, executor = shared_executor(tmp_path)
    initial = plan(executor, 'one', quantity_deltas={'AAPL': D(2)})
    run(executor, initial)
    run(executor, initial)
    rebalance = executor.create(account_id='paper', strategy_id='one', strategy_version='1',
        trading_session='2026-11-24', rebalance_id='rotate', weights={},
        owned_symbols=['AAPL', 'QQQ'], now=NOW, quantity_deltas={'AAPL': D(-1), 'QQQ': D(1)})
    broker.mode = 'pending'
    run(executor, rebalance)
    assert run(executor, rebalance).phase == 'WAITING'
    assert [order.instrument_id for order in broker.posts] == ['AAPL', 'AAPL']
    sell = broker.posts[-1]
    from factorlab.trading.contracts import OrderStatus
    broker.confirm(sell.client_order_id, sell.quantity, OrderStatus.FILLED)
    broker.mode = 'fill'
    run(executor, rebalance)
    assert [order.instrument_id for order in broker.posts] == ['AAPL', 'AAPL', 'QQQ']
    store.close()


def test_shared_account_cash_is_not_reserved_twice_across_instances(tmp_path):
    from factorlab.trading.contracts import Market, OrderIntent, OrderType, Side
    broker, store, executor = shared_executor(tmp_path)
    broker.cash = D(50)

    def buy(owner, symbol):
        order = OrderIntent(Market.BINANCE_EQUITY, symbol, Side.BUY, OrderType.LIMIT,
            D(3), D(10), owner + '-cash', time_in_force='GTC')
        return executor.create(account_id='paper', strategy_id=owner, strategy_version='1',
            trading_session='2026-11-24', weights={}, owned_symbols=[symbol], now=NOW,
            direct_orders=[(order, D(10))])

    first = buy('one', 'AAPL')
    with pytest.raises(ValueError, match='available account cash'):
        buy('two', 'QQQ')
    assert store.cash('paper', 'one') == D(10)
    assert store.cash('paper', 'two') == D(60)
    assert len(store.account_orders('paper')) == 1
    assert store.plan(first) is not None
    store.close()


def test_partial_fee_settlement_and_cancel_release_only_remaining_reservation(tmp_path):
    from dataclasses import replace
    from factorlab.trading.contracts import Market, OrderIntent, OrderType, Side

    class FeeBroker(Broker):
        def confirm(self, identity, quantity, status):
            previous = self.events.get(identity)
            event = super().confirm(identity, quantity, status)
            fee = quantity * D('.1')
            self.cash -= fee - (previous.cumulative_fee if previous else D(0))
            self.events[identity] = replace(event, cumulative_fee=fee)
            return self.events[identity]

    broker, store, executor = shared_executor(tmp_path, broker=FeeBroker(), fee_rate=D('.01'))
    order = OrderIntent(Market.BINANCE_EQUITY, 'AAPL', Side.BUY, OrderType.LIMIT,
        D(3), D(10), 'fee-buy', time_in_force='GTC')
    identity = executor.create(account_id='paper', strategy_id='one', strategy_version='1',
        trading_session='2026-11-24', weights={}, owned_symbols=['AAPL'], now=NOW,
        direct_orders=[(order, D(10))])
    assert store.cash('paper', 'one') == D('9.70')
    broker.mode = 'partial'
    run(executor, identity)
    assert store.cash('paper', 'one', reserve=False) == D('24.85')
    assert store.cash('paper', 'one') == D('9.70')
    executor.cancel(identity, 'fee-buy')
    assert store.cash('paper', 'one') == D('24.85')
    assert store.cash('paper', 'two') == D(60)
    assert store.holdings('paper', 'one')['AAPL'] == D('1.5')
    run(executor, identity)
    assert store.cash('paper', 'one') == D('24.85')
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
