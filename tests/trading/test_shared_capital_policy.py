from decimal import Decimal as D
import sqlite3

import pytest

from factorlab.trading.capital import CapitalPolicy, CapitalPolicyMode
from factorlab.trading.portfolio_execution import PortfolioExecutor, PortfolioStore
from test_capital_policy import setup
from test_portfolio_allocations import allocate, plan
from test_portfolio_execution import Broker, NOW, run
from factorlab.trading.contracts import Market, OrderIntent, OrderType, Side
from factorlab.trading.storage import dumps


SHARED = CapitalPolicyMode.SHARED_SAME_DIRECTION


def share(policy):
    policy.configure(account_id='paper', currency='USD', budget=D(100), fee_rate=D(0), mode=SHARED)


def claim(store, owner, symbol='AAPL'):
    with store.transaction():
        store.capital_policy.claim('paper', owner, symbol)


def test_shared_claims_preserve_identity_and_monotonic_symbol_limit(tmp_path):
    broker, store, executor = setup(tmp_path)
    policy = executor.capital_policy
    share(policy)
    claim(store, 'two')
    assert policy.symbol_limit('paper', 'AAPL') == D(60)
    claim(store, 'one')
    assert policy.symbol_limit('paper', 'AAPL') == D(40)
    owners = policy.ownership('paper', 'AAPL')
    assert [(o.instance_id, o.allocation_id, o.logical_strategy_id) for o in owners] == [
        ('one', 'one-cash', 'one'), ('two', 'two-cash', 'two')]
    with store.transaction():
        policy.release('paper', 'one', 'AAPL')
    assert policy.symbol_limit('paper', 'AAPL') == D(40)
    policy.set_symbol_limit('paper', 'AAPL', D(30))
    with pytest.raises(ValueError, match='cannot increase'):
        policy.set_symbol_limit('paper', 'AAPL', D(31))
    store.close()
    store = PortfolioStore(tmp_path / 'capital.sqlite3')
    policy = store.capital_policy
    policy.configure(account_id='paper', currency='USD', budget=D(100), fee_rate=D(0))
    assert policy.mode('paper') == SHARED
    assert policy.symbol_limit('paper', 'AAPL') == D(30)
    assert [o.instance_id for o in policy.ownership('paper')] == ['two']
    assert policy.snapshot('paper', 'one').allocation_id == 'one-cash'
    store.close()


def test_shared_claim_rejects_unbound_owner_and_identity_reuse(tmp_path):
    _, store, executor = setup(tmp_path)
    policy = executor.capital_policy
    share(policy)
    with pytest.raises(ValueError, match='binding required'):
        claim(store, 'unfunded')
    with pytest.raises(ValueError, match='binding changed'):
        policy.bind(account_id='paper', strategy_id='one', instance_id='one',
                    allocation_id='one-cash', logical_strategy='another-package')
    assert policy.ownership('paper') == ()
    store.close()


def test_allocation_identity_cannot_fund_two_instances(tmp_path):
    store = PortfolioStore(tmp_path / 'allocation-identity.sqlite3')
    executor = PortfolioExecutor(Broker(), store, clock=lambda: NOW)
    policy = executor.capital_policy
    share(policy)
    executor.allocate(account_id='paper', strategy_id='one', allocation_id='stable-allocation',
        capital=D(40), initial_cash=D(40), official_equity=D(100))
    policy.bind(account_id='paper', strategy_id='one', instance_id='one', allocation_id='stable-allocation')
    with pytest.raises(sqlite3.IntegrityError, match='UNIQUE'):
        executor.allocate(account_id='paper', strategy_id='two', allocation_id='stable-allocation',
            capital=D(40), initial_cash=D(40), official_equity=D(100))
    assert store.allocation('paper', 'two') is None
    assert policy.snapshot('paper', 'one').capital == D(40)
    store.close()


def legacy_schema(store):
    with store.transaction():
        store.db.execute('ALTER TABLE capital_accounts DROP COLUMN mode')
        store.db.execute('ALTER TABLE capital_symbols RENAME TO current_symbols')
        store.db.execute('CREATE TABLE capital_symbols(account TEXT NOT NULL,symbol TEXT NOT NULL,'
                         'strategy TEXT NOT NULL,PRIMARY KEY(account,symbol))')
        store.db.execute('INSERT INTO capital_symbols SELECT * FROM current_symbols')
        store.db.execute('DROP TABLE current_symbols')
        store.db.execute('DROP TABLE capital_symbol_limits')


def test_legacy_schema_upgrade_preserves_ownership_and_defaults_exclusive(tmp_path):
    _, store, _ = setup(tmp_path)
    claim(store, 'one')
    legacy_schema(store)
    store.close()
    store = PortfolioStore(tmp_path / 'capital.sqlite3')
    policy = store.capital_policy
    assert policy.mode('paper') == CapitalPolicyMode.EXCLUSIVE
    assert policy.ownership('paper')[0].allocation_id == 'one-cash'
    with pytest.raises(ValueError, match='ownership conflict'):
        claim(store, 'two')
    share(policy)
    claim(store, 'two')
    assert len(policy.ownership('paper')) == 2
    store.close()


def test_schema_upgrade_failure_rolls_back_ddl_and_account_mode(tmp_path):
    _, store, _ = setup(tmp_path)
    claim(store, 'one')
    legacy_schema(store)
    def authorize(action, table, *_):
        if action == sqlite3.SQLITE_DROP_TABLE and table == 'capital_symbols_exclusive':
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK
    store.db.set_authorizer(authorize)
    with pytest.raises(sqlite3.DatabaseError):
        CapitalPolicy(store)
    store.db.set_authorizer(None)
    assert 'mode' not in {row[1] for row in store.db.execute('PRAGMA table_info(capital_accounts)')}
    assert store.db.execute('SELECT * FROM capital_symbols').fetchall() == [('paper', 'AAPL', 'one')]
    assert [row[1] for row in store.db.execute('PRAGMA table_info(capital_symbols)') if row[5]] == ['account', 'symbol']
    CapitalPolicy(store)
    store.close()


def test_shared_mode_adopts_funded_inventory_but_rejects_opposite_owners(tmp_path):
    _, store, executor = setup(tmp_path)
    with store.transaction():
        for owner, quantity in [('one', '1'), ('two', '2')]:
            store.db.execute('INSERT INTO portfolio_claims VALUES (?,?,?,?,?,?)',
                             ('paper', 'AAPL', owner, quantity, quantity, '10'))
    share(executor.capital_policy)
    claim(store, 'one')
    claim(store, 'two')
    with pytest.raises(ValueError, match='shared symbols'):
        executor.capital_policy.configure(account_id='paper', currency='USD', budget=D(100),
                                         fee_rate=D(0), mode=CapitalPolicyMode.EXCLUSIVE)
    assert executor.capital_policy.mode('paper') == SHARED
    with pytest.raises(ValueError, match='flat instance'):
        with store.transaction():
            executor.capital_policy.release('paper', 'one', 'AAPL')
    with store.transaction():
        store.db.execute("UPDATE portfolio_claims SET quantity='-2' WHERE strategy='two'")
    with pytest.raises(ValueError, match='direction conflict'):
        claim(store, 'one')
    store.close()


def test_qualify_failure_rolls_back_all_claims_binding_and_tighter_limit(tmp_path):
    from types import SimpleNamespace
    _, store, executor = setup(tmp_path)
    policy = executor.capital_policy
    claim(store, 'two', 'BLOCKED')
    spec = SimpleNamespace(capital={'currency': 'USD', 'requested': D(40)},
        symbols=['AAPL', 'BLOCKED'], account_id='paper', market='BINANCE_EQUITY', strategy_id='one')
    binding = dict(account_id='paper', instance_id='one', strategy_id='one',
                   market='BINANCE_EQUITY', allocation_id='one-cash')
    with pytest.raises(ValueError, match='ownership conflict'):
        policy.qualify(spec, binding)
    assert [o.symbol for o in policy.ownership('paper')] == ['BLOCKED']
    with pytest.raises(KeyError):
        policy.symbol_limit('paper', 'AAPL')
    store.close()


def reserve(executor, owner, symbol, quantity, client_id):
    intent = OrderIntent(Market.BINANCE_EQUITY, symbol, Side.BUY, OrderType.LIMIT,
                         D(quantity), D(10), client_id, time_in_force='GTC')
    return executor.create(account_id='paper', strategy_id=owner, strategy_version='1',
        trading_session='2026-11-24', weights={}, owned_symbols=[symbol], now=NOW,
        direct_orders=[(intent, D(10))])


@pytest.mark.parametrize('uncertain_canceled', [False, True])
def test_aggregate_symbol_limit_is_not_multiplied_by_instance_count(tmp_path, uncertain_canceled):
    broker, store, executor = setup(tmp_path)
    share(executor.capital_policy)
    claim(store, 'one')
    claim(store, 'two')
    first = reserve(executor, 'one', 'AAPL', '3', 'first')
    if uncertain_canceled:
        from dataclasses import replace
        from factorlab.trading.contracts import OrderStatus
        order = store.orders(first)[0]
        order['record'] = replace(order['record'], snapshot=replace(order['record'].snapshot,
                                  status=OrderStatus.CANCELED))
        order['uncertain'] = True
        with store.transaction():
            store.db.execute('UPDATE portfolio_orders SET payload=? WHERE id=?', (dumps(order), 'first'))
    from factorlab.trading.contracts import OrderSnapshot
    from factorlab.trading.state import OrderRecord
    intent = OrderIntent(Market.BINANCE_EQUITY, 'AAPL', Side.BUY, OrderType.LIMIT,
                         D(2), D(10), 'second', time_in_force='GTC')
    order = dict(record=OrderRecord(OrderSnapshot(intent), NOW, D(10)),
                 reference_price=D(10), reservation_price=D(10), attempted=False, uncertain=False)
    with pytest.raises(ValueError, match='SYMBOL_EXPOSURE_LIMIT'):
        with store.transaction():
            store.db.execute('INSERT INTO portfolio_plans VALUES (?,?,?,?,?)',
                             ('second-plan', 'paper', 'two', '2026-11-24', dumps({})))
            store.db.execute('INSERT INTO portfolio_orders VALUES (?,?,?)',
                             ('second', 'second-plan', dumps(order)))
            executor.capital_policy.validate_orders(broker.read_account(), 'two')
    assert len(store.orders(first)) == 1
    assert store.db.execute("SELECT count(*) FROM portfolio_orders WHERE id='second'").fetchone()[0] == 0
    store.close()


def test_pending_order_prevents_release_and_exclusive_downgrade(tmp_path):
    _, store, executor = setup(tmp_path)
    share(executor.capital_policy)
    reserve(executor, 'one', 'AAPL', '1', 'live')
    with pytest.raises(ValueError, match='unfinished orders'):
        with store.transaction():
            executor.capital_policy.release('paper', 'one', 'AAPL')
    with pytest.raises(ValueError, match='unfinished orders'):
        executor.capital_policy.configure(account_id='paper', currency='USD', budget=D(100),
            fee_rate=D(0), mode=CapitalPolicyMode.EXCLUSIVE)
    assert executor.capital_policy.mode('paper') == SHARED
    store.close()


def test_configure_seeds_limits_for_existing_orders_before_plan_replay(tmp_path):
    broker = Broker()
    store = PortfolioStore(tmp_path / 'legacy-orders.sqlite3')
    executor = PortfolioExecutor(broker, store, clock=lambda: NOW)
    allocate(executor, 'one', '40')
    identity = reserve(executor, 'one', 'AAPL', '4', 'legacy-order')
    policy = executor.capital_policy
    policy.configure(account_id='paper', currency='USD', budget=D(100), fee_rate=D(0))
    policy.bind(account_id='paper', strategy_id='one', instance_id='one', allocation_id='one-cash')
    assert reserve(executor, 'one', 'AAPL', '4', 'legacy-order') == identity
    assert policy.symbol_limit('paper', 'AAPL') == D(40)
    with store.transaction():
        policy.validate_orders(broker.read_account(), 'one', include_id='legacy-order')
    store.close()


def test_partial_fill_and_broker_duplicate_count_remaining_only_once(tmp_path):
    broker, store, executor = setup(tmp_path)
    share(executor.capital_policy)
    identity = reserve(executor, 'one', 'AAPL', '4', 'partial')
    broker.mode = 'partial'
    run(executor, identity)
    assert store.holdings('paper', 'one')['AAPL'] == D(2)
    assert executor.capital_policy.snapshot('paper', 'one').reserved == D(20)
    with store.transaction():
        executor.capital_policy.validate_orders(broker.read_account(), 'one', include_id='partial')
    broker.mode = 'fill'
    store.close()


@pytest.mark.parametrize('quantity', ['1', '4'])
def test_appreciated_inventory_can_be_reduced_above_entry_risk_limits(tmp_path, quantity):
    broker, store, executor = setup(tmp_path)
    share(executor.capital_policy)
    identity = reserve(executor, 'one', 'AAPL', '4', 'entry')
    run(executor, identity)
    run(executor, identity)
    intent = OrderIntent(Market.BINANCE_EQUITY, 'AAPL', Side.SELL, OrderType.LIMIT,
                         D(quantity), D(20), 'exit', time_in_force='GTC')
    executor.create(account_id='paper', strategy_id='one', strategy_version='1',
        trading_session='2026-11-24', rebalance_id='exit', weights={}, owned_symbols=['AAPL'], now=NOW,
        direct_orders=[(intent, D(20))])
    from dataclasses import replace
    account = broker.read_account()
    appreciated = replace(account, positions=tuple(replace(p, mark_price=D(20)) for p in account.positions))
    with store.transaction():
        executor.capital_policy.validate_orders(appreciated, 'one', include_id='exit')
    store.close()


def test_account_limit_includes_other_symbols_and_cannot_be_split_by_instance(tmp_path):
    broker, store, executor = setup(tmp_path)
    share(executor.capital_policy)
    reserve(executor, 'one', 'AAPL', '3', 'new-risk')
    broker.positions['QQQ'] = D(8)
    with pytest.raises(ValueError, match='ACCOUNT_BUDGET_LIMIT'):
        with store.transaction():
            executor.capital_policy.validate_orders(broker.read_account(), 'one', include_id='new-risk')
    store.close()


def test_exit_preserves_other_shared_owner_and_allocation_binding(tmp_path):
    _, store, executor = setup(tmp_path)
    policy = executor.capital_policy
    share(policy)
    claim(store, 'one')
    claim(store, 'two')
    with store.transaction():
        store.db.execute('INSERT INTO portfolio_claims VALUES (?,?,?,?,?,?)',
                         ('paper', 'AAPL', 'one', '0', '0', '0'))
        store.db.execute('INSERT INTO portfolio_claims VALUES (?,?,?,?,?,?)',
                         ('paper', 'AAPL', 'two', '2', '2', '10'))
        policy.release('paper', 'one', 'AAPL')
    assert [o.instance_id for o in policy.ownership('paper', 'AAPL')] == ['two']
    policy.configure(account_id='paper', currency='USD', budget=D(100), fee_rate=D(0),
                     mode=CapitalPolicyMode.EXCLUSIVE)
    assert store.holdings('paper', 'two')['AAPL'] == D(2)
    assert policy.snapshot('paper', 'one').capital == D(40)
    store.close()
