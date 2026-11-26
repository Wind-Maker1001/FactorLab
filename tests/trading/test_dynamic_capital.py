"""Capital administration, serialization and flow-adjusted returns."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D
from multiprocessing import get_context
from os import _exit
from threading import Barrier

import pytest

from factorlab.trading.capital_cli import command_result, enqueue, process_command
from factorlab.trading.capital_performance import build_capital_performance
from factorlab.trading.contracts import Market, OrderIntent, OrderType, Side
from factorlab.trading.portfolio_execution import PortfolioExecutor, PortfolioStore
from test_portfolio_execution import Broker, NOW


class CashBroker(Broker):
    def read_account(self):
        return replace(super().read_account(), official_equity=self.cash)


def open_executor(store):
    return PortfolioExecutor(CashBroker(cash=D(1000)), store, clock=lambda: NOW)


def funded(path):
    with closing(PortfolioStore(path)) as store:
        executor = open_executor(store)
        store.capital_policy.configure(account_id='paper', currency='USD', budget=D(1000), fee_rate=D(0))
        for owner, value in [('A', D(400)), ('B', D(300))]:
            executor.allocate(account_id='paper', strategy_id=owner,
                allocation_id=owner + '-funded', capital=value, initial_cash=value, official_equity=D(1000))
            store.capital_policy.bind(account_id='paper', strategy_id=owner, instance_id=owner,
                                      allocation_id=owner + '-funded')


def test_transfer_top_up_withdraw_and_retire_preserve_budget_identity_and_wallet(tmp_path):
    path = tmp_path / 'capital.db'
    funded(path)
    with closing(PortfolioStore(path)) as store:
        capital = open_executor(store).capital
        initial = capital.snapshot('paper')
        assert initial.allocated == D(700) and initial.unallocated == initial.pool_cash == D(300)
        request = dict(account_id='paper', event_id='transfer', source='A', destination='B', amount=D(100))
        moved = capital.change(**request)
        assert capital.change(**request) == moved
        with pytest.raises(ValueError, match='conflicts'):
            capital.change(**dict(request, amount=D(101)))
        assert [(s.allocated, s.cash) for s in capital.snapshot('paper').strategies] == [
            (D(300), D(300)), (D(400), D(400))]
        capital.change(account_id='paper', event_id='top-up', source=None, destination='A', amount=D(25))
        capital.change(account_id='paper', event_id='withdraw', source='B', destination=None, amount=D(50))
        release = capital.release(account_id='paper', event_id='retire', strategy_id='A')
        assert release.applied == release.released_budget == D(325)
        with pytest.raises(ValueError, match='retired'):
            store.capital_policy.pause('paper', 'A', False)
        assert capital.performance('paper', 'A')['returns'].absolute_pnl == 0
    with closing(PortfolioStore(path)) as store:
        executor = open_executor(store)
        executor.allocate(account_id='paper', strategy_id='A', allocation_id='A-funded',
                           capital=D(400), initial_cash=D(400), official_equity=D(1000))
        after = executor.capital.snapshot('paper')
        a, b = after.strategies
        assert a.retired and a.cash == a.allocated == 0
        assert (a.instance_id, a.allocation_id, a.initial_capital) == ('A', 'A-funded', D(400))
        assert (b.cash, b.allocated, after.pool_cash) == (D(350), D(350), D(650))
        assert after.budget == after.allocated + after.unallocated
        assert after.official_wallet == sum(s.cash for s in after.strategies) + after.pool_cash == D(1000)
        assert [e['result'].event_id for e in executor.capital.history('paper')] == [
            'transfer', 'top-up', 'withdraw', 'retire']


def test_two_connections_cannot_allocate_the_same_capital(tmp_path):
    path = tmp_path / 'capital.db'
    funded(path)
    barrier = Barrier(2, timeout=10)

    def allocate(owner):
        with closing(PortfolioStore(path)) as store:
            executor = open_executor(store)
            barrier.wait()
            return executor.capital.change(account_id='paper', event_id='add-' + owner,
                source=None, destination=owner, amount=D(250))

    with ThreadPoolExecutor(2) as threads:
        receipts = list(threads.map(allocate, ('A', 'B')))
    assert sorted(r.applied for r in receipts) == [D(0), D(250)]
    assert sorted(r.executable for r in receipts) == [D(50), D(250)]
    with closing(PortfolioStore(path)) as store:
        view = open_executor(store).capital.snapshot('paper')
        assert view.unallocated == view.pool_cash == D(50)


def test_order_reservation_and_withdrawal_serialize(tmp_path):
    path = tmp_path / 'capital.db'
    funded(path)
    barrier = Barrier(2, timeout=10)

    def reserve():
        with closing(PortfolioStore(path)) as store:
            executor = open_executor(store)
            entry = OrderIntent(Market.BINANCE_EQUITY, 'AAPL', Side.BUY, OrderType.LIMIT,
                                D(30), D(10), 'reserved-buy')
            barrier.wait()
            try:
                return executor.create(account_id='paper', strategy_id='A', strategy_version='1',
                    trading_session='day', weights={}, owned_symbols=['AAPL'], now=NOW,
                    direct_orders=[(entry, D(10))])
            except ValueError as exc:
                assert 'allocated cash' in str(exc)
                return None

    def withdraw():
        with closing(PortfolioStore(path)) as store:
            executor = open_executor(store)
            barrier.wait()
            return executor.capital.change(account_id='paper', event_id='pull', source='A',
                                           destination=None, amount=D(200)).applied

    with ThreadPoolExecutor(2) as threads:
        plan, withdrawal = threads.submit(reserve), threads.submit(withdraw)
        identity, amount = plan.result(), withdrawal.result()
    assert (identity is None and amount == D(200)) or (identity is not None and amount == 0)
    with closing(PortfolioStore(path)) as store:
        assert store.cash('paper', 'A') in {D(100), D(200)}


def exit_after_first_posting(path):
    with closing(PortfolioStore(path)) as store:
        manager = open_executor(store).capital
        post = manager._post

        def interrupted(*args):
            post(*args)
            _exit(42)

        manager._post = interrupted
        manager.change(account_id='paper', event_id='crash', source='A', destination='B', amount=D(100))


def test_process_exit_rolls_back_first_posting_and_retry_commits_both(tmp_path):
    path = tmp_path / 'capital.db'
    funded(path)
    process = get_context('spawn').Process(target=exit_after_first_posting, args=(path,))
    process.start()
    process.join(15)
    assert process.exitcode == 42
    with closing(PortfolioStore(path)) as store:
        manager = open_executor(store).capital
        assert not list(manager.history('paper'))
        assert (store.cash('paper', 'A'), store.cash('paper', 'B')) == (D(400), D(300))
        receipt = manager.change(account_id='paper', event_id='crash', source='A', destination='B', amount=D(100))
        assert receipt.applied == D(100)
        assert (store.cash('paper', 'A'), store.cash('paper', 'B')) == (D(300), D(400))


def test_capital_performance_links_pre_flow_marks_and_dated_investor_flows():
    years = timedelta(days=365)
    returns = build_capital_performance(initial_capital=D(100), started_at=NOW,
        observations=[(NOW + years, D(110), D(90))], current=(NOW + 2 * years, D(220)))
    assert returns.capital_in == D(90) and returns.capital_out == 0
    assert returns.absolute_pnl == D(30)
    assert returns.twr == D('.21') and returns.xirr == pytest.approx(.1)
    assert tuple(point.equity for point in returns.nav) == (D(100), D(200), D(220))
    assert tuple(point.index for point in returns.nav) == (D(1), D('1.1'), D('1.21'))


def test_running_owner_recovers_queued_command_after_capital_commit(tmp_path):
    path = tmp_path / 'capital.db'
    funded(path)
    command = dict(action='change', event_id='transfer', source='A', destination='B', amount=D(100))
    with closing(PortfolioStore(path)) as store:
        enqueue(store, 'paper', 'command', command)
        assert command_result(store, 'paper', 'command')['status'] == 'PENDING'
        manager = open_executor(store).capital
        receipt = manager.change(account_id='paper',
            **{key: value for key, value in command.items() if key != 'action'})
    with closing(PortfolioStore(path)) as store:
        manager = open_executor(store).capital
        process_command(manager, 'paper')
        assert command_result(store, 'paper', 'command')['data']['event_id'] == receipt.event_id
        assert len(tuple(manager.history('paper'))) == 1
        assert store.cash('paper', 'A') == D(300)
