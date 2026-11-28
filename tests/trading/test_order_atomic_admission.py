"""One original event admits intent, policy ownership and cash reservation."""
from contextlib import closing
from decimal import Decimal as D
import sqlite3

import pytest

from factorlab.integrations.ledger_replication import BatchReceiver, EventAdmission
from factorlab.integrations.ledger_replay import AccountEventWriter, AccountReplay, DomainReplay, SnapshotPosition
from factorlab.trading.contracts import Market, OrderIntent, OrderType, Side
from factorlab.trading.ledger_reducers import reduce_portfolio_event
from factorlab.trading.portfolio_execution import PortfolioStore
from test_dynamic_capital import funded, open_executor
from test_portfolio_execution import NOW


@pytest.mark.parametrize('record_rejection', [False, True])
def test_atomic_original_plan_lost_ack_rollback_and_source_loss(tmp_path, record_rejection):
    source = tmp_path / 'source.db'
    funded(source)
    baseline = SnapshotPosition('paper', 'g1', 0, None)
    with closing(BatchReceiver(tmp_path / 'offsite', stream='paper', generation='g1')) as receiver:
        with closing(PortfolioStore(source)) as store, closing(sqlite3.connect(
                tmp_path / 'admission.db', isolation_level=None)) as journal:
            executor = open_executor(store)
            if record_rejection:
                from factorlab.trading.portfolio_risk import PortfolioRiskLimits
                store.portfolio_risk.configure('paper', PortfolioRiskLimits(stop_new_risk=True), {})
            domain = DomainReplay(store.db, reduce_portfolio_event, baseline=baseline)
            with closing(sqlite3.connect(tmp_path / 'snapshot.db')) as snapshot:
                store.db.backup(snapshot)
            intent = OrderIntent(Market.BINANCE_EQUITY, 'AAPL', Side.BUY, OrderType.LIMIT,
                                 D(2), D(10), 'atomic-buy')
            request = dict(account_id='paper', strategy_id='A', strategy_version='1',
                trading_session='day', weights={}, owned_symbols=['AAPL'], now=NOW,
                economic_multipliers={'AAPL': D(1)}, direct_orders=[(intent, D(10))],
                record_risk_rejection=record_rejection)

            def lost_ack(payload):
                assert not store.db.in_transaction
                assert store.db.execute('SELECT count(*) FROM portfolio_plans').fetchone()[0] == 0
                assert store.capital_policy.ownership('paper', 'AAPL') == ()
                assert store.cash('paper', 'A') == D(400)
                receiver.accept(payload)
                raise TimeoutError('lost atomic ACK')

            def acknowledge(payload):
                assert not store.db.in_transaction
                receiver.accept(payload)
                return receiver.cursor()

            admission = EventAdmission(journal, stream='paper', generation='g1', send=lost_ack)
            writer = AccountEventWriter(admission, AccountReplay({'portfolio': domain}, baseline=baseline))
            with pytest.raises(TimeoutError, match='lost atomic ACK'):
                executor.create(**request, event_writer=writer)
            assert store.db.execute('SELECT count(*) FROM portfolio_orders').fetchone()[0] == 0
            assert store.db.execute('SELECT count(*) FROM portfolio_claims').fetchone()[0] == 0
            assert store.db.execute('SELECT count(*) FROM portfolio_risk_multipliers').fetchone()[0] == 0
            admission.send = acknowledge
            store.db.execute("CREATE TEMP TRIGGER fail_order BEFORE INSERT ON portfolio_orders "
                             "BEGIN SELECT RAISE(ABORT, 'injected atomic application failure'); END")
            with pytest.raises(sqlite3.IntegrityError, match='injected atomic'):
                writer.resume()
            assert admission.pending() is not None
            assert store.db.execute('SELECT count(*) FROM portfolio_plans').fetchone()[0] == 0
            assert store.capital_policy.ownership('paper', 'AAPL') == ()
            store.db.execute('DROP TRIGGER fail_order')
            identity = executor.create(**request, event_writer=writer)
            assert executor.create(**request, event_writer=writer) == identity
            assert receiver.cursor().sequence == 1
            expected_cash = D(400) if record_rejection else D(380)
            assert store.cash('paper', 'A') == expected_cash
            original_confirmations = store.db.execute('SELECT * FROM portfolio_confirmations').fetchall()
            assert len(original_confirmations) == int(record_rejection)
            assert len(store.orders(identity)) == 1
            assert len(store.capital_policy.ownership('paper', 'AAPL')) == 1
            assert executor.adapter.posts == []
            original_plan, original_orders = store.plan(identity), store.orders(identity)
        source.unlink()
        with closing(PortfolioStore(tmp_path / 'snapshot.db')) as restored:
            replay = AccountReplay({'portfolio': DomainReplay(restored.db, reduce_portfolio_event,
                                                             baseline=baseline)}, baseline=baseline)
            replay.catch_up(receiver)
            replay.catch_up(receiver)
            assert restored.plan(identity) == original_plan
            assert restored.orders(identity) == original_orders
            assert restored.cash('paper', 'A') == expected_cash
            assert restored.db.execute('SELECT * FROM portfolio_confirmations').fetchall() == original_confirmations
            assert len(restored.capital_policy.ownership('paper', 'AAPL')) == 1
