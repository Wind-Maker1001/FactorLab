"""Original fill ownership survives durable-ACK loss and source destruction."""
from contextlib import closing
from dataclasses import replace
from decimal import Decimal as D
import sqlite3

import pytest

from factorlab.integrations.ledger_replication import BatchReceiver, EventAdmission
from factorlab.integrations.ledger_replay import AccountEventWriter, AccountReplay, DomainReplay, SnapshotPosition
from factorlab.trading.contracts import Market, OrderEvent, OrderIntent, OrderStatus, OrderType, Side
from factorlab.trading.ledger_reducers import reduce_portfolio_event
from factorlab.trading.portfolio_execution import OrderAbsent, PortfolioStore
from test_dynamic_capital import funded, open_executor
from test_portfolio_execution import NOW


def test_original_partial_fill_is_admitted_before_ownership_changes(tmp_path):
    source = tmp_path / 'source.db'
    funded(source)
    baseline = SnapshotPosition('paper', 'g1', 0, None)
    with closing(BatchReceiver(tmp_path / 'offsite', stream='paper', generation='g1')) as receiver:
        with closing(PortfolioStore(source)) as store, closing(sqlite3.connect(
                tmp_path / 'admission.db', isolation_level=None)) as journal:
            executor = open_executor(store)
            intent = OrderIntent(Market.BINANCE_EQUITY, 'AAPL', Side.BUY, OrderType.LIMIT,
                                 D(2), D(10), 'original-buy')
            identity = executor.create(account_id='paper', strategy_id='A', strategy_version='1',
                trading_session='day', weights={}, owned_symbols=['AAPL'], now=NOW,
                direct_orders=[(intent, D(10))])
            domain = DomainReplay(store.db, reduce_portfolio_event, baseline=baseline)
            with closing(sqlite3.connect(tmp_path / 'snapshot.db')) as snapshot:
                store.db.backup(snapshot)

            def lost_ack(payload):
                assert not store.db.in_transaction
                receiver.accept(payload)
                raise TimeoutError('lost fill ACK')

            def acknowledge(payload):
                receiver.accept(payload)
                return receiver.cursor()

            admission = EventAdmission(journal, stream='paper', generation='g1', send=lost_ack)
            writer = AccountEventWriter(admission, AccountReplay({'portfolio': domain}, baseline=baseline))
            def unavailable(client_id):
                assert client_id == 'original-buy'
                raise ConnectionError('venue GET unavailable')

            with pytest.raises(TimeoutError):
                executor.reconcile('paper', event_writer=writer,
                                   observe=lambda: (executor.adapter.read_account(), unavailable))
            assert not store.orders(identity)[0]['uncertain']
            admission.send = acknowledge
            observed_account = executor.adapter.read_account()
            assert executor.reconcile('paper', event_writer=writer,
                observe=lambda: (observed_account, unavailable)) == observed_account
            assert store.orders(identity)[0]['uncertain']
            assert not store.orders(identity)[0]['attempted']
            assert store.db.execute('SELECT active FROM portfolio_orders').fetchone()[0] == 1
            admission.send = lost_ack
            event = OrderEvent('original-buy', 'venue-original', OrderStatus.PARTIALLY_FILLED,
                               D(1), D(10), NOW, 'partial', 'venue-fill-1')
            with pytest.raises(TimeoutError):
                executor.confirm_order(identity, 'original-buy', event, event_writer=writer)
            assert store.holdings('paper', 'A').get('AAPL', D(0)) == 0
            assert store.orders(identity)[0]['record'].snapshot.cumulative_filled_quantity == 0
            admission.send = acknowledge
            executor.confirm_order(identity, 'original-buy', event, event_writer=writer)
            executor.confirm_order(identity, 'original-buy', event, event_writer=writer)
            assert receiver.cursor().sequence == 2
            assert store.holdings('paper', 'A')['AAPL'] == 1
            with pytest.raises(ValueError, match='another original order'):
                executor.confirm_order(identity, 'original-buy',
                    replace(event, client_order_id='other-order', status=OrderStatus.UNKNOWN),
                    event_writer=writer)
            with pytest.raises(ValueError, match='confirmation identity changed'):
                executor.confirm_order(identity, 'original-buy',
                    replace(event, cumulative_fee=D('.1')), event_writer=writer)
            assert admission.pending() is None
            assert receiver.cursor().sequence == 2
            unknown = replace(event, status=OrderStatus.UNKNOWN, event_id='query-uncertain')
            executor.confirm_order(identity, 'original-buy', unknown, event_writer=writer)
            assert store.orders(identity)[0]['uncertain']
            assert store.holdings('paper', 'A')['AAPL'] == 1
            admission.send = lost_ack
            with pytest.raises(TimeoutError):
                executor.confirm_order(identity, 'original-buy', event, event_writer=writer)
            assert store.orders(identity)[0]['uncertain']
            admission.send = acknowledge
            executor.confirm_order(identity, 'original-buy', event, event_writer=writer)
            assert not store.orders(identity)[0]['uncertain']
            assert store.holdings('paper', 'A')['AAPL'] == 1
            assert store.db.execute('SELECT count(*) FROM portfolio_confirmations').fetchone()[0] == 1
            executor.confirm_order(identity, 'original-buy', unknown, event_writer=writer)
            assert store.orders(identity)[0]['uncertain']
            executor.confirm_order(identity, 'original-buy', event, event_writer=writer)
            assert not store.orders(identity)[0]['uncertain']
            assert store.holdings('paper', 'A')['AAPL'] == 1
            assert store.db.execute('SELECT count(*) FROM portfolio_confirmations').fetchone()[0] == 1
            def absent(client_id):
                raise OrderAbsent(client_id)

            admission.send = lost_ack
            with pytest.raises(TimeoutError):
                executor.reconcile_order(identity, 'original-buy', event_writer=writer, query_order=absent)
            assert not store.orders(identity)[0]['uncertain']
            assert 'AAPL' not in store.plan(identity)['reasons']
            admission.send = acknowledge
            executor.reconcile_order(identity, 'original-buy', event_writer=writer, query_order=absent)
            assert store.orders(identity)[0]['attempted']
            assert store.orders(identity)[0]['uncertain']
            assert store.plan(identity)['reasons']['AAPL'] == 'ORDER_ABSENCE_AFTER_SUBMIT'
            assert store.db.execute('SELECT active FROM portfolio_orders').fetchone()[0] == 1

            executor.confirm_order(identity, 'original-buy', event, event_writer=writer)
            assert store.orders(identity)[0]['attempted']
            assert not store.orders(identity)[0]['uncertain']
            assert store.db.execute('SELECT active FROM portfolio_orders').fetchone()[0] == 1
            completed = replace(event, status=OrderStatus.FILLED, cumulative_filled_quantity=D(2),
                                cumulative_fee=D('.2'), cumulative_filled_notional=D(20), event_id='venue-fill-2')
            admission.send = lost_ack
            with pytest.raises(TimeoutError):
                executor.confirm_order(identity, 'original-buy', completed, event_writer=writer)
            assert store.holdings('paper', 'A')['AAPL'] == 1
            admission.send = acknowledge
            store.db.execute("CREATE TEMP TRIGGER fail_fill_posting BEFORE UPDATE ON portfolio_claims "
                             "BEGIN SELECT RAISE(ABORT, 'injected fill application failure'); END")
            with pytest.raises(sqlite3.IntegrityError, match='injected fill application failure'):
                executor.confirm_order(identity, 'original-buy', completed, event_writer=writer)
            assert admission.pending() is not None
            assert receiver.cursor().sequence == 9
            assert store.holdings('paper', 'A')['AAPL'] == 1
            assert store.orders(identity)[0]['record'].snapshot.status == OrderStatus.PARTIALLY_FILLED
            assert store.db.execute('SELECT active FROM portfolio_orders').fetchone()[0] == 1
            assert store.db.execute('SELECT count(*) FROM portfolio_confirmations').fetchone()[0] == 1
            store.db.execute('DROP TRIGGER fail_fill_posting')
            executor.confirm_order(identity, 'original-buy', completed, event_writer=writer)
            executor.confirm_order(identity, 'original-buy', event, event_writer=writer)
            executor.confirm_order(identity, 'original-buy', completed, event_writer=writer)
            assert receiver.cursor().sequence == 9
            assert store.holdings('paper', 'A')['AAPL'] == 2
            assert store.orders(identity)[0]['filled_notional'] == D(20)
            assert not store.db.execute('SELECT active FROM portfolio_orders').fetchone()[0]
            assert executor.adapter.posts == []
            original_order = store.orders(identity)[0]
            original_plan = store.plan(identity)
        source.unlink()
        with closing(PortfolioStore(tmp_path / 'snapshot.db')) as restored:
            replay = AccountReplay({'portfolio': DomainReplay(restored.db, reduce_portfolio_event,
                                                             baseline=baseline)}, baseline=baseline)
            replay.catch_up(receiver)
            replay.catch_up(receiver)
            assert restored.holdings('paper', 'A')['AAPL'] == 2
            assert restored.orders(identity)[0] == original_order
            assert restored.plan(identity) == original_plan
            assert restored.orders(identity)[0]['record'].snapshot.cumulative_fee == D('.2')
            assert restored.db.execute('SELECT event FROM portfolio_confirmations ORDER BY event').fetchall() == [
                ('venue-fill-1',), ('venue-fill-2',)]


def test_original_attempt_survives_lost_ack_without_authorizing_retry(tmp_path):
    source = tmp_path / 'source.db'
    funded(source)
    baseline = SnapshotPosition('paper', 'g1', 0, None)
    with closing(BatchReceiver(tmp_path / 'offsite', stream='paper', generation='g1')) as receiver:
        with closing(PortfolioStore(source)) as store, closing(sqlite3.connect(
                tmp_path / 'admission.db', isolation_level=None)) as journal:
            executor = open_executor(store)
            intent = OrderIntent(Market.BINANCE_EQUITY, 'AAPL', Side.BUY, OrderType.LIMIT,
                                 D(2), D(10), 'original-buy')
            identity = executor.create(account_id='paper', strategy_id='A', strategy_version='1',
                trading_session='day', weights={}, owned_symbols=['AAPL'], now=NOW,
                direct_orders=[(intent, D(10))])
            domain = DomainReplay(store.db, reduce_portfolio_event, baseline=baseline)
            with closing(sqlite3.connect(tmp_path / 'snapshot.db')) as snapshot:
                store.db.backup(snapshot)

            def lost_ack(payload):
                assert not store.db.in_transaction
                receiver.accept(payload)
                raise TimeoutError('lost attempt ACK')

            def acknowledge(payload):
                assert not store.db.in_transaction
                receiver.accept(payload)
                return receiver.cursor()

            admission = EventAdmission(journal, stream='paper', generation='g1', send=lost_ack)
            writer = AccountEventWriter(admission, AccountReplay({'portfolio': domain}, baseline=baseline))
            with pytest.raises(TimeoutError, match='lost attempt ACK'):
                executor.admit_order_attempt(identity, 'original-buy', event_writer=writer)
            assert not store.orders(identity)[0]['attempted']
            assert receiver.cursor().sequence == 1
            admission.send = acknowledge
            assert not executor.admit_order_attempt(identity, 'original-buy', event_writer=writer)
            assert store.orders(identity)[0]['attempted']
            assert not executor.admit_order_attempt(identity, 'original-buy', event_writer=writer)
            assert receiver.cursor().sequence == 1
            assert executor.adapter.posts == []
            original = store.orders(identity)[0]
        source.unlink()
        with closing(PortfolioStore(tmp_path / 'snapshot.db')) as restored:
            replay = AccountReplay({'portfolio': DomainReplay(restored.db, reduce_portfolio_event,
                                                             baseline=baseline)}, baseline=baseline)
            replay.catch_up(receiver)
            assert restored.orders(identity)[0] == original
            assert restored.db.execute('SELECT active FROM portfolio_orders').fetchone()[0] == 1

