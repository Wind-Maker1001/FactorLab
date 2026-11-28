"""Transfer occurrences retain business identity across retries and ownership cycles."""
from contextlib import closing
from decimal import Decimal as D
import sqlite3

import pytest

from factorlab.integrations.ledger_replication import BatchReceiver, EventAdmission
from factorlab.integrations.ledger_replay import AccountEventWriter, AccountReplay, DomainReplay, SnapshotPosition
from factorlab.trading.ledger_reducers import reduce_portfolio_event
from factorlab.trading.portfolio_execution import PortfolioStore
from test_dynamic_capital import funded, open_executor


@pytest.mark.parametrize('replicated', [False, True])
def test_transfer_cycles_retries_and_original_limits(tmp_path, replicated):
    source = tmp_path / 'source.db'
    funded(source)
    baseline = SnapshotPosition('paper', 'g1', 0, None)
    with closing(BatchReceiver(tmp_path / 'offsite', stream='paper', generation='g1')) as receiver:
        with closing(PortfolioStore(source)) as store, closing(sqlite3.connect(
                tmp_path / 'admission.db', isolation_level=None)) as journal:
            executor = open_executor(store)
            store.db.execute('INSERT INTO capital_symbol_limits VALUES (?,?,?)', ('paper', 'AAPL', '100'))
            store.db.execute('INSERT INTO capital_symbols VALUES (?,?,?)', ('paper', 'AAPL', 'A'))
            store.db.execute('INSERT INTO portfolio_claims VALUES (?,?,?,?,?,?)',
                             ('paper', 'AAPL', 'A', '0', '0', '0'))
            writer = None
            if replicated:
                domain = DomainReplay(store.db, reduce_portfolio_event, baseline=baseline)
                with closing(sqlite3.connect(tmp_path / 'snapshot.db')) as snapshot:
                    store.db.backup(snapshot)

                def acknowledge(payload):
                    assert not store.db.in_transaction
                    receiver.accept(payload)
                    return receiver.cursor()

                admission = EventAdmission(journal, stream='paper', generation='g1', send=acknowledge)
                writer = AccountEventWriter(admission, AccountReplay({'portfolio': domain}, baseline=baseline))

            def transfer(request, previous, owner):
                executor.transfer_flat_ownership(account_id='paper', from_strategy=previous,
                    to_strategy=owner, symbols=['AAPL'], request_id=request, event_writer=writer)

            for request, previous, owner in [('first', 'A', 'B'), ('return', 'B', 'A'), ('third', 'A', 'B')]:
                if replicated:
                    def lost_ack(payload):
                        receiver.accept(payload)
                        raise TimeoutError('lost transfer ACK')
                    admission.send = lost_ack
                    with pytest.raises(TimeoutError, match='lost transfer ACK'):
                        transfer(request, previous, owner)
                    assert store.db.execute('SELECT strategy FROM portfolio_claims').fetchone()[0] == previous
                    assert store.capital_policy.ownership('paper', 'AAPL')[0].strategy_id == previous
                    admission.send = acknowledge
                transfer(request, previous, owner)
                transfer(request, previous, owner)
                assert store.db.execute('SELECT strategy FROM portfolio_claims').fetchone()[0] == owner
                assert store.capital_policy.ownership('paper', 'AAPL')[0].strategy_id == owner
                assert store.capital_policy.symbol_limit('paper', 'AAPL') == D(100)
                if request == 'return':
                    transfer('first', 'A', 'B')
                    assert store.db.execute('SELECT strategy FROM portfolio_claims').fetchone()[0] == 'A'
            with pytest.raises(ValueError, match='conflicts'):
                transfer('first', 'B', 'A')
            assert store.db.execute("SELECT count(*) FROM events WHERE id LIKE 'flat-ownership:%'").fetchone()[0] == 3
            assert (store.cash('paper', 'A'), store.cash('paper', 'B')) == (D(400), D(300))
            if replicated:
                assert receiver.cursor().sequence == 3
        if replicated:
            source.unlink()
            with closing(PortfolioStore(tmp_path / 'snapshot.db')) as restored:
                replay = AccountReplay({'portfolio': DomainReplay(restored.db, reduce_portfolio_event,
                                                                 baseline=baseline)}, baseline=baseline)
                replay.catch_up(receiver)
                replay.catch_up(receiver)
                assert restored.db.execute('SELECT strategy FROM portfolio_claims').fetchone()[0] == 'B'
                assert restored.capital_policy.ownership('paper', 'AAPL')[0].strategy_id == 'B'
                assert restored.capital_policy.symbol_limit('paper', 'AAPL') == D(100)
                assert (restored.cash('paper', 'A'), restored.cash('paper', 'B')) == (D(400), D(300))
