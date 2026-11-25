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
    spec = SimpleNamespace(capital={'currency': 'USD', 'requested': D(40)}, symbols=['AAPL'],
                           account_id='paper', market='BINANCE_EQUITY', strategy_id='same-package')
    def binding(owner):
        return dict(account_id='paper', instance_id=owner, strategy_id='same-package', market='BINANCE_EQUITY',
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


def test_flat_transfer_keeps_predecessor_cost_history_and_symbol_reservation(tmp_path):
    broker = Broker(cash=D(80), positions={'AAPL': D(2)})
    store = PortfolioStore(tmp_path / 'transfer.sqlite3')
    executor = PortfolioExecutor(broker, store, clock=lambda: NOW)
    identity = executor.create(account_id='paper', strategy_id='one', strategy_version='1',
        trading_session='2026-11-24', weights={'AAPL': D(0)}, owned_symbols=['AAPL'], now=NOW,
        initial_inventory={'AAPL': (D(2), D(8))})
    executor.allocate(account_id='paper', strategy_id='one', allocation_id='one-cash',
        capital=D(40), initial_cash=D(20), official_equity=D(100))
    allocate(executor, 'two', '60')
    policy = executor.capital_policy
    policy.configure(account_id='paper', currency='USD', budget=D(100), fee_rate=D(0))
    for owner in ['one', 'two']:
        policy.bind(account_id='paper', strategy_id=owner, instance_id=owner, allocation_id=owner + '-cash')
    run(executor, identity)
    run(executor, identity)
    args = dict(account_id='paper', from_strategy='one', to_strategy='two', symbols=['AAPL'])
    executor.transfer_flat_ownership(**args)
    executor.transfer_flat_ownership(**args)
    assert executor.report(identity, official_equity=D(100), readonly=True).realized_pnl == D(4)
    assert store.holdings('paper', 'two')['AAPL'] == 0
    with pytest.raises(ValueError, match='ownership conflict'):
        plan(executor, 'one', rebalance_id='reclaim')
    assert store.db.execute('SELECT strategy FROM portfolio_plans WHERE id=?', (identity,)).fetchone() == ('one',)
    executor.transfer_flat_ownership(account_id='paper', from_strategy='two', to_strategy='one', symbols=['AAPL'])
    assert store.db.execute('SELECT initial_quantity,entry_price FROM portfolio_claims '
        'WHERE account=? AND symbol=? AND strategy=?', ('paper', 'AAPL', 'one')).fetchone() == ('2', '8')
    assert store.db.execute('SELECT count(*) FROM portfolio_claim_history WHERE strategy=?', ('one',)).fetchone()[0] == 0
    assert executor.report(identity, official_equity=D(100), readonly=True).realized_pnl == D(4)
    executor.transfer_flat_ownership(**args)
    assert executor.report(identity, official_equity=D(100), readonly=True).realized_pnl == D(4)
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


def test_paused_owner_recovers_terminal_release_after_restart(tmp_path):
    broker, store, executor = setup(tmp_path)
    broker.mode = 'partial'
    identity = plan(executor, 'one')
    run(executor, identity)
    executor.capital_policy.pause('paper', 'one')
    broker.cancel_order(broker.posts[0].client_order_id)
    store.close()
    store = PortfolioStore(tmp_path / 'capital.sqlite3')
    executor = PortfolioExecutor(broker, store, clock=lambda: NOW)
    result = run(executor, identity)
    assert result.reasons['capital'] == 'STRATEGY_PAUSED'
    snapshot = executor.capital_policy.snapshot('paper', 'one')
    assert snapshot.reserved == 0
    assert snapshot.cash == D(20)
    assert len(broker.posts) == 1
    assert executor.capital_policy.snapshot('paper', 'two').cash == D(60)
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
    executor.capital_policy.configure(account_id='paper', currency='USD', budget=D(100), fee_rate=D(0))
    for owner in ['one', 'two']:
        executor.capital_policy.bind(account_id='paper', strategy_id=owner,
            instance_id=owner, allocation_id=owner + '-cash')
    store.close()
    broker.cash = D(70)
    barrier = Barrier(2)

    def reserve(owner):
        local = PortfolioStore(path)
        worker = PortfolioExecutor(broker, local, clock=lambda: NOW)
        barrier.wait()
        symbol = 'AAPL' if owner == 'one' else 'QQQ'
        intent = OrderIntent(Market.BINANCE_EQUITY, symbol, Side.BUY, OrderType.LIMIT,
                             D(4), D(10), owner, time_in_force='GTC')
        try:
            worker.create(account_id='paper', strategy_id=owner, strategy_version='1',
                trading_session='2026-11-24', weights={}, owned_symbols=[symbol], now=NOW,
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


def test_instance_risk_limits_reuse_controller_caps_without_affecting_other_owner(tmp_path):
    from factorlab.trading.control import RiskLimits
    _, store, executor = setup(tmp_path)
    executor.capital_policy.set_limits('paper', 'one', RiskLimits(
        max_order_notional=D(10), max_instrument_exposure=D(20), account_budget=D(30)))
    first = plan(executor, 'one')
    with pytest.raises(ValueError, match='ORDER_NOTIONAL_LIMIT'):
        run(executor, first)
    assert store.orders(first) == []
    second = executor.create(account_id='paper', strategy_id='two', strategy_version='1',
        trading_session='2026-11-24', weights={'QQQ': D(1)}, owned_symbols=['QQQ'], now=NOW)
    run(executor, second)
    assert len(store.orders(second)) == 1
    store.close()


def test_optimizer_recommendation_does_not_change_static_allocations(tmp_path):
    import pandas as pd
    from factorlab.portfolio.optimizer import PortfolioOptimizer, OptimizationInput, OptimizerConfig
    _, store, executor = setup(tmp_path)
    labels = ['one', 'two']
    data = OptimizationInput(symbols=tuple(labels), decision_time=NOW,
        covariance=pd.DataFrame([[1., 0.], [0., 4.]], index=labels, columns=labels),
        covariance_as_of=NOW)
    result = executor.capital_policy.propose('paper', PortfolioOptimizer(OptimizerConfig()), data)
    assert result.target_weights['one'] > result.target_weights['two']
    assert store.allocation('paper', 'one')['capital'] == D(40)
    assert store.allocation('paper', 'two')['capital'] == D(60)
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
