from decimal import Decimal as D

import pytest

from factorlab.trading.portfolio_execution import PortfolioExecutor, PortfolioStore
from test_portfolio_execution import Broker, NOW, run
from test_portfolio_allocations import allocate, plan


def setup(tmp_path):
    broker = Broker()
    store = PortfolioStore(tmp_path / 'capital.sqlite3')
    executor = PortfolioExecutor(broker, store, clock=lambda: NOW)
    executor.capital_policy.configure(account_id='paper', currency='USD', budget=D(100), fee_rate=D(0))
    for owner, amount in [('one', '40'), ('two', '60')]:
        allocate(executor, owner, amount)
        executor.capital_policy.bind(account_id='paper', strategy_id=owner,
            instance_id=owner, allocation_id=owner + '-cash')
    return broker, store, executor


def test_qualify_uses_instance_owner_and_atomically_claims_symbols(tmp_path):
    from types import SimpleNamespace
    broker = Broker()
    store = PortfolioStore(tmp_path / 'qualified.sqlite3')
    executor = PortfolioExecutor(broker, store, clock=lambda: NOW)
    policy = executor.capital_policy
    policy.configure(account_id='paper', currency='USD', budget=D(100), fee_rate=D(0))
    for owner in ['first', 'second']:
        allocate(executor, owner, '40')
    spec = SimpleNamespace(capital={'currency': 'USD', 'requested': D(40)}, symbols=['AAPL'])
    def binding(owner):
        return dict(account_id='paper', instance_id=owner, strategy_id='same-package',
                    allocation_id=owner + '-cash')
    assert policy.qualify(spec, binding('first'))['capital'] == D(40)
    with pytest.raises(ValueError, match='ownership conflict'):
        policy.qualify(spec, binding('second'))
    assert store.db.execute('SELECT strategy FROM capital_symbols').fetchall() == [('first',)]
    assert store.db.execute('SELECT logical_strategy FROM capital_instances WHERE strategy=?',
                            ('first',)).fetchone()[0] == 'same-package'
    store.close()


def test_direct_reservation_includes_explicit_fee_allowance(tmp_path):
    from factorlab.trading.contracts import Market, OrderIntent, OrderType, Side
    store = PortfolioStore(tmp_path / 'fees.sqlite3')
    executor = PortfolioExecutor(Broker(), store, clock=lambda: NOW)
    policy = executor.capital_policy
    policy.configure(account_id='paper', currency='USD', budget=D(100), fee_rate=D('.01'))
    allocate(executor, 'one', '40')
    policy.bind(account_id='paper', strategy_id='one', instance_id='one', allocation_id='one-cash')
    intent = OrderIntent(Market.BINANCE_EQUITY, 'AAPL', Side.BUY, OrderType.LIMIT,
                         D(4), D(10), 'fees', time_in_force='GTC')
    with pytest.raises(ValueError, match='allocated cash'):
        executor.create(account_id='paper', strategy_id='one', strategy_version='1',
            trading_session='2026-11-24', weights={}, owned_symbols=['AAPL'], now=NOW,
            direct_orders=[(intent, D(10))])
    assert policy.snapshot('paper', 'one').reserved == 0
    store.close()


def test_explicit_policy_keeps_allocated_symbols_exclusive(tmp_path):
    _, store, executor = setup(tmp_path)
    first = plan(executor, 'one')
    with pytest.raises(ValueError, match='ownership conflict'):
        plan(executor, 'two')
    assert store.plan(first)['request']['strategy'] == 'one'
    assert store.holdings('paper', 'two') == {}
    store.close()


def test_pause_preserves_capital_and_other_strategy_admission(tmp_path):
    _, store, executor = setup(tmp_path)
    executor.capital_policy.pause('paper', 'one')
    with pytest.raises(ValueError, match='paused'):
        plan(executor, 'one')
    plan(executor, 'two')
    snapshot = executor.capital_policy.snapshot('paper', 'one')
    assert snapshot.capital == snapshot.cash == D(40)
    assert snapshot.paused
    store.close()


def test_policy_and_bindings_survive_restart(tmp_path):
    broker, store, executor = setup(tmp_path)
    executor.capital_policy.pause('paper', 'one')
    store.close()
    store = PortfolioStore(tmp_path / 'capital.sqlite3')
    executor = PortfolioExecutor(broker, store, clock=lambda: NOW)
    assert executor.capital_policy.snapshot('paper', 'one').paused
    with pytest.raises(ValueError, match='binding changed'):
        executor.capital_policy.bind(account_id='paper', strategy_id='one',
            instance_id='different', allocation_id='one-cash')
    store.close()


def test_concurrent_direct_reservations_roll_back_overcommit(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from factorlab.trading.contracts import Market, OrderIntent, OrderType, Side

    broker = Broker()
    path = tmp_path / 'parallel.sqlite3'
    store = PortfolioStore(path)
    executor = PortfolioExecutor(broker, store, clock=lambda: NOW)
    allocate(executor, 'one', '50')
    allocate(executor, 'two', '50')
    store.close()
    broker.cash = D(70)
    barrier = Barrier(2)

    def reserve(owner):
        local = PortfolioStore(path)
        worker = PortfolioExecutor(broker, local, clock=lambda: NOW)
        barrier.wait()
        intent = OrderIntent(Market.BINANCE_EQUITY, 'AAPL', Side.BUY, OrderType.LIMIT,
                             D(4), D(10), owner, time_in_force='GTC')
        try:
            worker.create(account_id='paper', strategy_id=owner, strategy_version='1',
                trading_session='2026-11-24', weights={}, owned_symbols=['AAPL'], now=NOW,
                direct_orders=[(intent, D(10))])
        except ValueError as error:
            assert 'available account cash' in str(error)
            return False
        finally:
            local.close()
        return True

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(reserve, ['one', 'two'])) == [False, True]
    store = PortfolioStore(path)
    assert store.db.execute('SELECT count(*) FROM portfolio_orders').fetchone()[0] == 1
    assert sorted([store.cash('paper', 'one'), store.cash('paper', 'two')]) == [D(10), D(50)]
    store.close()


def test_readonly_risk_snapshot_does_not_recover_or_save(tmp_path):
    broker, store, executor = setup(tmp_path)
    identity = plan(executor, 'one')
    run(executor, identity)
    run(executor, identity)
    before = store.db.total_changes
    executor._recover_account = lambda _: pytest.fail('read-only snapshot attempted recovery')
    snapshot = executor.capital_policy.risk_snapshot(executor, 'paper', official_equity=D(100))
    assert store.db.total_changes == before
    one, two = snapshot.strategies
    assert one.occupied == one.cost_basis == D(40)
    assert one.nav == D(40)
    assert two.nav == D(60)
    assert snapshot.allocated == snapshot.budget == D(100)
    store.close()


def test_static_account_budget_is_stricter_than_broker_equity(tmp_path):
    store = PortfolioStore(tmp_path / 'capital.sqlite3')
    executor = PortfolioExecutor(Broker(), store, clock=lambda: NOW)
    executor.capital_policy.configure(account_id='paper', currency='USD', budget=D(40), fee_rate=D(0))
    allocate(executor, 'one', '40')
    with pytest.raises(ValueError, match='static account capital budget'):
        allocate(executor, 'two', '1')
    assert store.allocation('paper', 'two') is None
    store.close()
