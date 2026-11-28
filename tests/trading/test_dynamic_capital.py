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

from factorlab.integrations.ledger_replication import BatchReceiver, EventAdmission, ReplicationCursor, make_batch
from factorlab.integrations.ledger_replay import AccountEventWriter, AccountReplay, DomainReplay, SnapshotPosition
from factorlab.trading.capital_management import CapitalManagement, reduce_capital_event
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


def test_capital_lost_ack_applies_original_result_without_reobservation(tmp_path, monkeypatch):
    import sqlite3

    path = tmp_path / 'source.db'
    funded(path)
    baseline = SnapshotPosition('paper', 'g1', 0, None)
    receiver = BatchReceiver(tmp_path / 'offsite', stream='paper', generation='g1')
    lose_ack = True

    def send(payload):
        receiver.accept(payload)
        if lose_ack:
            raise TimeoutError('offsite persisted; ACK lost')
        return ReplicationCursor('paper', 'g1', payload['sequence'], payload['digest'])

    with closing(PortfolioStore(path)) as store, closing(sqlite3.connect(
            tmp_path / 'admission.db', isolation_level=None)) as journal:
        replay = AccountReplay({'portfolio': DomainReplay(store.db, reduce_capital_event, baseline=baseline)},
                               baseline=baseline)
        admission = EventAdmission(journal, stream='paper', generation='g1', send=send)
        manager = CapitalManagement(open_executor(store), event_writer=AccountEventWriter(admission, replay))
        request = dict(account_id='paper', event_id='transfer', source='A', destination='B', amount=D(100))
        with pytest.raises(TimeoutError):
            manager.change(**request)
        assert (store.cash('paper', 'A'), store.cash('paper', 'B')) == (D(400), D(300))
        with pytest.raises(TimeoutError):
            manager.change(**request)

        def reject_reobservation(_account):
            raise AssertionError('confirmed capital event must not be recalculated')

        monkeypatch.setattr(manager, '_snapshot', reject_reobservation)
        lose_ack = False
        assert manager.change(**request).applied == D(100)
        assert (store.cash('paper', 'A'), store.cash('paper', 'B')) == (D(300), D(400))
        assert admission.pending() is None
        assert receiver.cursor().sequence == 1
    receiver.close()


def test_offsite_capital_command_acceptance_and_result_survive_lost_ack(tmp_path):
    import sqlite3

    path = tmp_path / 'commands.db'
    funded(path)
    baseline = SnapshotPosition('paper', 'g1', 0, None)
    receiver = BatchReceiver(tmp_path / 'offsite', stream='paper', generation='g1')
    fail_kind = 'CAPITAL_COMMAND_ENQUEUED'

    def send(payload):
        receiver.accept(payload)
        if payload['events'][0]['kind'] == fail_kind:
            raise TimeoutError('ACK lost')
        return ReplicationCursor('paper', 'g1', payload['sequence'], payload['digest'])

    with closing(PortfolioStore(path)) as store, closing(sqlite3.connect(
            tmp_path / 'admission.db', isolation_level=None)) as journal:
        replay = AccountReplay({'portfolio': DomainReplay(store.db, reduce_capital_event, baseline=baseline)},
                               baseline=baseline)
        writer = AccountEventWriter(EventAdmission(journal, stream='paper', generation='g1', send=send), replay)
        with closing(sqlite3.connect(tmp_path / 'snapshot.db')) as target:
            store.db.backup(target)
        request = dict(action='change', event_id='transfer', source='A', destination='B', amount=D(100))
        with pytest.raises(TimeoutError):
            enqueue(store, 'paper', 'command', request, event_writer=writer)
        with pytest.raises(KeyError):
            command_result(store, 'paper', 'command')
        fail_kind = None
        enqueue(store, 'paper', 'command', request, event_writer=writer)
        with pytest.raises(PermissionError, match='account event writer'):
            enqueue(store, 'paper', 'bypass', request)
        with pytest.raises(PermissionError, match='account event writer'):
            process_command(open_executor(store).capital, 'paper')
        assert command_result(store, 'paper', 'command')['status'] == 'PENDING'
        manager = CapitalManagement(open_executor(store), event_writer=writer)
        fail_kind = 'CAPITAL_COMMAND_COMPLETED'
        with pytest.raises(TimeoutError):
            process_command(manager, 'paper')
        assert command_result(store, 'paper', 'command')['status'] == 'PENDING'
        fail_kind = None
        process_command(manager, 'paper')
        expected = command_result(store, 'paper', 'command')
        assert expected['status'] == 'COMPLETE'
        assert receiver.cursor().sequence == 3
    path.unlink()
    with closing(PortfolioStore(tmp_path / 'snapshot.db')) as restored:
        replay = AccountReplay({'portfolio': DomainReplay(restored.db, reduce_capital_event, baseline=baseline)},
                               baseline=baseline)
        replay.catch_up(receiver)
        assert command_result(restored, 'paper', 'command') == expected
        assert (restored.cash('paper', 'A'), restored.cash('paper', 'B')) == (D(300), D(400))
    receiver.close()


def test_offsite_original_capital_result_replays_without_source_or_official_reads(tmp_path):
    import sqlite3

    path = tmp_path / 'source.db'
    baseline = SnapshotPosition('paper', 'g1', 0, None)
    funded(path)
    with closing(PortfolioStore(path)) as store:
        DomainReplay(store.db, reduce_capital_event, baseline=baseline)
        with closing(sqlite3.connect(tmp_path / 'snapshot.db')) as target:
            store.db.backup(target)
        manager = open_executor(store).capital
        with store.transaction():
            result, event = manager._prepare_change('paper', 'transfer', 'transfer',
                dict(kind='EXACT', source='A', destination='B', amount=D(100)))
        assert store.capital_balance('paper', 'A')[0] == 0
    receiver = BatchReceiver(tmp_path / 'offsite', stream='paper', generation='g1')
    receiver.accept(make_batch('paper', 'g1', 1, None, [event], NOW.isoformat()))
    path.unlink()
    with closing(PortfolioStore(tmp_path / 'snapshot.db')) as restored:
        replay = AccountReplay({'portfolio': DomainReplay(restored.db, reduce_capital_event,
                                                         baseline=baseline)}, baseline=baseline)
        replay.catch_up(receiver)
        replay.catch_up(receiver)
        assert (restored.cash('paper', 'A'), restored.cash('paper', 'B')) == (D(300), D(400))
        recovered = list(open_executor(restored).capital.history('paper'))
        assert len(recovered) == 1
        assert recovered[0]['result'] == result
        with pytest.raises(PermissionError, match='account event writer'):
            open_executor(restored).capital.change(account_id='paper', event_id='unadmitted',
                source='A', destination='B', amount=D(25))
    receiver.close()


def test_policy_changes_wait_for_offsite_ack_and_replay_without_source(tmp_path):
    import sqlite3
    from factorlab.trading.control import RiskLimits

    path = tmp_path / 'source.db'
    funded(path)
    baseline = SnapshotPosition('paper', 'g1', 0, None)
    receiver = BatchReceiver(tmp_path / 'offsite', stream='paper', generation='g1')
    with closing(PortfolioStore(path)) as store, closing(sqlite3.connect(tmp_path / 'admission.db')) as journal:
        store.db.execute('INSERT INTO capital_symbol_limits VALUES (?,?,?)', ('paper', 'AAPL', '100'))
        store.db.execute('INSERT INTO capital_symbols VALUES (?,?,?)', ('paper', 'AAPL', 'A'))
        store.db.execute("DELETE FROM capital_instances WHERE strategy='B'")
        store.db.execute('INSERT INTO portfolio_claims VALUES (?,?,?,?,?,?)',
                         ('paper', 'AAPL', 'A', '0', '0', '0'))
        store.db.execute('INSERT INTO portfolio_claim_history VALUES (?,?,?,?,?)',
                         ('paper', 'AAPL', 'B', '2', '9'))
        domain = DomainReplay(store.db, reduce_capital_event, baseline=baseline)
        with closing(sqlite3.connect(tmp_path / 'snapshot.db')) as snapshot:
            store.db.backup(snapshot)

        def lose_ack(payload):
            receiver.accept(payload)
            raise TimeoutError('lost policy ACK')

        admission = EventAdmission(journal, stream='paper', generation='g1', send=lose_ack)
        writer = AccountEventWriter(admission, AccountReplay({'portfolio': domain}, baseline=baseline))
        with pytest.raises(TimeoutError):
            store.capital_policy.pause('paper', 'A', event_writer=writer, event_id='pause-A')
        assert store.db.execute("SELECT paused FROM capital_instances WHERE strategy='A'").fetchone()[0] == 0
        def acknowledge(payload):
            receiver.accept(payload)
            return receiver.cursor()

        admission.send = acknowledge
        writer.resume()
        store.capital_policy.set_symbol_limit('paper', 'AAPL', D(75), event_writer=writer,
                                             event_id='limit-AAPL')
        limits = RiskLimits(D(50), D(60), D(200))
        admission.send = lose_ack
        with pytest.raises(TimeoutError):
            store.capital_policy.set_limits('paper', 'A', limits, event_writer=writer,
                                            event_id='instance-limits-A')
        assert store.capital_policy.symbol_limit('paper', 'AAPL') == D(75)
        assert store.db.execute('SELECT 1 FROM capital_limits').fetchone() is None
        admission.send = acknowledge
        store.capital_policy.set_limits('paper', 'A', limits, event_writer=writer,
                                        event_id='instance-limits-A')
        assert receiver.cursor().sequence == 3
        binding = dict(account_id='paper', strategy_id='B', instance_id='B', allocation_id='B-funded')
        with pytest.raises(PermissionError, match='account event writer'):
            store.capital_policy.bind(**binding)
        admission.send = lose_ack
        with pytest.raises(TimeoutError):
            store.capital_policy.bind(**binding, event_writer=writer, event_id='bind-B')
        assert store.db.execute("SELECT 1 FROM capital_instances WHERE strategy='B'").fetchone() is None
        admission.send = acknowledge
        store.capital_policy.bind(**binding, event_writer=writer, event_id='bind-B')
        assert receiver.cursor().sequence == 4
        configuration = dict(account_id='paper', currency='USD', budget=D(1000), fee_rate=D(0),
                             mode='SHARED_SAME_DIRECTION')
        with pytest.raises(PermissionError, match='account event writer'):
            store.capital_policy.configure(**configuration)
        admission.send = lose_ack
        with pytest.raises(TimeoutError):
            store.capital_policy.configure(**configuration, event_writer=writer, event_id='shared-mode')
        assert store.capital_policy.mode('paper').value == 'EXCLUSIVE'
        admission.send = acknowledge
        store.capital_policy.configure(**configuration, event_writer=writer, event_id='shared-mode')
        assert receiver.cursor().sequence == 5
        allocation = dict(account_id='paper', strategy_id='C', allocation_id='C-funded',
                          capital=D(100), initial_cash=D(100), official_equity=D(1000))
        executor = open_executor(store)
        with pytest.raises(ValueError, match='already belongs'):
            executor.allocate(**dict(allocation, allocation_id='A-funded'), event_writer=writer)
        assert admission.pending() is None
        assert receiver.cursor().sequence == 5
        with pytest.raises(PermissionError, match='account event writer'):
            executor.allocate(**allocation)
        admission.send = lose_ack
        with pytest.raises(TimeoutError):
            executor.allocate(**allocation, event_writer=writer)
        assert store.allocation_basis('paper', 'C') is None
        admission.send = acknowledge
        executor.allocate(**allocation, event_writer=writer)
        assert receiver.cursor().sequence == 6
        transfer = dict(account_id='paper', from_strategy='A', to_strategy='B', symbols=['AAPL'],
                        request_id='A-to-B')
        with pytest.raises(PermissionError, match='account event writer'):
            executor.transfer_flat_ownership(**transfer)
        admission.send = lose_ack
        with pytest.raises(TimeoutError):
            executor.transfer_flat_ownership(**transfer, event_writer=writer)
        assert store.db.execute('SELECT strategy FROM portfolio_claims').fetchone()[0] == 'A'
        admission.send = acknowledge
        executor.transfer_flat_ownership(**transfer, event_writer=writer)
        assert receiver.cursor().sequence == 7
        with pytest.raises(PermissionError, match='account event writer'):
            store.capital_policy.set_limits('paper', 'B', limits)
        with pytest.raises(PermissionError, match='account event writer'):
            store.capital_policy.pause('paper', 'A', False)
        with pytest.raises(PermissionError, match='account event writer'):
            store.capital_policy.set_symbol_limit('paper', 'AAPL', D(50))
    path.unlink()
    with closing(PortfolioStore(tmp_path / 'snapshot.db')) as restored:
        replay = AccountReplay({'portfolio': DomainReplay(restored.db, reduce_capital_event,
                                                         baseline=baseline)}, baseline=baseline)
        replay.catch_up(receiver)
        assert restored.db.execute("SELECT paused FROM capital_instances WHERE strategy='A'").fetchone()[0] == 1
        assert restored.capital_policy.symbol_limit('paper', 'AAPL') == D(60)
        assert restored.capital_policy.limits('paper', 'A') == limits
        assert restored.db.execute('SELECT strategy,quantity,initial_quantity,entry_price '
                                   'FROM portfolio_claims').fetchone() == ('B', '0', '2', '9')
        assert restored.db.execute('SELECT strategy FROM capital_symbols').fetchone()[0] == 'B'
        assert restored.db.execute('SELECT strategy FROM portfolio_claim_history').fetchone()[0] == 'A'
        assert restored.capital_policy.mode('paper').value == 'SHARED_SAME_DIRECTION'
        assert restored.allocation_basis('paper', 'C') == dict(
            allocation_id='C-funded', capital=D(100), initial_cash=D(100))
        assert restored.db.execute("SELECT started_at FROM capital_origins WHERE strategy='C'").fetchone()[0] == NOW.isoformat()
        assert restored.db.execute("SELECT instance,allocation,logical_strategy,paused "
                                   "FROM capital_instances WHERE strategy='B'").fetchone() == (
                                       'B', 'B-funded', 'B', 0)
    receiver.close()


def test_replicated_portfolio_cannot_bypass_incomplete_order_admission(tmp_path):
    path = tmp_path / 'quarantined.db'
    funded(path)
    with closing(PortfolioStore(path)) as store:
        executor = open_executor(store)
        intent = OrderIntent(Market.BINANCE_EQUITY, 'AAPL', Side.BUY, OrderType.LIMIT,
                             D(1), D(10), 'original-order')
        request = dict(account_id='paper', strategy_id='A', strategy_version='1',
                       trading_session='day', weights={}, owned_symbols=['AAPL'], now=NOW,
                       direct_orders=[(intent, D(10))])
        identity = executor.create(**request)
        DomainReplay(store.db, reduce_capital_event,
                     baseline=SnapshotPosition('paper', 'g1', 0, None))
        with pytest.raises(PermissionError, match='complete account admission'):
            executor.create(**dict(request, trading_session='later'))
        with pytest.raises(PermissionError, match='complete account admission'):
            executor.run_once(identity, equity=D(1000), quotes={}, rules={}, now=NOW)
        assert executor.adapter.posts == []
        assert store.db.execute('SELECT count(*) FROM portfolio_plans').fetchone()[0] == 1
        assert not store.orders(identity)[0]['attempted']


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
        store.db.create_function('crash_after_posting', 0, lambda: _exit(42))
        for operation in ('INSERT', 'UPDATE'):
            store.db.execute(f'CREATE TEMP TRIGGER crash_after_{operation.lower()} '
                             f'AFTER {operation} ON capital_balances '
                             'BEGIN SELECT crash_after_posting(); END')
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
