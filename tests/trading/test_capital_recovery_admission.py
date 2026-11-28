"""Economic preparation admits recovered order observations before local posting."""
from contextlib import closing
from decimal import Decimal as D
import sqlite3

import pytest

from factorlab.integrations.ledger_replication import BatchReceiver, EventAdmission
from factorlab.integrations.ledger_replay import AccountEventWriter, AccountReplay, DomainReplay, SnapshotPosition
from factorlab.trading.capital_management import CapitalManagement
from factorlab.trading.contracts import Market, OrderIntent, OrderStatus, OrderType, Side
from factorlab.trading.ledger_reducers import reduce_portfolio_event
from factorlab.trading.portfolio_execution import PortfolioStore
from test_dynamic_capital import funded, open_executor
from test_portfolio_execution import NOW


@pytest.mark.parametrize('operation', ['change', 'snapshot', 'performance', 'ownership'])
def test_economic_entry_admits_new_fill_before_preparation_and_source_loss(tmp_path, operation):
    source = tmp_path / 'source.db'
    funded(source)
    baseline = SnapshotPosition('paper', 'g1', 0, None)
    with closing(BatchReceiver(tmp_path / 'offsite', stream='paper', generation='g1')) as receiver:
        with closing(PortfolioStore(source)) as store, closing(sqlite3.connect(
                tmp_path / 'admission.db', isolation_level=None)) as journal:
            executor = open_executor(store)
            intent = OrderIntent(Market.BINANCE_EQUITY, 'AAPL', Side.BUY, OrderType.LIMIT,
                                 D(2), D(10), 'new-fill')
            identity = executor.create(account_id='paper', strategy_id='A', strategy_version='1',
                trading_session='day', weights={}, owned_symbols=['AAPL'], now=NOW,
                direct_orders=[(intent, D(10))])
            domain = DomainReplay(store.db, reduce_portfolio_event, baseline=baseline)
            with closing(sqlite3.connect(tmp_path / 'snapshot.db')) as snapshot:
                store.db.backup(snapshot)
            executor.adapter.orders[intent.client_order_id] = intent
            executor.adapter.confirm(intent.client_order_id, D(2), OrderStatus.FILLED)

            def lost_ack(payload):
                assert not store.db.in_transaction
                receiver.accept(payload)
                raise TimeoutError('lost recovery ACK')

            admission = EventAdmission(journal, stream='paper', generation='g1', send=lost_ack)
            writer = AccountEventWriter(admission, AccountReplay({'portfolio': domain}, baseline=baseline))
            manager = CapitalManagement(executor, event_writer=writer)

            def invoke():
                if operation == 'change':
                    return manager.change(account_id='paper', event_id='move', source='A',
                                          destination='B', amount=D(10))
                if operation == 'snapshot':
                    return manager.snapshot('paper')
                if operation == 'performance':
                    return manager.performance('paper', 'A')
                return executor.transfer_flat_ownership(account_id='paper', from_strategy='A',
                    to_strategy='B', symbols=['AAPL'], request_id='handoff', event_writer=writer)

            with pytest.raises(TimeoutError, match='lost recovery ACK'):
                invoke()
            assert store.holdings('paper', 'A').get('AAPL', D(0)) == 0
            assert store.cash('paper', 'A', reserve=False) == D(400)
            assert store.db.execute('SELECT count(*) FROM portfolio_confirmations').fetchone()[0] == 0
            assert store.orders(identity)[0]['record'].snapshot.cumulative_filled_quantity == 0

            def acknowledge(payload):
                assert not store.db.in_transaction
                receiver.accept(payload)
                return receiver.cursor()

            admission.send = acknowledge
            if operation == 'ownership':
                with pytest.raises(ValueError, match='flat sleeve|completed predecessor'):
                    invoke()
            else:
                invoke()
            assert store.holdings('paper', 'A')['AAPL'] == D(2)
            assert store.holdings('paper', 'B').get('AAPL', D(0)) == 0
            expected_cash = (D(370), D(310)) if operation == 'change' else (D(380), D(300))
            assert (store.cash('paper', 'A'), store.cash('paper', 'B')) == expected_cash
            order = store.orders(identity)[0]
            confirmations = store.db.execute('SELECT * FROM portfolio_confirmations').fetchall()
        source.unlink()
        with closing(PortfolioStore(tmp_path / 'snapshot.db')) as restored:
            replay = AccountReplay({'portfolio': DomainReplay(restored.db, reduce_portfolio_event,
                                                             baseline=baseline)}, baseline=baseline)
            replay.catch_up(receiver)
            replay.catch_up(receiver)
            assert restored.holdings('paper', 'A')['AAPL'] == D(2)
            assert restored.holdings('paper', 'B').get('AAPL', D(0)) == 0
            assert (restored.cash('paper', 'A'), restored.cash('paper', 'B')) == expected_cash
            assert restored.orders(identity)[0] == order
            assert restored.db.execute('SELECT * FROM portfolio_confirmations').fetchall() == confirmations
