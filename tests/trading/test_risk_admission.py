"""Original risk controls survive confirmation loss and source destruction."""
from contextlib import closing
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D
import sqlite3

import pytest

from factorlab.integrations.ledger_replication import BatchReceiver, EventAdmission
from factorlab.integrations.ledger_replay import AccountEventWriter, AccountReplay, DomainReplay, SnapshotPosition
from factorlab.trading.ledger_reducers import reduce_portfolio_event
from factorlab.trading.portfolio_execution import PortfolioExecutor, PortfolioStore
from factorlab.trading.portfolio_risk import PortfolioRiskLimits
from factorlab.trading.storage import dumps
from test_dynamic_capital import funded


def test_risk_controls_require_durable_confirmation_and_replay_original_configuration(tmp_path):
    source = tmp_path / 'source.db'
    funded(source)
    baseline = SnapshotPosition('paper', 'g1', 0, None)
    original = PortfolioRiskLimits(max_drawdown=D('.1'), stop_new_risk=True,
                                   paused_strategies=('A',), paused_symbols=('AAPL',))
    with closing(BatchReceiver(tmp_path / 'offsite', stream='paper', generation='g1')) as receiver:
        with closing(PortfolioStore(source)) as store, closing(sqlite3.connect(
                tmp_path / 'admission.db', isolation_level=None)) as journal:
            from test_portfolio_execution import NOW
            legacy_hour = int((NOW - timedelta(hours=1)).timestamp()) // 3600
            store.db.execute('INSERT INTO portfolio_risk_nav VALUES (?,?)',
                             ('paper', dumps(dict(at=NOW - timedelta(hours=1), peak=D(999),
                                                 start=D(999), day=legacy_hour // 24))))
            store.db.execute('INSERT INTO portfolio_risk_hour VALUES (?,?,?,?)',
                             ('paper', legacy_hour, (NOW - timedelta(hours=1)).isoformat(), '999'))
            domain = DomainReplay(store.db, reduce_portfolio_event, baseline=baseline)
            with closing(sqlite3.connect(tmp_path / 'snapshot.db')) as snapshot:
                store.db.backup(snapshot)

            def lost_ack(payload):
                assert not store.db.in_transaction
                receiver.accept(payload)
                raise TimeoutError('lost risk configuration ACK')

            def acknowledge(payload):
                assert not store.db.in_transaction
                receiver.accept(payload)
                return receiver.cursor()

            admission = EventAdmission(journal, stream='paper', generation='g1', send=lost_ack)
            writer = AccountEventWriter(admission, AccountReplay({'portfolio': domain}, baseline=baseline))
            with pytest.raises(TimeoutError):
                store.portfolio_risk.configure('paper', original, {'AAPL': D(3)},
                                               event_writer=writer, event_id='risk-pause')
            assert store.portfolio_risk.config('paper') is None
            admission.send = acknowledge
            store.portfolio_risk.configure('paper', original, {'AAPL': D(3)},
                                           event_writer=writer, event_id='risk-pause')
            assert store.portfolio_risk.config('paper') == (original, {'AAPL': D(3)})
            assert receiver.cursor().sequence == 1
            admission.send = lost_ack
            with pytest.raises(TimeoutError):
                store.portfolio_risk.record_multipliers('paper', 'A', {'AAPL': D(4)},
                                                       event_writer=writer, event_id='owner-multiplier')
            assert store.db.execute('SELECT 1 FROM portfolio_risk_multipliers').fetchone() is None
            admission.send = acknowledge
            store.portfolio_risk.record_multipliers('paper', 'A', {'AAPL': D(4)},
                                                   event_writer=writer, event_id='owner-multiplier')
            assert receiver.cursor().sequence == 2
            from test_portfolio_execution import Broker, NOW
            account = replace(Broker().read_account(), cumulative_external_cash_flow=D(100))
            admission.send = lost_ack
            with pytest.raises(TimeoutError):
                store.portfolio_risk._nav_history(account, D(100), NOW, original, True,
                                                 event_writer=writer, event_id='nav-origin')
            assert store.db.execute('SELECT nav FROM portfolio_risk_hour').fetchone()[0] == '999'
            admission.send = acknowledge
            assert store.portfolio_risk._nav_history(account, D(100), NOW, original, True,
                                                    event_writer=writer, event_id='nav-origin') == (D(0), D(0), None)
            assert receiver.cursor().sequence == 3
            later = NOW + timedelta(hours=1)
            with pytest.raises(PermissionError, match='account event writer'):
                store.portfolio_risk._nav_history(account, D(90), later, original, True)
            assert store.portfolio_risk._nav_history(account, D(90), later, original, True,
                                                    event_writer=writer, event_id='nav-loss') == (D('.1'), D(10), None)
            latest = later + timedelta(hours=1)
            observation = replace(account, observed_at=latest)
            executor = PortfolioExecutor(None, store, clock=lambda: latest)
            admission.send = lost_ack
            with pytest.raises(TimeoutError):
                executor.enforce_portfolio_risk('paper', official_equity=D(80), account=observation,
                                                event_writer=writer, event_id='observed-loss')
            admission.send = acknowledge
            snapshot = executor.enforce_portfolio_risk(
                'paper', official_equity=D(80), account=observation,
                event_writer=writer, event_id='observed-loss')
            assert snapshot.drawdown == D('.2')
            assert snapshot.daily_loss == D(20)
            from test_dynamic_capital import CashBroker
            report_at = latest + timedelta(hours=1)
            broker = CashBroker(cash=D(1000))
            broker.read_account = lambda: replace(observation, observed_at=report_at)
            reporter = PortfolioExecutor(broker, store, clock=lambda: report_at)
            admission.send = lost_ack
            with pytest.raises(TimeoutError):
                reporter.portfolio_risk_snapshot('paper', official_equity=D(70),
                                                 event_writer=writer, event_id='reported-loss')
            admission.send = acknowledge
            report = reporter.portfolio_risk_snapshot('paper', official_equity=D(70),
                                                      event_writer=writer, event_id='reported-loss')
            assert report.drawdown == D('.3')
            assert report.daily_loss == D(30)
            retained_at = NOW + timedelta(hours=750)
            admission.send = lost_ack
            with pytest.raises(TimeoutError):
                store.portfolio_risk._nav_history(account, D(60), retained_at, original, True,
                                                 event_writer=writer, event_id='retained-loss')
            assert store.db.execute('SELECT count(*) FROM portfolio_risk_hour').fetchone()[0] == 4
            admission.send = acknowledge
            assert store.portfolio_risk._nav_history(account, D(60), retained_at, original, True,
                                                    event_writer=writer, event_id='retained-loss') == (D('.4'), D(0), None)
            assert store.db.execute('SELECT count(*) FROM portfolio_risk_hour').fetchone()[0] == 1
            nav_payload = store.db.execute('SELECT payload FROM portfolio_risk_nav').fetchone()[0]
            hours = store.db.execute('SELECT hour,observed,nav FROM portfolio_risk_hour ORDER BY hour').fetchall()
            with pytest.raises(PermissionError, match='account event writer'):
                store.portfolio_risk.record_multipliers('paper', 'A', {'AAPL': D(5)})
            with pytest.raises(PermissionError, match='account event writer'):
                store.portfolio_risk.configure('paper', replace(original, stop_new_risk=False), None)
        source.unlink()
        with closing(PortfolioStore(tmp_path / 'snapshot.db')) as restored:
            replay = AccountReplay({'portfolio': DomainReplay(restored.db, reduce_portfolio_event,
                                                             baseline=baseline)}, baseline=baseline)
            replay.catch_up(receiver)
            assert restored.portfolio_risk.config('paper') == (original, {'AAPL': D(3)})
            assert restored.portfolio_risk._multipliers('paper', {}) == ({'AAPL': D(4)}, set())
            assert restored.db.execute('SELECT payload FROM portfolio_risk_nav').fetchone()[0] == nav_payload
            assert restored.db.execute('SELECT hour,observed,nav FROM portfolio_risk_hour ORDER BY hour').fetchall() == hours
