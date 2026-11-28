"""Policy ownership admissions preserve original ceilings across source loss."""
from contextlib import closing
import sqlite3

import pytest

from factorlab.integrations.ledger_replication import BatchReceiver, EventAdmission
from factorlab.integrations.ledger_replay import AccountEventWriter, AccountReplay, DomainReplay, SnapshotPosition
from factorlab.trading.ledger_reducers import reduce_portfolio_event
from factorlab.trading.portfolio_execution import PortfolioStore
from test_dynamic_capital import funded


def test_policy_claim_release_wait_for_ack_and_replay_occurrences(tmp_path):
    source = tmp_path / 'source.db'
    funded(source)
    baseline = SnapshotPosition('paper', 'g1', 0, None)
    with closing(BatchReceiver(tmp_path / 'offsite', stream='paper', generation='g1')) as receiver:
        with closing(PortfolioStore(source)) as store, closing(sqlite3.connect(
                tmp_path / 'admission.db', isolation_level=None)) as journal:
            policy = store.capital_policy
            domain = DomainReplay(store.db, reduce_portfolio_event, baseline=baseline)
            with closing(sqlite3.connect(tmp_path / 'snapshot.db')) as snapshot:
                store.db.backup(snapshot)

            def lost_ack(payload):
                assert not store.db.in_transaction
                receiver.accept(payload)
                raise TimeoutError('lost ownership ACK')

            def acknowledge(payload):
                assert not store.db.in_transaction
                receiver.accept(payload)
                return receiver.cursor()

            admission = EventAdmission(journal, stream='paper', generation='g1', send=lost_ack)
            writer = AccountEventWriter(admission, AccountReplay({'portfolio': domain}, baseline=baseline))
            with pytest.raises(TimeoutError):
                policy.claim('paper', 'A', 'AAPL', event_writer=writer)
            assert policy.ownership('paper', 'AAPL') == ()
            with pytest.raises(KeyError):
                policy.symbol_limit('paper', 'AAPL')
            with pytest.raises(PermissionError, match='account event writer'):
                policy.claim('paper', 'A', 'AAPL')
            admission.send = acknowledge
            policy.claim('paper', 'A', 'AAPL', event_writer=writer)
            assert len(policy.ownership('paper', 'AAPL')) == 1
            original_ceiling = policy.symbol_limit('paper', 'AAPL')
            policy.claim('paper', 'A', 'AAPL', event_writer=writer)
            assert receiver.cursor().sequence == 1
            with pytest.raises(ValueError, match='ownership conflict'):
                policy.claim('paper', 'B', 'AAPL', event_writer=writer)
            assert admission.pending() is None
            admission.send = lost_ack
            with pytest.raises(TimeoutError):
                policy.release('paper', 'A', 'AAPL', event_writer=writer)
            assert len(policy.ownership('paper', 'AAPL')) == 1
            with pytest.raises(PermissionError, match='account event writer'):
                policy.release('paper', 'A', 'AAPL')
            admission.send = acknowledge
            policy.release('paper', 'A', 'AAPL', event_writer=writer)
            assert policy.ownership('paper', 'AAPL') == ()
            assert policy.symbol_limit('paper', 'AAPL') == original_ceiling
            policy.claim('paper', 'A', 'AAPL', event_writer=writer)
            policy.release('paper', 'A', 'AAPL', event_writer=writer)
            policy.claim('paper', 'B', 'AAPL', event_writer=writer)
            assert receiver.cursor().sequence == 5
            original_owners = policy.ownership('paper', 'AAPL')
            resulting_ceiling = policy.symbol_limit('paper', 'AAPL')
            assert resulting_ceiling <= original_ceiling
            assert store.holdings('paper', 'A') == {}
        source.unlink()
        with closing(PortfolioStore(tmp_path / 'snapshot.db')) as restored:
            replay = AccountReplay({'portfolio': DomainReplay(restored.db, reduce_portfolio_event,
                                                             baseline=baseline)}, baseline=baseline)
            replay.catch_up(receiver)
            replay.catch_up(receiver)
            assert restored.capital_policy.ownership('paper', 'AAPL') == original_owners
            assert restored.capital_policy.symbol_limit('paper', 'AAPL') == resulting_ceiling
            assert restored.holdings('paper', 'A') == {}
